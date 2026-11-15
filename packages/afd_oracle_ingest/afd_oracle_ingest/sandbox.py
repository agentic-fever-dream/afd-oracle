"""Docker sandbox: measure test coverage and run mutation testing without executing repository code on the host.

The repository's tests are its own code, so they only ever run inside a throwaway container:

* the code is a fresh export of the commit (``git archive``), never the user's working copy;
* dependencies are installed in a first container run (network on, needed for ``pip``);
* tests run in a second container with **no network**, all Linux capabilities dropped, ``no-new-privileges``,
  and memory / CPU / process limits;
* only three host folders are mounted: the exported source, the virtualenv, and an output folder.

Results land in the store (see ``catalog/enrich.py`` for the tables):

* ``coverage_files`` / ``covered_lines``: which lines ran at HEAD, and under how many tests;
* ``test_map`` rows with ``source='coverage'`` (test -> function, measured);
* ``mutation_results``: per-mutant killed / survived for selected functions.

Python projects only for now (pytest + coverage.py). Anything else returns ``{"skipped": ...}``.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Callable

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store

from .ast.extractor import language_for

IMAGE = "python:{py}-slim"
TEST_REQ_RX = re.compile(r"(?i)(^|/)(requirements[/_-]?(tests?|dev|testing)[^/]*\.txt|test[s_-]*requirements[^/]*\.txt|"
                         r"requirements/(tests?|dev|testing)[^/]*\.txt)$")


def docker_bin() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    win = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    return str(win) if win.exists() else None


def _host_path(p: Path) -> str:
    return str(p.resolve())


def _export(repo: pygit2.Repository, sha: str, dest: Path) -> None:
    """Write the tree at ``sha`` into ``dest`` (a clean copy; no .git, no working-copy changes)."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    git_dir = repo.path
    data = subprocess.run(["git", f"--git-dir={git_dir}", "archive", "--format=tar", sha], capture_output=True,
                          check=True, stdin=subprocess.DEVNULL).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _python_version(src: Path) -> str:
    """Pick a CPython image version that satisfies ``requires-python`` (default 3.12)."""
    py = src / "pyproject.toml"
    if py.exists():
        m = re.search(r"requires-python\s*=\s*[\"']([^\"']+)", py.read_text(encoding="utf-8", errors="replace"))
        if m:
            spec = m.group(1)
            for cand in ("3.12", "3.13", "3.11", "3.10"):
                lo = re.search(r">=\s*3\.(\d+)", spec)
                hi = re.search(r"<\s*3\.(\d+)", spec)
                minor = int(cand.split(".")[1])
                if (not lo or minor >= int(lo.group(1))) and (not hi or minor < int(hi.group(1))):
                    return cand
    return "3.12"


def _install_script(src: Path) -> str:
    """Best-effort dependency install: test requirement files, then the project itself with test extras."""
    steps = ["set -e", "python -m venv /venv", ". /venv/bin/activate",
             "pip install -q --disable-pip-version-check --no-input pytest pytest-cov coverage"]
    reqs = sorted(p.relative_to(src).as_posix() for p in src.rglob("*.txt")
                  if TEST_REQ_RX.search(p.relative_to(src).as_posix()) and ".tox" not in p.parts)
    for r in reqs[:3]:
        steps.append(f"pip install -q --disable-pip-version-check --no-input -r '/src/{r}' || true")
    extras = ""
    py = src / "pyproject.toml"
    if py.exists():
        text = py.read_text(encoding="utf-8", errors="replace")
        found = [e for e in ("tests", "test", "testing", "dev") if re.search(rf"(?m)^\s*{e}\s*=\s*\[", text)]
        extras = f"[{','.join(found)}]" if found else ""
    if py.exists() or (src / "setup.py").exists() or (src / "setup.cfg").exists():
        steps.append(f"pip install -q --disable-pip-version-check --no-input -e '/src{extras}' || "
                     "pip install -q --disable-pip-version-check --no-input -e /src || true")
    elif (src / "requirements.txt").exists():
        steps.append("pip install -q --disable-pip-version-check --no-input -r /src/requirements.txt || true")
    return "\n".join(steps) + "\n"


def _limits(settings: Settings) -> list[str]:
    return ["--memory", str(getattr(settings, "sandbox_memory", "3g")), "--cpus",
            str(getattr(settings, "sandbox_cpus", 2)), "--pids-limit", "512", "--security-opt", "no-new-privileges"]


def _docker(args: list[str], timeout: float, log: Callable[[str], None]) -> subprocess.CompletedProcess:
    d = docker_bin()
    if not d:
        raise RuntimeError("Docker is not installed or not on PATH")
    log("  $ docker " + " ".join(a if len(a) < 80 else a[:77] + "..." for a in args))
    return subprocess.run([d, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          encoding="utf-8", errors="replace")


def _workdir(settings: Settings, repo_id: str) -> Path:
    return Path(settings.data_dir).resolve() / "sandbox" / repo_id


def prepare(repo: pygit2.Repository, store: Store, settings: Settings, sha: str | None = None,
            log: Callable[[str], None] = print, reinstall: bool = False) -> dict:
    """Export the commit and install its dependencies into a cached virtualenv (network on, nothing executed but
    pip). Returns paths and the image used."""
    sha = sha or store.get_meta("head")
    if not sha:
        raise RuntimeError("repository has no ingested HEAD")
    work = _workdir(settings, store.repo_id)
    src, venv, out, scripts = work / "src", work / "venv", work / "out", work / "scripts"
    stamp = work / "venv.sha"
    _export(repo, sha, src)
    if not any((src / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        return {"skipped": "not a Python project (no pyproject.toml / setup.py / requirements.txt)"}
    py = _python_version(src)
    image = IMAGE.format(py=py)
    for d in (venv, out, scripts):
        d.mkdir(parents=True, exist_ok=True)
    if reinstall or not stamp.exists() or stamp.read_text() != f"{sha}|{image}":
        shutil.rmtree(venv, ignore_errors=True)
        venv.mkdir(parents=True)
        (scripts / "install.sh").write_text(_install_script(src), encoding="utf-8", newline="\n")
        r = _docker(["run", "--rm", *_limits(settings), "-v", f"{_host_path(src)}:/src", "-v",
                     f"{_host_path(venv)}:/venv", "-v", f"{_host_path(scripts)}:/oracle:ro", image, "sh",
                     "/oracle/install.sh"], timeout=float(getattr(settings, "sandbox_install_timeout_s", 1200)), log=log)
        if r.returncode:
            return {"error": "dependency install failed", "output": (r.stdout + r.stderr)[-3000:]}
        stamp.write_text(f"{sha}|{image}")
    return {"sha": sha, "image": image, "src": src, "venv": venv, "out": out, "scripts": scripts}


def coverage(repo: pygit2.Repository, store: Store, settings: Settings, log: Callable[[str], None] = print,
             reinstall: bool = False) -> dict:
    """Run the test suite under coverage.py (per-test contexts) in the sandbox and store the results."""
    t0 = time.perf_counter()
    env = prepare(repo, store, settings, log=log, reinstall=reinstall)
    if "skipped" in env or "error" in env:
        return env
    out: Path = env["out"]
    for f in out.glob("*"):
        f.unlink()
    # Tests may fail; coverage is still recorded. The exit code is kept for the report.
    script = ("cd /src && . /venv/bin/activate && "
              "COVERAGE_FILE=/out/.coverage python -m pytest -q -p no:cacheprovider --cov=/src --cov-context=test "
              "--cov-report= --rootdir=/src > /out/pytest.log 2>&1; echo $? > /out/pytest.rc; "
              "coverage json --data-file=/out/.coverage --show-contexts -o /out/coverage.json > /out/json.log 2>&1")
    (env["scripts"] / "test.sh").write_text(script + "\n", encoding="utf-8", newline="\n")
    r = _docker(["run", "--rm", "--network", "none", "--cap-drop", "ALL", *_limits(settings),
                 "-v", f"{_host_path(env['src'])}:/src", "-v", f"{_host_path(env['venv'])}:/venv",
                 "-v", f"{_host_path(out)}:/out", "-v", f"{_host_path(env['scripts'])}:/oracle:ro", env["image"],
                 "sh", "/oracle/test.sh"], timeout=float(getattr(settings, "sandbox_test_timeout_s", 1800)), log=log)
    report = out / "coverage.json"
    if not report.exists():
        tail = (out / "pytest.log").read_text(errors="replace")[-3000:] if (out / "pytest.log").exists() else r.stderr
        return {"error": "no coverage data was produced", "output": tail}
    stats = store_coverage(store, json.loads(report.read_text(encoding="utf-8")), env["sha"])
    rc = (out / "pytest.rc").read_text().strip() if (out / "pytest.rc").exists() else "?"
    summary = (out / "pytest.log").read_text(errors="replace").strip().splitlines()[-1:] \
        if (out / "pytest.log").exists() else []
    return {**stats, "pytest_exit": rc, "pytest_summary": summary[0] if summary else "", "sha": env["sha"],
            "image": env["image"], "seconds": round(time.perf_counter() - t0, 1)}


def _norm_context(ctx: str) -> str | None:
    ctx = re.sub(r"\|(run|setup|teardown)$", "", ctx or "")
    if not ctx:
        return None
    path, _, rest = ctx.partition("::")
    path = path.removeprefix("/src/")
    return f"{path}::{rest}" if rest else path


def store_coverage(store: Store, data: dict, sha: str) -> dict:
    """Write a ``coverage json --show-contexts`` report into coverage_files / covered_lines / test_map."""
    from .catalog.enrich import ensure
    ensure(store)
    syms: dict[str, list] = {}
    for s in store.q("SELECT uid, path, qualname, kind, start_line, end_line FROM symbols WHERE alive=1 "
                     "AND kind != 'variable'"):
        syms.setdefault(s["path"], []).append(s)

    def innermost(path: str, line: int):
        best = None
        for s in syms.get(path, []):
            if s["start_line"] and s["start_line"] <= line <= (s["end_line"] or 0):
                if best is None or (s["end_line"] - s["start_line"]) < (best["end_line"] - best["start_line"]):
                    best = s
        return best

    files, lines, tmap, line_tests = [], [], {}, []
    for raw, f in (data.get("files") or {}).items():
        path = raw.replace("\\", "/").removeprefix("/src/")
        if not language_for(path):
            continue
        executed = set(f.get("executed_lines") or [])
        summary = f.get("summary") or {}
        files.append((path, summary.get("num_statements", len(executed) + len(f.get("missing_lines") or [])),
                      summary.get("covered_lines", len(executed))))
        contexts = f.get("contexts") or {}
        is_test_file = bool(re.search(r"(^|/)(tests?/|test_[^/]+$|[^/]+_test\.py$|conftest\.py$)", path))
        for ln in executed:
            tests = {t for t in (_norm_context(c) for c in contexts.get(str(ln), [])) if t}
            lines.append((path, ln, len(tests)))
            line_tests += [(path, ln, t) for t in tests]
            if is_test_file:
                continue
            s = innermost(path, ln)
            target = s["uid"] if s else path
            for t in tests:
                tmap[(t, target)] = None
    with store.tx() as conn:
        conn.execute("DELETE FROM coverage_files")
        conn.execute("DELETE FROM covered_lines")
        conn.execute("DELETE FROM covered_line_tests")
        conn.execute("DELETE FROM test_map WHERE source='coverage'")
        conn.executemany("INSERT OR REPLACE INTO coverage_files VALUES (?,?,?)", files)
        conn.executemany("INSERT OR REPLACE INTO covered_lines VALUES (?,?,?)", lines)
        conn.executemany("INSERT OR REPLACE INTO covered_line_tests VALUES (?,?,?)", line_tests)
        conn.executemany("INSERT OR REPLACE INTO test_map VALUES (?,?,'coverage',1.0)", list(tmap))
        conn.execute("INSERT INTO meta(key, value) VALUES ('coverage_at', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (sha,))
    total = sum(f[1] for f in files) or 1
    return {"files": len(files), "lines_executed": len(lines), "test_links": len(tmap),
            "line_coverage": round(sum(f[2] for f in files) / total, 3)}


# ---------------------------------------------------------------------------- mutation testing
def mutate(repo: pygit2.Repository, store: Store, settings: Settings, targets: list[str],
           tests: list[str] | None = None, limit: int = 15, log: Callable[[str], None] = print) -> dict:
    """Mutation-test functions (``path::Qual.name``) with their tests running inside the sandbox container."""
    from .safety import mutate as mut
    env = prepare(repo, store, settings, log=log)
    if "skipped" in env or "error" in env:
        return env
    name = f"afd-oracle-mutate-{store.repo_id}"
    _docker(["rm", "-f", name], 30, lambda *_: None)
    r = _docker(["run", "-d", "--name", name, "--network", "none", "--cap-drop", "ALL", *_limits(settings),
                 "-v", f"{_host_path(env['src'])}:/src", "-v", f"{_host_path(env['venv'])}:/venv", env["image"],
                 "sleep", "infinity"], 120, log)
    if r.returncode:
        return {"error": "could not start the sandbox container", "output": r.stderr[-2000:]}
    results, rows = [], []
    try:
        runner = [docker_bin(), "exec", "-w", "/src", name, "/venv/bin/python"]
        for target in targets:
            rel, _, qual = target.partition("::")
            try:
                out = mut.run(env["src"], target, python=runner, tests=tests, limit=limit,
                              timeout=float(getattr(settings, "sandbox_mutant_timeout_s", 300)), log=log)
            except Exception as e:  # a target that can't be found or parsed must not stop the batch
                out = {"target": target, "error": f"{type(e).__name__}: {e}"}
            results.append(out)
            if "error" in out:
                continue
            uid = _uid_for(store, rel, qual)
            survived = {(s["line"], s["op"], s["mutated"]) for s in out.get("survived", [])}
            for s in out.get("survived", []):
                rows.append((uid, s["line"], s["op"], "", s["mutated"], "survived", env["sha"]))
            if out.get("mutants"):
                rows.append((uid, 0, "summary", "", f"{out['killed']}/{out['mutants']} killed", "summary", env["sha"]))
            log(f"  {target}: {out.get('killed')}/{out.get('mutants')} killed, {len(survived)} survived")
    finally:
        _docker(["rm", "-f", name], 60, lambda *_: None)
    from .catalog.enrich import ensure
    ensure(store)
    with store.tx() as conn:
        for uid in {r[0] for r in rows}:
            conn.execute("DELETE FROM mutation_results WHERE target=?", (uid,))
        conn.executemany("INSERT OR REPLACE INTO mutation_results VALUES (?,?,?,?,?,?,?)", rows)
    return {"targets": len(targets), "results": results}


def _uid_for(store: Store, path: str, qual: str) -> str:
    r = store.q1("SELECT uid FROM symbols WHERE path=? AND qualname=? AND alive=1 AND kind != 'variable'", (path, qual))
    return r["uid"] if r else symbol_uid(store.repo_id, path, qual, "function")


def pick_mutation_targets(store: Store, n: int) -> list[str]:
    """The catalogue's most wanted mutation targets: functions named by boundary-fix and fix-recurrence candidates,
    then fix-prone functions with tests (mutation testing needs tests to kill mutants)."""
    picked: list[str] = []
    rows = store.q("SELECT props FROM candidates WHERE kind IN ('boundary_fix_no_boundary_test', "
                   "'fix_recurrence_despite_existing_test', 'proven_guard_test', 'crap_hotspot') ORDER BY score DESC")
    uids = [u for r in rows for u in json.loads(r["props"] or "{}").get("symbols", [])]
    uids += [r["uid"] for r in store.q("SELECT uid, count(*) n FROM fix_events GROUP BY uid ORDER BY n DESC LIMIT 200")]
    for uid in dict.fromkeys(uids):
        s = store.q1("SELECT path, qualname, kind, lang FROM symbols WHERE uid=? AND alive=1", (uid,))
        if s and s["kind"] == "function" and s["lang"] == "python" and not re.search(r"(^|/)tests?/", s["path"]):
            picked.append(f"{s['path']}::{s['qualname']}")
        if len(picked) >= n:
            break
    return picked


# ---------------------------------------------------------------------------- Helm rendering
HELM_IMAGE = "alpine/helm:3.14.4"


def helm(repo: pygit2.Repository, store: Store, settings: Settings, max_commits: int = 40,
         log: Callable[[str], None] = print) -> dict:
    """Render every Helm chart with ``helm template`` (in a container, no network) at HEAD and at the most recent
    commits that changed it, so builders can compare real manifests instead of templated text."""
    from .catalog.enrich import ensure
    ensure(store)
    head = store.get_meta("head")
    charts = sorted({p.rsplit("/", 1)[0] if "/" in p else "." for p in (r["path"] for r in store.q(
        "SELECT DISTINCT path FROM file_changes")) if p.endswith("Chart.yaml")})
    if not charts or not head:
        return {"skipped": "no Helm charts (Chart.yaml) in this repository"}
    work = _workdir(settings, store.repo_id) / "helm"
    rendered, failed = 0, []
    done = {(r["chart"], r["commit_hash"]) for r in store.q("SELECT chart, commit_hash FROM helm_renders")}
    for chart in charts:
        prefix = "" if chart == "." else chart + "/"
        commits = [r["hash"] for r in store.q(
            "SELECT DISTINCT fc.hash, c.seq FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
            "WHERE fc.path LIKE ? AND c.on_main=1 ORDER BY c.seq DESC LIMIT ?", (prefix + "%", max_commits))]
        for sha in dict.fromkeys([head, *commits]):
            if (chart, sha) in done:
                continue
            src = work / "src"
            try:
                _export(repo, sha, src)
            except Exception as e:
                failed.append(f"{chart}@{sha[:8]}: export {e}")
                continue
            if not (src / chart / "Chart.yaml").exists():
                continue
            r = _docker(["run", "--rm", "--network", "none", "--cap-drop", "ALL", *_limits(settings),
                         "-v", f"{_host_path(src)}:/src:ro", HELM_IMAGE, "template", "oracle", f"/src/{chart}"],
                        timeout=300, log=lambda *_: None)
            if r.returncode:
                failed.append(f"{chart}@{sha[:8]}: {r.stderr.strip()[:200]}")
                continue
            ts = store.q1("SELECT ts FROM commits WHERE hash=?", (sha,))
            with store.tx() as conn:
                conn.execute("INSERT OR REPLACE INTO helm_renders VALUES (?,?,?,?)",
                             (chart, sha, ts["ts"] if ts else None, r.stdout[:2_000_000]))
            rendered += 1
    log(f"  helm: {rendered} renders, {len(failed)} failed")
    return {"charts": charts, "rendered": rendered, "failed": failed[:20]}
