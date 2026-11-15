"""Python symbol extraction with tree-sitter."""
from __future__ import annotations

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from ..extractor import BODY_MAX, FileParse, ImportRecord, SymbolRecord, body_hash

_LANG = Language(tsp.language())


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


def _unwrap(node: Node) -> Node:
    if node.type == "decorated_definition":
        d = node.child_by_field_name("definition")
        return d if d is not None else node
    return node


def _docstring(body: Node | None) -> str:
    if body is None or not body.named_children:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
        raw = _t(first.named_children[0])
        return raw.strip("rbuRBU").strip("\"'").strip()[:1000]
    return ""


def _calls(node: Node) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "call":
            fn = n.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    out.append(_t(fn))
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None:
                        out.append("." + _t(attr))
        stack.extend(reversed(n.children))
    seen: dict[str, None] = {}
    for c in out:
        seen.setdefault(c, None)
    return list(seen)


def _assign_targets(stmt: Node) -> list[tuple[str, Node]]:
    """Module/class level ``NAME = ...`` / ``NAME: T = ...`` targets."""
    out = []
    if stmt.type != "expression_statement" or not stmt.named_children:
        return out
    a = stmt.named_children[0]
    if a.type == "assignment":
        left = a.child_by_field_name("left")
        if left is not None and left.type == "identifier":
            out.append((_t(left), a))
    return out


class PythonExtractor:
    lang = "python"

    def __init__(self) -> None:
        self.parser = Parser(_LANG)

    def parse(self, source: bytes) -> FileParse:
        tree = self.parser.parse(source)
        root = tree.root_node
        fp = FileParse(lang="python", loc=source.count(b"\n") + (0 if source.endswith(b"\n") else 1))
        for child in root.named_children:
            self._top(child, fp, parent=None)
            self._imports(child, fp)
        return fp

    # -- imports -------------------------------------------------------------
    def _imports(self, node: Node, fp: FileParse, type_only: bool = False) -> None:
        if node.type == "import_statement":
            for n in node.children_by_field_name("name"):
                target = n.child_by_field_name("name") if n.type == "aliased_import" else n
                fp.imports.append(ImportRecord(module=_t(target), type_only=type_only))
        elif node.type == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            names = []
            for n in node.children_by_field_name("name"):
                target = n.child_by_field_name("name") if n.type == "aliased_import" else n
                names.append(_t(target))
            if any(c.type == "wildcard_import" for c in node.children):
                names.append("*")
            fp.imports.append(ImportRecord(module=_t(mod), names=names, type_only=type_only))
        elif node.type in ("if_statement", "try_statement"):  # guarded imports
            # `if TYPE_CHECKING:` (or `if t.TYPE_CHECKING:`) bodies are type-hint-only: no runtime import edge.
            consequence = node.child_by_field_name("consequence") if node.type == "if_statement" else None
            cond = node.child_by_field_name("condition") if node.type == "if_statement" else None
            checking = cond is not None and _t(cond).split(".")[-1].strip() == "TYPE_CHECKING"
            for c in node.named_children:
                guarded = type_only or (checking and consequence is not None and c == consequence)
                for cc in (c.named_children if c.type in ("block", "except_clause", "else_clause") else [c]):
                    if cc.type in ("import_statement", "import_from_statement", "block"):
                        if cc.type == "block":
                            for b in cc.named_children:
                                self._imports(b, fp, guarded)
                        else:
                            self._imports(cc, fp, guarded)

    # -- symbols -------------------------------------------------------------
    def _top(self, node: Node, fp: FileParse, parent: str | None) -> None:
        node_u = _unwrap(node)
        if node_u.type == "function_definition":
            fp.symbols.append(self._function(node, node_u, parent))
        elif node_u.type == "class_definition":
            self._class(node, node_u, fp, parent)
        else:
            for name, a in _assign_targets(node):
                qual = f"{parent}.{name}" if parent else name
                text = _t(node)
                fp.symbols.append(SymbolRecord(
                    qualname=qual, name=name, kind="variable", start_line=a.start_point[0] + 1,
                    end_line=a.end_point[0] + 1, signature=text.splitlines()[0][:200] if text else name,
                    body_hash=body_hash(text), body=text[:BODY_MAX], parent=parent,
                ))

    def _function(self, outer: Node, fn: Node, parent: str | None) -> SymbolRecord:
        name = _t(fn.child_by_field_name("name"))
        body = fn.child_by_field_name("body")
        full = _t(outer)
        sig_end = body.start_byte - outer.start_byte if body is not None else len(full)
        signature = " ".join(outer.text[:sig_end].decode("utf-8", "replace").split()).rstrip(":")
        return SymbolRecord(
            qualname=f"{parent}.{name}" if parent else name, name=name, kind="function",
            start_line=outer.start_point[0] + 1, end_line=outer.end_point[0] + 1, signature=signature[:300],
            docstring=_docstring(body), body_hash=body_hash(full), body=full[:BODY_MAX], parent=parent,
            calls=_calls(body) if body is not None else [],
        )

    def _class(self, outer: Node, cls: Node, fp: FileParse, parent: str | None) -> None:
        name = _t(cls.child_by_field_name("name"))
        qual = f"{parent}.{name}" if parent else name
        body = cls.child_by_field_name("body")
        bases: list[str] = []
        sup = cls.child_by_field_name("superclasses")
        if sup is not None:
            for a in sup.named_children:
                if a.type in ("identifier", "attribute"):
                    bases.append(_t(a).rsplit(".", 1)[-1])
        full = _t(outer)
        sig_end = body.start_byte - outer.start_byte if body is not None else len(full)
        is_protocol = any(b in ("Protocol", "ABC") for b in bases)
        fp.symbols.append(SymbolRecord(
            qualname=qual, name=name, kind="interface" if is_protocol else "class",
            start_line=outer.start_point[0] + 1, end_line=outer.end_point[0] + 1,
            signature=" ".join(outer.text[:sig_end].decode("utf-8", "replace").split()).rstrip(":")[:300],
            docstring=_docstring(body), body_hash=body_hash(full), body=full[:BODY_MAX], parent=parent,
            bases=[b for b in bases if b not in ("Protocol", "ABC", "object", "Generic")],
        ))
        if body is not None:
            for member in body.named_children:
                m = _unwrap(member)
                if m.type == "function_definition":
                    fp.symbols.append(self._function(member, m, qual))
                elif m.type == "class_definition":
                    self._class(member, m, fp, qual)
                else:
                    self._top(member, fp, qual)
