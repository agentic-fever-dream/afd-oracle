"""Lazily created, process-wide Neo4j driver and Qdrant client + embedder."""
from __future__ import annotations

import atexit
import time

import neo4j
from qdrant_client import QdrantClient

from .config import Settings, get_settings
from .embedding import Embedder, get_embedder
from .graph import get_driver
from .vectors import get_client

_driver: neo4j.Driver | None = None
_qdrant: QdrantClient | None = None
_failed_at: float = 0.0
RETRY_AFTER_S = 30.0


def graph_driver(settings: Settings | None = None, required: bool = False) -> neo4j.Driver | None:
    """Connected driver, or None when Neo4j is unreachable (raises instead when ``required``)."""
    global _driver, _failed_at
    if _driver is not None:
        return _driver
    if not required and _failed_at and time.monotonic() - _failed_at < RETRY_AFTER_S:
        return None  # don't pay a connection timeout on every tool call while Neo4j is down
    s = settings or get_settings()
    drv = get_driver(s)
    try:
        drv.verify_connectivity()
    except Exception as e:
        drv.close()
        _failed_at = time.monotonic()
        if required:
            raise ConnectionError(f"Neo4j unreachable at {s.neo4j_uri}: {e}. "
                                  "Start it with `docker compose -f infra/docker-compose.yml up -d`.") from e
        return None
    _driver = drv
    return _driver


def qdrant(settings: Settings | None = None) -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = get_client(settings or get_settings())
    return _qdrant


def vectors(settings: Settings | None = None) -> tuple[QdrantClient, Embedder]:
    s = settings or get_settings()
    return qdrant(s), get_embedder(s)


def reset() -> None:
    global _driver, _qdrant, _failed_at
    if _driver is not None:
        _driver.close()
    if _qdrant is not None:
        try:
            _qdrant.close()
        except Exception:
            pass
    _driver = None
    _qdrant = None
    _failed_at = 0.0


atexit.register(reset)  # close embedded Qdrant / driver before interpreter teardown
