"""Run every registered catalogue builder and store its hits in the ``candidates`` table.

Each kind is ranked by score and capped at ``settings.catalog_max_per_kind``. A builder that raises is logged and
skipped; it never breaks ingestion. Rows use ids ``cat:<kind>:<hash>`` and are fully replaced on every run.
"""
from __future__ import annotations

import json
import math
import time
import traceback
from typing import Callable, Iterable

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import short_hash
from afd_oracle_core.store import Store

from . import load_all
from .base import Ctx, Hit, spec
from .difflines import build_diff_lines


def _clean(h: Hit) -> dict:
    return {"title": h.title[:240], "evidence": h.evidence, "commits": list(dict.fromkeys(h.commits))[:12],
            "diffs": [list(d) for d in list(dict.fromkeys(map(tuple, h.diffs)))[:6]],
            "paths": list(dict.fromkeys(h.paths))[:40], "symbols": list(dict.fromkeys(h.symbols))[:40]}


def run_catalog(repo: pygit2.Repository, store: Store, settings: Settings, only: Iterable[str] | None = None,
                log: Callable[[str], None] = lambda *_: None, progress=None, cache=None, ignore=None,
                enrich: bool = True) -> dict:
    t0 = time.perf_counter()
    dl = build_diff_lines(repo, store, progress=(lambda d, t: progress("catalog_index", d, t)) if progress else None)
    if enrich:
        from .enrich import build_all
        if progress:
            progress("catalog_enrich", 0, 1)
        dl["enrich"] = build_all(repo, store, settings, cache, ignore, log=log)
        if progress:
            progress("catalog_enrich", 1, 1)
    else:
        from .enrich import ensure
        ensure(store)
    registry = load_all()
    wanted = set(only) if only else set(registry)
    cap = int(getattr(settings, "catalog_max_per_kind", 40) or 10 ** 9)
    ctx = Ctx(repo, store, settings)
    meta = spec()["candidates"]
    kinds: dict[str, dict] = {}
    rows: list[tuple] = []
    for i, (cid, fn) in enumerate(sorted(registry.items())):
        if cid not in wanted:
            continue
        ctx._current = cid
        t = time.perf_counter()
        try:
            hits = [h for h in fn(ctx) if h is not None]
        except Exception as e:  # a broken builder must never break ingestion
            kinds[cid] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=3)[-600:]}
            log(f"  catalog {cid}: ERROR {e}")
            continue
        seen: dict[str, Hit] = {}
        for h in hits:
            if not h.subject or (isinstance(h.score, float) and math.isnan(h.score)):
                continue
            if h.subject not in seen or h.score > seen[h.subject].score:
                seen[h.subject] = h
        ranked = sorted(seen.values(), key=lambda h: -h.score)
        kept = ranked[:cap]
        if len(ranked) > cap:
            ctx.drop("over_cap", len(ranked) - cap)
        m = meta[cid]
        for h in kept:
            rows.append((f"cat:{cid}:{short_hash(h.subject)}", cid, h.subject, float(h.score),
                         json.dumps({"catalog": True, "family": m["family"], **_clean(h)}, default=str)))
        kinds[cid] = {"hits": len(ranked), "kept": len(kept), "s": round(time.perf_counter() - t, 2)}
        if progress:
            progress("catalog", i + 1, len(registry))
    with store.tx() as conn:
        if only:
            conn.executemany("DELETE FROM candidates WHERE id LIKE ?", [(f"cat:{k}:%",) for k in wanted])
        else:
            conn.execute("DELETE FROM candidates WHERE id LIKE 'cat:%'")
        conn.executemany("INSERT OR REPLACE INTO candidates(id, kind, subject, score, props) VALUES (?,?,?,?,?)", rows)
    errors = {k: v["error"] for k, v in kinds.items() if "error" in v}
    from . import IMPORT_ERRORS
    errors.update({f"module:{m}": e for m, e in IMPORT_ERRORS.items()})
    return {"index": dl, "builders": len([k for k, v in kinds.items() if "error" not in v]), "candidates": len(rows),
            "with_hits": sum(1 for v in kinds.values() if v.get("kept")), "errors": errors,
            "noise": dict(sorted(ctx.noise.items())), "kinds": kinds, "s": round(time.perf_counter() - t0, 1)}
