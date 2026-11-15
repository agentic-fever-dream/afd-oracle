"""Step 6: revert detection.

* explicit: ``This reverts commit <sha>`` (or ``Revert "<subject>"`` matched by subject)
* silent:   a later commit whose changed-line set is the inverse of an earlier commit's (full or >= 60% partial),
            within a time window, restricted to small commits that touch overlapping paths.
"""
from __future__ import annotations

import re
from functools import lru_cache

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.store import Store

from .walker import commit_diff

EXPLICIT_HASH = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
REVERT_SUBJECT = re.compile(r'^Revert "(.+)"\s*$')


def line_signature(repo: pygit2.Repository, sha: str) -> frozenset[tuple[str, str, str]]:
    commit = repo.get(sha)
    out: set[tuple[str, str, str]] = set()
    if commit is None:
        return frozenset()
    for patch in commit_diff(repo, commit, context_lines=0):
        if patch is None or patch.delta.is_binary:
            continue
        path = patch.delta.new_file.path
        for hunk in patch.hunks:
            for line in hunk.lines:
                if line.origin in "+-":
                    content = line.content.rstrip()
                    if content.strip():
                        out.add((path, line.origin, content))
    return frozenset(out)


def invert(sig: frozenset[tuple[str, str, str]]) -> frozenset[tuple[str, str, str]]:
    return frozenset((p, "-" if o == "+" else "+", c) for p, o, c in sig)


LOGIC_VERSION = "2"  # bump when detection rules change: forces a full recompute on the next ingest
BOT_MARKERS = ("[bot]", "pre-commit.ci", "dependabot", "renovate", "github-actions")
FORMATTER_SUBJECT = re.compile(r"\b(auto ?fix|autoformat|reformat|format(ting)?|lint|black|prettier|isort|ruff)\b", re.I)


def _is_mechanical(c) -> bool:
    msg = (c["message"] or "").lower()
    who = (c["author_email"] or "").lower()
    subject = msg.splitlines()[0] if msg else ""
    return any(m in who or m in subject for m in BOT_MARKERS) or bool(FORMATTER_SUBJECT.search(subject))


def detect_reverts(repo: pygit2.Repository, store: Store, settings: Settings, since_seq: int = 0) -> dict:
    if store.get_meta("reverts_logic") != LOGIC_VERSION:
        since_seq = 0
        with store.tx() as conn:
            conn.execute("DELETE FROM reverts")
            conn.execute("UPDATE commits SET is_revert=0")
        store.set_meta("reverts_logic", LOGIC_VERSION)
    commits = store.q("SELECT hash, seq, ts, message, author_email, is_merge, is_bulk, files_changed, on_main "
                      "FROM commits ORDER BY seq")
    by_subject: dict[str, str] = {}
    explicit = 0
    with store.tx() as conn:
        for c in commits:
            msg = c["message"] or ""
            subject = msg.splitlines()[0].strip() if msg else ""
            if c["seq"] >= since_seq:
                targets = []
                for m in EXPLICIT_HASH.finditer(msg):
                    full = store.resolve_commit(m.group(1))
                    if full and full != c["hash"]:
                        targets.append(full)
                sm = REVERT_SUBJECT.match(subject)
                if not targets and sm and sm.group(1) in by_subject:
                    targets.append(by_subject[sm.group(1)])
                if targets or sm:
                    conn.execute("UPDATE commits SET is_revert=1 WHERE hash=?", (c["hash"],))
                for t in targets:
                    conn.execute("INSERT OR REPLACE INTO reverts VALUES (?,?,?,?,?)", (c["hash"], t, "message", 0, 1.0))
                    explicit += 1
            by_subject.setdefault(subject, c["hash"])

    # -- silent reverts ----------------------------------------------------------
    # Only mainline history counts: undoing your own work inside a PR branch is iteration, not a rejected attempt.
    # Bot/formatter commits are excluded because they routinely "revert" whitespace the previous commit introduced.
    eligible = {c["hash"]: c for c in commits
                if not c["is_merge"] and not c["is_bulk"] and c["on_main"] and not _is_mechanical(c)
                and 0 < c["files_changed"] <= settings.revert_max_files}
    explicit_pairs = {(r["reverter"], r["reverted"]) for r in store.q(
        "SELECT reverter, reverted FROM reverts WHERE method='message'")}
    explicit_reverters = {a for a, _ in explicit_pairs}
    paths_of: dict[str, set[str]] = {}
    commits_by_path: dict[str, list[str]] = {}
    for r in store.q("SELECT hash, path, old_path FROM file_changes WHERE ignored=0"):
        if r["hash"] not in eligible:
            continue
        for p in (r["path"], r["old_path"]):
            if p:
                paths_of.setdefault(r["hash"], set()).add(p)
                commits_by_path.setdefault(p, []).append(r["hash"])

    sig = lru_cache(maxsize=4096)(lambda h: line_signature(repo, h))
    window = settings.revert_window_days * 86400
    silent = 0
    rows = []
    for h, c in eligible.items():
        if c["seq"] < since_seq or h in explicit_reverters:
            continue
        mine = sig(h)
        if not mine:
            continue
        cands = {x for p in paths_of.get(h, ()) for x in commits_by_path.get(p, ())}
        best: tuple[float, str, bool] | None = None
        for x in cands:
            xc = eligible[x]
            if x == h or xc["seq"] >= c["seq"] or c["ts"] - xc["ts"] > window or (h, x) in explicit_pairs:
                continue
            inv = invert(sig(x))
            if len(inv) < 1:
                continue
            if inv == mine:
                best = (1.0, x, False)
                break
            if len(inv) < 3:
                continue
            inter = len(inv & mine)
            overlap = inter / len(inv)
            if overlap >= settings.revert_partial_overlap and inter / len(mine) >= 0.5:
                if best is None or overlap > best[0]:
                    best = (overlap, x, True)
        if best is not None:
            rows.append((h, best[1], "patch_id", int(best[2]), round(best[0], 4)))
            silent += 1
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO reverts VALUES (?,?,?,?,?)", rows)
        conn.executemany("UPDATE commits SET is_revert=1 WHERE hash=?", [(r[0],) for r in rows])
    return {"explicit": explicit, "silent": silent}
