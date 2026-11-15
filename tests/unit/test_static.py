"""Schema files, ignore rules, extractors, lifecycle diffing and the Cypher guard (no repo needed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from afd_oracle_core.vectors import PAYLOAD_INDEXES
from afd_oracle_ingest.ast.extractor import FileParse, SymbolRecord, parse_source
from afd_oracle_ingest.ignore import IgnoreRules
from afd_oracle_ingest.lifecycle import detect_moves, file_events
from afd_oracle_mcp.guards import CypherRejected, check_read_only

ROOT = Path(__file__).resolve().parents[2]


def test_neo4j_schema_files_identical():
    pkg = (ROOT / "packages/afd_oracle_core/afd_oracle_core/neo4j_schema.cypher").read_text()
    infra = (ROOT / "infra/neo4j/schema.cypher").read_text()
    assert pkg == infra


def test_collections_json_matches_code():
    doc = json.loads((ROOT / "infra/qdrant/collections.json").read_text())["collections"]
    for name, fields in PAYLOAD_INDEXES.items():
        assert set(doc[name]["payload_indexes"]) == set(fields), name


def test_ignore_rules():
    rules = IgnoreRules.load(ROOT / ".oracleignore")
    assert rules.match("package-lock.json")
    assert rules.match("web/yarn.lock")
    assert rules.match("node_modules/x/index.js")
    assert rules.match("src/__generated__/types.ts")
    assert rules.match("app/static/app.min.js")
    assert not rules.match("api/schema.py")
    assert not rules.match("web/forms.ts")


PY = b'''"""Mod."""
from .base import Base
import os.path as osp

TIMEOUT = 30


class Repo(Base):
    """Stores things."""
    cache = {}

    def get(self, key):
        return self._load(key) or helper(key)

    def _load(self, key):
        return osp.join(key)


def helper(x):
    """Help."""
    return x
'''


def test_python_extractor_golden():
    fp = parse_source("python", PY)
    assert fp.error is None
    got = [(s.kind, s.qualname, s.parent) for s in fp.symbols]
    assert got == [
        ("variable", "TIMEOUT", None),
        ("class", "Repo", None),
        ("variable", "Repo.cache", "Repo"),
        ("function", "Repo.get", "Repo"),
        ("function", "Repo._load", "Repo"),
        ("function", "helper", None),
    ]
    by = {s.qualname: s for s in fp.symbols}
    assert by["Repo"].bases == ["Base"] and by["Repo"].docstring == "Stores things."
    assert by["Repo.get"].calls == ["._load", "helper"]
    assert by["helper"].signature == "def helper(x)" and by["helper"].docstring == "Help."
    assert [(i.module, i.names) for i in fp.imports] == [(".base", ["Base"]), ("os.path", [])]


TS = b'''import { a, b as c } from './util';
/** Entry point. */
export async function main(argv: string[]): Promise<number> { return run(argv) + a(); }
export class Service extends Base implements Runner {
  private n = 0;
  run(): void { this.helper(); }
}
export interface Runner { run(): void }
export const handler = async (e: Event) => new Service().run();
'''


def test_typescript_extractor_golden():
    fp = parse_source("typescript", TS)
    assert fp.error is None
    got = [(s.kind, s.qualname) for s in fp.symbols]
    assert got == [("function", "main"), ("class", "Service"), ("variable", "Service.n"), ("function", "Service.run"),
                   ("interface", "Runner"), ("function", "handler")]
    by = {s.qualname: s for s in fp.symbols}
    assert by["main"].docstring == "Entry point." and by["main"].calls == ["run", "a"]
    assert by["Service"].bases == ["Base"] and by["Service"].implements == ["Runner"]
    assert by["Service.run"].calls == [".helper"]
    assert "Service" in by["handler"].calls
    assert [(i.module, i.names) for i in fp.imports] == [("./util", ["a", "b"])]


def _fp(*recs: tuple[str, str, str]) -> FileParse:
    return FileParse(lang="python", loc=1, symbols=[SymbolRecord(qualname=q, name=q.rsplit(".", 1)[-1], kind=k,
                                                                 start_line=1, end_line=2, body_hash=h)
                                                    for q, k, h in recs])


def test_lifecycle_created_modified_deleted():
    old = _fp(("a", "function", "h1"), ("b", "function", "h2"), ("c", "function", "h3"))
    new = _fp(("a", "function", "h1"), ("b", "function", "h2x"), ("d", "function", "h4"))
    ev = {(e.qualname, e.change) for e in file_events("r", "m.py", "m.py", old, new)}
    assert ev == {("b", "modified"), ("d", "created"), ("c", "deleted")}


def test_lifecycle_rename_and_cross_file_move():
    old = _fp(("f", "function", "h1"))
    ev = file_events("r", "old.py", "new.py", old, _fp(("f", "function", "h1")))
    assert [(e.change, e.old_uid) for e in ev] == [("moved", "sym:r:old.py:f:function")]
    events = file_events("r", "a.py", "a.py", _fp(("g", "function", "hg")), _fp()) + \
        file_events("r", None, "b.py", None, _fp(("g", "function", "hg")))
    moved = detect_moves(events)
    assert [(e.change, e.path, e.old_uid) for e in moved] == [("moved", "b.py", "sym:r:a.py:g:function")]


@pytest.mark.parametrize("q", [
    "MERGE (n:X {a:1})", "MATCH (n) SET n.x = 1", "MATCH (n) DETACH DELETE n", "CREATE (n)",
    "MATCH (n) REMOVE n:Foo", "CALL apoc.create.node(['X'], {})", "LOAD CSV FROM 'x' AS r RETURN r",
    "CALL apoc.periodic.iterate('a','b',{})", "CALL dbms.security.createUser('x','y')",
    "MATCH (n) CALL { WITH n DELETE n } IN TRANSACTIONS", "DROP CONSTRAINT x", "",
])
def test_guard_rejects_writes(q):
    with pytest.raises(CypherRejected):
        check_read_only(q)


@pytest.mark.parametrize("q", [
    "MATCH (n:File) RETURN n.path LIMIT 5",
    "MATCH (c:Commit) WHERE c.message CONTAINS 'CREATE TABLE' RETURN c.hash",
    "MATCH (n) WHERE n.set = 1 OR n.delete IS NULL RETURN n",
    "// MERGE in a comment\nMATCH (a)-[r:CO_COMMITTED]-(b) RETURN a, r, b",
    "CALL db.index.fulltext.queryNodes('commit_msg', 'revert') YIELD node RETURN node",
])
def test_guard_allows_reads(q):
    check_read_only(q)


def test_length_buckets_bound_padded_attention_cost():
    from afd_oracle_core.embedding import length_buckets
    texts = ["x" * n for n in (10, 6000, 40, 3000, 5, 6000, 200)] + ["y" * 50] * 100
    groups = length_buckets(texts, max_batch=64)
    assert sorted(i for g in groups for i in g) == list(range(len(texts)))  # every text exactly once
    for g in groups:
        longest = max(len(texts[i]) for i in g) / 2.5
        assert len(g) <= 64 and (len(g) == 1 or len(g) * longest ** 2 <= 64 * 512 ** 2)
