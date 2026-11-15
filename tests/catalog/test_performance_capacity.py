"""Tests for the performance-capacity catalogue family."""
import pytest

CORE = ("def lookup(x):\n    return x\n\n\ndef a():\n    return lookup(1)\n\n\ndef b():\n    return lookup(2)\n\n\n"
        "def c():\n    return lookup(3)\n")
CORE2 = CORE.replace("    return x\n", "    r = requests.get('http://svc/' + str(x))\n    return r\n", 1)


@pytest.fixture
def perf(mini):
    r = mini()
    s = {}
    r.commit({"config.py": "REQUEST_TIMEOUT = 5\n", "app/views.py": "def orders():\n    qs = Order.objects.all()\n    return qs\n",
              "app/core.py": CORE, "app/cache.py": "_cache = {}\n\n\ndef get(k):\n    return _cache.get(k)\n",
              "app/export.py": "def rows():\n    return Report.objects.all()\n",
              "app/enums.py": "def countries():\n    return Country.objects.all()\n",
              "deploy/app.yaml": "resources:\n  limits:\n    memory: 256Mi\n"}, "Initial", days=0)
    s["t1"] = r.commit({"config.py": "REQUEST_TIMEOUT = 30\n"}, "Fix timeouts under load", days=1)
    s["t2"] = r.commit({"config.py": "REQUEST_TIMEOUT = 5\n"}, "Reduce timeout to avoid slow worker hang", days=2)
    s["t3"] = r.commit({"config.py": "REQUEST_TIMEOUT = 60\n"}, "Raise timeout after outage", days=3)
    s["n1"] = r.commit({"app/views.py": "def orders():\n    qs = Order.objects.select_related('user')\n    return qs\n"},
                       "Fix N+1 queries in orders", days=4)
    s["n2"] = r.commit({"app/views.py": "def orders():\n    qs = Order.objects.all()\n    return qs\n"},
                       "Simplify queryset", days=5)
    s["hot"] = r.commit({"app/core.py": CORE2}, "Look up from service", days=6)
    s["hotfix"] = r.commit({"app/core.py": CORE2.replace("return r", "return r  # cached")},
                           "Cache lookup to cut latency", days=20)
    s["leak"] = r.commit({"app/cache.py": "from functools import lru_cache\n\n\n@lru_cache(maxsize=256)\ndef get(k):\n    return k\n",
                          "deploy/app.yaml": "resources:\n  limits:\n    memory: 512Mi\n"},
                         "Fix memory leak: bound the cache", days=25)
    s["page"] = r.commit({"app/export.py": "def rows():\n    return Report.objects.all().iterator(chunk_size=500)\n"},
                         "Fix OOM on large exports: stream in chunks", days=26)
    r.commit({"app/enums.py": "def countries():\n    return Country.objects.all().iterator(chunk_size=500)\n"},
             "Fix slow country list: iterate in chunks", days=27)
    r.ingest()
    r.s = s
    return r


def test_knob_churn(perf):
    hits = perf.run("tunable_knob_churn")
    hit = [h for h in hits if h.subject == "config.py|request_timeout"]
    assert len(hit) == 1
    hits = hit
    assert hits[0].evidence["flip_back"] and perf.s["t3"] in hits[0].commits


def test_n_plus_one(perf):
    hits = perf.run("n_plus_one_fix_regression")
    assert [h.commits[0] for h in hits] == [perf.s["n1"]] and perf.s["n2"] in hits[0].commits


def test_hot_path(perf):
    hits = perf.run("hot_path_blocking_call_added")
    assert [h.commits[0] for h in hits] == [perf.s["hot"]] and perf.s["hotfix"] in hits[0].commits
    assert hits[0].symbols[0].endswith(":lookup:function")


def test_cache_fix(perf):
    hits = perf.run("cache_unbounded_growth_fix")
    assert [h.commits[0] for h in hits] == [perf.s["leak"]]
    assert hits[0].paths == ["app/cache.py"]


def test_pagination_fix(perf):
    hits = perf.run("unbounded_query_pagination_fix")
    assert [h.commits[0] for h in hits] == [perf.s["page"]]
    assert perf.ctx.noise["unbounded_query_pagination_fix/enum_sized_table"] == 1


SVC = ("def lookup(x):\n    return x\n\n\ndef other(x):\n    return x\n\n\ndef a():\n    return lookup(1)\n\n\n"
       "def b():\n    return lookup(2)\n\n\ndef c():\n    return lookup(3)\n")


def test_n_plus_one_same_function(mini):
    """The eager load must regress in the same function, not merely the same file."""
    r = mini()
    r.commit({"app/q.py": "def orders():\n    return Order.objects.all()\n\n\ndef users():\n    return User.objects.all()\n"},
             "Initial", days=0)
    r.commit({"app/q.py": "def orders():\n    return Order.objects.select_related('user')\n\n\ndef users():\n"
                          "    return User.objects.all()\n"}, "Fix N+1 queries in orders", days=1)
    c2 = r.commit({"app/q.py": "def orders():\n    return Order.objects.select_related('user')\n\n\ndef users():\n"
                               "    return User.objects.prefetch_related('groups')\n"}, "Add users groups", days=2)
    r.commit({"app/q.py": "def orders():\n    return Order.objects.select_related('user')\n\n\ndef users():\n"
                          "    return User.objects.all()\n"}, "Drop users join", days=3)
    hits = r.run("n_plus_one_fix_regression")
    assert [h.commits[0] for h in hits] == [c2]
    assert hits[0].evidence["scope"] == "symbol" and hits[0].symbols[0].endswith(":users:function")


def test_hot_path_exact_symbol(mini):
    """A blocking call added to a non-hot function is not charged to a hot symbol edited in the same commit."""
    r = mini()
    r.commit({"app/svc.py": SVC}, "Initial", days=0)
    r.commit({"app/svc.py": SVC.replace("def other(x):\n    return x", "def other(x):\n    return requests.get(x)")
              .replace("def lookup(x):\n    return x", "def lookup(x):\n    return x + 0")}, "Tweak svc", days=1)
    r.commit({"app/svc.py": SVC.replace("def lookup(x):\n    return x", "def lookup(x):\n    return x + 1")},
             "Speed up lookup latency", days=5)
    assert r.run("hot_path_blocking_call_added") == []
    assert r.ctx.noise["hot_path_blocking_call_added/call_not_in_hot_symbol"] == 1
    r2 = mini()
    r2.commit({"app/svc.py": SVC}, "Initial", days=0)
    sha = r2.commit({"app/svc.py": SVC.replace("def lookup(x):\n    return x", "def lookup(x):\n    return requests.get(x)")},
                    "Fetch in lookup", days=1)
    r2.commit({"app/svc.py": SVC.replace("def lookup(x):\n    return x", "def lookup(x):\n    return cached(x)")},
              "Cache lookup to cut latency", days=5)
    hits = r2.run("hot_path_blocking_call_added")
    assert [h.commits[0] for h in hits] == [sha] and hits[0].evidence["attribution"] == "symbol"


def test_pagination_exact_symbol(perf):
    hits = perf.run("unbounded_query_pagination_fix")
    assert hits[0].symbols and hits[0].symbols[0].endswith(":rows:function")
    assert hits[0].evidence["bound_in_query_symbol"] is True
