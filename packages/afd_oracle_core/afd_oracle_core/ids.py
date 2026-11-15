"""Stable identifiers. ``uid`` strings are the join key between Neo4j, Qdrant (``neo4j_id``) and the store."""
from __future__ import annotations

import hashlib
import uuid

_NS = uuid.UUID("6f1c3c1e-6f8f-4c59-9a55-2b0f6f0c9a11")


def repo_uid(repo: str) -> str:
    return f"repo:{repo}"


def dir_uid(repo: str, path: str) -> str:
    return f"dir:{repo}:{path}"


def file_uid(repo: str, path: str) -> str:
    return f"file:{repo}:{path}"


def symbol_uid(repo: str, path: str, qualname: str, kind: str) -> str:
    return f"sym:{repo}:{path}:{qualname}:{kind}"


def commit_uid(repo: str, sha: str) -> str:
    """Repo-scoped: forks/mirrors ingested under different ids must not share (and overwrite) Commit nodes."""
    return f"commit:{repo}:{sha}"


def split_commit_uid(uid: str) -> tuple[str, str]:
    """'commit:<repo>:<sha>' -> (repo, sha)."""
    body = uid[len("commit:"):]
    repo, _, sha = body.rpartition(":")
    return repo, sha


def author_uid(email: str) -> str:
    return f"author:{(email or '').strip().lower()}"


def branch_uid(repo: str, ref: str) -> str:
    return f"branch:{repo}:{ref}"


def new_insight_uid() -> str:
    return f"insight:{uuid.uuid4()}"


def point_id(uid: str) -> str:
    """Deterministic Qdrant point id for a uid (UUIDv5), so re-ingestion is idempotent."""
    return str(uuid.uuid5(_NS, uid))


def short_hash(*parts: str, n: int = 12) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:n]


def path_prefixes(path: str) -> list[str]:
    """``a/b/c.py`` -> ``['a', 'a/b', 'a/b/c.py']`` (lets Qdrant do prefix filters with keyword match)."""
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]
