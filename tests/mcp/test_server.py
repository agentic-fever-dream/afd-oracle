"""Roadmap phase 7: MCP tools through FastMCP's in-memory client (Neo4j absent -> graph tools degrade cleanly)."""
from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from conftest import neo4j_available


@pytest.fixture
def server(ingested):
    from afd_oracle_mcp import server as srv
    srv._repo_handles.cache_clear()
    return srv.mcp


async def call(server, name: str, args: dict) -> dict:
    async with Client(server) as c:
        res = await c.call_tool(name, args)
        return res.structured_content if res.structured_content is not None else res.data


async def test_tools_listed(server):
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == {"run_cypher", "search_qdrant", "get_commit_diff", "get_symbol_history", "get_work_packets",
                     "upsert_insight", "verify_citations", "set_insight_status", "list_insights",
                     "identify_repo", "repo_overview", "assess_change", "check_diff", "list_findings",
                     "get_contracts", "search_discussions", "list_vulnerabilities"}


async def test_list_vulnerabilities_before_and_after_a_scan(server):
    from afd_oracle_core.config import get_settings
    from afd_oracle_core.registry import open_store
    from afd_oracle_ingest import scan
    out = await call(server, "list_vulnerabilities", {"repo": "fixture"})
    store = open_store("fixture", get_settings())
    scan.ensure(store)
    f = scan.Finding("bandit", "B602:subprocess", "high", "shell=True", "core/a.py", 1, package="")
    with store.tx() as conn:
        conn.execute("DELETE FROM vuln_findings")
        conn.execute("INSERT INTO vuln_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f.id, f.scanner, f.rule, f.severity, f.title, "", f.path, f.line, None, "", "", "", "", "", "",
                      None, "abc", 0.0))
    out = await call(server, "list_vulnerabilities", {"repo": "fixture", "severity": "high"})
    assert [v["rule"] for v in out["vulnerabilities"]] == ["B602:subprocess"]
    assert (await call(server, "list_vulnerabilities", {"repo": "fixture", "scanner": "trivy"}))["vulnerabilities"] == []


async def test_identify_repo_matches_working_copies(server, fixture_repo, tmp_path):
    import subprocess
    path, _ = fixture_repo
    found = await call(server, "identify_repo", {"path": str(path / "api")})  # any folder inside the working copy
    assert found["found"] is True and found["repo_id"] == "fixture" and "root_commit" in found["matched_by"]
    assert found["commits_not_ingested"] == 0 and found["stale"] is False
    clone = tmp_path / "clone"  # a fresh clone shares the root commit; one new local commit makes it stale
    subprocess.run(["git", "clone", "-q", str(path), str(clone)], check=True)
    (clone / "new.txt").write_text("x")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-qm", "local"], check=True)
    behind = await call(server, "identify_repo", {"path": str(clone)})
    assert behind["repo_id"] == "fixture" and behind["commits_not_ingested"] == 1 and behind["stale"] is True
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    assert (await call(server, "identify_repo", {"path": str(other)}))["found"] is False
    assert (await call(server, "identify_repo", {"path": str(tmp_path)}))["found"] is False


async def test_repo_overview(server):
    out = await call(server, "repo_overview", {"repo": "fixture"})
    assert out["repo_id"] == "fixture" and out["size"]["commits"] == 24 and "top_verified_insights" in out


@pytest.mark.parametrize("q", ["MERGE (n:X) RETURN n", "MATCH (n) SET n.a=1", "CALL apoc.create.node(['X'],{})"])
async def test_run_cypher_rejects_writes(server, q):
    with pytest.raises(ToolError, match="read-only|not allowed"):
        await call(server, "run_cypher", {"query": q})


async def test_run_cypher_limit_validation(server):
    with pytest.raises(ToolError):
        await call(server, "run_cypher", {"query": "MATCH (n) RETURN n", "limit": 5000})


@pytest.mark.skipif(neo4j_available(), reason="only meaningful when Neo4j is down")
async def test_run_cypher_reports_unreachable_graph(server):
    with pytest.raises(ToolError, match="unreachable"):
        await call(server, "run_cypher", {"query": "MATCH (n) RETURN count(n)"})


async def test_get_commit_diff(server, ingested):
    _, facts, _ = ingested
    out = await call(server, "get_commit_diff", {"commit_hash": facts["explicit_revert"]["reverter"][:10]})
    assert out["hash"] == facts["explicit_revert"]["reverter"]
    assert out["is_revert"] and out["reverts"] == [facts["explicit_revert"]["reverted"]]
    assert {f["path"] for f in out["files"]} == {"feature/__init__.py", "feature/cache.py"}
    assert any(s["change"] == "deleted" for s in out["symbols_touched"])
    small = await call(server, "get_commit_diff", {"commit_hash": facts["commits"]["c01_initial"], "max_bytes": 1000})
    assert any(f["truncated"] for f in small["files"])


async def test_get_symbol_history_follows_rename(server):
    out = await call(server, "get_symbol_history", {"qualname": "slugify", "path": "util/text.py",
                                                    "include_bodies": True})
    assert out["symbol"]["previous_uids"] == ["sym:fixture:util/strings.py:slugify:function"]
    assert [e["change"] for e in out["events"]] == ["created", "moved"]
    assert "def slugify" in out["events"][0]["body"]


async def test_search_qdrant_with_store_fallback(server):
    out = await call(server, "search_qdrant", {"collection": "code_symbols", "query": "validate form",
                                               "filter": {"repo": "fixture"}, "limit": 5, "with_neo4j": True})
    assert out["hits"] and "Symbol" in out["hits"][0]["graph"]["labels"]  # graph node, or store fallback


async def test_get_work_packets_claims(server):
    first = await call(server, "get_work_packets", {"kind": "graveyard", "ids_only": True, "limit": 2})
    assert len(first["packets"]) == 2 and "body" not in first["packets"][0]
    ids = [p["id"] for p in first["packets"]]
    again = await call(server, "get_work_packets", {"kind": "graveyard", "limit": 50, "ids_only": True})
    assert not set(ids) & {p["id"] for p in again["packets"]}  # claimed packets are not handed out twice
    full = await call(server, "get_work_packets", {"kind": "graveyard", "ids": ids})
    assert {p["id"] for p in full["packets"]} == set(ids) and all("body" in p for p in full["packets"])


def _citations(facts, **over):
    c = {"commits": [{"hash": facts["commits"]["c09_pair3"], "role": "evidence"}],
         "files": [{"path": "api/schema.py", "at_commit": facts["commits"]["c09_pair3"]},
                   {"path": "web/forms.ts", "at_commit": facts["commits"]["c09_pair3"]}],
         "symbols": [{"uid": "sym:fixture:api/schema.py:validate_schema:function"}]}
    c.update(over)
    return c


def _insight(facts, **over):
    base = {"label": "AntiPattern", "kind": "hidden_coupling", "title": "Signup schema and form validated in lockstep",
            "summary": "Every signup field is added to api/schema.py and web/forms.ts in the same commit.",
            "severity": "medium", "confidence": 0.8, "citations": _citations(facts)}
    base.update(over)
    return base


@pytest.mark.parametrize("bad,reason", [
    ({"commits": [{"hash": "deadbeefdeadbeef", "role": "evidence"}]}, "not found"),
    ({"files": [{"path": "api/nope.py", "at_commit": "@c09"}]}, "does not exist"),
    ({"symbols": [{"uid": "sym:fixture:api/schema.py:ghost:function"}]}, "unknown symbol"),
    ({"commits": [{"hash": "@c02", "role": "evidence"}]}, "none of the cited commits touched"),
    ({"commits": [{"hash": "@c09", "role": "blamed"}]}, "role must be"),
])
async def test_upsert_insight_rejects_bad_citations(server, ingested, bad, reason):
    _, facts, _ = ingested
    for v in bad.values():
        for item in v:
            for k, val in list(item.items()):
                if val == "@c09":
                    item[k] = facts["commits"]["c09_pair3"]
                if val == "@c02":
                    item[k] = facts["commits"]["c02_cycle"]
    out = await call(server, "upsert_insight", _insight(facts, citations=_citations(facts, **bad)))
    assert out["insight_id"] is None and out["status"] == "invalid"
    assert any(reason in e["reason"] for e in out["citation_report"]["errors"])


async def test_insight_lifecycle(server, ingested):
    _, facts, _ = ingested
    out = await call(server, "upsert_insight", _insight(facts, model="sonnet"))
    iid = out["insight_id"]
    assert out["status"] == "proposed" and out["citation_report"]["valid"]
    assert isinstance(out["graph_synced"], bool)  # True only when Neo4j is up AND credentials work
    dup = await call(server, "upsert_insight", _insight(facts))
    assert dup["duplicate"] and dup["insight_id"] == iid

    check = await call(server, "verify_citations", {"insight_id": iid})
    assert check["valid"]
    done = await call(server, "set_insight_status", {
        "insight_id": iid, "status": "verified", "verdict_notes": "Re-derived from 5 shared commits.",
        "votes": [{"model": "opus", "refuted": False, "reason": "diffs confirm lockstep"}]})
    assert done["status"] == "verified" and done["embedded"]

    hits = await call(server, "search_qdrant", {"collection": "architectural_insights", "query": "signup lockstep",
                                                "limit": 3})
    assert hits["hits"][0]["neo4j_id"] == iid
    listed = await call(server, "list_insights", {"status": "verified", "kind": "hidden_coupling"})
    assert [i["insight_id"] for i in listed["insights"]] == [iid]
    assert listed["insights"][0]["citations"]["commits"][0]["hash"] == facts["commits"]["c09_pair3"]
    if neo4j_available():  # don't leave test nodes in a real database
        from afd_oracle_core import runtime
        drv = runtime.graph_driver()
        if drv is not None:
            with drv.session() as s:
                s.run("MATCH (i:Insight {uid:$id}) DETACH DELETE i", id=iid).consume()


def test_status_change_survives_vector_store_failure(ingested):
    """A locked/unreachable Qdrant must not block verification (embedding is retried by `afd-oracle sync-insights`)."""
    from afd_oracle_core.insights import InsightService
    ctx, facts, _ = ingested

    def broken():
        raise RuntimeError("Storage folder is already accessed by another instance of Qdrant client")

    svc = InsightService(ctx.store, ctx.repo, vectors=broken)
    c = facts["commits"]
    out = svc.upsert(label="Pattern", kind="idiom", title="Cycle stages share a one-call shape",
                     summary="Each core stage delegates to the next.", severity="info", confidence=0.5,
                     citations={"commits": [{"hash": c["c02_cycle"], "role": "introduced"}],
                                "files": [{"path": "core/a.py", "at_commit": c["c02_cycle"]}]})
    done = svc.set_status(out["insight_id"], "verified", "ok")
    assert done["status"] == "verified" and done["embedded"] is False


async def test_set_status_rejects_unknown(server):
    with pytest.raises(ToolError):
        await call(server, "set_insight_status", {"insight_id": "insight:nope", "status": "verified",
                                                  "verdict_notes": "x"})


async def test_change_safety_tools(server):
    out = await call(server, "assess_change", {"paths": ["api/schema.py"], "repo": "fixture"})
    assert any(p["partner"] == "web/forms.ts" and p["hidden"] for p in out["hidden_coupling"])
    assert out["risk_level"] in ("medium", "high") and out["must"]
    diff = ("diff --git a/api/schema.py b/api/schema.py\n--- a/api/schema.py\n+++ b/api/schema.py\n"
            "@@ -1,1 +1,2 @@\n+import pickle\n+data = pickle.loads(blob)\n")
    checked = await call(server, "check_diff", {"diff": diff, "repo": "fixture"})
    rules = {f["rule"] for f in checked["findings"]}
    assert "sec.deserialize" in rules and "coupling.partner_not_changed" in rules
    assert checked["verdict"] in ("warn", "block")
    found = await call(server, "list_findings", {"repo": "fixture", "limit": 5})
    assert "findings" in found
    assert (await call(server, "search_discussions", {"query": "cache", "repo": "fixture"}))["results"] == []
