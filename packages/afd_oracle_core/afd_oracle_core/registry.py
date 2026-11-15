"""Tiny JSON registry mapping repo ids to their on-disk git directories."""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings, get_settings
from .store import Store


def _load(settings: Settings) -> dict[str, dict]:
    p = settings.registry_path
    return json.loads(p.read_text()) if p.exists() else {}


def register(repo_id: str, git_path: Path, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    data = _load(settings)
    data[repo_id] = {"path": str(Path(git_path).resolve())}
    settings.registry_path.parent.mkdir(parents=True, exist_ok=True)
    settings.registry_path.write_text(json.dumps(data, indent=2))


def list_repos(settings: Settings | None = None) -> dict[str, dict]:
    return _load(settings or get_settings())


def resolve_repo(repo_id: str | None, settings: Settings | None = None) -> tuple[str, Path]:
    """Return (repo_id, git_path). With repo_id=None the single registered repo is used."""
    settings = settings or get_settings()
    data = _load(settings)
    if not data:
        raise LookupError("No repositories ingested yet. Run `afd-oracle ingest <path>` first.")
    if repo_id is None:
        if len(data) > 1:
            raise LookupError(f"Several repositories are ingested ({', '.join(sorted(data))}); pass repo.")
        repo_id = next(iter(data))
    if repo_id not in data:
        raise LookupError(f"Unknown repo '{repo_id}'. Known: {', '.join(sorted(data))}")
    return repo_id, Path(data[repo_id]["path"])


def open_store(repo_id: str, settings: Settings | None = None) -> Store:
    settings = settings or get_settings()
    return Store(settings.store_path(repo_id), repo_id)
