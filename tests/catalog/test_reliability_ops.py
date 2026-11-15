"""Tests for the reliability/ops catalogue family. Three planted repos (module-scoped) cover all 24 builders."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _miniclass():
    spec = importlib.util.spec_from_file_location("_cat_conftest_ro", Path(__file__).with_name("conftest.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_cat_conftest_ro"] = mod
    spec.loader.exec_module(mod)
    return mod.MiniRepo


def _make(tmp_path_factory, name):
    root = tmp_path_factory.mktemp(name)
    return _miniclass()(root / "repo", root / "data", name)


def _pem(not_after: datetime) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "svc.internal")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(datetime(2022, 1, 1, tzinfo=timezone.utc)).not_valid_after(not_after)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _rep(r, days=0):
    return r.ctx


# ====================================================================== repo A: config / deploy / history
@pytest.fixture(scope="module")
def ops(tmp_path_factory):
    r = _make(tmp_path_factory, "opsa")
    cron = ("apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: {n}\nspec:\n  schedule: \"0 2 * * *\"\n"
            "  jobTemplate:\n    spec:\n      template:\n        spec:\n          containers:\n"
            "          - name: c\n            command: [\"sh\", \"-c\", \"curl http://svc/{n}\"]\n")
    r.commit({
        "server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n",
        "Dockerfile": "FROM node:18\nCOPY . .\nCMD [\"node\", \"server/app.js\"]\n",
        ".github/workflows/release.yml": "name: release\non: push\njobs:\n  r:\n    steps:\n      - run: echo 1\n",
        "config/app.yaml": "pool_size: 5\nretries: 1\n",
        "config/limits.yaml": "name: limits\n",
        "gunicorn.conf.py": "# gunicorn config\nworkers = 4\n",
        "entrypoint.sh": "#!/bin/sh\nalembic upgrade head\nexec \"$@\"\n",
        "deploy/k8s/migrate-job.yaml": "kind: Job\nmetadata:\n  name: m\n",
        "deploy/k8s/hook.yaml": "metadata:\n  annotations:\n    helm.sh/hook-weight: \"5\"\n",
        "k8s/overlays/prod/kustomization.yaml": "replicas: 2\n",
        "k8s/overlays/staging/kustomization.yaml": "replicas: 2\n",
        "k8s/overlays/dev/kustomization.yaml": "replicas: 2\n",
        "certs/server.pem": _pem(datetime(2023, 3, 1, tzinfo=timezone.utc)),
        "config/tls.yaml": "name: tls\n",
        "k8s/cron/job1.yaml": cron.format(n="a"), "k8s/cron/job2.yaml": cron.format(n="b"),
        "k8s/cron/job3.yaml": cron.format(n="c"), "k8s/cron/job4.yaml": cron.format(n="d"),
    }, "Initial", days=0)
    r.git("tag", "v0.9.0", days=0)
    # keep-alive / 502
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"
                               "// n1\n"}, "Fix intermittent 502 bad gateway from ALB", days=2)
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"
                               "// n1\nserver.keepAliveTimeout = 65000;\n"}, "Raise keepAliveTimeout above ALB idle", days=3)
    # config change then fix
    r.commit({"config/app.yaml": "pool_size: 50\nretries: 1\n"}, "Tune pool", days=5)
    r.commit({"config/app.yaml": "pool_size: 20\nretries: 1\n"}, "Fix pool_size too large, exhausts db", days=5.5)
    # protective control added by fix, removed later
    r.commit({"config/limits.yaml": "name: limits\nrate_limit: 100\n"}, "Fix overload: add rate_limit", days=6)
    r.commit({"config/limits.yaml": "name: limits\n"}, "Tidy limits config", days=30)
    # release workflow fix chain
    for i in range(3):
        r.commit({".github/workflows/release.yml": f"name: release\non: push\njobs:\n  r:\n    steps:\n      - run: echo {i + 2}\n"},
                 f"Fix release workflow attempt {i + 1}", days=10 + i * 0.1)
    # emergency chain
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"
                               "// hotfix\n"}, "Hotfix: disable cache, temporary workaround", days=20)
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"
                               "// hotfix2\n"}, "Fix breakage from hotfix", days=20.2)
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"},
             "Revert hotfix", days=20.4)
    # hook weight change
    r.commit({"deploy/k8s/hook.yaml": "metadata:\n  annotations:\n    helm.sh/hook-weight: \"10\"\n"},
             "Reorder hook weight", days=40)
    # migration moved
    r.commit({"entrypoint.sh": "#!/bin/sh\nexec \"$@\"\n",
              "deploy/k8s/migrate-job.yaml": "kind: Job\nmetadata:\n  name: m\nspec:\n  command: alembic upgrade head\n"},
             "Move migrations to helm hook job", days=50)
    # memory leak + max_requests
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n"
                               "// m\n"}, "Fix memory leak in worker, rss grows", days=60)
    r.commit({"gunicorn.conf.py": "# gunicorn config\nworkers = 4\nmax_requests = 1000\n"}, "Recycle workers", days=61)
    # herd
    r.commit({"server/jitter.py": "import random\n\n\ndef jitter(x):\n    return x * random.random()\n"},
             "Add jitter to avoid thundering herd", days=70)
    # shutdown
    r.commit({"Dockerfile": "FROM node:18\nCOPY . .\nENTRYPOINT [\"tini\", \"--\"]\nCMD [\"node\", \"server/app.js\"]\n"},
             "Use tini so SIGTERM reaches node (graceful shutdown)", days=80)
    # flag valve
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n// p\n"},
             "Fix payment crash", days=90)
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n// p\n"
                               "const NEW_PATH = process.env.ENABLE_NEW_PATH !== 'false';\n"},
             "Add kill switch for new payment path", days=92)
    # bulk overlay change + revert
    r.commit({f"k8s/overlays/{e}/kustomization.yaml": "replicas: 10\n" for e in ("prod", "staging", "dev")},
             "Raise replicas everywhere", days=100)
    r.commit({f"k8s/overlays/{e}/kustomization.yaml": "replicas: 2\n" for e in ("prod", "staging", "dev")},
             "Revert replicas change", days=101)
    # release with post-release fixes
    r.commit({"server/app.js": "const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n// r1\n"},
             "Add feature for release", days=110)
    r.git("tag", "v1.0.0", days=110)
    for i in range(3):
        r.commit({"server/app.js": f"const http = require('http');\nconst server = http.createServer(h);\nserver.listen(80);\n// r1\n// f{i}\n"},
                 f"Fix crash {i} after release", days=111 + i)
    r.commit({"config/tls.yaml": "name: tls\nvalid_until: 2023-02-01\n"}, "Add tls expiry marker", days=115)
    r.ingest()
    return r


def _by(hits, key):
    return {getattr(h, key) if isinstance(key, str) else key(h): h for h in hits}


def test_keepalive(ops):
    hits = ops.run("app_keepalive_502_fix_lineage")
    assert len(hits) == 1 and "keepAliveTimeout" in str(hits[0].evidence["lines"])
    assert hits[0].evidence["symptom_commits"]


def test_release_chain(ops):
    hits = ops.run("release_workflow_edit_fix_chain")
    assert len(hits) == 1 and hits[0].paths == [".github/workflows/release.yml"] and len(hits[0].commits) == 3


def test_config_chain(ops):
    hits = ops.run("config_change_fix_chain")
    assert any(h.paths == ["config/app.yaml"] and "pool_size" in h.evidence["shared_keys"] for h in hits)


def test_protective_removed(ops):
    hits = ops.run("protective_control_removed")
    assert any(h.paths == ["config/limits.yaml"] for h in hits)


def test_emergency_chain(ops):
    hits = ops.run("emergency_commit_followup_chain")
    assert len(hits) == 1 and hits[0].evidence["ends_in_revert"] and len(hits[0].evidence["followups"]) == 2


def test_hook_ordering(ops):
    hits = ops.run("hook_and_init_ordering_change")
    assert any(h.paths == ["deploy/k8s/hook.yaml"] for h in hits)


def test_migration_moved(ops):
    hits = ops.run("migration_on_boot_placement_change")
    assert any("entrypoint->hook_or_job" in h.subject for h in hits)


def test_max_requests(ops):
    hits = ops.run("max_requests_leak_mask_and_herd")
    assert len(hits) == 1 and hits[0].evidence["jitter_configured_anywhere"] is False


def test_herd_commit(ops):
    hits = ops.run("synchronized_schedules_and_ttl_herd")
    subj = [h.subject for h in hits]
    assert any(s.startswith("herd|") for s in subj)
    assert any(s.startswith("slot:0 2") for s in subj)


def test_shutdown(ops):
    hits = ops.run("graceful_shutdown_signal_drift")
    assert any("Dockerfile" in h.paths for h in hits)


def test_flag_valve(ops):
    hits = ops.run("flag_added_as_rollback_valve")
    assert len(hits) == 1 and "ENABLE_NEW_PATH" in hits[0].evidence["flag_line"]


def test_bulk_blast(ops):
    hits = ops.run("config_bulk_blast_radius")
    assert len(hits) == 1 and len(hits[0].evidence["overlay_files"]) == 3


def test_post_release(ops):
    hits = ops.run("post_release_hotfix_cluster")
    assert any(h.evidence["release"] == "v1.0.0" and len(h.evidence["fixes"]) == 3 for h in hits)


def test_embedded_expiry(ops):
    hits = ops.run("embedded_expiry_literal")
    subj = [h.subject for h in hits]
    assert "cert|certs/server.pem" in subj
    assert any(s.startswith("date|config/tls.yaml") for s in subj)


# ====================================================================== repo B: code level
CLIENT0 = "import requests\nimport time\n\n\ndef fetch(url):\n    return requests.get(url)\n"
CLIENT1 = ("import requests\nimport time\n\n\ndef fetch(url):\n    for attempt in range(3):\n        try:\n"
           "            return requests.get(url)\n        except Exception:\n            time.sleep(1)\n")
CLIENT2 = CLIENT1.replace("requests.get(url)", "requests.get(url, timeout=5)")


@pytest.fixture(scope="module")
def code(tmp_path_factory):
    r = _make(tmp_path_factory, "opsb")
    r.commit({
        "app/client.py": CLIENT0,
        "app/client_ok.py": "import requests\nimport time\n\n\ndef fetch2(url):\n    return requests.get(url)\n",
        "app/jobs.py": "def do():\n    return 1\n\n\ndef sync():\n    do()\n",
        "deploy.sh": "#!/bin/sh\ncp a b\n",
        "app/http.py": "import requests\n\n\ndef fa(u):\n    return requests.get(u)\n\n\ndef fb(u):\n    return requests.get(u)\n\n\n"
                       "def fc(u):\n    return requests.get(u)\n",
        "app/tasks.py": "import requests\nfrom celery import Celery\n\napp = Celery()\n\n\n"
                        "@app.task(bind=True, autoretry_for=(Exception,), max_retries=5)\ndef send_invoice(self, id):\n"
                        "    requests.post('http://x', json={'id': id})\n",
        "app/layers.py": "import requests\nfrom tenacity import retry, stop_after_attempt\n\n\n"
                         "@retry(stop=stop_after_attempt(3))\ndef inner():\n    return requests.get('http://x')\n\n\n"
                         "@retry(stop=stop_after_attempt(4))\ndef outer():\n    return inner()\n",
        "app/auth.py": "def verify(t):\n    return t == 'x'\n\n\ndef check_token(t):\n    return verify(t)\n",
        "app/orders.py": "def place(o, queue):\n    o.save()\n    queue.publish(o)\n",
        "app/billing.py": "def charge(db, o):\n    db.execute(\"INSERT INTO charges VALUES (1)\")\n\n\n"
                          "def refund(db, o):\n    db.execute(\"INSERT INTO refunds VALUES (1)\")\n",
        "app/clock.py": "from datetime import datetime, timedelta\n\n\ndef expiry():\n    return datetime.now() + timedelta(hours=1)\n\n\n"
                        "def fmt(d):\n    return d.strftime('%Y')\n",
        "store/db.py": "def read(p):\n    f = open(p)\n    return f.read()\n\n\ndef write(p, d):\n    f = open(p, 'w')\n    f.write(d)\n",
        "ui/components/guard.py": "def check_ui(x):\n    return x\n",
        "tests/test_jobs.py": "def test_x():\n    assert True\n",
    }, "Initial", days=0)
    r.commit({"app/client.py": CLIENT1}, "Add retry to fetch", days=10)
    r.commit({"app/client_ok.py": "import requests\nimport time\nimport random\n\n\ndef fetch2(url):\n"
                                  "    for attempt in range(3):\n        try:\n            return requests.get(url)\n"
                                  "        except Exception:\n            time.sleep(2 ** attempt + random.random())\n"},
             "Add retry with backoff to fetch2", days=11)
    r.commit({"app/client_ok.py": "import requests\nimport time\nimport random\n\n\ndef fetch2(url):\n"
                                  "    for attempt in range(3):\n        try:\n            return requests.get(url)\n"
                                  "        except Exception:\n            time.sleep(2 ** attempt + random.random() + 1)\n"},
             "Fix fetch2 delay bug", days=40)
    r.commit({"app/client.py": CLIENT2}, "Fix retry storm bug in fetch: bound request", days=40)
    r.commit({"app/jobs.py": "def do():\n    return 1\n\n\ndef sync():\n    try:\n        do()\n    except Exception:\n"
                             "        pass\n",
              "deploy.sh": "#!/bin/sh\ncp a b || true\n",
              "tests/test_jobs.py": "def test_x():\n    try:\n        pass\n    except Exception:\n        pass\n"},
             "Ignore sync errors", days=5)
    r.commit({"app/jobs.py": "def do():\n    return 2\n\n\ndef sync():\n    try:\n        do()\n    except Exception as e:\n"
                             "        raise RuntimeError('sync failed') from e\n"},
             "Fix sync silently failing: error was swallowed and never reported", days=250)
    r.commit({"deploy.sh": "#!/bin/sh\ncp a b\n"}, "Fix deploy script ignoring copy errors", days=260)
    r.commit({"app/http.py": "import requests\n\n\ndef fa(u):\n    return requests.get(u, timeout=5)\n\n\ndef fb(u):\n"
                             "    return requests.get(u)\n\n\ndef fc(u):\n    return requests.get(u)\n"},
             "Fix hang in fa: add timeout to request", days=270)
    r.commit({"app/tasks.py": "import requests\nfrom celery import Celery\n\napp = Celery()\n\n\n"
                              "@app.task(bind=True, autoretry_for=(Exception,), max_retries=5)\ndef send_invoice(self, id):\n"
                              "    requests.post('http://x', json={'id': id, 'v': 2})\n"},
             "Fix invoice sent twice when task retried", days=280)
    r.commit({"app/auth.py": "def verify(t):\n    return t == 'x'\n\n\ndef check_token(t):\n    try:\n        return verify(t)\n"
                             "    except Exception:\n        return True\n",
              "ui/components/guard.py": "def check_ui(x):\n    try:\n        return x.y\n    except Exception:\n        return True\n"},
             "Handle verify errors gracefully", days=290)
    r.commit({"app/orders.py": "def place(o, queue, transaction):\n    o.save()\n    transaction.on_commit(lambda: queue.publish(o))\n"},
             "Fix ghost jobs: publish on_commit instead of inside the transaction", days=300)
    r.commit({"app/billing.py": "def charge(db, o):\n    db.execute(\"INSERT INTO charges VALUES (1) ON CONFLICT DO NOTHING\")\n\n\n"
                                "def refund(db, o):\n    db.execute(\"INSERT INTO refunds VALUES (1)\")\n"},
             "Fix duplicate charges when request replayed", days=310)
    for i, d in enumerate((320, 330)):
        r.commit({"app/clock.py": "from datetime import datetime, timedelta, timezone\n\n\ndef expiry():\n"
                                  f"    return datetime.now(timezone.utc) + timedelta(hours={i + 2})\n\n\n"
                                  "def fmt(d):\n    return d.strftime('%Y')\n"},
                 f"Fix timezone bug in expiry computation {i}", days=d)
    r.commit({"app/clock.py": "from datetime import datetime, timedelta, timezone\n\n\ndef expiry():\n"
                              "    return datetime.now(timezone.utc) + timedelta(hours=3)\n\n\n"
                              "def fmt(d):\n    return d.strftime('%Y-%m')\n"},
             "Fix timezone display format in fmt", days=335)
    r.commit({"store/db.py": "def read(p):\n    with open(p) as f:\n        return f.read()\n\n\n"
                             "def write(p, d):\n    f = open(p, 'w')\n    f.write(d)\n"},
             "Fix file descriptor leak in read", days=340)
    r.commit({"store/db.py": "def read(p):\n    with open(p) as f:\n        return f.read()\n\n\n"
                             "def write(p, d):\n    with open(p, 'w') as f:\n        f.write(d)\n\n\n"
                             "def tail(p):\n    g = open(p)\n    return g.read()\n"},
             "Fix leak: unclosed file in write", days=341)
    r.ingest()
    return r


def test_retry_without_backoff(code):
    hits = code.run("retry_without_backoff_jitter")
    syms = {u for h in hits for u in h.symbols}
    assert f"sym:{code.name}:app/client.py:fetch:function" in syms
    assert not any("client_ok" in u for u in syms)
    assert code.ctx.noise.get("retry_without_backoff_jitter/has_backoff", 0) >= 1


def test_swallow(code):
    hits = code.run("swallow_introduced_masks_later_fix")
    subs = [h.subject for h in hits]
    assert any(s.startswith(f"sym:{code.name}:app/jobs.py:sync") for s in subs)
    assert any(s.startswith("deploy.sh|") for s in subs)
    assert not any("tests/" in p for h in hits for p in h.paths)


def test_timeout_siblings(code):
    hits = code.run("timeout_added_after_hang_siblings_unbounded")
    assert len(hits) == 1 and hits[0].subject.startswith("requests.get|")
    assert hits[0].evidence["unbounded_siblings"] == ["app/http.py:8", "app/http.py:11"] or \
        len(hits[0].evidence["unbounded_siblings"]) >= 2


def test_retry_non_idempotent(code):
    hits = code.run("retry_on_non_idempotent_write")
    assert [h.symbols[0] for h in hits] == [f"sym:{code.name}:app/tasks.py:send_invoice:function"]
    assert hits[0].evidence["broad_trigger"] and hits[0].evidence["duplicate_worded_commits"]


def test_layered_retry(code):
    hits = code.run("layered_retry_amplification")
    assert any(h.subject.endswith("outer>" + f"sym:{code.name}:app/layers.py:inner:function") or
               "outer" in h.subject for h in hits)


def test_fail_open(code):
    hits = code.run("fail_open_fallback_added")
    assert [h.paths for h in hits] == [["app/auth.py"]]
    assert code.ctx.noise.get("fail_open_fallback_added/ui_default") == 1


def test_external_call(code):
    hits = code.run("external_call_inside_transaction")
    assert len(hits) == 1 and hits[0].paths == ["app/orders.py"]


def test_duplicate_idempotency(code):
    hits = code.run("duplicate_effect_idempotency_fix")
    assert len(hits) == 1
    assert [s["symbol"] for s in hits[0].evidence["sibling_writers_without_dedupe"]] == ["refund"]


def test_time_bug(code):
    hits = code.run("time_bug_hotspot")
    assert [h.subject for h in hits] == ["app/clock.py"]


def test_resource_leak(code):
    hits = code.run("resource_leak_fix_lineage")
    assert [h.subject for h in hits] == ["store"]
    assert hits[0].evidence["head_unguarded_opens"]


# ====================================================================== repo C: remaining builders on a tiny library repo
def test_negative_library_repo_and_empty(mini):
    r = mini()
    r.commit({"lib/a.py": "def f():\n    return 1\n"}, "Add f", days=0)
    r.commit({"lib/a.py": "def f():\n    return 2\n"}, "Fix f", days=3)
    r.ingest()
    assert r.run("migration_on_boot_placement_change") == []
    assert r.ctx.noise.get("migration_on_boot_placement_change/library_repo") == 1
    for cid in ("graceful_shutdown_signal_drift", "post_release_hotfix_cluster", "embedded_expiry_literal",
                "layered_retry_amplification", "max_requests_leak_mask_and_herd"):
        assert r.run(cid) == []


# ====================================================================== upgraded builders (symbol / blame / tags data)
def _fresh(mini):
    return mini("opsup")


def test_external_call_requires_same_function(mini):
    r = _fresh(mini)
    r.commit({"app/mix.py": "def a(q):\n    q.publish(1)\n    return 1\n\n\ndef b(t):\n    t.save()\n    return 2\n"},
             "Add mix", days=0)
    r.commit({"app/mix.py": "def a(q):\n    return 1\n\n\ndef b(t):\n    t.save()\n    t.on_commit(lambda: 1)\n    return 2\n"},
             "Fix ghost jobs: use on_commit", days=3)
    r.ingest()
    assert r.run("external_call_inside_transaction") == []      # same file, different functions

    r2 = _fresh(mini)
    r2.commit({"app/mix.py": "def a(q, t):\n    q.publish(1)\n    return 1\n"}, "Add mix", days=0)
    r2.commit({"app/mix.py": "def a(q, t):\n    t.on_commit(lambda: q.publish(1))\n    return 1\n"},
              "Fix ghost jobs: use on_commit", days=3)
    r2.ingest()
    hits = r2.run("external_call_inside_transaction")
    assert len(hits) == 1 and len(hits[0].symbols) == 1


def test_protective_control_uses_blame_origin(mini):
    r = _fresh(mini)
    r.commit({"app/net.py": "def a():\n    timeout = 5\n    return timeout\n"}, "Add a", days=0)
    r.commit({"app/net.py": "def a():\n    timeout = 5\n    return timeout\n\n\ndef b():\n    timeout = 5\n    return timeout\n"},
             "Fix hang in b: add timeout", days=5)
    r.commit({"app/net.py": "def a():\n    return 5\n\n\ndef b():\n    timeout = 5\n    return timeout\n"},
             "Fix a: drop timeout", days=9)
    r.ingest()
    assert r.ctx.line_origin  # blame table available
    hits = r.run("protective_control_removed")
    # the removed line in a() was written by "Add a" (not a fix); the identical-line proxy would wrongly pick b()'s fix
    assert hits == []

    r2 = _fresh(mini)
    r2.commit({"app/net.py": "def a():\n    return 5\n"}, "Add a", days=0)
    r2.commit({"app/net.py": "def a():\n    timeout = 5\n    return timeout\n"}, "Fix hang in a: add timeout", days=5)
    r2.commit({"app/net.py": "def a():\n    return 5\n"}, "Fix a: drop timeout", days=9)
    r2.ingest()
    hits = r2.run("protective_control_removed")
    assert len(hits) == 1 and hits[0].evidence["origin_source"] == "blame" and len(hits[0].symbols) == 1


def test_tags_come_from_enrichment_table(mini):
    r = _fresh(mini)
    r.commit({"app/x.py": "def f():\n    return 0\n"}, "Init", days=0)
    r.git("tag", "v0.9.0", days=0)
    r.commit({"app/x.py": "def f():\n    return 1\n"}, "Add feature", days=10)
    r.git("tag", "v1.0.0", days=10)
    for i in range(3):
        r.commit({"app/x.py": f"def f():\n    return {i + 2}\n"}, f"Fix crash {i} after release", days=11 + i)
    r.ingest()
    r.git("tag", "-d", "v1.0.0")     # refs gone; the enrichment table must still drive the builder
    hits = r.run("post_release_hotfix_cluster")
    assert any(h.evidence["release"] == "v1.0.0" and len(h.evidence["fixes"]) == 3 for h in hits)


def test_fail_open_needs_handler_in_security_symbol(mini):
    r = _fresh(mini)
    r.commit({"app/misc.py": "def verify_x(a):\n    return a\n\n\ndef process(x):\n    return x\n"}, "Add misc", days=0)
    r.commit({"app/misc.py": "def verify_x(a):\n    return a + 1\n\n\ndef process(x):\n    try:\n        return x.y\n"
                             "    except Exception:\n        return True\n"}, "Tweak", days=3)
    r.ingest()
    assert r.run("fail_open_fallback_added") == []   # handler is in process(); verify_x merely shares the commit


def test_idempotency_and_time_symbols_are_exact(code):
    dup = code.run("duplicate_effect_idempotency_fix")[0]
    assert [code.ctx.symbols[u]["qualname"] for u in dup.symbols if u in code.ctx.symbols][:1] == ["charge"]
    tb = code.run("time_bug_hotspot")[0]
    names = {code.ctx.symbols[u]["qualname"] for u in tb.symbols}
    assert "expiry" in names and "fmt" not in names


def test_retry_symbol_is_the_one_with_the_retry_line(mini):
    r = _fresh(mini)
    body = ("import requests\n\n\ndef a(u):\n    for attempt in range(3):\n        try:\n            return requests.get(u)\n"
            "        except Exception:\n            pass\n\n\ndef b(u):\n    return requests.get(u)  # v{n}\n")
    r.commit({"app/c.py": body.format(n=0)}, "Add a with retry", days=0)
    r.commit({"app/c.py": body.format(n=1)}, "Fix b request", days=5)
    r.ingest()
    for h in r.run("retry_without_backoff_jitter"):
        assert h.subject.split("|")[1] == r.ctx.commits[0]["hash"][:12]   # only the commit that added the retry


# ---------------------------------------------------------------------------------------------- round-2 enrichment
def _fresh_ctx(r):
    from afd_oracle_ingest.catalog.base import Ctx
    r.ctx.store.conn.commit()
    return Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)


def _run_on(ctx, cid):
    from afd_oracle_ingest.catalog import load_all
    ctx._current = cid
    return list(load_all()[cid](ctx))


def test_external_call_lists_unfixed_head_siblings(mini):
    r = mini()
    body = ("def {n}(order, queue, transaction):\n    with transaction.atomic():\n        order.save()\n"
            "        queue.publish(order)\n")
    r.commit({"app/orders.py": body.format(n="create") + "\n\n" + body.format(n="cancel"),
              "app/safe.py": "def done(order, queue, transaction):\n    with transaction.atomic():\n        order.save()\n"
                             "        transaction.on_commit(lambda: queue.publish(order))\n"}, "Add orders", days=0)
    r.commit({"app/orders.py": "def create(order, queue, transaction):\n    with transaction.atomic():\n        order.save()\n"
                               "        transaction.on_commit(lambda: queue.publish(order))\n\n\n" + body.format(n="cancel")},
             "Fix ghost jobs: publish create() on_commit", days=3)
    hits = r.run("external_call_inside_transaction")
    assert len(hits) == 1
    sibs = hits[0].evidence["unfixed_head_siblings"]
    assert [u.split(":")[3] for u in sibs] == ["cancel"] and hits[0].evidence["unfixed_head_sibling_count"] == 1
    assert not any(u.endswith(":create:function") or ":done:" in u for u in sibs)


def test_keepalive_uses_linked_pr_text(mini):
    r = mini()
    r.commit({"conf/nginx.conf": "keepalive_timeout 75;\n"}, "Init", days=0)
    for i in range(8):
        r.commit({f"app/f{i}.py": "x = 1\n"}, f"Work {i}", days=1 + i)
    sha = r.commit({"conf/nginx.conf": "keepalive_timeout 120;\n"}, "Tune upstream timeout", days=12)
    r.ingest()
    assert r.run("app_keepalive_502_fix_lineage") == []
    conn = r.ctx.store.conn
    conn.execute("INSERT INTO discussions (id, kind, number, title, body, state) VALUES "
                 "('pr9', 'pr', 9, 'Stop 502 Bad Gateway on idle connections', 'ALB drops them', 'merged')")
    conn.execute("INSERT INTO discussion_commits VALUES (9, ?)", (sha,))
    hits = _run_on(_fresh_ctx(r), "app_keepalive_502_fix_lineage")
    assert len(hits) == 1 and hits[0].subject == sha
    assert hits[0].evidence["symptom_prs"][0]["number"] == 9 and hits[0].evidence["symptom_commits"] == []


def test_release_chain_uses_github_releases(mini):
    r = mini()
    wf = ".github/workflows/release.yml"
    r.commit({wf: "name: release\non: push\n"}, "Add release workflow", days=0)
    for i, d in enumerate((10.0, 10.1, 10.2)):
        r.commit({wf: f"name: release\non: push\n# fix {i}\n"}, f"Fix release workflow {i}", days=d)
    r.git("tag", "v1.0.1", days=10.2)
    r.ingest()
    base = r.run("release_workflow_edit_fix_chain")
    assert len(base) == 1 and base[0].evidence["github_release_anomalies"] == []
    conn = r.ctx.store.conn
    conn.executemany("INSERT INTO github_releases VALUES (?,?,?,?,?,?)", [
        ("v1.0.0", "v1.0.0", "2023-01-12T10:30:00Z", "2023-01-12T10:30:00Z", 0, 0),   # tag deleted afterwards
        ("v1.0.1", "v1.0.1", "2023-01-12T09:00:00Z", "2023-01-12T09:00:00Z", 0, 0),   # tag moved to a later commit
        ("v0.1.0", "v0.1.0", "2023-01-02T10:00:00Z", "2023-01-02T10:00:00Z", 0, 0)])  # outside the window
    hits = _run_on(_fresh_ctx(r), "release_workflow_edit_fix_chain")
    assert len(hits) == 1
    anomalies = {a["tag"]: a["problem"] for a in hits[0].evidence["github_release_anomalies"]}
    assert anomalies == {"v1.0.0": "tag_deleted_after_release", "v1.0.1": "tag_commit_newer_than_release"}
    assert hits[0].score > base[0].score
