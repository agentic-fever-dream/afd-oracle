"""Candidate catalogue: registry, hits and the shared context every builder reads from.

A *builder* is a function decorated with ``@candidate("<id>")`` that takes a :class:`Ctx` and yields :class:`Hit`
objects. The id must exist in ``spec.json`` (name, family, priority, analyst question, insight label …). Builders are
deterministic and cheap: they only query data ingestion already produced (the store, the ``diff_lines`` index, git
blobs). An LLM never runs here; it judges the resulting packets later.

Conventions for builders
------------------------
* Yield at most a few hundred hits; the runner ranks by ``score`` and keeps the top ``catalog_max_per_kind``.
* ``subject`` must be stable across runs (a path, symbol uid, commit hash or a joined key) so packet status survives.
* Put the minimum evidence an analyst needs in ``evidence``; list commits in ``commits`` (they become briefs) and
  ``(sha, path)`` pairs in ``diffs`` (they become trimmed patches). Cite only real hashes, paths and symbol uids.
* Use ``ctx.drop(reason)`` for every candidate a noise filter removes, so filtered pools are reported, never hidden.
"""
from __future__ import annotations

import fnmatch
import functools
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store, loads

SPEC_PATH = Path(__file__).with_name("spec.json")


@functools.lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


@dataclass
class Hit:
    subject: str                      # stable key: path, symbol uid, commit hash or "a|b"
    score: float                      # ranking within the kind (higher = more worth an analyst's time)
    title: str                        # one line naming the concrete situation
    evidence: dict[str, Any] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)              # full hashes; rendered as briefs
    diffs: list[tuple[str, str]] = field(default_factory=list)    # (sha, path) -> trimmed patch in the packet
    paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)              # symbol uids


Builder = Callable[["Ctx"], Iterable[Hit]]
REGISTRY: dict[str, Builder] = {}


def candidate(cid: str) -> Callable[[Builder], Builder]:
    """Register a builder for catalogue entry ``cid`` (must exist in spec.json)."""
    if cid not in spec()["candidates"]:
        raise KeyError(f"catalogue id {cid!r} is not in spec.json")

    def deco(fn: Builder) -> Builder:
        if cid in REGISTRY:
            raise KeyError(f"catalogue id {cid!r} registered twice")
        REGISTRY[cid] = fn
        fn.catalog_id = cid  # type: ignore[attr-defined]
        return fn
    return deco


# ---------------------------------------------------------------------------- path classification
TEST_RX = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.[jt]sx?$|"
                     r"(^|/)conftest\.py$")
DOC_RX = re.compile(r"(?i)(^|/)docs?/|\.(md|rst|txt|adoc)$|(^|/)(readme|changelog|changes|history|news|license|"
                    r"authors|contributing)[^/]*$")
VENDOR_RX = re.compile(r"(?i)(^|/)(vendor|vendored|third_party|third-party|node_modules|site-packages|_vendor|"
                       r"external|extern)/")
GENERATED_RX = re.compile(r"(?i)(_pb2(_grpc)?\.py|\.pb\.go|\.generated\.|\.min\.(js|css)|(^|/)dist/|(^|/)build/|"
                          r"\.lock$|-lock\.(json|yaml)$|(^|/)package-lock\.json$|(^|/)yarn\.lock$|(^|/)poetry\.lock$|"
                          r"(^|/)uv\.lock$|(^|/)Cargo\.lock$|(^|/)go\.sum$)")
CI_RX = re.compile(r"(?i)(^|/)\.github/workflows/[^/]+\.ya?ml$|(^|/)\.gitlab-ci\.ya?ml$|(^|/)\.circleci/|"
                   r"(^|/)azure-pipelines\.ya?ml$|(^|/)Jenkinsfile$|(^|/)\.travis\.ya?ml$|(^|/)tox\.ini$|"
                   r"(^|/)\.pre-commit-config\.ya?ml$|(^|/)noxfile\.py$|(^|/)bitbucket-pipelines\.ya?ml$")
CONFIG_RX = re.compile(r"(?i)\.(ya?ml|json|toml|ini|cfg|conf|properties|env|tfvars|hcl|tf)$|(^|/)\.env[^/]*$|"
                       r"(^|/)(Dockerfile|Containerfile)[^/]*$|(^|/)docker-compose[^/]*\.ya?ml$|(^|/)Makefile$|"
                       r"(^|/)settings\.py$|(^|/)config\.py$")
DEP_MANIFEST_RX = re.compile(r"(?i)(^|/)(requirements[^/]*\.(txt|in)|pyproject\.toml|setup\.(py|cfg)|Pipfile|"
                             r"package\.json|go\.mod|Cargo\.toml|Gemfile|pom\.xml|build\.gradle(\.kts)?|"
                             r"constraints[^/]*\.txt)$")
LOCKFILE_RX = re.compile(r"(?i)(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|"
                         r"Pipfile\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|requirements[^/]*\.lock)$")
MIGRATION_RX = re.compile(r"(?i)(^|/)(migrations?|alembic/versions|db/migrate|schema_migrations|flyway|liquibase)/|"
                          r"\.sql$")
K8S_RX = re.compile(r"(?i)(^|/)(k8s|kubernetes|manifests?|deploy(ment)?s?|charts?|helm|kustomize|overlays)/.*\.ya?ml$|"
                    r"(^|/)values[^/]*\.ya?ml$|(^|/)Chart\.yaml$|(^|/)kustomization\.ya?ml$")
IAC_RX = re.compile(r"(?i)\.(tf|tfvars|hcl)$|(^|/)(cloudformation|cdk|pulumi)/|template\.ya?ml$")
CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt", ".rs", ".php", ".cs")


def is_test(path: str) -> bool:
    return bool(TEST_RX.search(path))


def is_docs(path: str) -> bool:
    return bool(DOC_RX.search(path)) and not DEP_MANIFEST_RX.search(path)


def is_vendored(path: str) -> bool:
    return bool(VENDOR_RX.search(path))


def is_generated(path: str) -> bool:
    return bool(GENERATED_RX.search(path))


def is_code(path: str) -> bool:
    return path.endswith(CODE_EXT) and not is_vendored(path) and not is_generated(path)


def is_ci(path: str) -> bool:
    return bool(CI_RX.search(path))


def is_config(path: str) -> bool:
    return bool(CONFIG_RX.search(path)) and not is_generated(path) and not is_vendored(path)


# ---------------------------------------------------------------------------- commit classification
FIX_RX = re.compile(r"(?i)\b(fix(e[sd]|ing)?|bug(fix)?|regression|crash(es|ed)?|broken|hotfix|issue\s*#?\d+|"
                    r"resolve[sd]?\s+#\d+|closes?\s+#\d+|workaround|correct(s|ed)?\s+(the\s+)?(handling|behaviou?r))\b")
NOT_FIX_RX = re.compile(r"(?i)^(fix(ed)?\s+)?(typo|typos|docs?|documentation|spelling|grammar|whitespace|lint|"
                        r"flake8|formatting|changelog|readme|tests?\s+only)\b|\btypo\b")
BOT_MARKERS = ("[bot]", "pre-commit.ci", "dependabot", "renovate", "github-actions", "greenkeeper", "snyk-bot")
REVERT_RX = re.compile(r"(?i)^revert\b|\brevert(s|ed)?\s+(commit\s+)?[0-9a-f]{7,40}\b")
HOTFIX_RX = re.compile(r"(?i)\b(hot-?fix|emergency|urgent|incident|outage|sev[ -]?[0-3]\b|p0\b|rollback|roll back)\b")


def is_fix_message(msg: str | None) -> bool:
    msg = msg or ""
    subject = msg.splitlines()[0] if msg else ""
    return bool(FIX_RX.search(msg)) and not NOT_FIX_RX.search(subject)


def subject_of(msg: str | None) -> str:
    return (msg or "").splitlines()[0][:200] if msg else ""


# ---------------------------------------------------------------------------- config parsing
def parse_config(path: str, text: str | None) -> Any:
    """Best-effort parse of YAML / JSON / TOML / INI / .env text. Returns None when it can't be parsed."""
    if not text:
        return None
    p = path.lower()
    try:
        if p.endswith((".yaml", ".yml")):
            import yaml
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
            return docs[0] if len(docs) == 1 else docs
        if p.endswith(".json"):
            return json.loads(text)
        if p.endswith(".toml"):
            import tomllib
            return tomllib.loads(text)
        if p.endswith((".ini", ".cfg")) or p.rsplit("/", 1)[-1] in ("setup.cfg", "tox.ini", ".flake8"):
            import configparser
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.read_string(text)
            return {s: dict(cp[s]) for s in cp.sections()}
        if "/.env" in "/" + p or p.endswith(".env") or ".env." in p:
            out = {}
            for ln in text.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    out[k.removeprefix("export ").strip()] = v.strip().strip('"\'')
            return out
    except Exception:
        return None
    return None


def flatten(obj: Any, prefix: str = "", max_items: int = 5000) -> dict[str, Any]:
    """{"a": {"b": [1, {"c": 2}]}} -> {"a.b[0]": 1, "a.b[1].c": 2}. Scalars only."""
    out: dict[str, Any] = {}

    def walk(o: Any, key: str) -> None:
        if len(out) >= max_items:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{key}.{k}" if key else str(k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{key}[{i}]")
        else:
            out[key] = o
    walk(obj, prefix)
    return out


# ---------------------------------------------------------------------------- context
class Ctx:
    """Read-only view over everything ingestion produced, with caches. One instance per catalogue run."""

    def __init__(self, repo: pygit2.Repository, store: Store, settings: Settings):
        self.repo = repo
        self.store = store
        self.settings = settings
        self.repo_id = store.repo_id
        self.noise: dict[str, int] = defaultdict(int)
        self._current: str = ""
        _install_regexp(store.conn)

    # -- bookkeeping -----------------------------------------------------------
    def drop(self, reason: str, n: int = 1) -> None:
        """Count a candidate removed by a noise filter (reported per kind and reason)."""
        self.noise[f"{self._current}/{reason}"] += n

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.store.q(sql, params)

    def q1(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.store.q1(sql, params)

    # -- commits ----------------------------------------------------------------
    @functools.cached_property
    def commits(self) -> list[sqlite3.Row]:
        """Every ingested commit, oldest first (columns of the ``commits`` table)."""
        return self.store.q("SELECT * FROM commits ORDER BY seq")

    @functools.cached_property
    def by_hash(self) -> dict[str, sqlite3.Row]:
        return {c["hash"]: c for c in self.commits}

    @functools.cached_property
    def main_commits(self) -> list[sqlite3.Row]:
        """Commits on the default branch, oldest first."""
        return [c for c in self.commits if c["on_main"]]

    @functools.cached_property
    def head(self) -> str | None:
        return self.store.get_meta("head") or (self.main_commits[-1]["hash"] if self.main_commits else None)

    @functools.cached_property
    def head_ts(self) -> int:
        return max((c["ts"] for c in self.commits), default=0)

    def commit(self, sha: str) -> sqlite3.Row | None:
        return self.by_hash.get(sha)

    def is_bot(self, c: sqlite3.Row) -> bool:
        who = f"{c['author_name']} {c['author_email']}".lower()
        return any(m in who or m in subject_of(c["message"]).lower() for m in BOT_MARKERS)

    def is_fix(self, c: sqlite3.Row) -> bool:
        return is_fix_message(c["message"])

    @functools.cached_property
    def changes_by_commit(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.store.q("SELECT * FROM file_changes WHERE ignored=0"):
            out[r["hash"]].append(r)
        return out

    @functools.cached_property
    def changes_by_path(self) -> dict[str, list[sqlite3.Row]]:
        """path -> its file_changes rows (by the path at that commit), oldest first."""
        seq = {c["hash"]: c["seq"] for c in self.commits}
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for rows in self.changes_by_commit.values():
            for r in rows:
                out[r["path"]].append(r)
        for v in out.values():
            v.sort(key=lambda r: seq.get(r["hash"], 0))
        return out

    def changes(self, sha: str) -> list[sqlite3.Row]:
        return self.changes_by_commit.get(sha, [])

    def path_commits(self, path: str) -> list[sqlite3.Row]:
        """Commits that touched ``path`` (by that name), oldest first."""
        return [self.by_hash[r["hash"]] for r in self.changes_by_path.get(path, []) if r["hash"] in self.by_hash]

    # -- line-level history (diff_lines index) ----------------------------------
    def grep(self, pattern: str, sign: str | None = "+", path_like: str | None = None,
             path_regex: str | None = None, limit: int = 20000) -> list[sqlite3.Row]:
        """Changed lines matching a regex. Rows: hash, seq, ts, path, sign, line, text, symbol (uid of the innermost function or class holding the line in that version; empty when none). Oldest first.

        ``sign`` is '+' (added), '-' (removed) or None (both). ``path_like`` is a SQL LIKE filter (cheap, use it),
        ``path_regex`` a Python regex on the path.
        """
        # Cheap filters first: SQLite evaluates AND terms left to right, and the Python REGEXP on text is the costly one.
        conds, params = [], []
        if sign:
            conds.append("sign=?")
            params.append(sign)
        if path_like:
            conds.append("path LIKE ?")
            params.append(path_like)
        if path_regex:
            conds.append("path REGEXP ?")
            params.append(path_regex)
        conds.append("text REGEXP ?")
        params.append(pattern)
        return self.store.q(f"SELECT * FROM diff_lines WHERE {' AND '.join(conds)} ORDER BY seq, path, line LIMIT ?",
                            (*params, limit))

    def stream(self, sql: str, params: Iterable[Any] = (), batch: int = 5000) -> Iterator[sqlite3.Row]:
        """Rows of a query, fetched in batches. Use it for full scans of ``diff_lines`` (millions of rows on large
        repositories): iterate once, keep only what you need. ``ctx.q`` would materialise every row."""
        cur = self.store.conn.execute(sql, tuple(params))
        try:
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    return
                yield from chunk
        finally:
            cur.close()

    def lines_of(self, sha: str, path: str | None = None) -> list[sqlite3.Row]:
        """All indexed changed lines of a commit (optionally one file)."""
        if path:
            return self.store.q("SELECT * FROM diff_lines WHERE hash=? AND path=? ORDER BY sign, line", (sha, path))
        return self.store.q("SELECT * FROM diff_lines WHERE hash=? ORDER BY path, sign, line", (sha,))

    def added_removed(self, sha: str, path: str) -> tuple[list[str], list[str]]:
        rows = self.lines_of(sha, path)
        return [r["text"] for r in rows if r["sign"] == "+"], [r["text"] for r in rows if r["sign"] == "-"]

    # -- files and text -----------------------------------------------------------
    @functools.cached_property
    def head_files(self) -> dict[str, str]:
        """Every tracked path at HEAD -> blob id (includes non-code files)."""
        from ..git.walker import tree_files
        return tree_files(self.repo, self.head) if self.head else {}

    def glob(self, *patterns: str) -> list[str]:
        """Head paths matching any fnmatch pattern (``**`` not special; use ``*/x.yml`` or regex helpers)."""
        return sorted(p for p in self.head_files if any(fnmatch.fnmatch(p, pat) for pat in patterns))

    def match(self, rx: re.Pattern[str] | str) -> list[str]:
        r = re.compile(rx) if isinstance(rx, str) else rx
        return sorted(p for p in self.head_files if r.search(p))

    @functools.lru_cache(maxsize=4096)
    def blob_text(self, oid: str | None, max_bytes: int = 512_000) -> str | None:
        if not oid:
            return None
        try:
            blob = self.repo.get(oid)
        except (ValueError, KeyError):
            return None
        if blob is None or not isinstance(blob, pygit2.Blob) or blob.is_binary or blob.size > max_bytes:
            return None
        return blob.data.decode("utf-8", "replace")

    def text_at_head(self, path: str) -> str | None:
        return self.blob_text(self.head_files.get(path))

    def text_at(self, sha: str, path: str) -> str | None:
        """File text as of commit ``sha`` (None if absent, binary or huge)."""
        try:
            entry = self.repo.get(sha).tree[path]  # type: ignore[union-attr]
        except (KeyError, AttributeError, ValueError):
            return None
        return self.blob_text(str(entry.id))

    def text_before(self, sha: str, path: str) -> str | None:
        """File text in the first parent of ``sha``."""
        c = self.repo.get(sha)
        if c is None or not c.parents:
            return None
        return self.text_at(str(c.parents[0].id), path)

    def config_at_head(self, path: str) -> Any:
        return parse_config(path, self.text_at_head(path))

    # -- code model ---------------------------------------------------------------
    @functools.cached_property
    def symbols(self) -> dict[str, sqlite3.Row]:
        return {r["uid"]: r for r in self.store.q("SELECT * FROM symbols")}

    @functools.cached_property
    def alive_symbols(self) -> list[sqlite3.Row]:
        return [r for r in self.symbols.values() if r["alive"]]

    @functools.cached_property
    def symbols_by_path(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.alive_symbols:
            out[r["path"]].append(r)
        return out

    def symbol_at_line(self, path: str, line: int) -> sqlite3.Row | None:
        """Innermost alive symbol whose HEAD line range contains ``line``."""
        best = None
        for s in self.symbols_by_path.get(path, []):
            if s["start_line"] and s["end_line"] and s["start_line"] <= line <= s["end_line"]:
                if best is None or (s["end_line"] - s["start_line"]) < (best["end_line"] - best["start_line"]):
                    best = s
        return best

    def symbol_events(self, uid: str) -> list[sqlite3.Row]:
        return self.store.q("SELECT se.*, c.message FROM symbol_events se LEFT JOIN commits c ON c.hash=se.hash "
                            "WHERE se.uid=? ORDER BY se.seq", (uid,))

    @functools.cached_property
    def events_by_commit(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.store.q("SELECT * FROM symbol_events"):
            out[r["hash"]].append(r)
        return out

    @functools.cached_property
    def calls(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(callees_of, callers_of) over CALLS edges at HEAD."""
        out_: dict[str, set[str]] = defaultdict(set)
        in_: dict[str, set[str]] = defaultdict(set)
        for r in self.store.q("SELECT src, dst FROM edges WHERE kind='CALLS'"):
            out_[r["src"]].add(r["dst"])
            in_[r["dst"]].add(r["src"])
        return out_, in_

    def callers(self, uid: str) -> set[str]:
        return self.calls[1].get(uid, set())

    def callees(self, uid: str) -> set[str]:
        return self.calls[0].get(uid, set())

    @functools.cached_property
    def imports(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(imports_of, imported_by) over IMPORTS edges between files at HEAD."""
        out_: dict[str, set[str]] = defaultdict(set)
        in_: dict[str, set[str]] = defaultdict(set)
        for r in self.store.q("SELECT src, dst FROM edges WHERE kind='IMPORTS'"):
            out_[r["src"]].add(r["dst"])
            in_[r["dst"]].add(r["src"])
        return out_, in_

    @functools.cached_property
    def tests_for(self) -> dict[str, list[sqlite3.Row]]:
        """target symbol uid -> test_map rows (test, source, weight)."""
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.store.q("SELECT * FROM test_map"):
            out[r["target"]].append(r)
        return out

    def best_test_weight(self, uid: str) -> float:
        return max((r["weight"] or 0 for r in self.tests_for.get(uid, [])), default=0.0)

    @functools.cached_property
    def fix_events(self) -> list[sqlite3.Row]:
        """fix_events rows: hash, uid, path, kinds (json), guards (json), tests (json), ts, subject."""
        return self.store.q("SELECT * FROM fix_events ORDER BY ts")

    @functools.cached_property
    def fixes_by_uid(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.fix_events:
            out[r["uid"]].append(r)
        return out

    @functools.cached_property
    def findings(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM findings")

    @functools.cached_property
    def contracts(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM contracts")

    @functools.cached_property
    def cochange(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM cochange ORDER BY score DESC")

    @functools.cached_property
    def api_surface(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM api_surface")

    @functools.cached_property
    def reverts(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM reverts")

    @functools.cached_property
    def branches(self) -> list[sqlite3.Row]:
        return self.store.q("SELECT * FROM branches")

    # -- enrichment (catalog/enrich.py) ---------------------------------------------
    @functools.cached_property
    def tags(self) -> list[sqlite3.Row]:
        """Release tags, oldest first: name, commit_hash, ts, message."""
        return self.store.q("SELECT * FROM tags ORDER BY ts")

    @functools.cached_property
    def file_modes(self) -> list[sqlite3.Row]:
        """Mode changes: hash, path, old_mode, new_mode (0o100755 = executable, 0o120000 = symlink)."""
        return self.store.q("SELECT * FROM file_modes")

    def symbol_versions(self, uid: str) -> list[sqlite3.Row]:
        """Every recorded version of a symbol, oldest first: hash, seq, ts, path, qualname, kind, signature,
        params (count, excluding self/cls), start_line, end_line, loc, complexity (decision points + 1)."""
        return self.store.q("SELECT * FROM symbol_versions WHERE uid=? ORDER BY seq", (uid,))

    @functools.cached_property
    def versions_by_uid(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in self.store.q("SELECT * FROM symbol_versions ORDER BY seq"):
            out[r["uid"]].append(r)
        return out

    def complexity(self, uid: str) -> int | None:
        """Complexity of the latest recorded version of a symbol."""
        v = self.versions_by_uid.get(uid)
        return v[-1]["complexity"] if v else None

    def line_origin(self, fix_hash: str, path: str, line: int) -> str | None:
        """The commit that last wrote ``line`` (old line number) before bug-fix ``fix_hash`` changed it (git blame)."""
        r = self.store.q1("SELECT origin_hash FROM line_origins WHERE fix_hash=? AND path=? AND line=?",
                          (fix_hash, path, line))
        return r["origin_hash"] if r else None

    @functools.cached_property
    def line_origins(self) -> list[sqlite3.Row]:
        """All blame results: fix_hash, path, line, origin_hash."""
        return self.store.q("SELECT * FROM line_origins")

    def lines_in_symbol(self, uid: str, sign: str | None = None) -> list[sqlite3.Row]:
        """Every changed line ever attributed to symbol ``uid`` (diff_lines.symbol), oldest first."""
        sql, params = "SELECT * FROM diff_lines WHERE symbol=?", [uid]
        if sign:
            sql += " AND sign=?"
            params.append(sign)
        return self.store.q(sql + " ORDER BY seq, line", params)

    def test_map_at(self, ref: str) -> list[sqlite3.Row]:
        """The static test map rebuilt at release tag ``ref``: test, target, source, weight."""
        return self.store.q("SELECT * FROM test_map_at WHERE ref=?", (ref,))

    def tag_before(self, ts: int) -> sqlite3.Row | None:
        """The latest release tag at or before ``ts``."""
        older = [t for t in self.tags if t["ts"] <= ts]
        return older[-1] if older else None

    def grep_aux(self, pattern: str, sign: str | None = "+", path_like: str | None = None,
                 limit: int = 20000) -> list[sqlite3.Row]:
        """Like :meth:`grep`, over lockfiles and vendored files (ignored by core ingestion)."""
        conds, params = [], []
        if sign:
            conds.append("sign=?")
            params.append(sign)
        if path_like:
            conds.append("path LIKE ?")
            params.append(path_like)
        conds.append("text REGEXP ?")
        params.append(pattern)
        return self.store.q(f"SELECT * FROM diff_lines_aux WHERE {' AND '.join(conds)} ORDER BY seq, path, line "
                            "LIMIT ?", (*params, limit))

    # -- measured in the Docker sandbox (afd_oracle_ingest.sandbox) -----------------
    @functools.cached_property
    def coverage_available(self) -> bool:
        """True once `afd-oracle sandbox coverage` has measured this repo; builders must fall back to the static
        test map when False."""
        try:
            return self.store.q1("SELECT count(*) n FROM coverage_files")["n"] > 0
        except sqlite3.OperationalError:
            return False

    @functools.cached_property
    def _covered(self) -> dict[str, dict[int, int]]:
        out: dict[str, dict[int, int]] = defaultdict(dict)
        if self.coverage_available:
            for r in self.store.q("SELECT path, line, n_tests FROM covered_lines"):
                out[r["path"]][r["line"]] = r["n_tests"]
        return out

    @functools.cached_property
    def measured_files(self) -> set[str]:
        return {r["path"] for r in self.store.q("SELECT path FROM coverage_files")} if self.coverage_available else set()

    def covered(self, path: str, line: int) -> int | None:
        """How many tests executed ``line`` at HEAD (0 = never), or None when ``path`` was not measured."""
        if path not in self.measured_files:
            return None
        return self._covered.get(path, {}).get(line, 0)

    def symbol_coverage(self, uid: str) -> tuple[int, int] | None:
        """(executed lines, total lines) of a HEAD symbol's body, or None when not measured."""
        s = self.symbols.get(uid)
        if not s or s["path"] not in self.measured_files or not s["start_line"]:
            return None
        lines = range(s["start_line"] + 1, (s["end_line"] or s["start_line"]) + 1)
        cov = self._covered.get(s["path"], {})
        return sum(1 for ln in lines if cov.get(ln, 0) > 0), len(lines)

    def mutations(self, uid: str) -> list[sqlite3.Row]:
        """Mutation-testing results for a symbol (status 'killed' | 'survived' | 'timeout' | 'error')."""
        try:
            return self.store.q("SELECT * FROM mutation_results WHERE target=? ORDER BY line", (uid,))
        except sqlite3.OperationalError:
            return []

    # -- round-2 enrichment -----------------------------------------------------------
    def _safe_q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        try:
            return self.store.q(sql, params)
        except sqlite3.OperationalError:  # table not created yet (older store)
            return []

    def tests_covering(self, path: str, line: int) -> set[str] | None:
        """The tests that executed ``line`` at HEAD (sandbox coverage), or None when ``path`` was not measured."""
        if path not in self.measured_files:
            return None
        return {r["test"] for r in self._safe_q("SELECT test FROM covered_line_tests WHERE path=? AND line=?",
                                                (path, line))}

    def coverage_tests(self, uid: str) -> set[str] | None:
        """Tests that executed any line of a HEAD symbol (measured), or None when its file was not measured."""
        s = self.symbols.get(uid)
        if not s or s["path"] not in self.measured_files:
            return None
        return {r["test"] for r in self.tests_for.get(uid, []) if r["source"] == "coverage"}

    def lines_run_by(self, test: str, path: str | None = None) -> set[tuple[str, int]]:
        """(path, line) pairs a given test executed (measured)."""
        sql, params = "SELECT path, line FROM covered_line_tests WHERE test=?", [test]
        if path:
            sql += " AND path=?"
            params.append(path)
        return {(r["path"], r["line"]) for r in self._safe_q(sql, params)}

    def callers_within(self, uid: str, hops: int = 2) -> dict[str, int]:
        """Transitive callers of ``uid`` up to ``hops`` CALLS edges away -> distance."""
        seen, frontier = {}, {uid}
        for d in range(1, hops + 1):
            nxt = set()
            for u in frontier:
                for c in self.callers(u):
                    if c != uid and c not in seen:
                        seen[c] = d
                        nxt.add(c)
            frontier = nxt
        return seen

    def callees_within(self, uid: str, hops: int = 2) -> dict[str, int]:
        """Transitive callees of ``uid`` up to ``hops`` CALLS edges away -> distance."""
        seen, frontier = {}, {uid}
        for d in range(1, hops + 1):
            nxt = set()
            for u in frontier:
                for c in self.callees(u):
                    if c != uid and c not in seen:
                        seen[c] = d
                        nxt.add(c)
            frontier = nxt
        return seen

    @functools.cached_property
    def patch_ids(self) -> dict[str, str]:
        """commit hash -> ``git patch-id`` (equal ids = the same change applied again)."""
        return {r["hash"]: r["patch_id"] for r in self._safe_q("SELECT hash, patch_id FROM commit_patch_ids")}

    @functools.cached_property
    def commits_by_patch_id(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for h, pid in self.patch_ids.items():
            out[pid].append(h)
        return out

    def raises_of(self, version: sqlite3.Row) -> list[str]:
        """Exception types a ``symbol_versions`` row raises (see enrich.raises)."""
        try:
            return json.loads(version["raises"] or "[]")
        except (IndexError, KeyError, ValueError, TypeError):
            return []

    def calls_at(self, ref: str) -> list[sqlite3.Row]:
        """Name-resolved call edges (src, dst symbol uids) at release tag ``ref``."""
        return self._safe_q("SELECT src, dst FROM calls_at WHERE ref=?", (ref,))

    def contracts_at(self, ref: str) -> list[sqlite3.Row]:
        """String contracts (kind, key, role, path, symbol) at release tag ``ref``."""
        return self._safe_q("SELECT kind, key, role, path, symbol FROM contracts_at WHERE ref=?", (ref,))

    @functools.cached_property
    def branch_commits(self) -> dict[str, set[str]]:
        """branch name -> commit hashes unique to it (merged/unmerged/abandoned branches)."""
        out: dict[str, set[str]] = defaultdict(set)
        for r in self._safe_q("SELECT branch, hash FROM branch_commits"):
            out[r["branch"]].add(r["hash"])
        return out

    @functools.cached_property
    def branch_of(self) -> dict[str, str]:
        """commit hash -> the branch it was made on, when known."""
        return {h: b for b, hs in self.branch_commits.items() for h in hs}

    # -- opt-in GitHub data (afd-oracle discussions) and Helm renders (afd-oracle sandbox helm)
    @functools.cached_property
    def github_repo(self) -> dict | None:
        """Repository facts from GitHub (private, visibility, fork, forks_count, archived, ...) or None if never
        synced."""
        raw = self.store.get_meta("github_repo")
        return json.loads(raw) if raw else None

    @functools.cached_property
    def github_releases(self) -> list[sqlite3.Row]:
        return self._safe_q("SELECT * FROM github_releases ORDER BY created_at")

    @functools.cached_property
    def discussions(self) -> dict[int, sqlite3.Row]:
        """PR / issue number -> row (kind, title, body, state, labels, url, created_at, closed_at, merged_at)."""
        return {r["number"]: r for r in self._safe_q("SELECT * FROM discussions")}

    def discussions_for(self, sha: str) -> list[sqlite3.Row]:
        """PRs and issues linked to a commit (merge commit, (#N) subject, fixes/closes references)."""
        return [self.discussions[r["number"]] for r in self._safe_q(
            "SELECT number FROM discussion_commits WHERE hash=?", (sha,)) if r["number"] in self.discussions]

    def helm_renders(self, chart: str | None = None) -> list[sqlite3.Row]:
        """Rendered manifests (chart, commit_hash, ts, manifest), oldest first."""
        if chart:
            return self._safe_q("SELECT * FROM helm_renders WHERE chart=? ORDER BY ts", (chart,))
        return self._safe_q("SELECT * FROM helm_renders ORDER BY chart, ts")

    def file_metrics(self, path: str) -> dict:
        r = self.store.q1("SELECT * FROM files WHERE path=?", (path,))
        return {**dict(r), "metrics": loads(r["metrics"], {})} if r else {}


def _regexp(pattern: str, value: Any) -> bool:
    if value is None:
        return False
    return _compiled(pattern).search(str(value)) is not None


@functools.lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _install_regexp(conn: sqlite3.Connection) -> None:
    conn.create_function("REGEXP", 2, lambda p, v: _regexp(p, v), deterministic=True)


def window(rows: list[sqlite3.Row], ts: int, days: float, after: bool = True) -> Iterator[sqlite3.Row]:
    """Rows whose ``ts`` is within ``days`` after (or before) ``ts``."""
    lo, hi = (ts, ts + days * 86400) if after else (ts - days * 86400, ts)
    return (r for r in rows if lo <= r["ts"] <= hi)


DAY = 86400
