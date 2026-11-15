"""Neo4j projection (skipped unless a Neo4j server answers on 127.0.0.1:7687).

Run against a disposable server; set AFD_ORACLE_NEO4J_PASSWORD. The test only touches nodes with repo='neo4j_fixture'
(plus the example.com authors it created) and removes them afterwards.
"""
from __future__ import annotations

import pytest

from conftest import neo4j_available

pytestmark = [pytest.mark.neo4j, pytest.mark.skipif(not neo4j_available(), reason="Neo4j not reachable")]
RID = "neo4j_fixture"


@pytest.fixture(scope="module")
def projected(env, fixture_repo, tmp_path_factory):
    from afd_oracle_core import runtime
    from afd_oracle_core.config import Settings, get_settings
    from afd_oracle_core.graph import apply_schema
    from afd_oracle_ingest.pipeline import open_context, run_deterministic
    from afd_oracle_ingest.writers.neo4j_writer import project

    s0 = get_settings()
    s = Settings(**{**s0.model_dump(), "data_dir": tmp_path_factory.mktemp("neo4jdata")})
    try:
        drv = runtime.graph_driver(s0, required=True)
    except ConnectionError as e:
        pytest.skip(f"Neo4j reachable but not usable (set AFD_ORACLE_NEO4J_PASSWORD?): {e}")
    assert apply_schema(drv, s.neo4j_database) == apply_schema(drv, s.neo4j_database)  # idempotent
    ctx = open_context(fixture_repo[0], RID, s, workers=1, log=lambda *_: None)
    run_deterministic(ctx, full=True)
    stats = project(ctx.store, drv, s.neo4j_database, ctx.repo.path)
    yield ctx, drv, s, stats
    with drv.session(database=s.neo4j_database) as sess:
        sess.run("MATCH (n {repo:$repo}) DETACH DELETE n", repo=RID).consume()
        sess.run("MATCH (a:Author) WHERE a.email ENDS WITH '@example.com' AND NOT (a)-->() DELETE a").consume()


def q(drv, s, cypher, **params):
    with drv.session(database=s.neo4j_database) as sess:
        return [r.data() for r in sess.run(cypher, **params)]


def test_counts_match_store(projected):
    ctx, drv, s, stats = projected
    n_commits = ctx.store.q1("SELECT count(*) n FROM commits")["n"]
    assert q(drv, s, "MATCH (c:Commit {repo:$r}) RETURN count(c) AS n", r=RID)[0]["n"] == n_commits
    parents = q(drv, s, "MATCH (:Commit {repo:$r})-[p:PARENT]->() RETURN count(p) AS n", r=RID)[0]["n"]
    assert parents == n_commits - 1 + 1  # 24 commits, one root, one merge with two parents
    syms = ctx.store.q1("SELECT count(*) n FROM symbols")["n"]
    assert q(drv, s, "MATCH (s:Symbol {repo:$r}) RETURN count(s) AS n", r=RID)[0]["n"] == syms
    assert q(drv, s, "MATCH (s:Function {repo:$r, qualname:'validate_schema'}) RETURN s.alive AS a", r=RID) == [{"a": True}]
    touched = q(drv, s, "MATCH (c:Commit {repo:$r})-[t:TOUCHED]->(:File) RETURN count(t) AS n", r=RID)[0]["n"]
    assert touched == ctx.store.q1("SELECT count(*) n FROM file_changes WHERE ignored=0")["n"]


def test_oracle_layer(projected, fixture_repo):
    ctx, drv, s, _ = projected
    facts = fixture_repo[1]
    rows = q(drv, s, """MATCH (a:File)-[r:CO_COMMITTED]-(b:File) WHERE a.repo=$r AND r.cross_dir AND NOT r.structural_link
                        RETURN a.path AS a, b.path AS b, r.jaccard AS j ORDER BY r.jaccard DESC LIMIT 10""", r=RID)
    assert {rows[0]["a"], rows[0]["b"]} == set(facts["pair"])
    reverts = q(drv, s, "MATCH (a:Commit {repo:$r})-[x:REVERTS]->(b) RETURN a.hash AS a, b.hash AS b, x.method AS m",
                r=RID)
    assert {"a": facts["explicit_revert"]["reverter"], "b": facts["explicit_revert"]["reverted"], "m": "message"} in reverts
    assert {"a": facts["silent_revert"]["reverter"], "b": facts["silent_revert"]["reverted"], "m": "patch_id"} in reverts
    br = q(drv, s, "MATCH (b:Branch {repo:$r, abandoned:true})<-[:UNIQUE_TO]-(c) RETURN b.name AS n, count(c) AS k",
           r=RID)
    assert br == [{"n": facts["abandoned_branch"], "k": 2}]
    cyc = q(drv, s, "MATCH p=(a:File {repo:$r})-[:IMPORTS*3]->(a) RETURN count(p) AS n", r=RID)[0]["n"]
    assert cyc == 3
    god = q(drv, s, "MATCH (c:Class {repo:$r}) RETURN c.name AS n ORDER BY c.god_score DESC LIMIT 1", r=RID)
    assert god == [{"n": "Engine"}]
    defines = q(drv, s, "MATCH (:Class {repo:$r, name:'Engine'})-[:DEFINES]->(m:Function) RETURN count(m) AS n", r=RID)
    assert defines[0]["n"] == 41  # __init__ + 40 steps


def test_reprojection_is_idempotent(projected):
    from afd_oracle_ingest.writers.neo4j_writer import project
    ctx, drv, s, _ = projected
    count = "MATCH (n {repo:$r}) OPTIONAL MATCH (n)-[x]-() RETURN count(DISTINCT n) AS n, count(DISTINCT x) AS e"
    before = q(drv, s, count, r=RID)
    with ctx.store.tx() as conn:
        conn.execute("UPDATE commits SET projected=0")
    project(ctx.store, drv, s.neo4j_database, ctx.repo.path)
    assert q(drv, s, count, r=RID) == before


def test_repos_sharing_history_stay_isolated(projected, fixture_repo, tmp_path):
    """A fork ingested under another id must get its own Commit nodes (no shared/overwritten history)."""
    from afd_oracle_core.config import Settings
    from afd_oracle_ingest.pipeline import open_context, run_deterministic
    from afd_oracle_ingest.writers.neo4j_writer import project
    ctx, drv, s, _ = projected
    other = "neo4j_fixture_fork"
    s2 = Settings(**{**s.model_dump(), "data_dir": tmp_path / "fork"})
    fork = open_context(fixture_repo[0], other, s2, workers=1, log=lambda *_: None)
    run_deterministic(fork, full=True)
    try:
        project(fork.store, drv, s.neo4j_database, fork.repo.path)
        n = ctx.store.q1("SELECT count(*) n FROM commits")["n"]
        for rid in (RID, other):
            assert q(drv, s, "MATCH (c:Commit {repo:$r}) RETURN count(c) AS n", r=rid)[0]["n"] == n
        touched = q(drv, s, "MATCH (c:Commit {repo:$r})-[t:TOUCHED]->(:File) RETURN count(t) AS n", r=RID)[0]["n"]
        assert touched == ctx.store.q1("SELECT count(*) n FROM file_changes WHERE ignored=0")["n"]
        cross = q(drv, s, "MATCH (c:Commit {repo:$a})-[:TOUCHED|PARENT]->(x {repo:$b}) RETURN count(*) AS n",
                  a=RID, b=other)[0]["n"]
        assert cross == 0
    finally:
        with drv.session(database=s.neo4j_database) as sess:
            sess.run("MATCH (n {repo:$repo}) DETACH DELETE n", repo=other).consume()


def test_read_transactions_refuse_writes(projected):
    from afd_oracle_core.graph import read_query
    _, drv, s, _ = projected
    cols, rows, _ = read_query(drv, s.neo4j_database, "MATCH (c:Commit {repo:$r}) RETURN count(c) AS n", {"r": RID},
                               10, 30)
    assert cols == ["n"] and rows[0][0] > 0
    with pytest.raises(Exception):
        read_query(drv, s.neo4j_database, "CREATE (n:ShouldNotExist {repo:$r})", {"r": RID}, 10, 30)


def test_insight_projection(projected, fixture_repo):
    from afd_oracle_core.insights import InsightService
    ctx, drv, s, _ = projected
    facts = fixture_repo[1]
    svc = InsightService(ctx.store, ctx.repo, s.neo4j_database, driver=lambda: drv)
    c = facts["commits"]
    out = svc.upsert(label="RejectedAttempt", kind="reverted_feature", title="Read-through cache was reverted",
                     summary="feature/cache.py was added and reverted.", severity="low", confidence=0.9,
                     citations={"commits": [{"hash": c["c06_cache"], "role": "introduced"},
                                            {"hash": c["c07_revert_cache"], "role": "reverted"}],
                                "files": [{"path": "feature/cache.py", "at_commit": c["c06_cache"]}],
                                "symbols": [{"uid": f"sym:{RID}:feature/cache.py:cached:function"}]})
    assert out["graph_synced"] is True
    iid = out["insight_id"]
    rows = q(drv, s, """MATCH (i:Insight:RejectedAttempt {uid:$id})-[e:EVIDENCED_BY]->(c:Commit)
                        RETURN e.role AS role, c.hash AS h ORDER BY role""", id=iid)
    assert rows == [{"role": "introduced", "h": c["c06_cache"]}, {"role": "reverted", "h": c["c07_revert_cache"]}]
    concerns = q(drv, s, "MATCH (:Insight {uid:$id})-[:CONCERNS]->(n) RETURN labels(n) AS l", id=iid)
    assert len(concerns) == 2
    svc.set_status(iid, "verified", "confirmed")
    assert q(drv, s, "MATCH (i:Insight {uid:$id}) RETURN i.status AS s", id=iid) == [{"s": "verified"}]
    with drv.session(database=s.neo4j_database) as sess:
        sess.run("MATCH (i:Insight {uid:$id}) DETACH DELETE i", id=iid).consume()
