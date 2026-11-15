"""Work packets for catalogue candidates: one generic, self-describing shape for every kind.

The packet carries the catalogue entry's own analyst question, insight label and noise notes, so a single generic
miner (``oracle-catalog`` workflow) can judge every kind without per-kind prompts.
"""
from __future__ import annotations

from collections import defaultdict

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import short_hash
from afd_oracle_core.store import Store, loads

from .base import spec

PRIORITY_BASE = {1: 0.75, 2: 0.5, 3: 0.25}


def catalog_packets(repo: pygit2.Repository, store: Store, settings: Settings, limit: int,
                    brief, patch, fit) -> list[tuple]:
    """``brief(store, sha)``, ``patch(repo, sha, path, n)`` and ``fit(build, max_tokens)`` come from packets.py."""
    meta = spec()["candidates"]
    by_kind: dict[str, list] = defaultdict(list)
    for c in store.q("SELECT * FROM candidates WHERE id LIKE 'cat:%' ORDER BY kind, score DESC"):
        by_kind[c["kind"]].append(c)
    out = []
    for kind, rows in by_kind.items():
        m = meta.get(kind)
        if not m:
            continue
        top = max((r["score"] for r in rows), default=1.0) or 1.0
        for c in rows[:limit]:
            p = loads(c["props"], {})

            def build(n: int, p=p, c=c, m=m, kind=kind) -> dict:
                return {
                    "kind": "catalog", "subkind": kind, "repo": store.repo_id, "candidate": m["name"],
                    "family": m["family"], "insight_label": m["label"], "insight_kind": kind,
                    "title": p.get("title"), "pitfall": m["pitfall"], "question": m["question"],
                    "dismiss_if": m["noise"], "evidence": p.get("evidence", {}),
                    "paths": p.get("paths", []), "symbols": p.get("symbols", []),
                    "commits": [brief(store, h) for h in p.get("commits", [])],
                    "diffs": [{"hash": sha, "path": path, "patch": patch(repo, sha, path, n)}
                              for sha, path in p.get("diffs", [])] if n else [],
                }
            prio = PRIORITY_BASE.get(m["priority"], 0.25) + 0.2 * (c["score"] or 0) / top
            out.append((f"catalog:{kind}:{short_hash(c['subject'])}", "catalog", kind, round(prio, 4),
                        fit(build, settings.packet_max_tokens)))
    return out
