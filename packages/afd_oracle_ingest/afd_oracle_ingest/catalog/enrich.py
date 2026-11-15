"""Enrichment: history facts the catalogue needs that core ingestion does not keep. All derived from git; no network,
no repository code is executed. Every part is incremental (resumes from a ``meta`` watermark) and bounded.

Tables
------
* ``tags(name, commit, ts, message)`` – release tags, peeled to commits.
* ``file_modes(hash, path, old_mode, new_mode)`` – mode changes (e.g. made executable), every non-merge commit.
* ``symbol_versions(uid, hash, seq, ts, path, qualname, kind, signature, params, start_line, end_line, loc,
  complexity)`` – one row per created/modified/moved symbol event: the signature and a cyclomatic-complexity
  estimate (decision points + 1) *of that version*.
* ``diff_lines.symbol`` – the uid of the innermost function/class containing each changed line, in the version the
  line belongs to (new file for '+', old file for '-'). Set by :func:`map_line_symbols` (index version 2).
* ``line_origins(fix_hash, path, line, origin_hash)`` – for lines removed by bug-fix commits: the commit that last
  wrote that line before the fix (``git blame`` of the fix's parent). Bounded by count and time per run.
* ``test_map_at(ref, test, target, source, weight)`` – the static test map rebuilt at each recent release tag.
* ``diff_lines_aux`` – like ``diff_lines`` but for lockfiles and vendored files, which ingestion ignores.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store

from ..ast.extractor import FileParse, language_for
from ..ast.parse_cache import ParseCache

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, commit_hash TEXT, ts INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS file_modes (hash TEXT, path TEXT, old_mode INTEGER, new_mode INTEGER,
  PRIMARY KEY (hash, path));
CREATE TABLE IF NOT EXISTS symbol_versions (
  uid TEXT, hash TEXT, seq INTEGER, ts INTEGER, path TEXT, qualname TEXT, kind TEXT, signature TEXT, params INTEGER,
  start_line INTEGER, end_line INTEGER, loc INTEGER, complexity INTEGER, PRIMARY KEY (uid, hash));
CREATE INDEX IF NOT EXISTS sv_uid ON symbol_versions(uid);
CREATE TABLE IF NOT EXISTS line_origins (fix_hash TEXT, path TEXT, line INTEGER, origin_hash TEXT,
  PRIMARY KEY (fix_hash, path, line));
CREATE INDEX IF NOT EXISTS lo_origin ON line_origins(origin_hash);
CREATE TABLE IF NOT EXISTS blame_done (fix_hash TEXT, path TEXT, PRIMARY KEY (fix_hash, path));
CREATE TABLE IF NOT EXISTS test_map_at (ref TEXT, test TEXT, target TEXT, source TEXT, weight REAL,
  PRIMARY KEY (ref, test, target, source));
CREATE TABLE IF NOT EXISTS diff_lines_aux (hash TEXT, seq INTEGER, ts INTEGER, path TEXT, sign TEXT, line INTEGER,
  text TEXT);
CREATE INDEX IF NOT EXISTS dla_path ON diff_lines_aux(path);
-- measured in a Docker sandbox (afd_oracle_ingest.sandbox); empty until `afd-oracle sandbox coverage` has run
CREATE TABLE IF NOT EXISTS covered_lines (path TEXT, line INTEGER, n_tests INTEGER, PRIMARY KEY (path, line));
CREATE TABLE IF NOT EXISTS coverage_files (path TEXT PRIMARY KEY, statements INTEGER, covered INTEGER);
CREATE TABLE IF NOT EXISTS covered_line_tests (path TEXT, line INTEGER, test TEXT, PRIMARY KEY (path, line, test));
CREATE INDEX IF NOT EXISTS clt_test ON covered_line_tests(test);
CREATE TABLE IF NOT EXISTS commit_patch_ids (hash TEXT PRIMARY KEY, patch_id TEXT);
CREATE INDEX IF NOT EXISTS cpi_patch ON commit_patch_ids(patch_id);
CREATE TABLE IF NOT EXISTS calls_at (ref TEXT, src TEXT, dst TEXT, PRIMARY KEY (ref, src, dst));
CREATE TABLE IF NOT EXISTS contracts_at (ref TEXT, kind TEXT, key TEXT, role TEXT, path TEXT, symbol TEXT);
CREATE INDEX IF NOT EXISTS ca_ref ON contracts_at(ref, kind, key);
-- opt-in GitHub sync (afd-oracle discussions): releases
CREATE TABLE IF NOT EXISTS github_releases (tag TEXT PRIMARY KEY, name TEXT, created_at TEXT, published_at TEXT,
  draft INTEGER, prerelease INTEGER);
-- rendered Helm charts (afd-oracle sandbox helm)
CREATE TABLE IF NOT EXISTS helm_renders (chart TEXT, commit_hash TEXT, ts INTEGER, manifest TEXT,
  PRIMARY KEY (chart, commit_hash));
CREATE TABLE IF NOT EXISTS mutation_results (target TEXT, line INTEGER, operator TEXT, original TEXT, mutant TEXT,
  status TEXT, at_commit TEXT, PRIMARY KEY (target, line, operator, mutant));
"""
RAISE_RX = re.compile(r"\b(?:raise|throw\s+new|throw)\s+([A-Za-z_][\w.]*)")


def raises(body: str | None) -> list[str]:
    """Exception types a function body raises or throws (sorted, de-duplicated; bare ``raise`` excluded)."""
    return sorted({m.group(1).rsplit(".", 1)[-1] for m in RAISE_RX.finditer(body or "")})


BRANCH_RX = re.compile(r"\b(if|elif|else\s+if|for|while|case|catch|except|and|or)\b|&&|\|\||\?(?!\.)")
AUX_RX = re.compile(r"(?i)(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Pipfile\.lock|"
                    r"Cargo\.lock|go\.sum|Gemfile\.lock|[^/]*\.lock)$|(^|/)(vendor|vendored|third_party|_vendor)/")
AUX_MAX_LINES = 3000
ENRICH_VERSION = "3"  # bump to rebuild symbol_versions and diff_lines.symbol (e.g. after a parser change)


def ensure_parsed(repo: pygit2.Repository, store: Store, cache: ParseCache, workers: int | None = None,
                  chunk: int = 800) -> int:
    """Parse every code blob that history touched but the parse cache lacks (the cache is keyed by extractor
    version, so an older ingest leaves it cold). Chunked to bound memory. Returns the number parsed."""
    from ..git.walker import blob_bytes
    wanted: dict[tuple[str, str], None] = {}
    for r in store.q("SELECT path, old_path, old_blob, new_blob FROM file_changes WHERE ignored=0"):
        for oid, path in ((r["new_blob"], r["path"]), (r["old_blob"], r["old_path"] or r["path"])):
            lang = language_for(path) if oid else None
            if lang:
                wanted[(oid, lang)] = None
    from ..ast.parse_cache import EXTRACTOR_VERSION
    have = set(cache.conn.execute("SELECT oid, lang FROM parses WHERE version=?", (EXTRACTOR_VERSION,)).fetchall())
    missing = [k for k in wanted if k not in have]
    n = 0
    for i in range(0, len(missing), chunk):
        jobs = {}
        for oid, lang in missing[i:i + chunk]:
            data = blob_bytes(repo, oid)
            if data is not None and bytes(1) not in data[:8000]:  # skip binary blobs
                jobs[(oid, lang)] = data
        cache.ensure(jobs, workers=workers)
        n += len(jobs)
    return n


def _reset_if_stale(store: Store) -> None:
    if store.get_meta("enrich_version") == ENRICH_VERSION:
        return
    with store.tx() as conn:
        conn.execute("DELETE FROM symbol_versions")
        conn.execute("UPDATE diff_lines SET symbol=NULL")
        conn.execute("DELETE FROM meta WHERE key='symver_seq'")
    store.set_meta("enrich_version", ENRICH_VERSION)


def ensure(store: Store) -> None:
    store.conn.executescript(SCHEMA)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(diff_lines)")}
    if cols and "symbol" not in cols:
        store.conn.execute("ALTER TABLE diff_lines ADD COLUMN symbol TEXT")
        store.conn.commit()
    if "raises" not in {r[1] for r in store.conn.execute("PRAGMA table_info(symbol_versions)")}:
        store.conn.execute("ALTER TABLE symbol_versions ADD COLUMN raises TEXT")
        store.conn.commit()


def complexity(body: str | None) -> int:
    """Decision points + 1 (a cyclomatic-complexity estimate that needs no per-language CFG)."""
    if not body:
        return 1
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(("#", "//", "*", "/*")))
    return 1 + len(BRANCH_RX.findall(code))


def _params(signature: str | None) -> int | None:
    if not signature or "(" not in signature:
        return None
    inner = signature[signature.index("(") + 1: signature.rfind(")")] if ")" in signature else ""
    parts = [p.strip() for p in re.split(r",(?![^\[\](){}]*[\])}])", inner) if p.strip()]
    return len([p for p in parts if p not in ("self", "cls", "*", "/")])


# ---------------------------------------------------------------------------- tags
def build_tags(repo: pygit2.Repository, store: Store) -> int:
    rows = []
    for ref in repo.references:
        if not ref.startswith("refs/tags/"):
            continue
        try:
            obj = repo.revparse_single(ref)
            msg = obj.message if isinstance(obj, pygit2.Tag) else None
            commit = obj.peel(pygit2.Commit)
        except Exception:
            continue
        rows.append((ref[len("refs/tags/"):], str(commit.id), commit.commit_time, (msg or "")[:500]))
    with store.tx() as conn:
        conn.execute("DELETE FROM tags")
        conn.executemany("INSERT OR REPLACE INTO tags VALUES (?,?,?,?)", rows)
    return len(rows)


# ---------------------------------------------------------------------------- file modes
def build_file_modes(repo: pygit2.Repository, store: Store) -> int:
    done = int(store.get_meta("modes_seq", "-1") or -1)
    rows, last = [], done
    for c in store.q("SELECT hash, seq FROM commits WHERE seq > ? AND is_merge=0 ORDER BY seq", (done,)):
        obj = repo.get(c["hash"])
        if obj is None:
            continue
        try:
            diff = repo.diff(obj.parents[0], obj) if obj.parents else obj.tree.diff_to_tree(swap=True)
        except Exception:
            continue
        for d in diff.deltas:
            om, nm = d.old_file.mode, d.new_file.mode
            added_exec = d.status_char() == "A" and nm == pygit2.GIT_FILEMODE_BLOB_EXECUTABLE
            if (om and nm and om != nm) or added_exec:
                rows.append((c["hash"], d.new_file.path, om, nm))
        last = c["seq"]
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO file_modes VALUES (?,?,?,?)", rows)
        conn.execute("INSERT INTO meta(key, value) VALUES ('modes_seq', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (str(last),))
    return len(rows)


# ---------------------------------------------------------------------------- symbol versions
def build_symbol_versions(store: Store, cache: ParseCache) -> int:
    done = int(store.get_meta("symver_seq", "-1") or -1)
    events = store.q(
        "SELECT se.uid, se.hash, se.seq, se.ts, se.path, se.qualname, se.kind, fc.new_blob "
        "FROM symbol_events se JOIN file_changes fc ON fc.hash=se.hash AND fc.path=se.path "
        "WHERE se.seq > ? AND se.change IN ('created', 'modified', 'moved') ORDER BY se.seq", (done,))
    parses: dict[str, FileParse | None] = {}
    rows, last = [], done
    for e in events:
        lang = language_for(e["path"])
        oid = e["new_blob"]
        if not lang or not oid:
            continue
        if oid not in parses:
            parses[oid] = cache.get(oid, lang)
            if len(parses) > 4000:
                parses.clear()
        fp = parses.get(oid)
        sym = next((s for s in (fp.symbols if fp else []) if s.qualname == e["qualname"] and s.kind == e["kind"]), None)
        if sym is None:
            continue
        rows.append((e["uid"], e["hash"], e["seq"], e["ts"], e["path"], e["qualname"], e["kind"], sym.signature[:400],
                     _params(sym.signature), sym.start_line, sym.end_line, sym.end_line - sym.start_line + 1,
                     complexity(sym.body) if e["kind"] != "variable" else None,
                     json.dumps(raises(sym.body)) if e["kind"] != "variable" else None))
        last = max(last, e["seq"])
        if len(rows) >= 20000:
            _flush_versions(store, rows, last)
            rows = []
    _flush_versions(store, rows, last)
    return store.q1("SELECT count(*) n FROM symbol_versions")["n"]


def _flush_versions(store: Store, rows: list, last: int) -> None:
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO symbol_versions(uid, hash, seq, ts, path, qualname, kind, signature, "
                         "params, start_line, end_line, loc, complexity, raises) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         rows)
        conn.execute("INSERT INTO meta(key, value) VALUES ('symver_seq', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (str(last),))


# ---------------------------------------------------------------------------- line -> symbol
def _innermost(fp: FileParse | None, line: int | None):
    if fp is None or line is None:
        return None
    best = None
    for s in fp.symbols:
        if s.kind != "variable" and s.start_line <= line <= s.end_line:
            if best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line):
                best = s
    return best


def map_line_symbols(store: Store, cache: ParseCache, budget_s: float = 900,
                     repo: pygit2.Repository | None = None) -> int:
    """Fill ``diff_lines.symbol`` for rows that have none yet (code files only)."""
    t0 = time.perf_counter()
    todo = store.q("SELECT DISTINCT d.hash, d.path, fc.old_blob, fc.new_blob, fc.old_path FROM diff_lines d "
                   "JOIN file_changes fc ON fc.hash=d.hash AND fc.path=d.path WHERE d.symbol IS NULL")
    n = 0
    batch: list[tuple] = []
    for t in todo:
        if time.perf_counter() - t0 > budget_s:
            break
        lang = language_for(t["path"])
        if not lang:
            batch.append(("", t["hash"], t["path"], None, None))  # mark non-code as done
            continue
        new_fp = cache.get(t["new_blob"], lang) if t["new_blob"] else None
        old_fp = cache.get(t["old_blob"], language_for(t["old_path"] or t["path"]) or lang) if t["old_blob"] else None
        rows = store.q("SELECT rowid, sign, line FROM diff_lines WHERE hash=? AND path=?", (t["hash"], t["path"]))
        for r in rows:
            fp, path = (new_fp, t["path"]) if r["sign"] == "+" else (old_fp, t["old_path"] or t["path"])
            s = _innermost(fp, r["line"])
            uid = symbol_uid(store.repo_id, path, s.qualname, s.kind) if s else ""
            batch.append((uid, None, None, r["rowid"], None))
            n += 1
        if len(batch) > 20000:
            _flush_symbols(store, batch)
            batch = []
    _flush_symbols(store, batch)
    n += _map_from_trees(store, cache, repo) if repo is not None else 0
    return n


def _map_from_trees(store: Store, cache: ParseCache, repo: pygit2.Repository) -> int:
    """Rows whose (commit, path) has no file_changes entry (history recorded the file under another path, e.g. a
    rename edge case): read both versions straight from the commit and its first parent."""
    from ..git.walker import blob_bytes
    todo = store.q("SELECT DISTINCT hash, path FROM diff_lines WHERE symbol IS NULL")
    if not todo:
        return 0
    pairs: list[tuple[str, str, str | None, str | None]] = []
    jobs: dict[tuple[str, str], bytes] = {}
    for t in todo:
        lang = language_for(t["path"])
        commit = repo.get(t["hash"])
        new_oid = old_oid = None
        if lang and commit is not None:
            try:
                new_oid = str(commit.tree[t["path"]].id)
            except KeyError:
                pass
            if commit.parents:
                try:
                    old_oid = str(commit.parents[0].tree[t["path"]].id)
                except KeyError:
                    pass
            for oid in (new_oid, old_oid):
                if oid and (oid, lang) not in jobs:
                    data = blob_bytes(repo, oid)
                    if data is not None and bytes(1) not in data[:8000]:
                        jobs[(oid, lang)] = data
        pairs.append((t["hash"], t["path"], new_oid, old_oid))
        if len(jobs) >= 800:
            cache.ensure(jobs)
            jobs = {}
    cache.ensure(jobs)
    n, batch = 0, []
    for h, path, new_oid, old_oid in pairs:
        lang = language_for(path)
        if not lang:
            batch.append(("", h, path, None, None))
            continue
        new_fp = cache.get(new_oid, lang) if new_oid else None
        old_fp = cache.get(old_oid, lang) if old_oid else None
        for r in store.q("SELECT rowid, sign, line FROM diff_lines WHERE hash=? AND path=?", (h, path)):
            s = _innermost(new_fp if r["sign"] == "+" else old_fp, r["line"])
            batch.append((symbol_uid(store.repo_id, path, s.qualname, s.kind) if s else "", None, None, r["rowid"], None))
            n += 1
        if len(batch) > 20000:
            _flush_symbols(store, batch)
            batch = []
    _flush_symbols(store, batch)
    return n


def _flush_symbols(store: Store, batch: list[tuple]) -> None:
    with store.tx() as conn:
        conn.executemany("UPDATE diff_lines SET symbol=? WHERE rowid=?", [(u, rid) for u, _, _, rid, _ in batch if rid])
        conn.executemany("UPDATE diff_lines SET symbol='' WHERE hash=? AND path=? AND symbol IS NULL",
                         [(h, p) for u, h, p, rid, _ in batch if rid is None and h])


# ---------------------------------------------------------------------------- blame of fix lines
def build_line_origins(repo: pygit2.Repository, store: Store, max_files: int = 3000, budget_s: float = 300) -> dict:
    """``git blame`` (at the fix's parent) of every line a bug-fix commit removed. Bounded per run; resumes."""
    t0 = time.perf_counter()
    done = {(r["fix_hash"], r["path"]) for r in store.q("SELECT fix_hash, path FROM blame_done")}
    todo = [r for r in store.q("SELECT DISTINCT hash, path FROM fix_events ORDER BY ts DESC")
            if (r["hash"], r["path"]) not in done][:max_files]
    rows, marks, n_files = [], [], 0
    flags = getattr(pygit2, "GIT_BLAME_IGNORE_WHITESPACE", 0)
    for t in todo:
        if time.perf_counter() - t0 > budget_s:
            break
        lines = [r["line"] for r in store.q("SELECT line FROM diff_lines WHERE hash=? AND path=? AND sign='-' "
                                            "AND line IS NOT NULL", (t["hash"], t["path"]))]
        commit = repo.get(t["hash"])
        marks.append((t["hash"], t["path"]))
        if not lines or commit is None or not commit.parents:
            continue
        parent = commit.parents[0]
        try:
            blame = repo.blame(t["path"], newest_commit=parent.id, min_line=min(lines), max_line=max(lines),
                               flags=flags)
        except Exception:
            continue
        n_files += 1
        for ln in lines:
            try:
                hunk = blame.for_line(ln)
            except Exception:
                continue
            rows.append((t["hash"], t["path"], ln, str(hunk.final_commit_id)))
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO line_origins VALUES (?,?,?,?)", rows)
        conn.executemany("INSERT OR REPLACE INTO blame_done VALUES (?,?)", marks)
    remaining = max(0, store.q1("SELECT count(*) n FROM (SELECT DISTINCT hash, path FROM fix_events)")["n"]
                    - store.q1("SELECT count(*) n FROM blame_done")["n"])
    return {"files_blamed": n_files, "lines": len(rows), "remaining": remaining}


# ---------------------------------------------------------------------------- patch ids
def build_patch_ids(repo: pygit2.Repository, store: Store) -> int:
    """``git patch-id`` of every non-merge commit: equal ids mean the same change (re-applied, cherry-picked)."""
    done = int(store.get_meta("patchid_seq", "-1") or -1)
    rows, last = [], done
    for c in store.q("SELECT hash, seq FROM commits WHERE seq > ? AND is_merge=0 ORDER BY seq", (done,)):
        obj = repo.get(c["hash"])
        last = c["seq"]
        if obj is None or not obj.parents:
            continue
        try:
            pid = repo.diff(obj.parents[0], obj).patchid
        except Exception:
            continue
        if pid is not None:
            rows.append((c["hash"], str(pid)))
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO commit_patch_ids VALUES (?,?)", rows)
        conn.execute("INSERT INTO meta(key, value) VALUES ('patchid_seq', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (str(last),))
    return len(rows)


# ---------------------------------------------------------------------------- snapshots at release tags
def _calls_at(srcs: dict, repo_id: str) -> list[tuple[str, str]]:
    """Name-resolved call edges in one snapshot: a call to a name defined exactly once (by its last qualname part)."""
    defs: dict[str, list[str]] = {}
    for path, src in srcs.items():
        for sym in (src.parse.symbols if getattr(src, "parse", None) else []):
            if sym.kind in ("function", "class"):
                defs.setdefault(sym.qualname.rsplit(".", 1)[-1], []).append(
                    symbol_uid(repo_id, path, sym.qualname, sym.kind))
    edges = set()
    for path, src in srcs.items():
        for sym in (src.parse.symbols if getattr(src, "parse", None) else []):
            if sym.kind != "function":
                continue
            me = symbol_uid(repo_id, path, sym.qualname, sym.kind)
            for call in sym.calls:
                target = defs.get(call.lstrip("."))
                if target and len(target) == 1 and target[0] != me:
                    edges.add((me, target[0]))
    return sorted(edges)


def build_test_map_at(repo: pygit2.Repository, store: Store, cache: ParseCache, ignore, n_tags: int = 6) -> int:
    """At each recent release tag: the static test map, name-resolved call edges and string contracts."""
    from ..safety import contracts, testmap
    from ..safety.source import sources_at
    have = ({r["ref"] for r in store.q("SELECT DISTINCT ref FROM test_map_at")}
            & {r["ref"] for r in store.q("SELECT DISTINCT ref FROM calls_at")}
            & {r["ref"] for r in store.q("SELECT DISTINCT ref FROM contracts_at")})
    tags = store.q("SELECT name, commit_hash FROM tags ORDER BY ts DESC LIMIT ?", (n_tags,))
    keep = {t["name"] for t in tags}
    tmap, calls, cons = [], [], []
    for t in tags:
        if t["name"] in have:
            continue
        try:
            srcs = sources_at(repo, t["commit_hash"], cache, ignore)
        except Exception:
            continue
        tmap += [(t["name"], *r) for r in testmap.build(store, srcs, store.repo_id)]
        calls += [(t["name"], a, b) for a, b in _calls_at(srcs, store.repo_id)]
        for src in srcs.values():
            try:
                cons += [(t["name"], c.kind, c.key, c.role, c.path, c.symbol)
                         for c in contracts.extract(src, store.repo_id)]
            except Exception:
                continue
    todo = keep - have
    with store.tx() as conn:
        for table in ("test_map_at", "calls_at", "contracts_at"):
            conn.executemany(f"DELETE FROM {table} WHERE ref=?", [(r,) for r in (have - keep) | todo])
        conn.executemany("INSERT OR REPLACE INTO test_map_at VALUES (?,?,?,?,?)", tmap)
        conn.executemany("INSERT OR REPLACE INTO calls_at VALUES (?,?,?)", calls)
        conn.executemany("INSERT INTO contracts_at VALUES (?,?,?,?,?,?)", cons)
    return store.q1("SELECT count(DISTINCT ref) n FROM test_map_at")["n"]


# ---------------------------------------------------------------------------- lockfile / vendored lines
def build_aux_lines(repo: pygit2.Repository, store: Store) -> int:
    done = int(store.get_meta("aux_seq", "-1") or -1)
    todo = store.q("SELECT fc.hash, fc.path, c.seq, c.ts FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
                   "WHERE fc.ignored=1 AND c.seq > ? AND c.is_merge=0 ORDER BY c.seq", (done,))
    rows, last = [], done
    for t in todo:
        last = max(last, t["seq"])
        if not AUX_RX.search(t["path"]):
            continue
        commit = repo.get(t["hash"])
        if commit is None:
            continue
        try:  # pygit2's diff has no path filter; diff the two blobs of this one file instead
            fc = store.q1("SELECT old_blob, new_blob FROM file_changes WHERE hash=? AND path=?", (t["hash"], t["path"]))
            old = repo.get(fc["old_blob"]) if fc and fc["old_blob"] else None
            new = repo.get(fc["new_blob"]) if fc and fc["new_blob"] else None
            if old is None and new is None:
                continue
            patch = pygit2.Patch.create_from(old, new, context_lines=0)
        except Exception:
            continue
        n = 0
        for patch in [patch]:
            if patch is None or patch.delta.is_binary:
                continue
            for h in patch.hunks:
                for ln in h.lines:
                    if ln.origin in "+-" and n < AUX_MAX_LINES:
                        rows.append((t["hash"], t["seq"], t["ts"], t["path"], ln.origin,
                                     ln.new_lineno if ln.origin == "+" else ln.old_lineno, ln.content.rstrip()[:300]))
                        n += 1
    with store.tx() as conn:
        conn.executemany("INSERT INTO diff_lines_aux VALUES (?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO meta(key, value) VALUES ('aux_seq', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value", (str(last),))
    return len(rows)


def build_all(repo: pygit2.Repository, store: Store, settings: Settings, cache: ParseCache | None = None,
              ignore=None, log: Callable[[str], None] = lambda *_: None) -> dict:
    from ..ignore import IgnoreRules
    ensure(store)
    _reset_if_stale(store)
    cache = cache or ParseCache(settings.parse_cache_path)
    ignore = ignore or IgnoreRules.load()
    out: dict[str, object] = {}
    for name, fn in (("tags", lambda: build_tags(repo, store)),
                     ("parse_history", lambda: ensure_parsed(repo, store, cache)),
                     ("file_modes", lambda: build_file_modes(repo, store)),
                     ("patch_ids", lambda: build_patch_ids(repo, store)),
                     ("symbol_versions", lambda: build_symbol_versions(store, cache)),
                     ("line_symbols", lambda: map_line_symbols(store, cache, repo=repo)),
                     ("line_origins", lambda: build_line_origins(
                         repo, store, budget_s=float(getattr(settings, "enrich_blame_budget_s", 300)))),
                     ("test_map_at", lambda: build_test_map_at(repo, store, cache, ignore,
                                                               int(getattr(settings, "safety_api_tags", 6)))),
                     ("aux_lines", lambda: build_aux_lines(repo, store))):
        t = time.perf_counter()
        try:
            out[name] = fn()
        except Exception as e:  # enrichment is best-effort; builders degrade to their proxies
            out[name] = {"error": f"{type(e).__name__}: {e}"}
        log(f"  enrich {name}: {out[name]} ({time.perf_counter() - t:.1f}s)")
    return out
