"""Concurrency-family builders: locks, in-process schedulers, ordering, worker counts, import-time state, preload."""
from __future__ import annotations

from .test_history_mining import Plan

CACHE0 = "class Cache:\n    def __init__(self):\n        self.data = {}\n\n    def get(self, key):\n        return self.data.get(key)\n"
CACHE1 = ("import threading\n\n\nclass Cache:\n    def __init__(self):\n        self.data = {}\n"
          "        self._lock = threading.Lock()\n\n    def get(self, key):\n        with self._lock:\n"
          "            return self.data.get(key)\n")

SIGN0 = ("import hashlib\n\n\ndef sign_params(params):\n"
         "    payload = \"&\".join(f\"{k}={v}\" for k, v in params.items())\n"
         "    return hashlib.sha256(payload.encode()).hexdigest()\n\n\ndef cache_key(params):\n"
         "    joined = \",\".join(str(v) for v in params.values())\n"
         "    return hashlib.md5(joined.encode()).hexdigest()\n")
SIGN1 = SIGN0.replace("for k, v in params.items())", "for k, v in sorted(params.items()))")

BOOT0 = ("import redis\n\nclient = redis.Redis(host=\"localhost\")\n\n\ndef handle():\n    return client.get(\"x\")\n")
BOOT1 = ("import redis\n\n\ndef get_client():\n    client = redis.Redis(host=\"localhost\")\n    return client\n\n\n"
         "def handle():\n    return get_client().get(\"x\")\n")

HANDLERS = ("_sessions = {}\nROUTES = {}\nROUTES[\"home\"] = 1\n\n\ndef login(user, data):\n"
            "    _sessions[user] = data\n    return _sessions[user]\n\n\ndef logout(user):\n    _sessions.pop(user, None)\n")


def test_concurrency_family(mini):
    r = mini()
    p = Plan(r)
    p.add("init", 0, {"svc/cache.py": CACHE0, "svc/sign.py": SIGN0, "svc/boot.py": BOOT0,
                      "svc/handlers.py": HANDLERS, "svc/db.py": "from sqlalchemy import create_engine\n\n"
                                                                "engine = create_engine(\"sqlite://\")\n",
                      "svc/fl.py": "import filelock\n\nfcntl.flock(fd, a)\n",
                      "gunicorn.conf.py": "workers = 1\nbind = \"0.0.0.0:8000\"\n"}, "Initial import")
    p.add("lockfix", 5, {"svc/cache.py": CACHE1}, "Fix race condition in cache get with a lock")
    p.add("fl1", 6, {"svc/fl.py": "import filelock\n\nfcntl.flock(fd, b)\n"}, "Tweak file lock")
    p.add("fl2", 7, {"svc/fl.py": "import filelock\n\nfcntl.flock(fd, c)\n"}, "Tweak file lock again")
    p.add("fl3", 8, {"svc/fl.py": "import filelock\n\nfcntl.flock(fd, d)\n"}, "Tweak file lock thrice")
    p.add("lockrm", 20, {"svc/cache.py": CACHE0}, "Simplify cache")
    p.add("sched", 10, {"svc/jobs.py": "from apscheduler.schedulers.background import BackgroundScheduler\n\n"
                                       "scheduler = BackgroundScheduler()\n\n\ndef send_digest_email():\n"
                                       "    requests.post(WEBHOOK, json={})\n\n\n"
                                       "scheduler.add_job(send_digest_email, \"interval\", hours=1)\n"},
          "Add hourly digest job")
    p.add("sched2", 12, {"svc/cachejobs.py": "from apscheduler.schedulers.background import BackgroundScheduler\n\n"
                                            "sched = BackgroundScheduler()\nsched.add_job(refresh_cache, \"interval\", minutes=5)\n"},
          "Add cache refresh job")
    p.add("guard", 14, {"svc/jobs.py": "from apscheduler.schedulers.background import BackgroundScheduler\n\n"
                                       "scheduler = BackgroundScheduler()\n\n\ndef send_digest_email():\n"
                                       "    if not try_advisory_lock(conn, 42):\n        return\n"
                                       "    requests.post(WEBHOOK, json={})\n\n\n"
                                       "scheduler.add_job(send_digest_email, \"interval\", hours=1)\n"},
          "Guard digest job with advisory lock to avoid duplicate emails")
    p.add("signfix", 21, {"svc/sign.py": SIGN1}, "Fix nondeterministic signature by sorting params")
    p.add("boot_fix", 25, {"svc/boot.py": BOOT1}, "Fix fork crash: create redis client lazily at startup")
    p.add("workers", 30, {"gunicorn.conf.py": "workers = 4\nbind = \"0.0.0.0:8000\"\n"}, "Run four gunicorn workers")
    p.add("preload", 40, {"gunicorn.conf.py": "workers = 4\npreload_app = True\nbind = \"0.0.0.0:8000\"\n"},
          "Enable preload to cut memory")
    p.add("forkfix", 45, {"gunicorn.conf.py": "workers = 4\npreload_app = True\nbind = \"0.0.0.0:8000\"\n\n\n"
                                               "def post_fork(server, worker):\n    engine.dispose()\n"},
          "Fix stale DB connections after fork")
    h = p.run()
    r.ingest()

    hits = r.run("lock_churn_concurrency_scars")
    assert {x.subject.split(":")[-2] for x in hits} == {"Cache.__init__", "Cache.get"}  # per symbol, not per file
    assert all(x.paths[0] == "svc/cache.py" and x.evidence["scope"] == "symbol" for x in hits)
    assert all(x.evidence["reason"] == "added_by_race_fix_then_removed" for x in hits)
    assert all(h["lockfix"] in x.commits and h["lockrm"] in x.commits for x in hits)
    assert r.ctx.noise.get("lock_churn_concurrency_scars/file_lock_helper", 0) >= 3

    hits = r.run("inprocess_scheduler_multi_instance")
    assert [x.subject.split("|")[0] for x in hits] == ["svc/jobs.py"]
    assert hits[0].evidence["side_effecting_job"] and h["guard"] in hits[0].commits
    assert r.ctx.noise.get("inprocess_scheduler_multi_instance/idempotent_job") == 1

    hits = r.run("nondeterministic_iteration_fix_siblings")
    assert len(hits) == 1 and hits[0].commits == [h["signfix"]]
    assert hits[0].symbols[1] == f"sym:{r.name}:svc/sign.py:cache_key:function"

    hits = r.run("worker_count_vs_process_local_state")
    hit = next(x for x in hits if x.commits == [h["workers"]])
    names = [c["name"] for c in hit.evidence["process_local_containers"]]
    assert "_sessions" in names and "ROUTES" not in names

    hits = r.run("import_time_side_effect_lineage")
    hit = next(x for x in hits if x.paths[0] == "svc/boot.py")
    assert hit.commits == [h["boot_fix"]] and hit.evidence["moved_into_function"]

    hits = r.run("preload_app_fork_unsafe_init")
    assert {x.commits[0] for x in hits} == {h["preload"], h["forkfix"]}
    assert any(res["path"] == "svc/db.py" for res in hits[0].evidence["import_time_resources_at_head"])


def test_lock_churn_is_per_symbol(mini):
    r = mini()
    p = Plan(r)
    mod = ("import threading\n\nlock = threading.Lock()\n\n\ndef hot(v):\n    with lock:\n        return v + {a}\n\n\n"
           "def calm(v):\n    with lock:\n        return v - {b}\n")
    p.add("init", 0, {"svc/mix.py": mod.format(a=0, b=0)}, "Initial import")
    for i in range(1, 4):
        p.add(f"hot{i}", i * 5, {"svc/mix.py": mod.format(a=i, b=0).replace("with lock:", f"with lock:  # try {i}", 1)},
              f"Tune hot path {i}")
    p.add("calm", 40, {"svc/mix.py": mod.format(a=3, b=1).replace("with lock:", "with lock:  # try 3", 1)}, "Tune calm path")
    p.run()
    r.ingest()
    hits = r.run("lock_churn_concurrency_scars")
    assert [x.subject for x in hits] == [f"sym:{r.name}:svc/mix.py:hot:function"]
    assert hits[0].evidence["commits_touching_locks"] >= 3
