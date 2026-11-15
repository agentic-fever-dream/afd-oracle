"""Source files at a revision (text + parse) and line -> enclosing-symbol lookup."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import pygit2

from afd_oracle_core.ids import symbol_uid

from ..ast.extractor import FileParse, SymbolRecord, language_for, parse_source
from ..ast.parse_cache import ParseCache
from ..git.walker import blob_bytes, tree_files
from ..ignore import IgnoreRules

TEXT_EXT = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json", ".yml", ".yaml",
            ".toml", ".ini", ".cfg", ".env", ".sql", ".html", ".jinja", ".j2", ".sh", ".txt", ".md", ".rst",
            ".dockerfile", ".conf"}
TEXT_NAMES = {"Dockerfile", "Makefile", "Procfile", ".env", ".env.example", ".env.sample"}
MAX_BYTES = 600_000


def is_text_path(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in TEXT_EXT or p.name in TEXT_NAMES or p.name.startswith(".env")


def is_test_path(path: str) -> bool:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return (p.startswith(("test/", "tests/")) or "/tests/" in p or "/test/" in p or "/__tests__/" in p
            or name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js",
                                                           ".spec.ts", ".spec.tsx", ".spec.js")) or name == "conftest.py")


@dataclass
class SourceFile:
    path: str
    text: str
    lang: str | None  # python | typescript | tsx | javascript | None (config/docs/sql…)
    parse: FileParse | None = None
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lines = self.text.splitlines()
        self._doc: set[int] | None = None

    def docstring_lines(self) -> set[int]:
        """1-based lines inside Python triple-quoted strings (docstrings, examples): not executable code."""
        if self._doc is None:
            self._doc = set()
            if self.lang == "python":
                inside, is_doc = None, False
                for i, line in enumerate(self.lines, 1):
                    rest = line
                    if inside:
                        if is_doc:
                            self._doc.add(i)
                        if inside not in rest:
                            continue
                        rest = rest.split(inside, 1)[1]
                        inside = None
                    for q in ('"""', "'''"):
                        while q in rest:
                            before, _, after = rest.partition(q)
                            # a docstring starts a statement; `x = """…`, `execute(f"""…` are data (SQL, templates)
                            statement = before.strip().lower() in ("", "r", "u", "b", "f", "rb", "br", "fr", "rf")
                            if q in after:
                                rest = after.split(q, 1)[1]
                                if statement:
                                    self._doc.add(i)
                            else:
                                inside, is_doc = q, statement
                                if statement:
                                    self._doc.add(i)
                                rest = ""
                                break
        return self._doc

    @property
    def is_doc_file(self) -> bool:
        return self.path.lower().endswith((".md", ".rst", ".txt"))

    def symbols(self) -> list[SymbolRecord]:
        return self.parse.symbols if self.parse else []

    def enclosing(self, line: int) -> SymbolRecord | None:
        """Innermost symbol whose range contains `line` (1-based)."""
        best = None
        for s in self.symbols():
            if s.kind == "variable":
                continue
            if s.start_line <= line <= s.end_line and (best is None or
                                                         s.end_line - s.start_line < best.end_line - best.start_line):
                best = s
        return best

    def uid_at(self, repo_id: str, line: int) -> str | None:
        s = self.enclosing(line)
        return symbol_uid(repo_id, self.path, s.qualname, s.kind) if s else None


def from_text(path: str, text: str) -> SourceFile:
    lang = language_for(path)
    fp = parse_source(lang, text.encode("utf-8", "replace")) if lang else None
    return SourceFile(path, text, lang, fp)


def _decode(data: bytes | None) -> str | None:
    if data is None or len(data) > MAX_BYTES or b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def sources_at(repo: pygit2.Repository, sha: str, cache: ParseCache, ignore: IgnoreRules,
               workers: int | None = None) -> dict[str, SourceFile]:
    """Every text file in the tree at `sha` (not ignored), code files parsed via the blob-keyed cache."""
    files = {p: o for p, o in tree_files(repo, sha).items() if not ignore.match(p) and is_text_path(p)}
    jobs: dict[tuple[str, str], bytes] = {}
    raw: dict[str, str] = {}
    for path, oid in files.items():
        data = blob_bytes(repo, oid)
        text = _decode(data)
        if text is None:
            continue
        raw[path] = text
        lang = language_for(path)
        if lang:
            jobs[(oid, lang)] = data  # type: ignore[assignment]
    cache.ensure(jobs, workers=workers)
    out = {}
    for path, text in raw.items():
        lang = language_for(path)
        fp = cache.get(files[path], lang) if lang else None
        out[path] = SourceFile(path, text, lang, fp)
    return out
