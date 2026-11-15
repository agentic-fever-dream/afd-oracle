"""Observability-family catalogue builders on one planted repo."""
import pytest

_REPO = {}

RULES0 = """groups:
- name: api
  rules:
  - alert: HighErrorRate
    expr: rate(errors[5m]) > 0.05
    for: 5m
    labels:
      severity: critical
    annotations:
      description: payment gateway timeout, will retry spike
"""
RULES1 = RULES0.replace("> 0.05", "> 0.2").replace("for: 5m", "for: 30m").replace("severity: critical", "severity: warning")
WORKER0 = ('def pay(log):\n    log.error("payment gateway timeout, will retry")\n'
           '    log.error("cache connection dropped unexpectedly")\n')
METRICS0 = 'REQS = Counter("reqs_total", "Requests", ["route", "status"])\n'


def _build(mini):
    r = mini()
    c = {}
    r.commit({"app/worker.py": WORKER0, "scripts/cli.py": 'def go(log):\n    log.error("cli failed to start up")\n',
              "alerts/rules.yml": RULES0, "alerts/disk.yml": "groups:\n- rules:\n  - alert: DiskFull\n    expr: disk_used > 0.9\n",
              "app/metrics.py": METRICS0, "config/prod/logging.yaml": "log_level: info\n"}, "Initial", days=0)
    c["log"] = r.commit({"app/worker.py": WORKER0.replace('log.error("payment', 'log.warning("payment')},
                        "Quiet noisy log", days=10)
    r.commit({"scripts/cli.py": 'def go(log):\n    log.info("cli failed to start up")\n'}, "cli quieter", days=11)
    c["cfg"] = r.commit({"config/prod/logging.yaml": "log_level: debug\n"}, "Debug prod", days=12)
    c["alert"] = r.commit({"alerts/rules.yml": RULES1}, "Tune alert thresholds", days=20)
    c["rm"] = r.commit({"alerts/disk.yml": "groups: []\n"}, "Drop disk alert", days=21)
    c["relabel"] = r.commit({"alerts/rules.yml": RULES1.replace("severity: warning", "severity: warning\n      team: api")},
                            "Add team label", days=22)
    r.commit({"app/hotfix.py": "x = 1\n"}, "Hotfix outage: HighErrorRate never paged", days=40)
    c["card"] = r.commit({"app/metrics.py": METRICS0.replace('"status"]', '"status", "user_id"]')}, "Per-user metrics", days=30)
    c["enum"] = r.commit({"app/metrics.py": METRICS0.replace('"status"]', '"status", "user_id", "tenant_tier"]')},
                         "Tier label", days=31)
    c["fix"] = r.commit({"app/other.py": "y = 1\n"}, "Drop labels to fix cardinality blowup", days=35)
    r.c = c
    r.ingest()
    return r


@pytest.fixture
def repo(mini):
    if "r" not in _REPO:
        _REPO["r"] = _build(mini)
    return _REPO["r"]


def test_log_verbosity_flip(repo):
    hits = repo.run("log_verbosity_flip")
    msg = [h for h in hits if h.evidence["variant"] == "message_demoted"]
    assert len(msg) == 1 and msg[0].commits == [repo.c["log"]]
    assert (msg[0].evidence["old_level"], msg[0].evidence["new_level"]) == ("error", "warning")
    assert repo.ctx.noise["log_verbosity_flip/cli_or_dev_path"] >= 1
    cfg = [h for h in hits if h.evidence["variant"] == "config_level_changes"]
    assert len(cfg) == 1 and cfg[0].evidence["debug_in_prod_path"] and repo.c["cfg"] in cfg[0].commits


def test_alert_or_slo_loosened(repo):
    hits = repo.run("alert_or_slo_loosened")
    by_commit = {h.subject.split("|")[0]: h for h in hits}
    a = by_commit[repo.c["alert"]]
    assert len(a.evidence["changes"]) == 3
    assert a.evidence["incident_commits_within_90d"]
    assert "alert removed" in by_commit[repo.c["rm"]].evidence["changes"][0]
    assert repo.c["relabel"] not in by_commit
    assert repo.ctx.noise["alert_or_slo_loosened/label_or_rename_only"] >= 1


def test_metric_label_cardinality(repo):
    hits = repo.run("metric_label_cardinality")
    labelled = [h for h in hits if not h.subject.startswith("fix|")]
    assert len(labelled) == 1 and labelled[0].evidence["labels"] == ["user_id"]
    assert labelled[0].commits == [repo.c["card"]]
    assert repo.ctx.noise["metric_label_cardinality/enum_like_label"] == 1
    assert [h.subject for h in hits if h.subject.startswith("fix|")] == ["fix|" + repo.c["fix"]]
