"""Tests for the legacy-evolution catalogue builders (planted histories, one repo per group of builders)."""
import json

BIG_HELPER = "def _helper(x):\n" + "".join(f"    x = x + {i}\n" for i in range(1, 18)) + "    return x\n"


def subjects(hits):
    return [h.subject for h in hits]


def _plant_a(r):
    r.commit({"scripts/legacy_tool.py": "".join(f"print('step {i}')\n" for i in range(12)),
              "Makefile": "run:\n\tpython scripts/legacy_tool.py\n",
              "app/limits.py": "# Requests time out after 30 seconds\nTIMEOUT = 30\n",
              "app/legacy.py": "def parse_input(x):\n    # TODO: handle unicode input properly here\n    return int(x)\n",
              "app/util.py": BIG_HELPER,
              "app/main.py": "from app.util import _helper\n\n\ndef run():\n    return _helper(1)\n",
              "app/configure.py": "def configure(a):\n    return a\n",
              "app/use.py": "from app.configure import configure\n\n\n" + "".join(
                  f"def u{i}():\n    return configure({i})\n\n\n" for i in range(6)),
              "app/boot.py": "import os\n"}, "Initial layout", days=0)
    r.commit({"scripts/legacy_tool.py": None,
              "scripts/modern_tool.py": "".join(f"print('step {i}')\n" for i in range(12))}, "Rename tool", days=10)
    r.commit({"app/limits.py": "# Requests time out after 30 seconds\nTIMEOUT = 60\n"}, "Raise timeout", days=20)
    r.commit({"app/calc.py": "def fast(x):\n    return x * 2\n\n\ndef total(x):\n    # old = compute_slow(x)\n"
                             "    # for i in range(10):\n    #     old += i\n    return fast(x)\n"},
             "Replace slow path with fast one", days=30)
    r.commit({"app/legacy.py": "def parse_input(x):\n    # TODO: handle unicode input properly here\n"
                               "    if x is None:\n        return 0\n    return int(x)\n"},
             "Fix crash when parse_input gets None", days=40)
    r.commit({"app/legacy.py": "def parse_input(x):\n    if x is None:\n        return 0\n    return int(x)\n"},
             "Drop stale note", days=60)
    sigs = ["def configure(a, b=None, c=False):\n    return a\n",
            "def configure(a, b=None, c=False, d=None, e=False):\n    return a\n",
            "def configure(a, b=None, c=False, d=None, e=False, f=None, g=False):\n    return a\n"]
    for k, sig in enumerate(sigs):
        r.commit({"app/configure.py": sig}, f"Add options batch {k}", days=70 + 10 * k)
    r.commit({"app/configure.py": "def configure(a, b=None, c=False, d=None, e=False, f=None, g=False):\n"
                                  "    if a is None:\n        return 0\n    return a\n"},
             "Fix crash in configure when a is None", days=95)
    r.commit({"app/main.py": "def run():\n    return 2\n"}, "Replace helper with new engine, no longer used", days=100)
    r.commit({"app/boot.py": "import os\nimport plugins  # noqa: F401  isort:skip\n"}, "Register plugins", days=105)
    r.commit({"README.md": "# readme\n"}, "Docs", days=500)
    return r.ingest()


def test_group_a(mini):
    r = mini()
    _plant_a(r)

    hits = r.run("renamed_path_still_referenced_in_tooling")
    assert [h.evidence["old_path"] for h in hits] == ["scripts/legacy_tool.py"]
    assert hits[0].paths[0] == "Makefile"

    hits = r.run("magic_constant_comment_divergence")
    assert len(hits) == 1 and hits[0].evidence["stale_value"] and "app/limits.py" in hits[0].paths

    hits = r.run("commented_out_code_block")
    assert len(hits) == 1 and hits[0].paths == ["app/calc.py"] and hits[0].evidence["survives_at_head"]

    hits = r.run("satd_comment_removed_code_unchanged")
    assert len(hits) == 1 and hits[0].symbols[0].endswith("app/legacy.py:parse_input:function")

    hits = r.run("dead_after_last_caller_removed")
    assert [h.symbols[0].split(":")[-2] for h in hits] == ["_helper"]

    hits = r.run("parameter_accretion")
    assert [h.symbols[0].split(":")[-2] for h in hits] == ["configure"]
    assert hits[0].evidence["flag_params"] >= 2

    hits = r.run("import_order_pin_marker")
    assert len(hits) == 1 and "bare_side_effect_import" in hits[0].evidence["signals"]


def test_import_pin_e402_after_sys_path_dropped(mini):
    r = mini()
    r.commit({"app/boot.py": "import sys\nsys.path.insert(0, 'lib')\nimport thing  # noqa: E402\n"}, "Add boot", days=0)
    assert r.run("import_order_pin_marker") == []
    assert r.ctx.noise.get("import_order_pin_marker/e402_after_sys_path") == 1


def test_magic_constant_comment_edited_same_commit_dropped(mini):
    r = mini()
    r.commit({"app/limits.py": "# Requests time out after 30 seconds\nTIMEOUT = 30\n"}, "Add", days=0)
    r.commit({"app/limits.py": "# Requests time out after 60 seconds\nTIMEOUT = 60\n"}, "Raise timeout", days=5)
    assert r.run("magic_constant_comment_divergence") == []


def test_commented_out_without_reason_dropped(mini):
    r = mini()
    r.commit({"app/calc.py": "def total(x):\n    # old = compute(x)\n    # for i in range(10):\n    #     old += i\n"
                             "    return x\n"}, "Tidy total", days=0)
    assert r.run("commented_out_code_block") == []
    assert r.ctx.noise.get("commented_out_code_block/no_reason_keyword") == 1


def _plant_b(r):
    def mod(n, call):
        return f"def f{n}():\n" + "".join(f"    r{k} = {call}({k})\n" for k in range(3)) + "    return r0\n"
    r.commit({f"app/m{n}.py": mod(n, "old_client_call") for n in range(4)} |
             {"migrations/001_add.sql": "ALTER TABLE users ADD COLUMN email_new TEXT;\n",
              "app/repo.py": "def save(row):\n    row['email_new'] = row['email']\n    return row\n"},
             "Initial modules", days=0)
    r.commit({f"app/m{n}.py": mod(n, "new_client_call") for n in range(4)}, "Migrate to new_client_call", days=10)
    r.commit({"app/late.py": "def late():\n    return old_client_call(9)\n"}, "Add late user", days=20)
    r.commit({"README.md": "x\n"}, "Docs", days=400)
    return r.ingest()


def test_group_b(mini):
    r = mini()
    _plant_b(r)
    hits = r.run("partial_api_migration_residue")
    assert len(hits) == 1
    assert hits[0].evidence["old"] == "old_client_call" and hits[0].evidence["post_sweep_additions"] == 1
    assert "app/late.py" in hits[0].evidence["residue_files"]

    hits = r.run("expand_contract_incomplete")
    assert subjects(hits) == ["email_new"] and hits[0].evidence["co_assigned_with_base"]


def test_expand_contract_dropped_later_is_dropped(mini):
    r = mini()
    r.commit({"migrations/001_add.sql": "ALTER TABLE users ADD COLUMN email_new TEXT;\n",
              "app/repo.py": "def save(row):\n    return row['email_new']\n"}, "Add", days=0)
    r.commit({"migrations/002_drop.sql": "ALTER TABLE users DROP COLUMN email_new;\n"}, "Drop", days=10)
    r.commit({"README.md": "x\n"}, "Docs", days=400)
    assert r.run("expand_contract_incomplete") == []
    assert r.ctx.noise.get("expand_contract_incomplete/dropped_later") == 1


def _plant_c(r):
    fn = lambda n, body: f"def f{n}(x):\n    {body}\n"  # noqa: E731
    r.commit({f"mods/m{n}.py": fn(n, "return x") for n in range(55)} |
             {"app/export.py": "def render(x):\n    return x\n",
              "app/export_v2.py": "def render_v2(x):\n    return x\n",
              "app/main.py": "from app.export import render\nfrom app.export_v2 import render_v2\n\n\n"
                             "def go():\n    render(1)\n    return render_v2(2)\n",
              "app/core.py": "def core_calc(x):\n    return x + 1\n",
              "app/users.py": "from app.core import core_calc\n\n\n" + "".join(
                  f"def c{i}():\n    return core_calc({i})\n\n\n" for i in range(4)),
              "shotgun/a.py": "A = 0\n", "shotgun/b.py": "B = 0\n", "shotgun/c.py": "C = 0\n", "shotgun/d.py": "D = 0\n"},
             "Add modules", days=0)
    r.commit({f"mods/m{n}.py": fn(n, "y = x\n    return y") for n in range(55)},
             "Refactor helpers across all modules", days=5)
    for k, n in enumerate((1, 2, 3)):
        r.commit({f"mods/m{n}.py": fn(n, "if x is None:\n        return 0\n    return x")},
                 f"Fix crash in f{n} when x is None", days=10 + k)
    for k in range(3):
        r.commit({f"shotgun/{c}.py": f"{c.upper()} = {k + 1}\n" for c in "abcd"}, f"Update schema fields {k}", days=20 + k)
    r.commit({f"shotgun/{c}.py": f"{c.upper()} = 9\n" for c in "abc"}, "Update schema again", days=100)
    r.commit({"shotgun/d.py": "D = 9\nif D is None:\n    D = 0\n"}, "Fix d missing field", days=110)
    for k in range(2):
        r.commit({"app/export_v2.py": f"def render_v2(x):\n    if x is None:\n        return {k}\n    return x\n"},
                 f"Fix crash in render_v2 case {k}", days=200 + k)
    r.commit({"app/core.py": "def core_calc(x):\n    return x + 2\n"}, "Update core calc rounding", days=900)
    r.commit({"app/core.py": "def core_calc(x):\n    if x is None:\n        return 0\n    return x + 2\n"},
             "Fix crash in core_calc when x is None", days=930)
    return r.ingest()


def test_group_c(mini):
    r = mini()
    _plant_c(r)

    hits = r.run("mass_automated_refactor_regression")
    assert len(hits) == 1 and hits[0].evidence["symbols_touched"] == 55 and len(hits[0].evidence["symbols_fixed_after"]) == 3

    hits = r.run("parallel_implementation_drift")
    assert len(hits) == 1 and hits[0].paths == ["app/export_v2.py", "app/export.py"]

    hits = r.run("shotgun_surgery_commit_pattern")
    assert len(hits) == 1 and hits[0].evidence["missing_file"] == "shotgun/d.py"

    hits = r.run("dormant_critical_path_code")
    assert len(hits) == 1 and hits[0].symbols[0].endswith("core_calc:function")


def test_special_case_accretion(mini):
    r = mini()
    body = 'def route(x):\n    if x == 0:\n        return "a"\n    return "z"\n'
    r.commit({"app/router.py": body}, "Add router", days=0)
    cur = ""
    for k in range(1, 11):
        cur += f'    if x == {k}:\n        y = "case{k}"\n        return y\n'
        msg = f"Fix crash for input {k}" if k % 3 == 0 else f"Handle input {k}"
        text = 'def route(x):\n    if x == 0:\n        return "a"\n' + cur + '    return "z"\n'
        if k % 3 == 0:
            text = text.replace('def route(x):\n', 'def route(x):\n    if x is None:\n        return "n"\n', 1)
        r.commit({"app/router.py": text}, msg, days=k * 10)
    hits = r.run("special_case_accretion")
    assert len(hits) == 1 and hits[0].evidence["branches_head"] >= 10


def test_adr_superseded_code_remains(mini):
    r = mini()
    r.commit({"app/legacy_store.py": "class LegacyStore:\n    def get(self):\n        return 1\n",
              "docs/adr/0003-use-legacy-store.md":
                  "# 3. Use store\n\nStatus: Superseded by ADR-0007\n\nWe use `app/legacy_store.py` and `LegacyStore`.\n",
              "docs/adr/0007-use-bar.md": "# 7. Use bar\n\nStatus: Accepted\n\nWe use `bar_store`.\n"}, "Add adrs", days=0)
    r.commit({"app/legacy_store.py": "class LegacyStore:\n    def get(self):\n        return 2\n"}, "Tweak store", days=50)
    hits = r.run("adr_superseded_code_remains")
    assert subjects(hits) == ["docs/adr/0003-use-legacy-store.md"]
    assert len(hits[0].evidence["resolved"]) == 2


def test_adr_without_modification_dropped(mini):
    r = mini()
    r.commit({"app/legacy_store.py": "class LegacyStore:\n    def get(self):\n        return 1\n",
              "docs/adr/0003-use-legacy-store.md":
                  "# 3. Use store\n\nStatus: Deprecated\n\nWe use `app/legacy_store.py` and `LegacyStore`.\n"},
             "Add adr", days=0)
    assert r.run("adr_superseded_code_remains") == []
    assert r.ctx.noise.get("adr_superseded_code_remains/not_modified_after_status") == 1


def test_loads_all():
    from afd_oracle_ingest.catalog import load_all
    ids = {"renamed_path_still_referenced_in_tooling", "magic_constant_comment_divergence", "special_case_accretion",
           "partial_api_migration_residue", "expand_contract_incomplete", "mass_automated_refactor_regression",
           "parallel_implementation_drift", "adr_superseded_code_remains", "shotgun_surgery_commit_pattern",
           "dormant_critical_path_code", "commented_out_code_block", "satd_comment_removed_code_unchanged",
           "dead_after_last_caller_removed", "import_order_pin_marker", "parameter_accretion"}
    assert ids <= set(load_all())
    json.dumps(sorted(ids))


def test_parameter_accretion_reads_multiline_signature_history(mini):
    r = mini()
    users = "from app.api import build\n\n\n" + "".join(f"def u{i}():\n    return build({i})\n\n\n" for i in range(6))

    def sig(n):
        names = ["a", "b=None", "c=False", "d=None", "e=False", "f=None", "g=False"][:n]
        return "def build(\n    " + ",\n    ".join(names) + ",\n):\n    return a\n"

    r.commit({"app/api.py": sig(3), "app/users.py": users}, "Add api", days=0)
    for k, n in enumerate((4, 5, 6, 7)):
        r.commit({"app/api.py": sig(n)}, f"Add option {n}", days=10 * (k + 1))
    r.commit({"app/api.py": sig(7).replace("return a", "if a is None:\n        return 0\n    return a")},
             "Fix crash in build when a is None", days=60)
    hits = r.run("parameter_accretion")
    assert [h.symbols[0].split(":")[-2] for h in hits] == ["build"]
    assert hits[0].evidence["param_count"] == 7 and hits[0].evidence["flag_params"] >= 2
    assert len(hits[0].evidence["growth_commits"]) == 4


def test_satd_removed_uses_enclosing_symbol_not_nearest_def(mini):
    r = mini()
    todo = "# TODO: this module needs a redesign someday soon\n"
    r.commit({"app/mod.py": "def worker(x):\n    return x\n\n\n" + todo + "LIMIT = 3\n"}, "Add worker", days=0)
    r.commit({"app/mod.py": "def worker(x):\n    if x is None:\n        return 0\n    return x\n\n\n" + todo + "LIMIT = 3\n"},
             "Fix crash in worker when x is None", days=20)
    r.commit({"app/mod.py": "def worker(x):\n    if x is None:\n        return 0\n    return x\n\n\nLIMIT = 3\n"},
             "Drop stale note", days=40)
    # the removed TODO sits at module level; scanning upward for the nearest def would wrongly pin it on worker()
    assert r.run("satd_comment_removed_code_unchanged") == []
    assert r.ctx.noise.get("satd_comment_removed_code_unchanged/no_enclosing_symbol") == 1


def test_dormant_and_mass_refactor_report_blame(mini):
    r = mini()
    _plant_c(r)
    # a later fix rewrites a line that the waking commit (day 900) wrote
    r.commit({"app/core.py": "def core_calc(x):\n    if x is None:\n        return 0\n    return x + 3\n"},
             "Fix wrong rounding in core_calc", days=935)
    r.ingest()
    hit = r.run("dormant_critical_path_code")[0]
    assert len(hit.evidence["blamed_fixes"]) == 1
    hit = r.run("mass_automated_refactor_regression")[0]
    assert len(hit.evidence["fixes_blaming_refactor"]) == 3


def test_special_case_accretion_reads_version_complexity(mini):
    r = mini()
    r.commit({"app/router.py": 'def route(x):\n    if x == 0:\n        return "a"\n    return "z"\n'}, "Add router", days=0)
    cur = ""
    for k in range(1, 11):
        cur += f'    if x == {k}:\n        y = "case{k}"\n        return y\n'
        msg = f"Fix crash for input {k}" if k % 3 == 0 else f"Handle input {k}"
        r.commit({"app/router.py": 'def route(x):\n    if x == 0:\n        return "a"\n' + cur + '    return "z"\n'},
                 msg, days=k * 10)
    hits = r.run("special_case_accretion")
    assert len(hits) == 1 and hits[0].evidence["versions"] >= 10
    assert hits[0].evidence["branches_first_seen"] == 1 and hits[0].evidence["branches_head"] == 11



def test_dormant_new_caller_between_tags_and_head(mini):
    r = mini()
    r.commit({"app/core.py": "def core_calc(x):\n    return x + 1\n",
              "app/a.py": "from app.core import core_calc\n\n\ndef use_a():\n    return core_calc(1)\n"},
             "Add core and first caller", days=0)
    r.git("tag", "v1", days=800)
    r.commit({"app/b.py": "from app.core import core_calc\n\n\ndef use_b():\n    return core_calc(2)\n",
              "app/c.py": "from app.core import core_calc\n\n\ndef use_c():\n    return core_calc(3)\n"},
             "Two new callers", days=900)
    r.git("tag", "v2", days=1000)
    r.commit({"app/d.py": "from app.core import core_calc\n\n\ndef use_d():\n    return core_calc(4)\n"},
             "Another caller after the last release", days=1100)
    r.ingest()
    assert r.ctx.calls_at("v1") and r.ctx.calls_at("v2")
    hits = [h for h in r.run("dormant_critical_path_code") if h.symbols[0].endswith("core_calc:function")]
    by_boundary = {h.evidence["boundary"]: h for h in hits}
    assert set(by_boundary) == {"v2"} or set(by_boundary) == {"v2", "HEAD"}
    h = by_boundary["v2"]
    assert h.evidence["trigger"] == "new_caller" and h.evidence["dormant_days"] >= 730
    assert len(h.evidence["new_callers"]) == 2
    # fallback: with no per-release call edges the trigger is silently skipped (and nothing else wakes core_calc)
    r.ctx.__dict__["tags"] = []
    assert not [x for x in r.run("dormant_critical_path_code") if x.symbols[0].endswith("core_calc:function")]


def test_satd_removal_uses_symbol_own_diff_not_file_diff(mini):
    r = mini()
    todo = "    # TODO: handle the empty case properly someday\n"
    other = "def other(x):\n    return x\n"
    r.commit({"app/mod.py": "def worker(x):\n" + todo + "    return x\n\n\n" + other}, "Add worker", days=0)
    r.commit({"app/mod.py": "def worker(x):\n" + todo + "    if x is None:\n        return 0\n    return x\n\n\n" + other},
             "Fix crash in worker when x is None", days=20)
    big_other = "def other(x):\n    a = x + 1\n    b = a * 2\n    c = b - 3\n    d = c / 4\n    return d\n"
    r.commit({"app/mod.py": "def worker(x):\n    if x is None:\n        return 0\n    return x\n\n\n" + big_other},
             "Drop stale note and rework other", days=40)
    hits = r.run("satd_comment_removed_code_unchanged")
    assert len(hits) == 1 and hits[0].symbols[0].endswith("app/mod.py:worker:function")
    assert hits[0].evidence["diff_basis"] == "symbol" and hits[0].evidence["non_comment_changed_lines"] == 0
    # the symbol's own body changing still disqualifies the removal
    r.commit({"app/mod.py": "def worker(x):\n    y = x\n    z = y\n    if x is None:\n        return 0\n    return z\n\n\n"
                            + big_other}, "Rework worker", days=60)
    r.ingest()
    assert [h for h in r.run("satd_comment_removed_code_unchanged") if h.evidence["diff_basis"] == "symbol"
            and h.evidence["non_comment_changed_lines"] >= 3] == []


def test_magic_constant_authority_needs_repeated_edits_uncommented(mini):
    r = mini()
    head = "# SLA policy sets the retry budget\n"
    r.commit({"app/limits.py": head + "RETRY_BUDGET = 3\n"}, "Add budget", days=0)
    r.commit({"app/limits.py": head + "RETRY_BUDGET = 5\n"}, "Raise budget", days=10)
    r.ingest()
    assert r.run("magic_constant_comment_divergence") == []     # one edit: authority alone no longer qualifies
    assert r.ctx.noise.get("magic_constant_comment_divergence/authority_single_edit") == 1
    h3 = r.commit({"app/limits.py": head + "RETRY_BUDGET = 8\n"}, "Raise budget again", days=20)
    r.ingest()
    hits = r.run("magic_constant_comment_divergence")
    assert len(hits) == 1 and hits[0].evidence["authority"] and not hits[0].evidence["stale_value"]
    assert hits[0].evidence["edit_count"] == 2 and hits[0].commits == [h3]

    # touching the comment in an edit resets the count
    r.commit({"app/limits.py": "# SLA policy section 4 sets the retry budget\nRETRY_BUDGET = 9\n"}, "Raise + doc", days=30)
    r.ingest()
    assert r.run("magic_constant_comment_divergence") == []
