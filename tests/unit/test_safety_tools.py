"""Mutation runner, diff parser, GitHub discussion sync (with a fake API), API signature parsing."""
from __future__ import annotations

import json
import sys

import pytest


def test_mutation_runner_finds_untested_behaviour(tmp_path):
    from afd_oracle_ingest.safety.mutate import run
    (tmp_path / "calc.py").write_text(
        "def clamp(x, lo, hi):\n"
        "    if x < lo:\n"
        "        return lo\n"
        "    if x > hi:\n"
        "        return hi\n"
        "    return x\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import clamp\n\n"
        "def test_low():\n    assert clamp(-5, 0, 10) == 0\n", encoding="utf-8")  # the upper bound is never tested
    before = (tmp_path / "calc.py").read_bytes()
    out = run(tmp_path, "calc.py::clamp", python=sys.executable, tests=["test_calc.py"], limit=10, log=lambda *_: None)
    assert (tmp_path / "calc.py").read_bytes() == before  # always restored
    assert not list(tmp_path.glob("*.bak"))
    assert out["mutants"] >= 4 and 0 < out["killed"] < out["mutants"]
    assert any(s["line"] in (4, 5) for s in out["survived"])  # the untested `x > hi` branch survives


def test_mutation_runner_refuses_failing_baseline(tmp_path):
    from afd_oracle_ingest.safety.mutate import run
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n", encoding="utf-8")
    out = run(tmp_path, "m.py::f", python=sys.executable, tests=["test_m.py"], log=lambda *_: None)
    assert "fail before any mutation" in out["error"]


def test_diff_parser_and_apply():
    from afd_oracle_ingest.safety import diffparse
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
            "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n")
    files = diffparse.parse(diff)
    assert [(f.path, f.status) for f in files] == [("x.py", "M"), ("new.py", "A")]
    assert files[0].added() == [(2, "B")] and files[0].removed() == [(2, "b")]
    assert diffparse.apply("a\nb\nc", files[0]) == "a\nB\nc"
    assert diffparse.apply(None, files[1]) == "x = 1\ny = 2"


def test_signature_params():
    from afd_oracle_ingest.safety.api import params
    p = params("def f(self, a, b: int = 1, *args, key=None, **kw) -> int", "python")
    assert [(x.name, x.default, x.kind) for x in p] == [("a", None, "positional"), ("b", "1", "positional"),
                                                         ("args", None, "var_positional"),
                                                         ("key", "None", "keyword_only"), ("kw", None, "var_keyword")]
    ts = params("export function g(cb: (x: number) => void, opt?: string, n = 3): void", "typescript")
    assert [(x.name, x.default) for x in ts] == [("cb", None), ("opt", "undefined"), ("n", "3")]


@pytest.fixture
def gh_store(tmp_path):
    from afd_oracle_core.store import Store
    s = Store(tmp_path / "s.sqlite", "r")
    s.set_meta("origin", "https://github.com/acme/widget.git")
    with s.tx() as conn:
        for i, (h, msg) in enumerate([("a" * 40, "Add cache (#7)"), ("b" * 40, "Tidy\n\nFixes #9"), ("c" * 40, "Unrelated")]):
            conn.execute("INSERT INTO commits(hash, seq, ts, message, is_merge, is_bulk, files_changed) VALUES (?,?,?,?,0,0,1)",
                         (h, i, 1000 + i, msg))
    return s


def test_discussions_sync_links_commits(gh_store, monkeypatch):
    from afd_oracle_ingest.safety import discussions
    monkeypatch.setattr(discussions, "token", lambda: None)
    pages = {
        "pulls": [{"number": 7, "title": "Add a read-through cache", "body": "Speeds up lookups", "state": "closed",
                   "merged_at": "2024-01-02T00:00:00Z", "merge_commit_sha": "d" * 40, "labels": [{"name": "perf"}],
                   "html_url": "u7", "user": {"login": "x"}, "created_at": "2024-01-01T00:00:00Z",
                   "closed_at": "2024-01-02T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z"}],
        "issues": [{"number": 9, "title": "Crash on empty input", "body": "Traceback ...", "state": "closed",
                    "labels": [], "html_url": "u9", "user": {"login": "y"}, "created_at": "2024-01-03T00:00:00Z",
                    "closed_at": "2024-01-04T00:00:00Z"},
                   {"number": 7, "pull_request": {}, "title": "dup of the PR"}],
    }
    calls = []

    def fake(url, tok):
        calls.append(url)
        if url.endswith("/repos/acme/widget"):
            return {"private": False, "visibility": "public", "fork": False, "forks_count": 12}, {}
        if "/releases?" in url:
            return ([{"tag_name": "v1.0", "name": "One", "created_at": "2024-01-05T00:00:00Z",
                      "published_at": "2024-01-05T00:00:00Z", "draft": False, "prerelease": False}]
                    if "page=1" in url else []), {}
        kind = "pulls" if "/pulls?" in url else "issues"
        return (pages[kind] if "page=1" in url else []), {}
    out = discussions.sync(gh_store, fetch=fake, log=lambda *_: None)
    assert out["pulls"] == 1 and out["issues"] == 1 and out["releases"] == 1
    assert any(c.startswith("https://api.github.com/repos/acme/widget/pulls") for c in calls)
    assert json.loads(gh_store.get_meta("github_repo"))["forks_count"] == 12
    assert gh_store.q1("SELECT tag FROM github_releases")["tag"] == "v1.0"
    assert {(r["number"], r["hash"][0]) for r in gh_store.q("SELECT * FROM discussion_commits")} == {(7, "a"), (9, "b")}
    assert discussions.for_commit(gh_store, "a" * 40)[0]["title"] == "Add a read-through cache"
    hits = discussions.search(gh_store, "cache lookups")
    assert hits[0]["number"] == 7 and hits[0]["labels"] == ["perf"]
    assert gh_store.get_meta("discussions_synced_at")
    json.dumps(out)
