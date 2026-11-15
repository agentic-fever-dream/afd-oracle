"""Orchestrates the deterministic ingestion steps. Every step is idempotent and can be run on its own."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pygit2

from afd_oracle_core.config import Settings, get_settings
from afd_oracle_core.registry import open_store, register
from afd_oracle_core.store import Store

from .ast.parse_cache import ParseCache
from .cochange import compute_cochange
from .git.branches import classify_branches
from .git.reverts import detect_reverts
from .git.walker import open_repo
from .history import ingest_history
from .ignore import IgnoreRules
from .lifecycle import link_cross_commit_moves
from .metrics import compute_metrics
from .structure import build_snapshot


@dataclass
class Context:
    repo_id: str
    repo: pygit2.Repository
    store: Store
    cache: ParseCache
    settings: Settings
    ignore: IgnoreRules
    workers: int | None = None
    log: Callable[[str], None] = print
    timings: dict[str, float] = field(default_factory=dict)
    # progress(step, done, total): machine-readable progress for the dashboard (afd-oracle ingest --progress-json)
    progress: Callable[[str, int, int], None] | None = None


def open_context(repo_path: Path, repo_id: str | None = None, settings: Settings | None = None,
                 workers: int | None = None, log: Callable[[str], None] = print,
                 ignore_file: Path | None = None) -> Context:
    settings = settings or get_settings()
    repo = open_repo(repo_path)
    rid = repo_id or Path(repo.path.rstrip("/\\")).name.removesuffix(".git")
    if rid in (".git", ""):
        rid = Path(repo.workdir or repo_path).resolve().name
    register(rid, Path(repo.path), settings)
    store = open_store(rid, settings)
    ignore = IgnoreRules.load(*(p for p in (ignore_file, Path(repo.workdir or "") / ".oracleignore", Path(".oracleignore")) if p))
    return Context(rid, repo, store, ParseCache(settings.parse_cache_path), settings, ignore, workers, log)


def _timed(ctx: Context, name: str, fn: Callable[[], object]) -> object:
    if ctx.progress:
        ctx.progress(name, 0, 1)
    t0 = time.perf_counter()
    out = fn()
    ctx.timings[name] = round(time.perf_counter() - t0, 3)
    ctx.log(f"  {name}: {out}  ({ctx.timings[name]}s)")
    if ctx.progress:
        ctx.progress(name, 1, 1)
    return out


def _catalog_summary(r: dict) -> dict:
    return {k: r[k] for k in ("builders", "candidates", "with_hits", "errors", "s")} | {"index": r["index"]}


def run_deterministic(ctx: Context, full: bool = False) -> dict:
    """Steps 2-9: history, AST lifecycle, snapshot, reverts, branches, co-change, metrics (store only)."""
    if full:
        with ctx.store.tx() as conn:
            for t in ("commits", "file_changes", "symbol_events", "symbols", "files", "reverts", "branches",
                      "branch_commits", "edges", "cochange", "candidates"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DROP TABLE IF EXISTS diff_lines")
            for t in ("symbol_versions", "file_modes", "line_origins", "blame_done", "diff_lines_aux"):
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute("DELETE FROM meta WHERE key IN ('modes_seq', 'symver_seq', 'aux_seq')")
            conn.execute("DELETE FROM meta WHERE key='difflines_seq'")
            conn.execute("DELETE FROM meta WHERE key IN ('processed_tips', 'projected_seq')")
    first_new_seq = ctx.store.next_seq()
    out: dict[str, object] = {"repo": ctx.repo_id}
    hist_progress = (lambda done, total: ctx.progress("history", done, total)) if ctx.progress else None
    out["history"] = _timed(ctx, "history", lambda: ingest_history(
        ctx.repo, ctx.store, ctx.cache, ctx.settings, ctx.ignore, ctx.workers, progress=hist_progress).__dict__)
    out["snapshot"] = _timed(ctx, "snapshot", lambda: build_snapshot(ctx.repo, ctx.store, ctx.cache, ctx.ignore,
                                                                     ctx.workers))
    out["moves"] = _timed(ctx, "moves", lambda: link_cross_commit_moves(ctx.store))
    out["reverts"] = _timed(ctx, "reverts", lambda: detect_reverts(ctx.repo, ctx.store, ctx.settings, first_new_seq))
    out["branches"] = _timed(ctx, "branches", lambda: classify_branches(ctx.repo, ctx.store, ctx.settings))
    out["cochange"] = _timed(ctx, "cochange", lambda: compute_cochange(ctx.store, ctx.settings))
    out["metrics"] = _timed(ctx, "metrics", lambda: compute_metrics(ctx.store, ctx.settings))
    if getattr(ctx.settings, "safety", True):
        from .safety.step import run_safety
        out["safety"] = _timed(ctx, "safety", lambda: run_safety(ctx.repo, ctx.store, ctx.cache, ctx.ignore,
                                                                 ctx.settings, ctx.workers, log=ctx.log))
    if getattr(ctx.settings, "catalog", True):
        from .catalog.runner import run_catalog
        cat_progress = (lambda step, done, total: ctx.progress(step, done, total)) if ctx.progress else None
        out["catalog"] = _timed(ctx, "catalog", lambda: _catalog_summary(run_catalog(
            ctx.repo, ctx.store, ctx.settings, log=ctx.log, progress=cat_progress, cache=ctx.cache,
            ignore=ctx.ignore)))
    out["parse_cache"] = ctx.cache.stats()
    return out
