"""The `safety` ingest step: run every detector over HEAD + history and (re)write the safety tables."""
from __future__ import annotations

import json
import time

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store

from ..ast.parse_cache import ParseCache
from ..git.walker import default_branch
from ..ignore import IgnoreRules
from . import api, callgraph, concurrency, contracts, lineage, risk, rules, testmap
from .contracts import CONSUMER, EXTERNAL_ENV, PRODUCER
from .rules import Finding
from .source import is_test_path, sources_at

TAGS_FOR_API = 6


def _contract_findings(by_key: dict) -> list[Finding]:
    out = []
    for (kind, key), roles in by_key.items():
        prods, cons = roles[PRODUCER], roles[CONSUMER]
        cons_live = [c for c in cons if not is_test_path(c.path)]
        if cons_live and not prods:
            if kind == "env" and (key in EXTERNAL_ENV or key.startswith(("GITHUB_", "RUNNER_", "PYTEST_"))):
                continue
            if kind in ("route",):  # a client calling a route this repo does not serve may target another service
                sev = "info"
            else:
                sev = "info" if kind == "env" else "warn"
            c = cons_live[0]
            out.append(Finding("contract", f"contract.orphan_consumer.{kind}", sev, c.path, c.line,
                               f"{kind} `{key}` is read/used but nothing in the repository defines it "
                               f"({len(cons_live)} use{'s' if len(cons_live) > 1 else ''})", c.symbol,
                               {"kind": kind, "key": key, "consumers": sorted({x.path for x in cons_live})[:10]}))
        if prods and not cons and kind in ("event", "registry", "flag"):
            p = prods[0]
            out.append(Finding("contract", f"contract.orphan_producer.{kind}", "info", p.path, p.line,
                               f"{kind} `{key}` is emitted/registered but nothing listens for it", p.symbol,
                               {"kind": kind, "key": key}))
    return out


def _tags(repo: pygit2.Repository, limit: int) -> list[tuple[str, str]]:
    out = []
    for ref in repo.references:
        if not ref.startswith("refs/tags/"):
            continue
        try:
            c = repo.references[ref].peel(pygit2.Commit)
        except Exception:
            continue
        out.append((c.commit_time, ref[len("refs/tags/"):], str(c.id)))
    out.sort(reverse=True)
    return [(name, sha) for _, name, sha in out[:limit]]


def run_safety(repo: pygit2.Repository, store: Store, cache: ParseCache, ignore: IgnoreRules, settings: Settings,
               workers: int | None = None, log=print) -> dict:
    t = {}
    t0 = time.perf_counter()
    rid = store.repo_id
    _, head = default_branch(repo)
    sources = sources_at(repo, head, cache, ignore, workers)
    t["load"] = time.perf_counter() - t0

    # 4. typed calls + overrides first: tests, risk and the gate all read CALLS
    t1 = time.perf_counter()
    cg: dict = {}
    if getattr(settings, "safety_typed_calls", True):
        try:
            cg = callgraph.typed_calls(store, sources, settings.data_dir / "tmp",
                                       budget_s=getattr(settings, "safety_typed_calls_budget_s", 180.0))
        except Exception as e:  # never fail an ingest over the optional resolver
            cg = {"error": str(e)[:200]}
    ov = callgraph.overrides(store)
    with store.tx() as conn:
        conn.execute("DELETE FROM edges WHERE kind='OVERRIDES'")
        conn.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?)", ov)
    t["callgraph"] = time.perf_counter() - t1

    # 3. contracts
    t1 = time.perf_counter()
    all_contracts = [c for src in sources.values() for c in contracts.extract(src, rid)]
    by_key = contracts.match(all_contracts)
    t["contracts"] = time.perf_counter() - t1

    # 7/8/9/12/14 line + structural rules
    t1 = time.perf_counter()
    findings: list[Finding] = []
    for src in sources.values():
        findings += rules.scan(src, rid)
        findings += concurrency.scan(src, rid)
    findings += _contract_findings(by_key)
    t["rules"] = time.perf_counter() - t1

    # 2. tests
    t1 = time.perf_counter()
    tmap = testmap.build(store, sources, rid)
    t["testmap"] = time.perf_counter() - t1

    # 5. API surface at HEAD and recent tags, deprecations
    t1 = time.perf_counter()
    api_rows = []
    for ref, sha, srcs in [("HEAD", head, sources)] + [
            (name, sha, None) for name, sha in _tags(repo, getattr(settings, "safety_api_tags", TAGS_FOR_API))]:
        if srcs is None:
            try:
                srcs = sources_at(repo, sha, cache, ignore, workers)
            except Exception:
                continue
        for src in srcs.values():
            pub = api.public_symbols(src)
            if not pub:
                continue
            dep = api.deprecations(src)
            for qual, rec in pub.items():
                api_rows.append((ref, f"sym:{rid}:{src.path}:{qual}:{rec.kind}", src.path, qual, rec.kind,
                                 rec.signature[:400], dep.get(qual)))
            for name, hit in dep.items():  # module __getattr__ deprecations of names that are not symbols
                if name not in pub and "." not in name and ref == "HEAD":
                    api_rows.append((ref, f"attr:{rid}:{src.path}:{name}", src.path, name, "attribute", "", hit))
    t["api"] = time.perf_counter() - t1

    # 6. fix lineage
    t1 = time.perf_counter()
    fixes = lineage.build(repo, store, cache, getattr(settings, "safety_max_fix_commits", 4000))
    t["lineage"] = time.perf_counter() - t1

    with store.tx() as conn:
        for table in ("findings", "contracts", "test_map", "api_surface", "fix_events", "risk"):
            conn.execute(f"DELETE FROM {table}" + (" WHERE source != 'coverage'" if table == "test_map" else ""))
        conn.executemany("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?)",
                         [(f.id, f.category, f.rule, f.severity, f.path, f.line, f.symbol, f.message,
                           json.dumps(f.detail)) for f in findings])
        conn.executemany("INSERT OR IGNORE INTO contracts VALUES (?,?,?,?,?,?)",
                         [(c.kind, c.key, c.role, c.path, c.line, c.symbol) for c in all_contracts])
        conn.executemany("INSERT OR REPLACE INTO test_map VALUES (?,?,?,?)", tmap)
        conn.executemany("INSERT OR REPLACE INTO api_surface VALUES (?,?,?,?,?,?,?)", api_rows)
        conn.executemany("INSERT OR REPLACE INTO fix_events VALUES (?,?,?,?,?,?,?,?)", fixes)

    # 13. risk last: it reads everything above
    t1 = time.perf_counter()
    rrows = risk.build(store, sources)
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO risk VALUES (?,?,?,?)", rrows)
    t["risk"] = time.perf_counter() - t1

    sev: dict[str, int] = {}
    cat: dict[str, int] = {}
    for f in findings:
        sev[f.severity] = sev.get(f.severity, 0) + 1
        cat[f.category] = cat.get(f.category, 0) + 1
    return {"files": len(sources), "findings": len(findings), "by_severity": sev, "by_category": cat,
            "contracts": len(all_contracts), "contract_keys": len(by_key), "test_links": len(tmap),
            "api_symbols_head": sum(1 for r in api_rows if r[0] == "HEAD"), "api_refs": len({r[0] for r in api_rows}),
            "fix_events": len(fixes), "fix_commits": len({r[0] for r in fixes}), "overrides": len(ov),
            "typed_calls": cg, "timings_s": {k: round(v, 2) for k, v in t.items()}}
