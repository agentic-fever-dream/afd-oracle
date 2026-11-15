"""TypeScript / TSX / JavaScript symbol extraction with tree-sitter."""
from __future__ import annotations

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from ..extractor import BODY_MAX, FileParse, ImportRecord, SymbolRecord, body_hash

_LANGS = {
    "typescript": lambda: Language(tsts.language_typescript()),
    "tsx": lambda: Language(tsts.language_tsx()),
    "javascript": lambda: Language(tsjs.language()),
}

FUNC_DECL = {"function_declaration", "generator_function_declaration", "function_signature"}
CLASS_DECL = {"class_declaration", "abstract_class_declaration", "class"}
FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
TYPE_NAMES = {"identifier", "type_identifier", "member_expression", "nested_type_identifier", "generic_type"}


def _t(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


def _type_name(node: Node) -> str:
    if node.type == "generic_type":
        n = node.child_by_field_name("name")
        return _type_name(n) if n is not None else _t(node)
    return _t(node).rsplit(".", 1)[-1].split("<", 1)[0]


def _doc(outer: Node) -> str:
    prev = outer.prev_named_sibling
    if prev is not None and prev.type == "comment" and prev.end_point[0] >= outer.start_point[0] - 1:
        txt = _t(prev)
        if txt.startswith("/**"):
            lines = [ln.strip().lstrip("*").strip() for ln in txt[3:-2].splitlines()]
            return " ".join(ln for ln in lines if ln)[:1000]
    return ""


def _calls(node: Node | None) -> list[str]:
    if node is None:
        return []
    out: dict[str, None] = {}
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None:
                if fn.type == "identifier":
                    out.setdefault(_t(fn), None)
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None:
                        out.setdefault("." + _t(prop), None)
        elif n.type == "new_expression":
            c = n.child_by_field_name("constructor")
            if c is not None and c.type == "identifier":
                out.setdefault(_t(c), None)
        stack.extend(reversed(n.children))
    return list(out)


def _signature(outer: Node, body: Node | None) -> str:
    raw = outer.text[: body.start_byte - outer.start_byte] if body is not None else outer.text
    return " ".join(raw.decode("utf-8", "replace").split()).rstrip("{ =>")[:300]


class TSExtractor:
    def __init__(self, lang: str):
        self.lang = lang
        self.parser = Parser(_LANGS[lang]())

    def parse(self, source: bytes) -> FileParse:
        tree = self.parser.parse(source)
        fp = FileParse(lang=self.lang, loc=source.count(b"\n") + (0 if source.endswith(b"\n") else 1))
        for child in tree.root_node.named_children:
            self._stmt(child, child, fp)
        return fp

    def _stmt(self, node: Node, outer: Node, fp: FileParse) -> None:
        t = node.type
        if t == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is None:
                decl = next((c for c in node.named_children if c.type not in ("comment", "string")), None)
            if decl is not None:
                self._stmt(decl, node, fp)
            src = node.child_by_field_name("source")
            if src is not None:  # export { x } from './y'
                fp.imports.append(ImportRecord(module=_t(src).strip("'\"`"), names=[]))
        elif t == "import_statement":
            self._import(node, fp)
        elif t in FUNC_DECL:
            fp.symbols.append(self._func(node, outer, _t(node.child_by_field_name("name")), None))
        elif t in CLASS_DECL:
            self._class(node, outer, fp)
        elif t == "interface_declaration":
            self._interface(node, outer, fp)
        elif t in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type != "variable_declarator":
                    continue
                name_node = d.child_by_field_name("name")
                if name_node is None or name_node.type != "identifier":
                    continue
                value = d.child_by_field_name("value")
                name = _t(name_node)
                if value is not None and value.type in FUNC_VALUES:
                    rec = self._func(value, outer, name, None)
                    rec.signature = _signature(outer, value.child_by_field_name("body"))
                    fp.symbols.append(rec)
                elif value is not None and value.type == "class":
                    self._class(value, outer, fp, name=name)
                else:
                    text = _t(outer)
                    fp.symbols.append(SymbolRecord(
                        qualname=name, name=name, kind="variable", start_line=outer.start_point[0] + 1,
                        end_line=outer.end_point[0] + 1, signature=text.splitlines()[0][:200] if text else name,
                        docstring=_doc(outer), body_hash=body_hash(text), body=text[:BODY_MAX],
                        calls=_calls(value),
                    ))

    def _import(self, node: Node, fp: FileParse) -> None:
        src = node.child_by_field_name("source")
        names: list[str] = []
        for c in node.named_children:
            if c.type != "import_clause":
                continue
            for cc in c.named_children:
                if cc.type == "identifier":
                    names.append("default")
                elif cc.type == "namespace_import":
                    names.append("*")
                elif cc.type == "named_imports":
                    for spec in cc.named_children:
                        if spec.type == "import_specifier":
                            n = spec.child_by_field_name("name")
                            if n is not None:
                                names.append(_t(n))
        type_only = any(c.type == "type" or _t(c) == "type" for c in node.children[1:2])  # `import type {...}`
        fp.imports.append(ImportRecord(module=_t(src).strip("'\"`"), names=names, type_only=type_only))

    def _func(self, fn: Node, outer: Node, name: str, parent: str | None, kind: str = "function") -> SymbolRecord:
        body = fn.child_by_field_name("body")
        text = _t(outer)
        return SymbolRecord(
            qualname=f"{parent}.{name}" if parent else name, name=name, kind=kind,
            start_line=outer.start_point[0] + 1, end_line=outer.end_point[0] + 1,
            signature=_signature(outer, body), docstring=_doc(outer), body_hash=body_hash(text),
            body=text[:BODY_MAX], parent=parent, calls=_calls(body),
        )

    def _class(self, cls: Node, outer: Node, fp: FileParse, name: str | None = None) -> None:
        name = name or _t(cls.child_by_field_name("name"))
        if not name:
            return
        body = cls.child_by_field_name("body")
        bases: list[str] = []
        impls: list[str] = []
        for c in cls.named_children:
            if c.type != "class_heritage":
                continue
            clauses = [cc for cc in c.named_children]
            if not any(cc.type in ("extends_clause", "implements_clause") for cc in clauses):
                bases += [_type_name(cc) for cc in clauses if cc.type in TYPE_NAMES]  # JS grammar
            for cc in clauses:
                if cc.type == "extends_clause":
                    bases += [_type_name(v) for v in cc.named_children if v.type in TYPE_NAMES]
                elif cc.type == "implements_clause":
                    impls += [_type_name(v) for v in cc.named_children if v.type in TYPE_NAMES]
        text = _t(outer)
        fp.symbols.append(SymbolRecord(
            qualname=name, name=name, kind="class", start_line=outer.start_point[0] + 1,
            end_line=outer.end_point[0] + 1, signature=_signature(outer, body), docstring=_doc(outer),
            body_hash=body_hash(text), body=text[:BODY_MAX], bases=bases, implements=impls,
        ))
        if body is None:
            return
        for m in body.named_children:
            if m.type in ("method_definition", "method_signature", "abstract_method_signature"):
                mname = _t(m.child_by_field_name("name"))
                if mname:
                    fp.symbols.append(self._func(m, m, mname, name))
            elif m.type in ("public_field_definition", "field_definition"):
                n = m.child_by_field_name("name") or m.child_by_field_name("property")
                value = m.child_by_field_name("value")
                fname = _t(n)
                if not fname:
                    continue
                if value is not None and value.type in FUNC_VALUES:
                    fp.symbols.append(self._func(value, m, fname, name))
                else:
                    mt = _t(m)
                    fp.symbols.append(SymbolRecord(
                        qualname=f"{name}.{fname}", name=fname, kind="variable", start_line=m.start_point[0] + 1,
                        end_line=m.end_point[0] + 1, signature=mt[:200], body_hash=body_hash(mt), body=mt[:BODY_MAX],
                        parent=name,
                    ))

    def _interface(self, node: Node, outer: Node, fp: FileParse) -> None:
        name = _t(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        bases = []
        for c in node.named_children:
            if c.type in ("extends_type_clause", "extends_clause"):
                bases += [_type_name(v) for v in c.named_children if v.type in TYPE_NAMES]
        text = _t(outer)
        fp.symbols.append(SymbolRecord(
            qualname=name, name=name, kind="interface", start_line=outer.start_point[0] + 1,
            end_line=outer.end_point[0] + 1, signature=_signature(outer, body), docstring=_doc(outer),
            body_hash=body_hash(text), body=text[:BODY_MAX], bases=bases,
        ))
