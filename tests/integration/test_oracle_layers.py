"""Roadmap phase 5: reverts, branches, co-change, metrics, structural snapshot."""
from __future__ import annotations

from itertools import combinations

from conftest import git


def _expected_pair_counts(repo, a: str, b: str, max_files: int = 30) -> tuple[int, int, int]:
    """Independent co-change count straight from `git log` (non-merge commits touching 2..max files)."""
    out = git(repo, "log", "--all", "--no-merges", "-M", "--name-only", "--format=@@%H")
    commits = [c.strip().splitlines() for c in out.split("@@") if c.strip()]
    n_a = n_b = n_ab = 0
    for c in commits:
        files = {f for f in c[1:] if f.strip()}
        if not 2 <= len(files) <= max_files:
            continue
        n_a += a in files
        n_b += b in files
        n_ab += a in files and b in files
    return n_a, n_b, n_ab


def test_cochange_pair_jaccard(ingested, fixture_repo):
    ctx, facts, _ = ingested
    repo, _ = fixture_repo
    a, b = facts["pair"]
    n_a, n_b, n_ab = _expected_pair_counts(repo, a, b)
    row = ctx.store.q1("SELECT * FROM cochange WHERE a=? AND b=?", (a, b))
    assert row is not None
    assert (row["n_a"], row["n_b"], row["count"]) == (n_a, n_b, n_ab)
    assert abs(row["jaccard"] - n_ab / (n_a + n_b - n_ab)) < 1e-9
    assert row["cross_dir"] == 1 and row["structural_link"] == 0
    top = ctx.store.q1("SELECT a, b FROM cochange ORDER BY score DESC LIMIT 1")
    assert (top["a"], top["b"]) == (a, b)


def test_reverts_detected(ingested):
    ctx, facts, _ = ingested
    rows = {(r["reverter"], r["reverted"]): r["method"] for r in ctx.store.q("SELECT * FROM reverts")}
    ex, si = facts["explicit_revert"], facts["silent_revert"]
    assert rows[(ex["reverter"], ex["reverted"])] == "message"
    assert rows[(si["reverter"], si["reverted"])] == "patch_id"
    assert ctx.store.commit(ex["reverter"])["is_revert"] == 1
    assert ctx.store.commit(si["reverter"])["is_revert"] == 1


def test_branches_classified(ingested):
    ctx, facts, _ = ingested
    b = {r["name"]: r for r in ctx.store.q("SELECT * FROM branches")}
    ab = b[facts["abandoned_branch"]]
    assert ab["abandoned"] == 1 and ab["merged"] == 0 and ab["unique_count"] == 2
    lg = b[facts["merged_branch"]]
    assert lg["merged"] == 1 and lg["abandoned"] == 0
    uniq = {r["hash"] for r in ctx.store.q("SELECT hash FROM branch_commits WHERE branch=?", (facts["abandoned_branch"],))}
    assert uniq == {facts["commits"]["g1_graphql"], facts["commits"]["g2_graphql"]}


def test_structural_edges(ingested):
    ctx, facts, _ = ingested
    imports = {(r["src"], r["dst"]) for r in ctx.store.q("SELECT src, dst FROM edges WHERE kind='IMPORTS'")}
    a, b, c = facts["cycle"]
    assert {(a, b), (b, c), (c, a)} <= imports
    assert ("web/login.ts", "web/forms.ts") in imports
    calls = {(r["src"], r["dst"]) for r in ctx.store.q("SELECT src, dst FROM edges WHERE kind='CALLS'")}
    assert ("sym:fixture:web/login.ts:login:function", "sym:fixture:web/forms.ts:validateForm:function") in calls
    assert ("sym:fixture:core/a.py:alpha:function", "sym:fixture:core/b.py:beta:function") in calls


def test_metrics_candidates(ingested, fixture_repo):
    ctx, facts, _ = ingested
    cyc = ctx.store.q("SELECT * FROM candidates WHERE kind='cycle'")
    assert len(cyc) == 1 and sorted(facts["cycle"]) == sorted(__import__("json").loads(cyc[0]["props"])["files"])
    god = ctx.store.q("SELECT subject FROM candidates WHERE kind='god_class' ORDER BY score DESC")
    assert god[0]["subject"] == "sym:fixture:core/engine.py:Engine:class"
    short = {r["subject"] for r in ctx.store.q("SELECT subject FROM candidates WHERE kind='short_lived_symbol'")}
    sl = facts["short_lived"]
    assert f"sym:fixture:{sl['path']}:{sl['qualname']}:function" in short
    assert "sym:fixture:util/helpers.py:legacy_parse:function" not in short  # lived 60 days, 12 commits
