"""Step 5: structural snapshot at the default branch HEAD (alive symbols, file table, structural edges)."""
from __future__ import annotations

import json

import pygit2

from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store

from .ast.extractor import FileParse, language_for
from .ast.parse_cache import ParseCache
from .ast.resolve import Snapshot, build_edges
from .git.walker import blob_bytes, default_branch, tree_files
from .ignore import IgnoreRules


def landing_points(repo: pygit2.Repository, head: str) -> dict[str, tuple[str, int]]:
    """hash -> (first-parent commit that brought it onto the main line, that commit's committer time).

    Walks the first-parent chain of the default branch. A non-merge commit on it lands as itself. A merge brings in
    everything reachable from it but not from its first parent. Total work is linear in the main-line history.
    """
    chain = []
    c = repo.get(head)
    while c is not None:
        chain.append(c)
        c = c.parents[0] if c.parents else None
    landed: dict[str, tuple[str, int]] = {}
    for m in reversed(chain):
        mid = str(m.id)
        if len(m.parents) < 2:
            landed[mid] = (mid, m.commit_time)
            continue
        walker = repo.walk(m.id, pygit2.GIT_SORT_NONE)
        walker.hide(m.parents[0].id)
        for x in walker:
            landed.setdefault(str(x.id), (mid, m.commit_time))
    return landed


def build_snapshot(repo: pygit2.Repository, store: Store, cache: ParseCache, ignore: IgnoreRules,
                   workers: int | None = None) -> dict:
    ref, head = default_branch(repo)
    all_files = {p: o for p, o in tree_files(repo, head).items() if not ignore.match(p)}
    jobs: dict[tuple[str, str], bytes] = {}
    for path, oid in all_files.items():
        lang = language_for(path)
        if lang and (oid, lang) not in jobs:
            data = blob_bytes(repo, oid)
            if data is not None and b"\0" not in data[:8000]:
                jobs[(oid, lang)] = data
    cache.ensure(jobs, workers=workers)
    parses: dict[str, FileParse] = {}
    for path, oid in all_files.items():
        lang = language_for(path)
        if lang:
            fp = cache.get(oid, lang)
            if fp is not None:
                parses[path] = fp

    snap = Snapshot(store.repo_id, parses)
    edges = build_edges(snap)
    head_row = store.commit(head)
    head_seq = head_row["seq"] if head_row else None

    mainline = [(str(c.id),) for c in repo.walk(head)]
    landed = landing_points(repo, head)
    with store.tx() as conn:
        conn.execute("UPDATE commits SET on_main=0, landed_in=NULL, landed_ts=NULL")
        conn.executemany("UPDATE commits SET on_main=1 WHERE hash=?", mainline)
        conn.executemany("UPDATE commits SET landed_in=?, landed_ts=? WHERE hash=?",
                         [(m, ts, h) for h, (m, ts) in landed.items()])
        conn.execute("UPDATE symbols SET alive=0")
        for uid, (path, rec) in snap.records.items():
            parent_uid = None
            if rec.parent:
                parent_uid = snap.sym_uid.get((path, rec.parent))
            exists = conn.execute("SELECT 1 FROM symbols WHERE uid=?", (uid,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE symbols SET alive=1, start_line=?, end_line=?, body_hash=?, signature=?, docstring=?, "
                    "body=?, loc=?, parent=?, lang=? WHERE uid=?",
                    (rec.start_line, rec.end_line, rec.body_hash, rec.signature, rec.docstring, rec.body,
                     rec.end_line - rec.start_line + 1, parent_uid, language_for(path), uid))
            else:  # e.g. history ingested before this language was supported
                first = conn.execute(
                    "SELECT c.hash, c.ts, c.seq FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
                    "WHERE fc.path=? ORDER BY c.seq LIMIT 1", (path,)).fetchone()
                conn.execute(
                    """INSERT INTO symbols(uid, path, qualname, name, kind, lang, parent, alive, first_seen, last_seen,
                       first_ts, last_ts, first_seq, last_seq, start_line, end_line, body_hash, signature, docstring,
                       body, loc) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, path, rec.qualname, rec.name, rec.kind, language_for(path), parent_uid,
                     first["hash"] if first else head, head, first["ts"] if first else None,
                     head_row["ts"] if head_row else None, first["seq"] if first else head_seq, head_seq,
                     rec.start_line, rec.end_line, rec.body_hash, rec.signature, rec.docstring, rec.body,
                     rec.end_line - rec.start_line + 1))

        # files table: every path ever seen (not ignored), aggregated from history
        conn.execute("DELETE FROM files")
        rows = conn.execute(
            """SELECT fc.path AS path, SUM(CASE WHEN c.is_merge=0 THEN fc.added + fc.deleted ELSE 0 END) AS churn,
                      SUM(CASE WHEN c.is_merge=0 THEN 1 ELSE 0 END) AS commits,
                      MIN(c.seq) AS fseq, MAX(c.seq) AS lseq, MAX(c.ts) AS last_ts
               FROM file_changes fc JOIN commits c ON c.hash = fc.hash
               WHERE fc.ignored = 0 GROUP BY fc.path""").fetchall()
        seq_to_hash = {r["seq"]: r["hash"] for r in conn.execute("SELECT seq, hash FROM commits")}
        lineage = {r["path"]: r["lineage"] for r in conn.execute(
            "SELECT fc.path, fc.lineage FROM file_changes fc JOIN commits c ON c.hash=fc.hash ORDER BY c.seq")}
        for r in rows:
            p = r["path"]
            loc = parses[p].loc if p in parses else None
            if loc is None and p in all_files:
                data = blob_bytes(repo, all_files[p])
                loc = data.count(b"\n") if data and b"\0" not in data[:8000] else None
            conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (p, language_for(p), int(p in all_files), loc, r["churn"], r["commits"],
                          seq_to_hash.get(r["fseq"]), seq_to_hash.get(r["lseq"]), r["last_ts"], lineage.get(p), None))

        conn.execute("DELETE FROM edges")
        conn.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?)",
                         [(k, s, d, json.dumps(p, sort_keys=True)) for k, s, d, p in edges])
    store.set_meta("default_branch", ref)
    store.set_meta("head", head)
    counts: dict[str, int] = {}
    for k, *_ in edges:
        counts[k] = counts.get(k, 0) + 1
    return {"default_branch": ref, "head": head, "files": len(all_files), "parsed_files": len(parses),
            "alive_symbols": len(snap.records), **{f"edges_{k.lower()}": v for k, v in counts.items()}}


def file_of(uid: str, repo_id: str) -> str:
    """'sym:<repo>:<path>:<qualname>:<kind>' -> path (paths never contain ':' in practice on git)."""
    body = uid[len(f"sym:{repo_id}:"):]
    return body.rsplit(":", 2)[0]


__all__ = ["build_snapshot", "file_of", "symbol_uid"]
