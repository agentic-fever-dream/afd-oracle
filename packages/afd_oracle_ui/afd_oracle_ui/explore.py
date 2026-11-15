"""Explore API: read-only browsing of a repository's graph (Neo4j) and vector store (Qdrant).

Every graph query runs in a READ transaction with a timeout and a row cap. Node lookups derive the label from the uid
prefix so they hit the per-label uniqueness constraint instead of scanning, and neighbour expansion is capped per edge
type so hubs (busy authors, much-touched files) can't flood the canvas.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from qdrant_client import models

from afd_oracle_core import runtime
from afd_oracle_core.config import get_settings
from afd_oracle_core.graph import read_query
from afd_oracle_core.registry import list_repos
from afd_oracle_core.vectors import search as vsearch

router = APIRouter(prefix="/api/repos/{repo}/explore", tags=["explore"])

LABEL_BY_PREFIX = {
    "repo": "Repository", "dir": "Directory", "file": "File", "sym": "Symbol", "commit": "Commit",
    "author": "Author", "branch": "Branch", "insight": "Insight", "vuln": "Vulnerability",
}
COLLECTIONS = {"code": "code_symbols", "commits": "commit_diffs", "insights": "architectural_insights",
               "vulns": "security_findings"}
COLLECTION_BY_PREFIX = {"sym": "code_symbols", "commit": "commit_diffs", "insight": "architectural_insights",
                        "vuln": "security_findings"}
BIG_PROPS = {"body", "text", "embedding"}

MAP_TTL_S = 600  # re-project after re-embedding or an update


def _repo(repo: str) -> None:
    if repo not in list_repos():
        raise HTTPException(404, f"unknown repository '{repo}'")


def _label(uid: str) -> str:
    label = LABEL_BY_PREFIX.get(uid.split(":", 1)[0])
    if not label:
        raise HTTPException(400, f"unrecognised uid '{uid}'")
    return label


def _driver():
    s = get_settings()
    drv = runtime.graph_driver(s)
    if drv is None:
        raise HTTPException(503, "Neo4j is not reachable. Start the services from the Repositories page.")
    return s, drv


def _q(query: str, params: dict, limit: int = 500) -> list[list[Any]]:
    s, drv = _driver()
    try:
        return read_query(drv, s.neo4j_database, query, params, limit, s.cypher_timeout_s)[1]
    except HTTPException:
        raise
    except Exception as e:  # surface Cypher/timeouts as a clean API error
        raise HTTPException(502, f"graph query failed: {e}") from e


def node_view(props: dict, labels: list[str] | None = None) -> dict:
    """A compact, display-ready description of a node."""
    p = {k: v for k, v in props.items() if k not in BIG_PROPS and k != "_labels"}
    labels = labels or props.get("_labels") or []
    uid = str(p.get("uid", ""))
    kind = next((lb for lb in labels if lb not in ("Symbol", "Insight")), labels[0] if labels else "Node")
    if "Symbol" in labels:
        kind = str(p.get("kind") or kind).capitalize()
    msg = str(p.get("message") or "").strip().splitlines()
    title = (p.get("qualname") or p.get("title") or (f"{p.get('short') or str(p.get('hash', ''))[:10]} {msg[0] if msg else ''}"
             if "Commit" in labels else None) or p.get("path") or p.get("name") or uid)
    sub = p.get("path") if "Symbol" in labels else p.get("email") if "Author" in labels else \
        p.get("kind") if "Insight" in labels else p.get("repo")
    return {"uid": uid, "group": LABEL_BY_PREFIX.get(uid.split(":", 1)[0], kind), "kind": kind, "labels": labels,
            "title": str(title)[:160], "subtitle": str(sub or ""), "alive": p.get("alive"), "props": p}


# ---------------------------------------------------------------------------- search
@router.get("/search")
def search(repo: str, q: str = Query(..., min_length=1), scope: str = "code", mode: str = "hybrid",
           alive: bool | None = None, limit: int = Query(25, ge=1, le=100)) -> dict:
    _repo(repo)
    if scope == "graph":
        return {"hits": _graph_search(repo, q, limit)}
    coll = COLLECTIONS.get(scope)
    if not coll:
        raise HTTPException(400, "scope must be code, commits, insights or graph")
    if mode not in ("hybrid", "dense", "sparse"):
        raise HTTPException(400, "mode must be hybrid, dense or sparse")
    client, emb = runtime.vectors()
    if not client.collection_exists(coll):
        return {"hits": [], "note": "This collection hasn't been built yet."}
    flt: dict[str, Any] = {"repo": repo}
    if alive is not None and coll == "code_symbols":
        flt["alive"] = alive
    try:
        points = vsearch(client, emb, coll, q, mode, flt, limit)
    except Exception as e:
        raise HTTPException(502, f"vector search failed: {e}") from e
    return {"hits": [_hit(p) for p in points]}


def _hit(p) -> dict:
    payload = dict(p.payload or {})
    text = str(payload.pop("text", ""))
    uid = payload.get("neo4j_id") or ""
    title = payload.get("qualname") or payload.get("title") or (
        f"{str(payload.get('commit_hash', ''))[:10]} {text.splitlines()[1] if len(text.splitlines()) > 1 else ''}"
        if payload.get("commit_hash") else uid)
    sub = payload.get("path") or ", ".join((payload.get("paths") or [])[:3]) or payload.get("kind") or ""
    return {"uid": uid, "group": LABEL_BY_PREFIX.get(uid.split(":", 1)[0], "Node"), "title": str(title)[:160],
            "subtitle": str(sub), "score": round(float(p.score), 4), "snippet": text[:700], "payload": payload}


def _graph_search(repo: str, q: str, limit: int) -> list[dict]:
    needle = q.strip().lower()
    per = max(5, limit // 4)
    rows: list[list[Any]] = []
    rows += _q("MATCH (n:Symbol {repo: $repo}) WHERE toLower(n.name) CONTAINS $q "
               "RETURN n ORDER BY n.alive DESC, size(n.name) LIMIT $per", {"repo": repo, "q": needle, "per": per})
    rows += _q("MATCH (n:File {repo: $repo}) WHERE toLower(n.path) CONTAINS $q "
               "RETURN n ORDER BY n.alive DESC, size(n.path) LIMIT $per", {"repo": repo, "q": needle, "per": per})
    rows += _q("MATCH (n:Branch {repo: $repo}) WHERE toLower(n.name) CONTAINS $q RETURN n LIMIT $per",
               {"repo": repo, "q": needle, "per": per // 2})
    rows += _q("MATCH (a:Author)-[:AUTHORED]->(:Commit {repo: $repo}) WHERE toLower(a.name) CONTAINS $q "
               "OR toLower(a.email) CONTAINS $q RETURN DISTINCT a LIMIT $per", {"repo": repo, "q": needle, "per": per // 2})
    if needle.isalnum() and len(needle) >= 6:  # looks like a commit hash prefix
        rows += _q("MATCH (n:Commit {repo: $repo}) WHERE n.hash STARTS WITH $q RETURN n LIMIT 5", {"repo": repo, "q": needle})
    return [node_view(r[0]) for r in rows][:limit]


# ---------------------------------------------------------------------------- nodes
@router.get("/node")
def node(repo: str, uid: str) -> dict:
    _repo(repo)
    label = _label(uid)
    rows = _q(f"MATCH (n:{label} {{uid: $uid}}) RETURN n", {"uid": uid}, 1)
    if not rows:
        raise HTTPException(404, f"no node {uid}")
    degrees = _q(f"MATCH (n:{label} {{uid: $uid}})-[r]-(m) RETURN type(r), startNode(r) = n, labels(m)[0], count(*) "
                 "ORDER BY count(*) DESC", {"uid": uid}, 200)
    out = node_view(rows[0][0])
    out["degrees"] = [{"type": t, "dir": "out" if is_out else "in", "other": other, "count": n}
                      for t, is_out, other, n in degrees]
    out["has_vector"] = uid.split(":", 1)[0] in COLLECTION_BY_PREFIX
    return out


@router.get("/neighbors")
def neighbors(repo: str, uid: str, type: str | None = None, dir: str | None = None,
              limit: int = Query(12, ge=1, le=200)) -> dict:
    """Neighbours of a node: one edge type (``type``/``dir``) or, without ``type``, up to ``limit`` per edge type."""
    _repo(repo)
    label = _label(uid)
    if type is not None and not type.replace("_", "").isalnum():
        raise HTTPException(400, "bad edge type")
    groups = [(type, dir)] if type else list(dict.fromkeys((d["type"], d["dir"]) for d in node(repo, uid)["degrees"]))
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for t, d in groups:
        pat = f"-[r:{t}]->" if d == "out" else f"<-[r:{t}]-" if d == "in" else f"-[r:{t}]-"
        order = "ORDER BY r.jaccard DESC" if t == "CO_COMMITTED" else \
            "ORDER BY m.ts DESC" if t in ("TOUCHED", "AUTHORED") and d == "in" and label == "File" else ""
        rows = _q(f"MATCH (n:{label} {{uid: $uid}}){pat}(m) WITH r, m {order} LIMIT $lim "
                  "RETURN m, type(r), startNode(r).uid, endNode(r).uid, properties(r)",
                  {"uid": uid, "lim": limit}, limit)
        for m, rt, src, dst, rp in rows:
            v = node_view(m)
            nodes.setdefault(v["uid"], v)
            edges.append({"source": src, "target": dst, "type": rt,
                          "props": {k: x for k, x in (rp or {}).items() if not isinstance(x, (list, dict))}})
    return {"nodes": list(nodes.values()), "edges": edges}


# ---------------------------------------------------------------------------- vectors
def _point_for(uid: str, with_vector: bool = True):
    coll = COLLECTION_BY_PREFIX.get(uid.split(":", 1)[0])
    if not coll:
        raise HTTPException(400, "only symbols, commits and insights have vectors")
    client = runtime.qdrant()
    pts, _ = client.scroll(coll, scroll_filter=models.Filter(must=[
        models.FieldCondition(key="neo4j_id", match=models.MatchValue(value=uid))]),
        limit=1, with_payload=True, with_vectors=["dense"] if with_vector else False)
    if not pts:
        raise HTTPException(404, "this node has no stored vector (it may not have been embedded)")
    return client, coll, pts[0]


@router.get("/similar")
def similar(repo: str, uid: str, limit: int = Query(15, ge=1, le=50)) -> dict:
    _repo(repo)
    client, coll, pt = _point_for(uid)
    vec = pt.vector["dense"] if isinstance(pt.vector, dict) else pt.vector
    res = client.query_points(coll, query=vec, using="dense", limit=limit + 1, with_payload=True,
                              query_filter=models.Filter(must=[models.FieldCondition(
                                  key="repo", match=models.MatchValue(value=repo))]))
    return {"hits": [_hit(p) for p in res.points if (p.payload or {}).get("neo4j_id") != uid][:limit]}


_MAPS: dict[tuple[str, str], dict] = {}
_MAP_LOCK = threading.Lock()


def _describe(payload: dict) -> dict:
    return _hit(type("P", (), {"payload": payload, "score": 0.0})())


@router.get("/map")
def vector_map(repo: str, collection: str = "code", q: str | None = None, refresh: bool = False) -> dict:
    """Topic map of a collection: clustered, labelled islands; with ``q``, where the query lands and its top hits."""
    import numpy as np

    from . import topicmap

    _repo(repo)
    coll = COLLECTIONS.get(collection)
    if not coll:
        raise HTTPException(400, "collection must be code, commits or insights")
    key = (repo, coll)
    with _MAP_LOCK:
        m = _MAPS.get(key)
        if m is None or refresh or time.time() - m.get("built", 0) > MAP_TTL_S:
            try:
                m = _MAPS[key] = topicmap.build(runtime.qdrant(), repo, coll, _describe)
            except Exception as e:
                raise HTTPException(502, f"could not build the map: {e}") from e
    out = {k: m.get(k) for k in ("points", "clusters", "total", "sampled", "skipped", "note")}
    out["collection"] = coll
    if q and m["points"]:
        _, emb = runtime.vectors()
        out["query"] = topicmap.place_query(m, np.asarray(emb.dense_query(q), dtype=np.float32))
        out["hits"] = [h["uid"] for h in search(repo, q, collection, "dense", None, 15)["hits"]]
    return out
