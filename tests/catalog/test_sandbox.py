"""Sandbox result ingestion (no Docker needed): a coverage.py JSON report becomes covered_lines, coverage_files and
measured test_map rows; the catalogue Ctx then reports measured coverage."""
from afd_oracle_ingest.catalog.base import Ctx
from afd_oracle_ingest.sandbox import _norm_context, store_coverage


def test_norm_context():
    assert _norm_context("tests/test_core.py::test_none|run") == "tests/test_core.py::test_none"
    assert _norm_context("/src/tests/test_core.py::TestX::test_y|setup") == "tests/test_core.py::TestX::test_y"
    assert _norm_context("") is None


def test_store_coverage_feeds_ctx(mini):
    r = mini()
    r.commit({"app/core.py": "def parse(x):\n    if x is None:\n        return 0\n    return int(x)\n",
              "tests/test_core.py": "from app.core import parse\n\n\ndef test_int():\n    assert parse('1') == 1\n"},
             "Add parser", days=0)
    ctx = r.ingest()
    report = {"files": {
        "/src/app/core.py": {"executed_lines": [1, 2, 4], "missing_lines": [3],
                             "summary": {"num_statements": 4, "covered_lines": 3},
                             "contexts": {"2": ["tests/test_core.py::test_int|run"],
                                          "4": ["tests/test_core.py::test_int|run"]}},
        "/src/tests/test_core.py": {"executed_lines": [1, 4, 5], "missing_lines": [], "summary": {},
                                    "contexts": {}}}}
    stats = store_coverage(ctx.store, report, "abc")
    assert stats["files"] == 2 and stats["test_links"] == 1
    fresh = Ctx(ctx.repo, ctx.store, ctx.settings)
    uid = f"sym:{r.name}:app/core.py:parse:function"
    assert fresh.coverage_available
    assert fresh.covered("app/core.py", 3) == 0 and fresh.covered("app/core.py", 2) == 1
    assert fresh.covered("other.py", 1) is None
    assert fresh.symbol_coverage(uid) == (2, 3)  # lines 2-4 of the body; line 3 (return 0) never ran
    assert any(t["source"] == "coverage" for t in fresh.tests_for[uid])
    assert fresh.tests_covering("app/core.py", 2) == {"tests/test_core.py::test_int"}
    assert fresh.tests_covering("app/core.py", 3) == set()
    assert fresh.coverage_tests(uid) == {"tests/test_core.py::test_int"}
