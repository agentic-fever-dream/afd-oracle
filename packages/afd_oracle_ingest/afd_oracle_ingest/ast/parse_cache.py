"""Blob-keyed parse cache: each (blob oid, language, extractor version) is parsed exactly once, ever.

Git blobs are content-addressed, so a file version that appears in 500 commits is parsed once. Misses are parsed
in a process pool when there are enough of them to amortise worker start-up.
"""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .extractor import EXTRACTOR_VERSION, FileParse, parse_source


def _parse_job(args: tuple[str, str, bytes]) -> tuple[str, str, dict]:
    oid, lang, data = args
    return oid, lang, parse_source(lang, data).to_dict()


class ParseCache:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS parses (oid TEXT, lang TEXT, version TEXT, data TEXT, "
            "PRIMARY KEY (oid, lang, version))"
        )
        self.hits = 0
        self.misses = 0
        self._mem: dict[tuple[str, str], FileParse] = {}

    def get(self, oid: str, lang: str) -> FileParse | None:
        key = (oid, lang)
        if key in self._mem:
            self.hits += 1
            return self._mem[key]
        row = self.conn.execute("SELECT data FROM parses WHERE oid=? AND lang=? AND version=?",
                                (oid, lang, EXTRACTOR_VERSION)).fetchone()
        if row is None:
            return None
        self.hits += 1
        fp = FileParse.from_dict(json.loads(row[0]))
        self._remember(key, fp)
        return fp

    def _remember(self, key: tuple[str, str], fp: FileParse) -> None:
        if len(self._mem) > 20000:
            self._mem.clear()
        self._mem[key] = fp

    def put_many(self, items: list[tuple[str, str, dict]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO parses VALUES (?,?,?,?)",
                              [(o, l, EXTRACTOR_VERSION, json.dumps(d)) for o, l, d in items])
        self.conn.commit()
        for o, l, d in items:
            self._remember((o, l), FileParse.from_dict(d))

    def ensure(self, jobs: dict[tuple[str, str], bytes], workers: int | None = None) -> None:
        """Parse every (oid, lang) in ``jobs`` that is not cached yet."""
        missing = [(o, l, data) for (o, l), data in jobs.items() if self.get(o, l) is None]
        self.hits -= len(jobs) - len(missing)  # get() above is a probe, not a use
        if not missing:
            return
        self.misses += len(missing)
        workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 2)
        if workers <= 1 or len(missing) < 64:
            results = [_parse_job(m) for m in missing]
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_parse_job, missing, chunksize=16))
        self.put_many(results)

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses}
