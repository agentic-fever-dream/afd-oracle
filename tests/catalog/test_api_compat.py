"""Tests for the API/compatibility catalogue family (builders in catalog/api_compat.py). One planted repo."""
from __future__ import annotations

import pytest

from .conftest import MiniRepo

API0 = ("import warnings\n\n\ndef fetch(url, timeout=5):\n    return url\n\n\ndef old_helper():\n    return 1\n\n\n"
        "def oldfunc():\n    warnings.warn('oldfunc is deprecated', DeprecationWarning)\n    return 2\n")
BASE0 = ("class Base:\n    def run(self, a):\n        return a\n\n\nclass Child(Base):\n    def run(self, a):\n"
         "        return a + 1\n\n\nclass Base2:\n    def go(self, a):\n        return a\n\n\nclass Child2(Base2):\n"
         "    def go(self, a):\n        return a\n")
SVC0 = ("from pkg.errors import NotFoundError\n\n\ndef lookup(key):\n    if key is None:\n        return None\n"
        "    raise NotFoundError(\"missing record for key\")\n\n\ndef caller_a(k):\n    try:\n        lookup(k)\n"
        "    except NotFoundError:\n        return 1\n\n\ndef caller_b(k):\n    try:\n        lookup(k)\n"
        "    except NotFoundError:\n        return 2\n")
MW0 = ('MIDDLEWARE = [\n    "app.sessions.SessionMiddleware",\n    "app.security.CsrfMiddleware",\n'
       '    "app.auth.AuthMiddleware",\n    "app.common.CommonMiddleware",\n]\n')
PROTO0 = 'syntax = "proto3";\npackage acme.v1;\nmessage User {\n  string name = 1;\n  int32 age = 2;\n}\n'
ALPHA0 = 'syntax = "proto3";\npackage acme.v1alpha;\nmessage Thing {\n  string name = 1;\n  int32 age = 2;\n}\n'
C_TMPL = ("from app.config import CACHE_TTL, RETRY_DELAY_MS\nfrom pkg.tools import render_report, build_thing\n\n\n"
          "def go(d):\n    return render_report(d, 'x') + CACHE_TTL + RETRY_DELAY_MS + build_thing(1)\n")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    base = tmp_path_factory.mktemp("apirepo")
    r = MiniRepo(base / "repo", base / "data", "apirepo")
    r.commit({"pkg/__init__.py": "", "pkg/api.py": API0, "pkg/base.py": BASE0, "pkg/svc.py": SVC0,
              "pkg/errors.py": "class AppError(Exception):\n    pass\n\n\nclass NotFoundError(AppError):\n    pass\n",
              "pkg/handlers.py": "from pkg.errors import AppError, LookupFailureError\n\n\ndef h1():\n    try:\n        pass\n"
                                 "    except AppError:\n        return 'fatal'\n\n\ndef h2():\n    try:\n        pass\n"
                                 "    except LookupFailureError:\n        return 'retry'\n",
              "pkg/monitor.py": "def check(exc):\n    if 'missing record for key' in str(exc):\n        return True\n",
              "pkg/tools.py": "def render_report(data, fmt):\n    return data\n\n\ndef build_thing(a):\n    return a\n",
              "settings.py": MW0, "app/installed.py": 'INSTALLED = [\n    "c",\n    "a",\n    "b",\n]\n',
              "schema/user.proto": PROTO0, "schema/alpha.proto": ALPHA0,
              "app/config.py": "RETRY_DELAY_MS = 500\nCACHE_TTL = 60\n",
              "app/c1.py": C_TMPL, "app/c2.py": C_TMPL,
              "app/c5.py": "from pkg.api import oldfunc\n\n\ndef z():\n    return oldfunc()\n"}, "Initial", days=0)
    r.git("tag", "v1.0.0")
    c1 = r.commit({"pkg/api.py": API0.replace("def fetch(url, timeout=5):", "def fetch(url, retries, timeout=5):").replace(
        "def old_helper():\n    return 1\n\n\n", "")}, "Tidy api", days=10)
    r.git("tag", "v1.0.1", c1)
    r.commit({"pkg/api.py": (r.root / "pkg/api.py").read_text() + "# keep fetch stable\n"},
             "Fix fetch regression", days=12)
    r.commit({"pkg/base.py": BASE0.replace("def run(self, a):\n        return a\n\n\nclass Child",
                                           "def run(self, a, b):\n        return a + b\n\n\nclass Child").replace(
        "def go(self, a):\n        return a\n\n\nclass Child2", "def go(self, a, b):\n        return a + b\n\n\nclass Child2"
    ).replace("class Child2(Base2):\n    def go(self, a):\n        return a\n",
              "class Child2(Base2):\n    def go(self, a, b):\n        return a - b\n")}, "Extend base signatures", days=15)
    r.commit({"pkg/errors.py": "class AppError(Exception):\n    pass\n\n\nclass LookupFailureError(AppError):\n    pass\n\n\n"
                               "class NotFoundError(LookupFailureError):\n    pass\n"}, "Reparent NotFoundError", days=20)
    svc = SVC0.replace('raise NotFoundError("missing record for key")', 'raise KeyMissingError("missing record for key")')
    r.commit({"pkg/svc.py": svc + "\n\nclass KeyMissingError(NotFoundError):\n    pass\n"}, "Raise KeyMissingError", days=22)
    r.commit({"pkg/svc.py": (svc + "\n\nclass KeyMissingError(NotFoundError):\n    pass\n").replace("missing record for key", "no such record")},
             "Clearer lookup message", days=25)
    r.commit({"settings.py": MW0.replace('    "app.security.CsrfMiddleware",\n    "app.auth.AuthMiddleware",\n',
                                         '    "app.auth.AuthMiddleware",\n    "app.security.CsrfMiddleware",\n'),
              "app/installed.py": 'INSTALLED = [\n    "a",\n    "b",\n    "c",\n]\n'}, "Reorder middleware", days=30)
    r.commit({"settings.py": (r.root / "settings.py").read_text() + "# order matters\n"},
             "Fix middleware ordering regression", days=33)
    r.commit({"schema/user.proto": PROTO0.replace("int32 age = 2;", "string years = 2;"),
              "schema/alpha.proto": ALPHA0.replace("int32 age = 2;", "string years = 2;")}, "Rework user schema", days=35)
    r.commit({"app/config.py": "RETRY_DELAY_SEC = 1\nCACHE_TTL = 60000\n"}, "Config in seconds", days=40)
    r.commit({"pkg/tools.py": "def render_report(data, fmt, locale):\n    return data\n\n\ndef build_thing(a):\n    return a\n"},
             "Localise reports", days=45)
    r.commit({"app/c1.py": C_TMPL.replace("render_report(d, 'x')", "render_report(d, 'x', 'en')")},
             "Update render_report call", days=46)
    r.commit({"app/c2.py": C_TMPL.replace("render_report(d, 'x')", "render_report(d, 'x', 'en')")},
             "Fix render_report call missing argument", days=47)
    r.commit({"pkg/tools.py": "def render_report(data, fmt, locale):\n    return data\n\n\ndef build_thing(a, b):\n    return a\n",
              "app/c1.py": C_TMPL.replace("render_report(d, 'x')", "render_report(d, 'x', 'en')").replace("build_thing(1)", "build_thing(1, 2)"),
              "app/c2.py": C_TMPL.replace("render_report(d, 'x')", "render_report(d, 'x', 'en')").replace("build_thing(1)", "build_thing(1, 2)")},
             "Extend build_thing", days=50)
    r.commit({"app/c1.py": (r.root / "app/c1.py").read_text() + "\n\ndef more():\n    return build_thing(2, 3)\n"},
             "More build_thing", days=51)
    r.commit({"app/c2.py": (r.root / "app/c2.py").read_text() + "\n\ndef more():\n    return build_thing(2, 4)\n"},
             "More build_thing again", days=52)
    r.commit({"pkg/api.py": (r.root / "pkg/api.py").read_text() +
              "\n\ndef modern(x):\n    return x\n\n\ndef legacy(x):\n    warnings.warn('legacy() is deprecated, use modern()', DeprecationWarning)\n    return modern(x)\n"},
             "Add legacy shim", days=55)
    r.commit({"app/c3.py": "from pkg.api import legacy\n\n\ndef use1():\n    return legacy(1)\n"}, "Use legacy once", days=56)
    r.commit({"app/c4.py": "from pkg.api import legacy\n\n\ndef use2():\n    return legacy(2)\n"}, "Use legacy twice", days=57)
    r.commit({"NOTES.md": "later\n"}, "Notes", days=700)
    r.ingest()
    return r


def test_public_api_breaking_change(repo):
    hits = repo.run("public_api_breaking_change")
    by = {h.symbols[0].split(":")[3]: h for h in hits if h.symbols}
    assert "old_helper" in by and "fetch" in by
    assert by["fetch"].evidence["bump"] == "patch"
    assert by["fetch"].evidence["fixed_or_reverted_within_14d"]
    assert "new required param retries" in by["fetch"].evidence["breaks"]


def test_deprecated_never_removed(repo):
    hits = repo.run("deprecated_never_removed")
    tiers = {h.subject: h.evidence["tier"] for h in hits}
    assert any(s.endswith(":legacy:function|primary") and t == "primary" for s, t in tiers.items())
    assert any(s.endswith(":oldfunc:function|low_age") for s in tiers)
    assert any(t == "low_removed_without_deprecation" for t in tiers.values())


def test_fragile_base_class(repo):
    hits = repo.run("fragile_base_class_override_drift")
    assert len(hits) == 1
    assert hits[0].evidence["stale_overrides"][0].endswith("Child.run:function")
    assert repo.ctx.noise.get("fragile_base_class_override_drift/all_overrides_updated", 0) >= 1


def test_exception_hierarchy(repo):
    hits = repo.run("exception_hierarchy_reparenting")
    assert [h.evidence["class"] for h in hits] == ["NotFoundError"]
    assert hits[0].evidence["old_bases"] == ["AppError"] and hits[0].evidence["new_bases"] == ["LookupFailureError"]


def test_message_consumer(repo):
    hits = repo.run("exception_message_matching_consumer")
    assert len(hits) == 1
    assert hits[0].evidence["consumers"][0]["path"] == "pkg/monitor.py"


def test_serialization(repo):
    hits = repo.run("serialization_field_renumber")
    assert len(hits) == 1 and hits[0].paths == ["schema/user.proto"]
    assert any("tag 2 reused" in b for b in hits[0].evidence["breaks"])
    assert repo.ctx.noise.get("serialization_field_renumber/alpha_package", 0) >= 1


def test_route_order(repo):
    hits = repo.run("route_and_middleware_order_change")
    assert [h.paths for h in hits] == [["settings.py"]]
    assert hits[0].evidence["fixes_within_14d"]
    assert repo.ctx.noise.get("route_and_middleware_order_change/formatter_resort", 0) >= 1


def test_api_signature_then_callsite_fix(repo):
    hits = repo.run("api_signature_change_then_callsite_fix")
    assert [h.evidence["function"] for h in hits] == ["render_report"]
    assert hits[0].evidence["error_wording"] >= 1
    assert repo.ctx.noise.get("api_signature_change_then_callsite_fix/callers_updated_in_same_commit", 0) >= 1


def test_raise_contract(repo):
    hits = repo.run("raise_contract_change_stale_callers")
    assert len(hits) == 1
    assert hits[0].evidence["mode"] == "class_changed"
    assert len(hits[0].evidence["stale_callers"]) == 2


def test_unit_change(repo):
    hits = repo.run("unit_change_without_callers")
    kinds = {h.evidence["kind"]: h for h in hits}
    assert set(kinds) == {"unit", "const"}
    assert "app/c1.py" in kinds["const"].evidence["stale_consumers"]


# ---- upgraded builders: symbol history / symbol attribution / tags table -------------------------------------
def test_signature_change_multiline_header_uses_symbol_versions(mini):
    """A multi-line ``def`` header (invisible to the old single-line pairing) is a signature change via symbol_versions."""
    r = mini()
    r.commit({"lib/core.py": "def compute_total(\n    items,\n    tax,\n):\n    return sum(items) + tax\n",
              "app/a.py": "from lib.core import compute_total\n\n\ndef a():\n    return compute_total([1], 2)\n",
              "app/b.py": "from lib.core import compute_total\n\n\ndef b():\n    return compute_total([2], 3)\n"},
             "Initial", days=0)
    ch = r.commit({"lib/core.py": "def compute_total(\n    items,\n    tax,\n    currency,\n):\n    return sum(items) + tax\n"},
                  "Add currency to compute_total", days=5)
    r.commit({"app/a.py": "from lib.core import compute_total\n\n\ndef a():\n    return compute_total([1], 2, 'usd')\n"},
             "Fix compute_total call missing argument", days=6)
    r.commit({"app/b.py": "from lib.core import compute_total\n\n\ndef b():\n    return compute_total([2], 3, 'usd')\n"},
             "Update compute_total call", days=7)
    hits = r.run("api_signature_change_then_callsite_fix")
    assert [h.evidence["function"] for h in hits] == ["compute_total"]
    assert hits[0].commits[0] == ch
    assert hits[0].evidence["new_params"] == ["items", "tax", "currency"]
    assert len(hits[0].evidence["followup_commits"]) == 2


def test_fragile_base_multiline_signature_history(mini):
    r = mini()
    r.commit({"pkg/base.py": "class Base:\n    def run(\n        self,\n        a,\n    ):\n        return a\n\n\n"
                             "class Child(Base):\n    def run(self, a):\n        return a + 1\n\n\n"
                             "class Other(Base):\n    def run(self, a):\n        return a + 2\n"}, "Initial", days=0)
    ch = r.commit({"pkg/base.py": "class Base:\n    def run(\n        self,\n        a,\n        b,\n    ):\n        return a + b\n\n\n"
                                  "class Child(Base):\n    def run(self, a):\n        return a + 1\n\n\n"
                                  "class Other(Base):\n    def run(self, a, b):\n        return a + b\n"},
                  "Extend Base.run", days=3)
    hits = r.run("fragile_base_class_override_drift")
    assert len(hits) == 1 and hits[0].commits == [ch]
    assert hits[0].evidence["old_params"] == ["a"] and hits[0].evidence["new_params"] == ["a", "b"]
    assert [u.split(":")[3] for u in hits[0].evidence["stale_overrides"]] == ["Child.run"]
    assert hits[0].evidence["updated_overrides"] == 1


def test_raise_contract_attributes_method_by_symbol(mini):
    r = mini()
    svc = ("class Repo:\n    def fetch(self, key):\n        if key is None:\n            return None\n"
           "        raise MissingError('no such key')\n")
    r.commit({"pkg/repo.py": svc, "pkg/errs.py": "class MissingError(Exception):\n    pass\n\n\nclass GoneError(Exception):\n    pass\n",
              "pkg/use1.py": "from pkg.repo import Repo\n\n\ndef one(k):\n    try:\n        Repo().fetch(k)\n    except MissingError:\n        return 1\n",
              "pkg/use2.py": "from pkg.repo import Repo\n\n\ndef two(k):\n    try:\n        Repo().fetch(k)\n    except MissingError:\n        return 2\n"},
             "Initial", days=0)
    r.commit({"pkg/repo.py": svc.replace("MissingError", "GoneError")}, "Raise GoneError instead", days=4)
    hits = r.run("raise_contract_change_stale_callers")
    assert len(hits) == 1
    assert hits[0].symbols[0].split(":")[3] == "Repo.fetch"
    assert hits[0].evidence["mode"] == "class_changed" and len(hits[0].evidence["stale_callers"]) == 2


def test_deprecated_counts_older_function_that_gained_a_call(mini):
    r = mini()
    r.commit({"pkg/api.py": "import warnings\n\n\ndef modern(x):\n    return x\n\n\ndef legacy(x):\n"
                            "    warnings.warn('legacy() is deprecated, use modern()', DeprecationWarning)\n    return modern(x)\n",
              "app/a.py": "def first():\n    return 1\n\n\ndef second():\n    return 2\n",
              "app/b.py": "def other():\n    return 1\n"}, "Initial", days=0)
    r.commit({"pkg/api.py": "import warnings\n\n\ndef modern(x):\n    return x\n\n\ndef legacy(x):\n"
                            "    warnings.warn('legacy() is deprecated, use modern()', DeprecationWarning)\n    return modern(x)\n"
                            "\n# note\n"}, "Touch api", days=1)
    # both callers already existed (created in the first commit) but start calling legacy() only later
    r.commit({"app/a.py": "from pkg.api import legacy\n\n\ndef first():\n    return legacy(1)\n\n\ndef second():\n    return 2\n"},
             "first uses legacy", days=30)
    r.commit({"app/b.py": "from pkg.api import legacy\n\n\ndef other():\n    return legacy(1)\n"}, "other uses legacy", days=31)
    r.commit({"pkg/api.py": (r.root / "pkg/api.py").read_text().replace("def legacy(x):\n", "def legacy(x):\n    # old\n")},
             "Comment", days=32)
    hits = [h for h in r.run("deprecated_never_removed") if h.subject.endswith(":legacy:function|primary")]
    assert hits, "older callers that adopted legacy() after its deprecation should be primary"
    assert hits[0].evidence["of_which_older_callers_with_new_call_lines"] == 2
    assert not any(u.endswith(":second:function") for u in hits[0].evidence["callers_adopting_after_deprecation"])


def test_public_api_breaking_change_uses_annotated_tags(mini):
    r = mini()
    r.commit({"pkg/__init__.py": "", "pkg/api.py": "def fetch(url, timeout=5):\n    return url\n"}, "Initial", days=0)
    r.git("tag", "-a", "v1.0.0", "-m", "Release one", days=0)
    c1 = r.commit({"pkg/api.py": "def fetch(url, retries, timeout=5):\n    return url\n"}, "Tidy api", days=10)
    r.git("tag", "-a", "v1.0.1", "-m", "Release one point one", c1, days=10)
    r.commit({"pkg/api.py": "def fetch(url, retries, timeout=5):\n    return url + ''\n"}, "Fix fetch regression", days=12)
    hits = r.run("public_api_breaking_change")
    assert [h.commits[0] for h in hits[:1]] == [c1]  # the peeled commit, not the annotated tag object
    assert hits[0].evidence["release_tag_message"] == "Release one point one"
    assert hits[0].evidence["fixed_or_reverted_within_14d"]


def test_serialization_version_constants(mini):
    r = mini()
    r.commit({"app/wire.py": "SCHEMA_VERSION = 2\n\n\ndef encode(msg):\n    return serialize(msg)\n",
              "app/proto.py": "PROTOCOL_VERSION = 1\n\n\ndef decode(blob):\n    return deserialize(blob)\n",
              "app/codec.py": "WIRE_VERSION = 5\n\n\ndef pack(msg):\n    return msg\n",
              "app/meta.py": "API_VERSION = 1\n__version__ = '1.0'\n"}, "Init", days=0)
    r.commit({"app/wire.py": "SCHEMA_VERSION = 3\n\n\ndef encode(msg):\n    return serialize(msg)\n"},
             "Bump schema version", days=10)
    r.commit({"app/proto.py": "PROTOCOL_VERSION = 2\n\n\ndef decode(blob):\n    if blob.version < PROTOCOL_VERSION:\n"
                              "        return upgrade(blob)\n    return deserialize(blob)\n"}, "Protocol v2 with reader gate", days=11)
    r.commit({"app/codec.py": "WIRE_VERSION = 4\n\n\ndef pack(msg):\n    return msg\n"}, "Roll back wire version", days=12)
    r.commit({"app/meta.py": "API_VERSION = 2\n__version__ = '1.1'\n"}, "Bump api version", days=13)
    hits = [h for h in r.run("serialization_field_renumber") if h.subject.endswith("|version")]
    by = {h.evidence["constant"]: h for h in hits}
    assert set(by) == {"SCHEMA_VERSION", "WIRE_VERSION"}              # gated bump and non-wire constants are not reported
    assert by["WIRE_VERSION"].evidence["monotonic_bump"] is False
    assert by["WIRE_VERSION"].score > by["SCHEMA_VERSION"].score        # a downgrade outranks a plain bump
    assert (by["SCHEMA_VERSION"].evidence["old"], by["SCHEMA_VERSION"].evidence["new"]) == ("2", "3")
    assert by["SCHEMA_VERSION"].evidence["reader_gates"] == []
    assert r.ctx.noise.get("serialization_field_renumber/bump_with_reader_gate", 0) == 1
