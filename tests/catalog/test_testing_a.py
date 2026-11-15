"""Tests for the testing-family builders (part A). Planted repos are shared between builders to keep ingest count low."""
import json

import pytest


def subjects(hits):
    return sorted(h.subject for h in hits)


def uid(r, path, qual):
    return f"sym:{r.name}:{path}:{qual}:function"


GAP = """def risky_gap(a, b):
    total = 0
    for i in range(a):
        total += i
    if b:
        total -= {n}
    return total
"""
FALSE = """def shaky(a, b):
    out = []
    for i in range(a):
        out.append(i)
    if b:
        out.append({n})
    return out
"""
MISC = "def test_misc():\n    assert 1 == {n}\n    assert 2 == 2\n"
CONVERT = "def convert(x):\n    y = x * {n}\n    return y\n\n\ndef other(x):\n    z = x + {n}\n    return z\n"
SHAPE = "def area(w, h):\n    return w * h + {n}\n\n\ndef volume(w, h, d):\n    return w * h * d + {n}\n\n\ndef old():\n    return 1\n"
GUARD = ("def guard(x):\n    if x is None:\n        return {n}\n    return x\n\n\n"
         "def sibling(x):\n    if x < {n}:\n        return 0\n    return x\n")
CRAP_BRANCHES = "".join(f"    if x > {i}:\n        total += {i}\n" for i in range(12))
CRAP = "def dispatch(path, x):\n    total = {n}\n    with open(path) as fh:\n        data = fh.read()\n" + CRAP_BRANCHES + "    return total, data\n"


@pytest.fixture(scope="module")
def repo_a(tmp_path_factory):
    # module-scoped manual construction (mini is function scoped), so build via the same class
    from .conftest import MiniRepo
    base = tmp_path_factory.mktemp("repoa")
    r = MiniRepo(base / "ra", base / "data", "ra")

    r.commit({
        "app/gap.py": GAP.format(n=1), "app/falsegap.py": FALSE.format(n=1), "tests/test_misc.py": MISC.format(n=1),
        "app/pseudo.py": CONVERT.format(n=2),
        "tests/test_pseudo.py": ("from app.pseudo import convert, other\n\n\ndef test_convert():\n    convert(2)\n\n\n"
                                 "def test_smoke_other():\n    other(1)\n"),
        "app/shape.py": SHAPE.format(n=0),
        "tests/test_shape.py": ("from app.shape import area, volume, old\n\n\ndef test_area():\n    assert area(1, 2) == 2\n"
                                "    assert area(2, 2) == 4\n    assert area(0, 2) == 0\n\n\n"
                                "def test_volume():\n    assert volume(1, 1, 1) == 1\n    assert volume(2, 1, 1) == 2\n"
                                "    assert volume(0, 1, 1) == 0\n    assert volume(1, 0, 1) == 0\n\n\n"
                                "def test_old():\n    assert old() == 1\n"),
        "app/guard.py": GUARD.format(n=0),
        "tests/test_guard.py": "from app.guard import guard\n\n\ndef test_guard():\n    assert guard(1) == 1\n",
        "app/crap.py": CRAP.format(n=0),
        "app/clock.py": ("from datetime import datetime\nimport logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
                         "def is_expired(ts):\n    return datetime.now() > ts\n\n\n"
                         "def stamp_logged():\n    logger.info(datetime.now())\n\n\n"
                         "def due(ts):\n    return datetime.utcnow() > ts\n"),
        "tests/test_clock.py": ("from app.clock import is_expired\n\n\ndef test_expired():\n    assert is_expired(0)\n"),
        "tests/test_due.py": ("import freezegun\nfrom app.clock import due\n\n\ndef test_due():\n    assert due(0)\n"),
        "app/bound.py": "def in_range(n):\n    return n < 10\n\n\ndef positive(n):\n    return n >= 1\n",
        "tests/test_bound.py": ("from app.bound import in_range, positive\n\n\ndef test_in_range():\n    assert in_range(3)\n\n\n"
                                "def test_positive():\n    assert positive(2)\n"),
        "app/conc.py": ("import threading\n\n_lock = threading.Lock()\ncounter = 0\n\n\ndef bump():\n    global counter\n"
                        "    with _lock:\n        counter += 1\n"),
        "tests/test_conc.py": "from app.conc import bump\n\n\ndef test_bump():\n    bump()\n    assert True\n",
        "app/recur.py": "def calc(x):\n    return x + 1\n",
        "tests/test_recur.py": "from app.recur import calc\n\n\ndef test_calc():\n    assert calc(1) == 2\n",
        "app/sec.py": "def sanitize(x):\n    return x\n",
        "tests/test_weak.py": "from app.recur import calc\n\n\ndef test_weak():\n    result = calc(4)\n    assert result == 5\n",
        "app/weakprod.py": "VALUE = 1\n",
        "config/settings.yaml": "timeout: 1\n",
        "package-lock.json": '{"a": 1}\n',
        "tests/test_cfg.py": "def test_cfg():\n    path = 'config/settings.yaml'\n    assert path\n",
    }, "Initial import", days=0)

    # false_test_gap: two fixes each, one with a test edit in the fix
    r.commit({"app/gap.py": GAP.format(n=2)}, "Fix risky_gap off by one", days=10)
    r.commit({"app/gap.py": GAP.format(n=3)}, "Fix risky_gap crash", days=11)
    r.commit({"app/falsegap.py": FALSE.format(n=2), "tests/test_misc.py": MISC.format(n=2)}, "Fix shaky appending", days=12)
    r.commit({"app/falsegap.py": FALSE.format(n=3), "tests/test_misc.py": MISC.format(n=3)}, "Fix shaky crash", days=13)
    # pseudo_tested: a fix on convert
    r.commit({"app/pseudo.py": CONVERT.format(n=3)}, "Fix convert multiplier", days=14)
    # guard test: edited with a new assert in 3 fixes plus production change
    for i in range(1, 4):
        body = "from app.guard import guard\n\n\ndef test_guard():\n    assert guard(1) == 1\n" \
               + "".join(f"    assert guard({j}) == {j}\n" for j in range(2, 2 + i))
        r.commit({"app/guard.py": GUARD.format(n=i), "tests/test_guard.py": body}, f"Fix guard edge case {i}", days=20 + i)
    # crap: two fixes
    r.commit({"app/crap.py": CRAP.format(n=1)}, "Fix dispatch total", days=30)
    r.commit({"app/crap.py": CRAP.format(n=2)}, "Fix dispatch crash", days=31)
    # time fixes
    r.commit({"app/clock.py": open_clock(r).replace("datetime.now() > ts", "datetime.now(timezone.utc) > ts")
              .replace("from datetime import datetime", "from datetime import datetime, timezone")},
             "Fix timezone handling in is_expired", days=32)
    r.commit({"app/clock.py": (r.root / "app/clock.py").read_text().replace("utcnow() > ts", "utcnow() >= ts")},
             "Fix timezone edge in due", days=33)
    # boundary fixes
    r.commit({"app/bound.py": "def in_range(n):\n    return n <= 10\n\n\ndef positive(n):\n    return n >= 1\n"},
             "Fix off by one in in_range", days=34)
    r.commit({"app/bound.py": "def in_range(n):\n    return n <= 10\n\n\ndef positive(n):\n    return n > 0\n"},
             "Fix positive check", days=35)
    # race fix
    r.commit({"app/conc.py": (r.root / "app/conc.py").read_text().replace("counter += 1", "counter += 1  # atomic")},
             "Fix race condition in bump", days=36)
    # recurrence: test untouched, two fixes
    r.commit({"app/recur.py": "def calc(x):\n    if x is None:\n        return 0\n    return x + 1\n"}, "Fix calc None crash", days=60)
    r.commit({"app/recur.py": "def calc(x):\n    if x is None:\n        return 0\n    if x < 0:\n        return 0\n    return x + 1\n"},
             "Fix calc negative", days=90)
    # test added at a security fix, later deleted
    r.commit({"app/sec.py": "def sanitize(x):\n    return x.replace('<', '&lt;')\n",
              "tests/test_sec.py": ("from app.sec import sanitize\n\n\ndef test_sanitize_escapes():\n"
                                    "    assert sanitize('<') == '&lt;'\n")}, "Fix security XSS escaping in sanitize", days=40)
    r.commit({"tests/test_sec.py": None}, "Drop flaky test", days=50)
    # weakened assertion with production change
    r.commit({"tests/test_weak.py": "from app.recur import calc\n\n\ndef test_weak():\n    result = calc(4)\n    assert result > 0\n",
              "app/weakprod.py": "VALUE = 2\n"}, "Fix flaky test on CI", days=55)
    r.commit({"tests/test_weak.py": "from app.recur import calc\n\n\ndef test_weak():\n    result = calc(4)\n",
              "app/weakprod.py": "VALUE = 3\n"}, "Migrate tests to pytest style", days=56)
    # non-code resource fixes
    r.commit({"config/settings.yaml": "timeout: 2\n", "package-lock.json": '{"a": 2}\n'}, "Fix default timeout setting", days=57)
    r.commit({"config/settings.yaml": "timeout: 3\n"}, "Fix timeout again", days=58)
    # deleted tests / reduced assertions while targets stay
    r.commit({"tests/test_shape.py": ("from app.shape import area, volume, old\n\n\n"
                                      "def test_volume():\n    assert volume(1, 1, 1) == 1\n\n\n"
                                      "def test_old():\n    assert old() == 1\n")},
             "Trim shape tests", days=70)
    r.commit({"app/shape.py": SHAPE.format(n=1)}, "Fix area rounding", days=80)
    # target and its test deleted together
    r.commit({"app/shape.py": SHAPE.format(n=1).replace("def old():\n    return 1\n", ""),
              "tests/test_shape.py": ("from app.shape import area, volume\n\n\n"
                                      "def test_volume():\n    assert volume(1, 1, 1) == 1\n")}, "Remove old", days=90)
    r.commit({"README.md": "# x\n"}, "Docs", days=400)
    r.ingest()
    return r


def open_clock(r):
    return (r.root / "app/clock.py").read_text()


def test_false_test_gap(repo_a):
    hits = {h.subject: h for h in repo_a.run("false_test_gap")}
    gap = hits[uid(repo_a, "app/gap.py", "risky_gap")]
    assert gap.evidence["evidence_level"] == 0 and gap.evidence["direction"] == "truly_untested_and_risky"
    # shaky is not gapped in the risk table (history link), so it is only emitted once the gap flag is forced on
    sid = uid(repo_a, "app/falsegap.py", "shaky")
    assert sid not in hits
    row = repo_a.ctx.q1("SELECT factors FROM risk WHERE subject=?", (sid,))
    factors = json.loads(row["factors"]) | {"test_gap": 1.0}
    repo_a.ctx.store.conn.execute("UPDATE risk SET factors=?, score=99 WHERE subject=?", (json.dumps(factors), sid))
    repo_a.ctx.__dict__.pop("_testing_a_memo", None)
    try:
        false = {h.subject: h for h in repo_a.run("false_test_gap")}[sid]
    finally:
        repo_a.ctx.store.conn.execute("UPDATE risk SET score=26.7 WHERE subject=?", (sid,))
        repo_a.ctx.__dict__.pop("_testing_a_memo", None)
    assert false.evidence["evidence_level"] == 2 and false.evidence["direction"] == "falsely_gapped"


def test_pseudo_tested_function(repo_a):
    hits = repo_a.run("pseudo_tested_function")
    assert subjects(hits) == [uid(repo_a, "app/pseudo.py", "convert")]
    assert repo_a.ctx.noise.get("pseudo_tested_function/smoke_import_main_test", 0) >= 1


def test_test_deleted_behavior_kept(repo_a):
    hits = repo_a.run("test_deleted_behavior_kept")
    by = {h.evidence["target"]: h for h in hits}
    assert by["area"].evidence["change"] == "deleted" and by["area"].evidence["fixes_on_target_after"] == 1
    assert by["volume"].evidence["change"] == "reduced"
    assert "old" not in by   # target deleted with its test
    assert len(hits) == 2


def test_proven_guard_test(repo_a):
    hits = repo_a.run("proven_guard_test")
    assert subjects(hits) == [uid(repo_a, "tests/test_guard.py", "test_guard")]
    h = hits[0]
    assert h.evidence["distinct_fixes"] == 3
    assert [s["symbol"] for s in h.evidence["siblings"]] == ["sibling"]


def test_crap_and_hard_to_test(repo_a):
    crap = repo_a.run("crap_hotspot")
    assert subjects(crap) == [uid(repo_a, "app/crap.py", "dispatch")]
    assert crap[0].evidence["tier"] == "unmeasured"
    hard = {h.subject: h for h in repo_a.run("hard_to_test_risky_code")}
    d = hard[uid(repo_a, "app/crap.py", "dispatch")]
    assert d.evidence["variant"] == "io" and "open" in d.evidence["io_calls"]
    gap = hard[uid(repo_a, "app/gap.py", "risky_gap")]
    assert gap.evidence["variant"] == "plain_risky_untested" and gap.score < d.score


def test_time_dependent(repo_a):
    hits = repo_a.run("time_dependent_code_no_frozen_clock")
    assert subjects(hits) == [uid(repo_a, "app/clock.py", "is_expired")]
    assert repo_a.ctx.noise["time_dependent_code_no_frozen_clock/logging_only"] == 1
    assert repo_a.ctx.noise["time_dependent_code_no_frozen_clock/time_frozen_in_tests"] == 1


def test_boundary(repo_a):
    hits = repo_a.run("boundary_fix_no_boundary_test")
    hits = [h for h in hits if h.paths[0] == "app/bound.py"]
    assert len(hits) == 1
    assert hits[0].symbols == [uid(repo_a, "app/bound.py", "in_range")]
    assert hits[0].evidence["changes"][0]["kind"] == "op"
    assert repo_a.ctx.noise["boundary_fix_no_boundary_test/semantic_noop"] == 1


def test_concurrency(repo_a):
    hits = repo_a.run("concurrency_code_without_concurrent_test")
    assert subjects(hits) == [uid(repo_a, "app/conc.py", "bump")]
    assert "race_fix" in hits[0].evidence["sources"]


def test_fix_recurrence(repo_a):
    hits = repo_a.run("fix_recurrence_despite_existing_test")
    assert len(hits) == 1
    assert hits[0].subject.endswith("|" + uid(repo_a, "app/recur.py", "calc"))
    assert hits[0].evidence["fix_number"] == 2


def test_test_added_at_fix_then_lost(repo_a):
    hits = repo_a.run("test_added_at_fix_then_lost")
    assert len(hits) == 1
    assert "test_deleted" in hits[0].evidence["reasons"]
    assert hits[0].symbols[1] == uid(repo_a, "app/sec.py", "sanitize")


def test_weakened_assertion(repo_a):
    hits = [h for h in repo_a.run("weakened_assertion") if h.paths == ["tests/test_weak.py"]]
    swap = [h for h in hits if "strict_to_loose" in h.evidence["signals"]]
    assert len(swap) == 1 and swap[0].evidence["message_matches_fix_ci"]
    assert repo_a.ctx.noise["weakened_assertion/framework_migration_or_format"] == 1


def test_non_code_dependency_gap(repo_a):
    hits = repo_a.run("non_code_dependency_gap")
    assert subjects(hits) == ["config/settings.yaml"]
    assert hits[0].evidence["evidence_class"] == "a_literal_reference"


# ----------------------------------------------------------------------------------------------- repo B
def test_skip_xfail_and_seams(mini):
    r = mini()
    r.commit({
        "app/pay.py": "def pay(x):\n    return round(x, 1)\n",
        "app/notify.py": ("def render(msg):\n    return msg.upper()\n\n\ndef send(msg):\n    return render(msg)\n\n\n"
                          "def other(msg):\n    return msg\n"),
        "app/runner.py": ("import subprocess\n\n\ndef run_tool(args):\n    return subprocess.run(args, capture_output=True)\n\n\n"
                          "def run_git(args):\n    return subprocess.run(['git'] + args)\n"),
        "tests/test_skip.py": ("import sys\nimport pytest\nfrom app.pay import pay\n\n\n"
                               "@pytest.mark.skip(reason='flaky')\ndef test_pay():\n    assert pay(1.04) == 1.0\n\n\n"
                               "@pytest.mark.skipif(sys.platform == 'win32', reason='win')\ndef test_pay_posix():\n    assert pay(2) == 2\n"),
        "tests/test_notify.py": ("from unittest import mock\nfrom app.notify import send, other\n\n\n"
                                 "@mock.patch('app.notify.render')\ndef test_send(m):\n    m.return_value = 'X'\n    assert send('a') == 'X'\n\n\n"
                                 "@mock.patch('app.notify.other')\ndef test_other_patched(m):\n    assert other('a')\n\n\n"
                                 "def test_other_real():\n    assert other('a') == 'a'\n"),
        "tests/test_runner.py": ("from unittest import mock\nfrom app.runner import run_tool, run_git\n\n\n"
                                 "@mock.patch('subprocess.run')\ndef test_run_tool(m):\n    run_tool(['x'])\n    assert m.called\n\n\n"
                                 "@mock.patch('subprocess.run')\ndef test_run_git(m):\n    run_git(['status'])\n    assert m.called\n"),
        ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest -q\n",
    }, "Initial", days=0)
    r.commit({"app/pay.py": "def pay(x):\n    return round(x, 2)\n",
              "app/notify.py": ("def render(msg):\n    return msg.upper().strip()\n\n\ndef send(msg):\n    return render(msg)\n\n\n"
                                "def other(msg):\n    return msg\n"),
              "app/runner.py": ("import subprocess\n\n\ndef run_tool(args):\n    return subprocess.run(args, capture_output=True, timeout=5)\n\n\n"
                                "def run_git(args):\n    return subprocess.run(['git'] + args, timeout=5)\n")},
             "Fix rounding in pay; fix render crash; fix hang with stdin", days=150)
    r.commit({".github/workflows/ci.yml": ("name: ci\non:\n  push:\n    paths-ignore:\n      - 'docs/**'\njobs:\n  test:\n"
                                           "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: pytest -q\n")},
             "Make CI tolerant", days=160)
    r.commit({".github/workflows/ci.yml": ("name: ci\non:\n  push:\n    paths-ignore:\n      - 'docs/**'\njobs:\n  test:\n"
                                           "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: echo disabled\n")},
             "Stop running tests", days=170)
    r.commit({"tests/test_new.py": "import pytest\nfrom app.pay import pay\n\n\n@pytest.mark.skip\ndef test_new():\n    assert pay(1) == 1\n"},
             "Add skipped new test", days=240)
    r.ingest()

    skips = r.run("skip_xfail_never_removed")
    assert len(skips) == 1 and skips[0].evidence["marker"].startswith("@pytest.mark.skip(reason")
    assert skips[0].symbols == [uid(r, "app/pay.py", "pay")]
    assert r.ctx.noise["skip_xfail_never_removed/conditional_skip"] == 1
    assert r.ctx.noise["skip_xfail_never_removed/marker_under_90_days"] == 1

    seam = r.run("seam_mocked_only")
    assert subjects(seam) == [uid(r, "app/notify.py", "render")]
    assert r.ctx.noise["seam_mocked_only/unpatched_test_exists"] == 1

    proc = r.run("process_seam_mocked")
    assert subjects(proc) == [uid(r, "app/runner.py", "run_tool")]
    assert r.ctx.noise["process_seam_mocked/git_is_available_in_ci"] == 1

    ci = r.run("ci_test_disabled")
    lines = {(h.evidence["direction"], h.evidence["line"]) for h in ci}
    assert ("added", "continue-on-error: true") in lines
    assert ("removed", "- run: pytest -q") in lines
    assert not any("paths-ignore" in h.evidence["line"] for h in ci)
    assert r.ctx.noise["ci_test_disabled/docs_only_path_filter"] == 1
    head = [h for h in ci if h.evidence["line"] == "continue-on-error: true"][0]
    assert head.evidence["still_at_head"]


# ------------------------------------------------------------------------- upgrades: enrichment + measured data
def _fresh(r):
    """New Ctx over the same store so cached properties (coverage, mutations) reload."""
    from afd_oracle_ingest.catalog.base import Ctx
    r.ctx = Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)
    return r.ctx


def _cover(r, path, executed, total):
    c = r.ctx.store.conn
    c.execute("DELETE FROM covered_lines WHERE path=?", (path,))
    c.execute("INSERT OR REPLACE INTO coverage_files VALUES (?,?,?)", (path, total, len(executed)))
    for ln in executed:
        c.execute("INSERT OR REPLACE INTO covered_lines VALUES (?,?,?)", (path, ln, 1))
    c.commit()


def test_fix_without_regression_test_uses_map_at_fix_time(mini):
    r = mini()
    r.commit({"app/m.py": "def f(x):\n    return x + 1\n", "app/o.py": "def g(x):\n    return x\n",
              "tests/test_o.py": "from app.o import g\n\n\ndef test_g():\n    assert g(1) == 1\n"}, "Initial", days=0)
    r.git("tag", "v1", days=0)
    r.commit({"tests/test_m.py": "from app.m import f\n\n\ndef test_f():\n    assert f(1) == 2\n    assert f(2) == 3\n"},
             "Add f test", days=5)
    r.commit({"app/m.py": "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n"},
             "Fix f crash on None", days=40)
    ctx = r.ingest()
    f = uid(r, "app/m.py", "f")
    assert ctx.best_test_weight(f) >= 0.6          # linked now ...
    hits = r.run("fix_without_regression_test")    # ... but it was not linked at the last release tag before the fix
    assert [h.subject.split("|")[1] for h in hits] == [f]
    assert hits[0].evidence["test_map_ref"] == "v1" and hits[0].evidence["best_test_weight"] == 0


def test_fix_without_regression_test_falls_back_without_tags(mini):
    r = mini()
    r.commit({"app/m.py": "def f(x):\n    return x + 1\n"}, "Initial", days=0)
    r.commit({"tests/test_m.py": "from app.m import f\n\n\ndef test_f():\n    assert f(1) == 2\n    assert f(2) == 3\n"},
             "Add f test", days=5)
    r.commit({"app/m.py": "def f(x):\n    if x is None:\n        return 0\n    return x + 1\n"},
             "Fix f crash on None", days=40)
    r.ingest()
    assert r.run("fix_without_regression_test") == []     # HEAD map says strongly tested, no tag to say otherwise


@pytest.fixture
def cov_repo(mini):
    r = mini()
    r.commit({
        "app/crap.py": CRAP.format(n=0),
        "app/svc.py": "def compute(x):\n    return x * 2\n",
        "tests/test_svc.py": ("from unittest import mock\nfrom app.svc import compute\n\n\ndef test_compute():\n"
                              "    with mock.patch(\"app.svc.compute\", return_value=4):\n        assert compute(2) == 4\n"),
        "app/recur.py": "def calc(x):\n    return x + 1\n",
        "tests/test_recur.py": "from app.recur import calc\n\n\ndef test_calc():\n    assert calc(1) == 2\n",
    }, "Initial import", days=0)
    r.commit({"app/crap.py": CRAP.format(n=1)}, "Fix dispatch total", days=30)
    r.commit({"app/crap.py": CRAP.format(n=2)}, "Fix dispatch crash", days=31)
    r.commit({"app/svc.py": "def compute(x):\n    return x * 3\n"}, "Fix compute multiplier", days=32)
    r.commit({"app/recur.py": "def calc(x):\n    if x is None:\n        return 0\n    return x + 1\n"}, "Fix calc None crash", days=60)
    r.commit({"app/recur.py": "def calc(x):\n    if x is None:\n        return 0\n    if x < 0:\n        return 0\n    return x + 1\n"},
             "Fix calc negative", days=90)
    r.ingest()
    return r


def test_complexity_comes_from_ast_versions(cov_repo):
    d = uid(cov_repo, "app/crap.py", "dispatch")
    crap = cov_repo.run("crap_hotspot")
    assert crap[0].evidence["complexity_source"] == "ast" and crap[0].evidence["complexity_proxy"] == cov_repo.ctx.complexity(d) == 13
    hard = {h.subject: h for h in cov_repo.run("hard_to_test_risky_code")}
    assert hard[d].evidence["complexity_source"] == "ast" and hard[d].evidence["measured_coverage"] is None


def test_crap_and_hard_to_test_use_measured_coverage(cov_repo):
    d = uid(cov_repo, "app/crap.py", "dispatch")
    s = cov_repo.ctx.symbols[d]
    body = range(s["start_line"] + 1, s["end_line"] + 1)
    total = len(body)
    # nothing executed: measured tier, CRAP = cc^2 + cc
    _cover(cov_repo, "app/crap.py", [], total)
    _fresh(cov_repo)
    h = cov_repo.run("crap_hotspot")[0]
    assert h.evidence["tier"] == "measured" and h.evidence["measured_coverage"] == 0 and h.evidence["crap"] == 13 * 13 + 13
    assert d in subjects(cov_repo.run("hard_to_test_risky_code"))
    # fully executed: both builders drop the symbol even though no static test links it
    _cover(cov_repo, "app/crap.py", list(body), total)
    _fresh(cov_repo)
    assert cov_repo.run("crap_hotspot") == []
    assert d not in subjects(cov_repo.run("hard_to_test_risky_code"))
    assert cov_repo.ctx.noise["crap_hotspot/measured_coverage_adequate"] == 1
    assert cov_repo.ctx.noise["hard_to_test_risky_code/measured_coverage_adequate"] >= 1


def test_seam_mocked_only_dropped_when_measured_executed(cov_repo):
    compute = uid(cov_repo, "app/svc.py", "compute")
    assert compute in subjects(cov_repo.run("seam_mocked_only"))
    s = cov_repo.ctx.symbols[compute]
    _cover(cov_repo, "app/svc.py", [s["start_line"] + 1], 1)
    _fresh(cov_repo)
    assert compute not in subjects(cov_repo.run("seam_mocked_only"))
    assert cov_repo.ctx.noise["seam_mocked_only/measured_executed"] == 1


def test_fix_recurrence_reports_mutation_survivors(cov_repo):
    calc = uid(cov_repo, "app/recur.py", "calc")
    base = cov_repo.run("fix_recurrence_despite_existing_test")
    assert len(base) == 1 and base[0].evidence["mutation_survivors"] == "unconfirmed"
    c = cov_repo.ctx.store.conn
    rows = [(calc, 4, "cmp", "x < 0", "x <= 0", "survived", "h"), (calc, 5, "arith", "x + 1", "x - 1", "killed", "h"),
            (calc, 5, "arith", "x + 1", "x + 2", "error", "h")]
    c.executemany("INSERT INTO mutation_results VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    _fresh(cov_repo)
    h = cov_repo.run("fix_recurrence_despite_existing_test")[0]
    assert h.evidence["mutation_survivors"] == 1 and h.evidence["mutants_tested"] == 2   # errors are not decided mutants
    assert h.evidence["survived_mutants"][0]["mutant"] == "x <= 0" and h.score > base[0].score


def test_false_test_gap_two_hop_callers(mini):
    r = mini()
    src = ("def core(a, b):\n    out = []\n    for i in range(a):\n        out.append(i)\n    if b:\n"
           "        out.append({n})\n    return out\n")
    r.commit({"app/__init__.py": "", "tests/__init__.py": "",
              "app/core.py": src.format(n=1),
              "app/mid.py": "from app.core import core\n\n\ndef mid():\n    return core(1, 0)\n",
              "app/top.py": "from app.mid import mid\n\n\ndef top():\n    return mid()\n",
              "tests/test_top.py": "from app.top import top\n\n\ndef test_top():\n    assert top() == [0]\n"},
             "Initial", days=0)
    r.commit({"app/core.py": src.format(n=2), "tests/test_top.py": "from app.top import top\n\n\ndef test_top():\n    assert top() == [0]\n# 2\n"},
             "Fix core appending", days=10)
    r.commit({"app/core.py": src.format(n=3), "tests/test_top.py": "from app.top import top\n\n\ndef test_top():\n    assert top() == [0]\n# 3\n"},
             "Fix core crash", days=11)
    r.ingest()
    sid = uid(r, "app/core.py", "core")
    assert set(r.ctx.callers_within(sid, 2).values()) == {1, 2}
    row = r.ctx.q1("SELECT factors FROM risk WHERE subject=?", (sid,))
    factors = json.loads(row["factors"]) | {"test_gap": 1.0}
    r.ctx.store.conn.execute("UPDATE risk SET factors=?, score=99 WHERE subject=?", (json.dumps(factors), sid))
    r.ctx.__dict__.pop("_testing_a_memo", None)
    h = {h.subject: h for h in r.run("false_test_gap")}[sid]
    # the only test imports top.py, two CALLS hops from core: found by the real walk, not by one-hop callers
    assert h.evidence["evidence_level"] == 3 and h.evidence["two_hop_only"] is True
    assert h.evidence["callers_2hop"] == 2
