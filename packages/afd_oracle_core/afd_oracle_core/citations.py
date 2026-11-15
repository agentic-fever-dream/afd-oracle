"""Deterministic citation gate. Nothing an agent claims is stored unless every citation checks out against Git and
the history store:

* every commit hash resolves (unique prefix) in the store AND exists in the git object database
* every ``{path, at_commit}`` exists in that commit's tree, or appears in that commit's diff (deletions/renames)
* every symbol uid exists in the store
* at least one cited commit actually touched at least one cited file or symbol
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import pygit2

from .store import Store

HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")
ROLES = {"introduced", "reverted", "evidence", "fixed"}


@dataclass
class CitationError:
    field: str
    value: str
    reason: str


def normalize_path(path: str) -> str:
    """Repo-relative POSIX path. Removes a leading './' prefix and leading slashes, never leading dots:
    `.github/...`, `.pre-commit-config.yaml` and `.travis.yml` are real paths. (The earlier `lstrip("./")` stripped
    every leading '.' and '/' character, so dotfile citations always failed.)"""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _path_in_tree(repo: pygit2.Repository, sha: str, path: str) -> bool:
    commit = repo.get(sha)
    if commit is None:
        return False
    try:
        commit.tree[path]
        return True
    except KeyError:
        return False


def validate_citations(citations: dict[str, Any], store: Store, repo: pygit2.Repository) -> tuple[list[dict], dict]:
    """-> (errors, normalized citations with full hashes)."""
    errors: list[CitationError] = []
    norm: dict[str, list] = {"commits": [], "files": [], "symbols": []}

    def resolve(field: str, value: Any) -> str | None:
        v = str(value or "").strip()
        if not HEX.match(v):
            errors.append(CitationError(field, v, "not a 7-40 character hex commit hash"))
            return None
        full = store.resolve_commit(v)
        if full is None:
            errors.append(CitationError(field, v, "commit not found in the ingested history (or prefix ambiguous)"))
            return None
        if repo.get(full) is None:
            errors.append(CitationError(field, v, "commit missing from the git object database"))
            return None
        return full

    commits = citations.get("commits") or []
    if not commits:
        errors.append(CitationError("citations.commits", "", "at least one commit citation is required"))
    for i, c in enumerate(commits):
        c = c if isinstance(c, dict) else {"hash": c}
        role = c.get("role", "evidence")
        if role not in ROLES:
            errors.append(CitationError(f"citations.commits[{i}].role", str(role), f"role must be one of {sorted(ROLES)}"))
        full = resolve(f"citations.commits[{i}].hash", c.get("hash"))
        if full:
            norm["commits"].append({"hash": full, "role": role})

    files = citations.get("files") or []
    if not files:
        errors.append(CitationError("citations.files", "", "at least one file citation is required"))
    for i, f in enumerate(files):
        if not isinstance(f, dict) or not f.get("path"):
            errors.append(CitationError(f"citations.files[{i}]", str(f), "expected {path, at_commit}"))
            continue
        path = normalize_path(str(f["path"]))
        full = resolve(f"citations.files[{i}].at_commit", f.get("at_commit"))
        if not full:
            continue
        if not (_path_in_tree(repo, full, path) or path in store.touched_paths(full)):
            errors.append(CitationError(f"citations.files[{i}].path", path,
                                        f"path does not exist at {full[:10]} and is not in that commit's diff"))
            continue
        norm["files"].append({"path": path, "at_commit": full})

    for i, s in enumerate(citations.get("symbols") or []):
        uid = s.get("uid") if isinstance(s, dict) else s
        if not uid or store.symbol(str(uid)) is None:
            errors.append(CitationError(f"citations.symbols[{i}].uid", str(uid), "unknown symbol uid"))
            continue
        norm["symbols"].append(str(uid))

    if norm["commits"] and (norm["files"] or norm["symbols"]):
        cited_paths = {f["path"] for f in norm["files"]}
        cited_syms = set(norm["symbols"])
        if not any(store.touched_paths(c["hash"]) & cited_paths or store.touched_symbols(c["hash"]) & cited_syms
                   for c in norm["commits"]):
            errors.append(CitationError("citations", "", "none of the cited commits touched any cited file or symbol"))
    return [asdict(e) for e in errors], norm
