"""Harness for catalogue builder tests: plant a tiny history, ingest it, run builders, inspect hits.

    def test_x(mini):
        r = mini()                                   # fresh empty repo + its own data dir
        r.commit({"app/core.py": "def f():\n    return 1\n"}, "Add f", days=0)
        r.commit({"app/core.py": "def f():\n    return 2\n"}, "Fix f returning wrong value", days=10)
        r.delete("app/old.py", "Remove old module", days=12)
        ctx = r.ingest()                             # full deterministic ingest (no graph, no vectors) -> Ctx
        hits = r.run("fix_without_regression_test")  # list[Hit] straight from the builder (no cap, no ranking)
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

START = datetime(2023, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
AUTHORS = {"alice": ("Alice Archer", "alice@example.com"), "bob": ("Bob Builder", "bob@example.com"),
           "carol": ("Carol Coder", "carol@example.com"), "bot": ("dependabot[bot]", "bot@users.noreply.github.com")}


class MiniRepo:
    def __init__(self, root: Path, data: Path, name: str):
        self.root, self.data, self.name = root, data, name
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.ctx = None
        self._ingest_ctx = None

    def git(self, *args: str, days: float = 0, author: str = "alice") -> str:
        name, email = AUTHORS.get(author, (author, f"{author}@example.com"))
        stamp = (START + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
               "GIT_COMMITTER_EMAIL": email, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", "-C", str(self.root), *args], env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, files: dict[str, str | None], message: str, days: float = 0, author: str = "alice") -> str:
        """Write (or, with None, delete) files and commit. Returns the full hash."""
        for rel, text in files.items():
            p = self.root / rel
            if text is None:
                if p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode())
        self.git("add", "-A", days=days, author=author)
        self.git("commit", "-q", "--allow-empty", "-m", message, days=days, author=author)
        return self.git("rev-parse", "HEAD")

    def delete(self, rel: str, message: str, days: float = 0, author: str = "alice") -> str:
        return self.commit({rel: None}, message, days, author)

    def branch(self, name: str) -> None:
        self.git("checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)

    def merge(self, name: str, message: str, days: float = 0, author: str = "alice") -> str:
        self.git("merge", "-q", "--no-ff", "-m", message, name, days=days, author=author)
        return self.git("rev-parse", "HEAD")

    def ingest(self):
        """Full deterministic ingest into this repo's own data dir; returns a catalogue Ctx."""
        from afd_oracle_core.config import Settings
        from afd_oracle_ingest.catalog.base import Ctx
        from afd_oracle_ingest.pipeline import open_context, run_deterministic

        s = Settings(data_dir=self.data, embedder="hash", qdrant_url=None, qdrant_path=":memory:",
                     safety_typed_calls=False, catalog=False)
        pctx = open_context(self.root, self.name, s, workers=1, log=lambda *_: None)
        run_deterministic(pctx, full=True)
        from afd_oracle_ingest.catalog.difflines import build_diff_lines
        from afd_oracle_ingest.catalog.enrich import build_all
        build_diff_lines(pctx.repo, pctx.store)
        build_all(pctx.repo, pctx.store, s, pctx.cache, pctx.ignore)
        self._ingest_ctx = pctx
        self.ctx = Ctx(pctx.repo, pctx.store, s)
        return self.ctx

    def run(self, cid: str) -> list:
        from afd_oracle_ingest.catalog import load_all
        if self.ctx is None:
            self.ingest()
        self.ctx._current = cid
        return list(load_all()[cid](self.ctx))


@pytest.fixture
def mini(tmp_path):
    n = {"i": 0}

    def make(name: str = "mini") -> MiniRepo:
        n["i"] += 1
        return MiniRepo(tmp_path / f"{name}{n['i']}", tmp_path / f"data{n['i']}", f"{name}{n['i']}")
    return make
