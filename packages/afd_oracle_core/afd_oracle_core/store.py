"""Per-repository SQLite history store.

The deterministic pipeline writes everything here first; Neo4j and Qdrant are projections of it. Keeping a local
store makes incremental runs cheap (no graph round-trips), lets co-change/metrics be recomputed offline, and gives
the citation gate a fast, authoritative source of commits, paths and symbols.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS commits (
  hash TEXT PRIMARY KEY, seq INTEGER UNIQUE, parents TEXT, author_name TEXT, author_email TEXT,
  ts INTEGER, message TEXT, is_merge INTEGER, is_bulk INTEGER, files_changed INTEGER,
  is_revert INTEGER DEFAULT 0, projected INTEGER DEFAULT 0, embedded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS commits_ts ON commits(ts);

CREATE TABLE IF NOT EXISTS file_changes (
  hash TEXT, path TEXT, old_path TEXT, status TEXT, added INTEGER, deleted INTEGER,
  old_blob TEXT, new_blob TEXT, lineage TEXT, ignored INTEGER DEFAULT 0,
  PRIMARY KEY (hash, path)
);
CREATE INDEX IF NOT EXISTS fc_path ON file_changes(path);
CREATE INDEX IF NOT EXISTS fc_old_path ON file_changes(old_path);
CREATE INDEX IF NOT EXISTS fc_lineage ON file_changes(lineage);

CREATE TABLE IF NOT EXISTS symbol_events (
  hash TEXT, uid TEXT, path TEXT, qualname TEXT, kind TEXT, change TEXT,
  body_hash_before TEXT, body_hash_after TEXT, old_uid TEXT, ts INTEGER, seq INTEGER,
  PRIMARY KEY (hash, uid, change)
);
CREATE INDEX IF NOT EXISTS se_uid ON symbol_events(uid);

CREATE TABLE IF NOT EXISTS symbols (
  uid TEXT PRIMARY KEY, path TEXT, qualname TEXT, name TEXT, kind TEXT, lang TEXT, parent TEXT,
  alive INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT, first_ts INTEGER, last_ts INTEGER,
  first_seq INTEGER, last_seq INTEGER, start_line INTEGER, end_line INTEGER, body_hash TEXT,
  signature TEXT, docstring TEXT, body TEXT, loc INTEGER, old_uid TEXT, modifications INTEGER DEFAULT 0,
  embedded_hash TEXT, metrics TEXT
);
CREATE INDEX IF NOT EXISTS sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS sym_path ON symbols(path);
CREATE INDEX IF NOT EXISTS sym_alive ON symbols(alive);

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, lang TEXT, alive INTEGER, loc INTEGER, churn INTEGER, commits INTEGER,
  first_seen TEXT, last_seen TEXT, last_ts INTEGER, lineage TEXT, metrics TEXT
);

CREATE TABLE IF NOT EXISTS reverts (
  reverter TEXT, reverted TEXT, method TEXT, partial INTEGER, overlap REAL,
  PRIMARY KEY (reverter, reverted)
);

CREATE TABLE IF NOT EXISTS branches (
  name TEXT PRIMARY KEY, ref TEXT, tip TEXT, tip_ts INTEGER, merged INTEGER, abandoned INTEGER,
  unique_count INTEGER, source TEXT, merge_commit TEXT
);
CREATE TABLE IF NOT EXISTS branch_commits (branch TEXT, hash TEXT, PRIMARY KEY (branch, hash));

CREATE TABLE IF NOT EXISTS edges (
  kind TEXT, src TEXT, dst TEXT, props TEXT, PRIMARY KEY (kind, src, dst)
);

CREATE TABLE IF NOT EXISTS cochange (
  a TEXT, b TEXT, count INTEGER, n_a INTEGER, n_b INTEGER, jaccard REAL, conf_ab REAL, conf_ba REAL,
  cross_dir INTEGER, lca_depth INTEGER, structural_link INTEGER, last_ts INTEGER, score REAL,
  shared_commits TEXT, PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY, kind TEXT, subject TEXT, score REAL, props TEXT
);

CREATE TABLE IF NOT EXISTS packets (
  id TEXT PRIMARY KEY, kind TEXT, subkind TEXT, priority REAL, body TEXT, status TEXT DEFAULT 'open',
  tokens INTEGER
);

CREATE TABLE IF NOT EXISTS insights (
  uid TEXT PRIMARY KEY, label TEXT, kind TEXT, title TEXT, summary TEXT, severity TEXT, confidence REAL,
  status TEXT, citations TEXT, dedupe_key TEXT, workflow_run TEXT, created_by_model TEXT,
  verifier_notes TEXT, votes TEXT, created_at REAL, updated_at REAL, graph_synced INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ins_dedupe ON insights(dedupe_key);

-- change-safety layer (afd_oracle_ingest.safety): deterministic, rebuilt from HEAD + history on every ingest
CREATE TABLE IF NOT EXISTS findings (
  id TEXT PRIMARY KEY, category TEXT, rule TEXT, severity TEXT, path TEXT, line INTEGER, symbol TEXT,
  message TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS findings_path ON findings(path);
CREATE INDEX IF NOT EXISTS findings_symbol ON findings(symbol);
CREATE TABLE IF NOT EXISTS contracts (
  kind TEXT, key TEXT, role TEXT, path TEXT, line INTEGER, symbol TEXT, PRIMARY KEY (kind, key, role, path, line)
);
CREATE INDEX IF NOT EXISTS contracts_path ON contracts(path);
CREATE TABLE IF NOT EXISTS test_map (
  test TEXT, target TEXT, source TEXT, weight REAL, PRIMARY KEY (test, target, source)
);
CREATE INDEX IF NOT EXISTS test_map_target ON test_map(target);
CREATE TABLE IF NOT EXISTS api_surface (
  ref TEXT, uid TEXT, path TEXT, qualname TEXT, kind TEXT, signature TEXT, deprecated TEXT, PRIMARY KEY (ref, uid)
);
CREATE TABLE IF NOT EXISTS fix_events (
  hash TEXT, uid TEXT, path TEXT, kinds TEXT, guards TEXT, tests TEXT, ts INTEGER, subject TEXT,
  PRIMARY KEY (hash, uid)
);
CREATE INDEX IF NOT EXISTS fix_uid ON fix_events(uid);
CREATE TABLE IF NOT EXISTS risk (subject TEXT PRIMARY KEY, kind TEXT, score REAL, factors TEXT);
CREATE TABLE IF NOT EXISTS discussions (
  id TEXT PRIMARY KEY, kind TEXT, number INTEGER, title TEXT, body TEXT, state TEXT, labels TEXT, url TEXT,
  author TEXT, created_at TEXT, closed_at TEXT, merged_at TEXT
);
CREATE TABLE IF NOT EXISTS discussion_commits (number INTEGER, hash TEXT, PRIMARY KEY (number, hash));
CREATE INDEX IF NOT EXISTS dc_hash ON discussion_commits(hash);
"""


class Store:
    @classmethod
    def readonly(cls, path: Path | str, repo_id: str, busy_ms: int = 2000) -> "Store":
        """A read-only connection that skips schema setup. In WAL mode reads never wait for a writer, so this never
        blocks behind a running ingest (the normal constructor runs CREATE ... IF NOT EXISTS, which takes the write
        lock). For dashboards and other read paths."""
        self = cls.__new__(cls)
        self.path = Path(path)
        self.repo_id = repo_id
        self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False,
                                    timeout=busy_ms / 1000)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        self.conn.execute("PRAGMA query_only=1")
        return self

    def __init__(self, path: Path | str, repo_id: str):
        self.path = Path(path)
        self.repo_id = repo_id
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(commits)")}
        if "on_main" not in cols:  # reachable from the default branch tip (set by the snapshot step)
            self.conn.execute("ALTER TABLE commits ADD COLUMN on_main INTEGER DEFAULT 0")
        # When a commit reached the main line: the first-parent commit (usually a merge) that brought it in, and its
        # committer time. Authored/committed dates of a PR commit can be weeks earlier than this.
        if "landed_in" not in cols:
            self.conn.execute("ALTER TABLE commits ADD COLUMN landed_in TEXT")
        if "landed_ts" not in cols:
            self.conn.execute("ALTER TABLE commits ADD COLUMN landed_ts INTEGER")
        self.conn.commit()

    # -- basic helpers -----------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def q1(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value if isinstance(value, str) else json.dumps(value)),
        )
        self.conn.commit()

    # -- lookups used by several layers -------------------------------------
    def known_commits(self) -> set[str]:
        return {r["hash"] for r in self.q("SELECT hash FROM commits")}

    def next_seq(self) -> int:
        row = self.q1("SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM commits")
        return int(row["n"])

    def resolve_commit(self, prefix: str) -> str | None:
        """Resolve a 7-40 char hex prefix to a unique full hash known to the store."""
        prefix = prefix.lower()
        if len(prefix) == 40:
            row = self.q1("SELECT hash FROM commits WHERE hash=?", (prefix,))
            return row["hash"] if row else None
        rows = self.q("SELECT hash FROM commits WHERE hash LIKE ? LIMIT 2", (prefix + "%",))
        return rows[0]["hash"] if len(rows) == 1 else None

    def commit(self, sha: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM commits WHERE hash=?", (sha,))

    def latest_ts(self) -> int:
        row = self.q1("SELECT COALESCE(MAX(ts), 0) AS t FROM commits")
        return int(row["t"])

    def symbol(self, uid: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM symbols WHERE uid=?", (uid,))

    def touched_paths(self, sha: str) -> set[str]:
        out: set[str] = set()
        for r in self.q("SELECT path, old_path FROM file_changes WHERE hash=?", (sha,)):
            out.add(r["path"])
            if r["old_path"]:
                out.add(r["old_path"])
        return out

    def touched_symbols(self, sha: str) -> set[str]:
        return {r["uid"] for r in self.q("SELECT uid FROM symbol_events WHERE hash=?", (sha,))}


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    return json.loads(s) if s else default
