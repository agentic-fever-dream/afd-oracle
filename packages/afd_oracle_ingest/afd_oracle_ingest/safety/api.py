"""Public API surface, deprecations and the breaking-change classifier.

Public (Python): modules whose path has no `_private` part and is not under tests/docs/examples/scripts; top-level
names without a leading underscore (restricted to `__all__` when the module defines it); methods of public classes,
plus `__init__`/`__call__`. TypeScript/JS: symbols whose declaration line starts with `export`, and their methods.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from afd_oracle_core.ids import symbol_uid

from ..ast.extractor import SymbolRecord
from .rules import Finding
from .source import SourceFile, is_test_path

NON_API_DIRS = ("tests/", "test/", "docs/", "doc/", "examples/", "example/", "scripts/", "benchmarks/", "bench/",
                "tools/", ".github/")
DEPRECATION = re.compile(r"DeprecationWarning|PendingDeprecationWarning|FutureWarning|@deprecated|\bdeprecated\(|"
                         r"@deprecated\b|\* @deprecated")


def is_api_path(path: str) -> bool:
    p = path.lower()
    if is_test_path(p) or p.startswith(NON_API_DIRS) or any(f"/{d}" in p for d in NON_API_DIRS):
        return False
    parts = p.split("/")
    return not any(part.startswith("_") and part not in ("__init__.py", "__init__.pyi") for part in parts)


def _all_names(src: SourceFile) -> set[str] | None:
    m = re.search(r"^__all__\s*(:\s*[^=]+)?=\s*[\[(]([^\])]*)[\])]", src.text, re.M | re.S)
    return set(re.findall(r"[\"'](\w+)[\"']", m.group(2))) if m else None


def public_symbols(src: SourceFile) -> dict[str, SymbolRecord]:
    """qualname -> record for the public symbols of one file."""
    if not src.parse or not is_api_path(src.path):
        return {}
    out: dict[str, SymbolRecord] = {}
    syms = [s for s in src.symbols() if s.kind in ("function", "class", "interface")]
    if src.lang == "python":
        allowed = _all_names(src)
        top = {s.qualname: s for s in syms if s.parent is None and not s.name.startswith("_")
               and (allowed is None or s.name in allowed)}
        out.update(top)
        for s in syms:
            if s.parent and s.parent in top and (not s.name.startswith("_") or s.name in ("__init__", "__call__")):
                out[s.qualname] = s
    else:
        exported = set()
        for s in syms:
            decl = src.lines[s.start_line - 1] if s.start_line - 1 < len(src.lines) else ""
            if s.parent is None and re.match(r"\s*export\b", decl):
                exported.add(s.qualname)
                out[s.qualname] = s
        for s in syms:
            if s.parent in exported and not s.name.startswith(("_", "#")):
                out[s.qualname] = s
    return out


def deprecations(src: SourceFile) -> dict[str, str]:
    """qualname -> evidence line for symbols whose body raises/marks a deprecation; module `__getattr__` names too."""
    out: dict[str, str] = {}
    for s in src.symbols():
        body = src.lines[max(0, s.start_line - 2): s.end_line]
        hit = next((ln.strip() for ln in body if DEPRECATION.search(ln)), None)
        if hit and s.kind != "variable":
            if s.name == "__getattr__" and s.parent is None:
                for name in re.findall(r"name\s*==\s*[\"'](\w+)[\"']", "\n".join(body)):
                    out[name] = hit[:160]
            else:
                out[s.qualname] = hit[:160]
    return out


# ---------------------------------------------------------------------------- signatures
@dataclass
class Param:
    name: str
    default: str | None
    kind: str  # positional | keyword_only | var_positional | var_keyword


def _split_top(s: str) -> list[str]:
    parts, depth, cur, quote = [], 0, "", None
    for i, ch in enumerate(s):
        if ch == ">" and i and s[i - 1] == "=":  # arrow `=>`, not a closing generic bracket
            cur += ch
            continue
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def params(signature: str, lang: str | None) -> list[Param] | None:
    start = signature.find("(")
    if start < 0:
        return None
    depth, end = 0, -1
    for i in range(start, len(signature)):
        if signature[i] == "(":
            depth += 1
        elif signature[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    out, kw_only = [], False
    for raw in _split_top(signature[start + 1:end]):
        if raw in ("/",):
            continue
        if raw == "*":
            kw_only = True
            continue
        default = None
        name_part = raw
        bits = re.split(r"(?<![<>!=])=(?![=>])", raw, maxsplit=1)  # `=` of a default, not `=>`, `==`, `<=`
        if len(bits) == 2 and not raw.startswith("*"):
            name_part, default = bits[0], bits[1].strip()
        name_part = name_part.split(":", 1)[0].strip()
        if lang != "python":
            name_part = name_part.replace("?", "").replace("readonly ", "").replace("public ", "").replace(
                "private ", "").replace("protected ", "").strip()
            if raw.split(":", 1)[0].strip().endswith("?") and default is None:
                default = "undefined"
        if name_part.startswith("**"):
            out.append(Param(name_part[2:], None, "var_keyword"))
        elif name_part.startswith("*") or name_part.startswith("..."):
            out.append(Param(name_part.lstrip("*."), None, "var_positional"))
            kw_only = True
        else:
            if name_part in ("self", "cls") and not out:
                continue
            out.append(Param(name_part, default, "keyword_only" if kw_only else "positional"))
    return out


def compare(old: SymbolRecord, new: SymbolRecord, lang: str | None) -> list[tuple[str, str, str]]:
    """[(severity, rule, message)] for one public symbol present on both sides."""
    out: list[tuple[str, str, str]] = []
    if old.kind != new.kind:
        out.append(("block", "api.kind_changed", f"`{old.qualname}` changed from {old.kind} to {new.kind}"))
    po, pn = params(old.signature, lang), params(new.signature, lang)
    if po is None or pn is None:
        return out
    old_by, new_by = {p.name: p for p in po}, {p.name: p for p in pn}
    has_var_kw = any(p.kind == "var_keyword" for p in pn)
    for p in po:
        if p.kind in ("var_positional", "var_keyword"):
            if not any(q.kind == p.kind for q in pn):
                out.append(("block", "api.param_removed", f"`{old.qualname}` no longer accepts *{p.name}"))
            continue
        q = new_by.get(p.name)
        if q is None:
            if not has_var_kw:
                out.append(("block", "api.param_removed", f"`{old.qualname}`: parameter `{p.name}` removed"))
            continue
        if p.default is not None and q.default is None:
            out.append(("block", "api.param_now_required", f"`{old.qualname}`: `{p.name}` is now required"))
        elif p.default is not None and q.default is not None and p.default != q.default:
            out.append(("warn", "api.default_changed",
                        f"`{old.qualname}`: default of `{p.name}` changed {p.default} -> {q.default}"))
        if p.kind == "positional" and q.kind == "keyword_only":
            out.append(("block", "api.param_now_keyword", f"`{old.qualname}`: `{p.name}` became keyword-only"))
    for q in pn:
        if q.name not in old_by and q.default is None and q.kind in ("positional", "keyword_only"):
            out.append(("block", "api.param_added_required", f"`{new.qualname}`: new required parameter `{q.name}`"))
    old_pos = [p.name for p in po if p.kind == "positional" and p.name in new_by]
    new_pos = [p.name for p in pn if p.kind == "positional" and p.name in old_by]
    if old_pos != new_pos:
        out.append(("warn", "api.param_reordered", f"`{old.qualname}`: positional parameters reordered "
                                                   f"({', '.join(old_pos)} -> {', '.join(new_pos)})"))
    ro = old.signature.rsplit("->", 1)[1].strip(" :") if "->" in old.signature else None
    rn = new.signature.rsplit("->", 1)[1].strip(" :") if "->" in new.signature else None
    if ro and rn and ro != rn:
        out.append(("info", "api.return_changed", f"`{old.qualname}`: return annotation {ro} -> {rn}"))
    return out


def classify(old: SourceFile | None, new: SourceFile | None, repo_id: str,
             deprecated: dict[str, str] | None = None) -> list[Finding]:
    """Breaking-change findings for one file between two versions (None = file absent)."""
    path = (new or old).path  # type: ignore[union-attr]
    o = public_symbols(old) if old else {}
    n = public_symbols(new) if new else {}
    out: list[Finding] = []
    lang = (new or old).lang  # type: ignore[union-attr]
    for qual, rec in o.items():
        uid = symbol_uid(repo_id, old.path, qual, rec.kind)  # type: ignore[union-attr]
        if qual not in n:
            dep = (deprecated or {}).get(qual)
            sev, note = ("warn", f" (deprecated: {dep})") if dep else ("block", "")
            out.append(Finding("api", "api.removed", sev, path, rec.start_line,
                               f"public {rec.kind} `{qual}` removed or renamed{note}", uid,
                               {"text": rec.signature[:200]}))
            continue
        for sev, rule, msg in compare(rec, n[qual], lang):
            out.append(Finding("api", rule, sev, path, n[qual].start_line, msg, uid,
                               {"old": rec.signature[:200], "new": n[qual].signature[:200]}))
    # exceptions a public function raises (changing them breaks callers that catch the old ones)
    if old and new:
        for qual, rec in n.items():
            if qual in o and rec.kind == "function":
                ro = set(re.findall(r"\braise\s+([A-Z]\w+)", "\n".join(old.lines[o[qual].start_line - 1:o[qual].end_line])))
                rn = set(re.findall(r"\braise\s+([A-Z]\w+)", "\n".join(new.lines[rec.start_line - 1:rec.end_line])))
                if ro - rn:
                    out.append(Finding("api", "api.exception_changed", "warn", path, rec.start_line,
                                       f"`{qual}` no longer raises {', '.join(sorted(ro - rn))} "
                                       f"(callers catching it may misbehave)",
                                       symbol_uid(repo_id, new.path, qual, rec.kind), {}))
    return out
