"""Step 11: compact, deterministic LLM work packets (<= ~6k tokens each), ranked and capped.

Packets pre-attach the evidence an analyst needs (metrics, messages, trimmed diffs) so subagents spend tokens on
judgement, not on tool round-trips. Rebuilding keeps each packet's status (open / claimed / done).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

import pygit2

from afd_oracle_core.config import Settings
from afd_oracle_core.ids import short_hash
from afd_oracle_core.store import Store, loads

from .ast.extractor import language_for
from .git.walker import file_patch

DIFF_STEPS = (80, 40, 20, 8, 0)
MAX_BRANCH_UNIQUE = 100


def _is_test(path: str) -> bool:
    p = path.lower()
    return p.startswith(("test/", "tests/")) or "/tests/" in p or "/test_" in p or p.startswith("test_")


# ---------------------------------------------------------------------------- noise filter
# Candidates a program can recognise as noise never reach an analyst (design rule: what a program can compute does not
# go to an LLM). On flask these were the bulk of what analysts skipped. Every drop is counted under a named reason
# and reported by build_packets, so a filtered pool is never mistaken for a complete one.
DOC_NAMES = {"readme", "changes", "changelog", "contributing", "license", "licence", "authors", "code_of_conduct",
             "security", "history", "notice", "copying"}
DEP_BUMP = re.compile(r"(?i)^(bump|update|upgrade|pin)\b.*\b(dep|deps|dependenc\w*|requirements?|pins?|versions?|actions?|"
                      r"pre-commit|hooks?)\b|pre-commit autoupdate|^\[pre-commit\.ci\]|^build\(deps|^chore\(deps")
VERSION_BUMP = re.compile(r"(?i)(bump(ed)?\s+(the\s+)?version|version\s+bump|set\s+version|^release\s+v?\d|"
                          r"^v?\d+\.\d+(\.\d+)?([.-]?dev\d*)?\s*$|^(start|begin)\s+v?\d+\.\d+)")
_NOISE: Counter = Counter()
_FILTER_ON = True


def _is_docs(path: str) -> bool:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    return (p.startswith(("docs/", "doc/")) or base.split(".")[0] in DOC_NAMES
            or (base.endswith((".rst", ".md", ".txt")) and "requirements" not in p and "constraints" not in p))


def _is_changelog(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower().split(".")[0] in {"changes", "changelog", "history", "news"}


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    for affix in ("test_", "tests_"):
        if base.startswith(affix):
            base = base[len(affix):]
    for affix in ("_test", "_tests", ".test", ".spec"):
        if base.endswith(affix):
            base = base[: -len(affix)]
    return base


def _noise(kind: str, reason: str | None) -> bool:
    """True (and counted) when the noise filter drops this candidate."""
    if reason is None or not _FILTER_ON:
        return False
    _NOISE[f"{kind}/{reason}"] += 1
    return True


def _change_noise(paths: list[str], lines: int, subj: str) -> str | None:
    """Why a revert / branch change is not worth an analyst, or None."""
    if not paths:
        return "no_tracked_files"
    if all(_is_docs(p) for p in paths):
        return "docs_only"
    if all(_is_test(p) for p in paths):
        return "tests_only"
    if lines <= 2:
        return "trivial_change"
    if VERSION_BUMP.search(subj) and lines <= 8:
        return "version_bump"
    if DEP_BUMP.search(subj):
        return "dependency_bump"
    return None


def iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None


def tokens(obj: Any) -> int:
    return len(json.dumps(obj)) // 4


def subject(msg: str | None) -> str:
    return (msg or "").splitlines()[0][:200] if msg else ""


def fit(build: Callable[[int], dict], max_tokens: int) -> dict:
    """Call build(max_diff_lines) with shrinking diff budgets until the packet fits."""
    body = {}
    for n in DIFF_STEPS:
        body = build(n)
        if tokens(body) <= max_tokens:
            return body
    body["_note"] = "trimmed to fit; use get_commit_diff for full patches"
    return body


_REPO: pygit2.Repository | None = None  # set by build_packets; lets briefs report the author date


def _authored(sha: str) -> str | None:
    if _REPO is None:
        return None
    try:
        return iso(_REPO.get(sha).author.time)
    except Exception:
        return None


def _commit_brief(store: Store, sha: str, msg_chars: int = 400) -> dict:
    c = store.commit(sha)
    if c is None:
        return {"hash": sha}
    lines = store.q1("SELECT coalesce(sum(added),0) a, coalesce(sum(deleted),0) d, "
                     "sum(status='R' AND added=0 AND deleted=0) r FROM file_changes WHERE hash=?", (sha,))
    # "date" is the committer date (reset by rebases and cherry-picks); "authored" is when the change was written.
    # Neither is when it reached the main line: that is the date of the merge that brought it in.
    brief = {"hash": sha, "date": iso(c["ts"]), "authored": _authored(sha), "author": c["author_email"],
             "subject": subject(c["message"]),
             "message": (c["message"] or "")[:msg_chars].strip(), "files_changed": c["files_changed"],
             "lines_added": lines["a"], "lines_deleted": lines["d"]}
    keys = c.keys()
    if "landed_ts" in keys and c["landed_ts"]:
        brief["landed_on_main"] = iso(c["landed_ts"])
        if c["landed_in"] and c["landed_in"] != sha:
            brief["landed_via"] = c["landed_in"]
    elif "on_main" in keys and not c["on_main"]:
        brief["landed_on_main"] = None  # never reached the main line
    if lines["a"] == 0 and lines["d"] == 0 and (lines["r"] or 0) > 0:
        brief["pure_rename"] = True  # moved files only; never cite as the commit that introduced a design
    return brief


def _patch(repo: pygit2.Repository, sha: str, path: str, n: int) -> str | None:
    if n <= 0:
        return None
    text, _ = file_patch(repo, sha, path, context_lines=2, max_lines=n)
    return text or None


def _symbols_in(store: Store, path: str, limit: int = 12) -> list[dict]:
    return [{"uid": r["uid"], "qualname": r["qualname"], "kind": r["kind"]} for r in store.q(
        "SELECT uid, qualname, kind FROM symbols WHERE path=? AND alive=1 AND parent IS NULL ORDER BY start_line "
        "LIMIT ?", (path, limit))]


def _lineage(store: Store, path: str) -> str | None:
    row = store.q1("SELECT lineage FROM file_changes WHERE path=? ORDER BY rowid DESC LIMIT 1", (path,))
    return row["lineage"] if row else None


def _path_at(store: Store, sha: str, path: str, lineage: str | None) -> str:
    """The path this file had in commit `sha` (co-change is lineage-keyed, so a shared commit may predate a rename)."""
    if lineage:
        row = store.q1("SELECT path FROM file_changes WHERE hash=? AND lineage=?", (sha, lineage))
        if row:
            return row["path"]
    return path


# ---------------------------------------------------------------------------- coupling
def coupling_packets(repo: pygit2.Repository, store: Store, settings: Settings, limit: int) -> list[tuple]:
    out = []
    rows = store.q("SELECT * FROM cochange ORDER BY (cross_dir=1 AND structural_link=0) DESC, score DESC")
    for r in rows:
        if len(out) >= limit:
            break
        shared = json.loads(r["shared_commits"])
        files = {}
        for p in (r["a"], r["b"]):
            f = store.q1("SELECT lang, alive, churn, commits FROM files WHERE path=?", (p,))
            files[p] = {**(dict(f) if f else {}), "top_level_symbols": _symbols_in(store, p)}
        a, b = r["a"], r["b"]
        reason = None
        if r["structural_link"]:
            reason = "explicit_link"  # an import/call path of 2 hops or fewer already explains the co-change
        elif _is_test(a) != _is_test(b) and _stem(a) == _stem(b):
            reason = "test_with_subject"
        elif _is_docs(a) and _is_docs(b):
            reason = "docs_lockstep"
        elif _is_changelog(a) or _is_changelog(b):
            reason = "changelog_lockstep"
        elif a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1] and not (files[a].get("alive") and files[b].get("alive")):
            reason = "rename_artifact"
        else:
            subjects = [subject((store.commit(h) or {"message": ""})["message"]) for h in shared[:20]]
            if subjects and sum(bool(DEP_BUMP.search(s) or VERSION_BUMP.search(s)) for s in subjects) / len(subjects) >= 0.5:
                reason = "dependency_bumps"
        if _noise("coupling", reason):
            continue

        lin = {p: _lineage(store, p) for p in (r["a"], r["b"])}

        def build(n: int, r=r, shared=shared, files=files, lin=lin) -> dict:
            return {
                "kind": "coupling", "repo": store.repo_id, "pair": {"a": r["a"], "b": r["b"]},
                "metrics": {k: r[k] for k in ("count", "n_a", "n_b", "jaccard", "conf_ab", "conf_ba", "cross_dir",
                                              "lca_depth", "structural_link", "score")},
                "files": files,
                "shared_commits": [
                    {**_commit_brief(store, h, 300),
                     "diff_a": _patch(repo, h, _path_at(store, h, r["a"], lin[r["a"]]), n),
                     "diff_b": _patch(repo, h, _path_at(store, h, r["b"], lin[r["b"]]), n)}
                    for h in shared[:3]],
                "all_shared_commits": shared,
            }

        body = fit(build, settings.packet_max_tokens)
        pid = f"coupling:{short_hash(r['a'], r['b'])}"
        out.append((pid, "coupling", "cross_dir_unlinked" if r["cross_dir"] and not r["structural_link"] else "pair",
                    r["score"], body))
    return out


# ---------------------------------------------------------------------------- graveyard
def _followups(store: Store, after_seq: int, paths: set[str], limit: int = 3) -> list[dict]:
    if not paths:
        return []
    marks = ",".join("?" * len(paths))
    rows = store.q(f"SELECT DISTINCT c.hash, c.seq FROM commits c JOIN file_changes fc ON fc.hash=c.hash "
                   f"WHERE c.seq > ? AND fc.path IN ({marks}) ORDER BY c.seq LIMIT ?", (after_seq, *paths, limit))
    return [_commit_brief(store, r["hash"], 200) for r in rows]


def graveyard_packets(repo: pygit2.Repository, store: Store, settings: Settings, limit: int) -> list[tuple]:
    out = []
    # A revert that was itself reverted, or that reverts a revert, means the change ended up back in the code:
    # that is churn, not a rejected attempt. Remaining reverts rank by how much code (not docs) they removed.
    rows = store.q("""SELECT r.*, (SELECT count(*) FROM file_changes fc WHERE fc.hash = r.reverted AND fc.ignored = 0
                                   AND (fc.path LIKE '%.py' OR fc.path LIKE '%.ts' OR fc.path LIKE '%.js')) AS code_files,
                             (SELECT count(*) FROM symbol_events se WHERE se.hash = r.reverted) AS sym_events
                      FROM reverts r
                      WHERE r.reverter NOT IN (SELECT reverted FROM reverts)
                        AND r.reverted NOT IN (SELECT reverter FROM reverts)""")
    rows = sorted(rows, key=lambda r: (-(r["method"] == "message"), -(r["code_files"] > 0), -r["sym_events"]))
    # Rebased or cherry-picked copies of the same change (same subject, author and author date) appear as separate
    # revert pairs on mirrored refs; keep one per change.
    seen_changes: set[tuple] = set()
    unique = []
    for r in rows:
        rc = store.commit(r["reverted"])
        key = (subject(rc["message"]) if rc else r["reverted"], rc["author_email"] if rc else "", _authored(r["reverted"]))
        if key in seen_changes:
            continue
        seen_changes.add(key)
        fcs = store.q("SELECT path, added, deleted FROM file_changes WHERE hash=? AND ignored=0", (r["reverted"],))
        if _noise("revert", _change_noise([f["path"] for f in fcs], sum(f["added"] + f["deleted"] for f in fcs),
                                          subject(rc["message"]) if rc else "")):
            continue
        unique.append(r)
    rows = unique[:limit]
    for r in rows:
        reverted, reverter = r["reverted"], r["reverter"]
        paths = sorted(store.touched_paths(reverted))
        rc = store.commit(reverter)
        syms = [dict(x) for x in store.q("SELECT uid, change FROM symbol_events WHERE hash=?", (reverted,))]

        def build(n: int, r=r, reverted=reverted, reverter=reverter, paths=paths, rc=rc, syms=syms) -> dict:
            return {
                "kind": "graveyard", "subkind": "revert", "repo": store.repo_id,
                "method": r["method"], "partial": bool(r["partial"]), "overlap": r["overlap"],
                "reverted": _commit_brief(store, reverted, 800), "reverter": _commit_brief(store, reverter, 800),
                "files": paths, "symbols_touched_by_reverted": syms[:30],
                "reverted_diffs": {p: _patch(repo, reverted, p, n) for p in paths[:6]},
                "followups_after_revert": _followups(store, rc["seq"], set(paths)) if rc else [],
            }

        prio = (0.9 if r["method"] == "message" else 0.8) + (0.05 if r["code_files"] else 0) + min(0.05, r["sym_events"] / 200)
        out.append((f"graveyard:revert:{short_hash(reverter, reverted)}", "graveyard", "revert",
                    round(prio, 4), fit(build, settings.packet_max_tokens)))

    # Refs with hundreds of "unique" commits are unrelated or rewritten histories (e.g. a PR ref from a fork with a
    # different root), not a coherent attempt; small, recent branches are the useful ones.
    n_branches = 0
    seen_branch_sets: set[tuple] = set()
    for b in store.q("SELECT * FROM branches WHERE (abandoned=1 OR (merged=0 AND source='ref')) "
                     "AND unique_count BETWEEN 1 AND ? ORDER BY abandoned DESC, tip_ts DESC",
                     (MAX_BRANCH_UNIQUE,)):
        if n_branches >= limit:
            break
        commits = [r["hash"] for r in store.q(
            "SELECT bc.hash FROM branch_commits bc JOIN commits c ON c.hash=bc.hash WHERE bc.branch=? ORDER BY c.seq",
            (b["name"],))]
        stat: dict[str, list[int]] = {}
        for h in commits:
            for fc in store.q("SELECT path, added, deleted FROM file_changes WHERE hash=? AND ignored=0", (h,)):
                s = stat.setdefault(fc["path"], [0, 0])
                s[0] += fc["added"]
                s[1] += fc["deleted"]
        subjects = " | ".join(subject((store.commit(h) or {"message": ""})["message"]) for h in commits[:5])
        reason = _change_noise(list(stat), sum(a + d for a, d in stat.values()), subjects)
        if reason is None:
            key = tuple(commits)
            if key in seen_branch_sets:
                reason = "duplicate_ref"  # another ref carries exactly the same unique commits (e.g. two PR refs)
            elif not any(language_for(p) and not _is_test(p) and not _is_docs(p) for p in stat):
                reason = "no_source_change"  # config/CI/examples-assets only: no design attempt in the code
            seen_branch_sets.add(key)
        if _noise("branch", reason):
            continue
        n_branches += 1
        created = [dict(x) for x in store.q(
            "SELECT DISTINCT se.uid, se.qualname, se.path FROM symbol_events se JOIN branch_commits bc ON bc.hash=se.hash "
            "WHERE bc.branch=? AND se.change='created' LIMIT 40", (b["name"],))]

        def build(n: int, b=b, commits=commits, stat=stat, created=created) -> dict:
            first_paths = sorted(stat)[:4]
            return {
                "kind": "graveyard", "subkind": "abandoned_branch" if b["abandoned"] else "unmerged_branch",
                "repo": store.repo_id, "branch": b["name"], "ref": b["ref"], "tip": b["tip"],
                "tip_date": iso(b["tip_ts"]), "unique_commits": b["unique_count"],
                "commits": [_commit_brief(store, h, 300) for h in commits[:20]],
                "diffstat": {p: {"added": a, "deleted": d} for p, (a, d) in sorted(stat.items())[:40]},
                "symbols_created": created,
                "sample_diffs": {f"{h[:10]}:{p}": _patch(repo, h, p, n) for h in commits[-2:] for p in first_paths
                                 if p in store.touched_paths(h)},
            }

        out.append((f"graveyard:branch:{short_hash(b['name'])}", "graveyard",
                    "abandoned_branch" if b["abandoned"] else "unmerged_branch",
                    0.8 if b["abandoned"] else 0.5, fit(build, settings.packet_max_tokens)))

    # A "deleted" symbol whose qualname and kind are alive elsewhere was moved (module split, package rename), not
    # abandoned; test code is rarely a rejected design. The noise filter drops both; with it off they are demoted.
    alive_names = {(r["qualname"], r["kind"]) for r in store.q("SELECT DISTINCT qualname, kind FROM symbols WHERE alive=1")}

    def sym_noise(c) -> str | None:
        if _is_test(loads(c["props"], {}).get("path", "")):
            return "test_code"
        sym = store.symbol(c["subject"])
        if sym is not None and (sym["qualname"], sym["kind"]) in alive_names:
            return "moved_not_removed"
        return None

    def sym_weight(c) -> float:
        reason = sym_noise(c)
        return 1.0 if reason is None else (0.3 if reason == "test_code" else 0.2)

    sym_cands = []
    for kind in ("short_lived_symbol", "churned_deleted"):  # separate caps, so one kind cannot crowd out the other
        rows = [c for c in store.q("SELECT * FROM candidates WHERE kind=?", (kind,))
                if not _noise("symbol", sym_noise(c))]
        weighted = [(float(c["score"]) * sym_weight(c), c) for c in rows]
        weighted.sort(key=lambda x: -x[0])
        sym_cands += [(w, c) for w, c in weighted[:limit]]
    top_w = {kind: max((w for w, c in sym_cands if c["kind"] == kind), default=1.0) or 1.0
             for kind in ("short_lived_symbol", "churned_deleted")}
    for w, c in sym_cands:
        props = loads(c["props"], {})
        events = [dict(e) for e in store.q(
            "SELECT se.hash, se.change, c.ts, c.message FROM symbol_events se JOIN commits c ON c.hash=se.hash "
            "WHERE se.uid=? ORDER BY se.seq", (c["subject"],))]

        def build(n: int, c=c, props=props, events=events) -> dict:
            return {
                "kind": "graveyard", "subkind": c["kind"], "repo": store.repo_id, "symbol": props,
                "events": [{"hash": e["hash"], "change": e["change"], "date": iso(e["ts"]), "authored": _authored(e["hash"]),
                            "subject": subject(e["message"])} for e in events],
                "created_diff": _patch(repo, props["created_in"], props["path"], n),
                "deleted_diff": _patch(repo, props["deleted_in"], props["path"], n),
            }

        prio = 0.2 + 0.5 * w / top_w[c["kind"]]  # 0.2..0.7, ordered by the weighted score within each kind
        out.append((f"graveyard:{c['kind']}:{short_hash(c['subject'])}", "graveyard", c["kind"],
                    round(prio, 4), fit(build, settings.packet_max_tokens)))
    return out


# ---------------------------------------------------------------------------- patterns
def pattern_packets(repo: pygit2.Repository, store: Store, settings: Settings, limit: int) -> list[tuple]:
    out = []
    for c in store.q("SELECT * FROM candidates WHERE kind IN ('god_class', 'cycle', 'leaky_abstraction', "
                     "'idiom_cluster') ORDER BY kind, score DESC"):
        props = loads(c["props"], {})
        kind = c["kind"]
        if kind == "god_class":
            members = [{"uid": r["uid"], "signature": r["signature"]} for r in store.q(
                "SELECT uid, signature FROM symbols WHERE parent=? AND alive=1 ORDER BY start_line LIMIT 60",
                (c["subject"],))]
            history = [_commit_brief(store, r["hash"], 160) for r in store.q(
                "SELECT DISTINCT se.hash, se.seq FROM symbol_events se JOIN symbols s ON s.uid=se.uid "
                "WHERE se.uid=? OR s.parent=? ORDER BY se.seq DESC LIMIT 8", (c["subject"], c["subject"]))]
            body = {"kind": "pattern", "subkind": kind, "repo": store.repo_id, "class_uid": c["subject"],
                    "metrics": props, "members": members, "recent_history": history}
        elif kind == "cycle":
            history = [_commit_brief(store, r["hash"], 160) for r in store.q(
                f"SELECT DISTINCT fc.hash, c.seq FROM file_changes fc JOIN commits c ON c.hash=fc.hash "
                f"WHERE fc.path IN ({','.join('?' * len(props['files']))}) ORDER BY c.seq LIMIT 8", props["files"])]
            body = {"kind": "pattern", "subkind": kind, "repo": store.repo_id, **props, "introducing_history": history}
        elif kind == "leaky_abstraction":
            callee = store.symbol(c["subject"])
            body = {"kind": "pattern", "subkind": kind, "repo": store.repo_id, **props,
                    "callee_signature": callee["signature"] if callee else None,
                    "callers": props.get("callers", [])[:20]}
        else:  # idiom_cluster
            body = {"kind": "pattern", "subkind": kind, "repo": store.repo_id, **props}
        if tokens(body) > settings.packet_max_tokens:
            body = {**{k: v for k, v in body.items() if k not in ("members", "callers", "symbols")},
                    "_note": "member/caller lists dropped to fit; use run_cypher for details"}
        out.append((f"pattern:{kind}:{short_hash(c['subject'])}", "pattern", kind, float(c["score"]), body))
    out.sort(key=lambda x: -x[3])
    return out[:limit]


def build_packets(repo: pygit2.Repository, store: Store, settings: Settings, limit_per_kind: int = 200) -> dict:
    global _REPO, _FILTER_ON
    _REPO = repo
    _FILTER_ON = getattr(settings, "packet_noise_filter", True)
    _NOISE.clear()
    if limit_per_kind <= 0:  # 0 = no cap: every candidate that survives the noise filter becomes a packet
        limit_per_kind = 10 ** 9
    from .catalog.packets import catalog_packets

    packets = (coupling_packets(repo, store, settings, limit_per_kind)
               + graveyard_packets(repo, store, settings, limit_per_kind)
               + pattern_packets(repo, store, settings, limit_per_kind)
               + catalog_packets(repo, store, settings, min(limit_per_kind, getattr(settings, "catalog_max_per_kind", 40)),
                                 _commit_brief, _patch, fit))
    ids = {p[0] for p in packets}
    with store.tx() as conn:
        for pid, kind, subkind, prio, body in packets:
            conn.execute(
                "INSERT INTO packets(id, kind, subkind, priority, body, status, tokens) VALUES (?,?,?,?,?,'open',?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body, priority=excluded.priority, tokens=excluded.tokens, "
                "subkind=excluded.subkind",
                (pid, kind, subkind, prio, json.dumps(body, default=str), tokens(body)))
        # Packets that are no longer candidates go, whatever their status: a claimed packet from an older ranking
        # would otherwise keep its stale priority and body forever.
        stale = [r["id"] for r in conn.execute("SELECT id FROM packets") if r["id"] not in ids]
        conn.executemany("DELETE FROM packets WHERE id=?", [(s,) for s in stale])
    counts: dict[str, int] = {}
    for p in packets:
        counts[f"{p[1]}/{p[2]}"] = counts.get(f"{p[1]}/{p[2]}", 0) + 1
    return {"packets": len(packets), "by_kind": counts,
            "max_tokens": max((tokens(p[4]) for p in packets), default=0),
            "filtered_as_noise": dict(sorted(_NOISE.items())), "noise_filter": _FILTER_ON}
