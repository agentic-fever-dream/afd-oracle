"""Roadmap phase 6: embeddings (Qdrant, in-memory) and work packets."""
from __future__ import annotations

import json

from afd_oracle_core import runtime
from afd_oracle_core.vectors import CODE_SYMBOLS, COMMIT_DIFFS, scroll_ids, search
from afd_oracle_ingest.embed import EMBED_KINDS


def test_search_finds_fixture_symbol(ingested):
    client, emb = runtime.vectors()
    hits = search(client, emb, CODE_SYMBOLS, "validate form schema", "hybrid", {"repo": "fixture"}, 3)
    ids = [h.payload["neo4j_id"] for h in hits]
    assert {"sym:fixture:web/forms.ts:validateForm:function",
            "sym:fixture:api/schema.py:validate_schema:function"} & set(ids)


def test_search_filters(ingested):
    client, emb = runtime.vectors()
    hits = search(client, emb, CODE_SYMBOLS, "engine step", "hybrid", {"repo": "fixture", "path_prefix": "core"}, 10)
    assert hits and all(h.payload["path"].startswith("core/") for h in hits)
    reverts = search(client, emb, COMMIT_DIFFS, "caching layer", "hybrid", {"repo": "fixture"}, 5)
    assert any(h.payload["is_revert"] for h in reverts)


def test_vector_graph_integrity(ingested):
    ctx, _, _ = ingested
    client, _ = runtime.vectors()
    point_ids = scroll_ids(client, CODE_SYMBOLS, {"repo": "fixture"})
    store_ids = {r["uid"] for r in ctx.store.q(
        f"SELECT uid FROM symbols WHERE kind IN ({','.join('?' * len(EMBED_KINDS))}) AND (alive = 1 OR uid IN "
        "(SELECT subject FROM candidates WHERE kind IN ('short_lived_symbol', 'churned_deleted')))", EMBED_KINDS)}
    assert point_ids == store_ids  # every point maps to a symbol and every embeddable symbol has a point
    assert "sym:fixture:util/helpers.py:experimental_hash:function" in point_ids  # dead but graveyard evidence
    commit_ids = scroll_ids(client, COMMIT_DIFFS, {"repo": "fixture"})
    assert commit_ids == {f"commit:fixture:{r['hash']}" for r in ctx.store.q("SELECT hash FROM commits")}


def test_packets(ingested):
    ctx, facts, result = ingested
    assert result["packets"]["max_tokens"] <= 6000
    rows = ctx.store.q("SELECT * FROM packets")
    kinds = {(r["kind"], r["subkind"]) for r in rows}
    assert {("coupling", "cross_dir_unlinked"), ("graveyard", "revert"), ("graveyard", "abandoned_branch"),
            ("graveyard", "short_lived_symbol"), ("pattern", "god_class"), ("pattern", "cycle")} <= kinds
    coupling = json.loads(ctx.store.q1("SELECT body FROM packets WHERE kind='coupling'")["body"])
    assert [coupling["pair"]["a"], coupling["pair"]["b"]] == facts["pair"]
    assert len(coupling["shared_commits"]) == 3 and coupling["shared_commits"][0]["diff_a"]
    revert = [json.loads(r["body"]) for r in rows if r["subkind"] == "revert"]
    assert any(p["reverted"]["hash"] == facts["explicit_revert"]["reverted"] for p in revert)
