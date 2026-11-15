"""``afd-oracle`` command-line interface: database setup, health checks, ingestion and maintenance."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import typer

from afd_oracle_core import runtime
from afd_oracle_core.config import get_settings
from afd_oracle_core.graph import apply_schema
from afd_oracle_core.insights import InsightService
from afd_oracle_core.registry import list_repos, open_store, resolve_repo
from afd_oracle_core.vectors import COLLECTIONS, ensure_collections

from .git.walker import mirror, open_repo

app = typer.Typer(help="Oracle knowledge base", no_args_is_help=True, add_completion=False)


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command("init-db")
def init_db(graph: bool = typer.Option(True, help="Apply the Neo4j schema"),
            vectors: bool = typer.Option(True, help="Create the Qdrant collections")) -> None:
    """Apply Neo4j constraints/indexes and create Qdrant collections (idempotent)."""
    s = get_settings()
    out = {}
    if graph:
        drv = runtime.graph_driver(s, required=True)
        out["neo4j_statements"] = apply_schema(drv, s.neo4j_database)
    if vectors:
        client, emb = runtime.vectors(s)
        out["qdrant_created"] = ensure_collections(client, emb.dim)
    _echo(out)


@app.command()
def doctor() -> None:
    """Check that Neo4j and Qdrant are reachable and the schema/collections exist."""
    s = get_settings()
    report: dict = {"ok": True}
    try:
        drv = runtime.graph_driver(s, required=True)
        with drv.session(database=s.neo4j_database) as sess:
            names = [r["name"] for r in sess.run("SHOW CONSTRAINTS YIELD name RETURN name")]
        report["neo4j"] = {"reachable": True, "constraints": sorted(names)}
        if len(names) < 8:
            report["ok"] = False
            report["neo4j"]["hint"] = "run `afd-oracle init-db`"
    except Exception as e:
        report["ok"] = False
        report["neo4j"] = {"reachable": False, "error": str(e)}
    try:
        client = runtime.qdrant(s)
        have = {c.name for c in client.get_collections().collections}
        report["qdrant"] = {"reachable": True, "collections": {c: c in have for c in COLLECTIONS}}
        if not all(c in have for c in COLLECTIONS):
            report["ok"] = False
            report["qdrant"]["hint"] = "run `afd-oracle init-db`"
    except Exception as e:
        report["ok"] = False
        report["qdrant"] = {"reachable": False, "error": str(e)}
    report["repos"] = list_repos(s)
    _echo(report)
    raise typer.Exit(0 if report["ok"] else 1)


def _looks_like_url(x: str) -> bool:
    return bool(re.match(r"^(https?://|git@|ssh://|git://)", x))


@app.command()
def ingest(source: str = typer.Argument(..., help="Local repo path or a git URL (mirrored into repos/)"),
           repo_id: Optional[str] = typer.Option(None, "--repo-id", help="Name for this repo (default: dir name)"),
           full: bool = typer.Option(False, "--full", help="Discard stored history and re-ingest from scratch"),
           fetch_pulls: bool = typer.Option(False, "--fetch-pulls", help="Also fetch refs/pull/* (URL sources)"),
           graph: bool = typer.Option(True, help="Project into Neo4j"),
           vectors: bool = typer.Option(True, help="Embed into Qdrant"),
           packets: bool = typer.Option(True, help="Build LLM work packets"),
           workers: Optional[int] = typer.Option(None, help="Parser processes (default: CPUs-2)"),
           packet_limit: int = typer.Option(200, help="Max packets per kind (0 = no cap)"),
           progress_json: bool = typer.Option(False, "--progress-json",
                                              help="Emit one JSON progress event per line (used by the dashboard)")) -> None:
    """Run the full deterministic pipeline: history -> AST -> oracle -> Neo4j -> Qdrant -> packets."""
    import time as _time

    from .embed import embed_all
    from .packets import build_packets
    from .pipeline import open_context, run_deterministic
    from .writers.neo4j_writer import project

    def emit(step: str, done: int, total: int, **extra) -> None:
        if progress_json:
            print(json.dumps({"event": "progress", "step": step, "done": done, "total": total, **extra}), flush=True)

    def log(msg: str) -> None:
        if progress_json:
            print(json.dumps({"event": "log", "msg": msg}), flush=True)
        else:
            typer.echo(msg)

    s = get_settings()
    path = Path(source)
    origin = source if _looks_like_url(source) else str(Path(source).resolve())
    if _looks_like_url(source):
        rid = repo_id or source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        emit("clone", 0, 1)
        path = mirror(source, s.repos_dir / f"{rid}.git", fetch_pulls)
        emit("clone", 1, 1)
        repo_id = rid
    drv = runtime.graph_driver(s, required=True) if graph else None
    ctx = open_context(path, repo_id, s, workers, log=log)
    ctx.progress = emit if progress_json else None
    log(f"ingesting {ctx.repo_id} from {ctx.repo.path}")
    out = run_deterministic(ctx, full=full)
    if drv is not None:
        emit("neo4j", 0, 1)
        out["neo4j"] = project(ctx.store, drv, s.neo4j_database, ctx.repo.path)
        emit("neo4j", 1, 1)
        log(f"  neo4j: {out['neo4j']}")
    if vectors:
        client, emb = runtime.vectors(s)
        out["qdrant"] = embed_all(ctx.repo, ctx.store, s, client, emb, progress=emit if progress_json else None)
        log(f"  qdrant: {out['qdrant']}")
    if packets:
        emit("packets", 0, 1)
        out["packets"] = build_packets(ctx.repo, ctx.store, s, packet_limit)
        emit("packets", 1, 1)
        log(f"  packets: {out['packets']}")
    # Where this repo came from, so identify_repo can match a working copy (by root commit or remote URL).
    remotes = sorted({r.url for r in ctx.repo.remotes if r.url} | ({source} if _looks_like_url(source) else set()))
    for key, value in (("origin", origin), ("remotes", json.dumps(remotes)),
                       ("head", str((out.get("snapshot") or {}).get("head") or "")),
                       ("ingested_at", str(int(_time.time())))):
        ctx.store.set_meta(key, value)
    out["timings_s"] = ctx.timings
    if progress_json:
        print(json.dumps({"event": "result", "result": out}, default=str), flush=True)
    else:
        _echo(out)


def _ctx(repo: Optional[str]):
    s = get_settings()
    rid, git_path = resolve_repo(repo, s)
    return s, rid, open_repo(git_path), open_store(rid, s)


@app.command("project")
def project_cmd(repo: Optional[str] = typer.Argument(None)) -> None:
    """(Re)project a repo's store into Neo4j."""
    from .writers.neo4j_writer import project
    s, rid, git, store = _ctx(repo)
    _echo(project(store, runtime.graph_driver(s, required=True), s.neo4j_database, git.path))


@app.command()
def embed(repo: Optional[str] = typer.Argument(None),
          force: bool = typer.Option(False, "--force", help="Re-embed everything, not just new or changed items")) -> None:
    """Embed new/changed symbols and commits into Qdrant and recompute idiom clusters."""
    from .embed import embed_all
    s, rid, git, store = _ctx(repo)
    if force:
        with store.tx() as conn:
            conn.execute("UPDATE symbols SET embedded_hash=NULL")
            conn.execute("UPDATE commits SET embedded=0")
    client, emb = runtime.vectors(s)
    _echo(embed_all(git, store, s, client, emb))


@app.command("packets")
def packets_cmd(repo: Optional[str] = typer.Argument(None), limit: int = 200,
                reopen: bool = typer.Option(False, help="Mark claimed packets open again")) -> None:
    """Rebuild work packets (keeps open/claimed status)."""
    from .packets import build_packets
    s, rid, git, store = _ctx(repo)
    if reopen:
        with store.tx() as conn:
            conn.execute("UPDATE packets SET status='open' WHERE status='claimed'")
    _echo(build_packets(git, store, s, limit))


@app.command()
def status(repo: Optional[str] = typer.Argument(None)) -> None:
    """Counts of what has been ingested and mined for a repo."""
    s, rid, git, store = _ctx(repo)
    tables = ["commits", "file_changes", "symbol_events", "symbols", "files", "reverts", "branches", "edges",
              "cochange", "candidates", "packets", "insights"]
    out = {t: store.q1(f"SELECT count(*) n FROM {t}")["n"] for t in tables}
    out["alive_symbols"] = store.q1("SELECT count(*) n FROM symbols WHERE alive=1")["n"]
    out["packets_by_status"] = {r["status"]: r["n"] for r in store.q(
        "SELECT status, count(*) n FROM packets GROUP BY status")}
    out["insights_by_status"] = {r["status"]: r["n"] for r in store.q(
        "SELECT status, count(*) n FROM insights GROUP BY status")}
    out["head"] = store.get_meta("head")
    _echo({"repo": rid, **out})


@app.command("sync-insights")
def sync_insights(repo: Optional[str] = typer.Argument(None)) -> None:
    """Replay insights that were stored while Neo4j/Qdrant were unreachable."""
    s, rid, git, store = _ctx(repo)
    svc = InsightService(store, git, s.neo4j_database, driver=lambda: runtime.graph_driver(s),
                         vectors=lambda: runtime.vectors(s))
    _echo(svc.sync_all())


@app.command("repos")
def repos() -> None:
    """List ingested repositories."""
    _echo(list_repos())


@app.command("ui")
def ui(port: int = typer.Option(8765, help="Port on 127.0.0.1"),
       no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser tab")) -> None:
    """Start the dashboard (add, ingest, mine and browse repositories) at http://127.0.0.1:<port>."""
    from afd_oracle_ui.app import main
    main(port=port, open_browser=not no_browser)


@app.command("safety")
def safety_cmd(repo: Optional[str] = typer.Argument(None),
               osv: bool = typer.Option(False, "--osv", help="Also query osv.dev for advisories on pinned "
                                                             "requirements (sends package names to osv.dev)")) -> None:
    """Re-run the change-safety detectors for a repository (they also run on every ingest)."""
    from .ast.parse_cache import ParseCache
    from .ignore import IgnoreRules
    from .safety.step import run_safety
    s, rid, git, store = _ctx(repo)
    ignore = IgnoreRules.load(Path(".oracleignore"))
    out = run_safety(git, store, ParseCache(s.parse_cache_path), ignore, s, log=typer.echo)
    if osv:
        from .safety import deps
        from .safety.source import sources_at
        from .git.walker import default_branch
        _, head = default_branch(git)
        out["osv"] = deps.osv_check(deps.pinned_packages(sources_at(git, head, ParseCache(s.parse_cache_path), ignore)))
    _echo(out)


@app.command("catalog")
def catalog_cmd(repo: Optional[str] = typer.Argument(None),
                only: Optional[list[str]] = typer.Option(None, "--only", help="Run just these catalogue ids"),
                list_: bool = typer.Option(False, "--list", help="List catalogue ids and whether they are built"),
                rebuild_index: bool = typer.Option(False, "--rebuild-index", help="Rebuild the diff_lines index"),
                packets: bool = typer.Option(True, help="Rebuild work packets afterwards")) -> None:
    """Run the candidate catalogue (about 190 extra candidate kinds); it also runs on every ingest."""
    from .catalog import load_all, spec
    from .catalog.difflines import reset_diff_lines
    from .catalog.runner import run_catalog
    if list_:
        reg = load_all()
        _echo({cid: {"name": m["name"], "family": m["family"], "priority": m["priority"], "built": cid in reg}
               for cid, m in spec()["candidates"].items()})
        return
    s, rid, git, store = _ctx(repo)
    if rebuild_index:
        reset_diff_lines(store)
    out = run_catalog(git, store, s, only=only, log=typer.echo)
    if packets:
        from .packets import build_packets
        out["packets"] = build_packets(git, store, s)
    _echo(out)


@app.command("sandbox")
def sandbox_cmd(action: str = typer.Argument(..., help="coverage | mutate | helm"),
                repo: Optional[str] = typer.Argument(None),
                target: Optional[list[str]] = typer.Option(None, "--target", help="mutate: path/to/file.py::Qual.name"),
                test: Optional[list[str]] = typer.Option(None, "--test", help="mutate: limit to these tests"),
                top: int = typer.Option(0, help="mutate: also pick the N riskiest under-tested candidate functions"),
                max_mutants: int = typer.Option(15, "--max", help="mutate: mutants per function"),
                reinstall: bool = typer.Option(False, help="Reinstall dependencies from scratch")) -> None:
    """Run the repository's own tests inside a Docker container (no network, dropped capabilities, resource
    limits): measure per-test coverage, or mutation-test functions; or render its Helm charts (helm).
    Results feed the catalogue builders."""
    from . import sandbox
    s, rid, git, store = _ctx(repo)
    if action == "coverage":
        _echo(sandbox.coverage(git, store, s, log=typer.echo, reinstall=reinstall))
    elif action == "mutate":
        targets = list(target or [])
        if top:
            targets += sandbox.pick_mutation_targets(store, top)
        if not targets:
            raise typer.BadParameter("give --target path::Qual.name or --top N")
        _echo(sandbox.mutate(git, store, s, targets, tests=list(test or []) or None, limit=max_mutants, log=typer.echo))
    elif action == "helm":
        _echo(sandbox.helm(git, store, s, log=typer.echo))
    else:
        raise typer.BadParameter("action must be coverage, mutate or helm")


@app.command("scan")
def scan_cmd(repo: Optional[str] = typer.Argument(None),
             scanner: Optional[list[str]] = typer.Option(None, "--scanner", help="Run only these (repeatable)"),
             osv: bool = typer.Option(False, "--osv", help="Also run osv-scanner (sends package names to osv.dev)"),
             snyk: bool = typer.Option(False, "--snyk", help="Also run `snyk test` (sends manifests to Snyk; needs "
                                                              "SNYK_TOKEN)"),
             snyk_code: bool = typer.Option(False, "--snyk-code", help="Also run `snyk code test` (UPLOADS SOURCE CODE "
                                                                        "to Snyk; needs SNYK_TOKEN)"),
             graph: bool = typer.Option(True, help="Project findings into Neo4j"),
             vectors: bool = typer.Option(True, help="Embed findings into Qdrant (security_findings)")) -> None:
    """Run vulnerability scanners in Docker over HEAD (semgrep, bandit, trivy, gitleaks, checkov, hadolint by
    default; none of these sends code or package names anywhere) and store the findings in the store, graph and
    vectors."""
    from . import scan as scanmod
    s, rid, git, store = _ctx(repo)
    names = list(scanner or scanmod.DEFAULT) + (["osv-scanner"] if osv else []) + (["snyk"] if snyk else []) + \
        (["snyk-code"] if snyk_code else [])
    out = scanmod.scan(git, store, s, list(dict.fromkeys(names)), log=typer.echo)
    if graph:
        drv = runtime.graph_driver(s)
        out["neo4j"] = scanmod.project(store, drv, s.neo4j_database) if drv else "skipped: Neo4j unreachable"
    if vectors:
        try:
            client, emb = runtime.vectors(s)
            out["qdrant"] = scanmod.embed(store, client, emb)
        except Exception as e:  # noqa: BLE001
            out["qdrant"] = f"skipped: {e}"
    _echo(out)


@app.command("assess")
def assess_cmd(repo: str = typer.Argument(...),
               path: list[str] = typer.Option([], "--path", "-p", help="File you plan to change (repeatable)"),
               symbol: list[str] = typer.Option([], "--symbol", "-s", help="Symbol you plan to change (repeatable)")) -> None:
    """Before a change: blast radius, coupling, contracts, API, tests, hazards and history for files/symbols."""
    from .safety.gate import Gate
    s, rid, git, store = _ctx(repo)
    _echo(Gate(store, git).assess_change(path or None, symbol or None))


@app.command("check")
def check_cmd(repo: str = typer.Argument(...),
              workdir: Optional[Path] = typer.Option(None, "--workdir", "-w", help="Working copy to diff against HEAD"),
              diff_file: Optional[Path] = typer.Option(None, "--diff", help="A unified diff file instead")) -> None:
    """After a change: block / warn / pass verdict with findings and the tests to run (exit 2 on block)."""
    from .safety.gate import Gate
    s, rid, git, store = _ctx(repo)
    diff = diff_file.read_text(encoding="utf-8") if diff_file else None
    out = Gate(store, git).check_diff(diff=diff, workdir=str(workdir) if workdir and not diff else None)
    _echo(out)
    if out.get("verdict") == "block":
        raise typer.Exit(2)


@app.command("coverage")
def coverage_cmd(repo: str = typer.Argument(...),
                 data: Path = typer.Option(..., "--data", help="coverage.py data file recorded with test contexts "
                                                               "(pytest --cov --cov-context=test)"),
                 root: Path = typer.Option(..., "--root", help="Working copy the coverage was recorded in")) -> None:
    """Import per-test coverage into the test map (precise affected-test selection)."""
    from .ast.parse_cache import ParseCache
    from .git.walker import default_branch
    from .ignore import IgnoreRules
    from .safety.source import sources_at
    from .safety.testmap import import_coverage
    s, rid, git, store = _ctx(repo)
    _, head = default_branch(git)
    srcs = sources_at(git, head, ParseCache(s.parse_cache_path), IgnoreRules.load(Path(".oracleignore")))
    _echo({"coverage_links": import_coverage(store, srcs, rid, str(data), str(root))})


@app.command("discussions")
def discussions_cmd(repo: str = typer.Argument(...),
                    max_pages: int = typer.Option(30, help="Pages of 100 per kind (PRs, issues)")) -> None:
    """Sync GitHub pull requests and issues and link them to commits (incremental; uses gh auth or GITHUB_TOKEN)."""
    from .safety import discussions
    s, rid, git, store = _ctx(repo)
    _echo(discussions.sync(store, max_pages=max_pages, log=typer.echo))


@app.command("mutate")
def mutate_cmd(target: str = typer.Argument(..., help="path/to/file.py::Qual.name, relative to --cwd"),
               cwd: Path = typer.Option(Path("."), "--cwd", help="Working copy containing the code and tests"),
               python: Optional[str] = typer.Option(None, "--python", help="The project's Python (default: this one)"),
               tests: list[str] = typer.Option([], "--test", "-t", help="Test node ids/paths to run (repeatable; "
                                                                         "default: from the test map, else all)"),
               repo: Optional[str] = typer.Option(None, "--repo", help="Repository id for test-map lookup"),
               limit: int = typer.Option(15, "--max", help="Maximum mutants"),
               timeout: float = typer.Option(300.0, help="Seconds per test run")) -> None:
    """Targeted mutation testing: would the selected tests catch a regression in this function?"""
    from .safety.mutate import run
    selected = list(tests)
    if not selected and repo:
        s, rid, git, store = _ctx(repo)
        rel, _, qual = target.partition("::")
        uid = store.q1("SELECT uid FROM symbols WHERE alive=1 AND path=? AND qualname=?", (rel, qual))
        rows = store.q("SELECT DISTINCT test FROM test_map WHERE target IN (?, ?)", (uid["uid"] if uid else "", rel))
        selected = sorted({r["test"] for r in rows})
    out = run(cwd.resolve(), target, python=python, tests=selected or None, limit=limit, timeout=timeout,
              log=typer.echo)
    _echo(out)


def _run_ui_job(start) -> None:
    """Start a dashboard job from the terminal and stream it until it ends."""
    from fastapi import HTTPException

    from afd_oracle_ui import app as ui
    from afd_oracle_ui.jobs import wait_and_stream
    try:
        job = start(ui)
    except HTTPException as e:
        raise typer.BadParameter(str(e.detail)) from e
    typer.echo(f"started {job['id']} (also visible in the dashboard's Activity page)")
    end = wait_and_stream(ui.jobs(), job["id"], out=typer.echo)
    _echo({k: end.get(k) for k in ("id", "status", "error", "summary")})
    if end.get("status") != "succeeded":
        raise typer.Exit(1)


@app.command("update")
def update(repo: str = typer.Argument(..., help="Repository id (see `repos`)"),
           full: bool = typer.Option(False, "--full", help="Rebuild from scratch")) -> None:
    """Refresh an ingested repository from where it came from (only new commits unless --full)."""
    _run_ui_job(lambda ui: ui.post_ingest(repo, ui.Update(full=full)))


@app.command("mine")
def mine(repo: str = typer.Argument(..., help="Repository id"),
         max_packets: Optional[int] = typer.Option(None, "--max", help="Mine at most N candidates (default: all)"),
         estimate: bool = typer.Option(False, "--estimate", help="Only print the candidate count and token estimate")) -> None:
    """Mine candidates for verified insights (runs Claude Code headless; same as the dashboard's Mine button)."""
    from afd_oracle_ui import app as ui
    if estimate:
        _echo(ui.get_estimate(repo, max_packets))
        return
    _run_ui_job(lambda u: u.post_mine(repo, u.Mine(max=max_packets)))


@app.command("service")
def service_cmd(action: str = typer.Argument("status", help="install | uninstall | start | stop | status"),
                port: int = typer.Option(8765, help="Port on 127.0.0.1")) -> None:
    """Run the dashboard as an OS service: starts at login, restarts on failure, independent of any terminal."""
    from afd_oracle_ui import service
    if action == "install":
        _echo(service.install(port))
    elif action == "uninstall":
        _echo(service.uninstall())
    elif action == "start":
        service.start()
        _echo(service.status(port))
    elif action == "stop":
        service.stop()
        _echo(service.status(port))
    elif action == "status":
        _echo(service.status(port))
    else:
        raise typer.BadParameter("action must be install, uninstall, start, stop or status")


@app.command("setup")
def setup_cmd(mcp: bool = typer.Option(True, help="Register the oracle MCP server for your user"),
              skill: bool = typer.Option(True, help="Install the afd-oracle Claude skill"),
              service: bool = typer.Option(True, help="Run the dashboard as a login service (restarts on failure)")) -> None:
    """Make the knowledge base available to Claude Code in every repository (same as the dashboard's buttons)."""
    from afd_oracle_ui import setup as ui_setup
    out = {}
    if mcp:
        out["mcp"] = ui_setup.install_mcp()
    if skill:
        out["skill"] = ui_setup.install_skill()
    if service:
        from afd_oracle_ui import service as ui_service
        out["service"] = ui_service.install()
    out["health"] = {k: v["ok"] for k, v in ui_setup.health()["checks"].items()}
    _echo(out)


if __name__ == "__main__":
    app()
