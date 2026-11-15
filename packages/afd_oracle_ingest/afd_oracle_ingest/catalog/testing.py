"""Catalogue family: testing and coverage (which untested code is worth testing, test erosion, test quality)."""
from __future__ import annotations

import json

from .base import DAY, Ctx, Hit, candidate, is_test


def _weight_at(ctx: Ctx, uid: str, ts: int) -> tuple[float, str]:
    """Best test weight for ``uid`` as of ``ts``: from the test map at the latest release tag before ``ts`` when one
    exists, else the HEAD map. Returns (weight, ref).

    ``test_map_at`` rows are built from the tag's source tree but HEAD's call/import edges, so rows whose test file was
    first committed after the tag, or whose target did not exist yet, are discarded here."""
    tag = ctx.tag_before(ts)
    if tag is not None:
        maps = _memo(ctx, "tmap_at", dict)
        ref = tag["name"]
        if ref not in maps:
            rows = ctx.test_map_at(ref)
            tc = ctx.by_hash.get(tag["commit_hash"])
            tag_seq = tc["seq"] if tc is not None else None
            existed: dict[str, bool] = {}

            def test_existed(test: str) -> bool:
                path = test.split("::", 1)[0]
                if path not in existed:
                    first = ctx.changes_by_path.get(path)
                    c0 = ctx.by_hash.get(first[0]["hash"]) if first else None
                    existed[path] = c0 is not None and c0["ts"] <= tag["ts"]
                return existed[path]

            m: dict[str, float] = {}
            for r in rows:
                t = ctx.symbols.get(r["target"])
                if not test_existed(r["test"]) or (t is not None and tag_seq is not None and t["first_seq"] is not None
                                                   and t["first_seq"] > tag_seq):
                    continue
                m[r["target"]] = max(m.get(r["target"], 0.0), r["weight"] or 0.0)
            maps[ref] = m if rows else None
        if maps[ref] is not None:
            return maps[ref].get(uid, 0.0), ref
    return ctx.best_test_weight(uid), "HEAD"


@candidate("fix_without_regression_test")
def fix_without_regression_test(ctx: Ctx):
    """A non-trivial fix with no test added in the commit or the following week, on a symbol no strong test covers.

    "No strong test" is judged at the time of the fix: the static test map rebuilt at the latest release tag before the
    fix (``test_map_at(tag_before(ts))``). Fixes older than the first tag (or when no tag maps were built) fall back to
    the HEAD test map, i.e. "linked now"."""
    by_uid_fixes: dict[str, int] = {}
    for f in ctx.fix_events:
        by_uid_fixes[f["uid"]] = by_uid_fixes.get(f["uid"], 0) + 1
    test_touch_ts: list[tuple[int, str]] = sorted(
        (c["ts"], c["author_email"]) for c in ctx.commits
        if any(is_test(fc["path"]) for fc in ctx.changes(c["hash"])))
    for f in ctx.fix_events:
        sym = ctx.symbols.get(f["uid"])
        if sym is None or not sym["alive"] or is_test(f["path"]):
            ctx.drop("symbol_gone_or_test")
            continue
        if json.loads(f["tests"] or "[]"):
            ctx.drop("test_in_same_commit")
            continue
        added, removed = ctx.added_removed(f["hash"], f["path"])
        if len(added) + len(removed) < 2:
            ctx.drop("trivial_fix")
            continue
        c = ctx.commit(f["hash"])
        if any(f["ts"] <= ts <= f["ts"] + 7 * DAY and who == c["author_email"] for ts, who in test_touch_ts):
            ctx.drop("delayed_test_same_author")
            continue
        weight, at_ref = _weight_at(ctx, f["uid"], f["ts"])
        if weight >= 0.6:
            ctx.drop("strong_test_link")
            continue
        repeats = by_uid_fixes.get(f["uid"], 1)
        zone = [k for k in json.loads(f["kinds"] or "[]") if k != "bug"]
        score = repeats * (2 if zone else 1) * (1 + 0.1 * min(20, len(ctx.callers(f["uid"])))) * (1 - weight)
        yield Hit(
            subject=f"{f['hash']}|{f['uid']}",
            score=score,
            title=f"{f['subject'][:90]} — {sym['qualname']} has no regression test",
            evidence={"fix_subject": f["subject"], "zones": zone, "fixes_on_symbol": repeats,
                      "guards_added": json.loads(f["guards"] or "[]")[:6], "best_test_weight": weight, "test_map_ref": at_ref,
                      "linked_tests": [r["test"] for r in ctx.tests_for.get(f["uid"], [])][:6],
                      "callers": len(ctx.callers(f["uid"]))},
            commits=[f["hash"]], diffs=[(f["hash"], f["path"])], paths=[f["path"]], symbols=[f["uid"]],
        )


# =====================================================================================================================
# Testing family, part A: test-gap evidence, assertion quality, skipped/disabled tests, seams and mocks.
#
# Data caveats shared by the builders below:
#   * Complexity is the AST estimate in ``symbol_versions`` (``_complexity``); ``_cc`` (branch keywords + 1) is only the
#     fallback. Measured sandbox coverage is used where present (``_measured_cov``); a static/coverage ``test_map`` row
#     stands in for "covered" otherwise.
#   * ``test_map`` reflects HEAD only, so where "linked at the time of a fix" matters the release-tag maps
#     (``test_map_at``) are used (``_weight_at``), else "linked now, and the test file already existed".
#   * Python test files are analysed in depth (ast per function); other languages fall back to file-level regexes.
# =====================================================================================================================
import ast
import difflib
import re
from collections import defaultdict

from .base import (CI_RX, DEP_MANIFEST_RX, LOCKFILE_RX, MIGRATION_RX, TEST_RX, is_ci, is_code, is_config, is_docs,
                   is_generated, is_vendored, subject_of)

ASSERT_RX = re.compile(r"\bassert\b|\bself\.assert\w*\(|\bassert_\w+\(|\bexpect\(|\.should\b|pytest\.raises|"
                       r"pytest\.warns|\bassertRaises|\.assert_\w+\(|\bt\.(Error|Fatal|Fail)\w*\(|\bassert\w*\(")
HELPER_ASSERT_RX = re.compile(r"\b(assert|check|verify|expect)\w*\s*\(")
BRANCH_RX = re.compile(r"\b(if|elif|else if|for|while|except|catch|case|and|or|&&|\|\||\?)\b")
IO_RX = re.compile(r"\b(open|urlopen|urlretrieve)\(|requests\.|httpx\.|aiohttp|urllib|socket\.|subprocess\.|os\.system|"
                   r"\.read_text\(|\.write_text\(|\.read_bytes\(|\.write_bytes\(|\.execute\(|cursor\.|sqlite3\.|"
                   r"os\.(remove|unlink|rename|makedirs|mkdir|listdir|walk)\(|shutil\.|\bfetch\(|boto3|redis|"
                   r"smtplib|psycopg|pymysql|\.recv\(|\.send\(|\.commit\(|fs\.(read|write)\w*\(|child_process")
SKIP_RX = re.compile(r"@pytest\.mark\.(skip|xfail)\w*|@unittest\.(skip|expectedFailure)\w*|\bpytest\.(skip|xfail)\(|"
                     r"@skip\w*\(|\b(xit|xdescribe|xtest)\(|\b(it|test|describe)\.(skip|fails)\b|\bt\.Skip\(|"
                     r"@Disabled|@Ignore\b|pytest\.importorskip\(")
CONDITIONAL_SKIP_RX = re.compile(r"(?i)skipif|importorskip|sys\.platform|platform\.|version_info|python_version|"
                                 r"os\.name|win32|\blinux\b|darwin|pypy|not\s+installed|requires?\b")
DAYS90 = 90 * DAY


def _memo(ctx: Ctx, key: str, fn):
    d = ctx.__dict__.setdefault("_testing_a_memo", {})
    if key not in d:
        d[key] = fn()
    return d[key]


def _cc(body: str | None) -> int:
    """Cyclomatic-complexity proxy: 1 + branch keywords in the body (no AST complexity is stored)."""
    return 1 + len(BRANCH_RX.findall(body or ""))


def _complexity(ctx: Ctx, s) -> tuple[int, str]:
    """(complexity, source): the AST estimate recorded for the latest version of the symbol (``ctx.complexity``), else
    the branch-keyword proxy over the body."""
    c = ctx.complexity(s["uid"])
    return (c, "ast") if c is not None else (_cc(s["body"]), "keyword_proxy")


def _measured_cov(ctx: Ctx, uid: str) -> float | None:
    """Fraction of the symbol's body lines executed by the sandbox test run, or None when coverage was not measured
    (callers then use the static test map)."""
    if not ctx.coverage_available:
        return None
    sc = ctx.symbol_coverage(uid)
    if sc is None or sc[1] <= 0:
        return None
    return sc[0] / sc[1]


def _loc(sym) -> int:
    return int(sym["loc"] or ((sym["end_line"] or 0) - (sym["start_line"] or 0) + 1))


def _assert_n(text: str | None) -> int:
    return sum(1 for ln in (text or "").splitlines() if ASSERT_RX.search(ln) and not ln.lstrip().startswith("#"))


def _tid(sym) -> str:
    return f"{sym['path']}::{sym['qualname'].replace('.', '::')}"


def _uid_for_tid(ctx: Ctx, tid: str) -> str:
    path, _, rest = tid.partition("::")
    return f"sym:{ctx.repo_id}:{path}:{rest.replace('::', '.')}:function"


def _test_file(tid: str) -> str:
    return tid.split("::", 1)[0]


def _stem(path: str) -> str:
    b = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    for a in ("test_", "tests_"):
        if b.startswith(a):
            b = b[len(a):]
    for a in ("_test", "_tests"):
        if b.endswith(a):
            b = b[: -len(a)]
    return b


def _py_funcs(text: str | None) -> dict[str, tuple[str, int, int]]:
    """qualname -> (source incl. decorators, first line, last line) for every def in Python text."""
    out: dict[str, tuple[str, int, int]] = {}
    if not text:
        return out
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    lines = text.splitlines()

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out[prefix + ch.name] = ("\n".join(lines[start - 1:ch.end_lineno]), start, ch.end_lineno or start)
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
    walk(tree, "")
    return out


def _risk(ctx: Ctx) -> dict[str, tuple[float, dict]]:
    def build():
        return {r["subject"]: (r["score"] or 0.0, json.loads(r["factors"] or "{}"))
                for r in ctx.q("SELECT * FROM risk WHERE kind='symbol'")}
    return _memo(ctx, "risk", build)


def _is_prod_func(s) -> bool:
    return s["kind"] == "function" and is_code(s["path"]) and not is_test(s["path"])


def _prod_funcs(ctx: Ctx) -> list:
    return _memo(ctx, "prod_funcs", lambda: [s for s in ctx.alive_symbols if _is_prod_func(s)])


def _test_funcs(ctx: Ctx, alive_only: bool = True) -> dict[str, object]:
    """tid -> symbol row for python test functions (name starts with test)."""
    def build():
        out = {}
        for s in ctx.symbols.values():
            if s["kind"] == "function" and is_test(s["path"]) and s["path"].endswith(".py") \
                    and s["name"].startswith("test") and not s["path"].endswith("conftest.py"):
                t = _tid(s)
                if t not in out or s["alive"]:
                    out[t] = s
        return out
    allf = _memo(ctx, "test_funcs", build)
    return {t: s for t, s in allf.items() if s["alive"]} if alive_only else allf


def _links(ctx: Ctx, uid: str, sources: tuple[str, ...] = ("static", "coverage")) -> list[str]:
    return sorted({r["test"] for r in ctx.tests_for.get(uid, []) if r["source"] in sources})


def _targets_by_test(ctx: Ctx) -> dict[str, set[str]]:
    def build():
        out: dict[str, set[str]] = defaultdict(set)
        for rows in ctx.tests_for.values():
            for r in rows:
                if r["source"] in ("static", "coverage"):
                    out[r["test"]].add(r["target"])
        return out
    return _memo(ctx, "targets_by_test", build)


def _fix_hashes(ctx: Ctx, uid: str) -> list[str]:
    seen: dict[str, None] = {}
    for f in ctx.fixes_by_uid.get(uid, []):
        seen[f["hash"]] = None
    return list(seen)


def _kinds(f) -> list[str]:
    return json.loads(f["kinds"] or "[]")


def _short(text: str | None, n: int = 700) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + " ..."


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _head_lines(ctx: Ctx, path: str) -> list[str]:
    return (ctx.text_at_head(path) or "").splitlines()


def _pct_threshold(values: list[float], pct: float) -> float:
    vals = sorted(values)
    return vals[int(pct * (len(vals) - 1))] if vals else 0.0


def _subj(ctx: Ctx, h: str) -> str:
    c = ctx.by_hash.get(h)
    return subject_of(c["message"]) if c else ""


# --------------------------------------------------------------------------------------------------- false_test_gap
@candidate("false_test_gap")
def false_test_gap(ctx: Ctx):
    """High-risk symbols the risk model calls untested (test_gap=1) with >=2 fix commits, split by whether tests were
    actually edited in those fixes (level 0 truly untested ... 3 caller-importing test edited in >=30% of commits).
    Risk percentile uses the stored composite symbol risk. Level 3 looks at tests importing the symbol's file or the
    file of any caller within two CALLS hops (``ctx.callers_within(uid, 2)``, a real transitive walk); a test file
    reached only through a 2-hop caller is reported in the evidence as ``two_hop_only``. No coverage data is needed;
    the walk uses the stored call graph (a symbol with no recorded callers degrades to its own file)."""
    risk = _risk(ctx)
    if not risk:
        return
    thr = _pct_threshold([v[0] for v in risk.values()], 0.8)
    imported_by = ctx.imports[1]
    hunk_rx = re.compile(ASSERT_RX.pattern + r"|\bexpected\b|\bsetUp\b|\bsetup\w*|fixture|parametrize|==")

    def test_hunks_in(h: str) -> dict[str, bool]:
        out = {}
        for fc in ctx.changes(h):
            p = fc["path"]
            if not is_test(p) or p.endswith("conftest.py") or is_generated(p) or "snapshot" in p.lower():
                continue
            out[p] = any(hunk_rx.search(r["text"]) for r in ctx.lines_of(h, p))
        return out

    for uid, (score, f) in risk.items():
        if f.get("test_gap", 0) < 1 or score < thr:
            continue
        sym = ctx.symbols.get(uid)
        if sym is None or not sym["alive"]:
            continue
        fixes = _fix_hashes(ctx, uid)
        if len(fixes) < 2:
            continue
        if _loc(sym) < 5:
            ctx.drop("tiny_symbol")
            continue
        if _is_dunder(sym["name"]) or re.search(r"@(\w+\.)?(property|cached_property)\b", sym["body"] or ""):
            ctx.drop("dunder_or_property")
            continue
        if is_generated(sym["path"]) or is_vendored(sym["path"]):
            ctx.drop("generated_or_vendored")
            continue
        touched: dict[str, set[str]] = defaultdict(set)
        helper_only = False
        for h in fixes:
            for p in {fc["path"] for fc in ctx.changes(h) if is_test(fc["path"])}:
                if p.endswith("conftest.py") or "snapshot" in p.lower() or LOCKFILE_RX.search(p):
                    helper_only = True
            for p, has in test_hunks_in(h).items():
                if has:
                    touched[p].add(h)
        if helper_only and not touched:
            ctx.drop("helper_only_coedit")
        level = 0
        if touched:
            level = 1
        if any(len(v) >= 2 for v in touched.values()):
            level = 2
        sym_commits = {e["hash"] for e in ctx.symbol_events(uid)} or set(fixes)
        walk = ctx.callers_within(uid, 2)
        near1 = {sym["path"]} | {ctx.symbols[c]["path"] for c, d in walk.items() if d == 1 and c in ctx.symbols}
        near2 = {ctx.symbols[c]["path"] for c, d in walk.items() if d == 2 and c in ctx.symbols} - near1
        test_files = {t for p in near1 | near2 for t in imported_by.get(p, ()) if is_test(t)}
        two_hop_only = False
        if sym_commits and test_files:
            near1_tests = {t for p in near1 for t in imported_by.get(p, ()) if is_test(t)}
            for t in sorted(test_files):
                hs = {r["hash"] for r in ctx.changes_by_path.get(t, [])}
                if len(hs & sym_commits) / len(sym_commits) >= 0.3:
                    level = 3
                    two_hop_only = t not in near1_tests
                    break
        direction = "truly_untested_and_risky" if level == 0 else "falsely_gapped"
        prio = 3.0 if level == 0 else 1.0
        yield Hit(
            subject=uid, score=prio * len(fixes) * (1 + score / 100),
            title=(f"{sym['qualname']}: risk {score:.0f}, {len(fixes)} fixes, test gap "
                   f"{'genuine (no test edited in any fix)' if level == 0 else f'looks false (evidence level {level})'}"),
            evidence={"direction": direction, "evidence_level": level, "risk": score, "fixes": len(fixes),
                      "fix_subjects": [_subj(ctx, h) for h in fixes[:5]],
                      "tests_edited_in_fixes": {p: len(v) for p, v in touched.items()},
                      "callers": len(ctx.callers(uid)), "callers_2hop": len(walk), "two_hop_only": two_hop_only},
            commits=fixes[:6], diffs=[(h, sym["path"]) for h in fixes[:4]], paths=[sym["path"]] + sorted(touched)[:3],
            symbols=[uid])


# ------------------------------------------------------------------------------------------- pseudo_tested_function
def _pseudo_tests(ctx: Ctx) -> dict[str, object]:
    """tid -> test symbol with zero assertion constructs after one-hop helper expansion."""
    def build():
        out = {}
        tbt = _targets_by_test(ctx)
        for tid, s in _test_funcs(ctx).items():
            body = s["body"] or ""
            name = s["name"].lower()
            linked = tid in tbt
            if re.search(r"smoke|import|starts|does_not_raise|main|doctest", name):
                if linked:
                    ctx.drop("smoke_import_main_test")
                continue
            if "snapshot" in body.lower():
                if linked:
                    ctx.drop("snapshot_test")
                continue
            if re.search(r"@(\w+\.)?given\b|hypothesis", body):
                if linked:
                    ctx.drop("property_based_test")
                continue
            if ASSERT_RX.search(body) or HELPER_ASSERT_RX.search(body):
                continue
            helper_asserts = False
            for c in ctx.callees(s["uid"]):
                cs = ctx.symbols.get(c)
                if cs and is_test(cs["path"]) and (ASSERT_RX.search(cs["body"] or "")
                                                   or HELPER_ASSERT_RX.search(cs["name"] + "(")):
                    helper_asserts = True
                    break
            if helper_asserts:
                ctx.drop("asserts_via_helper")
                continue
            out[tid] = s
        return out
    return _memo(ctx, "pseudo_tests", build)


@candidate("pseudo_tested_function")
def pseudo_tested_function(ctx: Ctx):
    """Production functions whose every statically linked test has no assertion construct (phase 1 only). Python tests
    only; JS tests are file-level ids and are treated as asserting (no link made)."""
    pseudo = _pseudo_tests(ctx)
    if not pseudo:
        return
    risk = _risk(ctx)
    for s in _prod_funcs(ctx):
        uid = s["uid"]
        tids = _links(ctx, uid)
        if not tids or not all(t in pseudo for t in tids):
            continue
        fixes = _fix_hashes(ctx, uid)
        fan_in = len(ctx.callers(uid))
        rscore = risk.get(uid, (0, {}))[0]
        if not (fixes or fan_in >= 5 or rscore >= 60):
            continue
        if _loc(s) < 3 or re.search(r"@(\w+\.)?(property|cached_property)\b", s["body"] or ""):
            ctx.drop("trivial_getter")
            continue
        if s["name"] == "main" or _is_dunder(s["name"]):
            ctx.drop("cli_main_or_dunder")
            continue
        yield Hit(
            subject=uid, score=3 * len(fixes) + fan_in + rscore / 20 + 1,
            title=f"{s['qualname']} is linked only to tests with no assertions ({len(tids)} test(s))",
            evidence={"tests": tids[:6], "fixes": len(fixes), "fan_in": fan_in, "risk": rscore,
                      "test_bodies": {t: _short(pseudo[t]["body"], 300) for t in tids[:2]}},
            commits=fixes[:5], diffs=[(h, s["path"]) for h in fixes[:3]],
            paths=[s["path"]] + [_test_file(t) for t in tids[:3]],
            symbols=[uid] + [pseudo[t]["uid"] for t in tids[:3]])


# ----------------------------------------------------------------------------------------- test_deleted_behavior_kept
@candidate("test_deleted_behavior_kept")
def test_deleted_behavior_kept(ctx: Ctx):
    """A test (named test_<target>) deleted, or whose assertion count fell >=50%, while the target function still
    exists at HEAD. Rank by fixes on the target after the change, then age. Reduced-assertion detection compares
    ast-parsed test functions before/after for commits that removed assertion lines."""
    prod_by_name: dict[str, list] = defaultdict(list)
    for s in _prod_funcs(ctx):
        prod_by_name[s["name"]].append(s)
    alive_tests = _test_funcs(ctx)
    alive_names = defaultdict(list)
    for t, s in alive_tests.items():
        alive_names[s["name"]].append(s)

    def target_for(test_name: str, test_path: str):
        base = re.sub(r"^test_?", "", test_name)
        cands = prod_by_name.get(base, [])
        if not cands:
            return None
        same = [c for c in cands if _stem(c["path"]) == _stem(test_path)]
        if same:
            return same[0]
        return cands[0] if len(cands) == 1 else None

    def examine(change_hash, tpath, tname, tuid, old_body, kind, new_assert):
        c = ctx.commit(change_hash)
        if c is None or c["is_bulk"] or c["is_merge"]:
            ctx.drop("bulk_commit")
            return None
        short = tname.rsplit(".", 1)[-1]
        target = target_for(short, tpath)
        if target is None:
            return None
        if target["first_seq"] is None or target["first_seq"] >= c["seq"]:
            ctx.drop("target_created_in_same_commit")
            return None
        for e in ctx.events_by_commit.get(change_hash, []):
            if e["uid"] == target["uid"] and e["change"] in ("renamed", "deleted", "moved"):
                ctx.drop("target_renamed_in_commit")
                return None
        if kind == "deleted":
            if alive_names.get(short):
                ctx.drop("same_name_test_exists")
                return None
            for lt in _links(ctx, target["uid"]):
                ls = alive_tests.get(lt)
                if ls and difflib.SequenceMatcher(None, old_body, ls["body"] or "").ratio() >= 0.8:
                    ctx.drop("similar_replacement_test")
                    return None
        later = [f for f in ctx.fixes_by_uid.get(target["uid"], []) if f["ts"] > c["ts"]]
        age_y = (ctx.head_ts - c["ts"]) / (365 * DAY)
        old_n = _assert_n(old_body)
        what = "deleted" if kind == "deleted" else f"lost assertions ({old_n} -> {new_assert})"
        return Hit(
            subject=f"{change_hash}|{tuid}", score=1 + 3 * len(later) + age_y + 0.2 * len(ctx.callers(target["uid"])),
            title=f"{tname} {what} while {target['qualname']} remains",
            evidence={"change": kind, "assertions_before": old_n, "assertions_after": new_assert,
                      "target": target["qualname"], "fixes_on_target_after": len(later),
                      "fix_subjects_after": [f["subject"] for f in later[:4]], "age_years": round(age_y, 1),
                      "commit_subject": subject_of(c["message"]), "old_test_body": _short(old_body)},
            commits=[change_hash] + [f["hash"] for f in later[:3]], diffs=[(change_hash, tpath)],
            paths=[tpath, target["path"]], symbols=[tuid, target["uid"]])

    seen: set[str] = set()
    for e in ctx.q("SELECT * FROM symbol_events WHERE change='deleted' AND kind='function'"):
        if not is_test(e["path"]) or not e["path"].endswith(".py") or not e["qualname"].rsplit(".", 1)[-1].startswith("test"):
            continue
        s = ctx.symbols.get(e["uid"])
        old_body = (s["body"] if s else "") or ""
        if not ASSERT_RX.search(old_body):
            ctx.drop("deleted_test_had_no_assertions")
            continue
        h = examine(e["hash"], e["path"], e["qualname"], e["uid"], old_body, "deleted", None)
        if h:
            seen.add(h.subject)
            yield h
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for r in ctx.grep(ASSERT_RX.pattern, "-", path_regex=TEST_RX.pattern, limit=60000):
        if r["path"].endswith(".py"):
            groups[(r["hash"], r["path"])] += 1
    for (h, path), _n in list(groups.items())[:400]:
        c = ctx.commit(h)
        if c is None or c["is_bulk"] or c["is_merge"]:
            continue
        before, after = _py_funcs(ctx.text_before(h, path)), _py_funcs(ctx.text_at(h, path))
        for q, (src, _a, _b) in before.items():
            if not q.rsplit(".", 1)[-1].startswith("test") or q not in after:
                continue
            b, a = _assert_n(src), _assert_n(after[q][0])
            if b >= 2 and a <= b * 0.5:
                hit = examine(h, path, q, f"sym:{ctx.repo_id}:{path}:{q}:function", src, "reduced", a)
                if hit and hit.subject not in seen:
                    seen.add(hit.subject)
                    yield hit


# ------------------------------------------------------------------------------------------------ proven_guard_test
@candidate("proven_guard_test")
def proven_guard_test(ctx: Ctx):
    """Test functions edited (with an assert/expect/parametrize change inside the function) in >=3 distinct fix commits
    that also changed production code; with up to 3 under-tested sibling symbols from the tests' target modules."""
    assert_rx = re.compile(ASSERT_RX.pattern + r"|parametrize")
    per_test: dict[str, dict[str, str]] = defaultdict(dict)   # tid -> {hash: subject}
    for c in ctx.commits:
        if c["is_merge"] or c["is_bulk"] or not ctx.is_fix(c):
            continue
        ch = ctx.changes(c["hash"])
        tpaths = [x["path"] for x in ch if is_test(x["path"]) and x["path"].endswith(".py")
                  and not x["path"].endswith("conftest.py")]
        if not tpaths:
            continue
        if not any(is_code(x["path"]) and not is_test(x["path"]) and not str(x["status"]).startswith("R") for x in ch):
            ctx.drop("no_production_change")
            continue
        for p in tpaths:
            rows = ctx.lines_of(c["hash"], p)
            adds = [r for r in rows if r["sign"] == "+" and assert_rx.search(r["text"])]
            if not adds:
                continue
            norm_add = sorted(re.sub(r"\s+", "", r["text"]) for r in rows if r["sign"] == "+")
            norm_rem = sorted(re.sub(r"\s+", "", r["text"]) for r in rows if r["sign"] == "-")
            if norm_add == norm_rem:
                ctx.drop("format_only_edit")
                continue
            funcs = _py_funcs(ctx.text_at(c["hash"], p))
            for q, (_src, a, b) in funcs.items():
                if q.rsplit(".", 1)[-1].startswith("test") and any(a <= r["line"] <= b for r in adds):
                    per_test[f"{p}::{q.replace('.', '::')}"][c["hash"]] = subject_of(c["message"])
    prod_by_path: dict[str, list] = defaultdict(list)
    for s in _prod_funcs(ctx):
        prod_by_path[s["path"]].append(s)
    tbt = _targets_by_test(ctx)
    for tid, fixes in per_test.items():
        if len(fixes) < 3:
            continue
        ts = ctx.symbols.get(_uid_for_tid(ctx, tid))
        if ts is None:
            continue
        target_paths = {ctx.symbols[t]["path"] for t in tbt.get(tid, ()) if t in ctx.symbols}
        sibs = []
        for p in target_paths:
            for s in prod_by_path.get(p, []):
                fh = _fix_hashes(ctx, s["uid"])
                if len(fh) < 2:
                    continue
                lt = _links(ctx, s["uid"])
                edited = {h for h in fh if any(_test_file(t) in {x["path"] for x in ctx.changes(h)} for t in lt)}
                counts = [_assert_n(ctx.symbols[_uid_for_tid(ctx, t)]["body"]) if _uid_for_tid(ctx, t) in ctx.symbols
                          else 2 for t in lt]
                weak = not lt or max(counts) <= 1
                if len(fh) - len(edited) >= 2 or weak:
                    sibs.append((len(fh), s, fh))
        sibs.sort(key=lambda x: -x[0])
        top = sibs[:3]
        hs = list(fixes)
        yield Hit(
            subject=ts["uid"], score=10 * len(fixes) + sum(n for n, _s, _f in top),
            title=f"{ts['qualname']} was tightened in {len(fixes)} separate fixes; {len(sibs)} sibling(s) lack such tests",
            evidence={"distinct_fixes": len(fixes), "fix_subjects": list(fixes.values())[:6],
                      "test_body": _short(ts["body"], 900),
                      "siblings": [{"symbol": s["qualname"], "fixes": n, "fix_subjects": [_subj(ctx, h) for h in fh[:3]]}
                                   for n, s, fh in top]},
            commits=hs[:6], diffs=[(h, _test_file(tid)) for h in hs[:4]], paths=[_test_file(tid)] + sorted(target_paths),
            symbols=[ts["uid"]] + [s["uid"] for _n, s, _f in top])


# ------------------------------------------------------------------------------------------------------ crap_hotspot
@candidate("crap_hotspot")
def crap_hotspot(ctx: Ctx):
    """CRAP hotspots: complexity >=10, >=2 fixes, churn >= 75th percentile (modifications in non-bulk, non-merge
    commits), body >=20 lines. Collapsing against the fix-prone-untested packets is left to the runner.

    Complexity is the AST estimate stored per symbol version (``ctx.complexity``: decision points + 1); symbols without
    a recorded version use the branch-keyword count + 1. Coverage: when the sandbox measured the symbol's file
    (``ctx.symbol_coverage``) the tier is "measured" and CRAP = cc^2 * (1 - cov)^3 + cc must be >= 30 (the static test
    link is not consulted). Otherwise the "unmeasured" tier applies: no static/coverage test link, CRAP = cc^2 + cc."""
    rows = ctx.q("SELECT se.uid, count(*) n FROM symbol_events se JOIN commits c ON c.hash=se.hash "
                 "WHERE c.is_bulk=0 AND c.is_merge=0 AND se.change='modified' GROUP BY se.uid")
    churn = {r["uid"]: r["n"] for r in rows}
    funcs = _prod_funcs(ctx)
    thr = _pct_threshold([churn[s["uid"]] for s in funcs if churn.get(s["uid"], 0) >= 1], 0.75)
    for s in funcs:
        uid, body = s["uid"], s["body"] or ""
        fixes = _fix_hashes(ctx, uid)
        if len(fixes) < 2:
            continue
        cc, cc_src = _complexity(ctx, s)
        if cc < 10 or _loc(s) < 20:
            continue
        cov = _measured_cov(ctx, uid)
        if cov is not None:
            if cc * cc * (1 - cov) ** 3 + cc < 30:
                ctx.drop("measured_coverage_adequate")
                continue
        elif _links(ctx, uid):
            continue
        p = s["path"]
        if is_generated(p) or is_vendored(p) or MIGRATION_RX.search(p) or (is_config(p) and not p.endswith(".py")):
            ctx.drop("generated_vendored_migration_config")
            continue
        n = churn.get(uid, 0)
        if n < 1:
            ctx.drop("initial_commit_churn_only")
            continue
        if n < thr:
            ctx.drop("low_churn_percentile")
            continue
        nonblank = [ln for ln in body.splitlines() if ln.strip()]
        flat = sum(1 for ln in nonblank if re.match(r"\s*(elif|case|else if)\b|\s*['\"][^'\"]+['\"]\s*:", ln))
        if flat / max(1, len(nonblank)) > 0.5:
            ctx.drop("flat_dispatch_table")
            continue
        if ctx.head_ts - (s["last_ts"] or 0) > 730 * DAY and not fixes:
            ctx.drop("stale_no_fixes")
            continue
        if cov is not None:
            crap = round(cc * cc * (1 - cov) ** 3 + cc, 1)
            title = (f"{s['qualname']}: complexity {cc}, {len(fixes)} fixes, {n} edits, {cov:.0%} measured coverage "
                     f"(CRAP {crap})")
        else:
            crap = cc * cc + cc
            title = (f"{s['qualname']}: complexity~{cc}, {len(fixes)} fixes, {n} edits, no linked test "
                     f"(unmeasured CRAP~{crap})")
        yield Hit(
            subject=uid, score=cc * len(fixes) * (1 + n / max(1, thr)) * (1 if cov is None else max(0.3, 1 - cov)),
            title=title,
            evidence={"tier": "unmeasured" if cov is None else "measured", "complexity_proxy": cc,
                      "complexity_source": cc_src, "measured_coverage": None if cov is None else round(cov, 3),
                      "crap_no_coverage": cc * cc + cc, "crap": crap, "fixes": len(fixes),
                      "churn_edits": n, "churn_p75": thr, "loc": _loc(s),
                      "fix_subjects": [_subj(ctx, h) for h in fixes[:5]], "callers": len(ctx.callers(uid))},
            commits=fixes[:6], diffs=[(h, p) for h in fixes[:4]], paths=[p], symbols=[uid])


# ------------------------------------------------------------------------------------------- skip_xfail_never_removed
@candidate("skip_xfail_never_removed")
def skip_xfail_never_removed(ctx: Ctx):
    """Skip/xfail markers still present at HEAD (same line text), added >=90 days before the last commit, on tests whose
    linked subject has risk>=50 or a fix since the marker was added. Boosts: unconditional marker, TODO/flaky reason,
    referenced issue closed in discussions."""
    rows = ctx.grep(SKIP_RX.pattern, "+", path_regex=TEST_RX.pattern, limit=20000)
    first: dict[tuple[str, str], object] = {}
    for r in rows:
        first.setdefault((r["path"], r["text"].strip()), r)
    risk = _risk(ctx)
    tbt = _targets_by_test(ctx)
    closed = {}
    try:
        closed = {d["number"]: d["state"] for d in ctx.q("SELECT number, state FROM discussions")}
    except Exception:
        pass
    for (path, text), r in first.items():
        if is_vendored(path):
            ctx.drop("vendored_test")
            continue
        hl = _head_lines(ctx, path)
        idxs = [i for i, ln in enumerate(hl) if ln.strip() == text]
        if not idxs:
            continue
        ln_no = idxs[0] + 1
        prev = hl[idxs[0] - 1].strip() if idxs[0] else ""
        if CONDITIONAL_SKIP_RX.search(text) or (text.startswith("pytest.skip(") and prev.startswith(("if ", "elif ", "else"))):
            ctx.drop("conditional_skip")
            continue
        age = ctx.head_ts - r["ts"]
        if age < DAYS90:
            ctx.drop("marker_under_90_days")
            continue
        sym = ctx.symbol_at_line(path, ln_no)
        if sym is not None and sym["kind"] == "class":
            inner = [x for x in ctx.symbols_by_path.get(path, []) if x["parent"] == sym["uid"]]
            sym = inner[0] if inner else sym
        tid = _tid(sym) if sym is not None else path
        if sym is None:
            subjects = {t for k, v in tbt.items() if _test_file(k) == path for t in v}
        else:
            subjects = set(tbt.get(tid, ()))
        subjects = {u for u in subjects if u in ctx.symbols and not is_test(ctx.symbols[u]["path"])}
        if not subjects:
            ctx.drop("no_linked_subject")
            continue
        fixes_since = [f for u in subjects for f in ctx.fixes_by_uid.get(u, []) if f["ts"] > r["ts"]]
        rk = max((risk.get(u, (0, {}))[0] for u in subjects), default=0)
        if rk < 50 and not fixes_since:
            ctx.drop("low_risk_subject_no_fix")
            continue
        only = [u for u in subjects if all(_test_file(t) == path for t in _links(ctx, u))]
        unconditional = "skipif" not in text
        boost = 2.0 if unconditional else 0.0
        boost += 1 if re.search(r"(?i)todo|flaky|fixme|broken|temporar", text) else 0
        issue = re.search(r"#(\d+)", text)
        if issue and str(closed.get(int(issue.group(1)), "")).lower() in ("closed", "merged"):
            boost += 2
        yield Hit(
            subject=f"{path}:{ln_no}:{text[:60]}", score=1 + boost + 1.5 * len(fixes_since) + rk / 50 + age / (365 * DAY),
            title=f"{tid} carries `{text[:70]}` since {int(age / DAY)} days; subject has {len(fixes_since)} fixes since",
            evidence={"marker": text, "line": ln_no, "age_days": int(age / DAY), "unconditional": unconditional,
                      "subjects": sorted(ctx.symbols[u]["qualname"] for u in subjects)[:5], "risk": rk,
                      "fixes_since": [f["subject"] for f in fixes_since[:5]],
                      "skipped_test_is_only_static_link_for": [ctx.symbols[u]["qualname"] for u in only][:5]},
            commits=[r["hash"]] + [f["hash"] for f in fixes_since[:3]], diffs=[(r["hash"], path)],
            paths=[path], symbols=sorted(subjects)[:5])


# ------------------------------------------------------------------------------------------ non_code_dependency_gap
RESOURCE_RX = re.compile(r"(?i)\.(sql|ya?ml|json|toml|ini|cfg|html?|jinja2?|tmpl|j2)$|(^|/)templates/|(^|/)migrations?/|"
                         r"(^|/)fixtures/|(^|/)conftest\.py$")


@candidate("non_code_dependency_gap")
def non_code_dependency_gap(ctx: Ctx):
    """Resource files (sql/yaml/json/templates/migrations/fixtures/conftest) edited in fix commits with tests_to_run <0.3
    (best test_map weight on the file), emitted when a test names the file as a string literal (a) or a test was edited
    in the same commit (b). Ranked by distinct fix commits; max 2 per directory."""
    per_file: dict[str, list[str]] = defaultdict(list)
    for c in ctx.commits:
        if c["is_merge"] or c["is_bulk"] or not ctx.is_fix(c):
            continue
        for fc in ctx.changes(c["hash"]):
            p = fc["path"]
            if not RESOURCE_RX.search(p):
                continue
            if LOCKFILE_RX.search(p) or is_ci(p) or is_docs(p) or "snapshot" in p.lower() or p.endswith(".snap"):
                ctx.drop("lock_ci_docs_snapshot")
                continue
            if is_test(p) and not p.endswith("conftest.py") and "/fixtures/" not in p:
                continue
            per_file[p].append(c["hash"])
    if not per_file:
        return
    test_texts = _memo(ctx, "test_texts", lambda: {p: (ctx.text_at_head(p) or "") for p in ctx.head_files
                                                      if is_test(p) and p.endswith((".py", ".js", ".ts", ".tsx", ".jsx"))})
    out = []
    for p, hashes in per_file.items():
        if p not in ctx.head_files:
            continue
        if max((r["weight"] or 0 for r in ctx.tests_for.get(p, [])), default=0) >= 0.3:
            ctx.drop("tests_cover_file")
            continue
        real = []
        for h in dict.fromkeys(hashes):
            rows = ctx.lines_of(h, p)
            if rows and all(not r["text"].strip() or r["text"].lstrip().startswith(("#", "//", "--")) for r in rows):
                continue
            real.append(h)
        if not real:
            ctx.drop("comment_or_whitespace_only")
            continue
        base = p.rsplit("/", 1)[-1]
        literal = [t for t, txt in test_texts.items() if t != p and base in txt
                   and re.search(r"[\"'][^\"'\n]*" + re.escape(base) + r"[\"']", txt)]
        edited = [h for h in real if any(is_test(fc["path"]) and fc["path"] != p for fc in ctx.changes(h))]
        if not literal and not edited:
            ctx.drop("no_test_reference")
            continue
        out.append((len(real), p, real, literal, edited))
    per_dir: dict[str, int] = defaultdict(int)
    for n, p, real, literal, edited in sorted(out, key=lambda x: (-x[0], x[1])):
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if per_dir[d] >= 2:
            ctx.drop("directory_cap")
            continue
        per_dir[d] += 1
        yield Hit(
            subject=p, score=n * (2 if literal else 1),
            title=f"{p} fixed {n}x with no mapped test"
                  + (f"; referenced as a literal by {literal[0]}" if literal else "; test edited in the same commit"),
            evidence={"distinct_fix_commits": n, "referenced_by_tests": literal[:4], "test_edited_in_fix": edited[:3],
                      "fix_subjects": [_subj(ctx, h) for h in real[:5]],
                      "evidence_class": "a_literal_reference" if literal else "b_test_edited"},
            commits=real[:6], diffs=[(h, p) for h in real[:4]], paths=[p] + literal[:2])


# ---------------------------------------------------------------------------------------- test_added_at_fix_then_lost
@candidate("test_added_at_fix_then_lost")
def test_added_at_fix_then_lost(ctx: Ctx):
    """Fix commits (security/regression/race zones) that added or changed a test function; at HEAD the test is deleted,
    skip-marked, has fewer assertions, or no longer mentions the fixed function, while the fixed function still exists.
    Similarity search is reduced to a same-name-alive check."""
    alive_tests = _test_funcs(ctx)
    names = defaultdict(list)
    for t, s in alive_tests.items():
        names[s["name"]].append(s)
    done: set[str] = set()
    for f in ctx.fix_events:
        zone = [k for k in _kinds(f) if k in ("security", "regression", "race")]
        target = ctx.symbols.get(f["uid"])
        if not zone or target is None or not target["alive"] or is_test(f["path"]):
            continue
        for e in ctx.events_by_commit.get(f["hash"], []):
            if e["kind"] != "function" or not is_test(e["path"]) or not e["path"].endswith(".py") \
                    or e["change"] not in ("created", "modified") or not e["qualname"].rsplit(".", 1)[-1].startswith("test"):
                continue
            key = f"{f['hash']}|{e['uid']}|{f['uid']}"
            if key in done:
                continue
            fix_src = _py_funcs(ctx.text_at(f["hash"], e["path"])).get(e["qualname"])
            name_rx = r"\b" + re.escape(target["name"]) + r"\b"
            if not fix_src or not re.search(name_rx, fix_src[0]):
                continue
            ts = ctx.symbols.get(e["uid"])
            reasons = []
            if ts is None or not ts["alive"]:
                reasons.append("test_deleted")
            else:
                body = ts["body"] or ""
                if SKIP_RX.search(body) and not CONDITIONAL_SKIP_RX.search(body):
                    reasons.append("skip_marked")
                if _assert_n(body) < _assert_n(fix_src[0]):
                    reasons.append("assertions_reduced")
                if not re.search(name_rx, body):
                    reasons.append("lost_link_to_fixed_function")
            if not reasons:
                continue
            if any(s["uid"] != e["uid"] and difflib.SequenceMatcher(None, fix_src[0], s["body"] or "").ratio() >= 0.8
                   for s in names.get(e["qualname"].rsplit(".", 1)[-1], [])):
                ctx.drop("equivalent_test_exists")
                continue
            done.add(key)
            yield Hit(
                subject=key, score=3 * len(zone) + len(reasons) + 0.2 * len(ctx.callers(target["uid"])),
                title=f"Test {e['qualname']} added at fix '{f['subject'][:60]}' is now: {', '.join(reasons)}",
                evidence={"zones": zone, "reasons": reasons, "fix_subject": f["subject"], "fixed_function": target["qualname"],
                          "test_at_fix": _short(fix_src[0], 600), "test_at_head": _short(ts["body"] if ts else None, 600),
                          "callers_of_fixed": len(ctx.callers(target["uid"]))},
                commits=[f["hash"]], diffs=[(f["hash"], e["path"]), (f["hash"], f["path"])],
                paths=[e["path"], f["path"]], symbols=[e["uid"], target["uid"]])


# ------------------------------------------------------------------------------------------ hard_to_test_risky_code
def _ancestors_have_test(ctx: Ctx, uid: str, hops: int = 3) -> bool:
    frontier, seen = {uid}, {uid}
    for _ in range(hops):
        nxt = set()
        for u in frontier:
            for c in ctx.callers(u):
                if c in seen:
                    continue
                seen.add(c)
                cs = ctx.symbols.get(c)
                if cs is not None and is_test(cs["path"]):
                    return True
                nxt.add(c)
        frontier = nxt
        if not frontier:
            break
    return False


@candidate("hard_to_test_risky_code")
def hard_to_test_risky_code(ctx: Ctx):
    """Production functions with no test within 3 caller hops, >=2 fixes (or CRAP>=30 at zero coverage) and an I/O-like
    call. Symbols without I/O are emitted as the plain 'risky untested' variant at half score.

    Complexity is the AST estimate stored per symbol version (``ctx.complexity``), falling back to the branch-keyword
    count + 1. "Untested" is the static test map (no direct link, none within 3 caller hops); when the sandbox measured
    the symbol, a body that is >=50% executed is dropped (``measured_coverage_adequate``) and the measured fraction is
    reported."""
    imported_by = ctx.imports[1]
    for s in _prod_funcs(ctx):
        uid, body = s["uid"], s["body"] or ""
        fixes = _fix_hashes(ctx, uid)
        cc, cc_src = _complexity(ctx, s)
        if len(fixes) < 2 and cc * cc + cc < 30:
            continue
        if not fixes:
            continue
        cov = _measured_cov(ctx, uid)
        if cov is not None:
            if cov >= 0.5:
                ctx.drop("measured_coverage_adequate")
                continue
        elif _links(ctx, uid) or _ancestors_have_test(ctx, uid):
            continue
        io = sorted({m.group(0).strip("(. ") for m in IO_RX.finditer(body)})
        stmts = [ln for ln in body.splitlines()[1:] if ln.strip() and not ln.strip().startswith(("#", '"""', "'''"))]
        if io and len(stmts) <= 5 and len(io) == 1:
            ctx.drop("thin_io_wrapper")
            continue
        p = s["path"]
        text = ctx.text_at_head(p) or ""
        is_script = '__name__ == "__main__"' in text or "__name__ == '__main__'" in text
        if s["name"] == "main" or (is_script and not imported_by.get(p)):
            ctx.drop("script_or_cli_main")
            continue
        if any(re.search(r"(?i)characteri[sz]ation|golden|approval", r["test"]) for r in ctx.tests_for.get(uid, [])):
            ctx.drop("characterization_test")
            continue
        variant = "io" if io else "plain_risky_untested"
        yield Hit(
            subject=uid, score=max(1, len(fixes)) * cc * (1.0 if io else 0.5),
            title=f"{s['qualname']}: untested, {len(fixes)} fixes, {cc} complexity"
                  + (f", does I/O ({', '.join(io[:3])})" if io else ""),
            evidence={"variant": variant, "io_calls": io[:6], "fixes": len(fixes), "complexity_proxy": cc,
                      "complexity_source": cc_src, "measured_coverage": None if cov is None else round(cov, 3),
                      "fix_subjects": [_subj(ctx, h) for h in fixes[:5]],
                      "callers": len(ctx.callers(uid)), "crap_no_coverage": cc * cc + cc},
            commits=fixes[:5], diffs=[(h, p) for h in fixes[:4]], paths=[p], symbols=[uid])


# ----------------------------------------------------------------------------------------------------- seam_mocked_only
PATCH_RX = re.compile(r"""\bpatch\(\s*["']([\w.]+)["']|"""
                      r"""\bpatch\.object\(\s*([\w.]+)\s*,\s*["'](\w+)["']|"""
                      r"""monkeypatch\.setattr\(\s*["']([\w.]+)["']|"""
                      r"""monkeypatch\.setattr\(\s*([\w.]+)\s*,\s*["'](\w+)["']""")
SEAM_NAME_RX = re.compile(r"""["'][\w.]*\b(time|datetime|random|requests|subprocess|socket|urllib|sleep|os|sys|shutil|"""
                          r"""uuid|logging)\b""")


def _resolve_patch(ctx: Ctx, dotted: str):
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod, qual = "/".join(parts[:i]), ".".join(parts[i:])
        for s in _prod_funcs(ctx):
            if s["qualname"] == qual and (s["path"].endswith(f"/{mod}.py") or s["path"] == f"{mod}.py"
                                          or s["path"].endswith(f"/{mod}/__init__.py")):
                return s
    return None


@candidate("seam_mocked_only")
def seam_mocked_only(ctx: Ctx):
    """First-party functions patched in tests where no linked test (static link on the function or on a caller) runs
    it unpatched; excludes I/O/clock/subprocess/network seams. Patch targets are resolved from dotted strings to
    symbols by module suffix, so heavily re-exported targets can be missed. "Never run for real" is judged from the
    static test links; when the sandbox measured the symbol's file and any body line was executed, the symbol is dropped
    (``measured_executed``), and a measured 0-line body is reported as ``measured_coverage`` 0."""
    patchers: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))  # uid -> tid -> patch lines
    for path in [p for p in ctx.head_files if is_test(p) and p.endswith(".py")]:
        text = ctx.text_at_head(path)
        if not text or ("patch" not in text and "monkeypatch" not in text):
            continue
        funcs = _py_funcs(text)
        for i, line in enumerate(text.splitlines(), 1):
            for m in PATCH_RX.finditer(line):
                g = m.groups()
                dotted = g[0] or g[3] or (f"{g[1]}.{g[2]}" if g[1] else None) or (f"{g[4]}.{g[5]}" if g[4] else None)
                if not dotted:
                    continue
                target = _resolve_patch(ctx, dotted)
                if target is None:
                    continue
                owner = next((q for q, (_s, a, b) in funcs.items() if a <= i <= b), None)
                tid = f"{path}::{owner.replace('.', '::')}" if owner else path
                patchers[target["uid"]][tid].append(line.strip()[:160])
    for uid, by_test in patchers.items():
        s = ctx.symbols[uid]
        body = s["body"] or ""
        lines = [l for ls in by_test.values() for l in ls]
        if IO_RX.search(body) or any(SEAM_NAME_RX.search(l) for l in lines):
            ctx.drop("io_clock_process_network_seam")
            continue
        if re.search(r"(?i)(^|/)(clients?|adapters?)(/|\.)", s["path"]):
            ctx.drop("client_adapter_package")
            continue
        mcov = _measured_cov(ctx, uid)
        if mcov is not None and mcov > 0:
            ctx.drop("measured_executed")
            continue
        patching = set(by_test)
        patching_files = {_test_file(t) for t in by_test}
        linked = set(_links(ctx, uid))
        for c in ctx.callers(uid):
            cs = ctx.symbols.get(c)
            if cs is not None and not is_test(cs["path"]):
                linked |= set(_links(ctx, c))
        real = [t for t in linked if t not in patching and not (t in patching_files and "::" not in t)]
        if real:
            ctx.drop("unpatched_test_exists")
            continue
        fixes = _fix_hashes(ctx, uid)
        fan_in = len(ctx.callers(uid))
        if not fixes and fan_in < 2:
            ctx.drop("no_fix_and_low_fan_in")
            continue
        yield Hit(
            subject=uid, score=2 * len(fixes) + fan_in + len(by_test),
            title=f"{s['qualname']} is mocked in {len(by_test)} test(s) and never run for real",
            evidence={"patched_in": sorted(by_test)[:6], "patch_lines": {t: v[:2] for t, v in list(by_test.items())[:4]},
                      "fixes": len(fixes), "fan_in": fan_in, "linked_tests_all_patching": sorted(linked)[:6],
                      "measured_coverage": mcov},
            commits=fixes[:5], diffs=[(h, s["path"]) for h in fixes[:3]],
            paths=[s["path"]] + sorted(patching_files)[:3], symbols=[uid])


# -------------------------------------------------------------------------------------------- process_seam_mocked
PROC_RX = re.compile(r"\bsubprocess\.(run|Popen|check_output|check_call|call)\(|\bos\.system\(|"
                     r"\basyncio\.create_subprocess_\w+\(|\bchild_process\b|\b(spawn|execFile|execSync|spawnSync)\(")
PROC_MSG_RX = re.compile(r"(?i)stdin|encoding|timeout|hang|quote|shell|windows|utf")
PROC_PATCH_RX = re.compile(r"patch\([^)]*(subprocess|Popen|check_output|os\.system)|setattr\([^)]*(subprocess|Popen|"
                           r"check_output|os\.system)|jest\.mock\(['\"]child_process")


@candidate("process_seam_mocked")
def process_seam_mocked(ctx: Ctx):
    """Functions that spawn a subprocess where every mapped test patches the spawn, and the function/file has a fix or
    a stdin/encoding/timeout/hang/quote/shell/windows/utf message. Rank by fix count then public reachability."""
    tid_syms = _test_funcs(ctx, alive_only=False)
    for s in ctx.alive_symbols:
        if s["kind"] != "function" or is_test(s["path"]) or not is_code(s["path"]):
            continue
        body = s["body"] or ""
        m = PROC_RX.search(body)
        if not m:
            continue
        p = s["path"]
        if re.search(r"(?i)(^|/)(build|scripts?|tools?|tasks?)/|setup\.py$|noxfile|fabfile|conftest", p):
            ctx.drop("one_off_script_or_build_tool")
            continue
        tids = _links(ctx, s["uid"])
        if not tids:
            ctx.drop("no_mapped_tests")
            continue
        patched_all = True
        for t in tids:
            ts = tid_syms.get(t)
            txt = (ts["body"] if ts else None) or ctx.text_at_head(_test_file(t)) or ""
            if not PROC_PATCH_RX.search(txt):
                patched_all = False
                break
        if not patched_all:
            ctx.drop("unmocked_test_exists")
            continue
        if re.search(r"""\[\s*["']git["']|["']git\s""", body):
            ctx.drop("git_is_available_in_ci")
            continue
        fixes = ctx.fixes_by_uid.get(s["uid"], [])
        path_fixes = [f for f in ctx.fix_events if f["path"] == p]
        msgs = [c for c in ctx.path_commits(p) if PROC_MSG_RX.search(c["message"] or "")]
        if not (fixes or path_fixes or msgs):
            continue
        public = not s["name"].startswith("_") or bool(ctx.callers(s["uid"]) - set(tids))
        yield Hit(
            subject=s["uid"], score=3 * len(fixes) + len(path_fixes) + len(msgs) + (2 if public else 0),
            title=f"{s['qualname']} spawns a process ({m.group(0)[:30]}) but every test mocks the spawn",
            evidence={"spawn": m.group(0), "mapped_tests": tids[:5], "function_fixes": len(fixes),
                      "file_fixes": len(path_fixes), "matching_messages": [subject_of(c["message"]) for c in msgs[:4]],
                      "public_entry": public},
            commits=list(dict.fromkeys([f["hash"] for f in fixes] + [c["hash"] for c in msgs]))[:6],
            diffs=[(f["hash"], p) for f in (fixes or path_fixes)[:3]], paths=[p] + [_test_file(t) for t in tids[:2]],
            symbols=[s["uid"]])


# ------------------------------------------------------------------------------------------------ ci_test_disabled
CI_CONFIG_RX = ("(?i)(?:" + CI_RX.pattern.removeprefix("(?i)") + r")|(?:(^|/)(pytest\.ini|pyproject\.toml|setup\.cfg|"
                r"jest\.config\.\w+|vitest\.config\.\w+|Makefile|package\.json|\.coveragerc|tox\.ini)$)")
DISABLE_RX = (r"continue-on-error:\s*true|allow[_-]failure|\bif:\s*(false|\$\{\{\s*false\s*\}\})|--ignore(-glob)?\b|"
              r"--deselect\b|-k\s+[\"']?not\b|testPathIgnorePatterns|paths-ignore|--maxfail|^\s*-?\s*exclude:|"
              r"^\s*#\s*-?\s*(run:\s*)?(python -m )?(pytest|tox|nox|npm (run )?test|yarn test|jest|vitest|go test|"
              r"cargo test|make test)\b")
TESTCMD_RX = re.compile(r"^\s*-?\s*(run:\s*)?[\"']?(\w+\s+)?(python -m pytest|pytest|tox|nox|npm (run )?test|npm t|yarn test|"
                        r"jest|vitest|go test|cargo test|make test)\b|codecov|coveralls")
DOCS_PATH_RX = re.compile(r"(?i)docs?\b|\.md\b|\.rst\b|readme|license|changelog|\.txt\b|\*\*/\*\.md")


@candidate("ci_test_disabled")
def ci_test_disabled(ctx: Ctx):
    """CI/test-config diffs that add a test-disabling construct (continue-on-error, allow_failure, --ignore, --deselect,
    -k "not", testPathIgnorePatterns, paths-ignore, matrix exclude, maxfail, commented-out test command) or remove a
    test/coverage command. Bonus when the line is still at HEAD and fixes touched the excluded tests' targets later."""
    added = ctx.grep(DISABLE_RX, "+", path_regex=CI_CONFIG_RX, limit=5000)
    removed = ctx.grep(TESTCMD_RX.pattern, "-", path_regex=CI_CONFIG_RX, limit=5000)
    added_cmds: dict[tuple[str, str], int] = defaultdict(int)
    for r in ctx.grep(TESTCMD_RX.pattern, "+", path_regex=CI_CONFIG_RX, limit=20000):
        added_cmds[(r["hash"], r["path"])] += 1
    tbt_rev: dict[str, set[str]] = defaultdict(set)
    for t, targets in _targets_by_test(ctx).items():
        tbt_rev[_test_file(t)] |= targets
    seen: set[str] = set()
    for kind, r in [("added", x) for x in added] + [("removed", x) for x in removed]:
        c = ctx.commit(r["hash"])
        if c is None:
            continue
        text = r["text"].strip()
        if kind == "removed":
            if added_cmds.get((r["hash"], r["path"])):
                ctx.drop("test_command_replaced")
                continue
        elif "paths-ignore" in text:
            by_line = {x["line"]: x["text"] for x in ctx.lines_of(r["hash"], r["path"]) if x["sign"] == "+"}
            nearby, ln = [], r["line"] + 1
            while ln in by_line and by_line[ln].lstrip().startswith("-"):   # the list items under paths-ignore
                nearby.append(by_line[ln])
                ln += 1
            if nearby and all(DOCS_PATH_RX.search(t) for t in nearby):
                ctx.drop("docs_only_path_filter")
                continue
        key = f"{r['hash']}|{r['path']}|{text[:70]}"
        if key in seen:
            continue
        seen.add(key)
        head_present = kind == "added" and text in {ln.strip() for ln in _head_lines(ctx, r["path"])}
        tokens = re.findall(r"(?:--ignore(?:-glob)?|--deselect)[= ]+([\w./*\-]+)", text)
        if "IgnorePatterns" in text:
            tokens += re.findall(r"""["'](?:<rootDir>/)?([\w./\-]+)["']""", text)
        excluded = sorted(tf for tf in tbt_rev for tok in tokens if tok.strip("*/") and tf.startswith(tok.strip("*/")))
        areas = {u for tf in excluded for u in tbt_rev.get(tf, ())}
        area_paths = {ctx.symbols[u]["path"] for u in areas if u in ctx.symbols}
        fixes_after = [f for f in ctx.fix_events if f["ts"] > r["ts"] and (f["uid"] in areas or f["path"] in area_paths)]
        score = 1 + (3 if head_present else 0) + 0.5 * min(10, len(fixes_after)) + (1 if kind == "removed" else 0)
        yield Hit(
            subject=key, score=score,
            title=f"{r['path']}: {kind} `{text[:70]}`" + (" (still present at HEAD)" if head_present else ""),
            evidence={"direction": kind, "line": text, "still_at_head": head_present, "excluded_test_files": excluded[:6],
                      "later_fixes_in_excluded_area": [f["subject"] for f in fixes_after[:5]],
                      "commit_subject": subject_of(c["message"])},
            commits=[r["hash"]] + [f["hash"] for f in fixes_after[:3]], diffs=[(r["hash"], r["path"])],
            paths=[r["path"]] + excluded[:3])


# ------------------------------------------------------------------------------------------------ weakened_assertion
STRICT_RX = re.compile(r"assertEquals?\b|assert_equal\b|assertListEqual|assertDictEqual|toEqual\b|toStrictEqual|toBe\(|==")
LOOSE_RX = re.compile(r"assertTrue\b|assertIn\b|assertIsNotNone|assertGreater|toBeTruthy|toBeDefined|toContain|>\s*0|"
                      r">=\s*0|is not None|!=\s*None|\bin\b")
TOL_RX = re.compile(r"(?:rel|abs|delta|tol\w*)\s*=\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)|places\s*=\s*(\d+)")
TOL_LINE_RX = r"approx|rel\s*=|abs\s*=|delta\s*=|places\s*=|toBeCloseTo"


def _weaker(r: str, a: str) -> str | None:
    if STRICT_RX.search(r) and LOOSE_RX.search(a) and not STRICT_RX.search(a):
        return "strict_to_loose"
    if re.search(r"raises\(\s*(?!Exception\b)[A-Z]\w+", r) and re.search(r"raises\(\s*Exception\b", a):
        return "specific_exception_to_Exception"
    mr, ma = TOL_RX.search(r), TOL_RX.search(a)
    if mr and ma:
        try:
            if mr.group(1) and ma.group(1) and float(ma.group(1)) > float(mr.group(1)):
                return "tolerance_loosened"
            if mr.group(2) and ma.group(2) and int(ma.group(2)) < int(mr.group(2)):
                return "tolerance_loosened"
        except ValueError:
            pass
    return None


@candidate("weakened_assertion")
def weakened_assertion(ctx: Ctx):
    """Test-file commits with net removed assertion lines, a strict-to-loose swap, or a looser tolerance literal, in a
    commit that also changes non-test code or whose message matches fix|flaky|ci|broken. Line-regex based."""
    pat = ASSERT_RX.pattern + "|" + TOL_LINE_RX
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in ctx.grep(pat, None, path_regex=TEST_RX.pattern, limit=80000):
        groups[(r["hash"], r["path"])].append(r)
    msg_rx = re.compile(r"(?i)\b(fix(es|ed)?|flaky|flake|ci|broken|unbreak)\b")
    mig_rx = re.compile(r"(?i)migrat|convert|port(ed)? to|switch(ed)? to|rewrite|refactor|rename|reformat|black|ruff|lint|prettier")
    for (h, path), rows in groups.items():
        c = ctx.commit(h)
        if c is None or c["is_bulk"] or c["is_merge"]:
            continue
        fc = next((x for x in ctx.changes(h) if x["path"] == path), None)
        if fc is not None and str(fc["status"]).startswith("R"):
            ctx.drop("rename")
            continue
        rem = [r for r in rows if r["sign"] == "-"]
        add = [r for r in rows if r["sign"] == "+"]
        rem_a = [r for r in rem if ASSERT_RX.search(r["text"])]
        add_a = [r for r in add if ASSERT_RX.search(r["text"])]
        why = []
        if len(rem_a) > len(add_a):
            why.append(f"net_assertions_removed:{len(rem_a) - len(add_a)}")
        if len(rem) <= 40 and len(add) <= 40:
            for r in rem:
                best = max(add, key=lambda a: difflib.SequenceMatcher(None, r["text"], a["text"]).quick_ratio(), default=None)
                if best is not None and difflib.SequenceMatcher(None, r["text"], best["text"]).ratio() >= 0.4:
                    w = _weaker(r["text"], best["text"])
                    if w:
                        why.append(w)
        if not why:
            if rem_a and len(add_a) >= len(rem_a):
                ctx.drop("adds_as_many_assertions")
            continue
        prod = any(not is_test(x["path"]) and is_code(x["path"]) for x in ctx.changes(h))
        fixy = bool(msg_rx.search(c["message"] or ""))
        if not (prod or fixy):
            ctx.drop("no_prod_change_or_fix_message")
            continue
        if mig_rx.search(subject_of(c["message"])) and not fixy:
            ctx.drop("framework_migration_or_format")
            continue
        yield Hit(
            subject=f"{h}|{path}", score=len(why) + (2 if fixy else 0) + (1 if prod else 0),
            title=f"{path}: assertions weakened ({', '.join(dict.fromkeys(why))[:80]}) in '{subject_of(c['message'])[:60]}'",
            evidence={"signals": list(dict.fromkeys(why)), "removed": [r["text"].strip()[:120] for r in rem[:5]],
                      "added": [r["text"].strip()[:120] for r in add[:5]], "production_change": prod,
                      "message_matches_fix_ci": fixy},
            commits=[h], diffs=[(h, path)], paths=[path])


# ---------------------------------------------------------------------------------- fix_recurrence_despite_existing_test
@candidate("fix_recurrence_despite_existing_test")
def fix_recurrence_despite_existing_test(ctx: Ctx):
    """A repeat fix (a prior fix exists) on a function that has a static/coverage-linked test whose file is >14 days older
    than the fix and not edited within +-7 days of it. Links are HEAD links. Mutation survivors come from
    ``ctx.mutations(uid)`` when the sandbox ran mutation testing on the symbol (evidence ``mutation_survivors`` = count,
    ``mutants_tested`` = total decided; each survivor raises the score); with no mutation rows the evidence stays
    'unconfirmed' and the score is unchanged."""
    dep_msg = re.compile(r"(?i)\b(bump|upgrade|dependabot|dependenc\w*|requirements?)\b")
    for uid, fixes in ctx.fixes_by_uid.items():
        s = ctx.symbols.get(uid)
        if s is None or not s["alive"] or is_test(s["path"]) or len(fixes) < 2:
            continue
        links = _links(ctx, uid)
        if not links:
            if ctx.tests_for.get(uid):
                ctx.drop("name_match_only_link")
            continue
        fixes = sorted(fixes, key=lambda f: f["ts"])
        muts = [m for m in ctx.mutations(uid) if m["status"] in ("killed", "survived")]
        tfiles = {_test_file(t) for t in links}
        for i, f in enumerate(fixes):
            if i == 0:
                continue
            old = [t for t in tfiles if ctx.changes_by_path.get(t)
                   and ctx.by_hash[ctx.changes_by_path[t][0]["hash"]]["ts"] < f["ts"] - 14 * DAY]
            if not old:
                continue
            near = [t for t in tfiles if any(abs(c["ts"] - f["ts"]) <= 7 * DAY for c in ctx.path_commits(t))]
            if near or any(is_test(x["path"]) for x in ctx.changes(f["hash"])):
                ctx.drop("test_touched_near_fix")
                continue
            ch = ctx.changes(f["hash"])
            c = ctx.commit(f["hash"])
            if sum(x["added"] or 0 for x in ch if is_code(x["path"]) and not is_test(x["path"])) > 40:
                ctx.drop("feature_like_fix")
                continue
            if dep_msg.search(subject_of(c["message"])) or (ch and all(DEP_MANIFEST_RX.search(x["path"])
                                                                       or LOCKFILE_RX.search(x["path"]) for x in ch)):
                ctx.drop("dependency_bump")
                continue
            prior = fixes[:i]
            surv = sum(1 for m in muts if m["status"] == "survived")
            yield Hit(
                subject=f"{f['hash']}|{uid}", score=2 * i + len(links) * 0.5 + len(ctx.callers(uid)) * 0.1
                + min(surv, 5) * 1.5,
                title=f"{s['qualname']} fixed again ('{f['subject'][:60]}') despite linked test(s)",
                evidence={"prior_fixes": [p["subject"] for p in prior[-3:]], "linked_tests": links[:5],
                          "test_files_older_than_fix": sorted(old)[:3], "mutation_survivors": surv if muts else "unconfirmed",
                          "mutants_tested": len(muts), "survived_mutants": [
                              {"line": m["line"], "operator": m["operator"], "mutant": m["mutant"]}
                              for m in muts if m["status"] == "survived"][:4],
                          "fix_number": i + 1},
                commits=[f["hash"]] + [p["hash"] for p in prior[-2:]], diffs=[(f["hash"], f["path"])],
                paths=[f["path"]] + sorted(old)[:2], symbols=[uid])


# ----------------------------------------------------------------------------- time_dependent_code_no_frozen_clock
TIME_RX = re.compile(r"datetime\.(now|utcnow|today)\(|\.utcnow\(|\btime\.time\(|fromtimestamp|strptime|strftime|timedelta|"
                     r"zoneinfo|pytz|Date\.now\(|new Date\b|date\.today\(")
TIME_MSG_RX = re.compile(r"(?i)\b(date|time|tz|timezone|dst|utc|epoch)\b")
FROZEN_RX = re.compile(r"freezegun|freeze_time|time_machine|useFakeTimers|\bclock\s*[=:]|\w+_clock\b|monkeypatch\.setattr\([^)]*(time|datetime)|"
                       r"patch\([^)]*(time|datetime)|mock_time|fake_time")


@candidate("time_dependent_code_no_frozen_clock")
def time_dependent_code_no_frozen_clock(ctx: Ctx):
    """Functions reading the clock or doing date arithmetic that had a date/time/tz fix, with no mapped test (or its
    conftest) freezing time or taking a clock. Only wall-clock lines count: logging-only and start/elapsed timing are
    excluded."""
    for s in ctx.alive_symbols:
        if s["kind"] != "function" or is_test(s["path"]) or not is_code(s["path"]):
            continue
        body = s["body"] or ""
        lines = [ln for ln in body.splitlines() if TIME_RX.search(ln)]
        if not lines:
            continue
        real = [ln for ln in lines if not re.search(r"\b(log|logger|logging)\w*\.|print\(", ln)]
        if not real:
            ctx.drop("logging_only")
            continue
        if all(re.search(r"(?i)\b(start|elapsed|t0|began|duration|took|perf)\w*", ln) for ln in real):
            ctx.drop("timing_only")
            continue
        if re.search(r"(?i)\b(clock|now)\s*[:=,)]", s["signature"] or ""):
            ctx.drop("injectable_clock")
            continue
        fixes = [f for f in ctx.fixes_by_uid.get(s["uid"], [])
                 if TIME_MSG_RX.search(f["subject"] or "")
                 or any(TIME_MSG_RX.search(r["text"]) for r in ctx.lines_of(f["hash"], f["path"]))]
        if not fixes:
            continue
        tids = _links(ctx, s["uid"]) or sorted({r["test"] for r in ctx.tests_for.get(s["path"], [])})
        unit = [t for t in tids if not re.search(r"(?i)e2e|integration|benchmark", t)]
        if tids and not unit:
            ctx.drop("only_e2e_integration_benchmark_tests")
            continue
        frozen = False
        for t in unit:
            tf = _test_file(t)
            d = tf.rsplit("/", 1)[0] if "/" in tf else ""
            for p in (tf, f"{d}/conftest.py" if d else "conftest.py", "conftest.py"):
                if FROZEN_RX.search(ctx.text_at_head(p) or ""):
                    frozen = True
            if frozen:
                break
        if frozen:
            ctx.drop("time_frozen_in_tests")
            continue
        yield Hit(
            subject=s["uid"], score=len(fixes) * 3 + len(unit) * 0.2 + len(ctx.callers(s["uid"])) * 0.1 + 1,
            title=f"{s['qualname']} uses the wall clock, fixed {len(fixes)}x for time bugs, tests never freeze time",
            evidence={"clock_lines": [ln.strip()[:100] for ln in real[:4]],
                      "time_fix_subjects": [f["subject"] for f in fixes[:5]],
                      "mapped_tests": unit[:5], "mapped_test_count": len(unit)},
            commits=[f["hash"] for f in fixes[:5]], diffs=[(f["hash"], f["path"]) for f in fixes[:3]],
            paths=[s["path"]] + [_test_file(t) for t in unit[:2]], symbols=[s["uid"]])


# ---------------------------------------------------------------------------------- boundary_fix_no_boundary_test
REL_OPS = {"<", "<=", ">", ">=", "==", "!="}
TOK_RX = re.compile(r"\w+|<=|>=|==|!=|<|>|\S")
CONST_NOISE_RX = re.compile(r"(?i)timeout|retr(y|ies)|version|\blog|logger|delay|sleep|interval|\bport\b|width|height|"
                            r"margin|padding|font|color|max_?age|ttl")
NOOP_PAIRS_DOWN = {(">=", ">"), ("<", "<=")}   # new int = old int - 1 is a no-op for these op swaps
NOOP_PAIRS_UP = {(">", ">="), ("<=", "<")}     # new int = old int + 1


def _boundary_kind(r: str, a: str) -> str | None:
    tr, ta = TOK_RX.findall(r), TOK_RX.findall(a)
    if len(tr) != len(ta):
        return None
    diffs = [(x, y) for x, y in zip(tr, ta) if x != y]
    if not diffs:
        return None
    ops = [(x, y) for x, y in diffs if x in REL_OPS and y in REL_OPS]
    ints = [(x, y) for x, y in diffs if x.isdigit() and y.isdigit() and abs(int(x) - int(y)) == 1]
    if len(ops) + len(ints) != len(diffs):
        return None
    if ops and ints and len(ops) == 1 and len(ints) == 1:
        (ro, ao), (ri, ai) = ops[0], ints[0]
        ri, ai = int(ri), int(ai)
        if ((ro, ao) in NOOP_PAIRS_DOWN and ai == ri - 1) or ((ro, ao) in NOOP_PAIRS_UP and ai == ri + 1):
            return "semantic_noop"
        return "op+int"
    return "op" if ops and not ints else ("int" if ints and not ops else "op+int")


@candidate("boundary_fix_no_boundary_test")
def boundary_fix_no_boundary_test(ctx: Ctx):
    """Fix hunks in non-test code where the old and new line differ only in a relational operator or an integer by +-1
    (token-wise compare of paired -/+ lines). Gate: the fix commit did not add a test mentioning the symbol, or the
    symbol has <=2 linked tests; zero-test symbols are left to fix_without_regression_test."""
    by_cp: dict[tuple[str, str], list] = defaultdict(list)
    for f in ctx.fix_events:
        if not is_test(f["path"]) and is_code(f["path"]):
            by_cp[(f["hash"], f["path"])].append(f)
    for (h, path), fes in by_cp.items():
        rows = ctx.lines_of(h, path)
        rem = [r for r in rows if r["sign"] == "-"]
        add = [r for r in rows if r["sign"] == "+"]
        if not rem or len(rem) > 60 or len(add) > 60:
            continue
        found = []
        for r in rem:
            best = max(add, key=lambda a: difflib.SequenceMatcher(None, r["text"], a["text"]).quick_ratio(), default=None)
            if best is None or difflib.SequenceMatcher(None, r["text"], best["text"]).ratio() < 0.6:
                continue
            kind = _boundary_kind(r["text"], best["text"])
            if kind is None:
                continue
            if kind == "semantic_noop":
                ctx.drop("semantic_noop")
                continue
            if re.search(r"\d\.\d", r["text"] + best["text"]):
                ctx.drop("float_constant")
                continue
            if CONST_NOISE_RX.search(r["text"]) or CONST_NOISE_RX.search(best["text"]):
                ctx.drop("log_timeout_retry_version_constant")
                continue
            if kind != "op" and "len(" in r["text"]:
                ctx.drop("len_derived_bound")
                continue
            found.append((kind, r["text"].strip(), best["text"].strip()))
        if not found:
            continue
        uids = [f["uid"] for f in fes if f["uid"] in ctx.symbols]
        if not uids:
            continue
        linked = sorted({t for u in uids for t in _links(ctx, u)})
        if not linked:
            ctx.drop("zero_tests")
            continue
        name_rx = r"\b(" + "|".join(re.escape(ctx.symbols[u]["name"]) for u in uids) + r")\b"
        test_edit = any(is_test(x["path"]) and any(re.search(name_rx, ln["text"]) for ln in ctx.lines_of(h, x["path"]))
                        for x in ctx.changes(h))
        if test_edit and len(linked) > 2:
            ctx.drop("fix_added_test_for_symbol")
            continue
        if any(re.search(r"hypothesis|@given", ctx.symbols[_uid_for_tid(ctx, t)]["body"] or "")
               for t in linked if _uid_for_tid(ctx, t) in ctx.symbols):
            ctx.drop("property_based_test")
            continue
        c = ctx.commit(h)
        yield Hit(
            subject=f"{h}|{path}", score=len(found) + 2 / max(1, len(linked)) + (0 if test_edit else 1),
            title=f"'{subject_of(c['message'])[:70]}' moved a boundary ({found[0][0]}) in {ctx.symbols[uids[0]]['qualname']}",
            evidence={"changes": [{"kind": k, "old": o[:120], "new": n[:120]} for k, o, n in found[:4]],
                      "linked_tests": linked[:5], "test_edited_in_fix": test_edit, "mutation_survivors": "unconfirmed"},
            commits=[h], diffs=[(h, path)], paths=[path] + [_test_file(t) for t in linked[:2]], symbols=uids[:4])


# -------------------------------------------------------------------------- concurrency_code_without_concurrent_test
CONC_TEST_RX = re.compile(r"\bThread\b|threading\.|\bBarrier\b|\bgather\(|create_task|ThreadPoolExecutor|multiprocessing|"
                          r"Promise\.all|asyncio\.(wait|TaskGroup)|concurrent\.futures|\bProcessPool|\bProcess\(|anyio|trio\.")
LOCK_BODY_RX = re.compile(r"\bwith\s+[\w.]*(lock|mutex|semaphore)\w*|\.acquire\(|threading\.(R?Lock|Event|Condition|"
                          r"Semaphore)|asyncio\.(Lock|Event|Semaphore|Queue)|\bqueue\.Queue|\bMutex\b|\bsync\.(Mutex|RWMutex)")
CONC_FINDING_RX = re.compile(r"(?i)race|thread|lock|concurren|deadlock|atomic|async")


@candidate("concurrency_code_without_concurrent_test")
def concurrency_code_without_concurrent_test(ctx: Ctx):
    """Symbols with a race-zone fix, a concurrency finding, or lock/event/queue-guarded shared state whose mapped tests
    (any mapping source, whole test files) use no thread/gather/task/pool construct. Ranked by race fixes x callers."""
    cand: dict[str, set[str]] = defaultdict(set)
    for f in ctx.fix_events:
        if "race" in _kinds(f):
            cand[f["uid"]].add("race_fix")
    for fd in ctx.findings:
        if fd["symbol"] and CONC_FINDING_RX.search(f"{fd['category']} {fd['rule']} {fd['message']}"):
            cand[fd["symbol"]].add("finding")
    for s in ctx.alive_symbols:
        if s["kind"] == "function" and not is_test(s["path"]) and LOCK_BODY_RX.search(s["body"] or ""):
            cand[s["uid"]].add("lock_state")
    for uid, srcs in cand.items():
        s = ctx.symbols.get(uid)
        if s is None or not s["alive"] or is_test(s["path"]) or not is_code(s["path"]):
            continue
        body = s["body"] or ""
        if "race_fix" not in srcs:
            if "async" in (s["signature"] or "") and not LOCK_BODY_RX.search(body) \
                    and not re.search(r"create_task|gather\(", body):
                ctx.drop("sequential_await_asyncio")
                continue
            if re.search(r"(?i)(^|/)(cli|commands?|scripts?|__main__)", s["path"]) and "lock_state" in srcs:
                ctx.drop("single_thread_cli_lock")
                continue
            if re.search(r"Thread\([^)]*(stdout|stderr|drain|pipe|_read)", body):
                ctx.drop("subprocess_drain_thread")
                continue
        tids = sorted({r["test"] for r in ctx.tests_for.get(uid, []) + ctx.tests_for.get(s["path"], [])})
        tfs = sorted({_test_file(t) for t in tids})
        if any(CONC_TEST_RX.search(ctx.text_at_head(t) or "") for t in tfs):
            continue
        race = [f for f in ctx.fixes_by_uid.get(uid, []) if "race" in _kinds(f)]
        callers = len(ctx.callers(uid))
        yield Hit(
            subject=uid, score=(len(race) * 3 + len(srcs)) * (1 + callers),
            title=f"{s['qualname']} is concurrency-sensitive ({', '.join(sorted(srcs))}) but no mapped test is concurrent",
            evidence={"sources": sorted(srcs), "race_fixes": [f["subject"] for f in race[:5]], "callers": callers,
                      "mapped_test_files": tfs[:5],
                      "findings": [fd["message"][:100] for fd in ctx.findings if fd["symbol"] == uid][:3]},
            commits=[f["hash"] for f in race[:5]], diffs=[(f["hash"], f["path"]) for f in race[:3]],
            paths=[s["path"]] + tfs[:2], symbols=[uid])
