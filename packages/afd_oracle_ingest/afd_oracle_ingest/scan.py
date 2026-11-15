"""Vulnerability scanning: run established scanners in Docker over a clean export of HEAD and keep their findings.

Every scanner runs in a container against a **read-only** copy of the code (``git archive``); nothing is executed
from the repository. Scanners differ in what leaves the machine, and that decides whether they run by default:

=============  =====================================================  ================================================
scanner        finds                                                  network
=============  =====================================================  ================================================
semgrep        code vulnerabilities, many languages (p/default)       downloads rules; ``--metrics=off``, sends nothing
bandit         Python security issues                                 none
trivy          vulnerable dependencies, IaC misconfig, secrets        downloads its vulnerability DB; sends nothing
gitleaks       hard-coded secrets                                     none
checkov        Terraform / Kubernetes / Helm / Docker / CI misconfig  none
hadolint       Dockerfile issues                                      none
osv-scanner    vulnerable dependencies                                **sends package names to osv.dev** (opt-in)
snyk           vulnerable dependencies (+ ``snyk code``: source)      **sends manifests (and source) to Snyk** (opt-in,
                                                                      needs ``SNYK_TOKEN``)
=============  =====================================================  ================================================

Findings are normalised (scanner, rule, severity, title, path, line, package, version, fixed version, CVE, CWE),
mapped to the enclosing symbol at HEAD, stored in ``vuln_findings``, projected to Neo4j as ``Vulnerability`` nodes
(``AFFECTS`` the file, ``IN_SYMBOL`` the function) and embedded into the ``security_findings`` Qdrant collection.
Secret values are never stored: only the rule, file and line.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store

from .sandbox import _docker, _export, _host_path, _limits, docker_bin

SCHEMA = """
CREATE TABLE IF NOT EXISTS vuln_findings (
  id TEXT PRIMARY KEY, scanner TEXT, rule TEXT, severity TEXT, title TEXT, message TEXT, path TEXT, line INTEGER,
  end_line INTEGER, package TEXT, version TEXT, fixed TEXT, cve TEXT, cwe TEXT, url TEXT, symbol TEXT, commit_hash TEXT,
  found_at REAL);
CREATE INDEX IF NOT EXISTS vf_path ON vuln_findings(path);
CREATE INDEX IF NOT EXISTS vf_sev ON vuln_findings(severity);
CREATE TABLE IF NOT EXISTS scan_runs (scanner TEXT PRIMARY KEY, ran_at REAL, commit_hash TEXT, status TEXT,
  findings INTEGER, seconds REAL, detail TEXT);
"""
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
_SEV = {"critical": "critical", "high": "high", "error": "high", "medium": "medium", "moderate": "medium",
        "warning": "medium", "low": "low", "info": "info", "note": "info", "style": "info", "unknown": "info",
        "negligible": "info"}


def sev(s: Any) -> str:
    return _SEV.get(str(s or "").strip().lower(), "medium")


@dataclass
class Finding:
    scanner: str
    rule: str
    severity: str
    title: str
    path: str = ""
    line: int | None = None
    end_line: int | None = None
    message: str = ""
    package: str = ""
    version: str = ""
    fixed: str = ""
    cve: str = ""
    cwe: str = ""
    url: str = ""

    @property
    def id(self) -> str:
        key = "|".join(str(x) for x in (self.scanner, self.rule, self.path, self.line, self.package, self.version))
        return hashlib.sha1(key.encode()).hexdigest()[:20]


def _rel(p: str) -> str:
    """Container path -> repository-relative path."""
    p = (p or "").replace("\\", "/")
    p = p.removeprefix("/src/").removeprefix("./")
    return p.removeprefix("/src") if p == "/src" else p


# ---------------------------------------------------------------------------- parsers
def parse_semgrep(data: dict) -> list[Finding]:
    out = []
    for r in data.get("results") or []:
        ex = r.get("extra") or {}
        md = ex.get("metadata") or {}
        cwe = md.get("cwe") or []
        refs = md.get("references") or []
        out.append(Finding("semgrep", r.get("check_id", ""), sev(md.get("impact") or ex.get("severity")),
                           (ex.get("message") or "").split("\n")[0][:200], _rel(r.get("path")),
                           (r.get("start") or {}).get("line"), (r.get("end") or {}).get("line"),
                           (ex.get("message") or "")[:1500], cwe=", ".join(cwe if isinstance(cwe, list) else [cwe])[:200],
                           url=(refs[0] if refs else md.get("source", "")) or ""))
    return out


def parse_trivy(data: dict) -> list[Finding]:
    out = []
    for res in data.get("Results") or []:
        target = _rel(res.get("Target", ""))
        for v in res.get("Vulnerabilities") or []:
            out.append(Finding("trivy", v.get("VulnerabilityID", ""), sev(v.get("Severity")),
                               (v.get("Title") or v.get("VulnerabilityID") or "")[:200], target, None, None,
                               (v.get("Description") or "")[:1500], v.get("PkgName", ""), v.get("InstalledVersion", ""),
                               v.get("FixedVersion", "") or "", v.get("VulnerabilityID", "")
                               if str(v.get("VulnerabilityID", "")).startswith("CVE") else "",
                               ", ".join(v.get("CweIDs") or [])[:200], v.get("PrimaryURL", "") or ""))
        for m in res.get("Misconfigurations") or []:
            if m.get("Status") not in (None, "FAIL"):
                continue
            cm = m.get("CauseMetadata") or {}
            out.append(Finding("trivy", m.get("AVDID") or m.get("ID", ""), sev(m.get("Severity")),
                               (m.get("Title") or "")[:200], target, cm.get("StartLine"), cm.get("EndLine"),
                               (m.get("Message") or m.get("Description") or "")[:1500], url=m.get("PrimaryURL", "") or ""))
        for s in res.get("Secrets") or []:  # never keep the matched value
            out.append(Finding("trivy", s.get("RuleID", ""), sev(s.get("Severity")), (s.get("Title") or "")[:200],
                               target, s.get("StartLine"), s.get("EndLine"), f"{s.get('Category', '')} secret"))
    return out


def parse_gitleaks(data: list) -> list[Finding]:
    return [Finding("gitleaks", r.get("RuleID", ""), "high", (r.get("Description") or "secret")[:200], _rel(r.get("File")),
                    r.get("StartLine"), r.get("EndLine"), "hard-coded secret (value not stored)")
            for r in data or []]


def parse_bandit(data: dict) -> list[Finding]:
    out = []
    for r in data.get("results") or []:
        cwe = r.get("issue_cwe") or {}
        rng = r.get("line_range") or []
        out.append(Finding("bandit", f"{r.get('test_id', '')}:{r.get('test_name', '')}", sev(r.get("issue_severity")),
                           (r.get("issue_text") or "")[:200], _rel(r.get("filename")), r.get("line_number"),
                           rng[-1] if rng else None, f"confidence {r.get('issue_confidence', '')}",
                           cwe=f"CWE-{cwe['id']}" if cwe.get("id") else "", url=r.get("more_info", "") or ""))
    return out


def parse_checkov(data: Any) -> list[Finding]:
    out = []
    for block in (data if isinstance(data, list) else [data]):
        for c in ((block or {}).get("results") or {}).get("failed_checks") or []:
            rng = c.get("file_line_range") or [None, None]
            out.append(Finding("checkov", c.get("check_id", ""), sev(c.get("severity") or "medium"),
                               (c.get("check_name") or "")[:200], _rel(c.get("repo_file_path") or c.get("file_path")).lstrip("/"),
                               rng[0], rng[1] if len(rng) > 1 else None, f"resource {c.get('resource', '')}",
                               url=c.get("guideline", "") or ""))
    return out


def parse_hadolint(data: list) -> list[Finding]:
    return [Finding("hadolint", r.get("code", ""), sev(r.get("level")), (r.get("message") or "")[:200],
                    _rel(r.get("file")), r.get("line"), r.get("line"), r.get("message", ""),
                    url=f"https://github.com/hadolint/hadolint/wiki/{r.get('code')}" if str(r.get("code", "")).startswith("DL")
                    else "") for r in data or []]


def parse_osv(data: dict) -> list[Finding]:
    out = []
    for res in data.get("results") or []:
        src = _rel((res.get("source") or {}).get("path", ""))
        for pkg in res.get("packages") or []:
            p = pkg.get("package") or {}
            sevs = {g_id: g.get("max_severity") for g in pkg.get("groups") or [] for g_id in g.get("ids") or []}
            for v in pkg.get("vulnerabilities") or []:
                cves = [a for a in [v.get("id", ""), *(v.get("aliases") or [])] if str(a).startswith("CVE")]
                score = sevs.get(v.get("id"))
                level = ("critical" if score and float(score) >= 9 else "high" if score and float(score) >= 7 else
                         "medium" if score and float(score) >= 4 else "low") if score not in (None, "") else \
                    sev((v.get("database_specific") or {}).get("severity"))
                out.append(Finding("osv-scanner", v.get("id", ""), level, (v.get("summary") or v.get("id", ""))[:200], src,
                                   message=(v.get("details") or "")[:1500], package=p.get("name", ""),
                                   version=p.get("version", ""), cve=cves[0] if cves else "",
                                   url=f"https://osv.dev/vulnerability/{v.get('id')}"))
    return out


def parse_snyk(data: Any) -> list[Finding]:
    out = []
    for proj in (data if isinstance(data, list) else [data]):
        target = _rel((proj or {}).get("displayTargetFile", ""))
        for v in (proj or {}).get("vulnerabilities") or []:
            ids = v.get("identifiers") or {}
            out.append(Finding("snyk", v.get("id", ""), sev(v.get("severity")), (v.get("title") or "")[:200], target,
                               package=v.get("packageName", ""), version=v.get("version", ""),
                               fixed=", ".join(v.get("fixedIn") or [])[:200], cve=", ".join(ids.get("CVE") or [])[:200],
                               cwe=", ".join(ids.get("CWE") or [])[:200], url=f"https://security.snyk.io/vuln/{v.get('id')}"))
    return out


def parse_sarif(data: dict, scanner: str) -> list[Finding]:
    out = []
    for run in data.get("runs") or []:
        for r in run.get("results") or []:
            loc = ((r.get("locations") or [{}])[0].get("physicalLocation") or {})
            region = loc.get("region") or {}
            out.append(Finding(scanner, r.get("ruleId", ""), sev(r.get("level")),
                               ((r.get("message") or {}).get("text") or "")[:200],
                               _rel((loc.get("artifactLocation") or {}).get("uri", "")), region.get("startLine"),
                               region.get("endLine"), ((r.get("message") or {}).get("text") or "")[:1500]))
    return out


# ---------------------------------------------------------------------------- scanner definitions
CODE_RX = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|java|rb|php|cs|kt|rs|c|cc|cpp|scala|swift)$")
IAC_RX = re.compile(r"(?i)\.(tf|tfvars|ya?ml|json)$|(^|/)Dockerfile|(^|/)\.github/workflows/")


@dataclass
class Scanner:
    name: str
    image: str
    network: str                     # "none" | "download" (rules / DB only) | "sends" (data leaves the machine)
    applies: Callable[[list[str]], bool]
    command: Callable[[list[str]], list[str]]
    parse: Callable[[Any], list[Finding]]
    output_file: str | None = None   # read /out/<file> instead of stdout
    needs_token: str | None = None
    extra_mounts: list[tuple[str, str]] = field(default_factory=list)
    pip: list[str] | None = None     # tools installed into a cached venv in a python image


def _has(rx: str) -> Callable[[list[str]], bool]:
    c = re.compile(rx)
    return lambda paths: any(c.search(p) for p in paths)


SCANNERS: dict[str, Scanner] = {s.name: s for s in [
    Scanner("semgrep", "semgrep/semgrep:1.97.0", "download", lambda ps: any(CODE_RX.search(p) for p in ps),
            lambda ps: ["semgrep", "scan", "--config", "p/default", "--json", "--metrics=off", "--quiet",
                        "--timeout", "20", "--max-target-bytes", "1000000", "/src"], parse_semgrep),
    Scanner("bandit", "python:3.12-slim", "none", _has(r"\.py$"),
            lambda ps: ["/tools/bin/bandit", "-r", "/src", "-f", "json", "-q", "--exit-zero",
                        "-x", "/src/tests,/src/test,/src/docs"], parse_bandit, pip=["bandit==1.7.10"]),
    Scanner("trivy", "aquasec/trivy:0.57.1", "download", lambda ps: True,
            lambda ps: ["fs", "--scanners", "vuln,misconfig,secret", "--format", "json", "--quiet",
                        "--cache-dir", "/cache", "/src"], parse_trivy, extra_mounts=[("trivy-cache", "/cache")]),
    Scanner("gitleaks", "zricethezav/gitleaks:v8.21.2", "none", lambda ps: True,
            lambda ps: ["detect", "--source", "/src", "--no-git", "--report-format", "json", "--report-path",
                        "/out/gitleaks.json", "--exit-code", "0", "--no-banner", "--redact"], parse_gitleaks,
            output_file="gitleaks.json"),
    Scanner("checkov", "python:3.12-slim", "none", _has(IAC_RX.pattern),
            lambda ps: ["/tools/bin/checkov", "-d", "/src", "-o", "json", "--quiet", "--compact", "--skip-download",
                        "--framework", "terraform,kubernetes,helm,dockerfile,github_actions,cloudformation"],
            parse_checkov, pip=["checkov>=3.2,<3.3"]),
    Scanner("hadolint", "hadolint/hadolint:v2.12.0", "none", _has(r"(^|/)Dockerfile[^/]*$"),
            lambda ps: ["hadolint", "-f", "json", "--no-fail",
                        *[f"/src/{p}" for p in ps if re.search(r"(^|/)Dockerfile[^/]*$", p)][:200]], parse_hadolint),
    Scanner("osv-scanner", "ghcr.io/google/osv-scanner:v1.9.1", "sends", lambda ps: True,
            lambda ps: ["--format", "json", "-r", "/src"], parse_osv),
    Scanner("snyk", "snyk/snyk:alpine", "sends", lambda ps: True,
            lambda ps: ["snyk", "test", "--all-projects", "--json", "/src"], parse_snyk, needs_token="SNYK_TOKEN"),
    Scanner("snyk-code", "snyk/snyk:alpine", "sends", lambda ps: any(CODE_RX.search(p) for p in ps),
            lambda ps: ["snyk", "code", "test", "--sarif", "/src"], lambda d: parse_sarif(d, "snyk-code"),
            needs_token="SNYK_TOKEN"),
]}
DEFAULT = [n for n, s in SCANNERS.items() if s.network != "sends"]


def ensure(store: Store) -> None:
    store.conn.executescript(SCHEMA)


def _tools_venv(settings: Settings, pkgs: list[str], log) -> Path | None:
    """A cached virtualenv with pip-installed scanners (bandit, checkov), built once in a python container."""
    venv = Path(settings.data_dir).resolve() / "scanners" / "tools"
    stamp = venv / ".installed"
    want = " ".join(sorted(pkgs))
    have = stamp.read_text() if stamp.exists() else ""
    if all(p in have for p in pkgs):
        return venv
    venv.mkdir(parents=True, exist_ok=True)
    allp = sorted(set(have.split()) | set(pkgs))
    r = _docker(["run", "--rm", *_limits(settings), "-v", f"{_host_path(venv)}:/tools", "python:3.12-slim", "sh", "-c",
                 "python -m venv /tools && /tools/bin/pip install -q --disable-pip-version-check " +
                 " ".join(f"'{p}'" for p in allp)], timeout=1200, log=log)
    if r.returncode:
        log(f"  scanner tool install failed: {(r.stderr or r.stdout)[-400:]}")
        return None
    stamp.write_text(" ".join(allp))
    return venv


def run_scanner(s: Scanner, src: Path, out: Path, paths: list[str], settings: Settings, log) -> tuple[list[Finding], str]:
    if s.needs_token and not os.environ.get(s.needs_token):
        return [], f"skipped: set {s.needs_token} to run {s.name}"
    mounts = ["-v", f"{_host_path(src)}:/src:ro", "-v", f"{_host_path(out)}:/out"]
    for host_name, cpath in s.extra_mounts:
        d = Path(settings.data_dir).resolve() / "scanners" / host_name
        d.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{_host_path(d)}:{cpath}"]
    env = ["-e", f"{s.needs_token}={os.environ[s.needs_token]}"] if s.needs_token else []
    if s.pip:
        venv = _tools_venv(settings, s.pip, log)
        if venv is None:
            return [], "error: could not install the scanner"
        mounts += ["-v", f"{_host_path(venv)}:/tools:ro"]
    net = ["--network", "none"] if s.network == "none" else []
    entry = ["--entrypoint", ""] if s.name in ("semgrep", "hadolint", "snyk", "snyk-code") else []
    cmd = s.command(paths)
    if s.name == "hadolint" and len(cmd) <= 4:
        return [], "skipped: no Dockerfiles"
    r = _docker(["run", "--rm", *net, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *_limits(settings),
                 *mounts, *env, *entry, s.image, *cmd],
                timeout=float(getattr(settings, "scan_timeout_s", 1800)), log=log)
    raw = (out / s.output_file).read_text(encoding="utf-8", errors="replace") if s.output_file and \
        (out / s.output_file).exists() else r.stdout
    raw = raw.strip()
    if not raw:
        return [], f"error: no output (exit {r.returncode}): {(r.stderr or '')[-300:]}"
    try:
        data = json.loads(raw[raw.find("{") if raw.lstrip()[0] == "{" else raw.find("["):])
    except Exception as e:  # noqa: BLE001
        return [], f"error: could not parse output: {e}; {(r.stderr or raw)[-300:]}"
    return s.parse(data), "ok"


def _symbol_for(store: Store, path: str, line: int | None, cache: dict) -> str | None:
    if not line or not path:
        return None
    if path not in cache:
        cache[path] = store.q("SELECT uid, start_line, end_line FROM symbols WHERE path=? AND alive=1 "
                              "AND kind != 'variable'", (path,))
    best = None
    for s in cache[path]:
        if s["start_line"] and s["start_line"] <= line <= (s["end_line"] or 0):
            if best is None or (s["end_line"] - s["start_line"]) < (best["end_line"] - best["start_line"]):
                best = s
    return best["uid"] if best else None


def scan(repo: pygit2.Repository, store: Store, settings: Settings, scanners: list[str] | None = None,
         log: Callable[[str], None] = print) -> dict:
    """Run the chosen scanners (default: every scanner that sends nothing off the machine) over HEAD."""
    if not docker_bin():
        return {"error": "Docker is not available"}
    ensure(store)
    head = store.get_meta("head")
    if not head:
        return {"error": "repository has no ingested HEAD"}
    work = Path(settings.data_dir).resolve() / "scans" / store.repo_id
    src, out = work / "src", work / "out"
    _export(repo, head, src)
    out.mkdir(parents=True, exist_ok=True)
    paths = [p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()]
    names = scanners or DEFAULT
    results: dict[str, dict] = {}
    sym_cache: dict = {}
    for name in names:
        s = SCANNERS.get(name)
        if s is None:
            results[name] = {"status": "error: unknown scanner"}
            continue
        if not s.applies(paths):
            results[name] = {"status": "skipped: nothing this scanner applies to"}
            continue
        t0 = time.perf_counter()
        log(f"  {name}: scanning")
        try:
            findings, status = run_scanner(s, src, out, paths, settings, log)
        except Exception as e:  # noqa: BLE001  one scanner failing must not stop the others
            findings, status = [], f"error: {type(e).__name__}: {e}"
        secs = round(time.perf_counter() - t0, 1)
        rows = {f.id: (f.id, f.scanner, f.rule, f.severity, f.title, f.message, f.path, f.line, f.end_line, f.package,
                       f.version, f.fixed, f.cve, f.cwe, f.url, _symbol_for(store, f.path, f.line, sym_cache), head,
                       time.time()) for f in findings}
        with store.tx() as conn:
            if status == "ok":  # replace this scanner's previous findings only when it ran successfully
                conn.execute("DELETE FROM vuln_findings WHERE scanner=?", (name,))
                conn.executemany("INSERT OR REPLACE INTO vuln_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 list(rows.values()))
            conn.execute("INSERT OR REPLACE INTO scan_runs VALUES (?,?,?,?,?,?,?)",
                         (name, time.time(), head, status, len(rows), secs, s.network))
        by_sev: dict[str, int] = {}
        for r in rows.values():
            by_sev[r[3]] = by_sev.get(r[3], 0) + 1
        results[name] = {"status": status, "findings": len(rows), "by_severity": by_sev, "seconds": secs}
        log(f"  {name}: {status}, {len(rows)} findings in {secs}s")
    return {"commit": head, "scanners": results, "total": store.q1("SELECT count(*) n FROM vuln_findings")["n"]}


# ---------------------------------------------------------------------------- graph + vectors
def project(store: Store, driver, database: str) -> int:
    """Replace this repository's Vulnerability nodes: AFFECTS -> File, IN_SYMBOL -> Symbol."""
    ensure(store)
    rid = store.repo_id
    rows = [dict(r) for r in store.q("SELECT * FROM vuln_findings")]
    for r in rows:
        r["uid"] = f"vuln:{rid}:{r['id']}"
        r["file_uid"] = f"file:{rid}:{r['path']}" if r["path"] else None
    with driver.session(database=database) as s:
        s.run("MATCH (v:Vulnerability {repo: $repo}) DETACH DELETE v", repo=rid).consume()
        for i in range(0, len(rows), 1000):
            s.run("""UNWIND $rows AS r
                     MERGE (v:Vulnerability {uid: r.uid})
                     SET v.repo = $repo, v.scanner = r.scanner, v.rule = r.rule, v.severity = r.severity,
                         v.title = r.title, v.path = r.path, v.line = r.line, v.package = r.package,
                         v.version = r.version, v.fixed = r.fixed, v.cve = r.cve, v.cwe = r.cwe, v.url = r.url,
                         v.commit = r.commit_hash
                     WITH v, r
                     OPTIONAL MATCH (f:File {uid: r.file_uid})
                     FOREACH (_ IN CASE WHEN f IS NULL THEN [] ELSE [1] END | MERGE (v)-[:AFFECTS]->(f))
                     WITH v, r
                     OPTIONAL MATCH (sym:Symbol {uid: r.symbol})
                     FOREACH (_ IN CASE WHEN sym IS NULL THEN [] ELSE [1] END | MERGE (v)-[:IN_SYMBOL]->(sym))""",
                  rows=rows[i:i + 1000], repo=rid).consume()
    return len(rows)


def embed(store: Store, client, embedder, batch: int = 64) -> int:
    """Replace this repository's points in the ``security_findings`` collection."""
    from afd_oracle_core.vectors import SECURITY, build_filter, ensure_collections, upsert_texts
    from qdrant_client import models
    ensure(store)
    ensure_collections(client, embedder.dim)
    client.delete(SECURITY, points_selector=models.FilterSelector(filter=build_filter({"repo": store.repo_id})))
    items = []
    for r in store.q("SELECT * FROM vuln_findings"):
        text = (f"{r['severity']} {r['scanner']} {r['rule']}: {r['title']}\n{r['message'] or ''}\n"
                f"{r['path']}:{r['line'] or ''} {r['package'] or ''} {r['version'] or ''} {r['cve'] or ''} {r['cwe'] or ''}")
        items.append((f"vuln:{store.repo_id}:{r['id']}", text, {
            "repo": store.repo_id, "scanner": r["scanner"], "severity": r["severity"], "rule": r["rule"],
            "title": r["title"], "path": r["path"], "line": r["line"], "package": r["package"], "cve": r["cve"],
            "kind": r["scanner"]}))
    return upsert_texts(client, embedder, SECURITY, items, batch) if items else 0
