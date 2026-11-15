"""FastMCP server exposing the Oracle knowledge base to Claude Code (stdio transport).

Read tools: run_cypher, search_qdrant, get_commit_diff, get_symbol_history, get_work_packets, list_insights,
            verify_citations
Write tools (insight layer only): upsert_insight, set_insight_status
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from afd_oracle_core import runtime
from afd_oracle_core.citations import validate_citations
from afd_oracle_core.config import get_settings
from afd_oracle_core.graph import read_query
from afd_oracle_core.ids import split_commit_uid
from afd_oracle_core.insights import InsightError, InsightService
from afd_oracle_core.registry import list_repos, open_store, resolve_repo
from afd_oracle_core.store import Store, loads
from afd_oracle_core.vectors import COLLECTIONS, search
from afd_oracle_ingest.ast.extractor import language_for
from afd_oracle_ingest.ast.parse_cache import ParseCache
from afd_oracle_ingest.git.walker import commit_diff, open_repo
from afd_oracle_ingest.safety import discussions as _discussions
from afd_oracle_ingest.safety.gate import Gate

from .guards import CypherRejected, check_read_only

mcp = FastMCP(
    "afd-oracle",
    instructions=(
        "Oracle knowledge base. When working inside a git repository, call identify_repo(path=<cwd>) "
        "first and pass the returned repo_id to the other tools. Graph (Neo4j) of files/symbols/commits/branches with TOUCHED, "
        "CO_COMMITTED, REVERTS, CALLS, IMPORTS edges; vectors (Qdrant) for code_symbols, commit_diffs and "
        "architectural_insights. Prefer get_work_packets for pre-assembled evidence. Every insight must cite exact "
        "commit hashes, file paths with at_commit, and symbol uids; upsert_insight validates them deterministically."
    ),
)


# ---------------------------------------------------------------------------- helpers
@lru_cache(maxsize=16)
def _repo_handles(repo_id: str) -> tuple[Store, Any]:
    s = get_settings()
    rid, git_path = resolve_repo(repo_id, s)
    return open_store(rid, s), open_repo(git_path)


def _handles(repo: str | None) -> tuple[str, Store, Any]:
    try:
        rid, _ = resolve_repo(repo)
    except LookupError as e:
        raise ToolError(str(e)) from e
    store, git = _repo_handles(rid)
    return rid, store, git


def _service(repo: str | None) -> InsightService:
    s = get_settings()
    _, store, git = _handles(repo)
    return InsightService(store, git, s.neo4j_database, driver=lambda: runtime.graph_driver(s),
                          vectors=lambda: runtime.vectors(s))


def _repo_of_insight(insight_id: str, repo: str | None) -> str | None:
    if repo:
        return repo
    for rid in list_repos():
        store, _ = _repo_handles(rid)
        if store.q1("SELECT 1 FROM insights WHERE uid=?", (insight_id,)):
            return rid
    raise ToolError(f"unknown insight {insight_id}")


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _subject(msg: str | None) -> str:
    return (msg or "").splitlines()[0] if msg else ""


# ---------------------------------------------------------------------------- tools
@mcp.tool
def run_cypher(
    query: Annotated[str, Field(description="Read-only Cypher. Write clauses are rejected.")],
    params: Annotated[dict[str, Any] | None, Field(description="Query parameters")] = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Max rows returned")] = 200,
) -> dict:
    """Run a read-only Cypher query against the oracle graph (READ transaction, 30 s timeout, row cap)."""
    try:
        check_read_only(query)
    except CypherRejected as e:
        raise ToolError(str(e)) from e
    s = get_settings()
    drv = runtime.graph_driver(s)
    if drv is None:
        raise ToolError(f"Neo4j is unreachable at {s.neo4j_uri}; start infra/docker-compose.yml")
    try:
        cols, rows, truncated = read_query(drv, s.neo4j_database, query, params, limit, s.cypher_timeout_s)
    except Exception as e:
        raise ToolError(f"Cypher error: {e}") from e
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}


@mcp.tool
def search_qdrant(
    collection: Literal["code_symbols", "commit_diffs", "architectural_insights", "security_findings"],
    query: Annotated[str, Field(min_length=1, description="Natural language or code")],
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid",
    filter: Annotated[dict[str, Any] | None, Field(
        description="Optional: repo, kind, path_prefix, alive, commit_hash, ts_from, ts_to, status, label, severity")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    with_neo4j: Annotated[bool, Field(description="Attach the graph/store record for each hit")] = False,
) -> dict:
    """Semantic + keyword (BM25) search with RRF fusion over code symbols, commit diffs or verified insights."""
    if collection not in COLLECTIONS:
        raise ToolError(f"collection must be one of {COLLECTIONS}")
    client, emb = runtime.vectors()
    try:
        if not client.collection_exists(collection):
            return {"hits": [], "note": "collection does not exist yet; run `afd-oracle init-db` and `afd-oracle ingest`"}
        points = search(client, emb, collection, query, mode, filter, limit)
    except Exception as e:
        raise ToolError(f"Qdrant error: {e}") from e
    hits = []
    for p in points:
        payload = dict(p.payload or {})
        text = payload.pop("text", "")
        hit = {"score": round(float(p.score), 4), "neo4j_id": payload.get("neo4j_id"), "payload": payload,
               "snippet": text[:600]}
        if with_neo4j and hit["neo4j_id"]:
            hit["graph"] = _lookup_node(hit["neo4j_id"], payload.get("repo"))
        hits.append(hit)
    return {"hits": hits}


def _lookup_node(uid: str, repo: str | None) -> dict | None:
    s = get_settings()
    drv = runtime.graph_driver(s)
    if drv is not None:
        label, key, value = ("Commit", "uid", uid) if uid.startswith("commit:") else \
            ("Insight", "uid", uid) if uid.startswith("insight:") else ("Symbol", "uid", uid)
        try:
            _, rows, _ = read_query(drv, s.neo4j_database, f"MATCH (n:{label} {{{key}: $v}}) RETURN n", {"v": value},
                                    1, s.cypher_timeout_s)
            if rows:
                node = rows[0][0]
                return {"labels": node.pop("_labels", []), "props": node}
        except Exception:
            pass
    try:
        _, store, _ = _handles(repo)
    except ToolError:
        return None
    if uid.startswith("sym:"):
        r = store.symbol(uid)
        return {"labels": ["Symbol"], "props": {k: r[k] for k in r.keys() if k not in ("body",)}} if r else None
    if uid.startswith("commit:"):
        r = store.commit(split_commit_uid(uid)[1])
        return {"labels": ["Commit"], "props": dict(r)} if r else None
    return None


@mcp.tool
def get_commit_diff(
    commit_hash: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{7,40}$")],
    paths: Annotated[list[str] | None, Field(description="Only these paths")] = None,
    context_lines: Annotated[int, Field(ge=0, le=20)] = 3,
    max_bytes: Annotated[int, Field(ge=1000, le=200000)] = 20000,
    repo: Annotated[str | None, Field(description="Repo id (optional when only one is ingested)")] = None,
) -> dict:
    """Full first-parent diff of a commit with metadata, revert links and the symbols it touched."""
    _, store, git = _handles(repo)
    full = store.resolve_commit(commit_hash)
    if not full:
        raise ToolError(f"commit {commit_hash} not found in the ingested history (or prefix is ambiguous)")
    c = git.get(full)
    row = store.commit(full)
    budget = max_bytes
    files = []
    wanted = set(paths or [])
    for patch in commit_diff(git, c, context_lines):
        if patch is None:
            continue
        d = patch.delta
        path = d.new_file.path if d.status_char() != "D" else d.old_file.path
        if wanted and path not in wanted and d.old_file.path not in wanted:
            continue
        _, adds, dels = patch.line_stats
        text = "(binary file)" if d.is_binary else (patch.text or "")
        truncated = len(text.encode()) > budget
        if truncated:
            text = text.encode()[: max(0, budget)].decode("utf-8", "ignore") + "\n... (truncated)"
        budget = max(0, budget - len(text.encode()))
        files.append({"path": path, "old_path": d.old_file.path if d.status_char() == "R" else None,
                      "status": d.status_char(), "added": adds, "deleted": dels, "patch": text,
                      "truncated": truncated})
    reverts = [r["reverted"] for r in store.q("SELECT reverted FROM reverts WHERE reverter=?", (full,))]
    reverted_by = [r["reverter"] for r in store.q("SELECT reverter FROM reverts WHERE reverted=?", (full,))]
    return {
        "hash": full, "parents": [str(p) for p in c.parent_ids], "author": f"{c.author.name} <{c.author.email}>",
        "ts": _iso(c.commit_time), "message": c.message, "is_merge": len(c.parent_ids) > 1,
        "is_revert": bool(row["is_revert"]) if row else False, "reverts": reverts, "reverted_by": reverted_by,
        "authored": _iso(c.author.time),
        # When the change reached the main line (merge commit time); None if it never did.
        "landed_on_main": _iso(row["landed_ts"]) if row is not None and row["landed_ts"] else None,
        "landed_via": row["landed_in"] if row is not None and row["landed_in"] not in (None, full) else None,
        "files": files, "symbols_touched": [{"uid": r["uid"], "change": r["change"]} for r in store.q(
            "SELECT uid, change FROM symbol_events WHERE hash=? ORDER BY uid", (full,))],
        # linked GitHub pull requests / issues (when the repository's discussions were synced)
        "discussions": _discussions.for_commit(store, full),
    }


@mcp.tool
def get_symbol_history(
    symbol_id: Annotated[str | None, Field(description="Symbol uid (sym:<repo>:<path>:<qualname>:<kind>)")] = None,
    qualname: str | None = None,
    path: str | None = None,
    repo: str | None = None,
    include_bodies: Annotated[bool, Field(description="Attach the symbol body at each event (from git)")] = False,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """Lifecycle of a symbol across history (follows renames/moves), plus symbols that co-change with it."""
    _, store, git = _handles(repo)
    if symbol_id:
        sym = store.symbol(symbol_id)
    elif qualname and path:
        sym = store.q1("SELECT * FROM symbols WHERE qualname=? AND path=? ORDER BY alive DESC LIMIT 1", (qualname, path))
    elif qualname:
        rows = store.q("SELECT * FROM symbols WHERE qualname=? ORDER BY alive DESC LIMIT 5", (qualname,))
        if len(rows) > 1:
            raise ToolError("ambiguous qualname; candidates: " + ", ".join(r["uid"] for r in rows))
        sym = rows[0] if rows else None
    else:
        raise ToolError("pass symbol_id, or qualname (+ path)")
    if sym is None:
        raise ToolError("symbol not found")
    chain, cur = [sym["uid"]], sym
    while cur is not None and cur["old_uid"] and cur["old_uid"] not in chain and len(chain) < 20:
        chain.append(cur["old_uid"])
        cur = store.symbol(cur["old_uid"])
    marks = ",".join("?" * len(chain))
    events = store.q(
        f"SELECT se.*, c.author_email, c.message FROM symbol_events se JOIN commits c ON c.hash=se.hash "
        f"WHERE se.uid IN ({marks}) ORDER BY se.seq LIMIT ?", (*chain, limit))
    cache = ParseCache(get_settings().parse_cache_path) if include_bodies else None
    out_events = []
    for e in events:
        ev = {"commit": e["hash"], "ts": _iso(e["ts"]), "author": e["author_email"], "subject": _subject(e["message"]),
              "change": e["change"], "uid": e["uid"], "old_uid": e["old_uid"]}
        if include_bodies and e["change"] != "deleted":
            ev["body"] = _body_at(git, cache, e["hash"], e["path"], e["qualname"], e["kind"])
        out_events.append(ev)
    co = store.q(
        f"SELECT se2.uid, count(*) n FROM symbol_events se1 JOIN symbol_events se2 ON se1.hash=se2.hash "
        f"WHERE se1.uid IN ({marks}) AND se2.uid NOT IN ({marks}) GROUP BY se2.uid ORDER BY n DESC LIMIT 10",
        (*chain, *chain))
    return {
        "symbol": {"uid": sym["uid"], "kind": sym["kind"], "qualname": sym["qualname"], "path": sym["path"],
                   "alive": bool(sym["alive"]), "first_seen": sym["first_seen"], "last_seen": sym["last_seen"],
                   "signature": sym["signature"], "previous_uids": chain[1:],
                   "metrics": loads(sym["metrics"], None)},
        "events": out_events,
        "co_changed_symbols": [{"uid": r["uid"], "count": r["n"]} for r in co],
    }


def _body_at(git, cache: ParseCache, sha: str, path: str, qualname: str, kind: str) -> str | None:
    commit = git.get(sha)
    try:
        oid = str(commit.tree[path].id)
    except (KeyError, AttributeError):
        return None
    lang = language_for(path)
    if not lang:
        return None
    fp = cache.get(oid, lang)
    if fp is None:
        cache.ensure({(oid, lang): git.get(oid).data}, workers=1)
        fp = cache.get(oid, lang)
    for s in fp.symbols if fp else []:
        if s.qualname == qualname and s.kind == kind:
            return s.body
    return None


@mcp.tool
def get_work_packets(
    kind: Literal["coupling", "graveyard", "pattern", "catalog"],
    repo: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 20,
    ids: Annotated[list[str] | None, Field(description="Fetch these packet ids (any status)")] = None,
    ids_only: Annotated[bool, Field(description="Return ids + priority only (for planning fan-out)")] = False,
    subkind: Annotated[str | None, Field(description="e.g. revert, abandoned_branch, god_class, cycle")] = None,
    claim: Annotated[bool, Field(description="Mark returned open packets as claimed")] = True,
) -> dict:
    """Pre-assembled, size-capped evidence packets produced by the deterministic pipeline."""
    _, store, _ = _handles(repo)
    if ids:
        marks = ",".join("?" * len(ids))
        rows = store.q(f"SELECT * FROM packets WHERE id IN ({marks}) ORDER BY priority DESC", ids)
    else:
        sql, params = "SELECT * FROM packets WHERE kind=? AND status='open'", [kind]
        if subkind:
            sql += " AND subkind=?"
            params.append(subkind)
        rows = store.q(sql + " ORDER BY priority DESC LIMIT ?", (*params, limit))
    if claim and rows:
        with store.tx() as conn:
            conn.executemany("UPDATE packets SET status='claimed' WHERE id=? AND status='open'",
                             [(r["id"],) for r in rows])
    remaining = store.q1("SELECT count(*) n FROM packets WHERE kind=? AND status='open'", (kind,))["n"]
    return {"packets": [{"id": r["id"], "kind": r["kind"], "subkind": r["subkind"],
                         "priority": round(r["priority"] or 0, 4), "tokens": r["tokens"],
                         **({} if ids_only else {"body": json.loads(r["body"])})} for r in rows],
            "open_remaining": remaining}


@mcp.tool
def upsert_insight(
    label: Literal["Pattern", "AntiPattern", "RejectedAttempt"],
    kind: Annotated[str, Field(description="e.g. hidden_coupling, god_class, cycle, leaky_abstraction, "
                                           "reverted_feature, abandoned_branch, idiom")],
    title: Annotated[str, Field(max_length=120)],
    summary: Annotated[str, Field(max_length=2000)],
    severity: Literal["info", "low", "medium", "high"],
    confidence: Annotated[float, Field(ge=0, le=1)],
    citations: Annotated[dict[str, Any], Field(description=(
        '{"commits": [{"hash": "<7-40 hex>", "role": "introduced|reverted|evidence|fixed"}], '
        '"files": [{"path": "a/b.py", "at_commit": "<hash>"}], "symbols": [{"uid": "sym:..."}]}'))],
    dedupe_key: str | None = None,
    workflow_run: str | None = None,
    model: Annotated[str | None, Field(description="Model that produced the insight")] = None,
    repo: str | None = None,
) -> dict:
    """Record a PROPOSED insight. Rejected (nothing written) unless every citation validates against git + history."""
    try:
        return _service(repo).upsert(label=label, kind=kind, title=title, summary=summary, severity=severity,
                                     confidence=confidence, citations=citations, dedupe_key=dedupe_key,
                                     workflow_run=workflow_run, model=model)
    except InsightError as e:
        raise ToolError(str(e)) from e


@mcp.tool
def verify_citations(insight_id: str, repo: str | None = None) -> dict:
    """Re-run the deterministic citation check for a stored insight."""
    svc = _service(_repo_of_insight(insight_id, repo))
    ins = svc.get(insight_id)
    errors, _ = validate_citations(ins["citations"], svc.store, svc.repo)
    return {"insight_id": insight_id, "valid": not errors, "errors": errors}


@mcp.tool
def set_insight_status(
    insight_id: str,
    status: Literal["verified", "rejected"],
    verdict_notes: Annotated[str, Field(max_length=4000)],
    votes: Annotated[list[dict[str, Any]] | None, Field(
        description='[{"model": "opus", "refuted": false, "reason": "..."}]')] = None,
    repo: str | None = None,
) -> dict:
    """Verifier-only: move a proposed insight to verified (embedded for retrieval) or rejected."""
    try:
        return _service(_repo_of_insight(insight_id, repo)).set_status(insight_id, status, verdict_notes, votes)
    except InsightError as e:
        raise ToolError(str(e)) from e


@mcp.tool
def list_insights(
    status: Literal["proposed", "verified", "rejected"] | None = None,
    kind: str | None = None,
    repo: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """List stored insights with their citations and verification state."""
    return {"insights": _service(repo).list(status, kind, limit)}


def _norm_url(url: str) -> str:
    """git@github.com:org/repo.git, https://user@github.com/org/repo/ -> github.com/org/repo"""
    u = url.strip().lower().removesuffix("/").removesuffix(".git")
    if "://" in u:
        u = u.split("://", 1)[1]
    if "@" in u.split("/", 1)[0]:
        u = u.split("@", 1)[1]
    return u.replace(":", "/", 1) if ":" in u.split("/", 1)[0] else u


@mcp.tool
def identify_repo(
    path: Annotated[str, Field(description="Absolute path of the current working directory (any folder inside a "
                                           "git working copy)")],
) -> dict:
    """Is this working copy in the knowledge base? Call this first when working inside a repository.

    Matches the working copy's root commit(s), then its remote URLs, against every ingested repository. Returns the
    repo id to pass to the other tools, plus how many commits the working copy is ahead of what was ingested.
    """
    import pygit2

    try:
        wc = pygit2.Repository(pygit2.discover_repository(path))
    except Exception:
        return {"found": False, "reason": f"{path} is not inside a git repository"}
    try:
        head = None if wc.head_is_unborn else wc.head.target
    except pygit2.GitError:  # empty repository: HEAD points at a branch with no commits yet
        head = None
    wc_roots = {str(c.id) for c in wc.walk(head, pygit2.GIT_SORT_NONE) if not c.parents} if head else set()
    wc_remotes = {_norm_url(r.url) for r in wc.remotes if r.url}
    matches = []
    for rid in sorted(list_repos()):
        store, _ = _repo_handles(rid)
        roots = {r["hash"] for r in store.q("SELECT hash FROM commits WHERE parents='[]'")}
        remotes = {_norm_url(u) for u in json.loads(store.get_meta("remotes", "[]") or "[]")}
        by = [m for m, ok in (("root_commit", bool(wc_roots & roots)), ("remote_url", bool(wc_remotes & remotes))) if ok]
        if by:
            matches.append((len(by), rid, by, store))
    if not matches:
        return {"found": False, "reason": "not in the knowledge base; add it in the dashboard (uv run afd-oracle ui)",
                "known_repos": sorted(list_repos())}
    matches.sort(key=lambda m: (-m[0], m[1]))
    _, rid, by, store = matches[0]
    behind = 0
    if head is not None and store.commit(str(head)) is None:
        for c in wc.walk(head, pygit2.GIT_SORT_TOPOLOGICAL):
            if store.commit(str(c.id)) is not None or behind >= 5000:
                break
            behind += 1
    ts = store.get_meta("ingested_at")
    return {"found": True, "repo_id": rid, "matched_by": by,
            "other_matches": [m[1] for m in matches[1:]],
            "ingested_at": _iso(int(ts)) if ts else None, "head_ingested": store.get_meta("head"),
            "working_copy_head": str(head) if head else None, "commits_not_ingested": behind,
            "stale": behind > 0}


@mcp.tool
def repo_overview(repo: str | None = None) -> dict:
    """One-call orientation: size, what was mined, the most important verified insights and the hottest files."""
    rid, store, _ = _handles(repo)
    q1 = lambda sql, *a: store.q1(sql, a)["n"]  # noqa: E731
    sev = {"high": 0, "medium": 1, "low": 2, "info": 3}
    verified = store.q("SELECT uid, label, kind, severity, title FROM insights WHERE status='verified'")
    top = sorted(verified, key=lambda r: (sev.get(r["severity"], 4), r["label"]))[:10]
    hot = store.q("SELECT props FROM candidates WHERE kind='hotspot' ORDER BY score DESC LIMIT 5")
    ts = store.get_meta("ingested_at")
    return {
        "repo_id": rid, "origin": store.get_meta("origin"), "head_ingested": store.get_meta("head"),
        "ingested_at": _iso(int(ts)) if ts else None,
        "size": {"commits": q1("SELECT count(*) n FROM commits"),
                 "alive_symbols": q1("SELECT count(*) n FROM symbols WHERE alive=1"),
                 "files": q1("SELECT count(*) n FROM files WHERE alive=1")},
        "insights": {r["status"]: r["n"] for r in store.q("SELECT status, count(*) n FROM insights GROUP BY status")},
        "top_verified_insights": [{"id": r["uid"], "label": r["label"], "kind": r["kind"], "severity": r["severity"],
                                   "title": r["title"]} for r in top],
        "hotspots": [loads(r["props"], {}) for r in hot],
        "hint": "Use list_insights / search_qdrant(collection='architectural_insights') for verified findings, "
                "get_symbol_history and get_commit_diff for history, run_cypher for co-change and call graphs.",
    }


# ---------------------------------------------------------------------------- change safety
def _repo_for(repo: str | None, path: str | None) -> str | None:
    if repo or not path:
        return repo
    found = getattr(identify_repo, "fn", identify_repo)(path=path)
    if not found.get("found"):
        raise ToolError(f"{path} is not in the knowledge base: {found.get('reason')}")
    return found["repo_id"]


@mcp.tool
def assess_change(
    paths: Annotated[list[str] | None, Field(description="Files you plan to change (repo-relative)")] = None,
    symbols: Annotated[list[str] | None, Field(description="Symbols you plan to change: uids, 'path::Qual.name' "
                                                           "or qualified names")] = None,
    repo: str | None = None,
    working_dir: Annotated[str | None, Field(description="Current working directory, used to find the repo "
                                                         "when `repo` is omitted")] = None,
    depth: Annotated[int, Field(ge=1, le=4)] = 2,
) -> dict:
    """BEFORE editing: blast radius, hidden coupling, string contracts, public API at stake, tests to run, hazards,
    past fixes (fragile / security / race zones), verified insights and a risk level, plus `must` / `should` items."""
    rid, store, git = _handles(_repo_for(repo, working_dir))
    return Gate(store, git).assess_change(paths, symbols, depth)


@mcp.tool
def check_diff(
    working_dir: Annotated[str | None, Field(description="Working copy to diff against its HEAD (staged, unstaged "
                                                         "and untracked files)")] = None,
    diff: Annotated[str | None, Field(description="A unified diff (git diff output) instead of a working copy")] = None,
    repo: str | None = None,
) -> dict:
    """AFTER editing: verdict (block / warn / pass) for the actual change. Breaking API changes, removed guards from
    past fixes, contract breaks, new security / race / logic-bomb / supply-chain findings, hidden partners not
    updated, untested changed code, and the tests to run. Every `block` must be fixed or acknowledged by the user."""
    rid, store, git = _handles(_repo_for(repo, working_dir))
    return Gate(store, git).check_diff(diff=diff, workdir=working_dir if diff is None else None)


@mcp.tool
def list_vulnerabilities(
    repo: str | None = None,
    severity: Annotated[Literal["critical", "high", "medium", "low", "info"] | None,
                        Field(description="Minimum severity")] = None,
    scanner: Annotated[str | None, Field(description="semgrep | bandit | trivy | gitleaks | checkov | hadolint | "
                                                     "osv-scanner | snyk | snyk-code")] = None,
    path: Annotated[str | None, Field(description="Exact file path or a directory prefix")] = None,
    query: Annotated[str | None, Field(description="Search by meaning over findings (security_findings collection)")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """Findings from vulnerability scanners run in the Docker sandbox (afd-oracle scan): vulnerable dependencies
    (CVE, fixed version), code vulnerabilities, misconfigurations and secrets (values never stored)."""
    _, store, _ = _handles(repo)
    if query:
        client, emb = runtime.vectors()
        if not client.collection_exists("security_findings"):
            return {"vulnerabilities": [], "note": "no scan has been embedded yet; run `afd-oracle scan`"}
        pts = search(client, emb, "security_findings", query, "hybrid", {"repo": store.repo_id}, limit)
        return {"vulnerabilities": [{**(p.payload or {}), "score": round(float(p.score), 4)} for p in pts]}
    order = ["critical", "high", "medium", "low", "info"]
    sql, args = "SELECT * FROM vuln_findings WHERE 1=1", []
    if severity:
        keep = order[: order.index(severity) + 1]
        sql += f" AND severity IN ({','.join('?' * len(keep))})"
        args += keep
    if scanner:
        sql += " AND scanner=?"
        args.append(scanner)
    if path:
        sql += " AND (path=? OR path LIKE ?)"
        args += [path, path.rstrip("/") + "/%"]
    try:
        rows = store.q(sql + " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
                       "WHEN 'low' THEN 3 ELSE 4 END, path, line LIMIT ?", (*args, limit))
        runs = [dict(r) for r in store.q("SELECT scanner, ran_at, commit_hash, status, findings FROM scan_runs")]
    except Exception:
        return {"vulnerabilities": [], "note": "this repository has not been scanned; run `afd-oracle scan`"}
    keys = ("scanner", "rule", "severity", "title", "path", "line", "package", "version", "fixed", "cve", "cwe", "url",
            "symbol")
    return {"vulnerabilities": [{k: r[k] for k in keys} for r in rows], "scans": runs}


@mcp.tool
def list_findings(
    repo: str | None = None,
    category: Annotated[str | None, Field(description="security | race | logic_bomb | contract | error_handling | "
                                                      "performance")] = None,
    severity: Literal["block", "warn", "info"] | None = None,
    path: Annotated[str | None, Field(description="Exact file path or a directory prefix")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """Baseline hazards found at HEAD (what is already there, before any change)."""
    _, store, _ = _handles(repo)
    sql, args = "SELECT * FROM findings WHERE 1=1", []
    if category:
        sql += " AND category=?"
        args.append(category)
    if severity:
        sql += " AND severity=?"
        args.append(severity)
    if path:
        sql += " AND (path=? OR path LIKE ?)"
        args += [path, path.rstrip("/") + "/%"]
    rows = store.q(sql + " ORDER BY CASE severity WHEN 'block' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, path, line "
                   "LIMIT ?", (*args, limit))
    return {"findings": [{**{k: r[k] for k in ("category", "rule", "severity", "path", "line", "symbol", "message")},
                          "detail": loads(r["detail"], {})} for r in rows]}


@mcp.tool
def get_contracts(
    repo: str | None = None,
    key: Annotated[str | None, Field(description="Exact key, e.g. an env var, config key, event name or route")] = None,
    kind: Annotated[str | None, Field(description="env | config | event | route | endpoint | sql_table | registry | "
                                                  "flag | cli_flag")] = None,
    path: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """String-linked producers and consumers (who sets / reads / emits / handles / serves / calls what)."""
    _, store, _ = _handles(repo)
    sql, args = "SELECT * FROM contracts WHERE 1=1", []
    for col, val in (("key", key), ("kind", kind), ("path", path)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    rows = store.q(sql + " ORDER BY kind, key, role LIMIT ?", (*args, limit))
    return {"contracts": [dict(r) for r in rows]}


@mcp.tool
def search_discussions(
    query: str,
    repo: str | None = None,
    kind: Literal["pr", "issue"] | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Search synced GitHub pull requests and issues (titles and bodies) with their linked commits: the 'why'."""
    _, store, _ = _handles(repo)
    if not store.q1("SELECT 1 FROM discussions LIMIT 1"):
        return {"results": [], "note": "no discussions synced for this repository (dashboard: Sync GitHub "
                                       "discussions, or `afd-oracle discussions <repo>`)"}
    return {"results": _discussions.search(store, query, limit, kind)}


def main() -> None:
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
