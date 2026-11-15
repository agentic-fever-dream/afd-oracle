"""Catalogue family: performance and capacity (tunable knobs, N+1 fixes, hot-path blocking calls, unbounded growth)."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .base import (DAY, K8S_RX, Ctx, Hit, candidate, is_docs, is_fix_message, is_generated, is_test, is_vendored,
                   subject_of, MIGRATION_RX)

COMMENT_PFX = ("#", "//", "*", "/*", ";", "--", "<!--")
LOCK_RX = re.compile(r"(?i)(^|/)(package-lock\.json|yarn\.lock|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum|Pipfile\.lock)$")
SCRIPT_RX = re.compile(r"(?i)(^|/)(scripts?|bin|tools|admin|manage\.py|management/commands|migrations?|fixtures?|"
                       r"examples?|benchmarks?|docs?)(/|$)")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip())


def _skip_path(path: str) -> bool:
    return is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path) or bool(LOCK_RX.search(path))


def _msg(ctx: Ctx, sha: str) -> str:
    c = ctx.commit(sha)
    return (c["message"] or "") if c else ""


# ---------------------------------------------------------------------------------------------------------------
_KNOB = (r"timeout|retries|retry|backoff|pool_?size|max_?connections|batch_?size|ttl|rate_?limit|concurrency|replicas|"
         r"memory|workers|max_?overflow|queue_?size|max_?workers|keepalive|chunk_?size|page_?size")
_KNOB_LINE = re.compile(rf"(?i)([\w.\-]*(?:{_KNOB})[\w.\-]*)[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)\s*(ms|s|m|h|Mi|Gi|k|M|G)?\b")
_KNOB_EXCL = re.compile(r"(?i)version|port|(^|[._\-])id$|_id\b|pid|uid|gid|year|seed|index|line|count_of|http_version")
_REASON = re.compile(r"(?i)\bbecause\b|\bdue to\b|to avoid|prevent|timeout|slow|oom|outage|incident|latency|too (many|few|"
                     r"high|low)|exhaust|starv|throttl|overload|flak|rate.?limit|capacity|tune|tuning|bump|increase|"
                     r"decrease|reduce|raise|lower")
_OUTAGE = re.compile(r"(?i)outage|incident|oom|crash|hotfix|rollback|exhaust|prod(uction)? (down|issue)|sev[ -]?\d|p0")
_UNIT_S = {None: 1.0, "": 1.0, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0, "k": 1e3, "M": 1e6, "G": 1e9,
           "Mi": 1048576.0, "Gi": 1073741824.0}


@candidate("tunable_knob_churn")
def tunable_knob_churn(ctx: Ctx):
    """(file, key) series of numeric values for timeout/retry/backoff/pool/batch/ttl/rate-limit/concurrency/replica/
    memory knobs, from added lines of code and config. Hit when >=3 distinct values, an A->B->A flip, or a >=2x step,
    and at least one later change has a fix-lineage or reason in its commit message. Proxy: line-level key=value
    regex (no scope resolution), bot commits, tests, docs and version/port/id keys dropped."""
    rows = ctx.grep(rf"(?i)(?:{_KNOB})[\"']?\s*[:=]\s*[\"']?\d", "+", limit=80000)
    series: dict[tuple[str, str], list[tuple[int, str, float, str]]] = defaultdict(list)
    for r in rows:
        path = r["path"]
        if _skip_path(path):
            continue
        c = ctx.commit(r["hash"])
        if c is None or ctx.is_bot(c):
            continue
        if r["text"].strip().startswith(COMMENT_PFX):
            continue
        for m in _KNOB_LINE.finditer(r["text"]):
            key = m.group(1).lower()
            if _KNOB_EXCL.search(key):
                ctx.drop("version_port_id_key")
                continue
            val = float(m.group(2)) * _UNIT_S.get(m.group(3), 1.0)
            ser = series[(path, key)]
            if ser and ser[-1][1] == r["hash"]:
                ser[-1] = (r["seq"], r["hash"], val, m.group(2) + (m.group(3) or ""))
            else:
                ser.append((r["seq"], r["hash"], val, m.group(2) + (m.group(3) or "")))
    for (path, key), ser in series.items():
        vals = [s[2] for s in ser]
        dedup = [s for i, s in enumerate(ser) if i == 0 or s[2] != ser[i - 1][2]]
        if len(dedup) < 2:
            ctx.drop("single_introduction")
            continue
        dv = [s[2] for s in dedup]
        distinct = len(set(dv))
        aba = any(dv[i] == dv[i + 2] and dv[i] != dv[i + 1] for i in range(len(dv) - 2))
        step2x = any(a and b and max(a, b) / min(a, b) >= 2 for a, b in zip(dv, dv[1:]))
        if not (distinct >= 3 or aba or step2x):
            ctx.drop("small_steady_change")
            continue
        later = dedup[1:]
        reasoned = [s for s in later if is_fix_message(_msg(ctx, s[1])) or _REASON.search(_msg(ctx, s[1]))]
        if not reasoned:
            ctx.drop("no_message_reason")
            continue
        outage = [s for s in dedup if _OUTAGE.search(_msg(ctx, s[1]))]
        yield Hit(subject=f"{path}|{key}", score=distinct + 2 * aba + 1.5 * step2x + 2 * bool(outage) + 0.5 * len(reasoned),
                  title=f"'{key}' in {path} changed {len(dedup) - 1} times: {' -> '.join(s[3] for s in dedup[-6:])}",
                  evidence={"values": [s[3] for s in dedup[-8:]], "flip_back": aba, "two_x_step": step2x,
                            "reasons": [subject_of(_msg(ctx, s[1])) for s in reasoned[-4:]]},
                  commits=[s[1] for s in dedup[-5:]], diffs=[(s[1], path) for s in dedup[-2:]], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_EAGER = (r"select_related|prefetch_related|joinedload|selectinload|subqueryload|\.includes\(|DataLoader|eager_load|"
          r"\.preload\(|with_related|contains_eager")


@candidate("n_plus_one_fix_regression")
def n_plus_one_fix_regression(ctx: Ctx):
    """Commit adds an eager-load token (select_related, prefetch_related, joinedload, includes, DataLoader ...) not
    present in that file before; a later commit on the same file removes the token (regression) or a later fix on the
    file mentions n+1/query/slow (recurrence). 'Same function' uses diff_lines.symbol: the symbols holding the added
    eager-load lines are the scope, a later removal must be a '-' line inside one of them, and a later query-count fix
    must have changed lines inside one of them. When the added lines have no enclosing symbol (module level) the scope
    falls back to the whole file. Migrations, admin/scripts and tests are dropped."""
    adds = ctx.grep(_EAGER, "+", limit=30000)
    rems = ctx.grep(_EAGER, "-", limit=30000)
    rem_by_path: dict[str, list] = defaultdict(list)
    for r in rems:
        rem_by_path[r["path"]].append(r)
    seen: set[tuple[str, str]] = set()
    rx = re.compile(_EAGER)
    for r in adds:
        sha, path = r["hash"], r["path"]
        if (sha, path) in seen:
            continue
        if _skip_path(path) or MIGRATION_RX.search(path) or SCRIPT_RX.search(path) or not path.endswith(
                (".py", ".rb", ".js", ".ts", ".java", ".kt", ".go", ".php", ".cs")):
            ctx.drop("migrations_scripts_tests")
            continue
        seen.add((sha, path))
        tokens = {m.group(0) for ln in ctx.added_removed(sha, path)[0] for m in rx.finditer(ln)}
        removed_here = {m.group(0) for ln in ctx.added_removed(sha, path)[1] for m in rx.finditer(ln)}
        if not (tokens - removed_here):
            ctx.drop("token_already_present")
            continue
        c = ctx.commit(sha)
        scope = {x["symbol"] for x in ctx.lines_of(sha, path)
                 if x["sign"] == "+" and x["symbol"] and rx.search(x["text"])}
        later_rm = [x for x in rem_by_path.get(path, []) if x["seq"] > r["seq"] and x["hash"] != sha
                    and not (x["text"].strip().startswith(COMMENT_PFX)) and (not scope or x["symbol"] in scope)]
        scope_hashes = {x["hash"] for u in scope for x in ctx.lines_in_symbol(u)} if scope else None
        later_fix = [x for x in ctx.path_commits(path) if x["seq"] > c["seq"] and re.search(
            r"(?i)n\+1|n \+ 1|queries|query count|slow quer|too many quer|prefetch|select_related|eager|lazy.?load",
            x["message"] or "") and (scope_hashes is None or x["hash"] in scope_hashes)]
        if not later_rm and not later_fix:
            ctx.drop("never_regressed")
            continue
        small_loop = len(ctx.added_removed(sha, path)[0]) <= 1 and not is_fix_message(c["message"])
        yield Hit(subject=f"{sha}|{path}", score=2 + 3 * bool(later_rm) + 1.5 * len(later_fix) + 2 * is_fix_message(c["message"]) - 0.5 * small_loop,
                  title=f"Eager load ({', '.join(sorted(tokens)[:2])}) added in {path}, "
                        f"{'later removed' if later_rm else 'later query-count fix'}",
                  evidence={"tokens": sorted(tokens), "fix_message": subject_of(c["message"]),
                            "removed_later_in": list(dict.fromkeys(x["hash"][:10] for x in later_rm))[:3],
                            "later_query_fixes": [subject_of(x["message"]) for x in later_fix[:3]],
                            "scope": "symbol" if scope else "file"},
                  commits=[sha] + list(dict.fromkeys(x["hash"] for x in later_rm))[:2] + [x["hash"] for x in later_fix[:2]],
                  diffs=[(sha, path)] + ([(later_rm[0]["hash"], path)] if later_rm else []), paths=[path],
                  symbols=sorted(scope))


# ---------------------------------------------------------------------------------------------------------------
_BLOCKING = re.compile(r"\b(requests\.(get|post|put|patch|delete|request|head)|urllib\.request|urlopen|httpx\.(get|post|put|"
                       r"request|Client)|aiohttp|subprocess\.(run|call|check_output|check_call|Popen)|os\.system|"
                       r"\.execute\(|\.executemany\(|cursor\(|session\.(query|execute)|\.objects\.(get|filter|all)|"
                       r"time\.sleep|socket\.|http\.(Get|Post)|exec\.Command|\.fetch\w*\(|\.query\()")
_PERF_MSG = re.compile(r"(?i)perf|latenc|slow|speed|cache|optimi[sz]|timeout|block|faster|hot path|n\+1|throughput|"
                       r"regress|remove .*(call|request)|avoid .*(call|request|lookup)")


@candidate("hot_path_blocking_call_added")
def hot_path_blocking_call_added(ctx: Ctx):
    """A diff adds a network/subprocess/DB/sleep call inside a top-decile in-degree symbol (>=3 callers),
    and a follow-up within 60 days on that symbol carries a perf/latency/cache message (mandatory).
    Each added call line is attributed to its exact enclosing symbol (diff_lines.symbol); only calls inside a hot symbol
    count. Fallback, only for a commit+file whose added call lines have no enclosing symbol (module level / unmapped):
    the old attribution to hot symbols that changed in that commit+file (at most 3)."""
    n_callers = {s["uid"]: len(ctx.callers(s["uid"])) for s in ctx.alive_symbols if s["kind"] in ("function", "method")}
    if not n_callers:
        return
    ordered = sorted(n_callers.values())
    thr = max(3, ordered[int(0.9 * (len(ordered) - 1))])
    hot = {u for u, n in n_callers.items() if n >= thr}
    if not hot:
        return
    ev_by_uid: dict[str, list] = defaultdict(list)
    for r in ctx.store.q("SELECT uid, hash, seq, ts, change FROM symbol_events ORDER BY seq"):
        if r["uid"] in hot:
            ev_by_uid[r["uid"]].append(r)
    rows = ctx.grep(_BLOCKING.pattern, "+", limit=40000)
    seen: set[tuple[str, str]] = set()
    by_sym: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r["text"].strip().startswith(COMMENT_PFX):
            by_sym[(r["hash"], r["path"])][r["symbol"] or ""].append(r["text"])
    for r in rows:
        sha, path = r["hash"], r["path"]
        if (sha, path) in seen or r["text"].strip().startswith(COMMENT_PFX):
            continue
        seen.add((sha, path))
        if _skip_path(path):
            ctx.drop("test_or_helper")
            continue
        per = by_sym[(sha, path)]
        exact = {u: ls for u, ls in per.items() if u and u in hot}
        if exact:
            syms = list(exact)
            call_by_sym = exact
        elif any(u for u in per):  # every call sits in a known, non-hot symbol
            if "" not in per:
                ctx.drop("call_not_in_hot_symbol")
                continue
            syms, call_by_sym = [], {}
        else:
            syms, call_by_sym = [], {}
        if not syms:  # fallback: unmapped lines, attribute to hot symbols changed in that commit+file
            syms = [e["uid"] for e in ctx.events_by_commit.get(sha, []) if e["path"] == path and e["uid"] in hot]
            if not syms or len(set(syms)) > 3:
                continue
            call_by_sym = {}
        added, _ = ctx.added_removed(sha, path)
        call_lines = [ln for ln in added if _BLOCKING.search(ln) and not ln.strip().startswith(COMMENT_PFX)]
        if not call_lines:
            continue
        if any(re.search(r"(?i)\b(log|logger|logging|print|debug)\b|cache|memoi[sz]e|lru_cache|feature|flag", ln)
               for ln in added if ln in call_lines) or all(
                re.search(r"(?i)logg(er|ing)\.|\bprint\(", ln) for ln in call_lines):
            ctx.drop("cached_flagged_or_logging")
            continue
        for uid in dict.fromkeys(syms):
            fol = [e for e in ev_by_uid[uid] if e["seq"] > r["seq"] and e["ts"] - r["ts"] <= 60 * DAY
                   and _PERF_MSG.search(_msg(ctx, e["hash"]))]
            if not fol:
                ctx.drop("no_perf_followup")
                continue
            s = ctx.symbols[uid]
            own = call_by_sym.get(uid)
            yield Hit(subject=f"{sha}|{uid}", score=2 + n_callers[uid] * 0.3 + 2 * len(fol),
                      title=f"Blocking call added to hot symbol {s['qualname']} ({n_callers[uid]} callers), perf follow-up",
                      evidence={"added_calls": [_norm(x) for x in (own or call_lines)[:3]], "callers": n_callers[uid],
                                "attribution": "symbol" if own else "commit_file_fallback",
                                "followups": [subject_of(_msg(ctx, e["hash"])) for e in fol[:3]]},
                      commits=[sha] + [e["hash"] for e in fol[:2]], diffs=[(sha, path)] + [(fol[0]["hash"], path)],
                      paths=[path], symbols=[uid])


# ---------------------------------------------------------------------------------------------------------------
_LEAK_MSG = re.compile(r"(?i)leak|\boom\b|out of memory|unbounded|evict|memory (growth|usage|bloat|blow)|grows? (forever|"
                       r"without|indefinitely)|never (freed|cleared|evicted)|bounded|bound the")
_BOUND = (r"maxsize\s*=\s*\d|lru_cache\(\s*(maxsize\s*=\s*)?\d|\bttl\s*=|\bttl_seconds|WeakValueDictionary|WeakKeyDictionary|"
          r"TTLCache|LRUCache|maxlen\s*=|max_size|\bmax_entries|\.popitem\(|cachetools")


@candidate("cache_unbounded_growth_fix")
def cache_unbounded_growth_fix(ctx: Ctx):
    """Fix commit whose message says leak/OOM/unbounded/evict adds a bound token (maxsize, ttl, WeakValueDictionary,
    TTLCache, maxlen ...). Attaches container memory-limit diffs within +/-30 days; ``lru_cache(maxsize=None)`` /
    ``@cache`` still at HEAD in the file boosts. Proxy: arg-less cached functions are detected from the file text at
    the commit; small-enum key spaces are not detectable and are left to the analyst. The symbols attached are those holding the
    added bound lines (diff_lines.symbol; fallback: symbol events of that commit+file). CLIs/tests dropped."""
    rows = ctx.grep(_BOUND, "+", limit=30000)
    mem_rows = ctx.grep(r"(?i)\bmemory\b", None, path_regex=K8S_RX.pattern)
    mem_commits = sorted({(r["ts"], r["hash"], r["path"]) for r in mem_rows})
    seen: set[tuple[str, str]] = set()
    for r in rows:
        sha, path = r["hash"], r["path"]
        if (sha, path) in seen:
            continue
        msg = _msg(ctx, sha)
        if not _LEAK_MSG.search(msg) or not (is_fix_message(msg) or re.search(r"(?i)leak|oom|unbounded", msg)):
            continue
        seen.add((sha, path))
        if _skip_path(path) or re.search(r"(?i)(^|/)(cli|__main__|scripts?|bin)\b", path) or not path.endswith(
                (".py", ".js", ".ts", ".go", ".java", ".kt", ".rb", ".cs", ".rs")):
            ctx.drop("tests_cli_or_non_code")
            continue
        text = ctx.text_at(sha, path) or ""
        lines = text.splitlines()
        window = lines[max(0, r["line"] - 1):r["line"] + 3]
        dm = next((re.match(r"\s*(?:async\s+)?def\s+\w+\(\s*\)", ln) for ln in window if re.match(r"\s*(?:async\s+)?def\s+\w+\(", ln)), None)
        if dm and "lru_cache" in r["text"]:
            ctx.drop("argless_function")
            continue
        c = ctx.commit(sha)
        mem = [x for x in mem_commits if abs(x[0] - c["ts"]) <= 30 * DAY and x[1] != sha][:3]
        head = ctx.text_at_head(path) or ""
        unbounded_head = bool(re.search(r"lru_cache\(\s*maxsize\s*=\s*None\s*\)|@(functools\.)?cache\b", head))
        uid_syms = list(dict.fromkeys(x["symbol"] for x in ctx.lines_of(sha, path)
                                      if x["sign"] == "+" and x["symbol"] and re.search(_BOUND, x["text"])))[:3] or [
            e["uid"] for e in ctx.events_by_commit.get(sha, []) if e["path"] == path][:3]
        yield Hit(subject=f"{sha}|{path}", score=3 + 2 * bool(mem) + 1.5 * unbounded_head + (1 if re.search(r"(?i)leak|oom", msg) else 0),
                  title=f"Cache bounded in {path}: {subject_of(msg)[:80]}",
                  evidence={"bound_line": _norm(r["text"]), "message": subject_of(msg),
                            "memory_limit_diffs": [{"commit": h[:10], "path": p} for _, h, p in mem],
                            "unbounded_cache_still_at_head": unbounded_head},
                  commits=[sha] + [h for _, h, _ in mem], diffs=[(sha, path)] + [(h, p) for _, h, p in mem[:2]],
                  paths=[path], symbols=uid_syms)


# ---------------------------------------------------------------------------------------------------------------
_PAG_MSG = re.compile(r"(?i)paginat|perf|slow|timeout|time.?out|\boom\b|out of memory|large|huge|too many|memory|"
                      r"unbounded|batch|stream|chunk|limit")
_PAG_TOKEN = (r"paginat|page_size|per_page|yield_per|\.limit\(|\bLIMIT\s+\d|\bcursor\b|chunk(ed|_size)|\[:\d+\]|"
              r"\.iterator\(|\bbatch_size|\.take\(|\.paginate\(|Pageable|LIMIT\s*%s|:limit")
_UNBOUNDED_Q = re.compile(r"(?i)\.all\(\)|fetchall\(\)|\.objects\.(all|filter)\(|\bSELECT\b[^;]*\bFROM\b|\.find\(\s*\)|"
                          r"\.find\(\{|\.select\(|session\.query\(|\.scalars\(|findAll\(")
_ENUMISH = re.compile(r"(?i)\b(Country|Countries|Currency|Role|Roles|Status|Statuses|Type|Types|Category|Categories|Enum|"
                      r"Choice|Choices|Permission|Language|Timezone)\b")


@candidate("unbounded_query_pagination_fix")
def unbounded_query_pagination_fix(ctx: Ctx):
    """Fix commit (perf / oom / timeout / slow / large wording) adds a bound token (paginate, page_size, yield_per,
    cursor, LIMIT, chunking) while removing an unbounded query line (.all(), fetchall, SELECT ... FROM, objects.filter)
    in the same file. Siblings (other unbounded query lines at HEAD in that file) are listed briefly. The function
    holding the unbounded query is the exact enclosing symbol of the removed query lines (diff_lines.symbol, old file
    version); a bound token added inside one of those symbols adds a small boost (``bound_in_query_symbol``). Fallback
    when removed lines have no enclosing symbol: symbol events of that commit+file."""
    rows = ctx.grep(_PAG_TOKEN, "+", limit=30000)
    seen: set[tuple[str, str]] = set()
    for r in rows:
        sha, path = r["hash"], r["path"]
        if (sha, path) in seen or r["text"].strip().startswith(COMMENT_PFX):
            continue
        msg = _msg(ctx, sha)
        if not (is_fix_message(msg) or re.search(r"(?i)paginat|\boom\b|out of memory|timeout|slow", msg)) or not _PAG_MSG.search(msg):
            continue
        seen.add((sha, path))
        if _skip_path(path) or MIGRATION_RX.search(path) or re.search(r"(?i)fixtures?", path) or not path.endswith(
                (".py", ".js", ".ts", ".go", ".java", ".kt", ".rb", ".php", ".cs", ".sql")):
            ctx.drop("tests_fixtures_migrations")
            continue
        added, removed = ctx.added_removed(sha, path)
        unb = [ln for ln in removed if _UNBOUNDED_Q.search(ln) and not ln.strip().startswith(COMMENT_PFX)
               and not re.search(_PAG_TOKEN, ln)]
        if not unb:
            ctx.drop("no_unbounded_query_before")
            continue
        if all(_ENUMISH.search(ln) for ln in unb):
            ctx.drop("enum_sized_table")
            continue
        head = (ctx.text_at_head(path) or "").splitlines()
        sib = [_norm(ln)[:90] for ln in head if _UNBOUNDED_Q.search(ln) and not re.search(_PAG_TOKEN, ln)
               and not ln.strip().startswith(COMMENT_PFX)][:4]
        rows_c = ctx.lines_of(sha, path)
        syms = list(dict.fromkeys(x["symbol"] for x in rows_c if x["sign"] == "-" and x["symbol"]
                                  and _UNBOUNDED_Q.search(x["text"]) and not re.search(_PAG_TOKEN, x["text"])))[:3]
        bound_in = bool(syms) and any(x["sign"] == "+" and x["symbol"] in syms and re.search(_PAG_TOKEN, x["text"])
                                      for x in rows_c)
        if not syms:
            syms = [e["uid"] for e in ctx.events_by_commit.get(sha, []) if e["path"] == path][:3]
        yield Hit(subject=f"{sha}|{path}", score=3 + (1.5 if re.search(r"(?i)\boom\b|out of memory", msg) else 0)
                  + (1 if re.search(r"(?i)timeout|slow", msg) else 0) + 0.3 * len(sib) + (0.5 if bound_in else 0),
                  title=f"Query bounded in {path}: {subject_of(msg)[:80]}",
                  evidence={"removed_query": [_norm(x) for x in unb[:3]], "bound_added": _norm(r["text"]),
                            "message": subject_of(msg), "siblings_still_unbounded": sib,
                            "bound_in_query_symbol": bound_in},
                  commits=[sha], diffs=[(sha, path)], paths=[path], symbols=syms)
