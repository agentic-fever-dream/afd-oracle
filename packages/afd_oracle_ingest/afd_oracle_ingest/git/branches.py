"""Step 7: branch classification (merged / unmerged / abandoned) plus merged branches recovered from merge messages."""
from __future__ import annotations

import json
import re

import pygit2
from pygit2.enums import SortMode

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store

from .walker import default_branch, ref_tips

MERGE_PATTERNS = [
    re.compile(r"^Merge branch '([^']+)'"),
    re.compile(r"^Merge remote-tracking branch '([^']+)'"),
    re.compile(r"^Merge pull request #(\d+) from (\S+)"),
    re.compile(r"^Merged? in (\S+) \(pull request #(\d+)\)"),
]
BRANCH_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/pull/", "refs/merge-requests/")
PR_REF = re.compile(r"^refs/(?:pull|merge-requests)/(\d+)/head$")
PR_REF_IN_SUBJECT = re.compile(r"(?:\(#|pull request #|!)(\d+)\)?")


def short_name(ref: str) -> str:
    for p in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(p):
            return ref[len(p):]
    return ref[len("refs/"):] if ref.startswith("refs/") else ref


def classify_branches(repo: pygit2.Repository, store: Store, settings: Settings, now_ts: int | None = None) -> dict:
    default_ref, default_tip = default_branch(repo)
    ref_now = now_ts or store.latest_ts()
    stale_before = ref_now - settings.abandoned_days * 86400
    stats = {"branches": 0, "merged": 0, "unmerged": 0, "abandoned": 0, "squash_merged": 0, "from_merge_messages": 0}
    # PR numbers that landed on the default branch via squash/rebase ("Subject (#123)") or merge messages
    landed: dict[str, str] = {}
    walker = repo.walk(default_tip, SortMode.TOPOLOGICAL)
    for c in walker:
        subject = (c.message or "").splitlines()[0] if c.message else ""
        for m in PR_REF_IN_SUBJECT.finditer(subject):
            landed.setdefault(m.group(1), str(c.id))
    with store.tx() as conn:
        conn.execute("DELETE FROM branches")
        conn.execute("DELETE FROM branch_commits")
        names: set[str] = set()
        for ref, tip in sorted(ref_tips(repo).items()):
            if not ref.startswith(BRANCH_PREFIXES):
                continue
            name = short_name(ref)
            if ref == default_ref:
                tip_ts = repo.get(tip).commit_time
                conn.execute("INSERT OR REPLACE INTO branches VALUES (?,?,?,?,?,?,?,?,?)",
                             (name, ref, tip, tip_ts, 1, 0, 0, "default", None))
                names.add(name)
                continue
            walker = repo.walk(tip, SortMode.TOPOLOGICAL)
            walker.hide(default_tip)
            unique = [str(c.id) for c in walker]
            tip_ts = repo.get(tip).commit_time
            merged = not unique
            source, merge_commit = "ref", None
            pr = PR_REF.match(ref)
            if not merged and pr and pr.group(1) in landed:
                merged, source, merge_commit = True, "squash_merge", landed[pr.group(1)]
                stats["squash_merged"] += 1
            abandoned = (not merged) and tip_ts < stale_before
            conn.execute("INSERT OR REPLACE INTO branches VALUES (?,?,?,?,?,?,?,?,?)",
                         (name, ref, tip, tip_ts, int(merged), int(abandoned), len(unique), source, merge_commit))
            conn.executemany("INSERT OR IGNORE INTO branch_commits VALUES (?,?)", [(name, h) for h in unique])
            names.add(name)
            stats["branches"] += 1
            stats["merged" if merged else "unmerged"] += 1
            stats["abandoned"] += int(abandoned)
        for c in store.q("SELECT hash, parents, ts, message FROM commits WHERE is_merge=1 ORDER BY seq"):
            subject = (c["message"] or "").splitlines()[0] if c["message"] else ""
            for pat in MERGE_PATTERNS:
                m = pat.match(subject)
                if not m:
                    continue
                if pat.pattern.startswith("^Merge pull request"):
                    name = f"{m.group(2)} (PR #{m.group(1)})"
                else:
                    name = m.group(1)
                if name in names:
                    break
                parents = json.loads(c["parents"])
                tip = parents[1] if len(parents) > 1 else None
                tip_row = store.commit(tip) if tip else None
                conn.execute("INSERT OR REPLACE INTO branches VALUES (?,?,?,?,?,?,?,?,?)",
                             (name, None, tip, tip_row["ts"] if tip_row else None, 1, 0, 0, "merge_message",
                              c["hash"]))
                names.add(name)
                stats["from_merge_messages"] += 1
                break
    return stats
