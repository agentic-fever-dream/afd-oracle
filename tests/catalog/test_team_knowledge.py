"""Tests for the team-knowledge catalogue builders."""


def _plant(r):
    r.commit({"app/order.py": "def setup():\n    # DO NOT REMOVE: order matters here\n    init_db()\n    init_cache()\n",
              "app/enums.py": "# keep in sync with app/schema.py\nCOLORS = ['red', 'green']\n",
              "app/schema.py": "SCHEMA = {'kind': 'enum', 'values': ['red', 'green']}\n",
              "app/dangling.py": "# must match app/missing_partner.py\nX = 1\n",
              "app/calc.py": "# see app/old_rates.py for the formula\ndef rate(x):\n    return x\n",
              "app/old_rates.py": "RATES = [1, 2]\n",
              "app/magic.py": "def weird(x):\n    # not sure why this works, don't touch\n    return x ^ 1\n\n\n"
                              "def a():\n    return weird(1)\n\n\ndef b():\n    return weird(2)\n\n\n"
                              "def c():\n    return weird(3)\n",
              "app/jobs.py": "def legacy_cleanup_job():\n    return 1\n",
              "scripts/old_deploy.sh": "echo deploy\n"}, "Initial layout", days=0, author="bob")
    r.commit({"docs/runbook.md": "# Runbook\n\nRun `scripts/old_deploy.sh` then `legacy_cleanup_job`.\n"},
             "Add runbook", days=10)
    r.commit({"app/jobs.py": "def legacy_cleanup_job():\n    return 2\n"}, "Touch job", days=40)
    r.commit({"scripts/old_deploy.sh": None}, "Remove old deploy script", days=50)
    r.commit({"app/jobs.py": None}, "Remove legacy job", days=60)
    r.commit({"app/old_rates.py": None}, "Remove old rates", days=65)
    r.commit({"app/order.py": "def setup():\n    # DO NOT REMOVE: order matters here\n    init_db(retry=True)\n"
                              "    init_cache()\n"}, "Fix startup crash by retrying init", days=70)
    for k in range(3):
        r.commit({"app/enums.py": "# keep in sync with app/schema.py\nCOLORS = ['red', 'green'" + ", 'blue'" * (k + 1) + "]\n"},
                 f"Add color variant {k}", days=80 + k * 10)
    r.commit({"app/net.py": "def connect():\n    retries = 3\n    delay = 0.5\n    session = open_session(retries, delay)\n"
                            "    return session\n"},
             "Add retry workaround: do not remove, otherwise upstream breaks on startup", days=120)
    return r.ingest()


def test_group(mini):
    r = mini()
    _plant(r)

    hits = r.run("chesterton_fence_comment_edited")
    top = max(hits, key=lambda h: h.score)
    assert top.paths == ["app/order.py"] and top.score >= 5

    hits = r.run("commit_message_signal_hack_workaround")
    assert [h.paths for h in hits] == [["app/net.py"]]

    hits = r.run("keep_in_sync_partner_unchanged")
    by = {h.subject.split("|")[0]: h for h in hits}
    assert by["app/enums.py"].evidence["partner"] == "app/schema.py"
    assert by["app/dangling.py"].evidence["state"] == "dangling"

    r.commit({"README.md": "x\n"}, "Docs", days=700)
    r.ingest()

    hits = r.run("confessed_ignorance_comment_live_code")
    assert len(hits) == 1 and hits[0].paths == ["app/magic.py"] and hits[0].evidence["fan_in"] >= 3

    hits = r.run("doc_identifier_orphan")
    assert [h.subject for h in hits] == ["docs/runbook.md"]
    assert {o["token"] for o in hits[0].evidence["orphans"]} == {"scripts/old_deploy.sh", "legacy_cleanup_job"}

    hits = r.run("comment_reference_decay")
    assert len(hits) == 1 and hits[0].paths == ["app/calc.py"] and hits[0].evidence["status"] == "deleted"


def test_fence_documented_removal_dropped(mini):
    r = mini()
    r.commit({"app/order.py": "def setup():\n    # do not remove this: order matters\n    init_db()\n"}, "Add", days=0)
    r.commit({"app/order.py": "def setup():\n    init_db2()\n"}, "Rewrite setup", days=20)
    hits = r.run("chesterton_fence_comment_edited")
    assert hits == []
    assert r.ctx.noise.get("chesterton_fence_comment_edited/documented_removal") == 1


def test_sync_partner_moving_with_host_dropped(mini):
    r = mini()
    r.commit({"app/a.py": "# keep in sync with app/b.py\nX = 0\n", "app/b.py": "Y = 0\n"}, "Add", days=0)
    for k in range(1, 5):
        r.commit({"app/a.py": f"# keep in sync with app/b.py\nX = {k}\n", "app/b.py": f"Y = {k}\n"}, f"Bump {k}", days=k * 30)
    assert r.run("keep_in_sync_partner_unchanged") == []
    assert r.ctx.noise.get("keep_in_sync_partner_unchanged/partner_moves_with_host") == 1


def test_signal_commit_bare_issue_and_bot_dropped(mini):
    r = mini()
    r.commit({"app/net.py": "a = 1111111\nb = 2222222\nc = 3333333\nd = 4444444\n"}, "workaround #123", days=0)
    r.commit({"app/net2.py": "a = 1111111\nb = 2222222\nc = 3333333\nd = 4444444\n"}, "hack: do not remove #12", days=1,
             author="bot")
    assert r.run("commit_message_signal_hack_workaround") == []


def test_orphaned_ownership_legacy_core(mini):
    r = mini()
    r.commit({"core/engine.py": "V = 0\n"}, "Create the engine module skeleton", days=0, author="alice")
    for k in range(1, 23):
        r.commit({"core/engine.py": f"V = {k}\n"}, f"Tune engine parameter set number {k}", days=k * 10, author="bob")
    r.commit({"core/engine.py": "V = 100\n"}, "Fix crash in engine when V overflows", days=700, author="alice")
    r.commit({"README.md": "x\n"}, "Docs", days=720, author="alice")
    hits = r.run("orphaned_ownership_legacy_core")
    assert [h.subject for h in hits] == ["core/engine.py"] and hits[0].evidence["owner_share"] >= 0.9


def test_orphaned_ownership_small_file_dropped(mini):
    r = mini()
    r.commit({"core/engine.py": "V = 0\n"}, "Create the engine module skeleton", days=0, author="bob")
    r.commit({"core/engine.py": "V = 1\n"}, "Fix crash in engine when V overflows", days=700, author="alice")
    assert r.run("orphaned_ownership_legacy_core") == []


CLI_V1 = ('import argparse\nimport os\n\n\ndef main():\n    p = argparse.ArgumentParser()\n'
          '    p.add_argument("--legacy-mode")\n    p.add_argument("--still-here")\n'
          '    token = os.environ.get("LEGACY_TOKEN")\n    keep = os.environ.get("KEEP_ME")\n    return p, token, keep\n')
CLI_V2 = ('import argparse\nimport os\n\n\ndef main():\n    p = argparse.ArgumentParser()\n'
          '    p.add_argument("--still-here")\n    keep = os.environ.get("KEEP_ME")\n    return p, keep\n')


def test_doc_identifier_orphan_flags_and_env_vars(mini):
    r = mini()
    r.commit({"app/cli.py": CLI_V1}, "Add cli", days=0)
    r.commit({"README.md": "# Usage\n\nPass `--legacy-mode` and set `LEGACY_TOKEN=abc`; also `--still-here`, `KEEP_ME`\n"
                           "and `--no-such-flag-ever`, `NEVER_SEEN_VAR`.\n"}, "Add readme", days=10)
    r.commit({"app/cli.py": CLI_V2}, "Drop legacy mode and token", days=50)
    r.commit({"app/other.py": "X = 1\n"}, "Later work", days=60)
    r.ingest()
    assert r.ctx.contracts
    hits = r.run("doc_identifier_orphan")
    assert [h.subject for h in hits] == ["README.md"]
    got = {(o["kind"], o["token"]) for o in hits[0].evidence["orphans"]}
    assert got == {("flag", "--legacy-mode"), ("env", "LEGACY_TOKEN")}

    # fallback: a repo without any HEAD contract cannot tell flags/env vars apart, so only paths/symbols remain
    r.ctx.__dict__["contracts"] = []
    assert r.run("doc_identifier_orphan") == []


def _closed_issue_repo(mini):
    r = mini()
    r.commit({"app/net.py": "def fetch(url):\n    # TODO: drop this retry hack once #42 is fixed upstream\n"
                            "    return url\n\n\ndef other(url):\n    # TODO: see #43 for a cleaner approach\n"
                            "    return url\n"}, "Add fetch with workaround", days=0)
    r.commit({"README.md": "x\n"}, "Docs", days=30)
    r.ingest()
    return r


def test_comment_reference_decay_closed_issue(mini):
    from afd_oracle_ingest.catalog.base import Ctx
    r = _closed_issue_repo(mini)
    # no GitHub sync: silently nothing
    assert r.run("comment_reference_decay") == []
    with r.ctx.store.tx() as conn:
        conn.execute("INSERT INTO discussions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("i42", "issue", 42, "Upstream timeout bug", "", "closed", "[]", "", "x", "2023-01-05", "2023-01-20", None))
        conn.execute("INSERT INTO discussions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("i43", "issue", 43, "Cleaner approach", "", "open", "[]", "", "x", "2023-01-05", None, None))
    r.ctx = Ctx(r.ctx.repo, r.ctx.store, r.ctx.settings)
    hits = r.run("comment_reference_decay")
    assert len(hits) == 1 and hits[0].evidence["status"] == "issue_closed" and hits[0].evidence["issue"] == 42
    assert hits[0].paths == ["app/net.py"]
