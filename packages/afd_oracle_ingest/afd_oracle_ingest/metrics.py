"""Step 9: deterministic metric candidates (stored in ``candidates``; headline numbers also on symbols/files)."""
from __future__ import annotations

import json
import math
from collections import defaultdict

import networkx as nx

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import short_hash
from afd_oracle_core.store import Store

from .structure import file_of

GOD_MIN_METHODS = 15
GOD_MIN_LOC = 400


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def compute_metrics(store: Store, settings: Settings) -> dict:
    _SUBCLASS_CACHE.clear()
    cands: list[tuple[str, str, str, float, str]] = []
    sym = {r["uid"]: r for r in store.q("SELECT * FROM symbols")}
    alive = {u: r for u, r in sym.items() if r["alive"]}
    members: dict[str, list[str]] = defaultdict(list)
    for u, r in alive.items():
        if r["parent"]:
            members[r["parent"]].append(u)
    calls = [(r["src"], r["dst"]) for r in store.q("SELECT src, dst FROM edges WHERE kind='CALLS'")]
    events_by_uid: dict[str, list[str]] = defaultdict(list)
    for r in store.q("SELECT uid, hash FROM symbol_events"):
        events_by_uid[r["uid"]].append(r["hash"])
    author_of = {r["hash"]: r["author_email"] for r in store.q("SELECT hash, author_email FROM commits")}

    # -- god classes ---------------------------------------------------------------
    sym_metrics: dict[str, dict] = {}
    for u, r in alive.items():
        if r["kind"] != "class":
            continue
        mem = set(members.get(u, []))
        methods = sum(1 for m in mem if alive[m]["kind"] == "function")
        loc = r["loc"] or 0
        group = mem | {u}
        fan_in = sum(1 for s, d in calls if d in group and s not in group)
        fan_out = sum(1 for s, d in calls if s in group and d not in group)
        touched = [h for x in group for h in events_by_uid.get(x, [])]
        churn = len(set(touched))
        authors = len({author_of.get(h) for h in touched})
        score = methods / 20 + loc / 500 + fan_in / 20 + fan_out / 30 + churn / 50 + authors / 5
        m = {"methods": methods, "loc": loc, "fan_in": fan_in, "fan_out": fan_out, "churn": churn,
             "authors": authors, "god_score": round(score, 4)}
        sym_metrics[u] = m
        if methods >= GOD_MIN_METHODS or loc >= GOD_MIN_LOC:
            cands.append((f"god_class:{short_hash(u)}", "god_class", u, score, json.dumps({**m, "path": r["path"],
                                                                                            "qualname": r["qualname"]})))

    # -- import cycles -------------------------------------------------------------
    # Runtime imports only: a back-edge imported under `if TYPE_CHECKING:` / `import type` cannot cause an import-order
    # problem, and counting it turned flask's mostly type-hint back-edges into a 19-module "cycle".
    g = nx.DiGraph()
    for r in store.q("SELECT src, dst, props FROM edges WHERE kind='IMPORTS'"):
        props = json.loads(r["props"])
        if props.get("type_only"):
            continue
        g.add_edge(r["src"], r["dst"], names=props.get("names", []))
    file_cycle: dict[str, str] = {}
    for comp in nx.strongly_connected_components(g):
        if len(comp) < 2:
            continue
        files = sorted(comp)
        cid = f"cycle:{short_hash(*files)}"
        sub = g.subgraph(files)
        edges = [{"from": a, "to": b, "names": d.get("names", [])} for a, b, d in sorted(sub.edges(data=True))]
        cands.append((cid, "cycle", files[0], float(len(files)), json.dumps({"files": files, "edges": edges})))
        for f in files:
            file_cycle[f] = cid

    # -- leaky abstractions --------------------------------------------------------
    leaks: dict[str, dict] = {}
    for s, d in calls:
        if d not in alive or s not in sym:
            continue
        callee, caller = alive[d], sym[s]
        reason = None
        if _is_private(callee["name"]):
            if callee["parent"]:
                caller_owner = caller["parent"] or s
                callee_owner = callee["parent"]
                if caller_owner != callee_owner and caller_owner not in _subclasses_of(store, callee_owner):
                    reason = "private member used outside its class"
            elif caller["path"] != callee["path"]:
                reason = "module-private function used from another module"
        elif "/internal/" in f"/{callee['path']}":
            root = f"/{callee['path']}".split("/internal/")[0]
            if not f"/{caller['path']}".startswith(root + "/"):
                reason = "internal package used from outside its parent"
        if reason:
            entry = leaks.setdefault(d, {"callee": d, "callee_path": callee["path"], "reason": reason, "callers": []})
            entry["callers"].append({"uid": s, "path": caller["path"]})
    for r in store.q("SELECT src, dst FROM edges WHERE kind='INHERITS'"):
        child, base = r["src"], r["dst"]
        base_private = {alive[m]["name"]: m for m in members.get(base, []) if _is_private(alive[m]["name"])
                        and alive[m]["kind"] == "function"}
        for m in members.get(child, []):
            if alive[m]["name"] in base_private:
                d = base_private[alive[m]["name"]]
                entry = leaks.setdefault(d, {"callee": d, "callee_path": alive[d]["path"],
                                             "reason": "subclass overrides private method", "callers": []})
                entry["callers"].append({"uid": m, "path": alive[m]["path"], "override": True})
    for d, e in leaks.items():
        cands.append((f"leaky:{short_hash(d)}", "leaky_abstraction", d, float(len(e["callers"])), json.dumps(e)))

    # -- graveyard candidates ------------------------------------------------------
    created: dict[str, tuple[int, int, str]] = {}
    deleted: dict[str, tuple[int, int, str]] = {}
    # Mainline only: create+delete inside an unmerged/PR branch is iteration (abandoned-branch packets cover it).
    for r in store.q("SELECT se.uid, se.change, se.ts, se.seq, se.hash, se.old_uid FROM symbol_events se "
                     "JOIN commits c ON c.hash = se.hash WHERE c.on_main = 1 ORDER BY se.seq"):
        if r["change"] in ("created", "moved") and r["uid"] not in created:
            # A moved symbol keeps its original birth, so a restructure does not make it look short-lived.
            created[r["uid"]] = created.get(r["old_uid"]) or (r["ts"], r["seq"], r["hash"])
        if r["change"] == "deleted":
            deleted[r["uid"]] = (r["ts"], r["seq"], r["hash"])
    for u, r in sym.items():
        if r["alive"] or r["kind"] == "variable" or u not in deleted or u not in created:
            continue
        (cts, cseq, ch), (dts, dseq, dh) = created[u], deleted[u]
        if dseq <= cseq:
            continue
        base = {"uid": u, "path": r["path"], "qualname": r["qualname"], "kind": r["kind"], "created_in": ch,
                "deleted_in": dh, "lifetime_days": round((dts - cts) / 86400, 2), "commits_between": dseq - cseq,
                "modifications": r["modifications"]}
        if dts - cts < settings.short_lived_days * 86400 or dseq - cseq < settings.short_lived_commits:
            cands.append((f"short_lived:{short_hash(u)}", "short_lived_symbol", u,
                          1.0 / (1 + base["lifetime_days"]), json.dumps(base)))
        elif (r["modifications"] or 0) >= settings.churned_deleted_min:
            cands.append((f"churned_deleted:{short_hash(u)}", "churned_deleted", u, float(r["modifications"]),
                          json.dumps(base)))

    # -- hotspots ------------------------------------------------------------------
    file_metrics: dict[str, dict] = {}
    hot = []
    for r in store.q("SELECT path, churn, loc, commits FROM files WHERE alive=1"):
        score = (r["churn"] or 0) * math.log1p(r["loc"] or 0)
        file_metrics[r["path"]] = {"hotspot": round(score, 2)}
        hot.append((score, r["path"], r["churn"], r["loc"], r["commits"]))
    for score, path, churn, loc, ncommits in sorted(hot, reverse=True)[:25]:
        cands.append((f"hotspot:{short_hash(path)}", "hotspot", path, score,
                      json.dumps({"path": path, "churn": churn, "loc": loc, "commits": ncommits})))
    for f, cid in file_cycle.items():
        file_metrics.setdefault(f, {})["cycle"] = cid

    with store.tx() as conn:
        conn.execute("DELETE FROM candidates")
        conn.executemany("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?)", cands)
        conn.execute("UPDATE symbols SET metrics=NULL")
        conn.executemany("UPDATE symbols SET metrics=? WHERE uid=?",
                         [(json.dumps(m), u) for u, m in sym_metrics.items()])
        conn.execute("UPDATE files SET metrics=NULL")
        conn.executemany("UPDATE files SET metrics=? WHERE path=?",
                         [(json.dumps(m), p) for p, m in file_metrics.items()])
    counts: dict[str, int] = defaultdict(int)
    for c in cands:
        counts[c[1]] += 1
    return dict(counts)


_SUBCLASS_CACHE: dict[tuple[str, str], set[str]] = {}


def _subclasses_of(store: Store, cls_uid: str) -> set[str]:
    key = (str(store.path), cls_uid)
    if key not in _SUBCLASS_CACHE:
        g = nx.DiGraph()
        for r in store.q("SELECT src, dst FROM edges WHERE kind='INHERITS'"):
            g.add_edge(r["dst"], r["src"])
        _SUBCLASS_CACHE[key] = nx.descendants(g, cls_uid) if cls_uid in g else set()
    return _SUBCLASS_CACHE[key]


__all__ = ["compute_metrics", "file_of"]
