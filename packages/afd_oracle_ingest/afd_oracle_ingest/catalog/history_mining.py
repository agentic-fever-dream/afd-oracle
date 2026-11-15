"""Catalogue family: history mining (fix chains, SZZ, reverts and relands, churn spikes, late clone propagation).

Everything here is derived from ``commits`` / ``file_changes`` / ``fix_events`` / ``symbol_events`` and the
``diff_lines`` index, plus the enrichment tables (``diff_lines.symbol``, ``line_origins`` = git blame of fix-removed
lines). Where the spec asks for data Oracle does not hold (patch-ids, hunk shingles) a proxy is used and described in
the builder's docstring.
"""
from __future__ import annotations

import bisect
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .base import (DAY, DEP_MANIFEST_RX, LOCKFILE_RX, REVERT_RX, Ctx, Hit, candidate, is_code, is_docs,
                   is_fix_message, is_generated, is_test, is_vendored, subject_of)

# ---------------------------------------------------------------------------- shared private helpers
_WS = re.compile(r"\s+")
_TRIVIAL = re.compile(r"^(#|//|\*|/\*|\"\"\"|'''|import\s|from\s+\S+\s+import\s|using\s|package\s|[\)\}\]\{\(,:;]*$|"
                      r"return$|pass$|else\s*:?$|try\s*:?$|break$|continue$|end$|@\w+$)")
_AGAIN = re.compile(r"(?i)\b(again|still|properly|actually|really|finally|once more|re-?fix|for real|yet another)\b")
_NOISY_PATH = re.compile(r"(?i)(^|/)(fixtures?|testdata|snapshots?|__snapshots__|locale|locales|i18n|l10n|"
                         r"translations?)/|\.(po|pot|snap)$")
_FORMAT_MSG = re.compile(r"(?i)\b(reformat\w*|format(ting)?|black|isort|prettier|autopep8|yapf|lint|style|whitespace)\b")
_REFACTOR_MSG = re.compile(r"(?i)\b(refactor\w*|restructur\w*|reorgani[sz]\w*|extract\w*|split|simplif\w*|clean-?up|"
                           r"rework\w*|rewrite|consolidat\w*|inline|move[sd]?|decouple|modulari[sz]\w*)\b")
_MOVE_MSG = re.compile(r"(?i)\b(renam\w+|move[sd]?|relocat\w+)\b")
_REAPPLY_MSG = re.compile(r"(?i)\b(re-?land\w*|re-?appl\w+|re-?introduc\w+|re-?instat\w+|restor\w+|bring\s+back|"
                          r"revert\s+\"?revert)\b")


def _cache(ctx: Ctx, key: str, fn):
    d = ctx.__dict__.setdefault("_hm_cache", {})
    if key not in d:
        d[key] = fn()
    return d[key]


def _norm(t: str) -> str:
    return _WS.sub(" ", t.strip())


def _trivial(n: str, min_len: int = 12) -> bool:
    return len(n) < min_len or bool(_TRIVIAL.match(n))


def _line_counts(ctx: Ctx) -> dict[tuple[str, str], tuple[int, int]]:
    """(hash, path) -> (added, removed) indexed lines."""
    def build():
        out: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        for r in ctx.stream("SELECT hash, path, sign, count(*) n FROM diff_lines GROUP BY hash, path, sign"):
            out[(r["hash"], r["path"])][0 if r["sign"] == "+" else 1] += r["n"]
        return {k: (v[0], v[1]) for k, v in out.items()}
    return _cache(ctx, "lc", build)


def _fix_commits(ctx: Ctx) -> list:
    return _cache(ctx, "fixes", lambda: [c for c in ctx.commits if not c["is_merge"] and not c["is_bulk"]
                                         and not ctx.is_bot(c) and ctx.is_fix(c)])


def _skip_path(path: str) -> bool:
    return is_test(path) or is_generated(path) or is_vendored(path) or bool(LOCKFILE_RX.search(path))


def _added_index(ctx: Ctx, path: str) -> dict[str, list[tuple[int, str, int]]]:
    """normalised added line -> [(seq, hash, ts)] in time order, for one path."""
    def build():
        out: dict[str, list] = defaultdict(list)
        for r in ctx.q("SELECT hash, seq, ts, text FROM diff_lines WHERE path=? AND sign='+' ORDER BY seq", (path,)):
            out[_norm(r["text"])].append((r["seq"], r["hash"], r["ts"]))
        return out
    return _cache(ctx, f"aidx:{path}", build)


def _blame(ctx: Ctx) -> dict[tuple[str, str], dict[int, str]]:
    """(fix_hash, path) -> {old line number: commit that last wrote it}, from ``line_origins`` (git blame at the fix's
    parent). Empty when enrichment has not blamed anything."""
    def build():
        out: dict[tuple[str, str], dict[int, str]] = defaultdict(dict)
        for r in ctx.line_origins:
            out[(r["fix_hash"], r["path"])][r["line"]] = r["origin_hash"]
        return out
    return _cache(ctx, "blame", build)


def _blamed_fixes(ctx: Ctx) -> dict[str, Counter]:
    """origin commit -> Counter of fix commits whose removed lines were last written by it (blame)."""
    def build():
        out: dict[str, Counter] = defaultdict(Counter)
        for (fix, _path), lines in _blame(ctx).items():
            for o in lines.values():
                if o != fix:
                    out[o][fix] += 1
        return out
    return _cache(ctx, "blamed", build)


def _symbols_mapped(ctx: Ctx) -> bool:
    """True when enrichment attributed changed lines to symbols (``diff_lines.symbol``)."""
    return _cache(ctx, "symmapped", lambda: ctx.q1("SELECT 1 FROM diff_lines WHERE symbol != '' LIMIT 1") is not None)


def _sym_counts(ctx: Ctx) -> dict[str, dict[str, list[int]]]:
    """hash -> {symbol uid: [added, removed]} over diff_lines.symbol (innermost symbol; module-level lines are skipped)."""
    def build():
        out: dict[str, dict[str, list[int]]] = defaultdict(dict)
        for r in ctx.stream("SELECT hash, symbol, sign, count(*) n FROM diff_lines WHERE symbol != '' "
                       "GROUP BY hash, symbol, sign"):
            out[r["hash"]].setdefault(r["symbol"], [0, 0])[0 if r["sign"] == "+" else 1] += r["n"]
        return out
    return _cache(ctx, "symcounts", build)


def _in_symbol(sym_uid: str, uid: str) -> bool:
    """``sym_uid`` is ``uid`` or a symbol nested in it (qualname prefix ``Outer.``)."""
    return sym_uid == uid or sym_uid.startswith(uid[:uid.rfind(":")] + ".")


def _symbol_lines(ctx: Ctx, h: str, path: str, uid: str) -> tuple[int, int]:
    """(added, removed) changed lines of ``uid`` (and symbols nested in it) in commit ``h``. Falls back to the whole
    file's counts when the commit has no symbol-attributed lines (enrichment missing)."""
    per = _sym_counts(ctx).get(h)
    if per is None:
        return _line_counts(ctx).get((h, path), (0, 0))
    a = d = 0
    for k, (x, y) in per.items():
        if _in_symbol(k, uid):
            a += x
            d += y
    return a, d


def _rows_in(ctx: Ctx, rows: list, uid: str) -> list:
    """Rows of one commit restricted to ``uid`` (and nested symbols); unchanged when the commit has no symbol data."""
    if rows and rows[0]["hash"] not in _sym_counts(ctx):
        return rows
    return [r for r in rows if r["symbol"] and _in_symbol(r["symbol"], uid)]


def _szz(ctx: Ctx, fix_hash: str, path: str) -> dict | None:
    """SZZ origin of a fix. Primary: ``line_origins`` (git blame of the fix's parent) for each removed non-trivial
    line - the commit that last wrote it. Fallback, only when blame holds nothing usable for this fix and file: the
    latest earlier commit that added an identical (whitespace-normalised) line in that file. Returns the dominant
    origin, its share of eligible removed lines, the lines involved and ``method`` ('blame' or 'text-match')."""
    def build():
        c = ctx.by_hash.get(fix_hash)
        if c is None:
            return None
        removed = [(r["line"], _norm(r["text"])) for r in ctx.lines_of(fix_hash, path) if r["sign"] == "-"]
        elig = [(ln, t) for ln, t in removed if not _trivial(t, 15)]
        if not elig:
            return {"reason": "comment_or_import_only" if removed else "no_removed_lines"}
        origins: Counter[str] = Counter()
        lines: dict[str, list[str]] = defaultdict(list)
        method = "blame"
        bl = _blame(ctx).get((fix_hash, path))
        if bl:
            for ln, t in elig:
                o = bl.get(ln)
                if o and o != fix_hash and o in ctx.by_hash:
                    origins[o] += 1
                    lines[o].append(t)
        if not origins:
            method = "text-match"
            idx = _added_index(ctx, path)
            for _, t in elig:
                cands = [e for e in idx.get(t, ()) if e[0] < c["seq"]]
                if cands:
                    origins[cands[-1][1]] += 1
                    lines[cands[-1][1]].append(t)
        if not origins:
            return {"reason": "no_origin"}
        h, n = origins.most_common(1)[0]
        o = ctx.by_hash[h]
        return {"origin": h, "share": n / len(elig), "n": n, "eligible": len(elig),
                "lifetime_days": (c["ts"] - o["ts"]) / DAY, "lines": lines[h][:6], "method": method}
    return _cache(ctx, f"szz:{fix_hash}:{path}", build)


def _sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b or abs(len(a) - len(b)) > 0.3 * max(len(a), len(b)):
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _fix_event_pairs(ctx: Ctx) -> list[tuple[str, str]]:
    """Distinct (hash, path) of fix events whose commit message is a real fix."""
    def build():
        seen, out = set(), []
        for f in ctx.fix_events:
            k = (f["hash"], f["path"])
            c = ctx.by_hash.get(f["hash"])
            if k in seen or c is None or c["is_merge"] or c["is_bulk"] or not is_fix_message(c["message"]):
                continue
            seen.add(k)
            out.append(k)
        return out
    return _cache(ctx, "fixpairs", build)


# ---------------------------------------------------------------------------- fix chains
@candidate("fix_on_fix_chain")
def fix_on_fix_chain(ctx: Ctx):
    """>=3 real fix commits (each >=3 changed code lines; same-PR series < 1h apart collapsed) on one symbol within
    60 days, or two fixes where the later removes lines the earlier one added. Line counts and the re-fixed-line overlap
    are per symbol (``diff_lines.symbol``: the symbol and anything nested in it). Falls back to whole-file counts for a
    commit that has no symbol-attributed lines (enrichment missing)."""
    for uid, evs in ctx.fixes_by_uid.items():
        path = evs[0]["path"]
        if _skip_path(path):
            ctx.drop("tests_generated_or_lockfile")
            continue
        good: list = []
        seen: set[str] = set()
        for e in sorted(evs, key=lambda r: r["ts"]):
            c = ctx.by_hash.get(e["hash"])
            if c is None or e["hash"] in seen:
                continue
            seen.add(e["hash"])
            if c["is_bulk"] or c["is_merge"] or not is_fix_message(c["message"]):
                ctx.drop("not_real_fix")
                continue
            a, d = _symbol_lines(ctx, e["hash"], e["path"], uid)
            if a + d < 3:
                ctx.drop("tiny_fix")
                continue
            if good and e["ts"] - good[-1]["ts"] < 3600:
                ctx.drop("same_pr_series")
                continue
            good.append(e)
        if len(good) < 2:
            continue
        # best window within 60 days
        best: list = []
        j = 0
        for i in range(len(good)):
            while good[i]["ts"] - good[j]["ts"] > 60 * DAY:
                j += 1
            if i - j + 1 > len(best):
                best = good[j:i + 1]
        if len(best) < 2:
            continue
        overlap = 0
        if len(best) >= 2:
            added_before: set[str] = set()
            for e in best:
                lines = _rows_in(ctx, ctx.lines_of(e["hash"], e["path"]), uid)
                rem = {_norm(r["text"]) for r in lines if r["sign"] == "-" and not _trivial(_norm(r["text"]))}
                overlap += len(rem & added_before)
                added_before |= {_norm(r["text"]) for r in lines if r["sign"] == "+" and not _trivial(_norm(r["text"]))}
        if len(best) < 3 and overlap < 2:
            continue
        again = sum(1 for e in best if _AGAIN.search(ctx.by_hash[e["hash"]]["message"] or ""))
        zones = {k for e in best for k in json.loads(e["kinds"] or "[]") if k in ("race", "security")}
        sym = ctx.symbols.get(uid)
        name = sym["qualname"] if sym else uid
        yield Hit(
            subject=uid,
            score=len(best) * 2 + overlap + again * 2 + (3 if zones else 0),
            title=f"{name}: {len(best)} fixes within {round((best[-1]['ts'] - best[0]['ts']) / DAY)} days, "
                  f"{overlap} lines re-fixed",
            evidence={"fixes": [{"hash": e["hash"], "subject": e["subject"]} for e in best[:8]],
                      "lines_removed_that_earlier_fix_added": overlap, "again_style_messages": again,
                      "zones": sorted(zones)},
            commits=[e["hash"] for e in best[:8]], diffs=[(e["hash"], e["path"]) for e in best[:4]],
            paths=[path], symbols=[uid],
        )


@candidate("reopened_same_symbol_fix_cadence")
def reopened_same_symbol_fix_cadence(ctx: Ctx):
    """Alive symbol with >=4 real fix commits over >=3 months or by >=3 authors, fix share of its edits >=0.4 and
    >=40% of those fixes changing >2 lines in that symbol (per-symbol counts from ``diff_lines.symbol``; whole-file
    counts only for commits with no symbol-attributed lines)."""
    edits = _cache(ctx, "symedits", lambda: {r["uid"]: r["n"] for r in ctx.q(
        "SELECT uid, count(*) n FROM symbol_events WHERE change IN ('modified','created') GROUP BY uid")})
    for uid, evs in ctx.fixes_by_uid.items():
        sym = ctx.symbols.get(uid)
        if sym is None or not sym["alive"]:
            continue
        if _skip_path(sym["path"]):
            ctx.drop("tests_or_generated")
            continue
        if sym["kind"] == "variable" or (sym["name"] or "").isupper():
            ctx.drop("table_or_constant")
            continue
        if re.search(r"(?i)dispatch|registry|route_map|mapping|handlers?$", sym["name"] or ""):
            ctx.drop("dispatcher")
            continue
        fixes = {}
        for e in evs:
            c = ctx.by_hash.get(e["hash"])
            if c and not c["is_bulk"] and not c["is_merge"] and is_fix_message(c["message"]):
                fixes[e["hash"]] = e
            elif c:
                ctx.drop("typo_or_non_fix")
        if len(fixes) < 4:
            continue
        cs = sorted((ctx.by_hash[h] for h in fixes), key=lambda c: c["ts"])
        span = (cs[-1]["ts"] - cs[0]["ts"]) / DAY
        authors = {c["author_email"] for c in cs}
        if span < 90 and len(authors) < 3:
            continue
        share = len(fixes) / max(edits.get(uid, 0), len(fixes))
        if share < 0.4:
            ctx.drop("low_fix_share")
            continue
        sizes = {h: sum(_symbol_lines(ctx, h, e["path"], uid)) for h, e in fixes.items()}
        big = [h for h, n in sizes.items() if n > 2]
        if len(big) < 0.4 * len(fixes):
            ctx.drop("mostly_tiny_fixes")
            continue
        recent = [c["hash"] for c in cs[-5:]]
        largest = sorted(sizes, key=lambda h: -sizes[h])[:2]
        shown = list(dict.fromkeys([*recent, *largest]))
        yield Hit(
            subject=uid, score=len(fixes) * share * (1 + span / 365) + len(authors),
            title=f"{sym['qualname']}: {len(fixes)} fixes over {round(span / 30)} months by {len(authors)} authors",
            evidence={"fix_count": len(fixes), "fix_share": round(share, 2), "span_days": round(span),
                      "authors": len(authors), "callers": len(ctx.callers(uid)),
                      "recent_fixes": [{"hash": h, "subject": fixes[h]["subject"]} for h in recent]},
            commits=shown, diffs=[(h, fixes[h]["path"]) for h in shown if h in fixes], paths=[sym["path"]],
            symbols=[uid],
        )


# ---------------------------------------------------------------------------- resurrected code
@candidate("resurrected_deleted_code")
def resurrected_deleted_code(ctx: Ctx):
    """A symbol deleted then re-created >=3 days later with the same body (body-hash equality, else line-set
    Jaccard >=0.85 between the lines removed at deletion and added at re-creation), >=10 lines and >=1 commit in
    between. The Jaccard is taken over the symbol's own lines (``diff_lines.symbol`` of the deleted symbol and symbols
    nested in it; likewise for the re-created one); it falls back to every line of the file in that commit when a side
    has no symbol-attributed lines."""
    evs = _cache(ctx, "lifeevents", lambda: ctx.q(
        "SELECT hash, uid, path, qualname, kind, change, body_hash_before, body_hash_after, ts, seq FROM symbol_events "
        "WHERE change IN ('created','deleted') ORDER BY seq"))
    dels: dict[tuple, list] = defaultdict(list)
    dels_by_body: dict[str, list] = defaultdict(list)
    out = []
    for e in evs:
        if e["change"] == "deleted":
            dels[(e["qualname"], e["kind"])].append(e)
            if e["body_hash_before"]:
                dels_by_body[e["body_hash_before"]].append(e)
            continue
        cr = ctx.by_hash.get(e["hash"])
        cands = [d for d in dels.get((e["qualname"], e["kind"]), []) if d["seq"] < e["seq"]]
        cands += [d for d in dels_by_body.get(e["body_hash_after"] or "-", []) if d["seq"] < e["seq"]]
        if not cands or cr is None:
            continue
        d = max(cands, key=lambda r: r["seq"])
        dc = ctx.by_hash.get(d["hash"])
        if dc is None:
            continue
        if cr["is_merge"] or dc["is_merge"] or cr["is_revert"] or REVERT_RX.search(cr["message"] or "") \
                or "cherry picked from" in (cr["message"] or ""):
            ctx.drop("merge_revert_or_cherrypick")
            continue
        if _skip_path(e["path"]):
            ctx.drop("generated_or_test")
            continue
        if e["seq"] - d["seq"] < 2 or e["ts"] - d["ts"] < 3 * DAY:
            ctx.drop("too_soon")
            continue
        if d["path"] != e["path"] and e["seq"] - d["seq"] <= 3:
            ctx.drop("rename_or_move")
            continue
        sym = ctx.symbols.get(e["uid"])
        same_body = bool(d["body_hash_before"]) and d["body_hash_before"] == e["body_hash_after"]
        sim = 1.0
        if not same_body:
            drows = [r for r in ctx.lines_of(d["hash"], d["path"]) if r["sign"] == "-"]
            erows = [r for r in ctx.lines_of(e["hash"], e["path"]) if r["sign"] == "+"]
            srem = [r for r in drows if r["symbol"] and _in_symbol(r["symbol"], d["uid"])]
            sadd = [r for r in erows if r["symbol"] and _in_symbol(r["symbol"], e["uid"])]
            rem = {_norm(r["text"]) for r in (srem or drows)}
            add = {_norm(r["text"]) for r in (sadd or erows)}
            rem = {t for t in rem if not _trivial(t, 3)}
            add = {t for t in add if not _trivial(t, 3)}
            if not rem or not add:
                ctx.drop("no_line_data")
                continue
            sim = len(rem & add) / len(rem | add)
            if sim < 0.85:
                ctx.drop("body_changed")
                continue
        loc = (sym["loc"] if sym and sym["loc"] else 0) or 0
        if loc < 10:
            ctx.drop("too_small")
            continue
        between = [c for c in ctx.path_commits(d["path"]) if d["seq"] < c["seq"] < e["seq"] and ctx.is_fix(c)]
        out.append(Hit(
            subject=f"{e['uid']}|{d['hash'][:10]}|{e['hash'][:10]}",
            score=loc * 0.05 + len(between) * 3 + (e["ts"] - d["ts"]) / DAY / 100,
            title=f"{e['qualname']} deleted ({subject_of(dc['message'])[:50]}) and back "
                  f"{round((e['ts'] - d['ts']) / DAY)} days later",
            evidence={"deleted_in": d["hash"], "recreated_in": e["hash"], "similarity": round(sim, 2),
                      "same_body_hash": same_body, "loc": loc, "fixes_in_between": len(between),
                      "deleted_path": d["path"], "recreated_path": e["path"]},
            commits=[d["hash"], e["hash"], *[c["hash"] for c in between[:3]]],
            diffs=[(d["hash"], d["path"]), (e["hash"], e["path"])], paths=sorted({d["path"], e["path"]}),
            symbols=[e["uid"]],
        ))
    yield from sorted(out, key=lambda h: -h.score)[:300]


# ---------------------------------------------------------------------------- https redirect flip
HTTPS_RX = r"SECURE_SSL_REDIRECT|force-ssl-redirect|redirect-to-https|UseHttpsRedirection"
_DEV_PATH = re.compile(r"(?i)(^|[/_.-])(dev|devel|development|local|test|tests|testing|example|examples|sample)"
                       r"([/_.-]|$)")
_PROXY_RX = (r"SECURE_PROXY_SSL_HEADER|X-Forwarded-Proto|ProxyFix|trust[_-]?proxy|use-forwarded-headers|"
             r"forwarded-allow-ips|SECURE_REDIRECT_EXEMPT|proxy_headers")
_LOOP_MSG = re.compile(r"(?i)\b(redirect(ion)?|loop|health-?\s?check|too many redirects|infinite)\b")


def _https_state(text: str) -> str:
    s = text.strip()
    if s.startswith(("#", "//")):
        return "off"
    m = re.search(r"[:=]\s*[\"']?(\w+)", s)
    if m and m.group(1).lower() in ("false", "0", "off", "no", "none", "disabled"):
        return "off"
    return "on"


@candidate("https_scheme_redirect_flip")
def https_scheme_redirect_flip(ctx: Ctx):
    """Per file, track the on/off state of the HTTPS-redirect key (added value, comment-out or removal = off). A
    flip-flop (two state changes <=15 commits apart) or a flip followed within 10 commits by a loop/redirect/
    healthcheck message qualifies. Proxy-trust settings found anywhere in history are attached."""
    by_path: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in ctx.grep(HTTPS_RX, None):
        by_path[r["path"]][r["hash"]].append(r)
    pos = {c["hash"]: i for i, c in enumerate(ctx.commits)}
    proxy = None
    for path, per_commit in by_path.items():
        if is_docs(path):
            ctx.drop("docs")
            continue
        if _DEV_PATH.search(path):
            ctx.drop("dev_or_test_profile")
            continue
        timeline: list[tuple[int, str, str]] = []
        for h, rows in per_commit.items():
            adds = [r for r in rows if r["sign"] == "+"]
            st = _https_state(adds[-1]["text"]) if adds else "off"
            timeline.append((rows[0]["seq"], h, st))
        timeline.sort()
        flips = [(timeline[i][0], timeline[i][1], timeline[i][2]) for i in range(1, len(timeline))
                 if timeline[i][2] != timeline[i - 1][2]]
        reason = None
        trigger = None
        for i in range(1, len(flips)):
            if flips[i][0] - flips[i - 1][0] <= 15:
                reason, trigger = "flip_flop", (flips[i - 1], flips[i])
                break
        if reason is None:
            for f in flips:
                i0 = pos.get(f[1], 0)
                follow = [c for c in ctx.commits[i0 + 1:i0 + 11] if _LOOP_MSG.search(c["message"] or "")]
                if follow:
                    reason, trigger = "followed_by_loop_message", (f, (follow[0]["seq"], follow[0]["hash"], ""))
                    break
        if reason is None:
            continue
        if proxy is None:
            proxy = [{"hash": r["hash"], "path": r["path"], "text": r["text"].strip()[:100]}
                     for r in ctx.grep(_PROXY_RX, "+", limit=400) if not is_docs(r["path"])][:6]
        hs = [t[1] for t in trigger]
        yield Hit(
            subject=path, score=len(flips) * 2 + (3 if reason == "flip_flop" else 1.5),
            title=f"HTTPS redirect setting flipped {len(flips)}x in {path} ({reason.replace('_', ' ')})",
            evidence={"reason": reason, "states": [{"hash": h[:10], "state": s} for _, h, s in timeline][:10],
                      "proxy_trust_settings": proxy},
            commits=list(dict.fromkeys([*hs, *[t[1] for t in flips[:4]]])),
            diffs=[(h, path) for h in hs if h in per_commit], paths=[path],
        )


# ---------------------------------------------------------------------------- revert of revert / reland
@candidate("revert_of_revert_or_reapplied_change")
def revert_of_revert_or_reapplied_change(ctx: Ctx):
    """Revert R of C followed (<=90 days) by C2 that re-lands it: a revert of R, an exact re-application, or a
    modified re-application.

    Exact: C2 has the same real ``git patch-id`` as C (``ctx.patch_ids`` / ``commits_by_patch_id``), found even when it
    touches other paths; reported with ``exact_patch_id`` and no delta. Modified: a Reland/Reapply subject, or >=50% of
    C's added lines (whitespace-insensitive) / shared symbols, with a non-whitespace delta of >=3 lines against C
    (diff_lines line sets; a changed patch has a different patch-id by definition). Fallback: when C or C2 has no
    patch id (not yet enriched), an identical line set is dropped as ``identical_or_whitespace_only`` as before."""
    lc_added = lambda h: {_WS.sub("", r["text"]) for r in ctx.lines_of(h) if r["sign"] == "+"
                          and not _trivial(_norm(r["text"]), 4)}  # noqa: E731
    reverted_by: dict[str, list[str]] = defaultdict(list)
    for r in ctx.reverts:
        reverted_by[r["reverted"]].append(r["reverter"])
    seen_pairs: set[str] = set()
    for r in ctx.reverts:
        R, C = ctx.by_hash.get(r["reverter"]), ctx.by_hash.get(r["reverted"])
        if R is None or C is None:
            continue
        for rr in reverted_by.get(R["hash"], []):
            RR = ctx.by_hash.get(rr)
            if RR is None or f"{C['hash']}|{RR['hash']}" in seen_pairs:
                continue
            seen_pairs.add(f"{C['hash']}|{RR['hash']}")
            yield Hit(
                subject=f"{C['hash']}|{R['hash']}|{RR['hash']}", score=6 + min(5, (RR["ts"] - R["ts"]) / DAY / 30),
                title=f"Revert of a revert: {subject_of(C['message'])[:60]} was reverted then reinstated",
                evidence={"original": C["hash"], "revert": R["hash"], "revert_of_revert": RR["hash"],
                          "days_reverted": round((RR["ts"] - R["ts"]) / DAY, 1)},
                commits=[C["hash"], R["hash"], RR["hash"]],
                diffs=[(C["hash"], fc["path"]) for fc in ctx.changes(C["hash"])[:2]],
                paths=[fc["path"] for fc in ctx.changes(C["hash"])[:6]],
            )
        paths = [fc["path"] for fc in ctx.changes(C["hash"])]
        if paths and all(DEP_MANIFEST_RX.search(p) or LOCKFILE_RX.search(p) for p in paths):
            ctx.drop("version_bump")
            continue
        c_lines = lc_added(C["hash"])
        if len(c_lines) < 3:
            ctx.drop("tiny_or_unindexed_original")
            continue
        c_syms = {e["uid"] for e in ctx.events_by_commit.get(C["hash"], [])}
        cands: dict[str, object] = {}
        for p in paths[:25]:
            for c2 in ctx.path_commits(p):
                if R["seq"] < c2["seq"] and c2["ts"] - R["ts"] <= 90 * DAY and not c2["is_merge"] \
                        and c2["hash"] not in (C["hash"], R["hash"]) and not c2["is_revert"]:
                    cands[c2["hash"]] = c2
        pid_c = ctx.patch_ids.get(C["hash"])
        exact: set[str] = set()
        if pid_c:
            for h2 in ctx.commits_by_patch_id.get(pid_c, []):
                c2 = ctx.by_hash.get(h2)
                if c2 is not None and h2 not in (C["hash"], R["hash"]) and 0 < c2["ts"] - R["ts"] <= 90 * DAY and not c2["is_merge"] and not c2["is_revert"]:
                    cands[h2] = c2
                    exact.add(h2)
        for c2 in cands.values():
            lines2 = lc_added(c2["hash"])
            overlap = len(c_lines & lines2) / len(c_lines)
            shared = c_syms & {e["uid"] for e in ctx.events_by_commit.get(c2["hash"], [])}
            msg = bool(_REAPPLY_MSG.search(c2["message"] or ""))
            is_exact = c2["hash"] in exact
            if not (is_exact or msg or overlap >= 0.5 or (shared and overlap >= 0.25)):
                continue
            delta = 0 if is_exact else len(lines2 ^ c_lines)
            key = f"{C['hash']}|{R['hash']}|{c2['hash']}"
            if key in seen_pairs:
                continue
            if delta == 0 and not is_exact:
                ctx.drop("identical_or_whitespace_only")
                continue
            if delta < 3 and not is_exact:
                ctx.drop("trivial_delta")
                continue
            if all(DEP_MANIFEST_RX.search(fc["path"]) or LOCKFILE_RX.search(fc["path"]) for fc in ctx.changes(c2["hash"])):
                ctx.drop("version_bump")
                continue
            seen_pairs.add(key)
            yield Hit(
                subject=key, score=3 + overlap * 4 + (2 if msg else 0) + min(4, delta / 10) + (3 if is_exact else 0),
                title=f"{subject_of(C['message'])[:60]} reverted, then reapplied "
                      + ("unchanged (same patch-id)" if is_exact else f"with {delta} changed lines"),
                evidence={"original": C["hash"], "revert": R["hash"], "reapplied_by": c2["hash"],
                          "exact_patch_id": is_exact,
                          "overlap": round(overlap, 2), "delta_lines": delta, "shared_symbols": sorted(shared)[:5],
                          "days_after_revert": round((c2["ts"] - R["ts"]) / DAY, 1), "reland_message": msg},
                commits=[C["hash"], R["hash"], c2["hash"]],
                diffs=[(C["hash"], paths[0]), (c2["hash"], paths[0])] if paths else [], paths=paths[:6],
                symbols=sorted(shared)[:4],
            )


# ---------------------------------------------------------------------------- wip / trial and error
_TRY_MSG = re.compile(r"(?i)\b(try(ing)?|attempt\w*|again|still\s+(fail|broke|not)\w*|retry|hopefully|maybe|wip|"
                      r"another\s+try|one\s+more|test(ing)?\s+(ci|build)|trial)\b")
_BUMP_MSG = re.compile(r"(?i)\b(bump|release|version|prepare\s+\d|changelog)\b")
_INFRA = re.compile(r"(?i)(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml)$")


@candidate("wip_trial_and_error_sequence")
def wip_trial_and_error_sequence(ctx: Ctx):
    """One branch, >=3 commits (2 for CI/Dockerfile/infra) touching the same file where consecutive commits change the
    same symbol (``diff_lines.symbol``) and either an A->B->A blob return or >=2 try/again/still-fail messages. Ranked
    by oscillations. "One branch" is ``ctx.branch_of`` (commits unique to a branch, any author, any time span) when the
    first commit's branch is known, and then only commits of that branch are grouped. Fallback for commits with no
    known branch: same author within 7 days, grouping only other branch-less commits. When either commit of a pair has
    no symbol-attributed lines (module-level or non-code files such as Dockerfiles/CI), overlap falls back to the
    changed line ranges (within 3 lines)."""
    from .base import IAC_RX, is_ci
    branch_of = ctx.branch_of
    for path, rows in ctx.changes_by_path.items():
        if len(rows) < 2 or is_generated(path) or LOCKFILE_RX.search(path):
            continue
        infra = is_ci(path) or bool(_INFRA.search(path)) or bool(IAC_RX.search(path))
        need = 2 if infra else 3
        i = 0
        while i < len(rows):
            c0 = ctx.by_hash.get(rows[i]["hash"])
            if c0 is None:
                i += 1
                continue
            b0 = branch_of.get(c0["hash"])
            window = []
            last_idx = i
            for k, r in enumerate(rows[i:], start=i):
                c = ctx.by_hash.get(r["hash"])
                if c is None or c["is_merge"] or c["is_bulk"]:
                    continue
                if branch_of.get(c["hash"]) != b0:
                    continue  # interleaved work from another branch (or from branch-less history)
                if not b0:
                    if c["ts"] - c0["ts"] > 7 * DAY:
                        break
                    if c["author_email"] != c0["author_email"]:
                        break
                window.append((r, c))
                last_idx = k
            if len(window) < need:
                i += 1
                continue
            i = last_idx + 1
            if all(_BUMP_MSG.search(c["message"] or "") for _, c in window):
                ctx.drop("version_bumps")
                continue
            # overlapping hunk ranges between consecutive commits
            ranges, syms = [], []
            for r, c in window:
                changed = ctx.lines_of(c["hash"], path)  # not `rows`: the outer loop iterates that
                ls = [x["line"] for x in changed if x["line"]]
                ranges.append((min(ls), max(ls)) if ls else None)
                syms.append({x["symbol"] for x in changed if x["symbol"]})
            overlapping = 0
            for (a, b), (sa, sb) in zip(zip(ranges, ranges[1:]), zip(syms, syms[1:])):
                if sa and sb:
                    overlapping += bool(sa & sb)
                elif a and b and a[0] <= b[1] + 3 and b[0] <= a[1] + 3:
                    overlapping += 1
            if overlapping < need - 1:
                ctx.drop("no_overlapping_hunks")
                continue
            osc = 0
            for a in range(len(window)):
                for b in range(a + 1, len(window)):
                    if window[b][0]["new_blob"] and window[b][0]["new_blob"] == window[a][0]["old_blob"]:
                        osc += 1
            tries = sum(1 for _, c in window if _TRY_MSG.search(c["message"] or ""))
            if osc == 0 and tries < 2:
                ctx.drop("no_oscillation_or_try_messages")
                continue
            adds, rems = set(), set()
            for _, c in window:
                for x in ctx.lines_of(c["hash"], path):
                    (adds if x["sign"] == "+" else rems).add(_WS.sub("", x["text"]))
            if adds == rems:
                ctx.drop("whitespace_flip")
                continue
            hs = [c["hash"] for _, c in window]
            yield Hit(
                subject=f"{path}|{hs[0]}", score=osc * 3 + tries * 1.5 + len(window),
                title=f"{len(window)} commits in {round((window[-1][1]['ts'] - window[0][1]['ts']) / 3600)}h "
                      f"fiddling with {path}" + (f" ({osc} reverts to an earlier state)" if osc else ""),
                evidence={"commits": [{"hash": c["hash"], "subject": subject_of(c["message"])} for _, c in window[:8]],
                          "oscillations": osc, "trial_messages": tries, "infra_file": infra,
                          "branch": b0 or None},
                commits=hs[:8], diffs=[(h, path) for h in hs[:4]], paths=[path],
            )


# ---------------------------------------------------------------------------- missing co-change partner
_FORGOT_MSG = re.compile(r"(?i)\b(forgot|missed|missing|also|follow-?up|oops|should have|left out|complete)\b")


@candidate("missing_cochange_partner")
def missing_cochange_partner(ctx: Ctx):
    """Rule A=>B (confidence >=0.8, support >=5) with >=2 violations: a commit touching A without B followed within
    14 days by a B-only commit by the same author. Message wording only boosts the score."""
    touch: dict[str, list] = {}

    def commits_of(p: str):
        if p not in touch:
            touch[p] = [c for c in ctx.path_commits(p) if not c["is_merge"] and not c["is_bulk"]]
        return touch[p]

    out = []
    for row in ctx.cochange:
        if row["count"] < 5:
            continue
        for a, b, conf in ((row["a"], row["b"], row["conf_ab"]), (row["b"], row["a"], row["conf_ba"])):
            if conf < 0.8:
                continue
            if row["structural_link"]:
                ctx.drop("explicit_link")
                continue
            if is_generated(b) or LOCKFILE_RX.search(b):
                ctx.drop("generated_partner")
                continue
            if is_docs(b) or is_docs(a):
                ctx.drop("docs_or_changelog")
                continue
            if is_test(b) or is_test(a):
                ctx.drop("test_subject_pair")
                continue
            ca, cb = commits_of(a), commits_of(b)
            sb = {c["hash"] for c in cb}
            sa = {c["hash"] for c in ca}
            b_only = [c for c in cb if c["hash"] not in sa]
            b_ts = [c["ts"] for c in b_only]
            viol = []
            for x in ca:
                if x["hash"] in sb:
                    continue
                k = bisect.bisect_right(b_ts, x["ts"])
                for y in b_only[k:k + 6]:
                    if y["ts"] - x["ts"] > 14 * DAY:
                        break
                    if y["author_email"] == x["author_email"] and y["seq"] > x["seq"]:
                        viol.append((x, y))
                        break
            if len(viol) < 2:
                continue
            boost = sum(1 for _, y in viol if _FORGOT_MSG.search(y["message"] or ""))
            out.append(Hit(
                subject=f"{a}=>{b}", score=len(viol) * conf * 2 + boost + math.log(row["count"]),
                title=f"{a} usually changes with {b} ({row['count']}x, conf {conf:.2f}) but {len(viol)} edits forgot it",
                evidence={"rule": f"{a} => {b}", "confidence": round(conf, 2), "support": row["count"],
                          "violations": [{"without_partner": x["hash"], "partner_later": y["hash"],
                                          "later_subject": subject_of(y["message"])} for x, y in viol[:5]],
                          "wording_boost": boost},
                commits=[h for x, y in viol[:4] for h in (x["hash"], y["hash"])],
                diffs=[(viol[0][0]["hash"], a), (viol[0][1]["hash"], b)], paths=[a, b],
            ))
    yield from sorted(out, key=lambda h: -h.score)[:300]


# ---------------------------------------------------------------------------- removed guard then regression
@candidate("removed_guard_then_regression")
def removed_guard_then_regression(ctx: Ctx):
    """Strong form: fix F adds a guard line (fix_events.guards), a later commit R removes a >=0.8-similar line in the
    same file (not re-adding it in the same commit), and a later fix F2 within 365 days of R re-adds a similar line."""
    rows_cache: dict[str, list] = {}

    def path_rows(path: str):
        if path not in rows_cache:
            rows_cache[path] = [(r["seq"], r["hash"], r["ts"], r["sign"], _norm(r["text"])) for r in ctx.q(
                "SELECT seq, hash, ts, sign, text FROM diff_lines WHERE path=? ORDER BY seq", (path,))]
        return rows_cache[path]

    seen = set()
    for f in ctx.fix_events:
        c = ctx.by_hash.get(f["hash"])
        if c is None or not is_fix_message(c["message"]) or _skip_path(f["path"]):
            continue
        for g in json.loads(f["guards"] or "[]")[:6]:
            gl = _norm(g.get("line", ""))
            if gl.startswith(("#", "//", "*", "/*")):
                ctx.drop("comment_guard")
                continue
            if len(gl) < 10:
                ctx.drop("whitespace_or_trivial_guard")
                continue
            key = (f["path"], gl)
            if key in seen:
                continue
            rows = path_rows(f["path"])
            later = [r for r in rows if r[0] > c["seq"] and r[1] != f["hash"]]
            removal = next((r for r in later if r[3] == "-" and _sim(gl, r[4]) >= 0.8), None)
            if removal is None:
                continue
            if any(r[1] == removal[1] and r[3] == "+" and _sim(gl, r[4]) >= 0.8 for r in later):
                ctx.drop("moved_in_same_commit")
                continue
            F2 = next((r for r in later if r[3] == "+" and r[0] > removal[0] and r[2] - removal[2] <= 365 * DAY
                       and _sim(gl, r[4]) >= 0.8 and is_fix_message(ctx.by_hash[r[1]]["message"])), None)
            if F2 is None:
                continue
            seen.add(key)
            Rc = ctx.by_hash[removal[1]]
            yield Hit(
                subject=f"{f['path']}|{f['hash'][:10]}|{removal[1][:10]}|{F2[1][:10]}",
                score=6 + min(4, (F2[2] - removal[2]) / DAY / 90),
                title=f"Guard added by fix, removed by '{subject_of(Rc['message'])[:50]}', fixed again",
                evidence={"guard": gl[:140], "guard_kind": g.get("kind"), "added_in": f["hash"],
                          "removed_in": removal[1], "readded_in": F2[1], "symbol": f["uid"]},
                commits=[f["hash"], removal[1], F2[1]],
                diffs=[(f["hash"], f["path"]), (removal[1], f["path"]), (F2[1], f["path"])], paths=[f["path"]],
                symbols=[f["uid"]],
            )


# ---------------------------------------------------------------------------- SZZ
_ORIGIN_SKIP = re.compile(r"(?i)\b(reformat\w*|format(ting)?|black|isort|prettier|autopep8|rename[sd]?|renaming|"
                          r"move[sd]?\s+(files?|to|into|from)|relocat\w+|lint|style)\b")


@candidate("szz_bug_introducing_commit")
def szz_bug_introducing_commit(ctx: Ctx):
    """SZZ for fixes in security/race/regression/performance zones: each removed non-trivial line of the fix is traced
    to the commit that last wrote it, from ``line_origins`` (git blame of the fix's parent). Only when blame holds
    nothing usable for that fix and file (not blamed yet, or every line unmapped) does it fall back to the latest
    earlier commit that added an identical whitespace-normalised line in the file; ``evidence.method`` says which was
    used. One origin must explain >=50% of the eligible lines and have lived >=14 days; formatter/rename/bulk origins
    are dropped."""
    seen: set[tuple[str, str]] = set()
    for f in ctx.fix_events:
        zones = [k for k in json.loads(f["kinds"] or "[]") if k in ("security", "race", "regression", "performance")]
        if not zones or (f["hash"], f["path"]) in seen:
            continue
        seen.add((f["hash"], f["path"]))
        if _skip_path(f["path"]):
            continue
        c = ctx.by_hash.get(f["hash"])
        if c is None or c["is_bulk"] or c["is_merge"]:
            continue
        res = _szz(ctx, f["hash"], f["path"])
        if not res:
            continue
        if "origin" not in res:
            if res.get("reason") == "comment_or_import_only":
                ctx.drop("comment_or_import_only_fix")
            continue
        o = ctx.by_hash[res["origin"]]
        if o["is_bulk"] or o["is_merge"] or _ORIGIN_SKIP.search(o["message"] or ""):
            ctx.drop("bulk_rename_format_origin")
            continue
        if res["share"] < 0.5:
            ctx.drop("no_dominant_origin")
            continue
        if res["lifetime_days"] < 14:
            ctx.drop("short_lived_origin")
            continue
        yield Hit(
            subject=f"{res['origin']}|{f['hash']}|{f['path']}",
            score=res["n"] * (1 + 0.5 * len(zones)) * math.log(2 + res["lifetime_days"], 10) + res["share"] * 3,
            title=f"{', '.join(zones)} fix {f['hash'][:10]} traced to {res['origin'][:10]} "
                  f"({subject_of(o['message'])[:50]}), {round(res['lifetime_days'])} days earlier",
            evidence={"zones": zones, "fix_subject": f["subject"], "origin_subject": subject_of(o["message"]),
                      "lines_traced": res["n"], "lines_eligible": res["eligible"], "share": round(res["share"], 2),
                      "lifetime_days": round(res["lifetime_days"]), "sample_lines": res["lines"][:4],
                      "method": res["method"], "origin_author": o["author_name"]},
            commits=[res["origin"], f["hash"]], diffs=[(res["origin"], f["path"]), (f["hash"], f["path"])],
            paths=[f["path"]], symbols=[f["uid"]],
        )


# ---------------------------------------------------------------------------- hubs
_HUB_NOISE = re.compile(r"(?i)(^|[/_.])(log|logger|logging|utils?|helpers?|const|consts|constants|compat)([/_.]|$)")


@candidate("fix_prone_high_fan_in_hub")
def fix_prone_high_fan_in_hub(ctx: Ctx):
    """Alive symbol with fan-in >= max(15, p95) from >=4 directories, >=3 fixes, and at least one fix followed
    within 30 days by edits in >=2 of its caller files."""
    fan = {u: len(ctx.callers(u)) for u in ctx.calls[1]}
    vals = sorted(v for v in fan.values() if v > 0)
    p95 = vals[int(0.95 * (len(vals) - 1))] if vals else 15
    thresh = max(15, p95)
    for uid, evs in ctx.fixes_by_uid.items():
        sym = ctx.symbols.get(uid)
        if sym is None or not sym["alive"] or fan.get(uid, 0) < thresh:
            continue
        if _skip_path(sym["path"]):
            ctx.drop("test_generated")
            continue
        if _HUB_NOISE.search(sym["path"]) or _HUB_NOISE.search(sym["name"] or "") or (sym["name"] or "").isupper():
            ctx.drop("logging_util_constants")
            continue
        callers = [ctx.symbols[c] for c in ctx.callers(uid) if c in ctx.symbols]
        cfiles = {c["path"] for c in callers if not is_test(c["path"])}
        dirs = {p.rsplit("/", 1)[0] if "/" in p else "" for p in cfiles}
        if len(dirs) < 4:
            ctx.drop("callers_in_few_dirs")
            continue
        fixes = sorted({e["hash"]: e for e in evs}.values(), key=lambda r: r["ts"])
        if len(fixes) < 3:
            continue
        followed = []
        for e in fixes:
            touched = {p for p in cfiles if p != e["path"] and any(
                e["ts"] < ctx.by_hash[r["hash"]]["ts"] <= e["ts"] + 30 * DAY
                for r in ctx.changes_by_path.get(p, []) if r["hash"] in ctx.by_hash)}
            if len(touched) >= 2:
                followed.append((e, sorted(touched)))
        if not followed:
            ctx.drop("no_caller_ripple")
            continue
        per_dir: dict[str, list[str]] = defaultdict(list)
        for p in sorted(cfiles):
            per_dir[p.rsplit("/", 1)[0] if "/" in p else ""].append(p)
        sample = [ps[0] for ps in list(per_dir.values())[:8]]
        yield Hit(
            subject=uid, score=len(fixes) * 2 + fan[uid] * 0.3 + len(followed) * 3 + len(dirs),
            title=f"{sym['qualname']}: {fan[uid]} callers in {len(dirs)} dirs, {len(fixes)} fixes, "
                  f"{len(followed)} followed by caller edits",
            evidence={"fan_in": fan[uid], "caller_dirs": len(dirs), "fix_count": len(fixes),
                      "fixes_with_caller_ripple": [{"hash": e["hash"], "subject": e["subject"], "caller_files": t[:5]}
                                                   for e, t in followed[:4]],
                      "caller_sample": sample},
            commits=[e["hash"] for e, _ in followed[:4]] + [e["hash"] for e in fixes[-2:]],
            diffs=[(e["hash"], e["path"]) for e, _ in followed[:3]], paths=[sym["path"], *sample[:4]], symbols=[uid],
        )


# ---------------------------------------------------------------------------- regression after refactor
@candidate("regression_after_refactor")
def regression_after_refactor(ctx: Ctx):
    """A fix within 60 days of its SZZ origin (blame via ``line_origins``, text-match fallback; see
    szz_bug_introducing_commit), where the origin looks like a refactor (refactor-style message or a
    moved/extracted symbol event) with >=10 behavioural added lines. Identifier-only renames (lines identical once
    identifiers are masked), pure moves, formatters and bulk commits are dropped."""
    ident = re.compile(r"[A-Za-z_]\w*")
    seen: set[tuple[str, str]] = set()
    for hash_, path in _fix_event_pairs(ctx):
        if (hash_, path) in seen or _skip_path(path):
            continue
        seen.add((hash_, path))
        res = _szz(ctx, hash_, path)
        if not res or "origin" not in res or res["share"] < 0.34:
            continue
        o, fx = ctx.by_hash[res["origin"]], ctx.by_hash[hash_]
        if fx["ts"] - o["ts"] > 60 * DAY:
            continue
        if o["is_bulk"] or o["is_merge"]:
            ctx.drop("bulk_or_merge_origin")
            continue
        if _FORMAT_MSG.search(o["message"] or "") and not _REFACTOR_MSG.search(o["message"] or ""):
            ctx.drop("formatter_origin")
            continue
        moved = [e for e in ctx.events_by_commit.get(o["hash"], []) if e["change"] in ("moved", "created")
                 and e["path"] == path]
        is_refactor = bool(_REFACTOR_MSG.search(o["message"] or "")) or any(e["change"] == "moved" for e in moved)
        if not is_refactor:
            continue
        adds, rems = [], []
        for r in ctx.lines_of(o["hash"], path):
            n = _norm(r["text"])
            if _trivial(n, 4):
                continue
            (adds if r["sign"] == "+" else rems).append(n)
        if len(adds) < 10:
            ctx.drop("too_few_behavioural_lines")
            continue
        if sorted(ident.sub("X", t) for t in adds) == sorted(ident.sub("X", t) for t in rems):
            ctx.drop("identifier_only_rename")
            continue
        if rems and len(set(adds) & set(rems)) / max(len(set(adds)), 1) >= 0.95:
            ctx.drop("pure_move")
            continue
        yield Hit(
            subject=f"{o['hash']}|{hash_}|{path}",
            score=res["n"] + len(adds) * 0.1 + (60 - (fx["ts"] - o["ts"]) / DAY) / 20,
            title=f"Fix {hash_[:10]} undoes damage from refactor {o['hash'][:10]} ({subject_of(o['message'])[:50]}) "
                  f"{round((fx['ts'] - o['ts']) / DAY)} days later",
            evidence={"refactor_subject": subject_of(o["message"]), "fix_subject": subject_of(fx["message"]),
                      "origin_share": round(res["share"], 2), "method": res["method"], "lines_traced": res["n"], "added_lines_in_refactor": len(adds),
                      "sample_lines": res["lines"][:4]},
            commits=[o["hash"], hash_], diffs=[(o["hash"], path), (hash_, path)], paths=[path],
        )


# ---------------------------------------------------------------------------- surprise defect in a quiet file
@candidate("surprise_defect_fix_in_quiet_file")
def surprise_defect_fix_in_quiet_file(ctx: Ctx):
    """A fix (>=3 code lines) on file F untouched for >=12 months where a linked file G (import edge, co-change
    pair or shared contract key) changed in the preceding 30 days and the fix references G (its module name or one of
    its symbols) or adds a test that does. At most 2 G per fix."""
    lc = _line_counts(ctx)
    ts_list = [c["ts"] for c in ctx.commits]
    cochange_of: dict[str, set[str]] = defaultdict(set)
    for r in ctx.cochange:
        cochange_of[r["a"]].add(r["b"])
        cochange_of[r["b"]].add(r["a"])
    keys_by_path: dict[str, set[tuple]] = defaultdict(set)
    paths_by_key: dict[tuple, set[str]] = defaultdict(set)
    for r in ctx.contracts:
        keys_by_path[r["path"]].add((r["kind"], r["key"]))
        paths_by_key[(r["kind"], r["key"])].add(r["path"])
    imp_out, imp_in = ctx.imports
    for hash_, path in _fix_event_pairs(ctx):
        if _skip_path(path) or is_docs(path) or not is_code(path):
            ctx.drop("non_code_or_test")
            continue
        fx = ctx.by_hash[hash_]
        a, d = lc.get((hash_, path), (0, 0))
        if a + d < 3:
            ctx.drop("tiny_fix")
            continue
        prior = [c for c in ctx.path_commits(path) if c["seq"] < fx["seq"] and c["hash"] != hash_]
        if not prior or fx["ts"] - prior[-1]["ts"] < 365 * DAY:
            continue
        linked: dict[str, str] = {}
        for g in set(imp_out.get(path, ())) | set(imp_in.get(path, ())):
            linked.setdefault(g, "import")
        for g in cochange_of.get(path, ()):
            linked.setdefault(g, "cochange")
        for k in keys_by_path.get(path, ()):
            for g in paths_by_key[k]:
                linked.setdefault(g, "contract")
        linked.pop(path, None)
        if not linked:
            continue
        lo = bisect.bisect_left(ts_list, fx["ts"] - 30 * DAY)
        hi = bisect.bisect_left(ts_list, fx["ts"])
        recent: dict[str, str] = {}
        for c in ctx.commits[lo:hi]:
            if c["is_bulk"] or c["is_merge"] or c["hash"] == hash_:
                continue
            for fc in ctx.changes(c["hash"]):
                if fc["path"] in linked and not is_test(fc["path"]):
                    recent[fc["path"]] = c["hash"]
        if not recent:
            continue
        added_text = "\n".join(r["text"] for r in ctx.lines_of(hash_) if r["sign"] == "+")
        pairs = []
        for g, gh in recent.items():
            stem = g.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            names = {s["name"] for s in ctx.symbols_by_path.get(g, []) if s["name"] and len(s["name"]) >= 5}
            refs = [n for n in [stem, *sorted(names)[:30]] if len(n) >= 4 and re.search(rf"\b{re.escape(n)}\b", added_text)]
            if not refs:
                continue
            pairs.append((g, gh, linked[g], refs[:3]))
        if not pairs:
            ctx.drop("no_reference_to_changed_partner")
            continue
        pairs = pairs[:2]
        yield Hit(
            subject=f"{hash_}|{path}", score=len(pairs) * 2 + math.log(2 + (fx["ts"] - prior[-1]["ts"]) / DAY, 10) * 3,
            title=f"Fix to {path} after {round((fx['ts'] - prior[-1]['ts']) / DAY / 30)} quiet months, "
                  f"following a change to {pairs[0][0]}",
            evidence={"fix_subject": subject_of(fx["message"]), "dormant_days": round((fx["ts"] - prior[-1]["ts"]) / DAY),
                      "partners": [{"path": g, "changed_in": gh, "link": ln, "referenced": refs}
                                   for g, gh, ln, refs in pairs]},
            commits=[hash_, *[gh for _, gh, _, _ in pairs]],
            diffs=[(hash_, path), *[(gh, g) for g, gh, _, _ in pairs]], paths=[path, *[g for g, *_ in pairs]],
        )


# ---------------------------------------------------------------------------- relative churn spike
@candidate("relative_churn_spike")
def relative_churn_spike(ctx: Ctx):
    """For each code file a 90-day window where deleted lines are >=50% of the running size at window start (>=40
    LOC; size is rebuilt from file_changes added/deleted), >=2 non-bulk commits by >=2 authors, churn >=3x the median
    nonzero 90-day bucket of the file, and a fix touching the file within 60 days after the window."""
    for path, rows in ctx.changes_by_path.items():
        if len(rows) < 4:
            continue
        if not is_code(path) or is_test(path) or LOCKFILE_RX.search(path) or _NOISY_PATH.search(path):
            continue
        tl = []
        size = 0
        for r in rows:
            c = ctx.by_hash.get(r["hash"])
            if c is None:
                continue
            tl.append((c, size, r["added"] or 0, r["deleted"] or 0))
            size = max(0, size + (r["added"] or 0) - (r["deleted"] or 0))
        if len(tl) < 4:
            continue
        t0 = tl[0][0]["ts"]
        buckets: Counter[int] = Counter()
        for c, _, a, d in tl:
            buckets[int((c["ts"] - t0) // (90 * DAY))] += a + d
        nonzero = [v for v in buckets.values() if v]
        if not nonzero:
            continue
        med = statistics.median(nonzero) or 1
        best = None
        j = 0
        for i in range(len(tl)):
            if tl[i][1] < 40:
                continue
            j = max(j, i)
            while j + 1 < len(tl) and tl[j + 1][0]["ts"] - tl[i][0]["ts"] <= 90 * DAY:
                j += 1
            win = tl[i:j + 1]
            nb = [w for w in win if not w[0]["is_bulk"] and not w[0]["is_merge"] and not ctx.is_fix(w[0])]
            deleted = sum(w[3] for w in nb)
            churn = sum(w[2] + w[3] for w in nb)
            if deleted < 0.5 * tl[i][1] or len(nb) < 2 or len({w[0]["author_email"] for w in nb}) < 2:
                continue
            if churn < 3 * med:
                ctx.drop("churn_not_unusual")
                continue
            end = win[-1][0]["ts"]
            hs = {w[0]["hash"] for w in nb}
            fixes = [c for c, *_ in tl if c["hash"] not in hs and nb[0][0]["ts"] < c["ts"] <= end + 60 * DAY
                     and ctx.is_fix(c) and not c["is_merge"]]
            if not fixes:
                ctx.drop("no_later_fix")
                continue
            ratio = deleted / tl[i][1]
            if best is None or ratio > best[0]:
                best = (ratio, nb, churn, fixes, tl[i][1], deleted)
        if best:
            ratio, win, churn, fixes, prior, deleted = best
            hs = [w[0]["hash"] for w in win]
            yield Hit(
                subject=path, score=ratio * 3 + churn / med + len(fixes),
                title=f"{path}: {round(ratio * 100)}% of its {prior} lines deleted in a 90-day window by "
                      f"{len({w[0]['author_email'] for w in win})} authors, then {len(fixes)} fixes",
                evidence={"size_before": prior, "deleted": deleted, "churn": churn, "median_90d_churn": med,
                          "commits_in_window": len(win), "later_fixes": [{"hash": c["hash"], "subject": subject_of(c["message"])}
                                                                         for c in fixes[:4]]},
                commits=[*hs[:5], *[c["hash"] for c in fixes[:3]]],
                diffs=[(hs[0], path), (fixes[0]["hash"], path)], paths=[path],
            )


# ---------------------------------------------------------------------------- clone late propagation
_CLONE_NOISE = re.compile(r"(?i)(copyright|license|licensed|all rights reserved|spdx|__version__|^\W*version\b|"
                          r"^\W*\"?version\"?\s*[:=]|noqa|type:\s*ignore|pragma|eslint-disable)")


@candidate("clone_late_propagation")
def clone_late_propagation(ctx: Ctx):
    """Rare normalised added lines (>=25 chars, in 2..6 units: an IDF cut-off) are indexed per function-level unit
    (commit, path, innermost symbol from ``diff_lines.symbol``; module-level lines form a (commit, path) unit). Unit
    pairs in different files 14-365 days apart that share >=3 rare lines (IDF-weighted), each adding >=4 lines to
    that function (or module level), whose files do not co-change (Jaccard <0.2). The shared lines must therefore sit
    in one function on each side, which is a function-level fingerprint; single normalised lines still stand in for
    hunk shingles. Work is capped at 250k pair keys. On a repo with no symbol data every unit is simply the file."""
    index: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)  # line -> {(hash, symbol): path}
    unit_added: dict[tuple[str, str, str], int] = defaultdict(int)  # (hash, path, symbol) -> added lines
    total_commits = max(1, len(ctx.commits))
    for r in ctx.stream("SELECT hash, path, text, symbol FROM diff_lines WHERE sign='+'"):
        p = r["path"]
        if not is_code(p) or is_test(p):
            continue
        unit_added[(r["hash"], p, r["symbol"])] += 1
        if len(r["text"]) < 25:
            continue
        n = _norm(r["text"])
        if _trivial(n, 25) or _CLONE_NOISE.search(n):
            continue
        index[n].setdefault((r["hash"], r["symbol"]), p)
    pairs: dict[tuple, list[float]] = {}
    for n, owners in index.items():
        if not 2 <= len(owners) <= 6:
            continue
        w = math.log(total_commits / len(owners))
        items = sorted(owners.items(), key=lambda kv: ctx.by_hash[kv[0][0]]["seq"])
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ((h1, s1), p1), ((h2, s2), p2) = items[i], items[j]
                if p1 == p2:
                    continue
                if len(pairs) > 250_000 and (h1, p1, s1, h2, p2, s2) not in pairs:
                    continue
                e = pairs.setdefault((h1, p1, s1, h2, p2, s2), [0, 0.0])
                e[0] += 1
                e[1] += w
    jac = {(r["a"], r["b"]): r["jaccard"] for r in ctx.cochange}
    out = []
    for (h1, p1, s1, h2, p2, s2), (n, w) in pairs.items():
        if n < 3:
            continue
        c1, c2 = ctx.by_hash[h1], ctx.by_hash[h2]
        gap = (c2["ts"] - c1["ts"]) / DAY
        if not 14 <= gap <= 365:
            continue
        if unit_added[(h1, p1, s1)] < 4 or unit_added[(h2, p2, s2)] < 4:
            ctx.drop("too_few_changed_lines")
            continue
        j = jac.get((p1, p2), jac.get((p2, p1), 0.0))
        if j >= 0.2:
            ctx.drop("already_cochange")
            continue
        if _FORMAT_MSG.search(c1["message"] or "") and _FORMAT_MSG.search(c2["message"] or ""):
            ctx.drop("format_commits")
            continue
        same_dir = p1.rsplit("/", 1)[0] == p2.rsplit("/", 1)[0]
        out.append((w * (1 if same_dir else 1.5), n, h1, p1, s1, h2, p2, s2, gap))
    out.sort(reverse=True)
    removed_cache: dict[str, set[str]] = {}

    def removed_in(h: str) -> set[str]:
        if h not in removed_cache:
            removed_cache[h] = {_norm(r["text"]) for r in ctx.q(
                "SELECT text FROM diff_lines WHERE hash=? AND sign='-'", (h,))}
        return removed_cache[h]

    def short(uid: str) -> str:
        sym = ctx.symbols.get(uid) if uid else None
        return sym["qualname"] if sym else ""

    kept = 0
    for w, n, h1, p1, s1, h2, p2, s2, gap in out:
        if kept >= 300:
            break
        c1, c2 = ctx.by_hash[h1], ctx.by_hash[h2]
        # Code moved (module split, package rename) rather than copied: the later commit removed the same lines.
        added2 = {ln for ln in removed_in(h2) if any(k[0] == h2 for k in index.get(ln, {}))}
        if len(added2) >= max(2, n // 2):
            ctx.drop("moved_not_copied")
            continue
        kept += 1
        q1_, q2_ = short(s1), short(s2)
        yield Hit(
            subject=f"{h1[:12]}:{p1}|{h2[:12]}:{p2}" + (f"#{q1_}|{q2_}" if q1_ or q2_ else ""), score=w,
            title=f"{q2_ + ' in ' if q2_ else ''}{p2} (day +{round(gap)}) repeats {n} rare lines from "
                  f"{q1_ + ' in ' if q1_ else ''}{p1}",
            evidence={"shared_rare_lines": n, "gap_days": round(gap), "first": subject_of(c1["message"]),
                      "second": subject_of(c2["message"]), "same_directory": p1.rsplit("/", 1)[0] == p2.rsplit("/", 1)[0],
                      "first_symbol": q1_ or None, "second_symbol": q2_ or None},
            commits=[h1, h2], diffs=[(h1, p1), (h2, p2)], paths=[p1, p2], symbols=[x for x in (s1, s2) if x],
        )
