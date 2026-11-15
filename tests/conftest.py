"""Shared fixtures: hermetic settings (temp data dir, hash embedder, in-memory Qdrant) and an ingested fixture repo."""
from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_fixture_repo", ROOT / "tests/fixtures/build_fixture_repo.py")
build_fixture_repo = importlib.util.module_from_spec(_spec)
sys.modules["build_fixture_repo"] = build_fixture_repo
_spec.loader.exec_module(build_fixture_repo)


def _settings_env(data_dir: Path) -> dict[str, str]:
    return {
        "AFD_ORACLE_DATA_DIR": str(data_dir),
        "AFD_ORACLE_REPOS_DIR": str(data_dir / "repos-mirrors"),
        "AFD_ORACLE_EMBEDDER": "hash",
        "AFD_ORACLE_EMBED_DIM": "256",
        "AFD_ORACLE_QDRANT_PATH": ":memory:",
        "AFD_ORACLE_NEO4J_URI": "bolt://127.0.0.1:7687",
    }


def reset_runtime() -> None:
    from afd_oracle_core import embedding, runtime
    from afd_oracle_core.config import get_settings

    get_settings.cache_clear()
    runtime.reset()
    embedding.reset_embedder()
    try:
        from afd_oracle_mcp import server
        server._repo_handles.cache_clear()
    except ImportError:
        pass


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("kbdata")
    mp = pytest.MonkeyPatch()
    for k, v in _settings_env(data).items():
        mp.setenv(k, v)
    reset_runtime()
    yield data
    mp.undo()
    reset_runtime()


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> tuple[Path, dict]:
    dest = tmp_path_factory.mktemp("fixture") / "fixture-repo"
    facts = build_fixture_repo.build(dest)
    return dest, facts


@pytest.fixture(scope="session")
def ingested(env, fixture_repo):
    """Full pipeline (no Neo4j): deterministic steps + embeddings + packets. Returns (ctx, facts, result)."""
    from afd_oracle_core import runtime
    from afd_oracle_core.config import get_settings
    from afd_oracle_ingest.embed import embed_all
    from afd_oracle_ingest.packets import build_packets
    from afd_oracle_ingest.pipeline import open_context, run_deterministic

    repo_path, facts = fixture_repo
    s = get_settings()
    ctx = open_context(repo_path, "fixture", s, workers=1, log=lambda *_: None)
    result = run_deterministic(ctx, full=True)
    client, emb = runtime.vectors(s)
    result["qdrant"] = embed_all(ctx.repo, ctx.store, s, client, emb)
    result["packets"] = build_packets(ctx.repo, ctx.store, s)
    return ctx, facts, result


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def neo4j_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 7687), timeout=0.5):
            return True
    except OSError:
        return False


def facts_json(facts: dict) -> str:
    return json.dumps(facts, indent=2)
