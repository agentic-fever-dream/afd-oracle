"""``diff_lines``: every added/removed line of every ingested commit, so catalogue builders can search history with SQL.

One row per changed line (first-parent diff, no context). Built incrementally: only commits with a sequence number
above ``meta.difflines_seq`` are indexed. Skipped, by design, to bound size: merge commits, bulk commits (they are
mostly mechanical), ignored files, binary files, files with more than ``MAX_FILE_LINES`` changed lines (generated,
vendored or reformatted). Lines are truncated to ``MAX_LINE_CHARS``.
"""
from __future__ import annotations

import pygit2

from afd_oracle_core.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS diff_lines (
  hash TEXT, seq INTEGER, ts INTEGER, path TEXT, sign TEXT, line INTEGER, text TEXT
);
CREATE INDEX IF NOT EXISTS dl_path ON diff_lines(path);
CREATE INDEX IF NOT EXISTS dl_hash ON diff_lines(hash);
CREATE INDEX IF NOT EXISTS dl_seq ON diff_lines(seq);
"""
MAX_FILE_LINES = 1500
MAX_LINE_CHARS = 300
BATCH = 200


def ensure_table(store: Store) -> None:
    store.conn.executescript(SCHEMA)


def build_diff_lines(repo: pygit2.Repository, store: Store, progress=None) -> dict:
    ensure_table(store)
    done_seq = int(store.get_meta("difflines_seq", "-1") or -1)
    commits = store.q("SELECT hash, seq, ts FROM commits WHERE seq > ? AND is_merge=0 AND is_bulk=0 ORDER BY seq",
                      (done_seq,))
    ignored = {(r["hash"], r["path"]) for r in store.q(
        "SELECT hash, path FROM file_changes WHERE ignored=1 AND hash IN "
        "(SELECT hash FROM commits WHERE seq > ?)", (done_seq,))}
    rows: list[tuple] = []
    n_lines = n_files = skipped_big = 0
    last_seq = done_seq
    for i, c in enumerate(commits):
        obj = repo.get(c["hash"])
        if obj is None:
            continue
        try:
            diff = (repo.diff(obj.parents[0], obj, context_lines=0) if obj.parents
                    else obj.tree.diff_to_tree(swap=True, context_lines=0))
        except Exception:
            continue
        for patch in diff:
            if patch is None or patch.delta.is_binary:
                continue
            path = patch.delta.new_file.path or patch.delta.old_file.path
            if (c["hash"], path) in ignored:
                continue
            _, adds, dels = patch.line_stats
            if adds + dels > MAX_FILE_LINES:
                skipped_big += 1
                continue
            n_files += 1
            for hunk in patch.hunks:
                for ln in hunk.lines:
                    if ln.origin not in "+-":
                        continue
                    no = ln.new_lineno if ln.origin == "+" else ln.old_lineno
                    rows.append((c["hash"], c["seq"], c["ts"], path, ln.origin, no,
                                 ln.content.rstrip("\r\n")[:MAX_LINE_CHARS]))
        last_seq = max(last_seq, c["seq"])
        if len(rows) > 20000 or i == len(commits) - 1 or (i and i % BATCH == 0):
            with store.tx() as conn:
                conn.executemany("INSERT INTO diff_lines(hash, seq, ts, path, sign, line, text) VALUES (?,?,?,?,?,?,?)", rows)
                conn.execute("INSERT INTO meta(key, value) VALUES ('difflines_seq', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(last_seq),))
            n_lines += len(rows)
            rows = []
            if progress:
                progress(i + 1, len(commits))
    max_seq = store.q1("SELECT max(seq) m FROM commits")["m"]
    if max_seq is not None:
        store.set_meta("difflines_seq", str(max(last_seq, max_seq)))
    return {"commits": len(commits), "files": n_files, "lines": n_lines, "skipped_large_files": skipped_big}


def reset_diff_lines(store: Store) -> None:
    ensure_table(store)
    with store.tx() as conn:
        conn.execute("DELETE FROM diff_lines")
        conn.execute("DELETE FROM meta WHERE key='difflines_seq'")
