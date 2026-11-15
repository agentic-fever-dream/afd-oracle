"""Dashboard API (FastAPI) + static SPA. Bound to 127.0.0.1; mutating routes need the `X-AFD-Oracle-UI: 1` header."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from afd_oracle_core import runtime
from afd_oracle_core.config import get_settings
from afd_oracle_core.registry import list_repos, resolve_repo
from afd_oracle_core.registry import open_store as _open_store
from afd_oracle_core.store import loads
from afd_oracle_ingest.safety.discussions import github_slug

from . import setup
from .jobs import ACTIVE, JobManager

TOKENS_PER_PACKET = 13_000  # measured on flask: analyst + verifier + share of synthesis, all subagent tokens
KINDS = ("coupling", "graveyard", "pattern", "catalog")
REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Oracle dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")
_jobs: JobManager | None = None


_STORES: dict[str, object] = {}


def open_store(repo: str, settings=None):
    """One connection per repository for the life of the server. Opening a Store re-runs its schema script, which
    waits on SQLite's write lock while an ingest is writing; opening one per request made the home page take ~30 s
    during a large ingest and leaked a connection per request. sqlite3 here is built serialized (threadsafety 3), so
    one connection can serve the request threads."""
    st = _STORES.get(repo)
    if st is None:
        st = _STORES[repo] = _open_store(repo, settings or get_settings())
    return st


_READERS: dict[str, object] = {}


def read_store(repo: str):
    """Read-only, schema-free connection for summaries and other GET paths: never waits on a running ingest."""
    from afd_oracle_core.store import Store
    st = _READERS.get(repo)
    if st is None:
        st = _READERS[repo] = Store.readonly(get_settings().store_path(repo), repo)
    return st


def forget_store(repo: str) -> None:
    for cache in (_STORES, _READERS):
        st = cache.pop(repo, None)
        if st is not None:
            try:
                st.close()
            except Exception:
                pass


def jobs() -> JobManager:
    global _jobs
    if _jobs is None:
        _jobs = JobManager(get_settings().data_dir)
    return _jobs


def csrf(request: Request) -> None:
    """Browsers can't send a custom header cross-site without a CORS preflight, which this app never grants."""
    if request.headers.get("x-afd-oracle-ui") != "1":
        raise HTTPException(403, "missing X-AFD-Oracle-UI header")


def _rstore(repo: str):
    """Read-only store for GET routes (never blocks behind an ingest)."""
    if repo not in list_repos():
        raise HTTPException(404, f"unknown repository '{repo}'")
    return read_store(repo)


def _store(repo: str):
    if repo not in list_repos():
        raise HTTPException(404, f"unknown repository '{repo}'")
    return open_store(repo, get_settings())


def _python() -> list[str]:
    return [sys.executable, "-m", "afd_oracle_ingest.cli"]


# ---------------------------------------------------------------------------- health & setup
@app.get("/api/health")
def get_health() -> dict:
    return setup.health()


@app.post("/api/setup/{what}", dependencies=[Depends(csrf)])
def post_setup(what: str) -> dict:
    try:
        if what == "mcp":
            return setup.install_mcp()
        if what == "skill":
            return setup.install_skill()
        if what == "service":
            from . import service
            return service.install(start_now=True)
        if what == "infra":
            cmds = setup.infra_command()
            # docker compose up, then wait for Neo4j and apply schema + collections (idempotent)
            script = ("import subprocess,sys,time\n"
                      f"r=subprocess.run({cmds[0]!r});\n"
                      "if r.returncode: sys.exit(r.returncode)\n"
                      "for i in range(60):\n"
                      f"  if subprocess.run({_python() + ['init-db']!r}).returncode==0: sys.exit(0)\n"
                      "  print('waiting for databases...', flush=True); time.sleep(5)\n"
                      "sys.exit(1)\n")
            return jobs().start("setup", None, "Start Docker services and initialise databases",
                                [sys.executable, "-c", script], setup.project_root())
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    raise HTTPException(404, f"unknown setup step {what}")


# ---------------------------------------------------------------------------- repositories
_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}
SUMMARY_TTL_S = 20


def _summary(repo: str) -> dict:
    """Repository card data. Counts are cached for a few seconds (large stores count millions of rows); the active
    and last job are always fresh."""
    hit = _SUMMARY_CACHE.get(repo)
    if hit and time.time() - hit[0] < SUMMARY_TTL_S:
        return {**hit[1], "active_job": jobs().active(repo), "last_job": next(iter(jobs().list(repo, 1)), None)}
    out = _summary_uncached(repo)
    _SUMMARY_CACHE[repo] = (time.time(), out)
    return out


_BEHIND: dict[tuple[str, str, str], int | None] = {}


def _commits_behind(repo: str, head: str | None) -> int | None:
    """Commits on the mirror's default branch that the last ingest has not read yet (None when unknown)."""
    if not head:
        return None
    try:
        from afd_oracle_ingest.git.walker import default_branch, open_repo
        _, path = resolve_repo(repo, get_settings())
        g = open_repo(path)
        tip = default_branch(g)[1]
        key = (repo, head, tip)
        if key not in _BEHIND:
            _BEHIND[key] = 0 if tip == head else g.ahead_behind(tip, head)[0]
        return _BEHIND[key]
    except Exception:
        return None


def _summary_uncached(repo: str) -> dict:
    store = read_store(repo)
    n = lambda sql: store.q1(sql)["n"]  # noqa: E731
    by = lambda sql: {r["k"]: r["n"] for r in store.q(sql)}  # noqa: E731
    ts = store.get_meta("ingested_at")
    last = next(iter(jobs().list(repo, 1)), None)
    active = jobs().active(repo)
    return {
        "id": repo, "origin": store.get_meta("origin"), "head": store.get_meta("head"),
        "ingested_at": int(ts) if ts else None,
        "commits": n("SELECT count(*) n FROM commits"),
        "alive_symbols": n("SELECT count(*) n FROM symbols WHERE alive=1"),
        "insights": by("SELECT status k, count(*) n FROM insights GROUP BY status"),
        "packets": by("SELECT status k, count(*) n FROM packets GROUP BY status"),
        "commits_behind": _commits_behind(repo, store.get_meta("head")),
        "active_job": active, "last_job": last,
    }


@app.get("/api/repos")
def get_repos() -> list[dict]:
    out = []
    for rid in sorted(list_repos()):
        try:
            out.append(_summary(rid))
        except Exception as e:  # a broken store must not hide the other repos
            out.append({"id": rid, "error": str(e)})
    # repos whose very first ingest is still running are not registered yet
    known = {r["id"] for r in out}
    for j in jobs().list(limit=50):
        if j["kind"] == "ingest" and j["status"] in ACTIVE and j["repo"] not in known:
            out.append({"id": j["repo"], "origin": (j["args"] or {}).get("source"), "commits": 0, "insights": {},
                        "packets": {}, "active_job": j, "pending": True})
            known.add(j["repo"])
    return out


class AddRepo(BaseModel):
    source: str = Field(description="git URL or local folder")
    repo_id: str | None = None
    include_prs: bool = True


def _looks_like_url(x: str) -> bool:
    return bool(re.match(r"^(https?://|ssh://|git@|git://)", x.strip()))


def _start_ingest(repo: str, source: str, *, full: bool = False, include_prs: bool = False) -> dict:
    if jobs().active(repo):
        raise HTTPException(409, f"'{repo}' already has a job running")
    cmd = [*_python(), "ingest", source, "--repo-id", repo, "--progress-json", "--packet-limit", "0",
           *os.environ.get("AFD_ORACLE_UI_INGEST_FLAGS", "").split()]  # e.g. "--no-graph --no-vectors" (tests, no Docker)
    if full:
        cmd.append("--full")
    if include_prs and _looks_like_url(source):
        cmd.append("--fetch-pulls")
    title = f"{'Rebuild' if full else 'Ingest'} {repo}"
    return jobs().start("ingest", repo, title, cmd, setup.project_root(),
                        args={"source": source, "full": full, "include_prs": include_prs})


@app.post("/api/repos", dependencies=[Depends(csrf)])
def post_repo(body: AddRepo) -> dict:
    source = body.source.strip()
    if _looks_like_url(source):
        guess = source.rstrip("/").rsplit("/", 1)[-1].split(":")[-1].removesuffix(".git")
    else:
        p = Path(source).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"folder not found: {source}")
        source = str(p.resolve())
        guess = p.resolve().name.removesuffix(".git")
    repo = (body.repo_id or guess).strip()
    if not REPO_ID.match(repo):
        raise HTTPException(400, "repository id may only contain letters, digits, '.', '_' and '-'")
    if repo in list_repos():
        raise HTTPException(409, f"'{repo}' is already in the knowledge base; use Update on its page")
    return _start_ingest(repo, source, include_prs=body.include_prs)


@app.get("/api/repos/{repo}")
def get_repo(repo: str) -> dict:
    store = _rstore(repo)
    out = _summary(repo)
    out["packets_by_kind"] = {}
    for r in store.q("SELECT kind, subkind, status, count(*) n FROM packets GROUP BY kind, subkind, status"):
        out["packets_by_kind"].setdefault(r["kind"], {}).setdefault(r["subkind"], {})[r["status"]] = r["n"]
    out["insights_by_label"] = {f"{r['status']}/{r['label']}": r["n"] for r in store.q(
        "SELECT status, label, count(*) n FROM insights GROUP BY status, label")}
    out["stats"] = {k: store.q1(f"SELECT count(*) n FROM {t}")["n"] for k, t in
                    (("reverts", "reverts"), ("branches", "branches"), ("cochange_pairs", "cochange"),
                     ("files", "files"))}
    ingest = next((j for j in jobs().list(repo, 30) if j["kind"] == "ingest" and j["status"] == "succeeded"), None)
    out["last_ingest"] = ingest
    out["noise_filtered"] = ((ingest or {}).get("summary") or {}).get("packets", {}).get("filtered_as_noise")
    try:
        from .about import about
        out["about"] = about(store) or None
    except Exception:  # an unreadable mirror must not break the overview
        out["about"] = None
    return out


class Update(BaseModel):
    full: bool = False


@app.post("/api/repos/{repo}/ingest", dependencies=[Depends(csrf)])
def post_ingest(repo: str, body: Update) -> dict:
    store = _store(repo)
    source = store.get_meta("origin") or str(list_repos()[repo]["path"])
    if not _looks_like_url(source) and not Path(source).exists():
        source = str(list_repos()[repo]["path"])
    include_prs = any((j["args"] or {}).get("include_prs") for j in jobs().list(repo, 30) if j["kind"] == "ingest")
    return _start_ingest(repo, source, full=body.full, include_prs=include_prs)


@app.delete("/api/repos/{repo}", dependencies=[Depends(csrf)])
def delete_repo(repo: str, confirm: str, remove_mirror: bool = False) -> dict:
    if confirm != repo:
        raise HTTPException(400, "type the repository id to confirm")
    _store(repo)
    if jobs().active(repo):
        raise HTTPException(409, "stop the running job first")
    forget_store(repo)  # release the cached connection before deleting its files
    s = get_settings()
    removed: dict[str, Any] = {}
    try:
        drv = runtime.graph_driver(s, required=True)
        with drv.session(database=s.neo4j_database) as sess:
            removed["neo4j_nodes"] = sess.run("MATCH (n {repo:$r}) DETACH DELETE n RETURN count(n) AS c",
                                              r=repo).single()["c"]
    except Exception as e:
        removed["neo4j"] = f"skipped: {e}"[:200]
    try:
        from qdrant_client import models
        client, _ = runtime.vectors(s)
        flt = models.Filter(must=[models.FieldCondition(key="repo", match=models.MatchValue(value=repo))])
        for c in client.get_collections().collections:
            client.delete(c.name, points_selector=models.FilterSelector(filter=flt), wait=True)
        removed["qdrant"] = "points deleted"
    except Exception as e:
        removed["qdrant"] = f"skipped: {e}"[:200]
    git_path = Path(list_repos()[repo]["path"])
    data = json.loads(s.registry_path.read_text())
    data.pop(repo, None)
    s.registry_path.write_text(json.dumps(data, indent=2))
    store_dir = s.store_path(repo).parent
    runtime.reset()
    shutil.rmtree(store_dir, ignore_errors=True)
    removed["store"] = str(store_dir)
    if remove_mirror and git_path.is_relative_to(s.repos_dir.resolve()):
        shutil.rmtree(git_path, ignore_errors=True)
        removed["mirror"] = str(git_path)
    return {"deleted": repo, **removed}


# ---------------------------------------------------------------------------- mining
def _open_by_kind(store) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in KINDS}
    rank: dict[str, int] = {}
    catalog: list[tuple] = []
    for r in store.q("SELECT id, kind, subkind, priority FROM packets WHERE status='open' ORDER BY priority DESC"):
        if r["kind"] == "catalog":  # interleave catalogue subkinds so a capped run samples many candidate types
            rank[r["subkind"]] = rank.get(r["subkind"], -1) + 1
            catalog.append((rank[r["subkind"]], -(r["priority"] or 0), r["id"]))
        else:
            out.setdefault(r["kind"], []).append(r["id"])
    out["catalog"] = [i for *_, i in sorted(catalog)]
    return out


def _pick(open_ids: dict[str, list[str]], cap: int | None) -> dict[str, list[str]]:
    """Round-robin across kinds (highest priority first) so a cap samples every kind."""
    if cap is None:
        return open_ids
    picked: dict[str, list[str]] = {k: [] for k in open_ids}
    queues = {k: list(v) for k, v in open_ids.items()}
    while cap > 0 and any(queues.values()):
        for k in KINDS:
            if cap > 0 and queues.get(k):
                picked[k].append(queues[k].pop(0))
                cap -= 1
    return picked


@app.get("/api/repos/{repo}/estimate")
def get_estimate(repo: str, max: int | None = None) -> dict:
    store = _rstore(repo)
    open_ids = _open_by_kind(store)
    picked = _pick(open_ids, max)
    n = sum(len(v) for v in picked.values())
    return {"open": {k: len(v) for k, v in open_ids.items()}, "selected": {k: len(v) for k, v in picked.items()},
            "packets": n, "estimated_tokens": n * TOKENS_PER_PACKET, "tokens_per_packet": TOKENS_PER_PACKET,
            "agents_estimate": _agents_estimate(n)}


def _agents_estimate(n: int) -> int:
    return 0 if n == 0 else -(-n // 5) + n // 2 + 2  # analysts + ~1 verifier per 2 packets + synthesis + critic


class Mine(BaseModel):
    max: int | None = Field(default=None, ge=1)


MINE_PROMPT = ("Read the file {args}. Run the Workflow named `oracle-full` with exactly that JSON object as its args "
               "(the user explicitly asked for this run from the oracle dashboard). When it completes, reply "
               "with only the workflow result as JSON.")


@app.post("/api/repos/{repo}/mine", dependencies=[Depends(csrf)])
def post_mine(repo: str, body: Mine) -> dict:
    store = _store(repo)
    if jobs().active(repo):
        raise HTTPException(409, f"'{repo}' already has a job running")
    claude = setup.claude_bin()
    if not claude:
        raise HTTPException(400, "The claude CLI was not found on PATH; mining runs through Claude Code.")
    picked = _pick(_open_by_kind(store), body.max)
    ids = [i for v in picked.values() for i in v]
    if not ids:
        raise HTTPException(400, "nothing to mine: no open candidates (update the repository to pick up new commits)")
    run_id = f"mine-{repo}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"  # unique even within a second
    with store.tx() as conn:
        conn.executemany("UPDATE packets SET status='claimed' WHERE id=? AND status='open'", [(i,) for i in ids])
    jm = jobs()
    jm.record_run_packets(run_id, repo, ids)
    runs = get_settings().data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    args_file = runs / f"{run_id}.json"
    args_file.write_text(json.dumps({"repo": repo, "run_id": run_id, "batch_size": 5,
                                     "coupling_ids": picked.get("coupling", []),
                                     "graveyard_ids": picked.get("graveyard", []),
                                     "pattern_ids": picked.get("pattern", []),
                                     "catalog_ids": picked.get("catalog", [])}, indent=1))
    try:
        rel = os.path.relpath(args_file, setup.project_root()).replace("\\", "/")
    except ValueError:  # data dir on another drive than the project (Windows): no relative path exists
        rel = str(args_file).replace("\\", "/")
    cmd = [claude, "-p", MINE_PROMPT.format(args=rel), "--model", "sonnet", "--output-format", "stream-json",
           "--verbose", "--allowedTools",
           "Workflow Read mcp__afd-oracle__get_work_packets mcp__afd-oracle__list_insights"]

    def finished(job: dict, rc: int) -> dict:
        st = open_store(repo, get_settings())
        if job.get("status") == "succeeded":
            with st.tx() as conn:
                conn.executemany("UPDATE packets SET status='done' WHERE id=?", [(i,) for i in ids])
        return {"insights": {r["status"]: r["n"] for r in st.q(
            "SELECT status, count(*) n FROM insights WHERE workflow_run=? OR workflow_run LIKE ? GROUP BY status", (run_id, run_id + "/%"))}}

    return jm.start("mine", repo, f"Mine {repo} ({len(ids)} candidates)", cmd, setup.project_root(),
                    args={"run_id": run_id, "packets": {k: len(v) for k, v in picked.items()},
                          "estimated_tokens": len(ids) * TOKENS_PER_PACKET},
                    job_id=run_id, on_exit=finished)


@app.get("/api/repos/{repo}/runs/{run_id}")
def get_run(repo: str, run_id: str) -> dict:
    store = _rstore(repo)
    counts = {r["status"]: r["n"] for r in store.q(
        "SELECT status, count(*) n FROM insights WHERE workflow_run=? OR workflow_run LIKE ? GROUP BY status", (run_id, run_id + "/%"))}
    ids = jobs().run_packets(run_id)
    st: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in store.q(f"SELECT status, count(*) n FROM packets WHERE id IN ({','.join('?' * len(chunk))}) "
                         "GROUP BY status", chunk):
            st[r["status"]] = st.get(r["status"], 0) + r["n"]
    return {"run_id": run_id, "insights": counts, "packets": st}


@app.post("/api/jobs/{job_id}/release", dependencies=[Depends(csrf)])
def post_release(job_id: str) -> dict:
    job = jobs().get(job_id)
    if not job or job["kind"] != "mine":
        raise HTTPException(404, "not a mining run")
    if job["status"] in ACTIVE:
        raise HTTPException(409, "the run is still going")
    store = _store(job["repo"])
    ids = jobs().run_packets(job_id)
    with store.tx() as conn:
        conn.executemany("UPDATE packets SET status='open' WHERE id=? AND status='claimed'", [(i,) for i in ids])
    return {"released": len(ids)}


# ---------------------------------------------------------------------------- change safety
@app.get("/api/repos/{repo}/safety")
def get_safety(repo: str) -> dict:
    store = _rstore(repo)
    by: dict[str, dict[str, int]] = {}
    for r in store.q("SELECT category, severity, count(*) n FROM findings GROUP BY category, severity"):
        by.setdefault(r["category"], {})[r["severity"]] = r["n"]
    risky = [{"path": r["subject"], "score": r["score"], "factors": loads(r["factors"], {})} for r in store.q(
        "SELECT subject, score, factors FROM risk WHERE kind='file' ORDER BY score DESC LIMIT 15")]
    funcs = store.q1("SELECT count(*) n FROM symbols WHERE alive=1 AND kind='function'")["n"]
    tested = store.q1("SELECT count(DISTINCT target) n FROM test_map WHERE target LIKE 'sym:%'")["n"]
    zones: dict[str, int] = {}
    for r in store.q("SELECT kinds FROM fix_events"):
        for k in json.loads(r["kinds"] or "[]"):
            zones[k] = zones.get(k, 0) + 1
    return {
        "findings": by,
        "risky_files": risky,
        "contracts": {"keys": store.q1("SELECT count(DISTINCT kind || key) n FROM contracts")["n"],
                      "orphans": store.q1("SELECT count(*) n FROM findings WHERE rule LIKE 'contract.orphan%'")["n"]},
        "tests": {"functions": funcs, "functions_with_tests": tested,
                  "links": store.q1("SELECT count(*) n FROM test_map")["n"],
                  "coverage_links": store.q1("SELECT count(*) n FROM test_map WHERE source='coverage'")["n"]},
        "api": {"public_symbols": store.q1("SELECT count(*) n FROM api_surface WHERE ref='HEAD'")["n"],
                "deprecated": store.q1("SELECT count(*) n FROM api_surface WHERE ref='HEAD' AND deprecated IS NOT NULL")["n"],
                "tags": [r["ref"] for r in store.q("SELECT DISTINCT ref FROM api_surface WHERE ref<>'HEAD'")]},
        "fixes": {"commits": store.q1("SELECT count(DISTINCT hash) n FROM fix_events")["n"], "zones": zones},
        "discussions": {"count": store.q1("SELECT count(*) n FROM discussions")["n"],
                        "synced_at": store.get_meta("discussions_synced_at"),
                        "github": github_slug(store)},
    }


@app.get("/api/bench")
def get_bench() -> dict:
    """The multi-repository benchmark (data/bench): progress log, memory, ingest and scan results, live log tails."""
    root = get_settings().data_dir / "bench"
    if not root.exists():
        return {"exists": False}

    def read_json(name: str) -> dict:
        try:
            return json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def tail(name: str, n: int) -> list[str]:
        try:
            lines = (root / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [ln for ln in lines if "eprecat" not in ln][-n:]

    progress = tail("progress.log", 80)
    running = None
    for ln in reversed(progress):
        parts = ln.split()
        if len(parts) >= 3 and parts[1] in ("START", "SCAN") and (parts[1] == "START" or parts[2] == "START"):
            running = {"repo": parts[-1], "phase": "scan" if parts[1] == "SCAN" else "ingest", "since": parts[0]}
            break
        if len(parts) >= 2 and (parts[1] == "DONE" or "ALL DONE" in ln or "MEMGUARD" in ln):
            break
    live = tail(f"{running['repo']}{'.scan' if running and running['phase'] == 'scan' else ''}.log", 12) if running else []
    memory = tail("memory.log", 60)
    return {"exists": True, "progress": progress, "running": running, "live": live, "memory": memory,
            "results": read_json("results.json"), "scans": read_json("scan_results.json")}


@app.get("/api/repos/{repo}/vulns")
def get_vulns(repo: str, severity: str | None = None, scanner: str | None = None, limit: int = 300) -> dict:
    store = _rstore(repo)
    try:
        runs = [dict(r) for r in store.q("SELECT * FROM scan_runs ORDER BY scanner")]
        counts = [dict(r) for r in store.q("SELECT scanner, severity, count(*) n FROM vuln_findings "
                                           "GROUP BY scanner, severity")]
        sql, args = "SELECT * FROM vuln_findings WHERE 1=1", []
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        if scanner:
            sql += " AND scanner=?"
            args.append(scanner)
        rows = [dict(r) for r in store.q(sql + " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                                         "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, path, line LIMIT ?",
                                         (*args, limit))]
    except Exception:
        return {"runs": [], "counts": [], "findings": []}
    return {"runs": runs, "counts": counts, "findings": rows}


class ScanReq(BaseModel):
    osv: bool = False
    snyk: bool = False


@app.post("/api/repos/{repo}/scan", dependencies=[Depends(csrf)])
def post_scan(repo: str, body: ScanReq) -> dict:
    _store(repo)
    if jobs().active(repo):
        raise HTTPException(409, f"'{repo}' already has a job running")
    cmd = [*_python(), "scan", repo, *(["--osv"] if body.osv else []), *(["--snyk"] if body.snyk else [])]
    return jobs().start("scan", repo, f"Vulnerability scan {repo}", cmd, setup.project_root(),
                        args={"osv": body.osv, "snyk": body.snyk})


@app.get("/api/repos/{repo}/findings")
def get_findings(repo: str, category: str | None = None, severity: str | None = None, path: str | None = None,
                 limit: int = 300) -> list[dict]:
    store = _rstore(repo)
    sql, args = "SELECT * FROM findings WHERE 1=1", []
    for col, val in (("category", category), ("severity", severity)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    if path:
        sql += " AND (path=? OR path LIKE ?)"
        args += [path, path.rstrip("/") + "/%"]
    rows = store.q(sql + " ORDER BY CASE severity WHEN 'block' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, path, line LIMIT ?",
                   (*args, min(limit, 2000)))
    return [{**{k: r[k] for k in ("category", "rule", "severity", "path", "line", "symbol", "message")},
             "detail": loads(r["detail"], {})} for r in rows]


class Assess(BaseModel):
    paths: list[str] = []
    symbols: list[str] = []


@app.post("/api/repos/{repo}/assess", dependencies=[Depends(csrf)])
def post_assess(repo: str, body: Assess) -> dict:
    from afd_oracle_ingest.git.walker import open_repo
    from afd_oracle_ingest.safety.gate import Gate
    store = _store(repo)
    git = open_repo(list_repos()[repo]["path"])
    return Gate(store, git).assess_change(body.paths or None, body.symbols or None)


@app.post("/api/repos/{repo}/discussions", dependencies=[Depends(csrf)])
def post_discussions(repo: str) -> dict:
    _store(repo)
    if jobs().active(repo):
        raise HTTPException(409, f"'{repo}' already has a job running")
    return jobs().start("discussions", repo, f"Sync GitHub discussions for {repo}",
                        [*_python(), "discussions", repo], setup.project_root())


# ---------------------------------------------------------------------------- insights, commits, reports
@app.get("/api/repos/{repo}/insights")
def get_insights(repo: str, status: str | None = None, label: str | None = None, kind: str | None = None,
                 q: str | None = None, limit: int = 500) -> list[dict]:
    store = _rstore(repo)
    sql, params = "SELECT * FROM insights WHERE 1=1", []
    for col, val in (("status", status), ("label", label), ("kind", kind)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    rows = store.q(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, min(limit, 2000)))
    out = []
    for r in rows:
        d = {k: r[k] for k in ("uid", "label", "kind", "title", "summary", "severity", "confidence", "status",
                               "verifier_notes", "workflow_run", "created_by_model", "created_at", "updated_at")}
        d["citations"] = loads(r["citations"], {})
        if q and q.lower() not in json.dumps(d).lower():
            continue
        out.append(d)
    return out


@app.get("/api/repos/{repo}/commits/{sha}")
def get_commit(repo: str, sha: str) -> dict:
    _store(repo)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise HTTPException(400, "bad commit hash")
    from afd_oracle_mcp import server
    fn = getattr(server.get_commit_diff, "fn", server.get_commit_diff)
    try:
        return fn(commit_hash=sha, repo=repo, max_bytes=60000, context_lines=3, paths=None)
    except Exception as e:
        raise HTTPException(404, str(e)) from e


def _reports_dir() -> Path:
    return setup.project_root() / "reports"


@app.get("/api/repos/{repo}/reports")
def get_reports(repo: str) -> list[dict]:
    d = _reports_dir()
    return [{"name": p.name, "modified": p.stat().st_mtime, "size": p.stat().st_size}
            for p in sorted(d.glob(f"{repo}-*.md"), key=lambda p: -p.stat().st_mtime)] if d.exists() else []


@app.get("/api/reports/{name}")
def get_report(name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.md", name):
        raise HTTPException(400, "bad report name")
    p = _reports_dir() / name
    if not p.is_file():
        raise HTTPException(404, "report not found")
    return {"name": name, "markdown": p.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------- jobs
@app.get("/api/jobs")
def get_jobs(repo: str | None = None, limit: int = 50) -> list[dict]:
    return jobs().list(repo, limit)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, tail: int = 200) -> dict:
    job = jobs().get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    p = jobs().log_path(job_id)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:] if p.exists() else []
    return {**job, "log": lines}


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(csrf)])
def post_cancel(job_id: str) -> dict:
    job = jobs().cancel(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Server-sent events: `log` lines as they are written, `job` snapshots, and a final `end`."""
    if not jobs().get(job_id):
        raise HTTPException(404, "unknown job")

    async def stream():
        pos = 0
        last = None
        while True:
            p = jobs().log_path(job_id)
            if p.exists():
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    yield f"event: log\ndata: {json.dumps(line)}\n\n"
            job = jobs().get(job_id) or {}
            snap = json.dumps({k: job.get(k) for k in ("status", "progress", "summary", "error")}, default=str)
            if snap != last:
                yield f"event: job\ndata: {snap}\n\n"
                last = snap
            if job.get("status") not in (*ACTIVE, "cancelling"):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------- explore (graph + vectors)
from .explore import router as explore_router  # noqa: E402

app.include_router(explore_router)


# ---------------------------------------------------------------------------- SPA
@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)
    target = (STATIC / path).resolve()
    if path and target.is_file() and target.is_relative_to(STATIC.resolve()):
        return FileResponse(target)
    index = STATIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "The web UI is not built yet. Run `uv run afd-oracle ui` (it builds packages/afd_oracle_ui/web)."},
                        status_code=503)


# ---------------------------------------------------------------------------- entry point
def build_web(force: bool = False) -> None:
    web = Path(__file__).resolve().parents[1] / "web"
    index = STATIC / "index.html"
    newest_src = max((p.stat().st_mtime for p in web.rglob("*") if p.is_file() and "node_modules" not in p.parts),
                     default=0)
    if not force and index.exists() and index.stat().st_mtime >= newest_src:
        return
    npm = shutil.which("npm")
    if not npm:
        if index.exists():
            return  # sources look newer (fresh checkout timestamps) but there is no Node to rebuild: serve what exists
        raise SystemExit("npm (Node.js 18+) is needed to build the dashboard UI the first time.")
    import subprocess
    if not (web / "node_modules").exists():
        subprocess.run([npm, "ci" if (web / "package-lock.json").exists() else "install"], cwd=web, check=True, stdin=subprocess.DEVNULL)
    subprocess.run([npm, "run", "build"], cwd=web, check=True, stdin=subprocess.DEVNULL)


def main(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn
    os.chdir(setup.project_root())  # settings read .env / infra/.env relative to the project
    build_web()
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
