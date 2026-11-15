"""Configuration-family catalogue builders: one planted repo per group of builders (fast)."""
import pytest

EDGE0 = """server {
  client_max_body_size 50m;
  add_header Strict-Transport-Security "max-age=300";
  ssl_protocols TLSv1.2 TLSv1.3;
  location /api/ {
    proxy_pass http://backend/;
  }
}
"""
EDGE1 = EDGE0.replace("max-age=300", "max-age=31536000; includeSubDomains").replace("TLSv1.2 TLSv1.3", "TLSv1 TLSv1.2")
EDGE2 = EDGE1.replace("max-age=31536000; includeSubDomains", "max-age=300").replace("http://backend/;", "http://backend;")
EDGE3 = EDGE2.replace("http://backend;", "http://backend/;")

SETTINGS0 = """SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 3600
ATOMIC_REQUESTS = False
TIME_ZONE = "UTC"
MAX_CONTENT_LENGTH = 16 * 1024 * 1024
"""
SVC = 'import os\n\n\ndef retries():\n    return int(os.getenv("MYAPP_RETRIES", "3"))\n'
FLAGS0 = 'import os\n\nFAST = os.getenv("MYAPP_FAST_PATH", "0")\n'
APP_YAML0 = "max_batch_wait: 5\nmax_legacy_window: 7\nretained_limit: 9\nlegacy_read_timeout_seconds: 12\n"
BOOT = ("import os\n\n\ndef start():\n    env = os.getenv('APP_ENV', 'dev')\n    if env == 'production':\n"
        + "".join(f"        step_{i} = {i}\n" for i in range(12)) + "    return env\n")


_REPO = {}


@pytest.fixture
def repo(mini):
    if "r" not in _REPO:
        _REPO["r"] = _build(mini)
    return _REPO["r"]


def _build(mini):
    r = mini()
    c = {}
    r.commit({
        "deploy/edge.conf": EDGE0, "app/settings.py": SETTINGS0, "app/svc_a.py": SVC, "app/svc_b.py": SVC,
        "app/reader.py": 'def t(cfg):\n    return cfg["max_batch_wait"]\n',
        "app/reader2.py": 'def t(cfg):\n    return cfg["legacy_read_timeout_seconds"]\n',
        "app/legacy.py": 'def w(cfg):\n    return cfg["max_legacy_window"]\n',
        "config/app.yaml": APP_YAML0, "app/flags.py": FLAGS0, "app/boot.py": BOOT,
        "app/orphan.py": 'import os\n\nTOK = os.environ["MYAPP_ORPHAN_TOKEN"]\n',
        "jobs/q.py": "def grab(cur):\n    cur.execute(\"SELECT id FROM jobs WHERE state='new'\")\n",
        "Dockerfile": "FROM alpine\nCOPY scripts/run.sh /run.sh\n",
        "webpack.config.js": "module.exports = {\n  output: {\n    filename: '[name].[contenthash].js',\n    publicPath: '/static/',\n  },\n};\n",
        "sw.js": "const CACHE_NAME = 'v1';\n",
        "package.json": '{\n  "name": "web",\n  "homepage": "/app"\n}\n',
        "vite.config.js": "export default {\n  base: '/app/',\n};\n",
    }, "Initial", days=0)
    r.commit({"app/legacy.py": None}, "Remove legacy window code", days=3)
    r.commit({"config/app.yaml": APP_YAML0.replace("legacy_read_timeout_seconds: 12\n", "read_timeout_seconds: 12\n")},
             "Rename read timeout key", days=4)
    r.commit({"app/flags.py": "import os\n"}, "Drop fast path", days=5)
    c["tls"] = r.commit({"deploy/edge.conf": EDGE1}, "Harden edge", days=10)
    s1 = (SETTINGS0.replace('"Lax"', '"None"').replace("3600", "7200")
          .replace("ATOMIC_REQUESTS = False", "ATOMIC_REQUESTS = True"))
    r.commit({"app/settings.py": s1}, "Tweak session settings", days=11)
    s2 = s1.replace('"UTC"', '"Europe/Paris"')
    r.commit({"app/settings.py": s2}, "Use Paris time", days=12)
    c["tlsrev"] = r.commit({"deploy/edge.conf": EDGE2}, "Back out hsts", days=14)
    r.commit({"app/svc_a.py": SVC.replace('"3"', '"30"'), "app/svc_b.py": SVC.replace('"3"', '"30"')},
             "Bump retries", days=15)
    c["413"] = r.commit({"app/settings.py": s2.replace("16 * 1024 * 1024", "1 * 1024 * 1024")},
                        "Fix 413 payload too large on uploads", days=16)
    r.commit({"app/settings.py": s2.replace("16 * 1024 * 1024", "32 * 1024 * 1024")}, "Raise upload limit", days=17)
    c["slash"] = r.commit({"deploy/edge.conf": EDGE3}, "Restore trailing slash", days=18)
    c["fixenv0"] = r.commit({"app/cfg.py": 'import os\n\nURL = os.environ["MYAPP_NEW_SECRET_URL"]\n'},
                            "Add secret url", days=20)
    r.commit({".env.example": "MYAPP_NEW_SECRET_URL=http://x\n", "README.md": "env\n"},
             "Fix KeyError MYAPP_NEW_SECRET_URL missing in env", days=21)
    c["lock"] = r.commit(
        {"jobs/q.py": "def grab(cur):\n    cur.execute(\"SELECT id FROM jobs WHERE state='new' FOR UPDATE SKIP LOCKED\")\n"},
        "Fix race: two workers grabbed same job", days=30)
    c["sql"] = r.commit({"migrations/0002_tz.sql": "ALTER TABLE t ALTER COLUMN created TYPE timestamptz;\n"},
                        "Use timestamptz", days=31)
    c["wp"] = r.commit({"webpack.config.js": "module.exports = {\n  output: {\n    filename: '[name].js',\n    publicPath: '/assets/',\n  },\n};\n",
                        "sw.js": "const CACHE_NAME = 'v1';\nself.skipWaiting();\n"}, "Simplify bundle names", days=32)
    c["bump"] = r.commit({"sw.js": "const CACHE_NAME = 'v2';\nself.skipWaiting();\n"}, "Bump cache", days=33)
    c["chunk"] = r.commit({"app/loader.js": "export const x = 1;\n"}, "Fix ChunkLoadError after deploy", days=34)
    c["base"] = r.commit({"package.json": '{\n  "name": "web",\n  "homepage": "/portal"\n}\n',
                          "vite.config.js": "export default {\n  base: '/portal/',\n};\n"},
                         "Move app under /portal", days=35)
    r.commit({"app/report.py": "def r():\n    return 1\n"}, "Fix timezone bug in reports", days=50)
    body = "".join(f"key_{i}: {i}\n" for i in range(12))

    def sib(extra):
        return body + extra
    r.commit({"config/config.dev.yaml": sib("pool_size: 10\ncache_ttl: 60\n"),
              "config/config.staging.yaml": sib("pool_size: 10\ncache_ttl: 60\n"),
              "config/config.prod.yaml": sib("pool_size: 10\ncache_ttl: 60\n")}, "Add env configs", days=60)
    c["flagback"] = r.commit({"app/flags.py": 'import os\n\nFAST = os.getenv("MYAPP_FAST_PATH", "false") == "true"\n'},
                             "Bring back fast path", days=100)
    c["sib_a"] = r.commit({"config/config.prod.yaml": sib("pool_size: 50\ncache_ttl: 60\n")}, "Prod pool", days=130)
    c["sib_c"] = r.commit({"config/config.prod.yaml": sib("pool_size: 50\ncache_ttl: 600\n")}, "Prod ttl", days=240)
    r.commit({"README.md": "end\n"}, "docs", days=300)
    r.c = c
    r.ingest()
    return r


def test_tls(repo):
    hits = repo.run("tls_hsts_policy_ratchet")
    h = next(h for h in hits if h.subject.startswith(repo.c["tls"]))
    assert "max-age raised" in h.title and "includeSubDomains" in h.title and "TLS floor lowered" in h.title
    assert repo.c["tlsrev"] in h.commits


def test_dead_and_orphan(repo):
    dead = repo.run("dead_config_key")
    assert [h.subject for h in dead] == ["config/app.yaml|max_legacy_window"]
    assert repo.ctx.noise["dead_config_key/never_had_consumer"] >= 1
    orph = repo.run("config_key_rename_orphan")
    assert [h.subject for h in orph] == ["config/app.yaml|legacy_read_timeout_seconds"]
    assert "app/reader2.py" in orph[0].paths


def test_env_contract(repo):
    hits = repo.run("env_var_config_contract_break")
    a = [h for h in hits if h.subject.startswith("a|MYAPP_NEW_SECRET_URL")]
    assert a and a[0].commits[0] == repo.c["fixenv0"]
    assert [h for h in hits if h.subject == "c|MYAPP_ORPHAN_TOKEN"]
    assert not [h for h in hits if "MYAPP_RETRIES" in h.subject]


def test_default_flip_and_size(repo):
    hits = repo.run("config_default_flip")
    assert {h.subject.split("|")[1] for h in hits} == {"app/svc_a.py", "app/svc_b.py"}
    assert all(h.evidence["key"] == "MYAPP_RETRIES" for h in hits)
    size = repo.run("payload_and_message_size_limit_drift")
    assert any(h.subject.startswith("fix|" + repo.c["413"]) for h in size)
    assert any(h.subject.startswith("churn|app/settings.py|max_content_length") for h in size)
    assert any(h.subject == "head|http_body" for h in size)


def test_cookie_isolation_tz(repo):
    cookie = repo.run("cookie_attribute_change")
    assert [h.evidence["key"] for h in cookie] == ["session_cookie_samesite"]
    assert repo.ctx.noise["cookie_attribute_change/lifetime_tweak"] == 1
    iso = repo.run("isolation_or_autocommit_change")
    assert any(h.evidence.get("key") == "atomic_requests" for h in iso)
    lock = [h for h in iso if h.evidence.get("variant") == "lock_clause"]
    assert len(lock) == 1 and lock[0].commits == [repo.c["lock"]]
    tz = repo.run("timezone_encoding_change")
    kv = next(h for h in tz if h.evidence.get("key") == "time_zone")
    assert kv.evidence["later_fix"] is not None
    assert any(h.subject.startswith("sql|" + repo.c["sql"]) for h in tz)


def test_proxy_slash(repo):
    hits = repo.run("proxy_pass_uri_trailing_slash")
    assert len(hits) == 2
    assert hits[0].evidence["reverted_in"] == repo.c["slash"] and hits[1].evidence["reverted_in"] is None


def test_asset_and_base_path(repo):
    hits = repo.run("asset_cache_busting_change")
    wp = next(h for h in hits if h.subject == f"{repo.c['wp']}|webpack.config.js")
    assert "hash token removed" in wp.evidence["changes"][0]
    assert any(h.subject == f"{repo.c['wp']}|sw.js" for h in hits)
    assert any(h.subject == "fix|" + repo.c["chunk"] for h in hits)
    assert not any(h.subject.startswith(repo.c["bump"]) for h in hits)
    assert repo.ctx.noise["asset_cache_busting_change/routine_cache_bump"] == 1
    bp = repo.run("base_path_prefix_change")
    assert len(bp) == 1 and repo.c["base"] in bp[0].commits and len(bp[0].evidence["changes"]) == 3


def test_flag_recycled_and_env_branching(repo):
    hits = repo.run("flag_name_recycled")
    assert [h.subject.split("|")[0] for h in hits] == ["MYAPP_FAST_PATH"]
    assert repo.c["flagback"] in hits[0].commits
    eb = repo.run("environment_branching_in_code")
    assert len(eb) == 1 and eb[0].paths == ["app/boot.py"] and eb[0].evidence["guarded_lines"] == 12


def test_sibling_drift(repo):
    hits = repo.run("sibling_config_drift")
    a = [h for h in hits if h.subject.startswith("a|" + repo.c["sib_a"])]
    assert len(a) == 1 and a[0].evidence["key"] == "pool_size"
    c = [h for h in hits if h.subject.startswith("c|" + repo.c["sib_c"])]
    assert len(c) == 1 and "cache_ttl" in c[0].evidence["keys"]


def test_file_mode(mini):
    r = mini()
    r.commit({"scripts/run.sh": "#!/bin/sh\necho hi\n", "scripts/other.sh": "#!/bin/sh\necho x\n",
              "Dockerfile": "FROM alpine\nCOPY scripts/run.sh /run.sh\n"}, "Initial", days=0)
    r.git("update-index", "--chmod=+x", "scripts/run.sh", "scripts/other.sh", days=3)
    r.git("commit", "-q", "-m", "tweak modes", days=3)
    r.commit({"README.md": "x\n"}, "docs", days=4)
    hits = r.run("file_mode_flip_scars")
    assert [h.subject.split("|")[1] for h in hits] == ["scripts/run.sh"]
    assert hits[0].evidence["new_mode"] == "0o100755"
    assert r.ctx.noise["file_mode_flip_scars/bare_flip"] == 1


def test_file_mode_with_content_edit_and_symbol_fix_lines(mini):
    """Upgraded: a mode flip that coincides with a content edit (invisible to the old zero-line heuristic) is found
    from ctx.file_modes; environment branching counts fix lines that touch the guarded block."""
    r = mini()
    r.commit({"scripts/run.sh": "#!/bin/sh\necho hi\n", "Dockerfile": "FROM alpine\nCOPY scripts/run.sh /run.sh\n"},
             "Initial", days=0)
    (r.root / "scripts/run.sh").write_text("#!/bin/sh\necho hello\n")
    r.git("add", "scripts/run.sh", days=2)
    r.git("update-index", "--chmod=+x", "scripts/run.sh", days=2)
    r.git("commit", "-q", "-m", "Update greeting", days=2)
    hits = r.run("file_mode_flip_scars")
    assert len(hits) == 1 and hits[0].paths == ["scripts/run.sh"]
    assert hits[0].evidence["new_mode"] == "0o100755" and hits[0].evidence["bulk_commit"] is False

    r2 = mini()
    boot = BOOT
    r2.commit({"app/boot.py": boot}, "Add boot", days=0)
    fixed = boot.replace("step_3 = 3", "step_3 = 33")
    r2.commit({"app/boot.py": fixed}, "Fix wrong step_3 value in production boot", days=5)
    eb = r2.run("environment_branching_in_code")
    assert len(eb) == 1 and eb[0].evidence["fix_events_on_symbol"] >= 1
    assert eb[0].evidence["fix_lines_in_guarded_block"] == 1


# ---------------------------------------------------------------------------------------------- round-2 enrichment
def test_flag_recycled_from_contracts_across_tags(mini):
    from afd_oracle_ingest.catalog.base import Ctx
    from afd_oracle_ingest.catalog import load_all
    r = mini()
    for i, (tag, day) in enumerate((("v1.0", 0), ("v1.1", 20), ("v1.2", 60))):
        r.commit({"app/mod.py": f"x = {i}\n"}, f"Release {tag}", days=day)
        r.git("tag", tag, days=day)
    r.ingest()
    conn = r.ctx.store.conn
    conn.execute("DELETE FROM contracts_at")
    rows = [("v1.0", "env", "FEATURE_NEW_CHECKOUT", "consumer", "app/checkout.py", "s1"),
            ("v1.0", "env", "KEPT_SETTING_KEY", "consumer", "app/a.py", "s2"),
            ("v1.1", "env", "KEPT_SETTING_KEY", "consumer", "app/a.py", "s2"),
            ("v1.2", "env", "KEPT_SETTING_KEY", "consumer", "app/a.py", "s2"),
            ("v1.2", "env", "FEATURE_NEW_CHECKOUT", "consumer", "app/billing.py", "s3"),
            # recycled with the same consumer: not a recycle
            ("v1.0", "flag", "feature.same_consumer", "consumer", "app/s.py", "s4"),
            ("v1.2", "flag", "feature.same_consumer", "consumer", "app/s.py", "s4")]
    conn.executemany("INSERT INTO contracts_at VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    ctx = Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)
    ctx._current = "flag_name_recycled"
    hits = list(load_all()["flag_name_recycled"](ctx))
    assert [h.subject for h in hits] == ["FEATURE_NEW_CHECKOUT|v1.2"]
    ev = hits[0].evidence
    assert ev["source"] == "contracts_at" and ev["absent_at"] == ["v1.1"] and ev["disjoint_consumers"] is True
    assert ev["old_consumers"] == ["app/checkout.py"] and ev["new_consumers"] == ["app/billing.py"]
    assert ctx.noise.get("flag_name_recycled/same_consumers_after_gap", 0) == 1
