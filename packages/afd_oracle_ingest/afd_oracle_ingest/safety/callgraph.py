"""Type-aware call resolution (Jedi, Python) and OVERRIDES edges.

The ingest resolver links calls by name and imports. Here each call site in non-test Python code whose callee name
matches a repository symbol is resolved with Jedi's go-to-definition against a materialised HEAD tree, giving
CALLS edges with confidence "typed". OVERRIDES links a subclass method to the base-class method it redefines, so a
change to the base reaches every override (and vice versa). Bounded by a time budget; coverage is reported.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from afd_oracle_core.store import Store

from .source import SourceFile, is_test_path

CALL = re.compile(r"(?<![\w.])(\w+)\s*\(|\.(\w+)\s*\(")
KEYWORDS = {"if", "for", "while", "return", "print", "def", "class", "with", "elif", "and", "or", "not", "in",
            "lambda", "yield", "assert", "except", "super", "isinstance", "len", "str", "int", "dict", "list", "set",
            "tuple", "type", "getattr", "setattr", "hasattr", "range", "enumerate", "zip", "bool", "float", "repr",
            "sorted", "min", "max", "any", "all", "iter", "next", "map", "filter", "callable", "id", "format"}


def overrides(store: Store) -> list[tuple[str, str, str, str]]:
    methods: dict[str, dict[str, str]] = {}
    for r in store.q("SELECT uid, name, parent FROM symbols WHERE alive=1 AND kind='function' AND parent IS NOT NULL"):
        methods.setdefault(r["parent"], {})[r["name"]] = r["uid"]
    out = []
    for e in store.q("SELECT src, dst FROM edges WHERE kind='INHERITS'"):
        sub, base = methods.get(e["src"], {}), methods.get(e["dst"], {})
        for name, uid in sub.items():
            if name in base:
                out.append(("OVERRIDES", uid, base[name], json.dumps({"confidence": "exact"})))
    return out


def typed_calls(store: Store, sources: dict[str, SourceFile], workdir: Path, budget_s: float = 180.0) -> dict:
    try:
        import jedi
    except ImportError:
        return {"skipped": "jedi not installed"}
    repo_id = store.repo_id
    py = {p: s for p, s in sources.items() if s.lang == "python"}
    if not py:
        return {"resolved": 0}
    root = workdir / f"{repo_id}-head"
    shutil.rmtree(root, ignore_errors=True)
    for p, s in py.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(s.text, encoding="utf-8")
    names = {r["name"] for r in store.q("SELECT DISTINCT name FROM symbols WHERE alive=1 AND kind IN ('function','class')")}
    by_path: dict[str, dict[str, str]] = {}
    for r in store.q("SELECT uid, path, qualname FROM symbols WHERE alive=1 AND kind IN ('function','class')"):
        by_path.setdefault(r["path"], {})[r["qualname"]] = r["uid"]
    project = jedi.Project(str(root), added_sys_path=[str(root / "src")] if (root / "src").is_dir() else ())
    edges: dict[tuple[str, str], int] = {}
    t0, sites, files_done = time.time(), 0, 0
    for p, s in sorted(py.items()):
        if is_test_path(p) or time.time() - t0 > budget_s:
            continue
        try:
            script = jedi.Script(s.text, path=str(root / p), project=project)
        except Exception:
            continue
        files_done += 1
        for i, line in enumerate(s.lines):
            stripped = line.lstrip()
            if stripped.startswith(("def ", "class ", "async def ", "#", "@")):
                continue
            for m in CALL.finditer(line):
                name = m.group(1) or m.group(2)
                if name in KEYWORDS or name not in names:
                    continue
                caller = s.uid_at(repo_id, i + 1)
                if caller is None:
                    continue
                col = m.start(1) if m.group(1) else m.start(2)
                sites += 1
                try:
                    defs = script.goto(i + 1, col, follow_imports=True)
                except Exception:
                    continue
                for d in defs:
                    if d.module_path is None or d.type not in ("function", "class"):
                        continue
                    try:
                        rel = Path(d.module_path).resolve().relative_to(root.resolve()).as_posix()
                    except ValueError:
                        continue
                    cands = by_path.get(rel, {})
                    full = d.full_name or ""
                    best = max((q for q in cands if full == q or full.endswith("." + q)), key=len, default=None)
                    if best and cands[best] != caller:
                        edges[(caller, cands[best])] = edges.get((caller, cands[best]), 0) + 1
            if time.time() - t0 > budget_s:
                break
    shutil.rmtree(root, ignore_errors=True)
    rows = [("CALLS", a, b, json.dumps({"confidence": "typed", "count": n})) for (a, b), n in edges.items()]
    with store.tx() as conn:
        existing = {(r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM edges WHERE kind='CALLS'")}
        new = [r for r in rows if (r[1], r[2]) not in existing]
        conn.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?)", new)
        for _, a, b, props in rows:  # upgrade confidence on edges the name resolver already had
            if (a, b) in existing:
                conn.execute("UPDATE edges SET props=? WHERE kind='CALLS' AND src=? AND dst=?", (props, a, b))
    return {"call_sites": sites, "files": files_done, "typed_edges": len(rows), "new_edges": len(new),
            "time_s": round(time.time() - t0, 1), "budget_exhausted": time.time() - t0 > budget_s}
