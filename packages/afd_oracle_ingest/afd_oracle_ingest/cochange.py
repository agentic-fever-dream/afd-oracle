"""Step 8: file co-change (CO_COMMITTED) with Jaccard / confidence, cross-module and structural-link flags.

Only non-merge, non-bulk commits touching between 2 and ``cochange_max_files`` non-ignored files count. Files are
keyed by rename lineage, so a renamed file keeps its history; each lineage is reported under its latest path.

    n(A)     = eligible commits touching A
    n(A,B)   = eligible commits touching both
    jaccard  = n(A,B) / (n(A) + n(B) - n(A,B))
    conf_ab  = n(A,B) / n(A)
    score    = jaccard * (1.0 if cross_dir else 0.4) * (0.3 if structural_link else 1.0) * (0.5 + 0.5 * recency)
               * (0.3 if either file is deleted) * (0.5 if neither file is source code)
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from itertools import combinations

import networkx as nx

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store

from .structure import file_of


def top_dir(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else ""


def lca_depth(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
        if x != y:
            break
        n += 1
    return n


def structural_graph(store: Store) -> nx.Graph:
    g = nx.Graph()
    for r in store.q("SELECT kind, src, dst FROM edges WHERE kind IN ('IMPORTS', 'CALLS')"):
        if r["kind"] == "IMPORTS":
            a, b = r["src"], r["dst"]
        else:
            a, b = file_of(r["src"], store.repo_id), file_of(r["dst"], store.repo_id)
        if a != b:
            g.add_edge(a, b)
    return g


def compute_cochange(store: Store, settings: Settings) -> dict:
    rows = store.q(
        """SELECT c.hash, c.seq, c.ts, fc.lineage, fc.path FROM commits c JOIN file_changes fc ON fc.hash = c.hash
           WHERE c.is_merge = 0 AND c.is_bulk = 0 AND fc.ignored = 0 AND c.on_main = 1 ORDER BY c.seq""")
    # Main line only: PR refs and stale branches can carry unrelated or pre-restructure history, which both inflates
    # pair counts and (since `seq` interleaves refs) would label a lineage with a path that is dead on HEAD.
    per_commit: dict[str, set[str]] = defaultdict(set)
    commit_ts: dict[str, int] = {}
    latest_path: dict[str, str] = {}
    for r in rows:
        per_commit[r["hash"]].add(r["lineage"])
        commit_ts[r["hash"]] = r["ts"]
        latest_path[r["lineage"]] = r["path"]

    n: Counter[str] = Counter()
    pair: Counter[tuple[str, str]] = Counter()
    shared: dict[tuple[str, str], list[str]] = defaultdict(list)
    last_ts: dict[tuple[str, str], int] = {}
    eligible = 0
    for h, lins in per_commit.items():
        if not (2 <= len(lins) <= settings.cochange_max_files):
            continue
        eligible += 1
        for lin in lins:
            n[lin] += 1
        for a, b in combinations(sorted(lins), 2):
            pair[(a, b)] += 1
            shared[(a, b)].append(h)
            last_ts[(a, b)] = max(last_ts.get((a, b), 0), commit_ts[h])

    g = structural_graph(store)
    files = {r["path"]: (bool(r["alive"]), r["lang"]) for r in store.q("SELECT path, alive, lang FROM files")}
    near: dict[str, dict[str, int]] = {}
    repo_latest = store.latest_ts() or 1
    out = []
    for (la, lb), cnt in pair.items():
        if cnt < settings.cochange_min_count:
            continue
        jac = cnt / (n[la] + n[lb] - cnt)
        if jac < settings.cochange_min_jaccard:
            continue
        pa, pb = latest_path[la], latest_path[lb]
        if pa == pb:  # two lineages ending at one path (deleted, then re-created there): not a pair
            continue
        a, b = (pa, pb) if pa < pb else (pb, pa)
        na, nb = (n[la], n[lb]) if pa < pb else (n[lb], n[la])
        cross = top_dir(a) != top_dir(b)
        if a not in near:
            near[a] = nx.single_source_shortest_path_length(g, a, cutoff=2) if a in g else {}
        link = b in near[a]
        age_days = max(0, repo_latest - last_ts[(la, lb)]) / 86400
        recency = math.exp(-age_days / 365)
        score = jac * (1.0 if cross else 0.4) * (0.3 if link else 1.0) * (0.5 + 0.5 * recency)
        # Coupling in today's code matters most: pairs involving a since-deleted file, or with no source code on
        # either side (docs/config lockstep), are kept but ranked lower.
        alive_a, lang_a = files.get(a, (True, None))
        alive_b, lang_b = files.get(b, (True, None))
        score *= (1.0 if alive_a and alive_b else 0.3) * (1.0 if (lang_a or lang_b) else 0.5)
        commits = sorted(shared[(la, lb)], key=lambda h: commit_ts[h], reverse=True)[:20]
        out.append((a, b, cnt, na, nb, jac, cnt / na, cnt / nb, int(cross), lca_depth(a, b), int(link),
                    last_ts[(la, lb)], round(score, 6), json.dumps(commits)))
    # Several lineages can share a current path, so one path pair can appear more than once: keep the strongest.
    best: dict[tuple[str, str], tuple] = {}
    for row in out:
        k = (row[0], row[1])
        if k not in best or row[2] > best[k][2]:
            best[k] = row
    out = list(best.values())
    with store.tx() as conn:
        conn.execute("DELETE FROM cochange")
        conn.executemany("INSERT INTO cochange VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return {"eligible_commits": eligible, "pairs": len(out),
            "cross_dir_unlinked": sum(1 for r in out if r[8] and not r[10])}
