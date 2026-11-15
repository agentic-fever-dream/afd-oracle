"""Runtime configuration. Values come from env vars prefixed ``AFD_ORACLE_`` or from ``.env`` / ``infra/.env``."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AFD_ORACLE_", env_file=(".env", "infra/.env"), extra="ignore")

    # --- Neo4j -------------------------------------------------------------
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me-please"
    neo4j_database: str = "neo4j"
    cypher_timeout_s: float = 30.0

    # --- Qdrant ------------------------------------------------------------
    qdrant_url: str | None = "http://127.0.0.1:6333"
    qdrant_api_key: str | None = None
    qdrant_path: str | None = None  # embedded/local mode (no server) when set; ":memory:" for tests

    # --- Embeddings --------------------------------------------------------
    embedder: str = "fastembed"  # "fastembed" | "hash" (deterministic, offline; tests)
    embed_model: str = "jinaai/jina-embeddings-v2-base-code"
    embed_dim: int = 768
    sparse_model: str = "Qdrant/bm25"
    embed_batch: int = 64

    # --- Paths -------------------------------------------------------------
    data_dir: Path = Path("data")
    repos_dir: Path = Path("repos")

    # --- Ingestion thresholds ----------------------------------------------
    bulk_files: int = 50
    cochange_max_files: int = 30
    cochange_min_count: int = 3
    cochange_min_jaccard: float = 0.15
    revert_window_days: int = 180
    revert_max_files: int = 20
    revert_partial_overlap: float = 0.6
    abandoned_days: int = 90
    short_lived_days: int = 30
    short_lived_commits: int = 10
    churned_deleted_min: int = 5
    packet_max_tokens: int = 6000
    # Drop candidates a program can recognise as noise (docs-only/trivial/version/dependency-bump changes, tests
    # changing with their subject, changelog lockstep, moved symbols, test code). Counts are reported per reason.
    packet_noise_filter: bool = True
    # change-safety layer (afd_oracle_ingest.safety)
    safety: bool = True
    safety_typed_calls: bool = True  # Jedi-resolved calls (Python); bounded by the budget below
    safety_typed_calls_budget_s: float = 180.0
    safety_api_tags: int = 6  # recent release tags whose public API is recorded
    safety_max_fix_commits: int = 4000
    # candidate catalogue (afd_oracle_ingest.catalog): ~190 extra deterministic candidate kinds
    catalog: bool = True
    catalog_max_per_kind: int = 40
    enrich_blame_budget_s: float = 300.0  # git blame of bug-fix lines per run (resumes on the next run)
    # Docker sandbox for running a repository's own tests (coverage, mutation); never on the host
    sandbox_memory: str = "3g"
    sandbox_cpus: float = 2.0
    sandbox_install_timeout_s: float = 1200.0
    sandbox_test_timeout_s: float = 1800.0
    sandbox_mutant_timeout_s: float = 300.0

    @property
    def parse_cache_path(self) -> Path:
        return self.data_dir / "parse_cache.sqlite"

    def store_path(self, repo_id: str) -> Path:
        return self.data_dir / "repos" / repo_id / "store.sqlite"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
