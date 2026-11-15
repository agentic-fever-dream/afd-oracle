"""Tests for the testing_quality family: three planted repos cover all 17 builders."""
import pytest

from afd_oracle_ingest.catalog import testing_quality as tq


def uid(r, path, qual, kind="function"):
    return f"sym:{r.name}:{path}:{qual}:{kind}"


def subjects(hits):
    return [h.subject for h in hits]


@pytest.fixture
def cfg(mini):
    """History-and-config repo: coverage gate, flaky markers, strictness, snapshots, contracts, wiring."""
    r = mini()
    r.commit({
        "pyproject.toml": '[tool.coverage.report]\nfail_under = 85\nomit = [\n    "app/legacy.py",\n]\n',
        "tsconfig.json": '{\n  "compilerOptions": {\n    "strict": true\n  }\n}\n',
        "app/db.py": "def migrate(x):\n    return x\n",
        "app/legacy.py": "def old():\n    return 1\n",
        "tests/test_flaky.py": "def test_a():\n    assert 1\n",
        "app/views.py": "def home():\n    return 1\n\n\ndef about():\n    return 2\n",
        "app/routes.py": "routes = {}\n",
        "app/settings.py": 'config = {}\nconfig["CHECKOUT_MODE"] = "live"\n',
        "app/a.py": 'def a(config):\n    return config["CHECKOUT_MODE"]\n',
        "app/b.py": 'def b(config):\n    return config["CHECKOUT_MODE"]\n',
    }, "Initial import", days=0)
    r.commit({"app/db.py": "def migrate(x):\n    if x is None:\n        return 0\n    return x\n"},
             "Fix migrate crash on None", days=10)
    r.commit({"pyproject.toml": '[tool.coverage.report]\nfail_under = 70\nomit = [\n    "app/legacy.py",\n    "app/db.py",\n]\n',
              "tsconfig.json": '{\n  "compilerOptions": {\n    "strict": false\n  }\n}\n'},
             "make CI green", days=20)
    r.commit({"tests/test_flaky.py": "import time\n\n\ndef test_a():\n    time.sleep(1)\n    assert 1\n"},
             "Fix flaky test_a with a wait", days=30)
    r.commit({"tests/test_flaky.py": "import time\nimport pytest\n\n\n@pytest.mark.flaky(reruns=3)\ndef test_a():\n"
                                     "    time.sleep(1)\n    assert 1\n"}, "Mark intermittent test to rerun", days=31)
    r.commit({"tests/test_rate.py": "import time\n\n\ndef test_r():\n    time.sleep(2)  # rate limit window\n"},
             "Add rate test", days=32)
    r.commit({"app/many.py": "".join(f"x{i} = int('1')  # type: ignore\n" for i in range(22)),
              "app/sec.py": "import subprocess\nsubprocess.call('ls', shell=True)  # nosec\n"},
             "Silence the checker", days=40)
    snaps = {f"tests/__snapshots__/s{i}.snap": f"snap {i}\n" for i in range(10)}
    r.commit({**snaps, "app/db.py": "def migrate(x):\n    if x is None:\n        return 0\n    return x + 1\n"},
             "Update stuff", days=50)
    r.commit({"app/db.py": "def migrate(x):\n    if x is None:\n        return 0\n    return x + 2\n"},
             "Fix migrate regression off by one", days=55)
    r.commit({"app/routes.py": 'routes = {}\nregister("home", home)\n'}, "Wire home", days=60)
    r.commit({"app/routes.py": 'routes = {}\nregister("home", home)\nregister("about", about)\n'}, "Wire about", days=61)
    r.ingest()
    return r


def test_coverage_threshold_lowered(cfg):
    hits = cfg.run("coverage_threshold_lowered")
    subs = subjects(hits)
    assert any(s.startswith("threshold|") and s.endswith("|pyproject.toml|fail_under") for s in subs)
    assert any(s.startswith("omit|") and s.endswith("|app/db.py") for s in subs)
    assert not any(s.endswith("app/legacy.py") for s in subs)  # in the initial omit list, not an addition


def test_flaky_test_markers(cfg):
    hits = cfg.run("flaky_test_markers")
    assert subjects(hits) == ["tests/test_flaky.py"]
    assert len(hits[0].commits) == 2
    assert cfg.ctx.noise.get("flaky_test_markers/rate_limit_sleep") == 1


def test_strictness_relaxation(cfg):
    hits = cfg.run("strictness_relaxation")
    subs = subjects(hits)
    assert any("|tsconfig.json|strict" in s for s in subs)
    assert any(s.endswith("|suppress") for s in subs)
    sup = next(h for h in hits if h.subject.endswith("|suppress"))
    assert sup.evidence["security_markers"]
    assert cfg.ctx.noise.get("strictness_relaxation/initial_config", 0) >= 1


def test_snapshot_blind_update(cfg):
    hits = cfg.run("snapshot_blind_update")
    assert len(hits) == 1
    assert hits[0].evidence["snapshot_files"] == 10
    assert hits[0].evidence["followup_fixes"][0]["subject"].startswith("Fix migrate regression")


def test_contract_one_side_tested(cfg):
    hits = cfg.run("contract_one_side_tested")
    assert subjects(hits) == ["config|CHECKOUT_MODE"]


def test_contract_tested_literal_is_dropped(cfg):
    cfg.commit({"tests/test_cfg.py": 'def test_mode():\n    assert "CHECKOUT_MODE"\n'}, "Add cfg test", days=70)
    cfg.ingest()
    assert cfg.run("contract_one_side_tested") == []
    assert cfg.ctx.noise.get("contract_one_side_tested/literal_in_tests") == 1


def test_composition_root_untested(cfg):
    hits = cfg.run("composition_root_untested")
    assert subjects(hits) == ["app/routes.py"]
    assert len(hits[0].commits) == 2


@pytest.fixture
def code(mini):
    r = mini()
    r.commit({
        "app/core.py": "import json\n\n\ndef parse_blob(data: str) -> dict:\n    return json.loads(data)\n\n\n"
                       "def format_blob(obj: dict) -> str:\n    return json.dumps(obj)\n",
        "tests/test_core.py": "from app.core import parse_blob\n\n\ndef test_parse():\n"
                              "    assert parse_blob('{\"a\": 1}') == {\"a\": 1}\n",
        "app/paths.py": 'import sys\n\n\ndef default_dir():\n    if sys.platform == "win32":\n'
                        '        return "C:/data"\n    return "/var/data"\n',
        "app/reader.py": "def read_text(path):\n    with open(path) as fh:\n        return fh.read()\n",
        "app/svc.py": "def compute(x):\n    return x * 2\n",
        "tests/test_svc.py": "from unittest import mock\nfrom app.svc import compute\n\n\ndef test_compute():\n"
                             "    with mock.patch(\"app.svc.compute\", return_value=4):\n        assert compute(2) == 4\n",
        "app/billing.py": "def charge(amount):\n    return amount\n",
        "tests/test_billing.py": "from unittest import mock\n\n\ndef test_pay():\n"
                                 "    with mock.patch(\"app.billing.charge\"):\n        pass\n",
        "app/modes.py": 'from enum import Enum\n\n\nclass Mode(Enum):\n    FAST = "fast"\n    SLOW = "slow"\n',
        "app/export.py": "def export(rows):\n    return rows\n",
        "tests/test_export.py": "from app.export import export\n\n\ndef test_export():\n    assert export([1]) == [1]\n",
        "pkgA/__init__.py": "",
        "pkgA/api.py": "def fetch(a):\n    return a\n",
        "pkgB/__init__.py": "",
        "pkgB/use.py": "from pkgA.api import fetch\n\n\ndef go():\n    return fetch(1)\n",
        **{f"app/gen/stubs{i}.py": f"def stub{i}():\n    return {i}\n" for i in range(5)},
    }, "Initial import", days=0)
    r.commit({"app/core.py": "import json\n\n\ndef parse_blob(data: str) -> dict:\n    if not data:\n        return {}\n"
                             "    return json.loads(data)\n\n\ndef format_blob(obj: dict) -> str:\n    return json.dumps(obj)\n"},
             "Fix parse_blob crash on empty input", days=10)
    r.commit({"app/core.py": "import json\n\n\ndef parse_blob(data: str) -> dict:\n    if not data:\n        return {}\n"
                             "    if not data.strip():\n        raise ValueError('blank')\n"
                             "    return json.loads(data)\n\n\ndef format_blob(obj: dict) -> str:\n    return json.dumps(obj)\n"},
             "Fix parse_blob blank input bug", days=11)
    r.commit({"app/paths.py": 'import sys\n\n\ndef default_dir():\n    if sys.platform == "win32":\n'
                              '        return "C:/data2"\n    return "/var/data"\n'},
             "Fix default_dir on Windows", days=12)
    r.commit({"app/reader.py": 'def read_text(path):\n    with open(path, encoding="utf-8") as fh:\n        return fh.read()\n'},
             "Fix read_text mojibake on windows", days=13)
    r.commit({"app/billing.py": 'def charge(amount, currency="USD"):\n    return amount\n'}, "Refactor charge", days=60)
    r.commit({"app/modes.py": 'from enum import Enum\n\n\nclass Mode(Enum):\n    FAST = "fast"\n    SLOW = "slow"\n'
                              '    TURBO = "turbo"\n',
              "app/runner.py": "from app.modes import Mode\n\n\ndef run(m):\n    return m == Mode.TURBO\n"},
             "Add turbo mode", days=70)
    r.commit({"app/export.py": 'def export(rows, fmt="csv"):\n    if fmt == "json":\n        return str(rows)\n    return rows\n',
              "app/cli.py": 'from app.export import export\n\n\ndef main():\n    return export([1], fmt="json")\n'},
             "Add json export", days=80)
    r.commit({"pkgA/api.py": "def fetch(a, b=0):\n    return a\n"}, "Extend fetch", days=90)
    r.commit({"pkgB/use.py": "from pkgA.api import fetch\n\n\ndef go():\n    return fetch(1, 2)\n"},
             "Fix go crash after fetch change", days=92)
    r.ingest()
    return r


def test_error_branch_untested(code):
    hits = code.run("error_branch_untested")
    assert uid(code, "app/core.py", "parse_blob") in subjects(hits)


def test_guard_input_class_unfed(code):
    hits = code.run("guard_input_class_unfed")
    assert f"{uid(code, 'app/core.py', 'parse_blob')}|empty" in subjects(hits)


def test_guard_input_type_hint_drops_none(code):
    code.commit({"app/core.py": open(code.root / "app/core.py").read().replace(
        "    if not data:", "    if data is None:\n        return {}\n    if not data:")}, "Fix parse_blob crash on None", days=95)
    code.ingest()
    subs = subjects(code.run("guard_input_class_unfed"))
    assert not any(s.endswith("|none") for s in subs)
    assert code.ctx.noise.get("guard_input_class_unfed/type_hint_excludes_class", 0) >= 1


def test_platform_config_branch_unexercised(code):
    hits = code.run("platform_config_branch_unexercised")
    assert subjects(hits) == [uid(code, "app/paths.py", "default_dir")]


def test_platform_text_path_class_gap(code):
    hits = code.run("platform_text_path_class_gap")
    assert f"{uid(code, 'app/reader.py', 'read_text')}|non_ascii" in subjects(hits)


def test_pure_function_roundtrip(code):
    hits = code.run("pure_function_roundtrip_property_candidate")
    assert any(h.evidence["law"] == "roundtrip" and {"parse_blob", "format_blob"} == set(h.evidence["pair"]) for h in hits)


def test_roundtrip_impure_is_dropped(code):
    code.commit({"app/core.py": open(code.root / "app/core.py").read().replace(
        "return json.dumps(obj)", "print(obj)\n    return json.dumps(obj)")}, "Log in format_blob", days=96)
    code.ingest()
    assert code.run("pure_function_roundtrip_property_candidate") == []
    assert code.ctx.noise["pure_function_roundtrip_property_candidate/impure_network_time_random_io"] >= 1


def test_mock_the_subject(code):
    hits = code.run("mock_the_subject")
    assert subjects(hits) == [uid(code, "app/svc.py", "compute")]


def test_mock_target_signature_drift(code):
    hits = code.run("mock_target_signature_drift")
    assert [h.subject.split("|")[0] for h in hits] == ["tests/test_billing.py"]
    assert hits[0].symbols == [uid(code, "app/billing.py", "charge")]


def test_mock_drift_dropped_when_test_touched(code):
    code.commit({"tests/test_billing.py": open(code.root / "tests/test_billing.py").read() + "\n# touched\n"},
                "Touch billing test", days=65)
    code.ingest()
    assert code.run("mock_target_signature_drift") == []
    assert code.ctx.noise["mock_target_signature_drift/test_touched_within_14d"] == 1


def test_declared_domain_gap(code):
    subs = subjects(code.run("declared_domain_gap"))
    assert f"{uid(code, 'app/modes.py', 'Mode', 'class')}|TURBO" in subs
    assert f"{uid(code, 'app/export.py', 'export')}|param|fmt" in subs


def test_interface_change_broke_seam(code):
    hits = code.run("interface_change_broke_seam")
    names = [h.subject.rsplit("|", 1)[1] for h in hits]
    assert "fetch" in names
    h = next(h for h in hits if h.subject.endswith("|fetch"))
    assert h.evidence["fix_files"] == ["pkgB/use.py"]


def test_benign_gap_not_worth_testing(code):
    hits = code.run("benign_gap_not_worth_testing")
    assert "app/gen" in subjects(hits)
    assert code.ctx.noise.get("benign_gap_not_worth_testing/never_benign_fix_api_contract_or_finding", 0) >= 1


def test_test_erosion_changepoint(mini, monkeypatch):
    monkeypatch.setattr(tq, "_MIN_WINDOW", 3)
    r = mini()
    for i, day in enumerate((0, 95, 190, 285)):
        r.commit({"app/core.py": f"def f():\n    return {i}\n", "tests/test_core.py": f"def test_f():\n    assert {i} >= 0\n"},
                 f"Change f {i}", days=day)
    for i, day in enumerate((380, 475, 570, 665)):
        r.commit({"app/core.py": f"def f():\n    return {i + 10}\n"}, f"Fix f bug {i}", days=day)
    r.ingest()
    hits = r.run("test_erosion_changepoint")
    assert len(hits) == 1
    assert hits[0].evidence["share_before"] == 1.0 and hits[0].evidence["share_after"] == 0.0
    assert hits[0].evidence["fixes_without_test_after"] == 4


def test_erosion_needs_enough_commits(mini):
    r = mini()
    for i, day in enumerate((0, 95, 190, 285)):
        r.commit({"app/core.py": f"def f():\n    return {i}\n", "tests/test_core.py": f"def test_f():\n    assert {i} >= 0\n"},
                 f"Change f {i}", days=day)
    r.commit({"app/core.py": "def f():\n    return 99\n"}, "Change f", days=380)
    r.ingest()
    assert r.run("test_erosion_changepoint") == []


def test_composition_root_ids_complete():
    from afd_oracle_ingest.catalog import load_all
    ids = """coverage_threshold_lowered error_branch_untested flaky_test_markers guard_input_class_unfed
    interface_change_broke_seam platform_config_branch_unexercised pure_function_roundtrip_property_candidate
    contract_one_side_tested mock_the_subject declared_domain_gap composition_root_untested mock_target_signature_drift
    snapshot_blind_update platform_text_path_class_gap strictness_relaxation test_erosion_changepoint
    benign_gap_not_worth_testing""".split()
    assert all(i in load_all() for i in ids)


# ------------------------------------------------------------------------- upgrades: enrichment + measured data
def _fresh(r):
    """New Ctx over the same store so cached properties (coverage) reload."""
    from afd_oracle_ingest.catalog.base import Ctx
    r.ctx = Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)
    return r.ctx


def _cover(r, path, executed, total=None):
    c = r.ctx.store.conn
    c.execute("DELETE FROM covered_lines WHERE path=?", (path,))
    c.execute("INSERT OR REPLACE INTO coverage_files VALUES (?,?,?)", (path, total or len(executed), len(executed)))
    for ln in executed:
        c.execute("INSERT OR REPLACE INTO covered_lines VALUES (?,?,?)", (path, ln, 1))
    c.commit()


def _all_lines_except(r, path, *skip_text):
    lines = (r.root / path).read_text().splitlines()
    return [i for i, ln in enumerate(lines, 1) if ln.strip() and ln.strip() not in skip_text]


def test_error_branch_untested_measured(code):
    parse = uid(code, "app/core.py", "parse_blob")
    assert parse in subjects(code.run("error_branch_untested"))
    assert code.run("error_branch_untested")[0].evidence["measured"] is False
    # every line executed (the guard branches ran): the text proxy is overruled and the symbol dropped
    _cover(code, "app/core.py", _all_lines_except(code, "app/core.py"))
    _fresh(code)
    assert parse not in subjects(code.run("error_branch_untested"))
    assert code.ctx.noise["error_branch_untested/guard_branch_executed"] == 1
    # the branch bodies never ran: measured hit, even though a linked test file exists
    _cover(code, "app/core.py", _all_lines_except(code, "app/core.py", "return {}", "raise ValueError('blank')"))
    _fresh(code)
    h = next(h for h in code.run("error_branch_untested") if h.subject == parse)
    assert h.evidence["measured"] is True and 0 in h.evidence["guard_branch_executions"]


def test_guard_input_class_unfed_measured(code):
    key = f"{uid(code, 'app/core.py', 'parse_blob')}|empty"
    assert key in subjects(code.run("guard_input_class_unfed"))
    _cover(code, "app/core.py", _all_lines_except(code, "app/core.py"))
    _fresh(code)
    assert key not in subjects(code.run("guard_input_class_unfed"))
    assert code.ctx.noise["guard_input_class_unfed/guard_branch_executed"] >= 1
    _cover(code, "app/core.py", _all_lines_except(code, "app/core.py", "return {}"))
    _fresh(code)
    h = next(h for h in code.run("guard_input_class_unfed") if h.subject == key)
    assert h.evidence["measured"] is True


def test_mock_the_subject_dropped_when_measured_executed(code):
    compute = uid(code, "app/svc.py", "compute")
    assert compute in subjects(code.run("mock_the_subject"))
    _cover(code, "app/svc.py", [2])
    _fresh(code)
    assert compute not in subjects(code.run("mock_the_subject"))
    assert code.ctx.noise["mock_the_subject/measured_executed"] == 1
    _cover(code, "app/svc.py", [], total=2)           # measured, never executed: kept, boosted, reported as 0
    _fresh(code)
    h = next(h for h in code.run("mock_the_subject") if h.subject == compute)
    assert h.evidence["measured_coverage"] == 0


def test_benign_gap_uses_measured_execution(code):
    h0 = next(h for h in code.run("benign_gap_not_worth_testing") if h.subject == "app/gen")
    assert h0.evidence["benign_symbols"] == 5
    for i in range(3):                                 # three stubs are in fact executed by the suite
        _cover(code, f"app/gen/stubs{i}.py", [2])
    for i in (3, 4):                                   # measured, never executed
        _cover(code, f"app/gen/stubs{i}.py", [], total=2)
    _fresh(code)
    h1 = next(h for h in code.run("benign_gap_not_worth_testing") if h.subject == "app/gen")
    assert h1.evidence["benign_symbols"] == 2 and h1.evidence["untested_symbols"] == 2


def test_interface_change_from_multiline_signature_history(mini):
    r = mini()
    r.commit({"pkgA/__init__.py": "", "pkgB/__init__.py": "",
              "pkgA/api.py": "def fetch(\n    a,\n    b=0,\n) -> int:\n    return a\n",
              "pkgB/use.py": "from pkgA.api import fetch\n\n\ndef go():\n    return fetch(1)\n"}, "Initial", days=0)
    r.commit({"pkgA/api.py": "def fetch(\n    a,\n    b=0,\n    *,\n    strict=False,\n) -> int:\n    return a\n"}, "Extend fetch", days=90)
    r.commit({"pkgB/use.py": "from pkgA.api import fetch\n\n\ndef go():\n    return fetch(1, strict=True)\n"},
             "Fix go crash after fetch change", days=92)
    r.ingest()
    hits = r.run("interface_change_broke_seam")
    assert [h.subject.rsplit("|", 1)[1] for h in hits] == ["fetch"]
    assert "strict" in hits[0].evidence["new_signature"] and "strict" not in hits[0].evidence["old_signature"]
    assert hits[0].symbols == [uid(r, "pkgA/api.py", "fetch")]


def test_platform_text_path_gap_attributes_by_symbol(mini):
    r = mini()
    r.commit({"app/reader.py": "def read_text(path):\n    with open(path) as fh:\n        return fh.read()\n\n\n"
                               "def other():\n    return 1\n"}, "Initial", days=0)
    r.commit({"app/reader.py": 'def read_text(path):\n    with open(path, encoding="utf-8") as fh:\n        return fh.read()\n\n\n'
                               "def other():\n    return 1\n"}, "Fix read_text mojibake on windows", days=13)
    # the fixed line is rewritten later, so it no longer appears in the HEAD body
    r.commit({"app/reader.py": 'def read_text(path):\n    with open(path, encoding="utf8") as fh:\n        return fh.read()\n\n\n'
                               "def other():\n    return 1\n"}, "Tidy reader", days=40)
    r.ingest()
    assert f"{uid(r, 'app/reader.py', 'read_text')}|non_ascii" in subjects(r.run("platform_text_path_class_gap"))


# ------------------------------------------------------------------------- upgrades: per-test coverage
def _cover_tests(r, path, rows, total=10):
    """rows: [(line, test)]. Registers the file as measured and writes per-line / per-test coverage."""
    c = r.ctx.store.conn
    c.execute("INSERT OR REPLACE INTO coverage_files VALUES (?,?,?)", (path, total, len({ln for ln, _ in rows})))
    for ln, t in rows:
        c.execute("INSERT OR REPLACE INTO covered_lines VALUES (?,?,?)", (path, ln, 1))
        c.execute("INSERT OR REPLACE INTO covered_line_tests VALUES (?,?,?)", (path, ln, t))
    c.commit()


def test_mock_the_subject_measured_per_test(code):
    compute = uid(code, "app/svc.py", "compute")
    tid = next(r["test"] for r in code.ctx.tests_for[compute] if r["source"] in ("static", "coverage"))
    # the patching test only ran its own lines; another (unlinked) run executed compute, so the run-level rule is moot
    _cover_tests(code, "tests/test_svc.py", [(5, tid)])
    _cover_tests(code, "app/svc.py", [(2, "tests/other.py::test_other")])
    _fresh(code)
    h = next(h for h in code.run("mock_the_subject") if h.subject == compute)
    assert h.evidence["measured_per_test"] == {"patching_tests": 1, "executing_subject": 0}
    # now the patching test itself executes a body line of compute: dropped
    _cover_tests(code, "app/svc.py", [(2, tid)])
    _fresh(code)
    assert compute not in subjects(code.run("mock_the_subject"))
    assert code.ctx.noise["mock_the_subject/patching_test_executes_subject"] == 1


def test_platform_text_gap_measured_covering_tests(code):
    key = f"{uid(code, 'app/reader.py', 'read_text')}|non_ascii"
    assert key in subjects(code.run("platform_text_path_class_gap"))
    lines = (code.root / "app/reader.py").read_text().splitlines()
    ln = next(i for i, x in enumerate(lines, 1) if "encoding" in x)
    # a measured test that executes the line but has no non-ASCII input anywhere in its source: still a hit
    code.commit({"tests/test_reader.py": "from app.reader import read_text\n\n\ndef test_read():\n    read_text('a.txt')\n"},
                "Add reader test", days=100)
    code.ingest()
    t_plain = "tests/test_reader.py::test_read"
    _cover_tests(code, "app/reader.py", [(ln, t_plain)])
    _fresh(code)
    h = next(h for h in code.run("platform_text_path_class_gap") if h.subject == key)
    assert h.evidence["measured"] is True and h.evidence["covering_tests"] == [t_plain]
    # the covering test feeds a non-ASCII literal -> dropped, even though no linked-test text scan is involved
    code.commit({"tests/test_reader.py": "from app.reader import read_text\n\n\ndef test_read():\n    read_text('caf\u00e9.txt')\n"},
                "Feed non-ascii", days=101)
    code.ingest()
    _cover_tests(code, "app/reader.py", [(ln, t_plain)])
    _fresh(code)
    assert key not in subjects(code.run("platform_text_path_class_gap"))
    assert code.ctx.noise["platform_text_path_class_gap/class_present_in_covering_tests"] >= 1


def test_guard_input_class_unfed_covering_tests(code):
    key = f"{uid(code, 'app/core.py', 'parse_blob')}|empty"
    lines = (code.root / "app/core.py").read_text().splitlines()
    guard = next(i for i, x in enumerate(lines, 1) if x.strip() == "if not data:")
    t_plain, t_empty = "tests/test_core.py::test_parse", "tests/test_core.py::test_empty"
    # the guard line ran (so the symbol is reached) but its branch body never did, and no covering test has an empty literal
    _cover_tests(code, "app/core.py", [(guard, t_plain)])
    _fresh(code)
    h = next(h for h in code.run("guard_input_class_unfed") if h.subject == key)
    assert h.evidence["covering_tests"] == [t_plain]
    # a covering test whose own source contains an empty-string literal: dropped
    code.commit({"tests/test_core.py": open(code.root / "tests/test_core.py").read()
                 + "\n\ndef test_empty():\n    assert parse_blob('') == {}\n"}, "Add empty test", days=100)
    code.ingest()
    _cover_tests(code, "app/core.py", [(guard, t_empty)])
    _fresh(code)
    assert key not in subjects(code.run("guard_input_class_unfed"))
    assert code.ctx.noise["guard_input_class_unfed/class_literal_in_covering_test"] >= 1


def test_interface_change_from_raises_change(mini):
    r = mini()
    r.commit({"pkgA/__init__.py": "", "pkgB/__init__.py": "",
              "pkgA/api.py": "def load(a):\n    return a\n",
              "pkgB/use.py": "from pkgA.api import load\n\n\ndef go():\n    return load(1)\n"}, "Initial", days=0)
    r.commit({"pkgA/api.py": "def load(a):\n    if a < 0:\n        raise ValueError('neg')\n    return a\n"},
             "Validate load input", days=90)
    r.commit({"pkgB/use.py": "from pkgA.api import load\n\n\ndef go():\n    try:\n        return load(1)\n"
                             "    except ValueError:\n        return 0\n"}, "Fix go crash after load change", days=92)
    r.ingest()
    hits = r.run("interface_change_broke_seam")
    h = next(h for h in hits if h.subject.endswith("|load"))
    assert h.evidence["change_kind"] == "raises" and h.evidence["new_raises"] == ["ValueError"]
    assert h.evidence["old_raises"] == [] and h.evidence["fix_files"] == ["pkgB/use.py"]
