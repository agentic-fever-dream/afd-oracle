"""Catalogue family: concurrency and process model (locks, schedulers, worker counts, import-time state, forking)."""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .base import (LOCKFILE_RX, Ctx, Hit, candidate, is_code, is_docs, is_fix_message, is_generated, is_test,
                   is_vendored, subject_of)

_WS = re.compile(r"\s+")
_SCRIPT_PATH = re.compile(r"(?i)(^|/)(scripts?|bin|tools?|examples?|samples?|docs?|benchmarks?|contrib)/|"
                          r"(^|/)(manage|setup|conftest|__main__|noxfile|fabfile|tasks)\.py$")
_RACE_MSG = re.compile(r"(?i)\b(race|deadlock|thread[- ]?safe\w*|concurren\w*|atomic\w*|lock(ing|s|ed)?|"
                       r"synchroni[sz]\w*|reentran\w*|mutex)\b")


def _norm(t: str) -> str:
    return _WS.sub(" ", t.strip())


def _cache(ctx: Ctx, key: str, fn):
    d = ctx.__dict__.setdefault("_cc_cache", {})
    if key not in d:
        d[key] = fn()
    return d[key]


def _prod(path: str) -> bool:
    return is_code(path) and not is_test(path) and not LOCKFILE_RX.search(path)


def _head_py(ctx: Ctx, limit: int = 3000) -> list[tuple[str, str]]:
    def build():
        out = []
        for p in sorted(ctx.head_files):
            if p.endswith(".py") and _prod(p) and not _SCRIPT_PATH.search(p):
                t = ctx.text_at_head(p)
                if t:
                    out.append((p, t))
                if len(out) >= limit:
                    break
        return out
    return _cache(ctx, "headpy", build)


def _col0_before_main(text: str):
    """(lineno, line) of column-0 lines that precede any ``if __name__`` guard."""
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.startswith("if __name__"):
            break
        yield i, ln


# ---------------------------------------------------------------------------- lock churn
LOCK_RX = (r"\b(threading\.)?(R?Lock|Semaphore|BoundedSemaphore|Condition)\(|\.acquire\(|\bsynchronized\b|\bMutex\b|"
           r"sync\.(RW)?Mutex|\.lock\(\)|\bwith\s+[\w.]*lock\b|asyncio\.Lock|select_for_update|\bflock\b")
_FILELOCK = re.compile(r"(?i)filelock|file_?lock|lockfile|fcntl|portalocker|\.lock[\"']|flock")


@candidate("lock_churn_concurrency_scars")
def lock_churn_concurrency_scars(ctx: Ctx):
    """Lock lines (Lock()/acquire/synchronized/Mutex/with ...lock/select_for_update) in production code, grouped per
    symbol: the innermost function/class of each changed line (``diff_lines.symbol``), with lock lines outside any
    symbol (module level, or a repo with no symbol data) grouped per file. A group qualifies when a lock line was added
    by a race-zone fix and a similar one removed later, or when >=3 distinct commits edit lock lines in that symbol.
    Other files whose lock lines changed in the same commits are listed as co-modified. The hit subject is the symbol
    uid (or the path for module-level groups)."""
    race_fix = {f["hash"] for f in ctx.fix_events if "race" in json.loads(f["kinds"] or "[]")}
    by_unit: dict[tuple[str, str], list] = defaultdict(list)
    for r in ctx.grep(LOCK_RX, None):
        if not _prod(r["path"]) or is_vendored(r["path"]) or is_generated(r["path"]):
            ctx.drop("tests_lockfiles_generated")
            continue
        if _FILELOCK.search(r["text"]) or _FILELOCK.search(r["path"]):
            ctx.drop("file_lock_helper")
            continue
        by_unit[(r["path"], r["symbol"] or "")].append(r)
    commit_paths: dict[str, set[str]] = defaultdict(set)
    for (p, _), rows in by_unit.items():
        for r in rows:
            commit_paths[r["hash"]].add(p)
    for (path, uid), rows in by_unit.items():
        hashes = sorted({r["hash"] for r in rows}, key=lambda h: ctx.by_hash[h]["seq"])
        added_in_race = [r for r in rows if r["sign"] == "+" and (
            r["hash"] in race_fix or (_RACE_MSG.search(ctx.by_hash[r["hash"]]["message"] or "") and ctx.is_fix(ctx.by_hash[r["hash"]])))]
        removed_after = None
        for a in added_in_race:
            removed_after = next((r for r in rows if r["sign"] == "-" and r["seq"] > a["seq"] and r["hash"] != a["hash"]), None)
            if removed_after:
                break
        if not removed_after and len(hashes) < 3:
            continue
        sym = ctx.symbols.get(uid) if uid else None
        if not uid:  # module-level: attach the HEAD enclosing symbol of a surviving lock line, as before
            head = ctx.text_at_head(path) or ""
            for i, ln in enumerate(head.splitlines(), 1):
                if re.search(LOCK_RX, ln):
                    sym = ctx.symbol_at_line(path, i)
                    if sym:
                        break
        co = sorted({p for h in hashes for p in commit_paths[h] if p != path})[:5]
        reason = "added_by_race_fix_then_removed" if removed_after else "repeated_lock_edits"
        where = sym["qualname"] if uid and sym else path
        yield Hit(
            subject=uid or path, score=len(hashes) * 1.5 + (6 if removed_after else 0) + len(co) * 0.5,
            title=f"{where}: lock lines edited in {len(hashes)} commits ({reason.replace('_', ' ')})",
            evidence={"reason": reason, "commits_touching_locks": len(hashes), "co_modified_lock_files": co,
                      "scope": "symbol" if uid else "file", "path": path,
                      "race_fix_added": added_in_race[0]["hash"] if added_in_race else None,
                      "lock_removed_in": removed_after["hash"] if removed_after else None,
                      "sample_lines": [_norm(r["text"])[:100] for r in rows[:5]]},
            commits=[*([added_in_race[0]["hash"]] if added_in_race else []),
                     *([removed_after["hash"]] if removed_after else []), *hashes[-4:]][:8],
            diffs=[(h, path) for h in hashes[:4]], paths=[path, *co[:3]],
            symbols=[sym["uid"]] if sym else [],
        )


# ---------------------------------------------------------------------------- in-process scheduler
SCHED_RX = (r"APScheduler|BackgroundScheduler|BlockingScheduler|AsyncIOScheduler|flask_apscheduler|schedule\.every|"
            r"node-cron|cron\.schedule|node-schedule|scheduleJob|@Scheduled|\.add_job\(")
GUARD_RX = (r"advisory_lock|WERKZEUG_RUN_MAIN|redlock|\bSET\b.*\bNX\b|nx\s*=\s*True|\bflock\b|fcntl\.lockf|is_leader|"
            r"leader_elect|scheduler_lock|skip_locked|CronJob")
MULTIWORKER_RX = r"(?i)\bworkers\b|WEB_CONCURRENCY|replicas|\binstances\b|--processes|\bprocesses\s*="
_SIDE_EFFECT = re.compile(r"(?i)send|email|mail|\bpost\(|requests\.|INSERT|UPDATE|publish|charge|notify|webhook|sms|"
                          r"\.delete\(|write")
_IDEMPOTENT = re.compile(r"(?i)cache|refresh|prune|expire|vacuum|clean-?up|reload|gc\b|metrics|heartbeat")


@candidate("inprocess_scheduler_multi_instance")
def inprocess_scheduler_multi_instance(ctx: Ctx):
    """An in-process scheduler is introduced (APScheduler/schedule/node-cron/@Scheduled...) and within 30 commits a
    guard appears (advisory lock, WERKZEUG_RUN_MAIN, redlock, SET NX, flock, is_leader, CronJob) or a Procfile
    clock/cron entry. Boosted when the lines around the intro have side effects. Dropped when the repo never
    configures >1 worker/instance, in tests, or when the job looks idempotent (cache/refresh/prune, no side effect)."""
    intros: dict[str, object] = {}
    for r in ctx.grep(SCHED_RX, "+"):
        if not is_code(r["path"]) or is_test(r["path"]):
            ctx.drop("tests_or_non_code")
            continue
        intros.setdefault(r["path"], r)
    if not intros:
        return
    guards = [r for r in ctx.grep(GUARD_RX, "+", limit=5000) if not is_docs(r["path"]) and not is_test(r["path"])]
    guards += [r for r in ctx.grep(r"(?i)(clock|scheduler|cron|beat)", "+", path_like="%Procfile%")]
    has_multiworker = bool(ctx.grep(MULTIWORKER_RX, "+", limit=1))
    for path, r in intros.items():
        if not has_multiworker:
            ctx.drop("no_multiworker_config")
            continue
        g = next((x for x in guards if r["seq"] <= x["seq"] <= r["seq"] + 30), None)
        if g is None:
            continue
        body = " ".join(x["text"] for x in ctx.lines_of(r["hash"], path) if x["sign"] == "+")
        side = bool(_SIDE_EFFECT.search(body))
        if not side and _IDEMPOTENT.search(body):
            ctx.drop("idempotent_job")
            continue
        yield Hit(
            subject=f"{path}|{r['hash']}", score=5 + (4 if side else 0) + (1.0 if "advisory" in g["text"] else 0),
            title=f"Scheduler added in {path}, then guarded in {g['path']} {g['seq'] - r['seq']} commits later",
            evidence={"scheduler_line": _norm(r["text"])[:120], "guard_line": _norm(g["text"])[:120],
                      "guard_path": g["path"], "side_effecting_job": side, "commits_between": g["seq"] - r["seq"]},
            commits=[r["hash"], g["hash"]], diffs=[(r["hash"], path), (g["hash"], g["path"])],
            paths=sorted({path, g["path"]}),
        )


# ---------------------------------------------------------------------------- nondeterministic iteration
SORT_RX = r"sorted\(|sort_keys\s*=\s*True|\.sort\(|sort\.(Strings|Slice|Ints)\("
SINK_RX = re.compile(r"(?i)hashlib|hmac|\bdigest\b|etag|cache_key|signature|sha1|sha256|md5|hexdigest")
_ITER_RX = re.compile(r"\.items\(\)|\.keys\(\)|\.values\(\)|\bjoin\(|json\.dumps\(|for\s+\w+(\s*,\s*\w+)?\s+in\s+")
_SORTED = re.compile(SORT_RX)
_LOGGING = re.compile(r"(?i)\blog(ger|ging)?\b|print\(|debug|repr\(|__str__|__repr__")


@candidate("nondeterministic_iteration_fix_siblings")
def nondeterministic_iteration_fix_siblings(ctx: Ctx):
    """A real fix adds sorted()/sort_keys=True/.sort() inside a symbol that feeds a hash/digest/etag/cache-key/
    signature; alive sibling symbols with the same sink tokens and unordered iteration (dict/set iteration, join,
    json.dumps) but no sorting are reported (cap 5, same file first)."""
    sinks = _cache(ctx, "sinks", lambda: [s for s in ctx.alive_symbols if s["body"] and SINK_RX.search(s["body"])
                                           and _prod(s["path"]) and s["kind"] in ("function", "method")])
    seen = set()
    for f in ctx.fix_events:
        c = ctx.by_hash.get(f["hash"])
        if c is None or not is_fix_message(c["message"]) or not _prod(f["path"]) or (f["hash"], f["uid"]) in seen:
            continue
        rows = [r for r in ctx.lines_of(f["hash"], f["path"]) if r["sign"] == "+" and re.search(SORT_RX, r["text"])]
        if not rows:
            continue
        seen.add((f["hash"], f["uid"]))
        sym = ctx.symbols.get(f["uid"])
        if sym is None:
            continue
        if all(_LOGGING.search(r["text"]) for r in rows):
            ctx.drop("logging_only")
            continue
        if not SINK_RX.search(sym["body"] or ""):
            ctx.drop("no_hash_sink")
            continue
        sibs = [s for s in sinks if s["uid"] != f["uid"] and _ITER_RX.search(s["body"]) and not _SORTED.search(s["body"])]
        sibs.sort(key=lambda s: (s["path"] != f["path"], s["path"], s["start_line"] or 0))
        sibs = sibs[:5]
        if not sibs:
            continue
        yield Hit(
            subject=f["hash"] + "|" + f["uid"], score=len(sibs) * 2 + 3 + 0.2 * len(ctx.callers(f["uid"])),
            title=f"Fix added ordering to {sym['qualname']}; {len(sibs)} sibling hash/cache functions still iterate unordered",
            evidence={"fix_subject": subject_of(c["message"]), "added_lines": [_norm(r["text"])[:100] for r in rows[:3]],
                      "siblings": [{"uid": s["uid"], "path": s["path"], "qualname": s["qualname"]} for s in sibs]},
            commits=[f["hash"]], diffs=[(f["hash"], f["path"])], paths=[f["path"], *[s["path"] for s in sibs[:4]]],
            symbols=[f["uid"], *[s["uid"] for s in sibs]],
        )


# ---------------------------------------------------------------------------- worker count vs process-local state
WORKER_RX = (r"(?i)\bworkers\s*[=:]\s*\S+|WEB_CONCURRENCY\s*[=:]\s*\S+|--workers[ =]\S+|\binstances\s*[=:]\s*\S+|"
             r"exec_mode[\"']?\s*[=:]\s*[\"']?cluster|cluster\.fork|\breplicas\s*:\s*\S+|\bgunicorn\b.*\s-w\s*\d+|"
             r"--processes[ =]\d+")
_NUM = re.compile(r"\d+")
_PY_CONTAINER = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(\{\s*\}|\[\s*\]|dict\(\)|set\(\)|list\(\)|"
                           r"(?:collections\.)?(?:defaultdict|OrderedDict|deque|Counter)\(|WeakValueDictionary\()", re.M)
_JS_CONTAINER = re.compile(r"^(?:let|var|const)\s+(\w+)\s*=\s*(\[\]|\{\}|new Map\(|new Set\()", re.M)
_HANDLERISH = re.compile(r"(?i)(view|route|handler|api|server|app|controller|endpoint|consumer|worker|session|cache|"
                         r"state|request|web)")
_ENTRY = re.compile(r"(?i)(^|/)(main|app|wsgi|asgi|server|manage|__init__|index)\.(py|js|ts)$")


def _worker_value(text: str, key: str) -> int | None:
    """Worker/instance count in a config line (99 for cpu_count/max/cluster style values)."""
    if re.search(r"(?i)cluster", text):
        return 99
    m = re.search(r"(?i)(?:workers|WEB_CONCURRENCY|instances|replicas|--processes|-w)\s*[=: ]\s*[\"']?(\w+(?:\([^)]*\))?)", text)
    if not m:
        return None
    v = m.group(1)
    if v.isdigit():
        return int(v)
    if re.search(r"(?i)cpu|max|auto|\(", v):
        return 99
    n = _NUM.search(v)
    return int(n.group()) if n else None


def _containers(ctx: Ctx):
    """Module-level mutable containers at HEAD with indented mutation sites, ranked."""
    def build():
        out = []
        entry_files = {p for p in ctx.head_files if _ENTRY.search(p)}
        for path, text in _head_py(ctx):
            handler = bool(_HANDLERISH.search(path))
            imported_by = ctx.imports[1].get(path, set())
            near = bool(imported_by & entry_files) or path in entry_files
            for m in _PY_CONTAINER.finditer(text):
                name = m.group(1)
                if name.startswith("__"):
                    continue
                esc = re.escape(name)
                mut = re.compile(rf"^[ \t]+.*(\b{esc}\s*\[[^\]\n]*\]\s*(=|\+=)(?!=)|\b{esc}\.(append|add|update|pop|setdefault|"
                                 rf"clear|extend|remove|popitem|discard|insert)\(|\bglobal\s+{esc}\b|\bdel\s+{esc}\[)", re.M)
                sites = len(mut.findall(text))
                top = len(re.findall(rf"^{esc}(\[|\.(append|add|update|setdefault|extend)\()", text, re.M))
                if sites == 0:
                    ctx.drop("constant_or_never_mutated" if name.isupper() or top == 0 else "import_time_registry")
                    continue
                out.append({"path": path, "name": name, "sites": sites, "handler": handler, "near_entry": near,
                            "score": sites * (1.5 if handler else 1) * (1.3 if near else 1)})
        for p in sorted(ctx.head_files):
            if p.endswith((".js", ".ts", ".mjs")) and _prod(p) and not _SCRIPT_PATH.search(p) and len(out) < 4000:
                t = ctx.text_at_head(p)
                for m in _JS_CONTAINER.finditer(t or ""):
                    name = m.group(1)
                    sites = len(re.findall(rf"^[ \t]+.*\b{re.escape(name)}(\.(push|set|add|delete|splice|pop)\(|\[[^\]\n]+\]\s*=)", t, re.M))
                    if sites:
                        out.append({"path": p, "name": name, "sites": sites, "handler": bool(_HANDLERISH.search(p)),
                                    "near_entry": p in entry_files, "score": sites * (1.5 if _HANDLERISH.search(p) else 1)})
        out.sort(key=lambda d: -d["score"])
        return out
    return _cache(ctx, "containers", build)


@candidate("worker_count_vs_process_local_state")
def worker_count_vs_process_local_state(ctx: Ctx):
    """Fires on a config transition from one worker/instance/replica to several (gunicorn workers, WEB_CONCURRENCY,
    pm2 instances/cluster, replicas). Evidence ranks HEAD module-level mutable containers by indented mutation sites,
    weighting handler-like files and files imported by an entrypoint. Constants, never-mutated names and import-time
    registries are dropped."""
    per: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(WORKER_RX, None):
        p = r["path"]
        if is_docs(p) or is_test(p) or not (re.search(r"(?i)\.(ya?ml|json|toml|ini|cfg|conf|py|js|sh|env|tf)$", p)
                                           or re.search(r"(?i)(Procfile|Dockerfile)", p)):
            continue
        per[(r["hash"], p)][r["sign"]].append(r)
    for (h, path), d in per.items():
        new = [(_worker_value(r["text"], ""), r) for r in d["+"]]
        new = [(v, r) for v, r in new if v is not None]
        old = [v for v in (_worker_value(r["text"], "") for r in d["-"]) if v is not None]
        if not new:
            continue
        nv, nr = max(new, key=lambda t: t[0])
        cluster_intro = bool(re.search(r"(?i)cluster", nr["text"])) and not old
        if nv <= 1 or not (cluster_intro or (old and min(old) <= 1)):
            continue
        conts = _containers(ctx)[:8]
        c = ctx.by_hash[h]
        yield Hit(
            subject=f"{path}|{h}", score=5 + sum(d_["score"] for d_ in conts[:5]) * 0.3 + (2 if nv >= 99 else 0),
            title=f"{path}: workers 1 -> {'many' if nv >= 99 else nv} ({subject_of(c['message'])[:50]})",
            evidence={"config_line": _norm(nr["text"])[:120], "old_values": old, "new_value": nv,
                      "process_local_containers": [{"path": x["path"], "name": x["name"], "mutation_sites": x["sites"],
                                                    "handler_like": x["handler"], "near_entrypoint": x["near_entry"]}
                                                   for x in conts]},
            commits=[h], diffs=[(h, path)], paths=[path, *dict.fromkeys(x["path"] for x in conts[:4])],
        )


# ---------------------------------------------------------------------------- import-time side effects
SIDE_RX = (r"^(\w+(\s*:\s*[\w\[\], .]+)?\s*=\s*)?(requests\.(get|post|put|head)\(|urllib\.request|socket\.(socket|"
           r"create_connection)|sqlite3\.connect|psycopg2?\.connect|pymysql\.connect|redis\.(Redis|StrictRedis|from_url)|"
           r"create_engine\(|signal\.signal\(|atexit\.register\(|threading\.Thread\(|\w+\.start\(\)|open\(|"
           r"boto3\.(client|resource|Session)|MongoClient\(|logging\.basicConfig\(|os\.makedirs\(|subprocess\.|"
           r"\w*Pool\()")
_SIDE = re.compile(SIDE_RX)
_STARTUP_MSG = re.compile(r"(?i)\b(import(-|\s)?time|circular|fork(ed|ing)?|worker|startup|start-up|side[- ]effects?|"
                          r"at import|lazy)\b")


@candidate("import_time_side_effect_lineage")
def import_time_side_effect_lineage(ctx: Ctx):
    """A column-0 side-effect statement (I/O, connect, signal, atexit, thread start, pools) is removed from a module
    and re-added indented in the same commit (moved into a function), or removed by a fix whose message mentions
    import/circular/fork/worker/startup while sibling modules still keep the pattern at HEAD. Weighted by importer
    count. `if __name__` blocks, scripts, conftest and tests are dropped."""
    col0 = lambda t: bool(t) and not t[0].isspace()  # noqa: E731
    removed = [r for r in ctx.grep(SIDE_RX, "-", path_like="%.py") if col0(r["text"])]
    added = [r for r in ctx.grep(SIDE_RX, "+", path_like="%.py") if col0(r["text"])]
    imported_by = ctx.imports[1]
    siblings_cache: dict[str, list[str]] = {}

    def siblings(exclude: str) -> list[str]:
        if "all" not in siblings_cache:
            seen = []
            for r in added:
                if r["path"] not in seen and _prod(r["path"]) and not _SCRIPT_PATH.search(r["path"]):
                    seen.append(r["path"])
            keep = []
            for p in seen[:80]:
                t = ctx.text_at_head(p) or ""
                if any(_SIDE.match(ln) for _, ln in _col0_before_main(t)):
                    keep.append(p)
            siblings_cache["all"] = keep
        return [p for p in siblings_cache["all"] if p != exclude][:6]

    seen = set()
    for r in removed:
        path = r["path"]
        if not _prod(path) or _SCRIPT_PATH.search(path):
            ctx.drop("tests_scripts_conftest")
            continue
        c = ctx.by_hash.get(r["hash"])
        if c is None or c["is_bulk"] or c["is_merge"]:
            continue
        stripped = r["text"].strip()
        same = ctx.lines_of(r["hash"], path)
        moved = any(x["sign"] == "+" and x["text"][:1].isspace() and x["text"].strip() == stripped for x in same)
        fixmsg = bool(_STARTUP_MSG.search(c["message"] or "")) and ctx.is_fix(c)
        sib = siblings(path)
        if not moved and not (fixmsg and sib):
            continue
        old = ctx.text_before(r["hash"], path) or ""
        lines = old.splitlines()
        if r["line"] and any(ln.startswith("if __name__") for ln in lines[:max(0, r["line"] - 1)]):
            ctx.drop("main_guarded")
            continue
        key = (path, stripped, r["hash"])
        if key in seen:
            continue
        seen.add(key)
        imp = len(imported_by.get(path, ()))
        yield Hit(
            subject=f"{path}|{stripped[:80]}|{r['hash'][:10]}",
            score=3 + (4 if moved else 0) + (2 if fixmsg else 0) + min(6, imp * 0.5) + len(sib) * 0.5,
            title=f"Import-time `{stripped[:60]}` in {path} {'moved into a function' if moved else 'removed'}"
                  f" ({subject_of(c['message'])[:40]})",
            evidence={"statement": stripped[:120], "moved_into_function": moved, "startup_fix_message": fixmsg,
                      "importers": imp, "siblings_still_import_time": sib},
            commits=[r["hash"]], diffs=[(r["hash"], path)], paths=[path, *sib[:3]],
        )


# ---------------------------------------------------------------------------- preload / fork safety
PRELOAD_RX = r"preload_app\s*=\s*True|--preload\b|\bpreload\s*[:=]\s*(true|True)|prefork"
FORKFIX_RX = (r"post_fork|register_at_fork|engine\.dispose\(|\.dispose\(close|worker_process_init|when_ready|on_starting|"
              r"_after_fork|pthread_atfork|reset_after_fork")
_RESOURCE = re.compile(r"^(\w+)\s*(?::[^=\n]+)?=\s*(create_engine|redis\.\w+|Redis|MongoClient|boto3\.\w+|"
                       r"requests\.Session|psycopg2\.connect|sqlite3\.connect|threading\.Thread|\w*Pool|"
                       r"ThreadPoolExecutor|httpx\.(?:Async)?Client|aiohttp\.ClientSession|\w+\.connect)\(")


@candidate("preload_app_fork_unsafe_init")
def preload_app_fork_unsafe_init(ctx: Ctx):
    """Trigger: a commit adding preload_app/--preload/prefork config, or a fix adding post_fork/register_at_fork/
    engine.dispose/worker_process_init. Evidence only: module-level resources created at import time (column 0, before
    any `if __name__`) at HEAD. Scripts, CLIs, tests and function-local (factory) creation are not listed."""
    def resources():
        out = []
        for path, text in _head_py(ctx):
            for ln_no, ln in _col0_before_main(text):
                m = _RESOURCE.match(ln)
                if m:
                    out.append({"path": path, "line": ln_no, "statement": ln.strip()[:90]})
        return out
    res = _cache(ctx, "resources", resources)
    trig: dict[str, tuple] = {}
    for r in ctx.grep(PRELOAD_RX, "+"):
        if is_docs(r["path"]) or is_test(r["path"]):
            ctx.drop("docs_or_tests")
            continue
        trig.setdefault(r["hash"], ("preload_config", r))
    for r in ctx.grep(FORKFIX_RX, "+"):
        c = ctx.by_hash.get(r["hash"])
        if is_docs(r["path"]) or is_test(r["path"]) or c is None:
            continue
        if not ctx.is_fix(c) and r["hash"] not in trig:
            ctx.drop("not_a_fix")
            continue
        trig.setdefault(r["hash"], ("fork_hook_fix", r))
    for h, (kind, r) in trig.items():
        c = ctx.by_hash[h]
        if c["is_bulk"] or c["is_merge"]:
            continue
        yield Hit(
            subject=f"{h}|{r['path']}", score=4 + (3 if kind == "fork_hook_fix" else 2) + min(5, len(res) * 0.5),
            title=f"{'Fork hook added' if kind == 'fork_hook_fix' else 'Preload enabled'} in {r['path']} "
                  f"({subject_of(c['message'])[:50]})",
            evidence={"trigger": kind, "line": _norm(r["text"])[:120],
                      "import_time_resources_at_head": res[:12], "resource_count": len(res)},
            commits=[h], diffs=[(h, r["path"])], paths=[r["path"], *dict.fromkeys(x["path"] for x in res[:4])],
        )
