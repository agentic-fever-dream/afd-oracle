"""Enrichment tables: tags, file modes, signature/complexity history, line->symbol, blame of fix lines, aux lines."""


def test_enrichment_tables(mini):
    r = mini()
    first = r.commit({"app/core.py": "def parse(x):\n    return int(x)\n",
                      "package-lock.json": '{"lockfileVersion": 3, "packages": {"a": {"version": "1.0.0"}}}\n'},
                     "Add parser", days=0)
    r.git("tag", "v1.0", days=0)
    r.commit({"app/core.py": "def parse(x, base=10):\n    if x is None:\n        return 0\n    return int(x, base)\n",
              "package-lock.json": '{"lockfileVersion": 3, "packages": {"a": {"version": "1.1.0"}}}\n'},
             "Support other bases", days=20)
    r.git("update-index", "--chmod=+x", "app/core.py", days=21)
    r.git("commit", "-q", "-m", "Make core executable", days=21)
    fix = r.commit({"app/core.py": "def parse(x, base=10):\n    if x is None or x == '':\n        return 0\n"
                                   "    return int(x, base)\n"}, "Fix crash on empty string", days=40)
    ctx = r.ingest()
    uid = f"sym:{r.name}:app/core.py:parse:function"

    assert [t["name"] for t in ctx.tags] == ["v1.0"] and ctx.tags[0]["commit_hash"] == first
    assert any(m["path"] == "app/core.py" and m["new_mode"] == 0o100755 for m in ctx.file_modes)

    sigs = [v["signature"] for v in ctx.symbol_versions(uid)]
    assert sigs[0].startswith("def parse(x)") and "base=10" in sigs[-1]
    assert [v["params"] for v in ctx.symbol_versions(uid)][:2] == [1, 2]
    assert ctx.complexity(uid) >= 3  # if + or + 1

    fix_lines = ctx.lines_of(fix, "app/core.py")
    assert fix_lines and all(ln["symbol"] == uid for ln in fix_lines)

    removed = [ln for ln in fix_lines if ln["sign"] == "-"]
    assert removed and ctx.line_origin(fix, "app/core.py", removed[0]["line"]) is not None

    assert any("1.1.0" in a["text"] for a in ctx.grep_aux(r"version", path_like="%package-lock.json"))
    assert ctx.test_map_at("v1.0") == [] or all(t["ref"] == "v1.0" for t in ctx.test_map_at("v1.0"))


def test_round2_enrichment(mini):
    r = mini()
    r.commit({"app/core.py": "def parse(x):\n    return int(x)\n\n\ndef use(x):\n    return parse(x)\n\n\n"
                             "def top(x):\n    return use(x)\n"}, "Add parser", days=0)
    r.git("tag", "v1.0", days=0)
    change = r.commit({"app/core.py": "def parse(x):\n    if x is None:\n        raise ValueError('x')\n"
                                      "    return int(x)\n\n\ndef use(x):\n    return parse(x)\n\n\n"
                                      "def top(x):\n    return use(x)\n"}, "Reject None", days=10)
    r.commit({"app/core.py": "def parse(x):\n    return int(x)\n\n\ndef use(x):\n    return parse(x)\n\n\n"
                             "def top(x):\n    return use(x)\n"}, "Revert reject None", days=11)
    again = r.commit({"app/core.py": "def parse(x):\n    if x is None:\n        raise ValueError('x')\n"
                                     "    return int(x)\n\n\ndef use(x):\n    return parse(x)\n\n\n"
                                     "def top(x):\n    return use(x)\n"}, "Reject None again", days=12)
    ctx = r.ingest()
    uid = f"sym:{r.name}:app/core.py:parse:function"
    assert ctx.patch_ids[change] == ctx.patch_ids[again]  # the same change applied twice
    assert any(ctx.raises_of(v) == ["ValueError"] for v in ctx.symbol_versions(uid))
    assert ctx.callers_within(uid, 2) == {f"sym:{r.name}:app/core.py:use:function": 1,
                                          f"sym:{r.name}:app/core.py:top:function": 2}
    assert {(e["src"].split(":")[3], e["dst"].split(":")[3]) for e in ctx.calls_at("v1.0")} >= {("use", "parse"),
                                                                                               ("top", "use")}
    assert ctx.github_repo is None and ctx.github_releases == [] and ctx.helm_renders() == []
