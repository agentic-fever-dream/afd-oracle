"""Composite risk per file and per symbol: churn, complexity, test gap, fragility, hazards, ownership concentration.

Each factor is turned into a percentile rank across the repository, then weighted. 0 = calm, 100 = handle with care.
"""
from __future__ import annotations

import json
import re

from afd_oracle_core.store import Store

from .source import SourceFile, is_test_path

WEIGHTS_FILE = {"churn": 0.2, "complexity": 0.15, "test_gap": 0.2, "fragility": 0.2, "hazards": 0.15, "ownership": 0.1}
WEIGHTS_SYM = {"fan_in": 0.25, "complexity": 0.15, "test_gap": 0.2, "fragility": 0.25, "hazards": 0.15}
BRANCH = re.compile(r"\b(if|elif|else if|for|while|except|catch|case|and|or|&&|\|\||\?)\b")


def _ranks(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(set(values.values()))
    pos = {v: i / max(1, len(ordered) - 1) for i, v in enumerate(ordered)}
    return {k: pos[v] for k, v in values.items()}


def build(store: Store, sources: dict[str, SourceFile]) -> list[tuple[str, str, float, str]]:
    prefix = f"sym:{store.repo_id}:"
    tested_targets = {r["target"] for r in store.q("SELECT DISTINCT target FROM test_map")}
    fixes_by_path: dict[str, int] = {}
    fixes_by_uid: dict[str, int] = {}
    for r in store.q("SELECT uid, path, count(*) n FROM fix_events GROUP BY uid, path"):
        fixes_by_path[r["path"]] = fixes_by_path.get(r["path"], 0) + r["n"]
        fixes_by_uid[r["uid"]] = r["n"]
    hazards_by_path: dict[str, int] = {}
    hazards_by_uid: dict[str, int] = {}
    for r in store.q("SELECT path, symbol, severity FROM findings WHERE severity IN ('warn','block')"):
        w = 3 if r["severity"] == "block" else 1
        hazards_by_path[r["path"]] = hazards_by_path.get(r["path"], 0) + w
        if r["symbol"]:
            hazards_by_uid[r["symbol"]] = hazards_by_uid.get(r["symbol"], 0) + w
    owners: dict[str, dict[str, int]] = {}
    for r in store.q("SELECT fc.path, c.author_email a, count(*) n FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
                     "WHERE c.is_merge=0 AND fc.ignored=0 GROUP BY fc.path, c.author_email"):
        owners.setdefault(r["path"], {})[r["a"]] = r["n"]
    files = {r["path"]: r for r in store.q("SELECT path, churn, loc FROM files WHERE alive=1 AND lang IS NOT NULL")}
    raw: dict[str, dict[str, float]] = {k: {} for k in WEIGHTS_FILE}
    sym_tested_paths = {t[len(prefix):].rsplit(":", 2)[0] for t in tested_targets if t.startswith(prefix)}
    for p, r in files.items():
        if is_test_path(p) or p not in sources:
            continue
        text = sources[p].text
        raw["churn"][p] = float(r["churn"] or 0)
        raw["complexity"][p] = float(len(BRANCH.findall(text)))
        raw["test_gap"][p] = 0.0 if (p in tested_targets or p in sym_tested_paths) else 1.0
        raw["fragility"][p] = float(fixes_by_path.get(p, 0))
        raw["hazards"][p] = float(hazards_by_path.get(p, 0))
        o = owners.get(p, {})
        raw["ownership"][p] = (max(o.values()) / sum(o.values())) if o and sum(o.values()) >= 5 else 0.0
    ranked = {k: _ranks(v) for k, v in raw.items()}
    rows = []
    for p in raw["churn"]:
        factors = {k: round(raw[k][p], 3) for k in WEIGHTS_FILE}
        score = 100 * sum(w * (ranked[k].get(p, 0) if k != "test_gap" else raw[k][p]) for k, w in WEIGHTS_FILE.items())
        rows.append((p, "file", round(score, 1), json.dumps(factors)))
    # symbols
    fan_in: dict[str, int] = {}
    for r in store.q("SELECT dst, count(*) n FROM edges WHERE kind='CALLS' GROUP BY dst"):
        fan_in[r["dst"]] = r["n"]
    sraw: dict[str, dict[str, float]] = {k: {} for k in WEIGHTS_SYM}
    for r in store.q("SELECT uid, path, body FROM symbols WHERE alive=1 AND kind IN ('function','class')"):
        if is_test_path(r["path"]):
            continue
        u = r["uid"]
        sraw["fan_in"][u] = float(fan_in.get(u, 0))
        sraw["complexity"][u] = float(len(BRANCH.findall(r["body"] or "")))
        sraw["test_gap"][u] = 0.0 if (u in tested_targets or r["path"] in tested_targets) else 1.0
        sraw["fragility"][u] = float(fixes_by_uid.get(u, 0))
        sraw["hazards"][u] = float(hazards_by_uid.get(u, 0))
    sranked = {k: _ranks(v) for k, v in sraw.items()}
    for u in sraw["fan_in"]:
        factors = {k: round(sraw[k][u], 3) for k in WEIGHTS_SYM}
        score = 100 * sum(w * (sranked[k].get(u, 0) if k != "test_gap" else sraw[k][u]) for k, w in WEIGHTS_SYM.items())
        rows.append((u, "symbol", round(score, 1), json.dumps(factors)))
    return rows
