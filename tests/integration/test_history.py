"""Roadmap phases 3-4: git walker, temporal layer, AST lifecycle, parse cache, incremental ingest."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import git

from afd_oracle_core.config import Settings
from afd_oracle_ingest.pipeline import open_context, run_deterministic


def test_commit_counts_match_git(ingested, fixture_repo):
    ctx, facts, _ = ingested
    repo, _ = fixture_repo
    expected = int(git(repo, "rev-list", "--all", "--count").strip())
    assert ctx.store.q1("SELECT count(*) n FROM commits")["n"] == expected
    parents = sum(len(json.loads(r["parents"])) for r in ctx.store.q("SELECT parents FROM commits"))
    expected_parents = sum(len(line.split()) - 1 for line in git(repo, "rev-list", "--all", "--parents").splitlines())
    assert parents == expected_parents
    authors = {r["author_email"] for r in ctx.store.q("SELECT author_email FROM commits")}
    assert authors == set(git(repo, "log", "--all", "--format=%ae").split())


def test_touched_line_counts_match_numstat(ingested, fixture_repo):
    ctx, _, _ = ingested
    repo, _ = fixture_repo
    for c in ctx.store.q("SELECT hash FROM commits WHERE is_merge=0"):
        out = git(repo, "show", "--numstat", "--format=", "-M", c["hash"])
        expected = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            a, d, path = line.split("\t", 2)
            if " => " in path:  # rename notation: util/{strings.py => text.py} or old => new
                if "{" in path:
                    pre, rest = path.split("{", 1)
                    mid, post = rest.split("}", 1)
                    path = pre + mid.split(" => ")[1] + post
                else:
                    path = path.split(" => ")[1]
            expected[path] = (int(a), int(d))
        got = {r["path"]: (r["added"], r["deleted"]) for r in ctx.store.q(
            "SELECT path, added, deleted FROM file_changes WHERE hash=?", (c["hash"],))}
        assert got == expected, c["hash"]


def test_rename_and_merge_recorded(ingested):
    ctx, facts, _ = ingested
    r = ctx.store.q1("SELECT * FROM file_changes WHERE hash=?", (facts["rename"]["commit"],))
    assert (r["status"], r["path"], r["old_path"]) == ("R", "util/text.py", "util/strings.py")
    assert r["lineage"] == "util/strings.py"
    assert ctx.store.commit(facts["commits"]["m1_merge_login"])["is_merge"] == 1


def test_symbol_lifecycle(ingested):
    ctx, facts, _ = ingested
    lf = facts["lifecycle_symbol"]
    uid = f"sym:fixture:{lf['path']}:{lf['qualname']}:function"
    events = [(r["hash"], r["change"]) for r in ctx.store.q(
        "SELECT hash, change FROM symbol_events WHERE uid=? ORDER BY seq", (uid,))]
    assert events == [(lf["created"], "created"), (lf["modified"], "modified"), (lf["deleted"], "deleted")]
    sym = ctx.store.symbol(uid)
    assert sym["alive"] == 0 and sym["first_seen"] == lf["created"] and sym["last_seen"] == lf["deleted"]
    moved = ctx.store.symbol("sym:fixture:util/text.py:slugify:function")
    assert moved["alive"] == 1 and moved["old_uid"] == "sym:fixture:util/strings.py:slugify:function"


def test_second_run_hits_cache_and_is_noop(ingested):
    ctx, _, _ = ingested
    misses = ctx.cache.misses
    out = run_deterministic(ctx)
    assert out["history"]["new_commits"] == 0
    assert out["history"]["parsed_blobs"] == 0
    assert ctx.cache.misses == misses  # zero new parses anywhere, including the HEAD snapshot


TABLES = ("commits", "file_changes", "symbol_events", "symbols", "files", "reverts", "branches", "edges", "cochange",
          "candidates")


def _counts(store):
    return {t: store.q1(f"SELECT count(*) n FROM {t}")["n"] for t in TABLES}


def test_incremental_equals_full(tmp_path, fixture_repo):
    src, _ = fixture_repo
    work = tmp_path / "work"
    shutil.copytree(src, work)
    s_inc = Settings(data_dir=tmp_path / "inc", embedder="hash", embed_dim=64, qdrant_path=":memory:")
    ctx = open_context(work, "inc", s_inc, workers=1, log=lambda *_: None)
    run_deterministic(ctx, full=True)
    misses_before = ctx.cache.misses

    (work / "api" / "schema.py").write_text((work / "api" / "schema.py").read_text() + "\n\ndef extra():\n    return 1\n")
    (work / "web" / "forms.ts").write_text((work / "web" / "forms.ts").read_text() + "export const EXTRA = 1;\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "Add extra field")
    out = run_deterministic(ctx)
    assert out["history"]["new_commits"] == 1
    assert ctx.cache.misses - misses_before == 2  # exactly the two new blobs

    s_full = Settings(data_dir=tmp_path / "full", embedder="hash", embed_dim=64, qdrant_path=":memory:")
    full = open_context(work, "full", s_full, workers=1, log=lambda *_: None)
    run_deterministic(full, full=True)
    assert _counts(ctx.store) == _counts(full.store)
    pair = "SELECT count, jaccard FROM cochange WHERE a='api/schema.py' AND b='web/forms.ts'"
    assert tuple(ctx.store.q1(pair)) == tuple(full.store.q1(pair))
