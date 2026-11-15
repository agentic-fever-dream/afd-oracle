"""Extractor protocol, records and language registry.

Extractors walk tree-sitter syntax trees directly (node types + fields) instead of ``.scm`` query files: the
py-tree-sitter query API has changed across 0.2x releases, while node/field traversal has stayed stable.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

EXTRACTOR_VERSION = "4"  # bump to invalidate the parse cache when extraction logic changes
BODY_MAX = 6000


@dataclass
class SymbolRecord:
    qualname: str
    name: str
    kind: str  # class | interface | function | variable
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str = ""
    body_hash: str = ""
    body: str = ""
    parent: str | None = None  # qualname of enclosing class/interface
    calls: list[str] = field(default_factory=list)  # "name" or ".attr" (attribute call)
    bases: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)


@dataclass
class ImportRecord:
    module: str  # python dotted ("a.b", ".x", "..") or JS specifier ("./foo")
    names: list[str] = field(default_factory=list)
    type_only: bool = False  # `if TYPE_CHECKING:` (Python) or `import type` (TS): no runtime dependency


@dataclass
class FileParse:
    lang: str
    loc: int
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FileParse":
        return cls(lang=d["lang"], loc=d["loc"], error=d.get("error"),
                   symbols=[SymbolRecord(**s) for s in d.get("symbols", [])],
                   imports=[ImportRecord(**i) for i in d.get("imports", [])])


def body_hash(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode()).hexdigest()


class Extractor(Protocol):
    lang: str

    def parse(self, source: bytes) -> FileParse: ...


_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
}


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    return _EXT.get(path[dot:].lower()) if dot != -1 else None


_FACTORIES: dict[str, Callable[[], Extractor]] = {}
_INSTANCES: dict[str, Extractor] = {}


def register(lang: str, factory: Callable[[], Extractor]) -> None:
    _FACTORIES[lang] = factory


def get_extractor(lang: str) -> Extractor:
    if lang not in _INSTANCES:
        if not _FACTORIES:
            from .languages import load_all
            load_all()
        _INSTANCES[lang] = _FACTORIES[lang]()
    return _INSTANCES[lang]


def parse_source(lang: str, source: bytes) -> FileParse:
    try:
        return get_extractor(lang).parse(source)
    except Exception as e:  # never let one bad file kill an ingest
        return FileParse(lang=lang, loc=source.count(b"\n") + 1, error=f"{type(e).__name__}: {e}")
