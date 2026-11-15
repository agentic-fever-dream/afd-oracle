"""Neo4j driver helpers: connection, schema application, batched writes, JSON-safe result conversion."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable

import neo4j
from neo4j import GraphDatabase
from neo4j.graph import Node, Path as GPath, Relationship

from .config import Settings, get_settings

SCHEMA_FILE = Path(__file__).with_name("neo4j_schema.cypher")


def get_driver(settings: Settings | None = None) -> neo4j.Driver:
    s = settings or get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))


def schema_statements() -> list[str]:
    text = SCHEMA_FILE.read_text()
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def apply_schema(driver: neo4j.Driver, database: str) -> int:
    stmts = schema_statements()
    with driver.session(database=database) as s:
        for stmt in stmts:
            s.run(stmt).consume()
    return len(stmts)


def write_batches(session: neo4j.Session, query: str, rows: list[dict], batch: int = 2000, **params: Any) -> int:
    for i in range(0, len(rows), batch):
        session.execute_write(lambda tx, chunk: tx.run(query, rows=chunk, **params).consume(), rows[i:i + batch])
    return len(rows)


def to_jsonable(v: Any) -> Any:
    if isinstance(v, Node):
        return {"_labels": sorted(v.labels), **{k: to_jsonable(x) for k, x in v.items()}}
    if isinstance(v, Relationship):
        return {"_type": v.type, **{k: to_jsonable(x) for k, x in v.items()}}
    if isinstance(v, GPath):
        return {"nodes": [to_jsonable(n) for n in v.nodes], "relationships": [to_jsonable(r) for r in v.relationships]}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if hasattr(v, "iso_format"):  # neo4j.time types
        return v.iso_format()
    return v


def read_query(driver: neo4j.Driver, database: str, query: str, params: dict | None, limit: int,
               timeout_s: float) -> tuple[list[str], list[list[Any]], bool]:
    """Run a query in a READ transaction; return (columns, rows, truncated)."""

    @neo4j.unit_of_work(timeout=timeout_s)
    def work(tx: neo4j.ManagedTransaction):
        result = tx.run(query, params or {})
        cols = list(result.keys())
        rows: list[list[Any]] = []
        truncated = False
        for rec in result:
            if len(rows) >= limit:
                truncated = True
                break
            rows.append([to_jsonable(rec[c]) for c in cols])
        result.consume()
        return cols, rows, truncated

    with driver.session(database=database, default_access_mode=neo4j.READ_ACCESS) as s:
        return s.execute_read(work)


def ping(driver: neo4j.Driver) -> None:
    driver.verify_connectivity()


def chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
