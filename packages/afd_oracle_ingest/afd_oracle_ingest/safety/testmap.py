"""Which tests exercise which code.

Sources (each row: test, target, source, weight):
  static    a test function calls the target symbol (resolved CALLS edges from test files)
  import    a test file imports the target file
  name      tests/test_foo.py <-> foo.py, foo.test.ts <-> foo.ts
  history   the test file and the target file change together (main-line co-change, Jaccard as weight)
  coverage  per-test coverage imported from coverage.py dynamic contexts (`afd-oracle coverage`)
Targets are symbol uids or file paths. Test ids are pytest node ids (path::Class::test) or test file paths.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from afd_oracle_core.store import Store

from .source import SourceFile, is_test_path


def stem(path: str) -> str:
    base = PurePosixPath(path).name.lower()
    for suf in (".test.tsx", ".test.ts", ".test.js", ".spec.tsx", ".spec.ts", ".spec.js"):
        if base.endswith(suf):
            return base[: -len(suf)]
    base = base.rsplit(".", 1)[0]
    for affix in ("test_", "tests_"):
        if base.startswith(affix):
            base = base[len(affix):]
    for affix in ("_test", "_tests"):
        if base.endswith(affix):
            base = base[: -len(affix)]
    return base


def test_id(path: str, qualname: str, lang: str | None) -> str:
    if lang == "python":
        return f"{path}::{qualname.replace('.', '::')}"
    return path


def _is_test_symbol(name: str, qualname: str, lang: str | None) -> bool:
    if lang == "python":
        return name.startswith("test") or qualname.split(".")[0].startswith("Test")
    return True


def build(store: Store, sources: dict[str, SourceFile], repo_id: str) -> list[tuple[str, str, str, float]]:
    rows: dict[tuple[str, str, str], float] = {}
    prefix = f"sym:{repo_id}:"
    test_uids: dict[str, str] = {}
    for path, src in sources.items():
        if not is_test_path(path) or not src.parse:
            continue
        for s in src.symbols():
            if s.kind == "function" and _is_test_symbol(s.name, s.qualname, src.lang):
                test_uids[f"{prefix}{path}:{s.qualname}:{s.kind}"] = test_id(path, s.qualname, src.lang)
    # static: calls from test functions, and from helpers/fixtures in test files (attributed to the file)
    for e in store.q("SELECT src, dst FROM edges WHERE kind='CALLS'"):
        src_uid, dst = e["src"], e["dst"]
        if not src_uid.startswith(prefix):
            continue
        src_path = src_uid[len(prefix):].rsplit(":", 2)[0]
        if not is_test_path(src_path):
            continue
        dst_path = dst[len(prefix):].rsplit(":", 2)[0] if dst.startswith(prefix) else ""
        if dst_path and is_test_path(dst_path):
            continue
        rows[(test_uids.get(src_uid, src_path), dst, "static")] = 1.0
    for e in store.q("SELECT src, dst FROM edges WHERE kind='IMPORTS'"):
        if is_test_path(e["src"]) and not is_test_path(e["dst"]):
            rows[(e["src"], e["dst"], "import")] = 0.5
    # naming convention
    by_stem: dict[str, list[str]] = {}
    for path, src in sources.items():
        if src.lang and not is_test_path(path) and stem(path) not in ("__init__", "index", "main", "conftest"):
            by_stem.setdefault(stem(path), []).append(path)
    for path, src in sources.items():
        if src.lang and is_test_path(path):
            for target in by_stem.get(stem(path), []):
                rows[(path, target, "name")] = 0.6
    # history
    for r in store.q("SELECT a, b, jaccard FROM cochange"):
        a, b = r["a"], r["b"]
        if is_test_path(a) != is_test_path(b):
            t, target = (a, b) if is_test_path(a) else (b, a)
            if t in sources:
                rows[(t, target, "history")] = round(float(r["jaccard"]), 4)
    return [(t, target, src, w) for (t, target, src), w in rows.items()]


def import_coverage(store: Store, sources: dict[str, SourceFile], repo_id: str, data_file: str,
                    root: str) -> int:
    """Per-test coverage from a coverage.py data file recorded with dynamic contexts (`--cov-context=test`)."""
    import os

    from coverage import CoverageData  # optional dependency

    data = CoverageData(basename=data_file)
    data.read()
    rows = []
    root_n = os.path.normcase(os.path.abspath(root))
    for measured in data.measured_files():
        rel = os.path.relpath(os.path.normcase(measured), root_n).replace("\\", "/")
        src = sources.get(rel)
        if src is None or is_test_path(rel):
            continue
        for line, contexts in (data.contexts_by_lineno(measured) or {}).items():
            uid = src.uid_at(repo_id, line)
            for ctx in contexts:
                if not ctx:
                    continue
                test = re.sub(r"\|(run|setup|teardown)$", "", ctx)
                rows.append((test, uid or rel, "coverage", 1.0))
    with store.tx() as conn:
        conn.execute("DELETE FROM test_map WHERE source='coverage'")
        conn.executemany("INSERT OR REPLACE INTO test_map VALUES (?,?,?,?)", list(dict.fromkeys(rows)))
    return len(set(rows))
