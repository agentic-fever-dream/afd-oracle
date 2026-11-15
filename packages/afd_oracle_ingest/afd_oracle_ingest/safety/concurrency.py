"""Structural race-hazard detection (Python; module-level `let` in TS/JS).

Finds state that outlives a single call and is written from functions: module-level mutable containers, rebound
globals, lazy initialisation without a lock, class-level mutable attributes, and check-then-act across an `await`.
A write inside a `with <lock>:` block, or in a module that never starts threads/tasks, is downgraded.
"""
from __future__ import annotations

import re

from .rules import Finding
from .source import SourceFile, is_test_path

MUTABLE_INIT = r"(\[|\{|set\(|dict\(|list\(|defaultdict\(|OrderedDict\(|deque\(|Counter\(|WeakValueDictionary\(|WeakKeyDictionary\()"
MUTATORS = r"\.(append|extend|add|update|pop|popitem|clear|setdefault|insert|remove|discard|appendleft|__setitem__)\("
LOCKED = re.compile(r"^\s*(async\s+)?with\s+.*(lock|Lock|mutex|Mutex|Semaphore|Condition|RLock)\b")


def _under_lock(lines: list[str], i: int) -> bool:
    ind = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, max(-1, i - 25), -1):
        s = lines[j]
        if not s.strip():
            continue
        si = len(s) - len(s.lstrip())
        if si < ind:
            if LOCKED.match(s):
                return True
            if re.match(r"\s*(async\s+)?def\b|\s*class\b", s):
                return False
            ind = si
    return False


def scan_python(src: SourceFile, repo_id: str, only_lines: set[int] | None = None) -> list[Finding]:
    if src.lang != "python" or is_test_path(src.path):
        return []
    lines = src.lines
    out: list[Finding] = []
    concurrent_module = re.search(r"\b(threading|asyncio|concurrent\.futures|multiprocessing|gevent|eventlet|"
                                  r"async\s+def)\b", src.text) is not None
    # module-level mutable containers
    shared = {}
    for i, s in enumerate(lines):
        m = re.match(rf"^([A-Za-z_]\w*)\s*(:\s*[^=]+)?=\s*{MUTABLE_INIT}", s)
        if m:  # only reported if a function actually mutates it, so constant tables stay quiet
            shared[m.group(1)] = i + 1
    for name, defined in shared.items():
        rx = re.compile(rf"(?<![\w.]){re.escape(name)}({MUTATORS}|\[[^\]]+\]\s*=(?!=)|\s*\+=)|^\s*del\s+{re.escape(name)}\[")
        for i, s in enumerate(lines):
            if not s.startswith((" ", "\t")) or not rx.search(s):
                continue
            n = i + 1
            if only_lines is not None and n not in only_lines:
                continue
            locked = _under_lock(lines, i)
            sev = "info" if locked or not concurrent_module else "warn"
            out.append(Finding("race", "race.shared_mutation", sev, src.path, n,
                               f"mutates module-level `{name}` (defined line {defined})"
                               + (" under a lock" if locked else ": unsynchronised if called concurrently"),
                               src.uid_at(repo_id, n), {"text": s.strip()[:200], "state": name}))
    # lazy init: `global X` + `if X is None:` + assignment, without a lock
    for i, s in enumerate(lines):
        m = re.match(r"^\s+if\s+(\w+)\s+is\s+None\s*:", s)
        if not m:
            continue
        name = m.group(1)
        fn_start = next((j for j in range(i, -1, -1) if re.match(r"\s*(async\s+)?def\b", lines[j])), None)
        if fn_start is None or not re.search(rf"^\s+global\s+.*\b{name}\b", "\n".join(lines[fn_start:i]), re.M):
            continue
        n = i + 1
        if only_lines is not None and n not in only_lines:
            continue
        if not _under_lock(lines, i):
            out.append(Finding("race", "race.lazy_init", "warn", src.path, n,
                               f"lazy initialisation of global `{name}` without a lock (double initialisation)",
                               src.uid_at(repo_id, n), {"text": s.strip()[:200], "state": name}))
    # class-level mutable attributes (shared by all instances)
    cls_indent = None
    for i, s in enumerate(lines):
        cm = re.match(r"^(\s*)class\s+\w+", s)
        if cm:
            cls_indent = len(cm.group(1))
            continue
        if cls_indent is None or not s.strip():
            continue
        ind = len(s) - len(s.lstrip())
        if ind <= cls_indent:
            cls_indent = None
            continue
        sym = src.enclosing(i + 1)
        if sym is None or sym.kind not in ("class",):
            continue  # inside a method
        m = re.match(r"^\s+(\w+)\s*(:\s*[^=]+)?=\s*(\[\]|\{\}|set\(\)|dict\(\)|list\(\))\s*(#.*)?$", s)
        if m and not m.group(1).startswith("__"):
            n = i + 1
            if only_lines is None or n in only_lines:
                out.append(Finding("race", "race.class_mutable", "warn", src.path, n,
                                   f"class attribute `{m.group(1)}` is a mutable container shared by every instance",
                                   src.uid_at(repo_id, n), {"text": s.strip()[:200]}))
    # check-then-act across an await (async def)
    for sym in src.symbols():
        if sym.kind != "function":
            continue
        head = lines[sym.start_line - 1] if sym.start_line - 1 < len(lines) else ""
        if "async def" not in head:
            continue
        body = lines[sym.start_line - 1: sym.end_line]
        for k, s in enumerate(body):
            m = re.match(r"^\s*if\s+.*\bself\.(\w+)", s)
            if not m:
                continue
            attr = m.group(1)
            window = body[k + 1: k + 9]
            awaited = next((w for w in range(len(window)) if re.search(r"\bawait\b", window[w])), None)
            if awaited is None:
                continue
            if any(re.search(rf"\bself\.{attr}\s*(=(?!=)|\+=|-=)", w) for w in window[awaited:]):
                n = sym.start_line + k
                if only_lines is None or n in only_lines:
                    out.append(Finding("race", "race.await_check_act", "warn", src.path, n,
                                       f"`self.{attr}` is checked, then written after an await: another task can "
                                       "interleave", src.uid_at(repo_id, n), {"text": s.strip()[:200]}))
    return out


def scan_js(src: SourceFile, repo_id: str, only_lines: set[int] | None = None) -> list[Finding]:
    if src.lang not in ("typescript", "tsx", "javascript") or is_test_path(src.path):
        return []
    lines = src.lines
    names = {m.group(1): i + 1 for i, s in enumerate(lines) for m in [re.match(r"^let\s+(\w+)", s)] if m}
    out = []
    if not names or "async" not in src.text:
        return out
    for name, defined in names.items():
        rx = re.compile(rf"^\s+.*(?<![\w.]){re.escape(name)}\s*(=(?!=)|\+=|-=|\+\+|--|\.push\(|\.set\()")
        for i, s in enumerate(lines):
            if rx.search(s):
                n = i + 1
                if only_lines is None or n in only_lines:
                    out.append(Finding("race", "race.js_module_state", "info", src.path, n,
                                       f"module-level `{name}` (line {defined}) written inside a function",
                                       src.uid_at(repo_id, n), {"text": s.strip()[:200]}))
    return out


def scan(src: SourceFile, repo_id: str, only_lines: set[int] | None = None) -> list[Finding]:
    return scan_python(src, repo_id, only_lines) + scan_js(src, repo_id, only_lines)
