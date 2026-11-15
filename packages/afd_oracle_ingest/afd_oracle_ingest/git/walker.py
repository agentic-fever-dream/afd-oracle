"""pygit2 commit-DAG walking and per-commit diff extraction (first-parent diffs, rename detection)."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pygit2
from pygit2.enums import DiffFind, SortMode

ZERO = "0" * 40
REF_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/tags/", "refs/pull/", "refs/merge-requests/")


@dataclass
class FileChange:
    path: str
    old_path: str | None
    status: str  # A M D R
    added: int
    deleted: int
    old_blob: str | None
    new_blob: str | None
    binary: bool = False


@dataclass
class CommitRecord:
    hash: str
    parents: list[str]
    author_name: str
    author_email: str
    ts: int
    message: str
    changes: list[FileChange] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def open_repo(path: str | Path) -> pygit2.Repository:
    found = pygit2.discover_repository(str(path))
    if not found:
        raise FileNotFoundError(f"No git repository at {path}")
    repo = pygit2.Repository(found)
    # Windows: history can contain paths longer than MAX_PATH (e.g. a file named after a pasted directory tree);
    # libgit2 refuses them unless core.longpaths is on. Set it in-process only.
    try:
        repo.config["core.longpaths"] = True
    except Exception:  # read-only config: the per-commit fallback in extract_commit still applies
        pass
    return repo


def mirror(url: str, dest: Path, fetch_pulls: bool = False) -> Path:
    """Bare mirror clone (or update). Optionally fetch GitHub PR / GitLab MR refs."""
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "remote", "update", "--prune"], check=True, stdin=subprocess.DEVNULL)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--mirror", url, str(dest)], check=True, stdin=subprocess.DEVNULL)
    if fetch_pulls:
        for spec in ("+refs/pull/*/head:refs/pull/*/head", "+refs/merge-requests/*/head:refs/merge-requests/*/head"):
            subprocess.run(["git", "-C", str(dest), "fetch", "origin", spec], check=False, stdin=subprocess.DEVNULL)
    return dest


def _peel_commit(repo: pygit2.Repository, ref: pygit2.Reference) -> pygit2.Commit | None:
    try:
        obj = ref.peel(pygit2.Commit)
        return obj if isinstance(obj, pygit2.Commit) else None
    except (pygit2.GitError, ValueError, KeyError):
        return None


def ref_tips(repo: pygit2.Repository) -> dict[str, str]:
    """All interesting refs -> commit hex (tags peeled; symbolic */HEAD refs skipped)."""
    tips: dict[str, str] = {}
    for name in repo.references:
        if not name.startswith(REF_PREFIXES) or name.endswith("/HEAD"):
            continue
        if name.startswith(("refs/pull/", "refs/merge-requests/")) and name.endswith("/merge"):
            continue  # forge-synthesised test merges, not real history
        c = _peel_commit(repo, repo.references[name])
        if c is not None:
            tips[name] = str(c.id)
    return tips


def default_branch(repo: pygit2.Repository) -> tuple[str, str]:
    """(refname, commit hex) of the default branch."""
    try:
        if not repo.head_is_unborn:
            ref = repo.references["HEAD"].resolve()
            return ref.name, str(ref.peel(pygit2.Commit).id)
    except (pygit2.GitError, KeyError):
        pass
    for cand in ("refs/heads/main", "refs/heads/master", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        if cand in repo.references:
            return cand, str(repo.references[cand].peel(pygit2.Commit).id)
    heads = sorted(n for n in repo.references if n.startswith("refs/heads/"))
    if not heads:
        raise LookupError("Repository has no branches")
    return heads[0], str(repo.references[heads[0]].peel(pygit2.Commit).id)


def iter_new_commits(repo: pygit2.Repository, known: set[str], hide_tips: list[str] | None = None) -> list[pygit2.Commit]:
    """Commits reachable from any ref and not yet known, parents before children (topological, reverse)."""
    tips = sorted(set(ref_tips(repo).values()))
    if not tips:
        return []
    walker = repo.walk(tips[0], SortMode.TOPOLOGICAL | SortMode.REVERSE)
    for t in tips[1:]:
        walker.push(t)
    for h in hide_tips or []:
        try:
            walker.hide(h)
        except (KeyError, ValueError, pygit2.GitError):
            pass
    return [c for c in walker if str(c.id) not in known]


def commit_diff(repo: pygit2.Repository, commit: pygit2.Commit, context_lines: int = 0) -> pygit2.Diff:
    if commit.parents:
        diff = repo.diff(commit.parents[0], commit, context_lines=context_lines)
    else:
        diff = commit.tree.diff_to_tree(swap=True, context_lines=context_lines)
    diff.find_similar(flags=DiffFind.FIND_RENAMES)
    return diff


_STATUS = {"A": "A", "D": "D", "M": "M", "R": "R", "C": "A", "T": "M"}


def extract_commit(repo: pygit2.Repository, commit: pygit2.Commit) -> CommitRecord:
    rec = CommitRecord(
        hash=str(commit.id),
        parents=[str(p) for p in commit.parent_ids],
        author_name=commit.author.name,
        author_email=(commit.author.email or "").lower(),
        ts=int(commit.commit_time),
        message=commit.message or "",
    )
    try:
        patches = list(commit_diff(repo, commit))
    except pygit2.GitError as e:
        # One undiffable commit (pathological path, corrupt object) must not abort the whole history: keep the
        # commit with no file changes and say why.
        rec.message = rec.message + f"\n\n[afd-oracle: diff unavailable: {str(e)[:120]}]"
        patches = []
    for patch in patches:
        if patch is None:
            continue
        d = patch.delta
        status = _STATUS.get(d.status_char(), "M")
        _, adds, dels = patch.line_stats
        old_id, new_id = str(d.old_file.id), str(d.new_file.id)
        rec.changes.append(FileChange(
            path=d.new_file.path if status != "D" else d.old_file.path,
            old_path=d.old_file.path if status == "R" else None,
            status=status,
            added=adds,
            deleted=dels,
            old_blob=None if old_id == ZERO else old_id,
            new_blob=None if new_id == ZERO else new_id,
            binary=bool(d.is_binary),
        ))
    return rec


def file_patch(repo: pygit2.Repository, sha: str, path: str, context_lines: int = 3, max_lines: int = 80,
               inverse: bool = False) -> tuple[str, bool]:
    """Unified patch text for one file in one commit (first-parent), trimmed to max_lines."""
    commit = repo.get(sha)
    if commit is None:
        return "", False
    if inverse and commit.parents:
        diff = repo.diff(commit, commit.parents[0], context_lines=context_lines)
    else:
        diff = commit_diff(repo, commit, context_lines)
    for patch in diff:
        if patch is None:
            continue
        if path in (patch.delta.new_file.path, patch.delta.old_file.path):
            if patch.delta.is_binary:
                return "(binary file)", False
            lines = (patch.text or "").splitlines()
            truncated = len(lines) > max_lines
            return "\n".join(lines[:max_lines]) + ("\n... (truncated)" if truncated else ""), truncated
    return "", False


HUNK_RE = re.compile(r"^@@[^@]*@@ ?(.*)$")


def hunk_headers(repo: pygit2.Repository, sha: str, limit: int = 30) -> list[str]:
    commit = repo.get(sha)
    out: list[str] = []
    if commit is None:
        return out
    for patch in commit_diff(repo, commit, context_lines=0):
        if patch is None or patch.delta.is_binary:
            continue
        for h in patch.hunks:
            m = HUNK_RE.match(h.header.strip())
            ctx = m.group(1).strip() if m else ""
            if ctx:
                out.append(f"{patch.delta.new_file.path}: {ctx}")
            if len(out) >= limit:
                return out
    return out


def blob_bytes(repo: pygit2.Repository, oid: str) -> bytes | None:
    obj = repo.get(oid)
    return obj.data if isinstance(obj, pygit2.Blob) else None


def tree_files(repo: pygit2.Repository, sha: str) -> dict[str, str]:
    """path -> blob oid for every file in a commit's tree."""
    out: dict[str, str] = {}

    def walk(tree: pygit2.Tree, prefix: str) -> None:
        for e in tree:
            p = f"{prefix}{e.name}"
            if e.type_str == "tree":
                walk(repo[e.id], p + "/")
            elif e.type_str == "blob":
                out[p] = str(e.id)

    walk(repo.get(sha).tree, "")
    return out


def path_in_tree(repo: pygit2.Repository, sha: str, path: str) -> bool:
    commit = repo.get(sha)
    if commit is None:
        return False
    try:
        commit.tree[path]
        return True
    except KeyError:
        return False
