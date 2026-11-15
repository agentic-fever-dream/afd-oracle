"""Name resolution for the structural snapshot: import specifiers -> files, call/base names -> symbols.

Resolution order: same file -> explicitly imported -> unique global name. Ambiguous name-only matches are dropped
rather than guessed; every edge carries a ``confidence`` (exact | import | name).
"""
from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass, field

from afd_oracle_core.ids import symbol_uid

from .extractor import FileParse, SymbolRecord

STOP_NAMES = {
    "get", "set", "append", "extend", "push", "pop", "map", "filter", "reduce", "keys", "values", "items", "join",
    "split", "strip", "format", "len", "print", "str", "int", "float", "bool", "list", "dict", "tuple", "range",
    "isinstance", "super", "log", "info", "debug", "warn", "warning", "error", "then", "catch", "forEach",
    "toString", "update", "add", "remove", "call", "apply", "bind", "open", "close", "read", "write", "send", "emit",
    "on", "off", "init", "run", "start", "stop", "copy", "sort", "find", "replace", "lower", "upper", "next",
    "has", "delete", "clear", "size", "length", "exec", "test", "match", "resolve", "reject", "require",
    "setTimeout", "setInterval", "parseInt", "parseFloat", "Promise", "Error", "Object", "Array", "String",
}
JS_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx")


def python_module_names(path: str) -> list[str]:
    if not path.endswith((".py", ".pyi")):
        return []
    parts = path.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts)) if parts[i:]]


@dataclass
class Snapshot:
    repo: str
    parses: dict[str, FileParse]
    py_index: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    sym_uid: dict[tuple[str, str], str] = field(default_factory=dict)  # (path, qualname) -> uid
    records: dict[str, tuple[str, SymbolRecord]] = field(default_factory=dict)  # uid -> (path, rec)

    def __post_init__(self) -> None:
        for path, fp in self.parses.items():
            for m in python_module_names(path):
                self.py_index[m].add(path)
            for rec in fp.symbols:
                uid = symbol_uid(self.repo, path, rec.qualname, rec.kind)
                self.sym_uid[(path, rec.qualname)] = uid
                self.records[uid] = (path, rec)

    # -- imports ---------------------------------------------------------------
    def _pick(self, importer: str, cands: set[str]) -> str | None:
        if not cands:
            return None
        if len(cands) == 1:
            return next(iter(cands))
        d = importer.split("/")[:-1]

        def shared(c: str) -> int:
            n = 0
            for x, y in zip(d, c.split("/")[:-1]):
                if x != y:
                    break
                n += 1
            return n

        return sorted(cands, key=lambda c: (-shared(c), len(c), c))[0]

    def resolve_import(self, importer: str, module: str, names: list[str]) -> list[tuple[str, list[str]]]:
        """-> [(target_path, imported_names)]"""
        fp = self.parses[importer]
        out: list[tuple[str, list[str]]] = []
        if fp.lang == "python":
            if module.startswith("."):
                level = len(module) - len(module.lstrip("."))
                rest = module[level:]
                base = posixpath.dirname(importer)
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                stem = posixpath.join(base, *rest.split(".")) if rest else base
                mods = [stem + ".py", stem + "/__init__.py"]
                sub = [(posixpath.join(stem, n) + ".py", n) for n in names if n != "*"]
                for path, n in sub:
                    if path in self.parses:
                        out.append((path, [n]))
                for m in mods:
                    if m in self.parses and m != importer:
                        out.append((m, names))
                        break
                return out
            for n in names:
                if n != "*":
                    t = self._pick(importer, self.py_index.get(f"{module}.{n}", set()))
                    if t:
                        out.append((t, [n]))
            t = self._pick(importer, self.py_index.get(module, set()))
            if t and t != importer:
                out.append((t, names))
            return out
        # JS / TS: only relative specifiers are resolvable inside the repo
        if module.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
            for ext in JS_EXTS:
                if base + ext in self.parses:
                    return [(base + ext, names)]
        return out

    # -- symbols ---------------------------------------------------------------
    def top_level(self, path: str, name: str, kinds: tuple[str, ...]) -> str | None:
        fp = self.parses.get(path)
        if not fp:
            return None
        for r in fp.symbols:
            if r.parent is None and r.name == name and r.kind in kinds:
                return symbol_uid(self.repo, path, r.qualname, r.kind)
        return None

    def constructor_of(self, class_uid: str) -> str | None:
        path, rec = self.records[class_uid]
        for ctor in ("__init__", "constructor"):
            u = self.sym_uid.get((path, f"{rec.qualname}.{ctor}"))
            if u:
                return u
        return None


def build_edges(snap: Snapshot) -> list[tuple[str, str, str, dict]]:
    """-> [(kind, src_uid_or_path, dst_uid_or_path, props)] for IMPORTS, CALLS, INHERITS, IMPLEMENTS."""
    edges: dict[tuple[str, str, str], dict] = {}
    imported_names: dict[str, dict[str, str]] = defaultdict(dict)  # path -> name -> target path
    module_imports: dict[str, set[str]] = defaultdict(set)  # path -> whole-module targets

    for path, fp in snap.parses.items():
        for imp in fp.imports:
            for target, names in snap.resolve_import(path, imp.module, imp.names):
                if target == path:
                    continue
                key = ("IMPORTS", path, target)
                props = edges.setdefault(key, {"names": [], "confidence": "exact", "type_only": True})
                props["names"] = sorted(set(props["names"]) | {n for n in names if n})
                props["type_only"] = props["type_only"] and bool(getattr(imp, "type_only", False))
                real = [n for n in names if n not in ("*", "default")]
                for n in real:
                    imported_names[path][n] = target
                if not real:
                    module_imports[path].add(target)

    # global name indexes for name-only fallback
    top_funcs: dict[str, list[str]] = defaultdict(list)
    methods: dict[str, list[str]] = defaultdict(list)
    classes: dict[str, list[str]] = defaultdict(list)
    for uid, (path, rec) in snap.records.items():
        if rec.kind == "function":
            (methods if rec.parent else top_funcs)[rec.name].append(uid)
        elif rec.kind in ("class", "interface"):
            classes[rec.name].append(uid)

    def resolve_type(path: str, name: str) -> tuple[str | None, str]:
        u = snap.top_level(path, name, ("class", "interface"))
        if u:
            return u, "exact"
        t = imported_names[path].get(name)
        if t:
            u = snap.top_level(t, name, ("class", "interface"))
            if u:
                return u, "import"
        c = classes.get(name, [])
        return (c[0], "name") if len(c) == 1 else (None, "")

    # inheritance first (method resolution below uses bases)
    bases_of: dict[str, list[str]] = defaultdict(list)
    for uid, (path, rec) in snap.records.items():
        if rec.kind not in ("class", "interface"):
            continue
        for b in rec.bases:
            t, conf = resolve_type(path, b)
            if t and t != uid:
                kind = "IMPLEMENTS" if (snap.records[t][1].kind == "interface" and rec.kind == "class") else "INHERITS"
                edges[(kind, uid, t)] = {"confidence": conf}
                bases_of[uid].append(t)
        for b in rec.implements:
            t, conf = resolve_type(path, b)
            if t and t != uid:
                edges[("IMPLEMENTS", uid, t)] = {"confidence": conf}

    def class_method(class_uid: str, name: str, depth: int = 0) -> str | None:
        path, crec = snap.records[class_uid]
        u = snap.sym_uid.get((path, f"{crec.qualname}.{name}"))
        if u or depth > 4:
            return u
        for b in bases_of.get(class_uid, []):
            u = class_method(b, name, depth + 1)
            if u:
                return u
        return None

    for uid, (path, rec) in snap.records.items():
        if rec.kind != "function" and not (rec.kind == "variable" and rec.calls):
            continue
        owner = snap.sym_uid.get((path, rec.parent)) if rec.parent else None
        if owner is None and rec.parent:
            owner = next((u for (p, q), u in snap.sym_uid.items() if p == path and q == rec.parent), None)
        for call in rec.calls:
            target, conf = None, ""
            if call.startswith("."):
                name = call[1:]
                if owner and owner in snap.records and snap.records[owner][1].kind in ("class", "interface"):
                    target = class_method(owner, name)
                    conf = "exact" if target else ""
                if not target:
                    for mod in module_imports[path]:
                        target = snap.top_level(mod, name, ("function",))
                        if target:
                            conf = "import"
                            break
                if not target and name not in STOP_NAMES and len(name) > 3:
                    cands = methods.get(name, []) + top_funcs.get(name, [])
                    if len(cands) == 1:
                        target, conf = cands[0], "name"
            else:
                name = call
                target = snap.top_level(path, name, ("function",))
                conf = "exact" if target else ""
                if not target:
                    cls = snap.top_level(path, name, ("class",))
                    if cls:
                        target, conf = snap.constructor_of(cls), "exact"
                if not target and name in imported_names[path]:
                    t = imported_names[path][name]
                    target = snap.top_level(t, name, ("function",))
                    if not target:
                        cls = snap.top_level(t, name, ("class",))
                        target = snap.constructor_of(cls) if cls else None
                    conf = "import" if target else ""
                if not target and name not in STOP_NAMES:
                    cands = top_funcs.get(name, [])
                    if len(cands) == 1:
                        target, conf = cands[0], "name"
            if target and target != uid:
                props = edges.setdefault(("CALLS", uid, target), {"count": 0, "confidence": conf})
                props["count"] += 1
    return [(k, s, d, p) for (k, s, d), p in edges.items()]
