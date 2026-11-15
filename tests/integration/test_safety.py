"""Change-safety layer: planted facts at HEAD (tests/fixtures/build_safety_repo.py) and the gate on real diffs."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_safety_repo", ROOT / "tests/fixtures/build_safety_repo.py")
build_safety_repo = importlib.util.module_from_spec(_spec)
sys.modules["build_safety_repo"] = build_safety_repo
_spec.loader.exec_module(build_safety_repo)


@pytest.fixture(scope="module")
def safety(env, tmp_path_factory):
    from afd_oracle_core.config import Settings, get_settings
    from afd_oracle_ingest.pipeline import open_context, run_deterministic

    repo_path = tmp_path_factory.mktemp("safety") / "safety-repo"
    facts = build_safety_repo.build(repo_path)
    # own data dir: other suites rely on `fixture` being the only registered repository
    s = Settings(**{**get_settings().model_dump(), "data_dir": tmp_path_factory.mktemp("safetydata")})
    ctx = open_context(repo_path, "safety", s, workers=1, log=lambda *_: None)
    out = run_deterministic(ctx, full=True)
    return ctx, facts, out["safety"], repo_path


def rules_at(ctx, path):
    return {(r["rule"], r["severity"]) for r in ctx.store.q("SELECT rule, severity FROM findings WHERE path=?", (path,))}


def test_head_findings(safety):
    ctx, _, summary, _ = safety
    core = rules_at(ctx, "app/core.py")
    assert {("sec.shell_true", "block"), ("race.shared_mutation", "warn"), ("race.lazy_init", "warn"),
            ("race.class_mutable", "warn")} <= core
    assert ("sec.sql_format", "block") in rules_at(ctx, "app/db.py")
    assert {("lb.date_gate", "warn"), ("lb.encoded_exec", "block")} <= rules_at(ctx, "app/maint.py")
    assert ("sec.js_html", "warn") in rules_at(ctx, "web/client.ts")
    assert not rules_at(ctx, "app/shapes.py")  # clean code stays quiet
    assert summary["by_severity"]["block"] == 3


def test_contracts(safety):
    ctx, *_ = safety
    roles = {(r["kind"], r["key"], r["role"]) for r in ctx.store.q("SELECT kind, key, role FROM contracts")}
    for kind, key in (("env", "DB_URL"), ("config", "TIMEOUT"), ("event", "user.created"), ("route", "/api/items")):
        assert {(kind, key, "producer"), (kind, key, "consumer")} <= roles, (kind, key)
    assert ("config", "DEFAULTS", "producer") not in roles
    orphans = {json.loads(r["detail"])["key"]: r["severity"] for r in ctx.store.q(
        "SELECT detail, severity FROM findings WHERE rule LIKE 'contract.orphan_consumer.%'")}
    assert orphans == {"MISSING_KEY": "warn", "SERVICE_URL": "info", "order.paid": "warn"}


def test_tests_api_lineage_callgraph(safety):
    ctx, facts, summary, _ = safety
    fetch = "sym:safety:app/api.py:fetch:function"
    tests = {(r["test"], r["source"]) for r in ctx.store.q("SELECT test, source FROM test_map WHERE target IN (?, ?)",
                                                           (fetch, "app/api.py"))}
    assert ("tests/test_api.py::test_fetch", "static") in tests and ("tests/test_api.py", "name") in tests
    assert not ctx.store.q1("SELECT 1 FROM test_map WHERE test='tests/__init__.py'")
    refs = {r["ref"] for r in ctx.store.q("SELECT DISTINCT ref FROM api_surface")}
    assert refs == {"HEAD", "v1.0"}
    api = {r["qualname"] for r in ctx.store.q("SELECT qualname FROM api_surface WHERE ref='HEAD' AND path='app/api.py'")}
    assert api == {"fetch", "Client", "Client.__init__", "Client.get"}  # __all__ respected, _helper excluded
    fix = ctx.store.q1("SELECT * FROM fix_events WHERE uid=?", (fetch,))
    assert fix["hash"] == facts["commits"]["c2_fix_none"]
    assert {g["kind"] for g in json.loads(fix["guards"])} >= {"none_check", "raise"}
    assert json.loads(fix["tests"]) == ["tests/test_api.py::test_fetch_none"]
    sec = ctx.store.q1("SELECT kinds FROM fix_events WHERE uid='sym:safety:app/views.py:render:function'")
    assert "security" in json.loads(sec["kinds"])
    typed = {(r["src"], r["dst"]) for r in ctx.store.q("SELECT src, dst FROM edges WHERE kind='CALLS' AND props LIKE '%typed%'")}
    assert ("sym:safety:app/api.py:Client.get:function", fetch) in typed
    assert ("sym:safety:app/events.py:user_created:function", "sym:safety:app/events.py:Bus.emit:function") in typed
    assert ctx.store.q1("SELECT 1 FROM edges WHERE kind='OVERRIDES' AND src LIKE '%Square.area%' AND dst LIKE '%Shape.area%'")
    assert ctx.store.q1("SELECT count(*) n FROM risk WHERE kind='file'")["n"] > 5


def test_assess_change(safety):
    from afd_oracle_ingest.safety.gate import Gate
    ctx, *_ = safety
    out = Gate(ctx.store, ctx.repo).assess_change(paths=["app/api.py"])
    assert out["risk_level"] == "high"  # public API
    assert {t["qualname"] for t in out["public_api"]} >= {"fetch", "Client.get"}
    assert any(t["test"] == "tests/test_api.py::test_fetch_none" for t in out["tests"])
    fetch_hist = next(h for h in out["history"] if h["uid"].endswith(":fetch:function"))
    assert fetch_hist["fixes"] == 1 and fetch_hist["regression_tests"] == ["tests/test_api.py::test_fetch_none"]
    schema = Gate(ctx.store, ctx.repo).assess_change(paths=["app/schema.py"])
    assert any(p["partner"] == "web/forms.ts" and p["hidden"] for p in schema["hidden_coupling"])


@pytest.fixture
def clone(safety, tmp_path):
    _, _, _, repo_path = safety
    dest = tmp_path / "wc"
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(repo_path), str(dest)], check=True)
    return dest


def check(safety, wc):
    from afd_oracle_ingest.safety.gate import Gate
    ctx, *_ = safety
    return Gate(ctx.store, ctx.repo).check_diff(workdir=str(wc))


def edit(wc: Path, rel: str, old: str, new: str) -> None:
    p = wc / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, (rel, old)
    p.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def rules_of(out):
    return {(f["rule"], f["severity"]) for f in out["findings"]}


def test_check_breaking_change_and_removed_guard(safety, clone):
    edit(clone, "app/api.py", "def fetch(key, timeout=10):", "def fetch(key, timeout):")
    edit(clone, "app/api.py", '    if key is None:\n        raise ValueError("key is required")\n', "")
    out = check(safety, clone)
    assert out["verdict"] == "block"
    assert {("api.param_now_required", "block"), ("regression.guard_removed", "block")} <= rules_of(out)
    assert any(t["test"].startswith("tests/test_api.py") for t in out["tests_to_run"])


def test_check_logic_bomb_and_contract_rename(safety, clone):
    edit(clone, "app/events.py", 'bus.emit("user.created", user)', 'bus.emit("user.made", user)\n'
         '    exec(__import__("base64").b64decode("cHJpbnQoMSk="))')
    out = check(safety, clone)
    r = rules_of(out)
    assert ("contract.producer_removed", "block") in r
    assert ("lb.encoded_exec", "block") in r and ("sec.eval", "block") in r


def test_check_supply_chain_and_coupling(safety, clone):
    (clone / "requirements.txt").write_text("flask==3.0.0\nreqeusts==2.31.0\nattrs==23.1.0\n", encoding="utf-8")
    edit(clone, "app/schema.py", '"phone"]', '"phone", "country"]')
    out = check(safety, clone)
    r = rules_of(out)
    assert ("sc.typosquat", "block") in r and ("sc.new_dependency", "warn") in r
    assert ("coupling.partner_not_changed", "warn") in r


def test_check_clean_change_passes(safety, clone):
    edit(clone, "app/shapes.py", "    def area(self):\n        return 0", "    def area(self):\n        return 0.0")
    out = check(safety, clone)
    assert out["counts"]["block"] == 0
    assert out["verdict"] in ("pass", "warn")


def test_check_new_race_and_untested(safety, clone):
    edit(clone, "app/shapes.py", "class Square(Shape):", "_SEEN = []\n\n\ndef remember(x):\n    _SEEN.append(x)\n\n\n"
         "class Square(Shape):")
    out = check(safety, clone)
    r = rules_of(out)
    assert any(rule == "race.shared_mutation" for rule, _ in r)
    assert ("tests.untested_change", "warn") in r


def test_diff_text_mode(safety, clone):
    """A diff passed as text (no working copy) is applied to the knowledge base's HEAD."""
    from afd_oracle_ingest.safety.gate import Gate
    ctx, *_ = safety
    edit(clone, "app/api.py", "def get(self, path, retries=3):", "def get(self, path):")
    diff = subprocess.run(["git", "-C", str(clone), "diff"], capture_output=True, text=True).stdout
    out = Gate(ctx.store, ctx.repo).check_diff(diff=diff)
    assert ("api.param_removed", "block") in rules_of(out)
