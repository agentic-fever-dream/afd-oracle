"""Reference tests for the two framework example builders (pattern for every family test module)."""


def test_fix_without_regression_test(mini):
    r = mini()
    r.commit({"app/core.py": "def parse(x):\n    return int(x)\n\n\ndef other():\n    return 1\n",
              "tests/test_other.py": "from app.core import other\n\n\ndef test_other():\n    assert other() == 1\n"},
             "Add parser", days=0)
    fix = r.commit({"app/core.py": "def parse(x):\n    if x is None:\n        return 0\n    return int(x)\n\n\n"
                                   "def other():\n    return 1\n"}, "Fix crash when parse gets None", days=30)
    hits = r.run("fix_without_regression_test")
    assert [h.subject.split("|")[0] for h in hits] == [fix]
    assert hits[0].symbols == ["sym:" + r.name + ":app/core.py:parse:function"]


def test_fix_with_test_in_same_commit_is_dropped(mini):
    r = mini()
    r.commit({"app/core.py": "def parse(x):\n    return int(x)\n"}, "Add parser", days=0)
    r.commit({"app/core.py": "def parse(x):\n    if x is None:\n        return 0\n    return int(x)\n",
              "tests/test_core.py": "from app.core import parse\n\n\ndef test_none():\n    assert parse(None) == 0\n"},
             "Fix crash when parse gets None", days=30)
    assert r.run("fix_without_regression_test") == []
    assert r.ctx.noise.get("fix_without_regression_test/test_in_same_commit") == 1
