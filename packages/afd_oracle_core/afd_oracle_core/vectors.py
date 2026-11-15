"""Qdrant collections, upserts and hybrid (dense + BM25, RRF-fused) search."""
from __future__ import annotations

import warnings
from typing import Any

from qdrant_client import QdrantClient, models

from .config import Settings, get_settings
from .embedding import Embedder, Sparse
from .ids import point_id

CODE_SYMBOLS = "code_symbols"
COMMIT_DIFFS = "commit_diffs"
INSIGHTS = "architectural_insights"
SECURITY = "security_findings"  # vulnerability scanner findings (afd_oracle_ingest.scan)
COLLECTIONS = (CODE_SYMBOLS, COMMIT_DIFFS, INSIGHTS, SECURITY)

K = models.PayloadSchemaType.KEYWORD
B = models.PayloadSchemaType.BOOL
I = models.PayloadSchemaType.INTEGER

# Mirrors infra/qdrant/collections.json (tests/unit/test_collections_json.py keeps them in sync).
PAYLOAD_INDEXES: dict[str, dict[str, models.PayloadSchemaType]] = {
    CODE_SYMBOLS: {"neo4j_id": K, "repo": K, "path": K, "path_prefixes": K, "kind": K, "lang": K, "alive": B,
                   "last_seen_commit": K},
    COMMIT_DIFFS: {"neo4j_id": K, "commit_hash": K, "repo": K, "ts": I, "is_revert": B, "is_merge": B, "paths": K,
                   "path_prefixes": K, "author": K},
    INSIGHTS: {"neo4j_id": K, "repo": K, "label": K, "kind": K, "severity": K, "status": K, "commit_hashes": K,
               "symbol_ids": K, "paths": K, "path_prefixes": K},
    SECURITY: {"neo4j_id": K, "repo": K, "scanner": K, "severity": K, "rule": K, "path": K, "package": K, "cve": K,
               "kind": K},
}


def get_client(settings: Settings | None = None) -> QdrantClient:
    s = settings or get_settings()
    if s.qdrant_path:
        return QdrantClient(location=":memory:") if s.qdrant_path == ":memory:" else QdrantClient(path=s.qdrant_path)
    import warnings
    with warnings.catch_warnings():  # the server is on loopback by default; the key is a local-only guard
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None, timeout=60)


def ensure_collections(client: QdrantClient, dim: int) -> list[str]:
    created = []
    for name in COLLECTIONS:
        if not client.collection_exists(name):
            client.create_collection(
                name,
                vectors_config={"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)},
                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            created.append(name)
        else:
            existing = client.get_collection(name).config.params.vectors
            size = existing["dense"].size if isinstance(existing, dict) else existing.size
            if size != dim:
                raise ValueError(f"Collection {name} has dense size {size}, embedder produces {dim}. "
                                 "Recreate the collection or change AFD_ORACLE_EMBED_MODEL/AFD_ORACLE_EMBED_DIM.")
        for field, schema in PAYLOAD_INDEXES[name].items():
            try:
                with warnings.catch_warnings():  # embedded mode ignores payload indexes and warns about it
                    warnings.simplefilter("ignore")
                    client.create_payload_index(name, field_name=field, field_schema=schema)
            except Exception:  # already exists
                pass
    return created


def _sparse(s: Sparse) -> models.SparseVector:
    return models.SparseVector(indices=s.indices, values=s.values)


def _usable(v: list[float] | None) -> bool:
    """A dense vector Qdrant can use for cosine search: present, finite and not all zeros.

    Qdrant normalises cosine vectors on write, so a NaN or zero vector is silently stored as all zeros and every
    semantic query then scores it 0. Catch it here rather than discovering it later as "meaning search finds nothing".
    """
    if not v:
        return False
    norm = 0.0
    for x in v:
        if x != x or x in (float("inf"), float("-inf")):
            return False
        norm += x * x
    return norm > 1e-12


def upsert_texts(client: QdrantClient, embedder: Embedder, collection: str,
                 items: list[tuple[str, str, dict[str, Any]]], batch: int = 64, progress=None) -> int:
    """items: (uid, text, payload). Point id is derived from uid; payload gets ``neo4j_id=uid``.
    ``progress(done, total)`` is called after each batch."""
    n = 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        texts = [t for _, t, _ in chunk]
        dense = embedder.dense(texts)
        for j, d in enumerate(dense):
            if not _usable(d):  # retry alone (a padded batch can produce NaN), then refuse to store garbage
                dense[j] = embedder.dense([texts[j]])[0]
                if not _usable(dense[j]):
                    raise ValueError(f"embedder returned an unusable vector for {chunk[j][0]} in {collection}")
        sparse = embedder.sparse(texts)
        points = [
            models.PointStruct(id=point_id(uid), vector={"dense": d, "bm25": _sparse(s)},
                               payload={**payload, "neo4j_id": uid, "text": text[:4000]})
            for (uid, text, payload), d, s in zip(chunk, dense, sparse)
        ]
        client.upsert(collection, points=points, wait=True)
        n += len(points)
        if progress:
            progress(n, len(items))
    return n


def build_filter(f: dict[str, Any] | None) -> models.Filter | None:
    if not f:
        return None
    must: list[models.Condition] = []
    for key in ("repo", "kind", "commit_hash", "status", "label", "severity", "lang"):
        if f.get(key) is not None:
            field = "commit_hashes" if (key == "commit_hash" and f.get("_collection") == INSIGHTS) else key
            must.append(models.FieldCondition(key=field, match=models.MatchValue(value=f[key])))
    if f.get("alive") is not None:
        must.append(models.FieldCondition(key="alive", match=models.MatchValue(value=bool(f["alive"]))))
    if f.get("path_prefix"):
        must.append(models.FieldCondition(key="path_prefixes",
                                          match=models.MatchValue(value=str(f["path_prefix"]).rstrip("/"))))
    if f.get("ts_from") is not None or f.get("ts_to") is not None:
        must.append(models.FieldCondition(key="ts", range=models.Range(gte=f.get("ts_from"), lte=f.get("ts_to"))))
    return models.Filter(must=must) if must else None


def search(client: QdrantClient, embedder: Embedder, collection: str, query: str, mode: str = "hybrid",
           flt: dict[str, Any] | None = None, limit: int = 10) -> list[models.ScoredPoint]:
    qf = build_filter({**(flt or {}), "_collection": collection})
    if mode == "dense":
        res = client.query_points(collection, query=embedder.dense_query(query), using="dense",
                                  query_filter=qf, limit=limit, with_payload=True)
    elif mode == "sparse":
        res = client.query_points(collection, query=_sparse(embedder.sparse_query(query)), using="bm25",
                                  query_filter=qf, limit=limit, with_payload=True)
    else:
        res = client.query_points(
            collection,
            prefetch=[
                models.Prefetch(query=embedder.dense_query(query), using="dense", filter=qf, limit=limit * 4),
                models.Prefetch(query=_sparse(embedder.sparse_query(query)), using="bm25", filter=qf, limit=limit * 4),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit, with_payload=True,
        )
    return res.points


def scroll_ids(client: QdrantClient, collection: str, flt: dict[str, Any] | None = None) -> set[str]:
    out: set[str] = set()
    offset = None
    while True:
        pts, offset = client.scroll(collection, scroll_filter=build_filter(flt), limit=512, offset=offset,
                                    with_payload=["neo4j_id"])
        out.update(p.payload["neo4j_id"] for p in pts if p.payload)
        if offset is None:
            return out
