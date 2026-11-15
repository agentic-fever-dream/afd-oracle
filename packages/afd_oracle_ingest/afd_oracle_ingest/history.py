"""Steps 2-4: walk new commits, record file changes with rename lineage, parse blobs, derive symbol lifecycles."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store

from .ast.extractor import FileParse, language_for
from .ast.parse_cache import ParseCache
from .git.walker import CommitRecord, blob_bytes, extract_commit, iter_new_commits, ref_tips
from .ignore import IgnoreRules
from .lifecycle import SymbolEvent, detect_moves, file_events


@dataclass
class HistoryStats:
    new_commits: int = 0
    file_changes: int = 0
    symbol_events: int = 0
    parsed_blobs: int = 0


def _load_lineage(store: Store) -> dict[str, str]:
    lineage: dict[str, str] = {}
    for r in store.q("SELECT fc.path, fc.lineage FROM file_changes fc JOIN commits c ON c.hash = fc.hash "
                     "ORDER BY c.seq"):
        lineage[r["path"]] = r["lineage"]
    return lineage


def _parse_jobs(repo: pygit2.Repository, records: list[CommitRecord], ignore: IgnoreRules) -> dict[tuple[str, str], bytes]:
    jobs: dict[tuple[str, str], bytes] = {}
    for rec in records:
        if rec.is_merge:
            continue
        for ch in rec.changes:
            if ch.binary or ignore.match(ch.path):
                continue
            for oid, path in ((ch.old_blob, ch.old_path or ch.path), (ch.new_blob, ch.path)):
                lang = language_for(path) if oid else None
                if lang and (oid, lang) not in jobs:
                    data = blob_bytes(repo, oid)
                    if data is not None and b"\0" not in data[:8000]:
                        jobs[(oid, lang)] = data
    return jobs


def _get_parse(cache: ParseCache, oid: str | None, path: str | None) -> FileParse | None:
    if not oid or not path:
        return None
    lang = language_for(path)
    return cache.get(oid, lang) if lang else None


def ingest_history(repo: pygit2.Repository, store: Store, cache: ParseCache, settings: Settings,
                   ignore: IgnoreRules, workers: int | None = None, batch: int = 400,
                   progress=None) -> HistoryStats:
    stats = HistoryStats()
    known = store.known_commits()
    hide = json.loads(store.get_meta("processed_tips", "[]"))
    commits = iter_new_commits(repo, known, hide)
    lineage = _load_lineage(store)
    seq = store.next_seq()
    misses_before = cache.misses

    for i in range(0, len(commits), batch):
        records = [extract_commit(repo, c) for c in commits[i:i + batch]]
        cache.ensure(_parse_jobs(repo, records, ignore), workers=workers)
        with store.tx() as conn:
            for rec in records:
                relevant = [ch for ch in rec.changes if not ignore.match(ch.path)]
                conn.execute(
                    "INSERT OR IGNORE INTO commits(hash, seq, parents, author_name, author_email, ts, message, "
                    "is_merge, is_bulk, files_changed) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rec.hash, seq, json.dumps(rec.parents), rec.author_name, rec.author_email, rec.ts, rec.message,
                     int(rec.is_merge), int(len(relevant) > settings.bulk_files), len(rec.changes)),
                )
                for ch in rec.changes:
                    if ch.status == "R" and ch.old_path:
                        lin = lineage.get(ch.old_path, ch.old_path)
                    else:
                        lin = lineage.get(ch.path, ch.path)
                    lineage[ch.path] = lin
                    conn.execute(
                        "INSERT OR REPLACE INTO file_changes VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rec.hash, ch.path, ch.old_path, ch.status, ch.added, ch.deleted, ch.old_blob, ch.new_blob,
                         lin, int(ignore.match(ch.path) or ch.binary)),
                    )
                    stats.file_changes += 1
                if not rec.is_merge:
                    events: list[SymbolEvent] = []
                    for ch in rec.changes:
                        if ch.binary or ignore.match(ch.path) or not language_for(ch.path):
                            continue
                        old_path = ch.old_path or ch.path
                        old = _get_parse(cache, ch.old_blob, old_path)
                        new = _get_parse(cache, ch.new_blob, ch.path) if ch.status != "D" else None
                        events += file_events(store.repo_id, old_path if ch.old_blob else None,
                                              ch.path if ch.status != "D" else None, old, new)
                    for ev in detect_moves(events):
                        _write_event(conn, store.repo_id, rec, seq, ev)
                        stats.symbol_events += 1
                seq += 1
                stats.new_commits += 1
        if progress:
            progress(min(i + batch, len(commits)), len(commits))

    store.set_meta("processed_tips", sorted(set(ref_tips(repo).values())))
    stats.parsed_blobs = cache.misses - misses_before
    return stats


def _write_event(conn, repo_id: str, rec: CommitRecord, seq: int, ev: SymbolEvent) -> None:
    conn.execute("INSERT OR REPLACE INTO symbol_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (rec.hash, ev.uid, ev.path, ev.qualname, ev.kind, ev.change, ev.body_hash_before,
                  ev.body_hash_after, ev.old_uid, rec.ts, seq))
    if ev.record is not None:
        r = ev.record
        parent_uid = None
        if r.parent:
            parent_uid = symbol_uid(repo_id, ev.path, r.parent, "class")
        conn.execute(
            """INSERT INTO symbols(uid, path, qualname, name, kind, lang, parent, first_seen, last_seen, first_ts,
                   last_ts, first_seq, last_seq, start_line, end_line, body_hash, signature, docstring, body, loc,
                   old_uid, modifications)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uid) DO UPDATE SET last_seen=excluded.last_seen, last_ts=excluded.last_ts,
                   last_seq=excluded.last_seq, start_line=excluded.start_line, end_line=excluded.end_line,
                   body_hash=excluded.body_hash, signature=excluded.signature, docstring=excluded.docstring,
                   body=excluded.body, loc=excluded.loc,
                   old_uid=COALESCE(excluded.old_uid, symbols.old_uid),
                   modifications=symbols.modifications + excluded.modifications""",
            (ev.uid, ev.path, r.qualname, r.name, r.kind, language_for(ev.path), parent_uid, rec.hash, rec.hash,
             rec.ts, rec.ts, seq, seq, r.start_line, r.end_line, r.body_hash, r.signature, r.docstring, r.body,
             r.end_line - r.start_line + 1, ev.old_uid, 1 if ev.change == "modified" else 0),
        )
    else:
        conn.execute("UPDATE symbols SET last_seen=?, last_ts=?, last_seq=? WHERE uid=?",
                     (rec.hash, rec.ts, seq, ev.uid))
