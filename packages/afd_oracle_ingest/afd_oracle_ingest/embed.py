"""Step 10: embed new/changed symbols and new commits into Qdrant; derive idiom clusters from near-duplicates."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

import pygit2
from qdrant_client import QdrantClient, models

from afd_oracle_core.config import Settings
from afd_oracle_core.embedding import Embedder
from afd_oracle_core.ids import commit_uid, path_prefixes, point_id, short_hash
from afd_oracle_core.store import Store
from afd_oracle_core.vectors import CODE_SYMBOLS, COMMIT_DIFFS, ensure_collections, upsert_texts

from .git.walker import hunk_headers

EMBED_KINDS = ("function", "class", "interface")


def symbol_text(r) -> str:
    parts = [f"{r['kind']} {r['qualname']}  ({r['path']})", r["signature"] or ""]
    if r["docstring"]:
        parts.append(r["docstring"])
    parts.append(r["body"] or "")
    return "\n".join(parts)[:6000]


def commit_text(repo: pygit2.Repository, store: Store, c) -> tuple[str, list[str]]:
    date = datetime.fromtimestamp(c["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    lines = [f"commit {c['hash'][:10]} by {c['author_email']} on {date}", (c["message"] or "").strip()[:1500], "Files:"]
    paths = []
    for fc in store.q("SELECT path, old_path, status, added, deleted FROM file_changes WHERE hash=? AND ignored=0 "
                      "ORDER BY path LIMIT 60", (c["hash"],)):
        paths.append(fc["path"])
        ren = f" (from {fc['old_path']})" if fc["old_path"] else ""
        lines.append(f"  {fc['status']} {fc['path']}{ren} (+{fc['added']}/-{fc['deleted']})")
    ev: dict[str, list[str]] = defaultdict(list)
    for e in store.q("SELECT change, qualname FROM symbol_events WHERE hash=? LIMIT 80", (c["hash"],)):
        ev[e["change"]].append(e["qualname"])
    if ev:
        lines.append("Symbols:")
        lines += [f"  {k}: {', '.join(v[:20])}" for k, v in sorted(ev.items())]
    if not c["is_merge"]:
        hh = hunk_headers(repo, c["hash"], limit=20)
        if hh:
            lines.append("Hunks:")
            lines += [f"  {h}" for h in hh]
    return "\n".join(lines), paths


def embed_all(repo: pygit2.Repository, store: Store, settings: Settings, client: QdrantClient,
              embedder: Embedder, progress=None) -> dict:
    """``progress(step, done, total)`` reports embed_symbols / embed_commits batches."""
    ensure_collections(client, embedder.dim)
    repo_id = store.repo_id
    sym_items, sym_marks = [], []
    # Alive symbols, plus dead ones that are graveyard evidence. Other historical versions stay reachable via
    # get_symbol_history; embedding every dead version (often 30x the alive set) would dominate ingest time.
    rows = store.q(
        f"""SELECT * FROM symbols WHERE kind IN ({','.join('?' * len(EMBED_KINDS))})
            AND (alive = 1 OR uid IN (SELECT subject FROM candidates
                                      WHERE kind IN ('short_lived_symbol', 'churned_deleted')))""", EMBED_KINDS)
    for r in rows:
        mark = f"{r['body_hash']}:{r['alive']}"
        if r["embedded_hash"] == mark or not r["body"]:
            continue
        sym_items.append((r["uid"], symbol_text(r), {
            "repo": repo_id, "path": r["path"], "path_prefixes": path_prefixes(r["path"]), "kind": r["kind"],
            "lang": r["lang"], "alive": bool(r["alive"]), "qualname": r["qualname"],
            "first_seen_commit": r["first_seen"], "last_seen_commit": r["last_seen"], "loc": r["loc"]}))
        sym_marks.append((mark, r["uid"]))
    n_sym = upsert_texts(client, embedder, CODE_SYMBOLS, sym_items, settings.embed_batch,
                         progress=(lambda d, t: progress("embed_symbols", d, t)) if progress else None)
    with store.tx() as conn:
        conn.executemany("UPDATE symbols SET embedded_hash=? WHERE uid=?", sym_marks)

    commit_items, done = [], []
    for c in store.q("SELECT * FROM commits WHERE embedded=0 ORDER BY seq"):
        text, paths = commit_text(repo, store, c)
        prefixes = sorted({p for path in paths for p in path_prefixes(path)})
        commit_items.append((commit_uid(repo_id, c["hash"]), text, {
            "repo": repo_id, "commit_hash": c["hash"], "ts": c["ts"], "is_revert": bool(c["is_revert"]),
            "is_merge": bool(c["is_merge"]), "paths": paths, "path_prefixes": prefixes, "author": c["author_email"]}))
        done.append((c["hash"],))
    n_commits = upsert_texts(client, embedder, COMMIT_DIFFS, commit_items, settings.embed_batch,
                             progress=(lambda d, t: progress("embed_commits", d, t)) if progress else None)
    with store.tx() as conn:
        conn.executemany("UPDATE commits SET embedded=1 WHERE hash=?", done)
    idioms = idiom_clusters(store, client)
    return {"symbols": n_sym, "commits": n_commits, "idiom_clusters": idioms}


def idiom_clusters(store: Store, client: QdrantClient, threshold: float = 0.9, min_dirs: int = 3,
                   max_symbols: int = 2000) -> int:
    """Near-duplicate alive functions spread over >= min_dirs directories -> 'idiom_cluster' candidates."""
    rows = store.q("SELECT uid, path, qualname, signature FROM symbols WHERE alive=1 AND kind='function' AND loc >= 5 "
                   "ORDER BY loc DESC LIMIT ?", (max_symbols,))
    info = {r["uid"]: r for r in rows}
    parent = {u: u for u in info}

    def find(u: str) -> str:
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u

    flt = models.Filter(must=[models.FieldCondition(key="repo", match=models.MatchValue(value=store.repo_id)),
                              models.FieldCondition(key="alive", match=models.MatchValue(value=True)),
                              models.FieldCondition(key="kind", match=models.MatchValue(value="function"))])
    for uid in info:
        try:
            res = client.query_points(CODE_SYMBOLS, query=point_id(uid), using="dense", query_filter=flt, limit=10,
                                      score_threshold=threshold, with_payload=["neo4j_id"])
        except Exception:
            continue
        for p in res.points:
            other = p.payload.get("neo4j_id") if p.payload else None
            if other in info and other != uid:
                parent[find(other)] = find(uid)
    groups: dict[str, list[str]] = defaultdict(list)
    for u in info:
        groups[find(u)].append(u)
    cands = []
    for members in groups.values():
        dirs = {info[m]["path"].rsplit("/", 1)[0] if "/" in info[m]["path"] else "" for m in members}
        if len(members) >= 3 and len(dirs) >= min_dirs:
            members.sort()
            props = {"members": [{"uid": m, "path": info[m]["path"], "signature": info[m]["signature"]}
                                 for m in members[:25]], "size": len(members), "directories": sorted(dirs)}
            cands.append((f"idiom:{short_hash(*members)}", "idiom_cluster", members[0], float(len(members)),
                          json.dumps(props)))
    with store.tx() as conn:
        conn.execute("DELETE FROM candidates WHERE kind='idiom_cluster'")
        conn.executemany("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?)", cands)
    return len(cands)
