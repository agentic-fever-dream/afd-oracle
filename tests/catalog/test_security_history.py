"""Tests for the security-history catalogue family."""
from __future__ import annotations

VIEWS = """from flask import Blueprint
bp = Blueprint('x', __name__)


@bp.route('/a')
def a():
    return 1


@bp.route('/b')
def b():
    return 2


@bp.route('/c')
def c():
    return 3


@bp.route('/health')
def health():
    return 'ok'
"""
VIEWS_GUARDED = VIEWS.replace("@bp.route('/a')", "@bp.route('/a')\n@login_required").replace(
    "@bp.route('/b')", "@bp.route('/b')\n@login_required")
ADMIN = "@bp.route('/u')\n@admin_required\ndef users():\n    return 1\n\n\n@bp.route('/r')\n@admin_required\ndef roles():\n" \
        "    return 2\n\n\n@bp.route('/s')\n@admin_required\ndef stats():\n    return 3\n"
PR_TARGET = """name: pr
on:
  pull_request_target:
jobs:
  b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@main
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: someorg/deploy@main
"""
ITEMS = """def get_item(item_id):
    return Item.objects.get(id=item_id)


def delete_item(item_id):
    return Item.objects.get(id=item_id).delete()
"""
HOOKS = "\n\n".join(f"@csrf_exempt\ndef h{i}(request):\n    return {i}" for i in range(1, 4)) + "\n"
POLICY = '{\n  "Statement": [\n    {\n      "Action": ["s3:GetObject", "s3:PutObject"],\n      "Resource": "arn:aws:s3:::b/*"\n    }\n  ]\n}\n'
POLICY_TIGHT = POLICY.replace('"s3:GetObject", "s3:PutObject"', '"s3:GetObject"')
CSP_HOSTS = ["a.example.com", "b.example.com", "c.example.com", "d.example.com", "e.example.com", "f.example.com"]


def _csp(n: int) -> str:
    return 'CSP = "script-src \'self\' ' + " ".join(CSP_HOSTS[:n]) + '"\n'


def test_security_family(mini):
    r = mini()
    c = {}
    r.commit({
        "app/views.py": VIEWS, "app/admin.py": ADMIN, "app/items.py": ITEMS,
        "app/cors.py": "CORS(app, origins=['https://a.com'])\n",
        "app/settings.py": "ALLOWED_ORIGINS = ['https://a.com']\n",
        "svc/auth.py": "import logging\nlog = logging.getLogger()\n\n\ndef login(user, password):\n"
                       "    log.info('login %s %s', user, password)\n",
        "svc/other.py": "import logging\nlog = logging.getLogger()\n\n\ndef retry(password):\n"
                       "    log.warning('retry login password=%s', password)\n\n\n"
                       "def safe(password):\n    log.info('pw %s', mask(password))\n",
        "app/csp.py": 'CSP = "default-src \'self\'; script-src \'self\' https://cdn.a.com"\n',
        "app/csp2.py": 'HEADER = "Content-Security-Policy-Report-Only"\n',
        "web/webpack.dev.config.js": "const csp = \"script-src 'self'\";\n",
        "app/csp3.py": _csp(1),
        "infra/policy.json": POLICY,
        "app/models.py": "class User(db.Model):\n    id = db.Column(db.Integer)\n    email = db.Column(db.String)\n",
        "app/gdpr.py": "def erase_user(user):\n    user.email = None\n    user.nickname = None\n",
    }, "Initial", days=0)
    c["pr"] = r.commit({".github/workflows/pr.yml": PR_TARGET}, "Add PR preview deploy", days=5)
    c["guard"] = r.commit({"app/views.py": VIEWS_GUARDED}, "Require login on views", days=10)
    c["cors"] = r.commit({"app/cors.py": "CORS(app, origins='*', supports_credentials=True)\n"}, "Open CORS", days=12)
    c["cors_local"] = r.commit({"app/cors2.py": "ORIGINS = ['http://localhost:3000']\nCORS(app, origins=ORIGINS)\n"},
                               "dev cors", days=13)
    c["unguard"] = r.commit({"app/admin.py": ADMIN.replace("@admin_required\ndef stats", "def stats")},
                            "stats public for dashboards", days=20)
    r.commit({"app/settings.py": "ALLOWED_ORIGINS = ['https://a.com', 'https://b.com']\n"}, "origins b", days=30)
    r.commit({"app/settings.py": "ALLOWED_ORIGINS = ['https://b.com', 'https://c.com']\n"}, "origins c", days=31)
    c["origins3"] = r.commit({"app/settings.py": "ALLOWED_ORIGINS = ['https://c.com', 'https://d.com']\n"}, "origins d", days=32)
    for i in range(3):
        r.commit({"app/more_cors.py": "".join(f"allowed_origins.append('https://x{j}.com')\n" for j in range(i + 1))},
                 f"add x{i}", days=33 + i)
    c["scrub"] = r.commit({"svc/auth.py": "import logging\nlog = logging.getLogger()\n\n\ndef login(user, password):\n"
                                          "    log.info('login %s', user)\n"}, "Redact password from login log", days=40)
    c["unsafe"] = r.commit({"app/csp.py": 'CSP = "default-src \'self\'; script-src \'self\' \'unsafe-inline\' https://cdn.a.com"\n'},
                           "Allow inline scripts", days=50)
    c["enforce"] = r.commit({"app/csp2.py": 'HEADER = "Content-Security-Policy"\n'}, "Enforce CSP", days=51)
    r.commit({"web/webpack.dev.config.js": "const csp = \"script-src 'self' 'unsafe-eval'\";\n"}, "dev eval", days=52)
    for n in range(2, 7):
        r.commit({"app/csp3.py": _csp(n)}, f"allow host {n}", days=52 + n)
    c["tenant"] = r.commit({"app/items.py": ITEMS.replace("Item.objects.get(id=item_id)\n",
                                                          "Item.objects.get(id=item_id, tenant_id=tenant_id)\n", 1)},
                           "Fix IDOR: scope item lookup by tenant", days=60)
    for i in range(1, 4):
        r.commit({"app/hooks.py": "\n\n".join(f"@csrf_exempt\ndef h{k}(request):\n    return {k}" for k in range(1, i + 1)) + "\n"},
                 f"exempt h{i}", days=70 + i)
    r.commit({"app/hooks.py": HOOKS + "\n\n@csrf_exempt\ndef h4(request):\n    hmac.compare_digest(a, b)\n    return 4\n"},
             "exempt signed webhook", days=74)
    c["csrf_msg"] = r.commit({"app/web_settings.py": "CSRF_TRUSTED_ORIGINS = ['https://a.com']\n"},
                             "Fix 403 csrf origin check failed", days=75)
    r.commit({"infra/policy.json": POLICY_TIGHT}, "Tighten policy", days=81)
    c["readd"] = r.commit({"infra/policy.json": POLICY}, "Restore PutObject for uploader", days=90)
    c["wide"] = r.commit({"infra/policy.json": POLICY.replace("arn:aws:s3:::b/*", "*")},
                         "Fix AccessDenied 403 on bucket", days=95)
    r.commit({"app/models.py": "class User(db.Model):\n    id = db.Column(db.Integer)\n    email = db.Column(db.String)\n"
                               "    phone = db.Column(db.String)\n",
              "app/gdpr.py": "def erase_user(user):\n    user.email = None\n    user.nickname = None\n    user.phone = None\n"},
             "Add phone and erase it", days=100)
    c["pii"] = r.commit({"app/models.py": "class User(db.Model):\n    id = db.Column(db.Integer)\n    email = db.Column(db.String)\n"
                                          "    phone = db.Column(db.String)\n    address = db.Column(db.String)\n"},
                        "Add address", days=130)
    r.ingest()

    hits = r.run("route_guard_partial_rollout")
    roll = [h for h in hits if h.commits == [c["guard"]]]
    assert len(roll) == 1 and roll[0].evidence["unguarded_older_handlers"] == ["c"]
    assert [h for h in hits if h.commits == [c["unguard"]] and h.evidence["now_unguarded"] == ["stats"]]
    assert r.ctx.noise["route_guard_partial_rollout/excluded_handler_name"] >= 1

    hits = r.run("ci_privilege_and_trigger_widening")
    rules = {h.subject.split("|")[2]: h for h in hits}
    assert "pull_request_target" in rules and rules["pull_request_target"].evidence["pr_head_checkout"]
    assert "unpinned_main" in rules and r.ctx.noise["ci_privilege_and_trigger_widening/first_party_action"] >= 1

    hits = r.run("cors_policy_change_history")
    kinds = {h.subject.split("|")[-1] for h in hits}
    assert {"credentials_with_open_origin", "wildcard_origin"} <= kinds
    assert any(h.subject == "app/settings.py|origin_churn" for h in hits)
    assert not any("more_cors" in h.subject or "cors2" in h.subject for h in hits)
    assert r.ctx.noise["cors_policy_change_history/repeated_single_domain_additions"] == 1

    hits = r.run("pii_in_log_statement_fix")
    assert [h.subject for h in hits] == [c["scrub"]]
    assert [s["path"] for s in hits[0].evidence["siblings_at_head"]] == ["svc/other.py"]
    assert r.ctx.noise["pii_in_log_statement_fix/already_masked"] >= 1

    hits = r.run("security_header_csp_policy_change")
    kinds = {h.subject.split("|")[2] if h.subject.count("|") >= 2 else "x": h for h in hits}
    assert any(h.commits == [c["unsafe"]] for h in hits)
    assert any(h.commits == [c["enforce"]] for h in hits)
    assert any(h.subject.endswith("|hosts") and "csp3" in h.subject for h in hits)
    assert r.ctx.noise["security_header_csp_policy_change/dev_config"] >= 1
    assert not any("webpack" in p for h in hits for p in h.paths)

    hits = r.run("tenant_filter_sibling_drift")
    assert len(hits) == 1 and hits[0].commits == [c["tenant"]]
    assert [s["text"] for s in hits[0].evidence["siblings_at_head"]] == ["return Item.objects.get(id=item_id).delete()"]

    hits = r.run("csrf_exemption_and_trusted_origin_drift")
    subj = {h.commits[0] for h in hits}
    assert c["csrf_msg"] in subj
    ex = [h for h in hits if h.evidence["ordinal"] == 3]
    assert len(ex) == 1 and "h3" in ex[0].evidence["handler"]
    assert r.ctx.noise["csrf_exemption_and_trusted_origin_drift/signed_webhook"] == 1

    hits = r.run("access_scope_oscillation")
    assert any(h.commits[-1] == c["readd"] and "PutObject" in h.title for h in hits)
    assert any(h.commits == [c["wide"]] for h in hits)

    hits = r.run("pii_field_without_erasure_path")
    assert [h.subject.split("|")[1] for h in hits] == ["address"] and hits[0].commits[0] == c["pii"]
    assert hits[0].symbols == ["sym:" + r.name + ":app/gdpr.py:erase_user:function"]


def test_tenant_siblings_are_attributed_to_functions(mini):
    r = mini()
    items = ("def get_item(item_id):\n    return Item.objects.get(id=item_id)\n\n\n"
             "def delete_item(item_id):\n    return Item.objects.get(id=item_id).delete()\n")
    r.commit({"app/items.py": items}, "Initial", days=0)
    r.commit({"app/items.py": items + "\n\ndef old_item(old_id):\n    return Item.objects.get(id=old_id)\n"}, "Add old_item", days=2)
    r.commit({"app/items.py": items}, "Drop old_item", days=4)
    fix = r.commit({"app/items.py": items.replace("Item.objects.get(id=item_id)\n", "Item.objects.get(id=item_id, tenant_id=tenant_id)\n", 1)},
                   "Fix IDOR: scope item lookup by tenant", days=10)
    hits = r.run("tenant_filter_sibling_drift")
    assert len(hits) == 1 and hits[0].commits == [fix]
    assert [(s["text"], s["function"]) for s in hits[0].evidence["siblings_at_head"]] == [
        ("return Item.objects.get(id=item_id).delete()", "delete_item")]
    assert r.ctx.noise["tenant_filter_sibling_drift/site_gone_at_head"] >= 1


def test_route_guard_uses_symbol_creation_times(mini):
    r = mini()
    base = ("@bp.route('/a')\ndef a():\n    return 1\n\n\n@bp.route('/b')\ndef b():\n    return 2\n\n\n"
            "@bp.route('/c')\ndef c():\n    return 3\n")
    r.commit({"app/v.py": base}, "Initial", days=0)
    r.commit({"app/v.py": base.replace("@bp.route('/a')", "@bp.route('/a')\n@login_required").replace(
        "@bp.route('/b')", "@bp.route('/b')\n@login_required") + "\n\n@bp.route('/d')\ndef d():\n    return 4\n"},
        "Require login on a and b, add d", days=5)
    hits = r.run("route_guard_partial_rollout")
    assert len(hits) == 1
    # c existed before the rollout commit and is unguarded; d was created by it and is not "older"
    assert hits[0].evidence["unguarded_older_handlers"] == ["c"]


# ---------------------------------------------------------------------------------------------- round-2 enrichment
def _fresh_ctx(r):
    from afd_oracle_ingest.catalog.base import Ctx
    r.ctx.store.conn.commit()
    return Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)


def _run_on(ctx, cid):
    from afd_oracle_ingest.catalog import load_all
    ctx._current = cid
    return list(load_all()[cid](ctx))


def test_route_guard_models_class_based_views(mini):
    r = mini()
    base = ("from django.views import View\nfrom flask.views import MethodView\n\n\n"
            "class AView(View):\n    def get(self, request):\n        return 1\n\n\n"
            "class BView(MethodView):\n    def get(self):\n        return 2\n\n\n"
            "class CView(View):\n    def dispatch(self, request, *a, **k):\n        if not request.user.is_authenticated:\n"
            "            return None\n        return super().dispatch(request, *a, **k)\n\n    def get(self, request):\n        return 3\n\n\n"
            "class ReportView(View):\n    def get(self, request):\n        return 4\n\n    def post(self, request):\n        return 5\n")
    r.commit({"app/views.py": base}, "Initial", days=0)
    new = base.replace("class AView(View):\n", "class AView(View):\n    decorators = [login_required]\n\n") \
              .replace("class BView(MethodView):\n", "class BView(MethodView):\n    decorators = [login_required]\n\n")
    r.commit({"app/views.py": new}, "Require login on A and B views", days=5)
    hits = r.run("route_guard_partial_rollout")
    assert len(hits) == 1
    assert hits[0].evidence["guard"] == "login_required" and hits[0].evidence["added_to"] == 2
    assert sorted(hits[0].evidence["guarded_at_head"]) == ["AView", "BView"]
    # CView has an auth check in dispatch (other auth); only ReportView (verb methods, older, unguarded) is missing
    assert hits[0].evidence["unguarded_older_handlers"] == ["ReportView"]
    assert r.ctx.noise.get("route_guard_partial_rollout/inline_or_other_auth", 0) >= 1


def test_route_guard_class_decorator_form(mini):
    r = mini()
    base = ("class A(APIView):\n    def get(self, r):\n        return 1\n\n\nclass B(APIView):\n    def get(self, r):\n        return 2\n\n\n"
            "class Legacy(APIView):\n    def get(self, r):\n        return 3\n")
    r.commit({"api/views.py": base}, "Initial", days=0)
    r.commit({"api/views.py": base.replace("class A(", "@method_decorator(login_required, name='dispatch')\nclass A(")
              .replace("class B(", "@method_decorator(login_required, name='dispatch')\nclass B(")}, "Login for A, B", days=3)
    hits = r.run("route_guard_partial_rollout")
    assert len(hits) == 1 and hits[0].evidence["unguarded_older_handlers"] == ["Legacy"]


_PRT = "on:\n  pull_request_target:\n    branches: [main]\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


def test_ci_pull_request_target_dropped_for_private_repo_without_forks(mini):
    import json
    r = mini()
    r.commit({".github/workflows/ci.yml": "on: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"},
             "Init", days=0)
    r.commit({".github/workflows/ci.yml": _PRT}, "Run on PRs with secrets", days=2)
    r.ingest()
    assert [h.evidence["rule"] for h in r.run("ci_privilege_and_trigger_widening")] == ["pull_request_target"]  # unknown
    st = r.ctx.store
    st.set_meta("github_repo", json.dumps({"private": True, "visibility": "private", "fork": False, "forks_count": 0,
                                           "allow_forking": False}))
    ctx = _fresh_ctx(r)
    assert _run_on(ctx, "ci_privilege_and_trigger_widening") == []
    assert ctx.noise.get("ci_privilege_and_trigger_widening/private_repo_without_forks", 0) == 1
    st.set_meta("github_repo", json.dumps({"private": False, "visibility": "public", "forks_count": 12,
                                           "allow_forking": True}))
    assert len(_run_on(_fresh_ctx(r), "ci_privilege_and_trigger_widening")) == 1       # public: kept
    st.set_meta("github_repo", json.dumps({"private": True, "visibility": "private", "forks_count": 3,
                                           "allow_forking": True}))
    assert len(_run_on(_fresh_ctx(r), "ci_privilege_and_trigger_widening")) == 1       # private but forked: kept
