"""Insight lifecycle: proposed -> verified | rejected.

The per-repo SQLite store is the system of record (so the gate works and is testable without a graph server);
every write is projected to Neo4j (``:Insight`` + ``:Pattern|:AntiPattern|:RejectedAttempt``, ``EVIDENCED_BY``,
``CONCERNS``) and verified insights are embedded into the ``architectural_insights`` Qdrant collection.
If Neo4j is unreachable the write still succeeds with ``graph_synced=false``; ``afd-oracle sync-insights`` replays it.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import neo4j
import pygit2
from qdrant_client import QdrantClient

from .citations import validate_citations
from .embedding import Embedder
from .ids import file_uid, new_insight_uid, path_prefixes, point_id
from .store import Store, loads
from .vectors import INSIGHTS, ensure_collections, upsert_texts

LABELS = ("Pattern", "AntiPattern", "RejectedAttempt")
SEVERITIES = ("info", "low", "medium", "high")
STATUSES = ("proposed", "verified", "rejected")


class InsightError(ValueError):
    pass


def _row_to_dict(r) -> dict:
    d = dict(r)
    d["citations"] = loads(d.get("citations"), {})
    d["votes"] = loads(d.get("votes"), [])
    d["insight_id"] = d.pop("uid")
    d["graph_synced"] = bool(d.get("graph_synced"))
    return d


class InsightService:
    def __init__(self, store: Store, repo: pygit2.Repository, database: str = "neo4j",
                 driver: Callable[[], neo4j.Driver | None] | None = None,
                 vectors: Callable[[], tuple[QdrantClient, Embedder] | None] | None = None):
        self.store = store
        self.repo = repo
        self.database = database
        self._driver = driver or (lambda: None)
        self._vectors = vectors or (lambda: None)

    # ------------------------------------------------------------------ queries
    def get(self, uid: str) -> dict | None:
        r = self.store.q1("SELECT * FROM insights WHERE uid=?", (uid,))
        return _row_to_dict(r) if r else None

    def list(self, status: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql, params = "SELECT * FROM insights WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [_row_to_dict(r) for r in self.store.q(sql, params)]

    # ------------------------------------------------------------------ writes
    def upsert(self, *, label: str, kind: str, title: str, summary: str, severity: str, confidence: float,
               citations: dict[str, Any], dedupe_key: str | None = None, workflow_run: str | None = None,
               model: str | None = None) -> dict:
        problems = []
        if label not in LABELS:
            problems.append({"field": "label", "value": label, "reason": f"must be one of {list(LABELS)}"})
        if severity not in SEVERITIES:
            problems.append({"field": "severity", "value": severity, "reason": f"must be one of {list(SEVERITIES)}"})
        if not kind or len(kind) > 60:
            problems.append({"field": "kind", "value": kind, "reason": "required, <= 60 chars"})
        if not title or len(title) > 120:
            problems.append({"field": "title", "value": title[:40], "reason": "required, <= 120 chars"})
        if not summary or len(summary) > 2000:
            problems.append({"field": "summary", "value": summary[:40], "reason": "required, <= 2000 chars"})
        if not (0.0 <= float(confidence) <= 1.0):
            problems.append({"field": "confidence", "value": str(confidence), "reason": "must be within 0..1"})
        errors, norm = validate_citations(citations or {}, self.store, self.repo)
        errors = problems + errors
        if errors:
            return {"insight_id": None, "status": "invalid", "citation_report": {"valid": False, "errors": errors}}

        key = dedupe_key or f"{kind}|" + "|".join(sorted({f['path'] for f in norm['files']}))
        existing = self.store.q1("SELECT uid, status FROM insights WHERE dedupe_key=? ORDER BY created_at LIMIT 1",
                                 (key,))
        if existing is not None:
            return {"insight_id": existing["uid"], "status": existing["status"], "duplicate": True,
                    "citation_report": {"valid": True, "errors": []},
                    "note": "an insight with the same dedupe_key already exists; not re-created"}
        uid = new_insight_uid()
        now = time.time()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO insights(uid, label, kind, title, summary, severity, confidence, status, citations, "
                "dedupe_key, workflow_run, created_by_model, verifier_notes, votes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, label, kind, title, summary, severity, float(confidence), "proposed", json.dumps(norm), key,
                 workflow_run, model, None, "[]", now, now))
        synced = self._project(uid)
        return {"insight_id": uid, "status": "proposed", "graph_synced": synced,
                "citation_report": {"valid": True, "errors": []}}

    def set_status(self, uid: str, status: str, verdict_notes: str, votes: list[dict] | None = None) -> dict:
        if status not in ("verified", "rejected"):
            raise InsightError("status must be 'verified' or 'rejected'")
        ins = self.get(uid)
        if ins is None:
            raise InsightError(f"unknown insight {uid}")
        notes = verdict_notes
        if status == "verified":  # the gate is re-run at verification time; reality may have changed
            errors, _ = validate_citations(ins["citations"], self.store, self.repo)
            if errors:
                status = "rejected"
                notes = f"auto-rejected: citations no longer validate: {errors[:3]}. Verifier notes: {verdict_notes}"
        with self.store.tx() as conn:
            conn.execute("UPDATE insights SET status=?, verifier_notes=?, votes=?, updated_at=? WHERE uid=?",
                         (status, notes, json.dumps(votes or []), time.time(), uid))
        synced = self._project(uid)
        embedded = self._embed(uid) if status == "verified" else self._unembed(uid)
        return {"insight_id": uid, "status": status, "graph_synced": synced, "embedded": embedded}

    # ------------------------------------------------------------------ projections
    def _project(self, uid: str) -> bool:
        try:
            driver = self._driver()
        except Exception:
            driver = None
        if driver is None:
            return False
        ins = self.get(uid)
        c = ins["citations"]
        repo_id = self.store.repo_id
        props = {k: ins[k] for k in ("label", "kind", "title", "summary", "severity", "confidence", "status",
                                     "verifier_notes", "workflow_run", "created_by_model", "dedupe_key",
                                     "created_at", "updated_at")}
        props.update(repo=repo_id, commit_hashes=[x["hash"] for x in c.get("commits", [])],
                     paths=[f["path"] for f in c.get("files", [])], symbol_ids=c.get("symbols", []))
        try:
            with driver.session(database=self.database) as s:
                s.run("MERGE (i:Insight {uid:$uid}) SET i += $props", uid=uid, props=props).consume()
                s.run("MATCH (i:Insight {uid:$uid}) REMOVE i:Pattern:AntiPattern:RejectedAttempt", uid=uid).consume()
                s.run(f"MATCH (i:Insight {{uid:$uid}}) SET i:{ins['label']}", uid=uid).consume()
                s.run("MATCH (i:Insight {uid:$uid})-[r:EVIDENCED_BY|CONCERNS]->() DELETE r", uid=uid).consume()
                s.run("UNWIND $rows AS row MATCH (i:Insight {uid:$uid}) MATCH (c:Commit {uid: $cp + row.hash}) "
                      "MERGE (i)-[:EVIDENCED_BY {role: row.role}]->(c)", uid=uid, rows=c.get("commits", []),
                      cp=f"commit:{repo_id}:").consume()
                s.run("UNWIND $rows AS row MATCH (i:Insight {uid:$uid}) MATCH (f:File {uid: row.uid}) "
                      "MERGE (i)-[r:CONCERNS]->(f) SET r.at_commit = row.at_commit", uid=uid,
                      rows=[{"uid": file_uid(repo_id, f["path"]), "at_commit": f["at_commit"]}
                            for f in c.get("files", [])]).consume()
                s.run("UNWIND $rows AS sid MATCH (i:Insight {uid:$uid}) MATCH (s:Symbol {uid: sid}) "
                      "MERGE (i)-[:CONCERNS]->(s)", uid=uid, rows=c.get("symbols", [])).consume()
        except Exception:
            return False
        with self.store.tx() as conn:
            conn.execute("UPDATE insights SET graph_synced=1 WHERE uid=?", (uid,))
        return True

    def evidence_text(self, ins: dict) -> str:
        lines = [ins["title"], ins["summary"], "Evidence:"]
        for c in ins["citations"].get("commits", []):
            row = self.store.commit(c["hash"])
            subj = (row["message"] or "").splitlines()[0] if row and row["message"] else ""
            lines.append(f"- {c['role']} {c['hash'][:10]}: {subj}")
        lines += [f"- file {f['path']} @ {f['at_commit'][:10]}" for f in ins["citations"].get("files", [])]
        lines += [f"- symbol {s}" for s in ins["citations"].get("symbols", [])]
        return "\n".join(lines)

    def _vec(self):
        try:
            return self._vectors()
        except Exception:  # e.g. embedded Qdrant locked by another process: status change must still succeed
            return None

    def _embed(self, uid: str) -> bool:
        v = self._vec()
        if v is None:
            return False
        client, embedder = v
        ins = self.get(uid)
        c = ins["citations"]
        paths = [f["path"] for f in c.get("files", [])]
        try:
            ensure_collections(client, embedder.dim)
            upsert_texts(client, embedder, INSIGHTS, [(uid, self.evidence_text(ins), {
                "repo": self.store.repo_id, "label": ins["label"], "kind": ins["kind"], "severity": ins["severity"],
                "status": ins["status"], "title": ins["title"], "commit_hashes": [x["hash"] for x in c.get("commits", [])],
                "symbol_ids": c.get("symbols", []), "paths": paths,
                "path_prefixes": sorted({p for path in paths for p in path_prefixes(path)})})])
            return True
        except Exception:
            return False

    def _unembed(self, uid: str) -> bool:
        v = self._vec()
        if v is None:
            return False
        client, _ = v
        try:
            if client.collection_exists(INSIGHTS):
                client.delete(INSIGHTS, points_selector=[point_id(uid)])
            return True
        except Exception:
            return False

    def sync_all(self) -> dict:
        rows = self.store.q("SELECT uid, status FROM insights WHERE graph_synced=0")
        synced = sum(1 for r in rows if self._project(r["uid"]))
        for r in self.store.q("SELECT uid FROM insights WHERE status='verified'"):
            self._embed(r["uid"])
        return {"pending": len(rows), "synced": synced}
