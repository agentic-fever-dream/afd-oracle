"""Catalogue family: legacy evolution (debt, clones, workarounds, half-finished migrations)."""
from __future__ import annotations

import ast
import bisect
import json
import re
import textwrap
from collections import Counter, defaultdict

from .history_mining import _blamed_fixes, _symbols_mapped
from .base import (DAY, DEP_MANIFEST_RX, LOCKFILE_RX, MIGRATION_RX, Ctx, Hit, candidate, is_ci, is_code, is_docs,
                   is_fix_message, is_generated, is_test, is_vendored, subject_of)

SATD = r"(#|//|/\*|\*|--)\s*(TODO|FIXME|HACK|XXX|KLUDGE|WORKAROUND)\b"


@candidate("satd_lingering_in_hot_code")
def satd_lingering_in_hot_code(ctx: Ctx):
    """A TODO/FIXME/HACK naming a concrete limitation, still present, inside a symbol that keeps changing and being
    fixed after the comment was written."""
    for r in ctx.grep(SATD, "+"):
        path, text = r["path"], r["text"].strip()
        if not is_code(path) or is_test(path):
            ctx.drop("not_production_code")
            continue
        if len(text) < 30:
            ctx.drop("generic_todo")
            continue
        if ctx.head_ts - r["ts"] < 180 * DAY:
            ctx.drop("too_recent")
            continue
        head = ctx.text_at_head(path)
        if not head or text not in head:
            ctx.drop("gone_at_head")
            continue
        line = next((i for i, ln in enumerate(head.splitlines(), 1) if ln.strip() == text), None)
        sym = ctx.symbol_at_line(path, line) if line else None
        if sym is None:
            ctx.drop("no_enclosing_symbol")
            continue
        mods = [e for e in ctx.symbol_events(sym["uid"])
                if e["seq"] > r["seq"] and e["change"] == "modified" and not ctx.by_hash[e["hash"]]["is_bulk"]]
        fixes = [f for f in ctx.fixes_by_uid.get(sym["uid"], []) if f["ts"] > r["ts"]]
        if len(mods) < 5 or not fixes:
            ctx.drop("not_hot_or_never_fixed")
            continue
        yield Hit(
            subject=f"{sym['uid']}|{text[:80]}",
            score=len(fixes) * 2 + len(mods) * 0.3,
            title=f"{text[:100]} — in {sym['qualname']}, edited {len(mods)}x and fixed {len(fixes)}x since",
            evidence={"comment": text, "comment_added": r["hash"], "age_days": round((ctx.head_ts - r["ts"]) / DAY),
                      "symbol": sym["qualname"], "modifications_since": len(mods),
                      "fixes_since": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:6]]},
            commits=[r["hash"], *[f["hash"] for f in fixes[:5]]],
            diffs=[(r["hash"], path), *[(f["hash"], path) for f in fixes[:2]]],
            paths=[path], symbols=[sym["uid"]],
        )


@candidate("clone_sibling_fix_divergence")
def clone_sibling_fix_divergence(ctx: Ctx):
    """A fix removed lines from one member of a near-duplicate cluster while a sibling still contains them."""
    clusters = ctx.q("SELECT subject, props FROM candidates WHERE kind='idiom_cluster'")
    member_of: dict[str, list[str]] = defaultdict(list)
    import json
    for c in clusters:
        members = [m["uid"] if isinstance(m, dict) else m for m in json.loads(c["props"] or "{}").get("symbols", [])]
        for m in members:
            member_of[m] = [x for x in members if x != m]
    for fx in ctx.fix_events:
        siblings = member_of.get(fx["uid"])
        if not siblings:
            continue
        _, removed = ctx.added_removed(fx["hash"], fx["path"])
        removed = [ln.strip() for ln in removed if len(ln.strip()) > 12]
        if len(removed) < 2:
            ctx.drop("trivial_fix")
            continue
        for sib in siblings:
            s = ctx.symbols.get(sib)
            if not s or not s["alive"] or not s["body"] or is_test(s["path"]) or is_vendored(s["path"]) \
                    or is_generated(s["path"]):
                continue
            body = {ln.strip() for ln in s["body"].splitlines()}
            still = [ln for ln in removed if ln in body]
            if len(still) < 2:
                continue
            yield Hit(
                subject=f"{fx['hash']}|{sib}",
                score=len(still) + 0.2 * len(ctx.callers(sib)),
                title=f"Fix {fx['hash'][:10]} changed {ctx.symbols[fx['uid']]['qualname'] if fx['uid'] in ctx.symbols else fx['uid']}; "
                      f"clone {s['qualname']} still has the old lines",
                evidence={"fix_subject": fx["subject"], "fixed_symbol": fx["uid"], "sibling": sib,
                          "lines_still_in_sibling": still[:12], "sibling_callers": len(ctx.callers(sib))},
                commits=[fx["hash"]], diffs=[(fx["hash"], fx["path"])],
                paths=[fx["path"], s["path"]], symbols=[fx["uid"], sib],
            )


# ====================================================================================================== helpers
CODE_PATH_RX = r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rb|java|kt|rs|php|cs)$"
_CMT_RX = re.compile(r"^\s*(#|//|/\*|\*|--)")
_IDENT = re.compile(r"[A-Za-z_]\w*")
COMMON_TOKENS = {"self", "true", "false", "none", "null", "string", "list", "dict", "name", "value", "values", "data",
                 "type", "types", "result", "results", "item", "items", "key", "keys", "test", "tests", "path",
                 "file", "files", "text", "line", "lines", "index", "error", "errors", "args", "kwargs", "return",
                 "class", "import", "from", "else", "elif", "async", "await", "print", "len", "int", "str", "float"}


def _is_comment(text: str) -> bool:
    return bool(_CMT_RX.match(text))


def _prod(path: str) -> bool:
    return is_code(path) and not is_test(path)


def _nparents(c) -> int:
    try:
        return len(json.loads(c["parents"] or "[]"))
    except (TypeError, ValueError):
        return len((c["parents"] or "").split())


def _find_block(text: str, name: str, path: str) -> list[str]:
    """Source lines of the first def/function/class called ``name`` in ``text`` (indent- or brace-delimited)."""
    lines = text.splitlines()
    rx = re.compile(r"^\s*(?:(?:async|export|public|private|protected|static|default)\s+)*"
                    r"(?:def|function|func|fn|class)\s+(?:\([^)]*\)\s*)?" + re.escape(name) + r"\b")
    for i, ln in enumerate(lines):
        if not rx.match(ln):
            continue
        if path.endswith((".py", ".rb")):
            base = len(ln) - len(ln.lstrip())
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > base):
                j += 1
            return lines[i:j]
        depth, started, j = 0, False, i
        while j < len(lines) and j < i + 1500:
            depth += lines[j].count("{") - lines[j].count("}")
            started = started or "{" in lines[j]
            j += 1
            if started and depth <= 0:
                break
        return lines[i:j]
    return []


BRANCH_RX = re.compile(r"^\s*(?:\}\s*)?(if|elif|else\s+if|case|when|except|catch|switch)\b")


def _branches(lines: list[str]) -> int:
    return sum(1 for ln in lines if BRANCH_RX.match(ln))


def _group_rows(rows):
    out: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        out[(r["hash"], r["path"])].append(r)
    return out


def _iter_groups(rows):
    """Like ``_group_rows(...).items()`` for rows already ordered so each (hash, path) is contiguous (ORDER BY seq,
    path): yields one group at a time instead of holding every row."""
    from itertools import groupby
    for key, grp in groupby(rows, key=lambda r: (r["hash"], r["path"])):
        yield key, list(grp)


def _tail(path: str, n: int = 2) -> str:
    return "/".join(path.split("/")[-n:])


def _enclosing_name(lines: list[str], lineno: int) -> str | None:
    """Name of the nearest def/function/class at or above 1-based ``lineno``."""
    rx = re.compile(r"^\s*(?:(?:async|export|public|private|protected|static)\s+)*(?:def|function|func|fn|class)\s+"
                    r"(?:\([^)]*\)\s*)?(\w+)")
    for i in range(min(lineno, len(lines)) - 1, -1, -1):
        m = rx.match(lines[i])
        if m:
            return m.group(1)
    return None


# ====================================================================================================== builders
TOOLING_RX = re.compile(r"(?i)(^|/)(CODEOWNERS|Makefile|Dockerfile[^/]*|Containerfile[^/]*|Jenkinsfile|MANIFEST\.in|"
                        r"tox\.ini|setup\.cfg|\.dockerignore|\.gitattributes)$|^\.github/|^\.circleci/|"
                        r"\.(ya?ml|toml|cfg|ini|sh|bash|json|mk)$")


@candidate("renamed_path_still_referenced_in_tooling")
def renamed_path_still_referenced_in_tooling(ctx: Ctx):
    """A path renamed away (and absent at HEAD) is still named in CODEOWNERS / CI / Dockerfile / Makefile / config /
    scripts that the rename commit did not touch. Match is a path-boundary token match on tooling lines (comments, URLs
    and docs are dropped)."""
    tooling = [p for p in ctx.head_files
               if TOOLING_RX.search(p) and not is_test(p) and not is_docs(p) and not LOCKFILE_RX.search(p)
               and not is_generated(p) and not is_vendored(p)]
    texts = {p: (ctx.text_at_head(p) or "") for p in tooling}
    head_paths = list(ctx.head_files)
    renames: dict[str, tuple[str, str]] = {}
    for h, rows in ctx.changes_by_commit.items():
        for r in rows:
            if r["status"] == "R" and r["old_path"]:
                renames[r["old_path"]] = (h, r["path"])
    for old, (rh, new) in sorted(renames.items()):
        if len(old) < 5:
            continue
        if old in ctx.head_files or any(p.startswith(old.rstrip("/") + "/") for p in head_paths):
            ctx.drop("resurrected_or_prefix_path")
            continue
        rx = re.compile(r"(?<![\w./-])" + re.escape(old) + r"(?![\w-]|/\w)")
        touched = {c["path"] for c in ctx.changes(rh)}
        for tp, text in texts.items():
            if old not in text or tp in touched:
                continue
            hits = []
            for i, ln in enumerate(text.splitlines(), 1):
                if not rx.search(ln):
                    continue
                s = ln.strip()
                if s.startswith(("#", "//", ";")):
                    ctx.drop("comment_only")
                    continue
                if "://" in ln:
                    ctx.drop("url")
                    continue
                hits.append((i, s[:160]))
            if not hits:
                continue
            low = tp.lower()
            weight = 5 if "codeowners" in low else 4 if (is_ci(tp) or tp.startswith(".github/")) else \
                4 if re.search(r"^\s*(COPY|ADD)\b", hits[0][1]) else 2
            yield Hit(
                subject=f"{old}|{tp}",
                score=weight + 0.3 * len(hits),
                title=f"{tp} still names {old}, renamed to {new}",
                evidence={"old_path": old, "new_path": new, "rename_commit": rh, "tooling_file": tp,
                          "lines": [{"line": n, "text": t} for n, t in hits[:4]]},
                commits=[rh], diffs=[(rh, new)], paths=[tp, new],
            )


NUM = re.compile(r"\d+(?:\.\d+)?")
AUTHORITY_RX = re.compile(r"\b(RFC\s*\d+|SLA|GDPR|PCI|policy|SOC\s*2|HIPAA)\b", re.I)
VERSIONISH_RX = re.compile(r"(?i)version|\b\d+\.\d+\.\d+\b|\bport\b|_port|sleep\(|\bseed\b")


def _comment_part(line: str) -> str:
    m = re.search(r"(#|//)\s*(.*)$", line)
    return m.group(2).strip() if m else ""


@candidate("magic_constant_comment_divergence")
def magic_constant_comment_divergence(ctx: Ctx):
    """A numeric literal was changed on a line whose neighbouring (or trailing) comment was left untouched and still
    states the old value (also 30s / 24h forms), or cites an authority (RFC/SLA/policy/GDPR/PCI).

    Edit-count variant (the spec's authority-only path): a comment that cites an authority but does not state the old
    value qualifies only when that same constant line (same file, same line shape with numbers masked) was value-edited
    >=2 times while the comment was never touched in those commits; reported once, at the latest edit, with
    ``edit_count`` and ``edit_commits``. An authority comment next to a single edit is dropped as
    ``authority_single_edit``. A stale-value hit still qualifies from one edit and reports ``edit_count`` too."""
    rows = ctx.stream("SELECT hash, seq, ts, path, sign, line, text FROM diff_lines WHERE text GLOB '*[0-9]*' "
                      "ORDER BY seq, path, sign, line")
    cands = []
    for (h, path), grp in _iter_groups(r for r in rows if _prod(r["path"])):
        minus = defaultdict(list)
        for r in grp:
            if r["sign"] == "-":
                minus[NUM.sub("#", r["text"].strip())].append(r)
        for r in grp:
            if r["sign"] != "+":
                continue
            sk = NUM.sub("#", r["text"].strip())
            if sk not in minus or not minus[sk]:
                continue
            old = minus[sk].pop(0)
            on, nn = NUM.findall(old["text"]), NUM.findall(r["text"])
            diff = [(a, b) for a, b in zip(on, nn) if a != b]
            if len(diff) != 1 or on == nn:
                continue
            cands.append((r, old, diff[0]))
    series: dict[tuple[str, str], list] = defaultdict(list)
    for r, old, _vals in cands:
        series[(r["path"], NUM.sub("#", r["text"].strip()))].append(r)
    edit_count_done: set[tuple[str, str, str]] = set()

    def comment_untouched(h2: str, path2: str, comment: str) -> bool:
        rows2 = ctx.lines_of(h2, path2)
        key = comment.strip()
        plus = sum(1 for x in rows2 if x["sign"] == "+" and key in x["text"])
        minus = sum(1 for x in rows2 if x["sign"] == "-" and key in x["text"])
        return plus == minus

    seen = 0
    for r, old, (ov, nv) in reversed(cands):
        path, h = r["path"], r["hash"]
        if len(NUM.findall(r["text"])) >= 6 or re.search(r"(?i)fixture|sample|mock", path):
            ctx.drop("table_or_fixture")
            continue
        if VERSIONISH_RX.search(r["text"]):
            ctx.drop("version_or_port")
            continue
        if ov in ("0", "1", "2", "-1") and not AUTHORITY_RX.search(r["text"]):
            continue
        seen += 1
        if seen > 3000:
            break
        text = ctx.text_at(h, path)
        if not text:
            continue
        lines = text.splitlines()
        i = r["line"] - 1
        added = {x["text"].strip() for x in ctx.lines_of(h, path) if x["sign"] == "+"}
        old_rx = re.compile(r"(?<![\w.])" + re.escape(ov) + r"(?![\w.]|\.\d)\s*(s|sec|secs|seconds?|m|min|minutes?|h|hrs?|hours?|ms|d|days?)?\b")
        new_rx = re.compile(r"(?<![\w.])" + re.escape(nv) + r"(?![\w.])")
        for j in range(max(0, i - 3), min(len(lines), i + 2)):
            ln = lines[j]
            if j == i:
                cm = _comment_part(ln)
                if cm and cm == _comment_part(old["text"]):
                    comment = cm
                else:
                    continue
            elif _is_comment(ln):
                comment = ln.strip()
                if comment in added:
                    ctx.drop("comment_edited_same_commit")
                    continue
            else:
                continue
            authority = bool(AUTHORITY_RX.search(comment))
            stale = bool(old_rx.search(comment)) and not new_rx.search(comment)
            if not (stale or authority):
                continue
            head = ctx.text_at_head(path)
            if not head or comment not in head:
                ctx.drop("comment_gone_at_head")
                continue
            sk = (path, NUM.sub("#", r["text"].strip()))
            edits = [x for x in series[sk] if x["seq"] <= r["seq"]]
            quiet = [x for x in edits if comment.strip() in (ctx.text_at(x["hash"], path) or "")
                     and comment_untouched(x["hash"], path, comment)]
            n_edits = len(quiet) if r in quiet else 0
            if not stale:
                ek = (path, sk[1], comment.strip())
                if n_edits < 2:
                    ctx.drop("authority_single_edit")
                    continue
                if ek in edit_count_done:
                    continue
                edit_count_done.add(ek)
            yield Hit(
                subject=f"{h}|{path}|{r['line']}",
                score=(3 if authority else 0) + (3 if stale else 0) + 0.2 + (min(3, n_edits - 1) if not stale else 0),
                title=f"{path}:{r['line']} value {ov} -> {nv} but comment still says: {comment[:90]}",
                evidence={"old_line": old["text"].strip()[:160], "new_line": r["text"].strip()[:160],
                          "comment": comment[:200], "stale_value": stale, "authority": authority,
                          "edit_count": n_edits, "edit_commits": [x["hash"] for x in quiet][-5:]},
                commits=[h], diffs=[(h, path)], paths=[path],
            )
            break


@candidate("special_case_accretion")
def special_case_accretion(ctx: Ctx):
    """A function whose branch count (decision points) grew >=3x since first seen, over >=8 modifying commits
    and >=2 fixes. Branch count is read from the per-version complexity in ``symbol_versions`` (decision points =
    complexity - 1): first recorded version versus latest, and the series must never fall more than 10% below its
    running peak at any version (net-adding). Only a symbol with no recorded versions falls back to counting
    branch lines in the extracted source at first-seen versus HEAD (without the never-reduced test)."""
    cands = [s for s in ctx.alive_symbols if s["kind"] in ("function", "method") and _prod(s["path"])
             and (s["modifications"] or 0) >= 8 and (s["loc"] or 0) >= 15]
    cands.sort(key=lambda s: -(s["modifications"] or 0))
    for s in cands[:400]:
        uid, path = s["uid"], s["path"]
        if re.search(r"(?i)pars|lex|grammar|token|dispatch|migrat|generated", path) \
                or re.search(r"(?i)pars|dispatch|visit", s["name"] or ""):
            ctx.drop("parser_or_dispatch")
            continue
        if is_generated(path):
            ctx.drop("generated")
            continue
        mods = [e for e in ctx.symbol_events(uid)
                if e["change"] == "modified" and not (ctx.by_hash.get(e["hash"]) or {"is_bulk": 0})["is_bulk"]]
        fixes = ctx.fixes_by_uid.get(uid, [])
        if len(mods) < 8 or len(fixes) < 2:
            ctx.drop("not_enough_edits_or_fixes")
            continue
        vers = ctx.versions_by_uid.get(uid, [])
        if len(vers) >= 2:
            series = [max((v["complexity"] or 1) - 1, 0) for v in vers]
            bt, bn = series[0], series[-1]
            if bn < 6 or bn < 3 * max(bt, 1):
                ctx.drop("no_branch_growth")
                continue
            peak, reduced = 0, False
            for b in series:
                if peak and b < 0.9 * peak:
                    reduced = True
                peak = max(peak, b)
            if reduced:
                ctx.drop("complexity_was_reduced")
                continue
            if (vers[-1]["loc"] or s["loc"] or 0) / max(bn, 1) < 3:
                ctx.drop("dispatch_table")
                continue
        else:
            head_text = ctx.text_at_head(path)
            first_text = ctx.text_at(s["first_seen"], path) if s["first_seen"] else None
            if not head_text or not first_text:
                ctx.drop("no_comparable_versions")
                continue
            now = _find_block(head_text, s["name"], path)
            then = _find_block(first_text, s["name"], path)
            bn, bt = _branches(now), _branches(then)
            if not now or not then or bn < 6 or bn < 3 * max(bt, 1):
                ctx.drop("no_branch_growth")
                continue
            if len(now) / max(bn, 1) < 3:
                ctx.drop("dispatch_table")
                continue
        yield Hit(
            subject=uid, score=bn - bt + len(fixes),
            title=f"{s['qualname']} grew from {bt} to {bn} branches over {len(mods)} edits, {len(fixes)} fixes",
            evidence={"branches_first_seen": bt, "branches_head": bn, "versions": len(vers) or None,
                      "modifications": len(mods),
                      "fix_subjects": [f["subject"] for f in fixes[:5]], "loc": s["loc"]},
            commits=[s["first_seen"], *[f["hash"] for f in fixes[:4]]],
            diffs=[(f["hash"], path) for f in fixes[:3]], paths=[path], symbols=[uid],
        )


SWEEP_MSG_RX = re.compile(r"(?i)\b(migrat|renam|deprecat|replac|switch|convert|port|upgrade|move)\w*")


@candidate("partial_api_migration_residue")
def partial_api_migration_residue(ctx: Ctx):
    """A sweep commit (>=10 line-paired single-identifier substitutions old->new across >=3 files) after which the
    old identifier is still present at HEAD, with the count of post-sweep additions of it. Sweeps that were committed
    as bulk commits are absent from the diff_lines index, so only sub-bulk sweeps are seen."""
    rows = (r for r in ctx.stream("SELECT hash, seq, ts, path, sign, line, text FROM diff_lines "
                                  "ORDER BY seq, path, sign, line") if is_code(r["path"]))
    subs: dict[tuple[str, str, str], dict] = {}
    for (h, path), grp in _iter_groups(rows):
        minus = [r["text"] for r in grp if r["sign"] == "-"]
        plus = [r["text"] for r in grp if r["sign"] == "+"]
        if not minus or not plus:
            continue
        pairs = list(zip(minus, plus)) if len(minus) == len(plus) else []
        if not pairs and len(minus) <= 40 and len(plus) <= 40:
            used = set()
            for m in minus:
                tm = re.findall(r"\w+|\W", m)
                for k, p in enumerate(plus):
                    if k in used:
                        continue
                    tp = re.findall(r"\w+|\W", p)
                    if len(tm) == len(tp) and sum(a != b for a, b in zip(tm, tp)) == 1:
                        used.add(k)
                        pairs.append((m, p))
                        break
        for m, p in pairs:
            tm, tp = re.findall(r"\w+|\W", m), re.findall(r"\w+|\W", p)
            if len(tm) != len(tp):
                continue
            d = [(a, b) for a, b in zip(tm, tp) if a != b]
            if len(d) != 1:
                continue
            a, b = d[0]
            if not (_IDENT.fullmatch(a) and _IDENT.fullmatch(b)) or len(a) < 5 or a.lower() in COMMON_TOKENS:
                continue
            e = subs.setdefault((h, a, b), {"files": set(), "n": 0})
            e["files"].add(path)
            e["n"] += 1
    sweeps = {k: v for k, v in subs.items() if v["n"] >= 10 and len(v["files"]) >= 3}
    if not sweeps:
        return
    olds = {k[1] for k in sweeps}
    sweep_seq = {k: ctx.by_hash[k[0]]["seq"] for k in sweeps}
    post_adds: dict[str, list] = defaultdict(list)
    for r in ctx.stream("SELECT hash, seq, ts, path, sign, line, text FROM diff_lines WHERE sign='+' ORDER BY seq"):
        if not is_code(r["path"]) or is_test(r["path"]) or is_vendored(r["path"]):
            continue
        for tok in set(_IDENT.findall(r["text"])) & olds:
            post_adds[tok].append(r)
    for (h, a, b), v in sweeps.items():
        if all(is_test(p) or is_docs(p) for p in v["files"]):
            ctx.drop("tests_or_docs_only")
            continue
        post = [r for r in post_adds.get(a, []) if r["seq"] > sweep_seq[(h, a, b)]]
        residue_files = []
        for p in {r["path"] for r in post_adds.get(a, [])}:
            t = ctx.text_at_head(p)
            if t and re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", t):
                residue_files.append(p)
        if not residue_files:
            ctx.drop("old_token_gone_at_head")
            continue
        post_live = [r for r in post if r["path"] in residue_files]
        c = ctx.by_hash[h]
        yield Hit(
            subject=f"{h}|{a}->{b}",
            score=len(post_live) * 2 + len(residue_files) + (3 if SWEEP_MSG_RX.search(subject_of(c["message"])) else 0),
            title=f"{a} -> {b} swept in {h[:10]} ({v['n']} hunks, {len(v['files'])} files); {a} still in {len(residue_files)} files",
            evidence={"old": a, "new": b, "sweep_hunks": v["n"], "sweep_files": sorted(v["files"])[:6],
                      "residue_files": sorted(residue_files)[:8], "post_sweep_additions": len(post_live),
                      "post_sweep_commits": sorted({r["hash"] for r in post_live})[:5],
                      "sweep_subject": subject_of(c["message"])},
            commits=[h, *sorted({r["hash"] for r in post_live})[:3]],
            diffs=[(h, sorted(v["files"])[0]), *[(r["hash"], r["path"]) for r in post_live[:2]]],
            paths=sorted(residue_files)[:4],
        )


MIG_ADD_RX = [re.compile(r"(?i)\badd\s+column\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)"),
              re.compile(r"(?i)\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([\w.]+)"),
              re.compile(r"add_column\s*\(?\s*:?['\"]?\w+['\"]?\s*,\s*:?['\"]?(\w+)"),
              re.compile(r"\bColumn\(\s*['\"](\w+)['\"]"),
              re.compile(r"(?i)\bcreate_table\s*\(?\s*:?['\"]?(\w+)")]
MIG_DROP_RX = re.compile(r"(?i)drop\s+(column|table)|remove_column|drop_column|drop_table|DeleteModel|RemoveField")
EXPAND_NAME_RX = re.compile(r"(?i)(_new|_old|_legacy|_v2|_bak|_tmp)$")


@candidate("expand_contract_incomplete")
def expand_contract_incomplete(ctx: Ctx):
    """A column/table named *_new|_old|_legacy|_v2|_bak|_tmp added by a migration, never dropped by a later
    migration, older than 180 days, and still read or written by non-migration code at HEAD."""
    added: dict[str, tuple] = {}
    drops: list = []
    mig_rows = ctx.stream("SELECT hash, seq, ts, path, sign, line, text FROM diff_lines WHERE sign='+' ORDER BY seq")
    for r in mig_rows:
        if not MIGRATION_RX.search(r["path"]):
            continue
        t = r["text"]
        if MIG_DROP_RX.search(t):
            drops.append(r)
        for rx in MIG_ADD_RX:
            for m in rx.finditer(t):
                nm = m.group(1).split(".")[-1]
                if EXPAND_NAME_RX.search(nm) and nm not in added:
                    added[nm] = (r, rx.pattern)
    if not added:
        return
    names = set(added)
    refs: dict[str, list] = defaultdict(list)
    co_assign: dict[str, list] = defaultdict(list)
    # second pass: a fresh stream (the first one is consumed)
    for r in ctx.stream("SELECT hash, seq, ts, path, sign, line, text FROM diff_lines WHERE sign='+' ORDER BY seq"):
        if MIGRATION_RX.search(r["path"]) or not is_code(r["path"]) or is_test(r["path"]):
            continue
        toks = set(re.findall(r"\w+", r["text"]))
        for nm in toks & names:
            refs[nm].append(r)
            base = EXPAND_NAME_RX.sub("", nm)
            if base in toks and base != nm:
                co_assign[nm].append(r)
    for nm, (r, _) in sorted(added.items()):
        if re.search(r"(?i)archive|history|audit", nm):
            ctx.drop("archive_table")
            continue
        if ctx.head_ts - r["ts"] < 180 * DAY:
            ctx.drop("too_recent")
            continue
        wb = re.compile(r"(?<!\w)" + re.escape(nm) + r"(?!\w)")
        if any(d["seq"] > r["seq"] and wb.search(d["text"]) for d in drops):
            ctx.drop("dropped_later")
            continue
        live = []
        for p in {x["path"] for x in refs.get(nm, [])}:
            t = ctx.text_at_head(p)
            if t and wb.search(t):
                live.append(p)
        if not live:
            ctx.drop("migration_only_references")
            continue
        yield Hit(
            subject=nm, score=len(live) * 2 + (3 if co_assign.get(nm) else 0),
            title=f"{nm} added by migration {r['path']} {round((ctx.head_ts - r['ts']) / DAY)} days ago, never dropped, "
                  f"still used in {len(live)} files",
            evidence={"name": nm, "migration": r["path"], "added_line": r["text"].strip()[:160],
                      "readers_writers": sorted(live)[:8], "co_assigned_with_base": bool(co_assign.get(nm))},
            commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"], *sorted(live)[:3]],
        )


REFACTOR_MSG_RX = re.compile(r"(?i)\b(refactor\w*|renam\w*|migrat\w*|codemod|automated?|convert\w*|modernis\w*|"
                             r"modernize\w*|replace\w*|upgrade\w*|port(ed|ing)?|clean\s*up|deprecat\w*)\b")
FORMAT_MSG_RX = re.compile(r"(?i)\b(format(ting)?|black|prettier|isort|lint\w*|license|header|copyright|whitespace|"
                           r"autopep8|ruff|style)\b")


@candidate("mass_automated_refactor_regression")
def mass_automated_refactor_regression(ctx: Ctx):
    """A bulk (or >=50-file) commit with a refactor message that modified symbols, followed within 30 days by
    fix-class commits touching >=3 (or >=10%) of the same symbols. 'Non-AST-equal' is a symbol modification event (body
    hash changed); the qualifying rule is still symbol overlap with later fixes. ``line_origins`` adds corroboration:
    fix commits (within 30 days) whose removed lines blame to the refactor commit are listed as
    ``fixes_blaming_refactor`` and boost the score. Without blame the symbol-overlap rule alone decides."""
    fix_ts = [f["ts"] for f in ctx.fix_events]
    blamed = _blamed_fixes(ctx)
    for c in ctx.commits:
        if not (c["is_bulk"] or (c["files_changed"] or 0) >= 50):
            continue
        msg = c["message"] or ""
        if _nparents(c) == 0:
            ctx.drop("initial_import")
            continue
        if not REFACTOR_MSG_RX.search(subject_of(msg)):
            continue
        if FORMAT_MSG_RX.search(subject_of(msg)):
            ctx.drop("formatting_or_license")
            continue
        touched = {e["uid"] for e in ctx.events_by_commit.get(c["hash"], [])
                   if e["change"] == "modified" and not is_generated(e["path"]) and not is_test(e["path"])}
        if len(touched) < 3:
            ctx.drop("few_symbols_touched")
            continue
        lo = bisect.bisect_right(fix_ts, c["ts"])
        hi = bisect.bisect_right(fix_ts, c["ts"] + 30 * DAY)
        hit_fixes = [f for f in ctx.fix_events[lo:hi] if f["uid"] in touched and f["hash"] != c["hash"]]
        uids = {f["uid"] for f in hit_fixes}
        if len(uids) < 2 or not (len(uids) >= 3 or len(uids) >= 0.1 * len(touched)):
            continue
        fixes = sorted({f["hash"] for f in hit_fixes})
        blame_fixes = [h for h, _ in blamed.get(c["hash"], {}).most_common()
                       if h in ctx.by_hash and 0 < ctx.by_hash[h]["ts"] - c["ts"] <= 30 * DAY]
        yield Hit(
            subject=c["hash"], score=len(uids) * 2 + len(fixes) + 10 * len(uids) / len(touched) + 2 * len(blame_fixes),
            title=f"{subject_of(msg)[:80]} touched {len(touched)} symbols; {len(uids)} fixed within 30 days",
            evidence={"files_changed": c["files_changed"], "symbols_touched": len(touched),
                      "fixes_blaming_refactor": blame_fixes[:6],
                      "symbols_fixed_after": sorted(uids)[:8],
                      "fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in hit_fixes[:6]]},
            commits=[c["hash"], *fixes[:5]], diffs=[(f["hash"], f["path"]) for f in hit_fixes[:3]],
            paths=sorted({f["path"] for f in hit_fixes})[:5], symbols=sorted(uids)[:6],
        )


VERSION_TOKEN_RX = re.compile(r"(?i)^(.*?)(?:_?v\d+|_new|_old|_legacy|_deprecated|_compat)$|^(?:legacy_|old_|new_|v\d+_)(.*)$")


def _norm_name(name: str) -> str | None:
    m = VERSION_TOKEN_RX.match(name)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").lower() or None


@candidate("parallel_implementation_drift")
def parallel_implementation_drift(ctx: Ctx):
    """Two live symbols paired by name transform (_v2 / legacy_ / _old ...) or by mirrored v1/v2/legacy directories
    with the same qualname, both with callers, where one side received >=2 fixes after the other's last change."""
    last_mod: dict[str, int] = {}
    for r in ctx.q("SELECT uid, max(ts) t FROM symbol_events WHERE change IN ('modified','added') GROUP BY uid"):
        last_mod[r["uid"]] = r["t"]
    groups: dict[tuple, list] = defaultdict(list)
    for s in ctx.alive_symbols:
        if s["kind"] not in ("function", "method", "class") or not _prod(s["path"]):
            continue
        norm = _norm_name(s["name"] or "") or (s["name"] or "").lower()
        d = s["path"].rsplit("/", 1)[0] if "/" in s["path"] else ""
        mdir = re.sub(r"(?i)(?<=/)(v\d+|legacy|old|new|deprecated)(?=/|$)|^(v\d+|legacy|old)(?=/)", "~", d)
        stem = re.sub(r"(?i)(_v\d+|_new|_old|_legacy|^legacy_|^old_|^v\d+_)", "", s["path"].rsplit("/", 1)[-1])
        groups[(s["kind"], norm, mdir, stem)].append(s)
    clusters = [json.loads(c["props"] or "{}").get("symbols", []) for c in
                ctx.q("SELECT props FROM candidates WHERE kind='idiom_cluster'")]
    clustered = [{(m["uid"] if isinstance(m, dict) else m) for m in cl} for cl in clusters]
    seen: set[frozenset] = set()
    for key, members in groups.items():
        if len(members) < 2 or len(members) > 6:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a["uid"] == b["uid"] or (a["name"] == b["name"] and a["path"] == b["path"]):
                    continue
                if _norm_name(a["name"] or "") is None and _norm_name(b["name"] or "") is None \
                        and a["path"].rsplit("/", 1)[0] == b["path"].rsplit("/", 1)[0]:
                    continue
                pair = frozenset((a["uid"], b["uid"]))
                if pair in seen:
                    continue
                seen.add(pair)
                if re.search(r"(?i)(^|/)api/v\d+/", a["path"]) and re.search(r"(?i)(^|/)api/v\d+/", b["path"]):
                    ctx.drop("frozen_api_versions")
                    continue
                if re.search(r"(?i)proto|_pb2|generated", a["path"] + b["path"]):
                    ctx.drop("generated")
                    continue
                if not ctx.callers(a["uid"]) or not ctx.callers(b["uid"]):
                    ctx.drop("zero_caller_side")
                    continue
                if any(a["uid"] in cl and b["uid"] in cl for cl in clustered):
                    ctx.drop("already_clone_cluster")
                    continue
                for x, y in ((a, b), (b, a)):
                    fixes = [f for f in ctx.fixes_by_uid.get(x["uid"], []) if f["ts"] > last_mod.get(y["uid"], 0)]
                    if len(fixes) >= 2:
                        yield Hit(
                            subject=f"{x['uid']}|{y['uid']}",
                            score=len(fixes) * 2 + (2 if x["path"] == y["path"] else 0)
                            + 0.2 * (len(ctx.callers(x["uid"])) + len(ctx.callers(y["uid"]))),
                            title=f"{x['qualname']} fixed {len(fixes)}x while sibling {y['qualname']} stayed untouched",
                            evidence={"fixed_side": x["uid"], "stale_side": y["uid"],
                                      "stale_side_last_change_days_ago": round((ctx.head_ts - last_mod.get(y['uid'], 0)) / DAY),
                                      "fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:5]],
                                      "callers": [len(ctx.callers(x["uid"])), len(ctx.callers(y["uid"]))]},
                            commits=[f["hash"] for f in fixes[:5]], diffs=[(f["hash"], x["path"]) for f in fixes[:2]],
                            paths=[x["path"], y["path"]], symbols=[x["uid"], y["uid"]],
                        )


ADR_PATH_RX = re.compile(r"(?i)(^|/)(adrs?|decisions?|architecture[-_]decisions?|rfcs?)/[^/]+\.(md|rst|txt)$")
ADR_STATUS_RX = re.compile(r"(?im)status[\s:*_#\-]*?(superseded|deprecated|rejected)\b([^\n]*)")
GENERIC_TOKENS = COMMON_TOKENS | {"adr", "api", "json", "yaml", "http", "https", "docker", "python", "django", "rest",
                                  "sql", "main", "master", "config", "docs", "readme", "github"}


@candidate("adr_superseded_code_remains")
def adr_superseded_code_remains(ctx: Ctx):
    """An ADR whose status is Superseded/Deprecated/Rejected whose backticked tokens (>=2 non-generic, >=1 path /
    class / package) still resolve at HEAD with >=1 path or symbol modified after the status date. Tokens that also
    appear in the successor decision are excluded. Package tokens are resolved against dependency manifests."""
    adrs = [p for p in ctx.head_files if ADR_PATH_RX.search(p) or re.search(r"(^|/)\d{3,4}-[\w-]+\.md$", p)]
    if not adrs:
        return
    by_name: dict[str, list] = defaultdict(list)
    for s in ctx.alive_symbols:
        if _prod(s["path"]):
            by_name[s["name"]].append(s)
    manifest = "\n".join((ctx.text_at_head(p) or "") for p in ctx.head_files if DEP_MANIFEST_RX.search(p)).lower()
    last_mod: dict[str, list[int]] = defaultdict(list)
    for r in ctx.q("SELECT uid, ts FROM symbol_events WHERE change='modified' ORDER BY ts"):
        last_mod[r["uid"]].append(r["ts"])
    for adr in sorted(adrs):
        text = ctx.text_at_head(adr) or ""
        m = ADR_STATUS_RX.search(text)
        if not m:
            continue
        status, tail = m.group(1).lower(), m.group(2)
        srows = ctx.grep(r"(?i)superseded|deprecated|rejected", "+", path_like=adr)
        srows = [r for r in srows if re.search(r"(?i)status", r["text"]) or status in r["text"].lower()]
        date = (srows[0]["ts"], srows[0]["hash"]) if srows else None
        if not date:
            date = (ctx.by_hash[ctx.path_commits(adr)[-1]["hash"]]["ts"], ctx.path_commits(adr)[-1]["hash"]) \
                if ctx.path_commits(adr) else None
        if not date:
            continue
        succ_text = ""
        nm = re.search(r"(?i)(?:adr[-\s]*|#|\[)?(\d{2,4})\b", tail)
        if nm:
            for p in adrs:
                if p != adr and re.search(r"(^|/)0*" + nm.group(1).lstrip("0") + r"[-_]", p):
                    succ_text = (ctx.text_at_head(p) or "").lower()
        resolved: list[tuple[str, str, list]] = []
        for tok in dict.fromkeys(re.findall(r"`([^`\n]{3,80})`", text)):
            t = tok.strip().rstrip("()")
            if t.lower() in GENERIC_TOKENS or " " in t or t.lower() in succ_text.replace("`", ""):
                continue
            if "/" in t or re.search(r"\.\w{1,5}$", t):
                p = t if t in ctx.head_files else next((x for x in ctx.head_files if x.endswith("/" + t)), None)
                if p and p != adr and not is_docs(p):
                    mods = [c for c in ctx.path_commits(p) if c["ts"] > date[0]]
                    resolved.append(("path", p, [c["hash"] for c in mods]))
            elif re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+", t) and t in by_name:
                s = by_name[t][0]
                mods = [x for x in last_mod.get(s["uid"], []) if x > date[0]]
                resolved.append(("class", s["uid"], mods))
            elif re.fullmatch(r"[a-z]\w*_\w+", t) and len(t) >= 6 and t in by_name:
                s = by_name[t][0]
                mods = [x for x in last_mod.get(s["uid"], []) if x > date[0]]
                resolved.append(("function", s["uid"], mods))
            elif re.fullmatch(r"[a-z][\w.-]{3,}", t) and re.search(r"(?<![\w-])" + re.escape(t.lower()) + r"(?![\w-])", manifest):
                resolved.append(("package", t, []))
        if len(resolved) < 2 or not any(k in ("path", "class", "package") for k, _, _ in resolved):
            ctx.drop("no_resolvable_tokens")
            continue
        if not any(mods for _, _, mods in resolved):
            ctx.drop("not_modified_after_status")
            continue
        mod_hashes = [h for k, _, mods in resolved if k == "path" for h in mods]
        yield Hit(
            subject=adr, score=len(resolved) + sum(1 for _, _, m_ in resolved if m_) * 2,
            title=f"{adr} is {status} but {len(resolved)} of its referenced code items live on and keep changing",
            evidence={"adr": adr, "status": status, "status_line": (m.group(0).strip())[:160],
                      "status_commit": date[1], "resolved": [{"type": k, "ref": v, "modified_after": bool(mm)}
                                                              for k, v, mm in resolved][:10]},
            commits=[date[1], *mod_hashes[:3]], diffs=[(h, v) for k, v, mm in resolved if k == "path" for h in mm[:1]][:3],
            paths=[adr, *[v for k, v, _ in resolved if k == "path"][:4]],
            symbols=[v for k, v, _ in resolved if k in ("class", "function")][:4],
        )


def _file_set(ctx: Ctx, c) -> set[str]:
    out = set()
    for fc in ctx.changes(c["hash"]):
        p = fc["path"]
        if (LOCKFILE_RX.search(p) or is_generated(p) or is_vendored(p) or is_docs(p)
                or re.search(r"(?i)(^|/)(locale|locales|i18n|translations?|l10n)/|\.(po|pot|xlf)$", p)):
            continue
        out.add(p)
    return out


@candidate("shotgun_surgery_commit_pattern")
def shotgun_surgery_commit_pattern(ctx: Ctx):
    """A file set of >=4 recurring together in >=3 commits; another commit touches all but one file of the set and a
    fix touching the missing file follows within 30 days. Recurring sets are found by intersecting commit pairs that
    share >=4 files (via a file->commits index, popular files skipped)."""
    fsets: dict[str, set[str]] = {}
    for c in ctx.commits:
        if c["is_merge"] or c["is_bulk"] or REVERT_RX_LOCAL.search(subject_of(c["message"])) \
                or re.search(r"(?i)\b(release|bump|version)\b", subject_of(c["message"])):
            continue
        fs = _file_set(ctx, c)
        if 3 <= len(fs) <= 25:
            rn = sum(1 for fc in ctx.changes(c["hash"]) if fc["status"] == "R")
            if rn * 2 >= len(ctx.changes(c["hash"])):
                ctx.drop("bulk_rename")
                continue
            fsets[c["hash"]] = fs
    idx: dict[str, list[str]] = defaultdict(list)
    for h, fs in fsets.items():
        for p in fs:
            idx[p].append(h)
    order = {c["hash"]: c["seq"] for c in ctx.commits}
    cores: dict[frozenset, set[str]] = {}
    budget = 3_000_000
    for h, fs in fsets.items():
        cnt: Counter = Counter()
        for p in fs:
            lst = idx[p]
            if len(lst) > 120:
                continue
            for o in lst:
                if order[o] > order[h]:
                    cnt[o] += 1
            budget -= len(lst)
        if budget < 0:
            break
        for o, n in cnt.items():
            if n >= 4:
                core = frozenset(fs & fsets[o])
                cores.setdefault(core, set()).update((h, o))
    pop_ok = lambda p: len(idx[p]) <= 120  # noqa: E731
    out: list[tuple[frozenset, set[str]]] = []
    for core, sup in cores.items():
        first = min(core, key=lambda p: len(idx[p]))
        support = {h for h in idx[first] if core <= fsets[h]}
        if sum(1 for p in core if not is_test(p)) < 2:
            ctx.drop("test_only_set")  # test files moving together is test refactoring, not shotgun surgery
            continue
        if len(support) >= 3:
            out.append((core, support))
    out.sort(key=lambda x: (-len(x[0]), -len(x[1])))
    kept: list[tuple[frozenset, set[str]]] = []
    for core, sup in out:
        if any(core < k and sup <= s for k, s in kept):
            continue
        kept.append((core, sup))
    emitted: set[tuple[str, str]] = set()
    for core, sup in kept[:300]:
        cand = {h for p in core for h in idx[p] if pop_ok(p)} - sup
        for h in sorted(cand, key=lambda x: order[x]):
            fs = fsets[h]
            miss = core - fs
            if len(miss) != 1 or len(core & fs) < max(3, len(core) - 1):
                continue
            f = next(iter(miss))
            ck = ctx.by_hash[h]
            fix = next((c for c in ctx.path_commits(f) if c["hash"] != h and 0 < c["ts"] - ck["ts"] <= 30 * DAY
                        and ctx.is_fix(c)), None)
            if not fix or (h, f) in emitted:
                continue
            emitted.add((h, f))
            yield Hit(
                subject=f"{h}|{f}", score=len(core) + len(sup) + 3,
                title=f"{ck['message'].splitlines()[0][:70]} skipped {f} of a {len(core)}-file set; fix followed",
                evidence={"file_set": sorted(core), "recurring_in": len(sup), "missing_file": f,
                          "partial_commit": h, "fix_commit": fix["hash"], "fix_subject": subject_of(fix["message"])},
                commits=[h, fix["hash"], *sorted(sup, key=lambda x: order[x])[:2]],
                diffs=[(fix["hash"], f)], paths=sorted(core)[:6],
            )


REVERT_RX_LOCAL = re.compile(r"(?i)^revert\b")


def _new_callers(ctx: Ctx) -> dict[str, list[tuple[str, int, set[str]]]]:
    """uid -> [(boundary tag name or 'HEAD', boundary ts, callers gained)] between consecutive release tags and from
    the last tag to HEAD (needs ``calls_at``). Callers must be known symbols."""
    tags = ctx.tags
    if not tags:
        return {}
    snaps = []
    for t in tags:
        by_dst: dict[str, set[str]] = defaultdict(set)
        for r in ctx.calls_at(t["name"]):
            by_dst[r["dst"]].add(r["src"])
        snaps.append((t, by_dst))
    if not any(d for _, d in snaps):
        return {}
    out: dict[str, list] = defaultdict(list)
    for (_, before), (t2, after) in zip(snaps, snaps[1:]):
        for dst, srcs in after.items():
            gained = {x for x in srcs - before.get(dst, set()) if x in ctx.symbols and x != dst}
            if gained:
                out[dst].append((t2["name"], t2["ts"], gained))
    last_t, last = snaps[-1]
    if ctx.head_ts > last_t["ts"]:
        for dst, srcs in ctx.calls[1].items():
            gained = {x for x in srcs - last.get(dst, set()) if x in ctx.symbols and x != dst}
            if gained:
                out[dst].append(("HEAD", ctx.head_ts, gained))
    return out


@candidate("dormant_critical_path_code")
def dormant_critical_path_code(ctx: Ctx):
    """A function/class dormant >=2 years that was then modified and within 90 days needed another modification or a
    fix, with fan-in in the top 10% of callers. Fixes of the symbol whose removed lines blame (``line_origins``)
    to the waking commit are reported as ``blamed_fixes`` and boost the score.

    Second trigger, ``new_caller`` (only for symbols with no waking-modification hit): the symbol gained a caller
    between two consecutive release tags (``ctx.calls_at(prev)`` vs ``calls_at(next)``), or at HEAD versus the last tag
    (``ctx.callers(uid)`` vs ``calls_at(last tag)``), after it had been untouched (no symbol event) for >=2 years at that
    point, with the same fan-in threshold. Fixes within 90 days after the boundary boost the score. Needs the
    per-release call edges (``calls_at``) and >=2 tags (or 1 tag + HEAD); with none the trigger is silently skipped."""
    blamed = _blamed_fixes(ctx)
    fan = sorted(len(ctx.callers(s["uid"])) for s in ctx.alive_symbols if ctx.callers(s["uid"]))
    thr = max(2, fan[int(len(fan) * 0.9)] if fan else 2)
    evs: dict[str, list] = defaultdict(list)
    for r in ctx.q("SELECT uid, hash, change, ts FROM symbol_events ORDER BY uid, seq"):
        evs[r["uid"]].append(r)
    new_callers_by_uid = _new_callers(ctx)
    for s in ctx.alive_symbols:
        uid, path = s["uid"], s["path"]
        if not _prod(path):
            continue
        if s["kind"] not in ("function", "method", "class"):
            ctx.drop("constant_or_variable")
            continue
        if re.match(r"(get|set|is)_", s["name"] or "") and (s["loc"] or 0) <= 5:
            ctx.drop("trivial_accessor")
            continue
        n_in = len(ctx.callers(uid))
        if n_in < thr:
            continue
        es = evs.get(uid, [])
        woke = False
        for i in range(1, len(es)):
            e, prev = es[i], es[i - 1]
            if e["change"] != "modified" or e["ts"] - prev["ts"] < 730 * DAY or ctx.by_hash[e["hash"]]["is_bulk"]:
                continue
            follow = [x for x in es[i + 1:] if x["change"] == "modified" and 0 < x["ts"] - e["ts"] <= 90 * DAY]
            fixes = [f for f in ctx.fixes_by_uid.get(uid, []) if 0 <= f["ts"] - e["ts"] <= 90 * DAY and f["hash"] != e["hash"]]
            if not follow and not fixes:
                continue
            blame_fixes = [f["hash"] for f in fixes if f["hash"] in blamed.get(e["hash"], {})]
            woke = True
            yield Hit(
                subject=f"{uid}|{e['hash']}", score=2 * len(blame_fixes) + n_in + 3 * len(fixes) + len(follow) + (e["ts"] - prev["ts"]) / (365 * DAY),
                title=f"{s['qualname']} (fan-in {n_in}) woke after {round((e['ts'] - prev['ts']) / DAY)} days dormant, "
                      f"then {len(fixes)} fixes / {len(follow)} further edits within 90 days",
                evidence={"dormant_days": round((e["ts"] - prev["ts"]) / DAY), "fan_in": n_in,
                          "waking_commit": e["hash"], "blamed_fixes": blame_fixes[:4], "followups": [x["hash"] for x in follow][:5],
                          "fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:4]]},
                commits=[e["hash"], *[f["hash"] for f in fixes[:3]], *[x["hash"] for x in follow[:2]]],
                diffs=[(e["hash"], path), *[(f["hash"], path) for f in fixes[:2]]], paths=[path], symbols=[uid],
            )
            break
        if woke:
            continue
        for tag_name, ts_b, new_callers in new_callers_by_uid.get(uid, []):
            touched = [x["ts"] for x in es if x["ts"] <= ts_b]
            if not touched or ts_b - max(touched) < 730 * DAY:
                continue
            fixes = [f for f in ctx.fixes_by_uid.get(uid, []) if 0 <= f["ts"] - ts_b <= 90 * DAY]
            dormant = round((ts_b - max(touched)) / DAY)
            last_ev = [x["hash"] for x in es if x["ts"] == max(touched)][-1:]
            yield Hit(
                subject=f"{uid}|new_caller|{tag_name}", score=n_in + 3 * len(fixes) + len(new_callers) + dormant / 365,
                title=f"{s['qualname']} (fan-in {n_in}) untouched {dormant} days, then gained "
                      f"{len(new_callers)} caller(s) by {tag_name}",
                evidence={"trigger": "new_caller", "dormant_days": dormant, "fan_in": n_in, "boundary": tag_name,
                          "new_callers": sorted(new_callers)[:6],
                          "fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:4]]},
                commits=[*last_ev, *[f["hash"] for f in fixes[:3]]],
                diffs=[(f["hash"], path) for f in fixes[:2]], paths=[path], symbols=[uid],
            )
            break


REASON_RX = re.compile(r"(?i)\b(broke|broken|breaks|slow|slows|disabled?|causes?|caused|crash\w*|fails?|failing|"
                       r"doesn'?t work|bug|regression|temporarily|hangs?|deadlock)\b")
CODEISH_RX = re.compile(r"[=;{}()\[\]]|^\s*(if|for|while|return|def|class|import|from|var|let|const|function|print)\b")


def _looks_like_code(lines: list[str], path: str) -> bool:
    body = [re.sub(r"^\s*(#|//)\s?", "", ln) for ln in lines]
    if path.endswith(".py"):
        try:
            tree = ast.parse(textwrap.dedent("\n".join(body)))
            return any(not isinstance(n, ast.Expr) or isinstance(n.value, ast.Call) for n in tree.body)
        except SyntaxError:
            pass
    codeish = sum(1 for b in body if CODEISH_RX.search(b) and not re.match(r"^[A-Z][a-z]+\s+\w+\s+\w+.*[a-z]\.$", b.strip()))
    return codeish >= 0.7 * len(body)


@candidate("commented_out_code_block")
def commented_out_code_block(ctx: Ctx):
    """An added comment block of >=3 lines that parses as code (ast for Python, a code-token heuristic elsewhere) with a
    reason keyword in the block, the adjacent comment or the commit message and a live replacement line adjacent to the
    block (added in the same commit). Survival is checked by searching HEAD text."""
    rows = ctx.grep(r"^\s*(#|//)", "+", path_regex=CODE_PATH_RX)
    for (h, path), grp in _group_rows(rows).items():
        if is_docs(path) or is_vendored(path) or is_generated(path):
            continue
        grp.sort(key=lambda r: r["line"])
        blocks, cur = [], [grp[0]]
        for r in grp[1:]:
            if r["line"] == cur[-1]["line"] + 1:
                cur.append(r)
            else:
                blocks.append(cur)
                cur = [r]
        blocks.append(cur)
        for blk in blocks:
            if len(blk) < 3:
                continue
            texts = [r["text"] for r in blk]
            joined = "\n".join(texts)
            if re.search(r"(?i)copyright|license|>>>|noqa|pylint|type:\s*ignore", joined):
                ctx.drop("license_or_docstring_example")
                continue
            if not _looks_like_code(texts, path):
                continue
            ft = ctx.text_at(h, path)
            if not ft:
                continue
            fl = ft.splitlines()
            a, b = blk[0]["line"] - 1, blk[-1]["line"]
            adj = [fl[k] for k in (a - 1, b) if 0 <= k < len(fl) and _is_comment(fl[k])]
            msg = ctx.by_hash[h]["message"] or ""
            if not (REASON_RX.search(joined) or any(REASON_RX.search(x) for x in adj) or REASON_RX.search(msg)):
                ctx.drop("no_reason_keyword")
                continue
            added = {x["text"].strip() for x in ctx.lines_of(h, path) if x["sign"] == "+"}
            repl = [fl[k] for k in (a - 1, b) if 0 <= k < len(fl) and fl[k].strip() and not _is_comment(fl[k])
                    and fl[k].strip() in added]
            if not repl:
                ctx.drop("no_live_replacement")
                continue
            head = ctx.text_at_head(path)
            first = texts[0].strip()
            survives = bool(head and first in head)
            yield Hit(
                subject=f"{h}|{path}|{blk[0]['line']}", score=len(blk) + (3 if survives else 0) + (1 if REASON_RX.search(joined) else 0),
                title=f"{len(blk)}-line commented-out code in {path} kept next to its replacement ({'still at HEAD' if survives else 'removed since'})",
                evidence={"block": [t.strip()[:120] for t in texts[:8]], "replacement": [x.strip()[:120] for x in repl],
                          "adjacent_comment": [x.strip()[:120] for x in adj], "survives_at_head": survives,
                          "commit_subject": subject_of(msg)},
                commits=[h], diffs=[(h, path)], paths=[path],
            )


@candidate("satd_comment_removed_code_unchanged")
def satd_comment_removed_code_unchanged(ctx: Ctx):
    """A TODO/FIXME/HACK line removed while the surrounding file diff has <3 non-comment changed lines, on a symbol that
    has fix-lineage commits within 12 months of the removal (hard requirement). The enclosing symbol is the removed
    line's ``diff_lines.symbol`` (innermost function/class in the pre-commit file; a module-level comment has none).
    Only when enrichment has attributed no lines in this repo at all does it fall back to scanning upward from the
    removed line in the pre-commit text and matching by name.

    The 'symbol body diff' is the enclosing symbol's own changed non-comment, non-blank lines in that commit
    (``diff_lines`` rows whose ``symbol`` is that uid; <3 required), so edits to other functions in the same file no
    longer hide the removal. Fallback (no symbol attributed to the removed line, or no enrichment at all): the file's
    non-comment diff in the commit, as before."""
    mapped = _symbols_mapped(ctx)
    for r in ctx.grep(SATD, "-"):
        h, path = r["hash"], r["path"]
        if not _prod(path):
            ctx.drop("not_production_code")
            continue
        c = ctx.by_hash[h]
        if c["is_bulk"] or FORMAT_MSG_RX.search(subject_of(c["message"])):
            ctx.drop("reformat_or_bulk_commit")
            continue
        added, removed = ctx.added_removed(h, path)
        if mapped and r["symbol"]:
            code_diff = [x["text"] for x in ctx.lines_of(h, path) if x["symbol"] == r["symbol"]
                         and x["text"].strip() and not _is_comment(x["text"].strip())]
            basis = "symbol"
        else:
            code_diff = [x for x in added + removed if x.strip() and not _is_comment(x.strip())]
            basis = "file"
        if len(code_diff) >= 3:
            ctx.drop("code_changed")
            continue
        if any(re.search(r"(#\d+|[A-Z]{2,}-\d+|issues?/\d+|github\.com)", x) for x in added if _is_comment(x.strip())):
            ctx.drop("converted_to_issue_ref")
            continue
        if mapped:
            sym = ctx.symbols.get(r["symbol"]) if r["symbol"] else None
        else:
            before = ctx.text_before(h, path)
            if not before:
                continue
            name = _enclosing_name(before.splitlines(), r["line"])
            sym = next((s for s in ctx.symbols.values() if s["path"] == path and s["name"] == name), None) if name else None
        if sym is None:
            ctx.drop("no_enclosing_symbol")
            continue
        if not sym["alive"]:
            ctx.drop("symbol_deleted_or_moved")
            continue
        fixes = [f for f in ctx.fixes_by_uid.get(sym["uid"], []) if abs(f["ts"] - r["ts"]) <= 365 * DAY and f["hash"] != h]
        if not fixes:
            ctx.drop("no_fix_lineage")
            continue
        yield Hit(
            subject=f"{h}|{sym['uid']}", score=len(fixes) * 2 + (2 if not is_fix_message(c["message"]) else 0),
            title=f"'{r['text'].strip()[:80]}' removed from {sym['qualname']} with no code change; {len(fixes)} fixes nearby",
            evidence={"removed_comment": r["text"].strip(), "commit_subject": subject_of(c["message"]),
                      "non_comment_changed_lines": len(code_diff), "diff_basis": basis,
                      "fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:5]]},
            commits=[h, *[f["hash"] for f in fixes[:4]]], diffs=[(h, path), *[(f["hash"], path) for f in fixes[:2]]],
            paths=[path], symbols=[sym["uid"]],
        )


@candidate("dead_after_last_caller_removed")
def dead_after_last_caller_removed(ctx: Ctx):
    """A private function (>=15 lines, no CALLS in-edges at HEAD) whose last call was removed >=6 months ago in a commit
    with rejection/replacement language (or that has fix history), and whose name is never used as a string literal in
    any added line (config/dispatch). 'Non-public' = leading underscore (or lowercase in Go)."""
    cands = {}
    for s in ctx.alive_symbols:
        n = s["name"] or ""
        if s["kind"] not in ("function", "method") or not _prod(s["path"]) or (s["loc"] or 0) < 15:
            continue
        if ctx.callers(s["uid"]):
            continue
        private = (n.startswith("_") and not n.startswith("__")) or (s["path"].endswith(".go") and n[:1].islower())
        if not private:
            continue
        if n in ("main", "run", "setup") or re.match(r"(test|on_|handle_|visit_|do_)", n.lstrip("_")):
            ctx.drop("entrypoint_or_convention")
            continue
        cands.setdefault(n, []).append(s)
    if not cands:
        return
    names = set(cands)
    last_removed: dict[str, dict] = {}
    str_used: set[str] = set()
    decl = re.compile(r"^\s*(async\s+)?(def|function|func)\b")
    for r in ctx.stream("SELECT hash, seq, ts, path, sign, text FROM diff_lines ORDER BY seq"):
        t = r["text"]
        if r["sign"] == "+":
            for m in re.findall(r"['\"]([A-Za-z_]\w*)['\"]", t):
                if m in names:
                    str_used.add(m)
            continue
        if not is_code(r["path"]) or is_test(r["path"]) or decl.match(t):
            continue
        for m in re.finditer(r"(?<![\w])(\w+)\s*\(", t):
            if m.group(1) in names:
                last_removed[m.group(1)] = r
    for n, syms in cands.items():
        for s in syms:
            if n in str_used:
                ctx.drop("name_used_as_string")
                continue
            r = last_removed.get(n)
            if not r:
                ctx.drop("no_call_removal_found")
                continue
            if ctx.head_ts - r["ts"] < 180 * DAY:
                ctx.drop("dead_less_than_6_months")
                continue
            ht = ctx.text_at_head(s["path"]) or ""
            hl = ht.splitlines()
            if s["start_line"] and s["start_line"] >= 2 and hl[s["start_line"] - 2:s["start_line"] - 1] \
                    and hl[s["start_line"] - 2].strip().startswith("@"):
                ctx.drop("decorated_registry")
                continue
            c = ctx.by_hash[r["hash"]]
            lang = bool(re.search(r"(?i)\b(replac\w+|instead|obsolete|no longer|unused|deprecat\w+|superseded|dead|drop\w*|remov\w+|reject\w*|abandon\w*)\b",
                                  c["message"] or ""))
            fixes = ctx.fixes_by_uid.get(s["uid"], [])
            if not (lang or fixes):
                ctx.drop("no_rejection_language_or_fix_history")
                continue
            yield Hit(
                subject=s["uid"], score=(s["loc"] or 0) / 10 + 3 * lang + 2 * len(fixes) + (ctx.head_ts - r["ts"]) / (365 * DAY),
                title=f"{s['qualname']} ({s['loc']} lines) lost its last caller in {r['hash'][:10]} and is still defined",
                evidence={"loc": s["loc"], "last_call_removed": r["hash"], "removal_subject": subject_of(c["message"]),
                          "removed_line": r["text"].strip()[:160], "dead_days": round((ctx.head_ts - r["ts"]) / DAY),
                          "fix_history": len(fixes)},
                commits=[r["hash"], *[f["hash"] for f in fixes[:2]]], diffs=[(r["hash"], r["path"])],
                paths=[s["path"]], symbols=[s["uid"]],
            )


PIN_RX = (r"(?i)isort:\s*skip|noqa:?\s*E402|noqa:?\s*I00\d|fmt:\s*(off|skip)|must\s+(be\s+)?import(ed)?\s+(first|before|last|after)|"
          r"import\s+order|order\s+of\s+imports?|pylint:\s*disable=.*(wrong-import|cyclic-import)|eslint-disable.*import/(order|first)|"
          r"circular\s+import|avoid\s+circular|import\s+cycle")
IMPORT_FIX_RX = re.compile(r"(?i)\b(import\w*|circular|cycl\w+|order\w*)\b")


def _sccs(graph: dict[str, set[str]]) -> dict[str, int]:
    """Iterative Tarjan; returns node -> component size."""
    index, low, on, stack, comp, counter = {}, {}, set(), [], {}, [0]
    for root in list(graph):
        if root in index:
            continue
        work = [(root, iter(graph.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            adv = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(graph.get(w, ()))))
                    adv = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if adv:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                members = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    members.append(w)
                    if w == v:
                        break
                for w in members:
                    comp[w] = len(members)
    return comp


@candidate("import_order_pin_marker")
def import_order_pin_marker(ctx: Ctx):
    """An import-order pin marker (isort skip, noqa E402, 'must import first', ...) still at HEAD, backed by an IMPORTS
    cycle through the module, an import-related fix commit on the file, or a bare side-effect import. E402 after
    sys.path manipulation is dropped."""
    comp = _sccs(ctx.imports[0])
    for r in ctx.grep(PIN_RX, "+", path_regex=CODE_PATH_RX + r"|\.pyi$"):
        path, text = r["path"], r["text"]
        if is_test(path) or path.endswith(".pyi"):
            ctx.drop("tests_or_stubs")
            continue
        head = ctx.text_at_head(path)
        if not head or text.strip() not in head:
            ctx.drop("gone_at_head")
            continue
        hl = head.splitlines()
        ln = next((i for i, x in enumerate(hl) if x.strip() == text.strip()), 0)
        if re.search(r"(?i)E402", text) and any(re.search(r"sys\.path\.(insert|append)", x) for x in hl[:ln]):
            ctx.drop("e402_after_sys_path")
            continue
        if path.endswith("__init__.py") and re.search(r"^\s*from\s+\.", text):
            ctx.drop("init_reexport")
            continue
        c = ctx.by_hash[r["hash"]]
        if FORMAT_MSG_RX.search(subject_of(c["message"])):
            ctx.drop("lint_commit")
            continue
        signals = []
        if comp.get(path, 1) > 1:
            signals.append("import_cycle")
        later = [x for x in ctx.path_commits(path) if x["seq"] > c["seq"]][:25]
        fixes = [x for x in later if ctx.is_fix(x) and IMPORT_FIX_RX.search(x["message"] or "")]
        if fixes or IMPORT_FIX_RX.search(c["message"] or "") and "import" in (c["message"] or "").lower():
            signals.append("import_related_fix")
        if re.match(r"^\s*import\s+[\w.]+\s*(#.*)?$", text) or re.match(r"^\s*import\s+['\"]", text):
            signals.append("bare_side_effect_import")
        if not signals:
            ctx.drop("no_supporting_signal")
            continue
        yield Hit(
            subject=f"{path}|{text.strip()[:80]}", score=2 * len(signals) + (comp.get(path, 1) > 1) * 2 + len(fixes),
            title=f"Import pin in {path}: {text.strip()[:70]} ({', '.join(signals)})",
            evidence={"marker": text.strip(), "signals": signals, "cycle_size": comp.get(path, 1),
                      "related_fixes": [{"hash": f["hash"], "subject": subject_of(f["message"])} for f in fixes[:4]]},
            commits=[r["hash"], *[f["hash"] for f in fixes[:3]]], diffs=[(r["hash"], path)], paths=[path],
        )


def _params(line: str) -> list[str] | None:
    """Parameters of a one-line def/function signature, or None when unbalanced/multi-line."""
    i = line.find("(")
    if i < 0:
        return None
    depth, j = 0, i
    for j in range(i, len(line)):
        depth += (line[j] in "([{") - (line[j] in ")]}")
        if depth == 0:
            break
    else:
        return None
    if depth:
        return None
    inner, parts, d, cur = line[i + 1:j], [], 0, ""
    for ch in inner:
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        if ch == "," and d == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return [p for p in parts if p and p not in ("*", "/") and p.lstrip("*").split(":")[0].split("=")[0].strip() not in ("self", "cls")]


def _flag_params(params: list[str]) -> int:
    n = 0
    for p in params:
        d = p.split("=", 1)[1].strip() if "=" in p else ""
        if re.search(r":\s*bool\b", p) or d in ("None", "False", "True", "null", "undefined", "false", "true"):
            n += 1
    return n


def _growth_from_diffs(ctx: Ctx) -> dict[tuple[str, str], list]:
    """Fallback (no symbol_versions): paired one-line '-def f(' / '+def f(' diff lines; multi-line signatures skipped."""
    rows = ctx.grep(r"^\s*(async\s+)?def\s+\w+\s*\(|^\s*(export\s+)?(async\s+)?function\s+\w+\s*\(", None,
                    path_regex=r"\.(py|js|jsx|ts|tsx|mjs)$")
    growth: dict[tuple[str, str], list] = defaultdict(list)
    for (h, path), grp in _group_rows(rows).items():
        old: dict[str, list] = {}
        for r in grp:
            m = re.search(r"(?:def|function)\s+(\w+)", r["text"])
            ps = _params(r["text"])
            if not m or ps is None:
                continue
            if r["sign"] == "-":
                old[m.group(1)] = ps
            elif m.group(1) in old and len(ps) > len(old[m.group(1)]):
                growth[(path, m.group(1))].append((h, len(ps) - len(old[m.group(1)])))
    return growth


@candidate("parameter_accretion")
def parameter_accretion(ctx: Ctx):
    """A function whose signature keeps gaining parameters (>=3 commits adding params, >=4 params gained in total, >=2
    bool/None/False-default flags at HEAD), with in-degree >=5 and >=1 fix commit. Growth is read from the signature
    history in ``symbol_versions`` (``params`` of consecutive versions of the same symbol, so multi-line signatures and
    moves are handled); flags are counted in the latest recorded signature. Only when the repo has no symbol
    versions at all does it fall back to paired one-line '-def f(' / '+def f(' diff lines (multi-line signatures
    skipped)."""
    growth: dict[tuple[str, str], list] = {}
    by_key: dict[tuple[str, str], str] = {}
    if ctx.versions_by_uid:
        for uid, vs in ctx.versions_by_uid.items():
            if vs[0]["kind"] not in ("function", "method"):
                continue
            evs = []
            for a, b in zip(vs, vs[1:]):
                if a["params"] is not None and b["params"] is not None and b["params"] > a["params"]:
                    evs.append((b["hash"], b["params"] - a["params"]))
            if evs:
                key = (vs[-1]["path"], vs[-1]["qualname"])
                growth[key] = evs
                by_key[key] = uid
    else:
        growth = _growth_from_diffs(ctx)
    for (path, name), evs in growth.items():
        if len(evs) < 3 or sum(d for _, d in evs) < 4:
            continue
        if not _prod(path):
            ctx.drop("not_production_code")
            continue
        if (path, name) in by_key:
            sym = ctx.symbols.get(by_key[(path, name)])
            if sym is not None and not sym["alive"]:
                sym = None
        else:
            sym = next((s for s in ctx.symbols_by_path.get(path, []) if s["name"] == name), None)
        if sym is None:
            ctx.drop("symbol_gone")
            continue
        uid = sym["uid"]
        head = ctx.text_at_head(path) or ""
        hl = head.splitlines()
        prev = hl[sym["start_line"] - 2] if sym["start_line"] and sym["start_line"] >= 2 else ""
        if re.search(r"dataclass|click\.|typer\.|@app\.command|argparse", prev) or re.search(r"(Config|Settings|Options)$", sym["parent"] or ""):
            ctx.drop("config_ctor_or_cli_binder")
            continue
        n_in = len(ctx.callers(uid))
        if n_in < 5:
            ctx.drop("low_fan_in")
            continue
        fixes = ctx.fixes_by_uid.get(uid, [])
        if not fixes:
            ctx.drop("no_fix_commits")
            continue
        last = (ctx.versions_by_uid.get(uid) or [None])[-1]
        sig = last["signature"] if last else ""
        ps = _params(sig or "") if sig else None
        if ps is None:
            sig = hl[sym["start_line"] - 1] if sym["start_line"] and sym["start_line"] <= len(hl) else (sym["signature"] or "")
            ps = _params(sig) or _params(sym["signature"] or "") or []
        flags = _flag_params(ps)
        if flags < 2:
            ctx.drop("few_flag_params")
            continue
        yield Hit(
            subject=uid, score=sum(d for _, d in evs) * 1.5 + n_in * 0.3 + len(fixes) + flags,
            title=f"{sym['qualname']} gained {sum(d for _, d in evs)} parameters over {len(evs)} commits ({flags} flags), fan-in {n_in}",
            evidence={"signature": sig.strip()[:200], "param_count": len(ps), "flag_params": flags, "fan_in": n_in,
                      "growth_commits": [{"hash": h, "added": d} for h, d in evs[:6]],
                      "fixes": [f["subject"] for f in fixes[:4]]},
            commits=[h for h, _ in evs[:4]] + [f["hash"] for f in fixes[:2]],
            diffs=[(h, path) for h, _ in evs[:3]], paths=[path], symbols=[uid],
        )
