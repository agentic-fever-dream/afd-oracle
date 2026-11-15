"""Project the SQLite store into Neo4j with batched ``UNWIND ... MERGE`` writes.

Commits / authors / TOUCHED edges are projected incrementally (``commits.projected``); derived layers (symbols,
structural edges, reverts, branches, co-change, metrics) are replaced wholesale per repo because they are cheap to
rewrite and are recomputed by every ingest.
"""
from __future__ import annotations

import json

import neo4j

from afd_oracle_core.graph import write_batches
from afd_oracle_core.ids import author_uid, branch_uid, commit_uid, dir_uid, file_uid, repo_uid
from afd_oracle_core.store import Store, loads

KIND_LABEL = {"class": "Class", "interface": "Interface", "function": "Function", "variable": "Variable"}


def _dirs(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def project(store: Store, driver: neo4j.Driver, database: str, git_path: str = "") -> dict:
    repo = store.repo_id
    ruid = repo_uid(repo)
    stats: dict[str, int] = {}
    with driver.session(database=database) as s:
        def wb(query: str, rows: list[dict], **kw) -> int:  # every batch gets the repo scope params
            return write_batches(s, query, rows, cp=f"commit:{repo}:", repo=repo, **kw)

        s.run("MERGE (r:Repository {uid:$uid}) SET r.name=$repo, r.repo=$repo, r.path=$path, "
              "r.default_branch=$db, r.head=$head",
              uid=ruid, repo=repo, path=git_path, db=store.get_meta("default_branch"), head=store.get_meta("head")).consume()

        # -- directories and files ----------------------------------------------------
        files = [dict(r) for r in store.q("SELECT * FROM files")]
        dirs = sorted({d for f in files for d in _dirs(f["path"])})
        wb("UNWIND $rows AS row MERGE (d:Directory {uid: row.uid}) "
                         "SET d.repo=row.repo, d.path=row.path, d.name=row.name",
                      [{"uid": dir_uid(repo, d), "repo": repo, "path": d, "name": d.rsplit("/", 1)[-1]} for d in dirs])
        wb("UNWIND $rows AS row MATCH (r:Repository {uid: $ruid}) MATCH (d:Directory {uid: row.uid}) "
                         "MERGE (r)-[:CONTAINS]->(d)",
                      [{"uid": dir_uid(repo, d)} for d in dirs if "/" not in d], ruid=ruid)
        wb("UNWIND $rows AS row MATCH (p:Directory {uid: row.p}) MATCH (c:Directory {uid: row.c}) "
                         "MERGE (p)-[:CONTAINS]->(c)",
                      [{"p": dir_uid(repo, d.rsplit("/", 1)[0]), "c": dir_uid(repo, d)} for d in dirs if "/" in d])
        file_rows = []
        for f in files:
            m = loads(f["metrics"], {}) or {}
            file_rows.append({"uid": file_uid(repo, f["path"]), "props": {
                "repo": repo, "path": f["path"], "name": f["path"].rsplit("/", 1)[-1], "lang": f["lang"],
                "alive": bool(f["alive"]), "loc": f["loc"], "churn": f["churn"], "commits": f["commits"],
                "first_seen": f["first_seen"], "last_seen": f["last_seen"], "lineage": f["lineage"],
                "hotspot": m.get("hotspot"), "cycle": m.get("cycle")}})
        stats["files"] = wb("UNWIND $rows AS row MERGE (f:File {uid: row.uid}) SET f += row.props",
                                       file_rows)
        wb("UNWIND $rows AS row MATCH (d:Directory {uid: row.d}) MATCH (f:File {uid: row.f}) "
                         "MERGE (d)-[:CONTAINS]->(f)",
                      [{"d": dir_uid(repo, f["path"].rsplit("/", 1)[0]), "f": file_uid(repo, f["path"])}
                       for f in files if "/" in f["path"]])
        wb("UNWIND $rows AS row MATCH (r:Repository {uid: $ruid}) MATCH (f:File {uid: row.f}) "
                         "MERGE (r)-[:CONTAINS]->(f)",
                      [{"f": file_uid(repo, f["path"])} for f in files if "/" not in f["path"]], ruid=ruid)

        # -- commits, authors, parents, file TOUCHED (incremental) ----------------------
        new = [dict(r) for r in store.q("SELECT * FROM commits WHERE projected=0 ORDER BY seq")]
        stats["commits"] = wb("""
            UNWIND $rows AS row
            MERGE (a:Author {uid: row.author_uid}) SET a.email = row.email, a.name = row.name
            MERGE (c:Commit {uid: $cp + row.hash})
            SET c.uid = row.uid, c.repo = row.repo, c.short = row.short, c.message = row.message, c.ts = row.ts,
                c.seq = row.seq, c.is_merge = row.is_merge, c.is_bulk = row.is_bulk,
                c.files_changed = row.files_changed
            MERGE (a)-[:AUTHORED]->(c)""",
            [{"author_uid": author_uid(c["author_email"]), "email": c["author_email"], "name": c["author_name"],
              "hash": c["hash"], "uid": commit_uid(repo, c["hash"]), "repo": repo, "short": c["hash"][:10],
              "message": c["message"], "ts": c["ts"], "seq": c["seq"], "is_merge": bool(c["is_merge"]),
              "is_bulk": bool(c["is_bulk"]), "files_changed": c["files_changed"]} for c in new])
        wb("UNWIND $rows AS row MATCH (c:Commit {uid: $cp + row.child}) MERGE (p:Commit {uid: $cp + row.parent}) SET p.hash = row.parent, p.repo = $repo "
                         "MERGE (c)-[:PARENT {idx: row.idx}]->(p)",
                      [{"child": c["hash"], "parent": p, "idx": i} for c in new
                       for i, p in enumerate(json.loads(c["parents"]))])
        new_hashes = [c["hash"] for c in new]
        touched = []
        for i in range(0, len(new_hashes), 500):
            chunk = new_hashes[i:i + 500]
            touched += [dict(r) for r in store.q(
                f"SELECT fc.*, c.ts FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
                f"WHERE fc.ignored=0 AND fc.hash IN ({','.join('?' * len(chunk))})", chunk)]
        stats["touched_files"] = wb("""
            UNWIND $rows AS row MATCH (c:Commit {uid: $cp + row.hash}) MATCH (f:File {uid: row.file})
            MERGE (c)-[t:TOUCHED]->(f)
            SET t.change = row.change, t.added = row.added, t.deleted = row.deleted, t.old_path = row.old_path,
                t.ts = row.ts""",
            [{"hash": t["hash"], "file": file_uid(repo, t["path"]), "change": t["status"], "added": t["added"],
              "deleted": t["deleted"], "old_path": t["old_path"], "ts": t["ts"]} for t in touched])

        # -- symbols (all; cheap) + DEFINES ---------------------------------------------
        syms = [dict(r) for r in store.q("SELECT * FROM symbols")]
        total = 0
        for kind, label in KIND_LABEL.items():
            rows = []
            for r in syms:
                if r["kind"] != kind:
                    continue
                m = loads(r["metrics"], {}) or {}
                rows.append({"uid": r["uid"], "props": {
                    "repo": repo, "path": r["path"], "name": r["name"], "qualname": r["qualname"], "kind": kind,
                    "lang": r["lang"], "alive": bool(r["alive"]), "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"], "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                    "start_line": r["start_line"], "end_line": r["end_line"], "body_hash": r["body_hash"],
                    "loc": r["loc"], "signature": r["signature"], "docstring": r["docstring"],
                    "old_uid": r["old_uid"], "modifications": r["modifications"], **m}})
            total += wb(f"UNWIND $rows AS row MERGE (s:Symbol {{uid: row.uid}}) SET s:{label}, "
                                      "s += row.props", rows)
        stats["symbols"] = total
        s.run("MATCH (:File {repo:$repo})-[d:DEFINES]->() DELETE d", repo=repo).consume()
        s.run("MATCH (:Symbol {repo:$repo})-[d:DEFINES]->() DELETE d", repo=repo).consume()
        defines = [{"src": file_uid(repo, r["path"]), "dst": r["uid"], "since": r["first_seen"],
                    "until": None if r["alive"] else r["last_seen"]} for r in syms if not r["parent"]]
        wb("UNWIND $rows AS row MATCH (f:File {uid: row.src}) MATCH (s:Symbol {uid: row.dst}) "
                         "MERGE (f)-[d:DEFINES]->(s) SET d.since=row.since, d.until=row.until", defines)
        members = [{"src": r["parent"], "dst": r["uid"], "since": r["first_seen"],
                    "until": None if r["alive"] else r["last_seen"]} for r in syms if r["parent"]]
        wb("UNWIND $rows AS row MATCH (p:Symbol {uid: row.src}) MATCH (s:Symbol {uid: row.dst}) "
                         "MERGE (p)-[d:DEFINES]->(s) SET d.since=row.since, d.until=row.until", members)

        # -- symbol TOUCHED (incremental) ---------------------------------------------------
        events = []
        for i in range(0, len(new_hashes), 500):
            chunk = new_hashes[i:i + 500]
            events += [dict(r) for r in store.q(
                f"SELECT * FROM symbol_events WHERE hash IN ({','.join('?' * len(chunk))})", chunk)]
        stats["touched_symbols"] = wb("""
            UNWIND $rows AS row MATCH (c:Commit {uid: $cp + row.hash}) MATCH (s:Symbol {uid: row.uid})
            MERGE (c)-[t:TOUCHED]->(s)
            SET t.change = row.change, t.body_hash_before = row.before, t.body_hash_after = row.after,
                t.old_uid = row.old_uid, t.ts = row.ts""",
            [{"hash": e["hash"], "uid": e["uid"], "change": e["change"], "before": e["body_hash_before"],
              "after": e["body_hash_after"], "old_uid": e["old_uid"], "ts": e["ts"]} for e in events])

        # -- structural edges (replace) -----------------------------------------------------
        s.run("MATCH (:Symbol {repo:$repo})-[r:CALLS|INHERITS|IMPLEMENTS]->() DELETE r", repo=repo).consume()
        s.run("MATCH (:File {repo:$repo})-[r:IMPORTS]->() DELETE r", repo=repo).consume()
        edges = [dict(r) for r in store.q("SELECT * FROM edges")]
        for kind in ("CALLS", "INHERITS", "IMPLEMENTS"):
            rows = [{"src": e["src"], "dst": e["dst"], "props": json.loads(e["props"])} for e in edges if e["kind"] == kind]
            stats[kind.lower()] = write_batches(
                s, f"UNWIND $rows AS row MATCH (a:Symbol {{uid: row.src}}) MATCH (b:Symbol {{uid: row.dst}}) "
                   f"MERGE (a)-[r:{kind}]->(b) SET r += row.props", rows)
        rows = [{"src": file_uid(repo, e["src"]), "dst": file_uid(repo, e["dst"]), "props": json.loads(e["props"])}
                for e in edges if e["kind"] == "IMPORTS"]
        stats["imports"] = wb("UNWIND $rows AS row MATCH (a:File {uid: row.src}) "
                                            "MATCH (b:File {uid: row.dst}) MERGE (a)-[r:IMPORTS]->(b) SET r += row.props",
                                         rows)

        # -- reverts + revert flags (replace) --------------------------------------------------
        s.run("MATCH (c:Commit {repo:$repo})-[r:REVERTS]->() DELETE r", repo=repo).consume()
        s.run("MATCH (c:Commit {repo:$repo}) SET c.is_revert = false", repo=repo).consume()
        wb("UNWIND $rows AS row MATCH (c:Commit {uid: $cp + row.h}) SET c.is_revert = true",
                      [{"h": r["hash"]} for r in store.q("SELECT hash FROM commits WHERE is_revert=1")])
        stats["reverts"] = wb("""
            UNWIND $rows AS row MATCH (a:Commit {uid: $cp + row.a}) MATCH (b:Commit {uid: $cp + row.b})
            MERGE (a)-[r:REVERTS]->(b) SET r.method = row.method, r.partial = row.partial, r.overlap = row.overlap""",
            [{"a": r["reverter"], "b": r["reverted"], "method": r["method"], "partial": bool(r["partial"]),
              "overlap": r["overlap"]} for r in store.q("SELECT * FROM reverts")])

        # -- branches (replace) --------------------------------------------------------------
        s.run("MATCH (b:Branch {repo:$repo}) DETACH DELETE b", repo=repo).consume()
        branches = [dict(r) for r in store.q("SELECT * FROM branches")]
        stats["branches"] = wb("""
            UNWIND $rows AS row MERGE (b:Branch {uid: row.uid}) SET b += row.props
            WITH b, row WHERE row.tip IS NOT NULL
            MATCH (c:Commit {uid: $cp + row.tip}) MERGE (b)-[:POINTS_TO]->(c)""",
            [{"uid": branch_uid(repo, b["name"]), "tip": b["tip"], "props": {
                "repo": repo, "name": b["name"], "ref": b["ref"], "merged": bool(b["merged"]),
                "abandoned": bool(b["abandoned"]), "tip_ts": b["tip_ts"], "unique_commits": b["unique_count"],
                "source": b["source"], "merge_commit": b["merge_commit"]}} for b in branches])
        wb("UNWIND $rows AS row MATCH (b:Branch {uid: row.b}) MATCH (c:Commit {uid: $cp + row.h}) "
                         "MERGE (c)-[:UNIQUE_TO]->(b)",
                      [{"b": branch_uid(repo, r["branch"]), "h": r["hash"]} for r in store.q("SELECT * FROM branch_commits")])

        # -- co-change (replace) -------------------------------------------------------------
        s.run("MATCH (:File {repo:$repo})-[r:CO_COMMITTED]-() DELETE r", repo=repo).consume()
        stats["co_committed"] = wb("""
            UNWIND $rows AS row MATCH (a:File {uid: row.a}) MATCH (b:File {uid: row.b})
            MERGE (a)-[r:CO_COMMITTED]->(b) SET r += row.props""",
            [{"a": file_uid(repo, r["a"]), "b": file_uid(repo, r["b"]), "props": {
                "count": r["count"], "jaccard": r["jaccard"], "conf_ab": r["conf_ab"], "conf_ba": r["conf_ba"],
                "cross_dir": bool(r["cross_dir"]), "lca_depth": r["lca_depth"],
                "structural_link": bool(r["structural_link"]), "last_ts": r["last_ts"], "score": r["score"]}}
             for r in store.q("SELECT * FROM cochange")])

        s.run("MERGE (st:IngestState {repo:$repo}) SET st.last_walk_ts = timestamp(), st.processed_tips = $tips, "
              "st.head = $head", repo=repo, tips=json.loads(store.get_meta("processed_tips", "[]")),
              head=store.get_meta("head")).consume()

    with store.tx() as conn:
        conn.executemany("UPDATE commits SET projected=1 WHERE hash=?", [(h,) for h in new_hashes])
    return stats
