"""Catalogue family: test quality (gates, seams, mocks, snapshots, erosion, benign gaps).

"Coverage" signals are proxies unless the Docker sandbox has measured the repo (``ctx.coverage_available``): ``test_map``
links (static call edges are symbol level; name/import/history links are file level), the text of the linked tests, and
``diff_lines`` history. Builders that can use measured ``covered()`` / ``symbol_coverage()`` say so in their docstring
and keep the proxy as the fallback. Signature history comes from ``symbol_versions``; changed lines carry their
containing symbol (``diff_lines.symbol``).
"""
from __future__ import annotations

import ast
import fnmatch
import json
import math
import re
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .base import (DAY, TEST_RX, Ctx, Hit, candidate, is_ci, is_code, is_docs, is_generated, is_test, is_vendored,
                   subject_of)

# thresholds (module level so tests can shrink the windows)
_MIN_WINDOW = 10          # commits per 4-quarter window for test_erosion_changepoint
_MIN_DROP = 0.3           # share drop for test_erosion_changepoint
_MAX_HITS = 300


# ---------------------------------------------------------------------------- shared helpers
def _cache(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_tqb_cache", {})


def _top(hits: list[Hit], n: int = _MAX_HITS) -> list[Hit]:
    return sorted(hits, key=lambda h: -h.score)[:n]


def _pkg(path: str) -> str:
    parts = path.split("/")
    if len(parts) == 1:
        return "."
    if parts[0] in ("src", "lib", "packages", "python", "pkg", "libs", "apps") and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _tests_of(ctx: Ctx, s) -> list[str]:
    """Linked test ids of a symbol: symbol-level rows plus file-level rows of its file."""
    ids = {r["test"] for r in ctx.tests_for.get(s["uid"], [])}
    ids |= {r["test"] for r in ctx.tests_for.get(s["path"], [])}
    return sorted(ids)


def _test_text(ctx: Ctx, tid: str) -> str:
    c = _cache(ctx).setdefault("test_text", {})
    if tid not in c:
        path, _, rest = tid.partition("::")
        text = None
        if rest:
            s = ctx.symbols.get(f"sym:{ctx.repo_id}:{path}:{rest.replace('::', '.')}:function")
            if s is not None and s["body"]:
                text = s["body"]
        c[tid] = text if text is not None else (ctx.text_at_head(path) or "")
    return c[tid]


def _test_file_text(ctx: Ctx, path: str) -> str:
    return ctx.text_at_head(path) or ""


def _linked_text(ctx: Ctx, s) -> str:
    return "\n".join(_test_text(ctx, t) for t in _tests_of(ctx, s))


def _test_blob(ctx: Ctx) -> str:
    """All test source files at HEAD concatenated (cached)."""
    c = _cache(ctx)
    if "test_blob" not in c:
        paths = [p for p in ctx.head_files if is_test(p) and p.endswith(
            (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rb", ".java", ".json", ".yaml", ".yml", ".txt"))]
        c["test_blob"] = "\n".join(ctx.text_at_head(p) or "" for p in paths[:3000])
    return c["test_blob"]


def _prod(s) -> bool:
    return not is_test(s["path"]) and is_code(s["path"])


def _measured_cov(ctx: Ctx, uid: str) -> float | None:
    """Fraction of a symbol's body lines executed in the sandbox run; None when not measured (use the static proxy)."""
    if not ctx.coverage_available:
        return None
    sc = ctx.symbol_coverage(uid)
    return None if sc is None or sc[1] <= 0 else sc[0] / sc[1]


def _guard_hits(ctx: Ctx, s, guard_lines: list[str]) -> list[int] | None:
    """Measured execution counts of the branch each guard line opens, located by exact text inside the symbol's HEAD
    range (the line itself for one-line guards such as ``raise``, the next statement for ``if ...:`` / ``except``).
    None when coverage was not measured for the file or no guard line could be located (use the static proxy)."""
    if not ctx.coverage_available or s["path"] not in ctx.measured_files:
        return None
    lines = _file_lines(ctx, s["path"])
    lo, hi = (s["start_line"] or 1), (s["end_line"] or len(lines))
    out: list[int] = []
    for g in guard_lines:
        want = g.strip()
        if not want:
            continue
        for ln in range(lo, min(hi, len(lines)) + 1):
            if lines[ln - 1].strip() == want:
                tgt = ln
                if want.endswith((":", "{")):
                    nxt = next((k for k in range(ln + 1, min(hi, len(lines)) + 1) if lines[k - 1].strip()), None)
                    if nxt is None:
                        break
                    tgt = nxt
                n = ctx.covered(s["path"], tgt)
                if n is not None:
                    out.append(n)
                break
    return out or None


def _per_test_cov(ctx: Ctx, path: str) -> bool:
    """True when per-test line coverage (``covered_line_tests``) exists and ``path`` was measured."""
    if not ctx.coverage_available or path not in ctx.measured_files:
        return False
    c = _cache(ctx)
    if "per_test" not in c:
        c["per_test"] = bool(ctx._safe_q("SELECT 1 FROM covered_line_tests LIMIT 1"))
    return c["per_test"]


def _locate_lines(ctx: Ctx, s, texts: list[str]) -> list[int]:
    """HEAD line numbers (inside the symbol's range) of the lines whose stripped text equals each of ``texts``
    (texts truncated to 120 chars match by prefix)."""
    lines = _file_lines(ctx, s["path"])
    lo, hi = (s["start_line"] or 1), (s["end_line"] or len(lines))
    out: list[int] = []
    for t in texts:
        want = t.strip()
        if not want:
            continue
        for ln in range(lo, min(hi, len(lines)) + 1):
            got = lines[ln - 1].strip()
            if got == want or (len(want) >= 120 and got.startswith(want)):
                out.append(ln)
                break
    return out


def _covering_tests(ctx: Ctx, path: str, lns: list[int]) -> set[str]:
    out: set[str] = set()
    for ln in lns:
        out |= ctx.tests_covering(path, ln) or set()
    return out


def _fix_counts(ctx: Ctx) -> Counter:
    c = _cache(ctx)
    if "fix_counts" not in c:
        c["fix_counts"] = Counter(f["uid"] for f in ctx.fix_events)
    return c["fix_counts"]


def _sym_by_path_qual(ctx: Ctx) -> dict:
    c = _cache(ctx)
    if "pq" not in c:
        c["pq"] = {(s["path"], s["qualname"]): s for s in ctx.alive_symbols}
    return c["pq"]


def _file_lines(ctx: Ctx, path: str) -> list[str]:
    c = _cache(ctx).setdefault("file_lines", {})
    if path not in c:
        c[path] = (ctx.text_at_head(path) or "").splitlines()
    return c[path]


# -- signatures -------------------------------------------------------------
_SIG_RX = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function\*?)\s+(\w+)\s*\((.*)\)\s*"
                     r"(?:(?:->|:)\s*([^{:]+?))?\s*[:{]?\s*$")


def _split_params(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def _parse_params(s: str) -> list[tuple[str, str | None, str | None]]:
    """-> [(name, default, annotation)] without self/cls."""
    res = []
    for p in _split_params(s):
        m = re.match(r"\**\s*(\w+)", p)
        if not m or m.group(1) in ("self", "cls"):
            continue
        default = ann = None
        body = p[m.end():]
        if "=" in body:
            body, default = body.split("=", 1)
            default = default.strip()
        if ":" in body:
            ann = body.split(":", 1)[1].strip()
        res.append((m.group(1), default, ann))
    return res


def _sig_params_of(signature: str | None) -> list[tuple[str, str | None, str | None]]:
    sig = signature or ""
    i = sig.find("(")
    if i < 0:
        return []
    depth = 0
    for j in range(i, len(sig)):
        depth += sig[j] == "("
        depth -= sig[j] == ")"
        if depth == 0:
            return _parse_params(sig[i + 1:j])
    return _parse_params(sig[i + 1:])


def _sig_ret(signature: str | None) -> str:
    """Whitespace-free return annotation of a signature ('' when none)."""
    sig = signature or ""
    i = sig.find("(")
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(sig)):
        depth += sig[j] == "("
        depth -= sig[j] == ")"
        if depth == 0:
            m = re.match(r"\s*->\s*(.+?)\s*:?\s*$", sig[j + 1:], re.S)
            return re.sub(r"\s+", "", m.group(1)) if m else ""
    return ""


def _version_sig_changes(ctx: Ctx) -> list[dict]:
    """Parameter-list / return-annotation changes between consecutive recorded versions of the same function
    (``symbol_versions``): exact, includes multi-line signatures, ignores renames/moves (different uid / path)."""
    out = []
    for uid, vs in ctx.versions_by_uid.items():
        for a, b in zip(vs, vs[1:]):
            if b["kind"] != "function" or is_test(b["path"]) or a["path"] != b["path"] or not a["signature"] or not b["signature"]:
                continue
            old, new = _sig_params_of(a["signature"]), _sig_params_of(b["signature"])
            norm = lambda ps: [(n, re.sub(r"\s+", "", d or ""), re.sub(r"\s+", "", an or "")) for n, d, an in ps]
            if norm(old) == norm(new) and _sig_ret(a["signature"]) == _sig_ret(b["signature"]):
                continue
            out.append({"hash": b["hash"], "ts": b["ts"], "seq": b["seq"], "path": b["path"],
                        "name": b["qualname"].rsplit(".", 1)[-1], "old": old, "new": new,
                        "old_line": a["signature"].strip(), "new_line": b["signature"].strip(), "uid": uid,
                        "params_changed": [(n, d_) for n, d_, _ in old] != [(n, d_) for n, d_, _ in new]})
    return out


def _sig_changes(ctx: Ctx) -> list[dict]:
    """Every commit where a function's parameter list or return annotation changed (same name). Primary source:
    ``symbol_versions`` signatures (exact, multi-line aware). Fallback, only for (commit, file) pairs that have no
    recorded symbol version (languages the parser does not cover): ``def``/``function`` lines in diff_lines,
    single-line signatures only."""
    c = _cache(ctx)
    if "sigch" in c:
        return c["sigch"]
    have = {(v["hash"], v["path"]) for vs in ctx.versions_by_uid.values() for v in vs}
    out = _version_sig_changes(ctx)
    out += [e for e in _sig_changes_diff(ctx) if (e["hash"], e["path"]) not in have]
    out.sort(key=lambda e: e["seq"])
    c["sigch"] = out
    return out


def _sig_changes_diff(ctx: Ctx) -> list[dict]:
    """diff_lines fallback of :func:`_sig_changes`."""
    c = _cache(ctx)
    if "sigch_diff" in c:
        return c["sigch_diff"]
    rows = ctx.grep(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function\*?)\s+\w+\s*\(", None,
                    path_regex=r"\.(py|js|jsx|ts|tsx|mjs)$", limit=300000)
    grouped: dict[tuple, dict[str, list]] = defaultdict(lambda: {"-": [], "+": []})
    for r in rows:
        if is_test(r["path"]):
            continue
        m = _SIG_RX.match(r["text"])
        if not m:
            continue
        grouped[(r["hash"], r["path"], m.group(1))][r["sign"]].append((r, m))
    out = []
    for (h, path, name), d in grouped.items():
        if not d["-"] or len(d["-"]) != len(d["+"]):
            continue
        for (ro, mo), (rn, mn) in zip(d["-"], d["+"]):
            old, new = _parse_params(mo.group(2)), _parse_params(mn.group(2))
            norm = lambda m: re.sub(r"\s+", "", m.group(2)).rstrip(",") + "->" + re.sub(r"\s+", "", m.group(3) or "")
            if norm(mo) == norm(mn):
                continue
            out.append({"hash": h, "ts": rn["ts"], "seq": rn["seq"], "path": path, "name": name, "old": old,
                        "new": new, "old_line": ro["text"].strip(), "new_line": rn["text"].strip(),
                        "params_changed": [(n, d_) for n, d_, _ in old] != [(n, d_) for n, d_, _ in new]})
    out.sort(key=lambda e: e["seq"])
    c["sigch_diff"] = out
    return out


def _raise_changes(ctx: Ctx) -> list[dict]:
    """Consecutive recorded versions of a function whose set of raised exception types differs."""
    out = []
    for uid, vs in ctx.versions_by_uid.items():
        for a, b in zip(vs, vs[1:]):
            if b["kind"] != "function" or is_test(b["path"]) or a["path"] != b["path"]:
                continue
            ra, rb = sorted(set(ctx.raises_of(a))), sorted(set(ctx.raises_of(b)))
            if ra == rb:
                continue
            out.append({"hash": b["hash"], "ts": b["ts"], "seq": b["seq"], "path": b["path"],
                        "name": b["qualname"].rsplit(".", 1)[-1], "old": [], "new": [], "uid": uid,
                        "old_line": f"{(a['signature'] or '').strip()}  # raises {ra}",
                        "new_line": f"{(b['signature'] or '').strip()}  # raises {rb}", "params_changed": False,
                        "change_kind": "raises", "old_raises": ra, "new_raises": rb})
    return out


def _uid_for(ctx: Ctx, path: str, name: str) -> str | None:
    for s in ctx.symbols_by_path.get(path, []):
        if s["name"] == name and s["kind"] == "function":
            return s["uid"]
    return None


# -- mock / patch targets ----------------------------------------------------
_PATCH_RX = re.compile(r"""(?:\bpatch|mocker\.patch)\(\s*(?P<q>["'])(?P<t>[\w.]+)(?P=q)(?P<rest>[^\n]*)""")
_PATCH_OBJ_RX = re.compile(r"""patch\.object\(\s*(?P<obj>[\w.]+)\s*,\s*["'](?P<attr>\w+)["'](?P<rest>[^\n]*)""")
_MP_RX = re.compile(r"""monkeypatch\.setattr\(\s*(?:["'](?P<t>[\w.]+)["']|(?P<obj>[\w.]+)\s*,\s*["'](?P<attr>\w+)["'])"""
                    r"""(?P<rest>[^\n]*)""")
_JS_MOCK_RX = re.compile(r"""\b(?:jest|vi)\.(?:mock|doMock)\(\s*["'`](?P<m>[^"'`]+)["'`]""")
_SPY_RX = re.compile(r"""\bspyOn\(\s*(?P<obj>[\w.]+)\s*,\s*["'`](?P<attr>\w+)["'`]""")
_INFRA_RX = re.compile(r"(?i)\b(time|datetime|date|random|requests|httpx|urllib\w*|socket|subprocess|environ|getenv|"
                       r"sleep|open|boto3?|os|shutil|pathlib|uuid|secrets|input|print|logging|tempfile|aiohttp)\b")


def _mod_index(ctx: Ctx) -> dict[str, list[str]]:
    c = _cache(ctx)
    if "modidx" not in c:
        idx: dict[str, list[str]] = defaultdict(list)
        for p in ctx.head_files:
            if p.endswith(".py"):
                parts = p[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                for i in range(len(parts)):
                    idx[".".join(parts[i:])].append(p)
        c["modidx"] = idx
    return c["modidx"]


def _resolve_dotted(ctx: Ctx, target: str) -> set[str]:
    parts = target.split(".")
    pq = _sym_by_path_qual(ctx)
    idx = _mod_index(ctx)
    for i in range(len(parts) - 1, 0, -1):
        mod, qual = ".".join(parts[:i]), ".".join(parts[i:])
        found = {pq[(p, qual)]["uid"] for p in idx.get(mod, []) if (p, qual) in pq}
        if found:
            return found
    return set()


def _resolve_obj_attr(ctx: Ctx, obj: str, attr: str) -> set[str]:
    c = _cache(ctx)
    if "qual_idx" not in c:
        qi: dict[str, set[str]] = defaultdict(set)
        for s in ctx.alive_symbols:
            qi[s["qualname"]].add(s["uid"])
            stem = s["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            qi[f"{stem}.{s['name']}"].add(s["uid"])
        c["qual_idx"] = qi
    return set(c["qual_idx"].get(f"{obj.split('.')[-1]}.{attr}", set()))


def _patches_in(ctx: Ctx, key: str, text: str) -> list[dict]:
    """Patch targets in a test text: [{raw, uids, modules (paths), autospec, full_string}]."""
    cc = _cache(ctx).setdefault("patches", {})
    if key in cc:
        return cc[key]
    out = []

    def add(raw, uids, modules, rest, full):
        out.append({"raw": raw, "uids": uids, "modules": modules, "full_string": full,
                    "autospec": bool(re.search(r"autospec|\bspec(_set)?\s*=", rest or ""))})

    for m in _PATCH_RX.finditer(text):
        add(m["t"], _resolve_dotted(ctx, m["t"]), set(), m["rest"], True)
    for m in _MP_RX.finditer(text):
        if m["t"]:
            add(m["t"], _resolve_dotted(ctx, m["t"]), set(), m["rest"], True)
        else:
            add(f"{m['obj']}.{m['attr']}", _resolve_obj_attr(ctx, m["obj"], m["attr"]), set(), m["rest"], False)
    for m in _PATCH_OBJ_RX.finditer(text):
        add(f"{m['obj']}.{m['attr']}", _resolve_obj_attr(ctx, m["obj"], m["attr"]), set(), m["rest"], False)
    for m in _SPY_RX.finditer(text):
        add(f"{m['obj']}.{m['attr']}", _resolve_obj_attr(ctx, m["obj"], m["attr"]), set(), "", False)
    for m in _JS_MOCK_RX.finditer(text):
        stem = m["m"].rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        mods = {p for p in ctx.head_files if p.rsplit("/", 1)[-1].rsplit(".", 1)[0] == stem and not is_test(p)}
        add(m["m"], set(), mods, "", False)
    cc[key] = out
    return out


def _test_patches(ctx: Ctx, tid: str) -> list[dict]:
    return _patches_in(ctx, tid, _test_text(ctx, tid))


# ---------------------------------------------------------------------------- 1. coverage_threshold_lowered
_COV_PATH_RX = (r"(?i)(\.coveragerc|pyproject\.toml|setup\.cfg|tox\.ini|pytest\.ini|jest\.config\.[cm]?[jt]s|"
                r"package\.json|\.?codecov\.ya?ml|Makefile|\.github/workflows/[^/]+\.ya?ml|noxfile\.py|"
                r"\.gitlab-ci\.ya?ml|\.circleci/.*|\.travis\.ya?ml)$")
_THRESH_RX = (r"fail[_-]under\s*[=:]\s*\d|--cov-fail-under[= ]\s*\d|\"?(statements|branches|functions|lines)\"?\s*:\s*\d|"
              r"\btarget:\s*\d")
_THRESH_PARTS = [
    ("fail_under", re.compile(r"fail[_-]under\s*[=:]\s*(\d+(?:\.\d+)?)")),
    ("cov-fail-under", re.compile(r"--cov-fail-under[= ]\s*(\d+(?:\.\d+)?)")),
    ("jest", re.compile(r"\"?(statements|branches|functions|lines)\"?\s*:\s*(\d+(?:\.\d+)?)")),
    ("codecov_target", re.compile(r"\btarget:\s*(\d+(?:\.\d+)?)")),
]
_PRAGMA_RX = r"pragma:\s*no\s*cover|istanbul\s+ignore|c8\s+ignore|v8\s+ignore"
_PRAGMA_SAFE_RX = re.compile(r"TYPE_CHECKING|NotImplementedError|__repr__|__str__|abstract|__main__")


def _thresh_key(path: str, text: str):
    for name, rx in _THRESH_PARTS:
        m = rx.search(text)
        if m:
            if name == "jest":
                if not re.search(r"jest\.config|package\.json", path):
                    continue
                return f"jest.{m.group(1)}", float(m.group(2))
            if name == "codecov_target" and "codecov" not in path.lower():
                continue
            return name, float(m.group(1))
    return None


@candidate("coverage_threshold_lowered")
def coverage_threshold_lowered(ctx: Ctx):
    """Threshold timeline (fail_under, --cov-fail-under, jest coverageThreshold, codecov target) from diff_lines:
    a decrease of >=2 points not restored within 90 days; new `omit` entries matching a file with fix history;
    >=5 net `pragma: no cover` / istanbul additions inside a 90-day window. No measured coverage is needed."""
    hits: list[Hit] = []
    rows = ctx.grep(_THRESH_RX, None, path_regex=_COV_PATH_RX)
    by: dict[tuple, dict] = defaultdict(lambda: {"-": None, "+": None})
    timeline: dict[tuple, list] = defaultdict(list)
    for r in rows:
        k = _thresh_key(r["path"], r["text"])
        if not k:
            continue
        by[(r["hash"], r["path"], k[0])][r["sign"]] = (k[1], r)
    for (h, path, key), d in by.items():
        if d["+"]:
            timeline[(path, key)].append((d["+"][1]["ts"], d["+"][0]))
    for (h, path, key), d in sorted(by.items(), key=lambda kv: (kv[1]["+"] or kv[1]["-"])[1]["seq"]):
        if not d["-"] or not d["+"]:
            ctx.drop("unpaired_threshold_change")
            continue
        old, new = d["-"][0], d["+"][0]
        if old - new <= 0:
            continue
        r = d["+"][1]
        if old - new < 2:
            ctx.drop("under_two_points")
            continue
        if any(ts > r["ts"] and ts <= r["ts"] + 90 * DAY and v >= old for ts, v in timeline[(path, key)]):
            ctx.drop("recovered_within_90d")
            continue
        c = ctx.commit(h)
        hits.append(Hit(
            subject=f"threshold|{h}|{path}|{key}", score=3 + (old - new),
            title=f"{key} lowered {old:g} -> {new:g} in {path} ({subject_of(c['message'])[:60]})",
            evidence={"key": key, "old": old, "new": new, "commit_subject": subject_of(c["message"]),
                      "later_values": [(ts, v) for ts, v in timeline[(path, key)] if ts > r["ts"]][:4]},
            commits=[h], diffs=[(h, path)], paths=[path]))
    # omit growth
    fix_paths = Counter(f["path"] for f in ctx.fix_events)
    omit_rows = ctx.grep(r"[\w*/.\\-]+", "+", path_regex=r"(?i)(\.coveragerc|pyproject\.toml|setup\.cfg|tox\.ini)$")
    for r in omit_rows:
        text = r["text"].strip()
        if re.match(r"^\s*(omit|exclude\w*|source|include|branch|fail_under|\[)", text) or text.startswith("#"):
            continue
        if not _in_omit_section(ctx, r):
            continue
        entry = re.sub(r"""^[\s\-"',]+|[\s"',\]]+$""", "", text)
        if not entry or not re.search(r"[/.*]", entry):
            continue
        if is_generated(entry) or re.search(r"(?i)generated|_pb2|\.min\.", entry):
            ctx.drop("generated_omit")
            continue
        pat = entry.lstrip("./")
        matched = [p for p in fix_paths if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, "*/" + pat.lstrip("*/"))
                   or p.endswith(pat) or (pat.endswith("/") and p.startswith(pat))]
        if not matched:
            ctx.drop("omit_without_fix_history")
            continue
        nfix = sum(fix_paths[p] for p in matched)
        hits.append(Hit(
            subject=f"omit|{r['hash']}|{entry}", score=3 + nfix,
            title=f"coverage omit '{entry}' added in {r['path']} — covers {len(matched)} file(s) with {nfix} fixes",
            evidence={"entry": entry, "matched_files": matched[:6], "fixes": nfix,
                      "commit_subject": subject_of(ctx.commit(r["hash"])["message"])},
            commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"], *matched[:4]]))
    # pragma growth
    prag = ctx.grep(_PRAGMA_RX, None)
    events = []
    for r in prag:
        if is_test(r["path"]) or is_vendored(r["path"]) or is_generated(r["path"]) or is_docs(r["path"]):
            continue
        if _PRAGMA_SAFE_RX.search(r["text"]):
            ctx.drop("benign_pragma")
            continue
        events.append((r["ts"], 1 if r["sign"] == "+" else -1, r))
    events.sort(key=lambda e: e[0])
    i = 0
    while i < len(events):
        j, net = i, 0
        while j < len(events) and events[j][0] - events[i][0] <= 90 * DAY:
            net += events[j][1]
            j += 1
        if net >= 5:
            window = events[i:j]
            added = [e[2] for e in window if e[1] > 0]
            commits = list(dict.fromkeys(r["hash"] for r in added))
            hits.append(Hit(
                subject=f"pragma|{commits[0]}", score=2 + net,
                title=f"{net} net coverage-exclusion pragmas added within 90 days",
                evidence={"net_added": net, "files": sorted({r["path"] for r in added})[:8],
                          "sample": [r["text"].strip()[:100] for r in added[:5]]},
                commits=commits[:8], diffs=[(r["hash"], r["path"]) for r in added[:4]],
                paths=sorted({r["path"] for r in added})[:8]))
            i = j
        else:
            i += 1
    return _top(hits)


def _in_omit_section(ctx: Ctx, r) -> bool:
    text = ctx.text_at(r["hash"], r["path"])
    if not text:
        return False
    lines = text.splitlines()
    for k in range(min(r["line"], len(lines)) - 2, max(-1, r["line"] - 60), -1):
        ln = lines[k]
        if re.match(r"^\s*omit\s*[=:]", ln):
            return True
        if re.match(r"^\s*\[", ln) and not re.match(r"^\s*\[\s*$", ln) and "\"" not in ln and "'" not in ln:
            return False
        if re.match(r"^\s*\w[\w.]*\s*[=:]", ln):
            return False
    return False


# ---------------------------------------------------------------------------- 2. error_branch_untested
_ERR_GUARDS = {"raise", "exception_handling", "none_check", "empty_check"}
_ERR_TEST_RX = re.compile(r"raises|assertRaises|toThrow|throws|side_effect|mockRejectedValue|rejects|\bNone\b|\bnull\b|"
                          r"undefined|\(\s*\)|[\"']{2}|\[\s*\]|\{\s*\}|Error\b|Exception\b|timeout")
_DEF_RX = re.compile(r"\b(?:async\s+)?def\s+\w+\([^)]*\)|\bfunction\s+\w+\([^)]*\)")
_ERR_SKIP_RX = re.compile(r"ImportError|ModuleNotFoundError|KeyboardInterrupt|TYPE_CHECKING|NotImplementedError|"
                          r"assert_never|pragma:\s*no\s*cover")
_IO_RX = re.compile(r"(?i)timeout|retry|IOError|OSError|ConnectionError|urlopen|requests\.|socket|TimeoutError|"
                    r"ECONN|fetch\(")


@candidate("error_branch_untested")
def error_branch_untested(ctx: Ctx):
    """Guard lines (raise/except/None/empty checks) added by fix commits, on symbols that have linked tests none of
    which contains an error-path marker (raises/side_effect/None or empty literal ...). Needs fan_in>=2 or >=2 fix
    edits; I/O/timeout/retry guards get a score boost.

    Measured path: when the sandbox measured the symbol's file, each guard line is located in the HEAD body and the
    branch it opens is checked with ``ctx.covered``; a hit needs at least one branch with 0 executions (the linked-test
    and error-marker text checks are skipped) and is dropped (``guard_branch_executed``) when every located branch ran.
    Fallback (coverage not measured or guard not located): the linked-test text proxy."""
    per_uid: dict[str, list] = defaultdict(list)
    for f in ctx.fix_events:
        gl = [g for g in json.loads(f["guards"] or "[]") if g["kind"] in _ERR_GUARDS]
        if gl:
            per_uid[f["uid"]].append((f, gl))
    hits = []
    for uid, items in per_uid.items():
        s = ctx.symbols.get(uid)
        if s is None or not s["alive"] or not _prod(s):
            ctx.drop("symbol_gone_or_not_production")
            continue
        guards = [g for _, gl in items for g in gl]
        guards = [g for g in guards if not _ERR_SKIP_RX.search(g["line"])]
        if not guards:
            ctx.drop("benign_guard_kind")
            continue
        body = s["body"] or ""
        if re.search(r"except[^\n]*:\s*\n\s*(?:log|logger|logging|_log)\.\w+\([^\n]*\)\s*\n\s*raise\s*$", body, re.M):
            ctx.drop("log_and_reraise")
            continue
        tests = _tests_of(ctx, s)
        fan_in = len(ctx.callers(uid))
        mh = _guard_hits(ctx, s, [g["line"] for g in guards])
        if mh is None and not tests:
            ctx.drop("no_linked_tests")
            continue
        if fan_in < 2 and len(items) < 2:
            ctx.drop("low_fan_in_no_fix_history")
            continue
        if mh is not None:
            if min(mh) > 0:
                ctx.drop("guard_branch_executed")
                continue
        elif _ERR_TEST_RX.search(_DEF_RX.sub("", _linked_text(ctx, s))):
            ctx.drop("tests_exercise_error_path")
            continue
        io = bool(_IO_RX.search(" ".join(g["line"] for g in guards)) or _IO_RX.search(body))
        f0 = items[-1][0]
        hits.append(Hit(
            subject=uid, score=(len(items) + 0.5 * fan_in) * (1.5 if io else 1) * (1.2 - ctx.best_test_weight(uid) * 0.4),
            title=f"{s['qualname']}: fix-added {guards[0]['kind']} guard, no linked test has an error-path input",
            evidence={"guards": [g["line"] for g in guards[:5]], "fix_subjects": [f["subject"] for f, _ in items[:4]],
                      "fan_in": fan_in, "linked_tests": tests[:5], "io_boost": io,
                      "measured": mh is not None, "guard_branch_executions": mh},
            commits=[f["hash"] for f, _ in items[:4]], diffs=[(f0["hash"], f0["path"])], paths=[s["path"]],
            symbols=[uid]))
    return _top(hits)


# ---------------------------------------------------------------------------- 3. flaky_test_markers
_FLAKY_MSG_RX = re.compile(r"(?i)flak|intermittent|sporadic|\brerun|retry|retries|\btiming\b|\brace\b")
_FLAKY_LINE_RX = (r"@flaky|pytest\.mark\.flaky|reruns\s*=|retryTimes|\.retries\(|pytest\.mark\.skip.*(flak|intermittent)|"
                  r"time\.sleep\(|\bsleep\(|jest\.setTimeout|timeout\s*=\s*\d+|timeout:\s*\d+")


@candidate("flaky_test_markers")
def flaky_test_markers(ctx: Ctx):
    """Per test file: commits touching it that either carry a flake-ish message (flak/intermittent/rerun/retry/timing/
    race) or add a marker line (@flaky, reruns=, new sleep(), raised timeout). Flag >=2 marker commits. Joined with race
    fixes on the symbols the file tests."""
    marks: dict[str, dict[str, dict]] = defaultdict(dict)  # path -> hash -> info
    for c in ctx.main_commits:
        if c["is_bulk"] or c["is_merge"] or not _FLAKY_MSG_RX.search(c["message"] or ""):
            continue
        if re.match(r"(?i)^(ci|build|chore\(ci\))\b", subject_of(c["message"])):
            ctx.drop("ci_infra_commit")
            continue
        for fc in ctx.changes(c["hash"]):
            if is_test(fc["path"]) and is_code(fc["path"]):
                marks[fc["path"]].setdefault(c["hash"], {"why": "message", "subject": subject_of(c["message"])})
    rows = ctx.grep(_FLAKY_LINE_RX, "+", path_regex=TEST_RX.pattern)
    removed = {(r["hash"], r["path"]): r["text"] for r in ctx.grep(r"timeout\s*[=:]\s*\d+", "-", path_regex=TEST_RX.pattern)}
    for r in rows:
        if not is_code(r["path"]):
            continue
        line = r["text"]
        if re.search(r"timeout", line) and not re.search(r"@flaky|reruns", line):
            m_new = re.search(r"timeout\s*[=:]\s*(\d+)", line)
            m_old = re.search(r"timeout\s*[=:]\s*(\d+)", removed.get((r["hash"], r["path"]), ""))
            if not (m_new and m_old and int(m_new.group(1)) > int(m_old.group(1))):
                continue
        if re.search(r"sleep\(", line) and re.search(r"(?i)rate|throttl|limit|debounce|backoff|expire|ttl", line + r["path"]):
            ctx.drop("rate_limit_sleep")
            continue
        marks[r["path"]].setdefault(r["hash"], {"why": "line", "subject": subject_of(ctx.commit(r["hash"])["message"]),
                                                "line": line.strip()[:100]})
    rev: dict[str, set] = defaultdict(set)
    for target, rws in ctx.tests_for.items():
        for tr in rws:
            rev[tr["test"].split("::")[0]].add(target)
    hits = []
    for path, d in marks.items():
        if len(d) < 2:
            ctx.drop("single_marker_commit")
            continue
        race = sum(1 for t in rev.get(path, ()) for f in ctx.fixes_by_uid.get(t, []) if "race" in (f["kinds"] or ""))
        hs = sorted(d, key=lambda h: ctx.by_hash[h]["seq"])
        hits.append(Hit(
            subject=path, score=len(d) + 2 * race,
            title=f"{path}: {len(d)} flake/timing marker commits" + (f", {race} race fixes on code it tests" if race else ""),
            evidence={"marker_commits": [{"hash": h, **d[h]} for h in hs[:8]], "race_fixes_on_tested_code": race},
            commits=hs[:8], diffs=[(h, path) for h in hs[:4]], paths=[path]))
    return _top(hits)


# ---------------------------------------------------------------------------- 4. guard_input_class_unfed
_CLASS_RX = {
    "none": re.compile(r"\bis\s+(not\s+)?None\b|[!=]==?\s*(null|undefined)\b|==\s*None\b"),
    "empty": re.compile(r"^\s*(?:if|elif)\s+not\s+\w+|\blen\(\w+\)\s*(==|<=?)\s*[01]\b|\.length\s*(===?|<=?)\s*[01]\b|"
                        r"==\s*[\"']{2}|==\s*\[\s*\]|^\s*if\s*\(\s*!\w+"),
    "zero_negative": re.compile(r"(<=|<|==|===)\s*0\b|\b0\s*(>=|>|==)|\bmax\(\s*0|[<>]=?\s*-\d"),
}
_CLASS_LIT_RX = {
    "none": re.compile(r"\bNone\b|\bnull\b|\bundefined\b"),
    "empty": re.compile(r"\(\s*\)|[\"']{2}|\[\s*\]|\{\s*\}|\bb[\"']{2}"),
    "zero_negative": re.compile(r"(?<![\w.\]])-\s*\d|(?<![\w.])0(?![\w.])"),
}


def _guard_class(line: str) -> list[str]:
    return [k for k, rx in _CLASS_RX.items() if rx.search(line)]


@candidate("guard_input_class_unfed")
def guard_input_class_unfed(ctx: Ctx):
    """Fix commits that added a None / empty / zero-negative guard inside a mapped function, where the fix touched no
    test, and (proxy) the symbol has linked tests none of which contains a literal of that input class. Dropped when
    the type hint excludes the class, the guard only logs, or helpers are private with <2 callers.

    Measured path: when the sandbox measured the file, the proxy is replaced by the real fact: the branch opened by the
    guard line (located in the HEAD body, ``ctx.covered``) has 0 executions; a guard branch that ran is dropped
    (``guard_branch_executed``). With per-test coverage (``covered_line_tests``) the guard line is also located and
    ``ctx.tests_covering`` gives the tests that actually execute it: if any of those tests (source text) contains a
    literal of the input class the symbol is dropped (``class_literal_in_covering_test``), otherwise it is a hit
    (``covering_tests`` in the evidence; no covering test at all also counts as unfed). Fallback when the file is not
    measured, per-test data is absent, or the guard line cannot be located: the linked-test literal proxy."""
    reverted = {r["reverted"] for r in ctx.reverts}
    by_key: dict[tuple, list] = defaultdict(list)
    for f in ctx.fix_events:
        c = ctx.commit(f["hash"])
        if c is None or c["is_bulk"] or f["hash"] in reverted:
            ctx.drop("bulk_or_reverted_commit")
            continue
        for g in json.loads(f["guards"] or "[]"):
            if g["kind"] not in ("none_check", "empty_check", "bounds", "raise", "assert"):
                continue
            line = g["line"]
            if re.search(r"\b(self|cls)\b\s*(is|==)", line):
                ctx.drop("self_cls_guard")
                continue
            for cl in _guard_class(line):
                by_key[(f["uid"], cl)].append((f, line))
    hits = []
    for (uid, cl), items in by_key.items():
        s = ctx.symbols.get(uid)
        if s is None or not s["alive"] or not _prod(s) or re.search(r"(?i)migration", s["path"]):
            ctx.drop("symbol_gone_or_excluded_path")
            continue
        items = [(f, ln) for f, ln in items
                 if not json.loads(f["tests"] or "[]") and not any(is_test(fc["path"]) for fc in ctx.changes(f["hash"]))]
        if not items:
            ctx.drop("fix_touched_test")
            continue
        if all(re.search(r"\b(log|logger|logging|print)\b", ln) for _, ln in items):
            ctx.drop("guard_only_logs")
            continue
        params = _sig_params_of(s["signature"])
        if cl == "none" and params and all(a for _, _, a in params) and not re.search(
                r"Optional|None|\?|undefined|null|Any", s["signature"] or ""):
            ctx.drop("type_hint_excludes_class")
            continue
        callers = len(ctx.callers(uid))
        if s["name"].startswith("_") and callers < 2:
            ctx.drop("private_helper_few_callers")
            continue
        tests = _tests_of(ctx, s)
        mh = _guard_hits(ctx, s, [ln for _, ln in items])
        covering: set[str] | None = None
        if _per_test_cov(ctx, s["path"]):
            gl = _locate_lines(ctx, s, [ln for _, ln in items])
            if gl:
                covering = _covering_tests(ctx, s["path"], gl)
        if mh is not None and min(mh) > 0:
            ctx.drop("guard_branch_executed")
            continue
        if covering is not None:
            if any(_CLASS_LIT_RX[cl].search(_DEF_RX.sub("", _test_text(ctx, t))) for t in covering):
                ctx.drop("class_literal_in_covering_test")
                continue
        elif mh is not None:
            pass
        elif not tests:
            ctx.drop("no_linked_tests")
            continue
        elif _CLASS_LIT_RX[cl].search(_DEF_RX.sub("", _linked_text(ctx, s))):
            ctx.drop("class_literal_in_test")
            continue
        nfix = _fix_counts(ctx)[uid]
        f0 = items[-1][0]
        hits.append(Hit(
            subject=f"{uid}|{cl}", score=nfix * (1 + 0.1 * min(20, callers)) * (1.2 - ctx.best_test_weight(uid) * 0.4),
            title=f"{s['qualname']}: {cl} guard added by fix, no linked test feeds a {cl} input",
            evidence={"input_class": cl, "guards": [ln for _, ln in items[:4]], "fix_subjects": [f["subject"] for f, _ in items[:3]],
                      "symbol_fixes": nfix, "callers": callers, "linked_tests": tests[:5],
                      "measured": mh is not None, "guard_branch_executions": mh,
                      "covering_tests": sorted(covering)[:5] if covering is not None else None},
            commits=[f["hash"] for f, _ in items[:3]], diffs=[(f0["hash"], f0["path"])], paths=[s["path"]],
            symbols=[uid]))
    return _top(hits)


# ---------------------------------------------------------------------------- 5. interface_change_broke_seam
@candidate("interface_change_broke_seam")
def interface_change_broke_seam(ctx: Ctx):
    """C1: a function whose parameter list or return annotation changed between two recorded versions
    (``symbol_versions`` signatures: exact, multi-line signatures included, renames/moves and whitespace-only changes
    ignored; diff_lines ``def`` lines, single-line only, are used just for files with no recorded versions). A change
    in the raised exception types between consecutive versions (``ctx.raises_of(version)``, the ``raises`` column of
    ``symbol_versions``) counts as an interface change too (``change_kind`` = "raises", old/new raise lists in the
    evidence); versions with no ``raises`` data contribute none, so the fallback is signature-only. C2: a fix commit
    within 30 days / 20 main-line commits touching a non-test file in a different top-level package that mentions the
    symbol name in its diff (or the commit message), and not the callee file."""
    mc = ctx.main_commits
    pos = {c["hash"]: i for i, c in enumerate(mc)}
    lcache: dict[tuple, str] = {}

    def diff_text(sha, path):
        k = (sha, path)
        if k not in lcache:
            lcache[k] = "\n".join(r["text"] for r in ctx.lines_of(sha, path))
        return lcache[k]

    hits = []
    changes = list(_sig_changes(ctx))
    seen = {(c["hash"], c["path"], c["name"]) for c in changes}
    changes += [c for c in _raise_changes(ctx) if (c["hash"], c["path"], c["name"]) not in seen]
    changes.sort(key=lambda e: e["seq"])
    for ch in changes:
        i = pos.get(ch["hash"])
        c1 = ctx.commit(ch["hash"])
        if i is None or c1["is_bulk"]:
            ctx.drop("not_main_line_or_bulk")
            continue
        name, pkg1 = ch["name"], _pkg(ch["path"])
        rx = re.compile(rf"\b{re.escape(name)}\b")
        reason, found = "no_followup_fix", None
        for j in range(i + 1, min(len(mc), i + 21)):
            c2 = mc[j]
            if c2["ts"] - c1["ts"] > 30 * DAY:
                break
            if c2["is_bulk"] or c2["is_merge"] or not ctx.is_fix(c2):
                continue
            files = [fc["path"] for fc in ctx.changes(c2["hash"]) if is_code(fc["path"]) and not is_test(fc["path"])]
            if ch["path"] in files:
                reason = "callee_touched_again"
                continue
            cross = [f for f in files if _pkg(f) != pkg1]
            if not cross:
                reason = "same_package_caller" if files else reason
                continue
            msg_hit = bool(rx.search(c2["message"] or ""))
            mention = [f for f in cross if rx.search(diff_text(c2["hash"], f))]
            if not mention and not msg_hit:
                reason = "unrelated_followup"
                continue
            found = (c2, mention or cross)
            break
        if not found:
            ctx.drop(reason)
            continue
        c2, files = found
        gap = (c2["ts"] - c1["ts"]) / DAY
        c1_cross = [fc["path"] for fc in ctx.changes(ch["hash"]) if is_code(fc["path"]) and _pkg(fc["path"]) != pkg1]
        uid = ch.get("uid") or _uid_for(ctx, ch["path"], name)
        hits.append(Hit(
            subject=f"{ch['hash']}|{c2['hash']}|{name}", score=10 / (1 + gap) + (3 if not c1_cross else 0),
            title=f"{name}() signature changed, then fix {subject_of(c2['message'])[:60]!r} in {_pkg(files[0])}",
            evidence={"old_signature": ch["old_line"][:160], "new_signature": ch["new_line"][:160], "gap_days": round(gap, 1),
                      "change_kind": ch.get("change_kind", "signature"), "old_raises": ch.get("old_raises"),
                      "new_raises": ch.get("new_raises"),
                      "c1_edited_cross_package_files": c1_cross[:4], "fix_files": files[:4],
                      "fix_subject": subject_of(c2["message"])},
            commits=[ch["hash"], c2["hash"]], diffs=[(ch["hash"], ch["path"]), (c2["hash"], files[0])],
            paths=[ch["path"], *files[:3]], symbols=[uid] if uid else []))
    return _top(hits)


# ---------------------------------------------------------------------------- 6. platform_config_branch_unexercised
_PLAT_COND_RX = re.compile(r"sys\.platform|os\.name\b|platform\.system\(\)|process\.platform|runtime\.GOOS|"
                           r"\bIS_WINDOWS\b|\bis_windows\b|os\.sep\b|WIN32|_WIN32|\bsys\.getwindowsversion")
_OS_NAMES = {"windows": r"windows|win32|\bwin\b|msys|\bnt\b", "macos": r"mac\s?os|darwin|\bosx\b",
             "linux": r"linux", "posix": r"posix|unix"}
_PLAT_TEST_RX = re.compile(r"sys\.platform|skipif|os\.name|platform\.system|process\.platform|win32|darwin|\bwindows\b|"
                           r"IS_WINDOWS|is_windows|GOOS")


def _ci_text(ctx: Ctx) -> str:
    c = _cache(ctx)
    if "ci_text" not in c:
        c["ci_text"] = "\n".join((ctx.text_at_head(p) or "") for p in ctx.head_files if is_ci(p)).lower()
    return c["ci_text"]


@candidate("platform_config_branch_unexercised")
def platform_config_branch_unexercised(ctx: Ctx):
    """Functions with an OS/platform conditional (sys.platform, os.name, platform.system(), process.platform ...), >=1
    fix whose subject names an OS, and either no linked test referencing the platform or a CI matrix that lacks
    that OS. Proxy for 'arm never run': test text + CI file text instead of coverage contexts."""
    ci = _ci_text(ctx)
    hits = []
    for s in ctx.alive_symbols:
        if not _prod(s) or s["kind"] != "function":
            continue
        body = s["body"] or ""
        lines = [ln for ln in body.splitlines() if _PLAT_COND_RX.search(ln) and re.search(r"\bif\b|\belif\b|==|!=|startswith|\?|&&|\|\|", ln)]
        if not lines:
            continue
        if re.search(r"(?i)(^|/)(setup|build|conftest|noxfile|tasks)[^/]*\.py$|(^|/)(scripts?|tools|build)/", s["path"]):
            ctx.drop("setup_or_build_script")
            continue
        if all(re.search(r"TYPE_CHECKING|__main__|CI\b|PYTHONUNBUFFERED", ln) for ln in lines):
            ctx.drop("benign_probe")
            continue
        fixes = [f for f in ctx.fixes_by_uid.get(s["uid"], [])
                 if any(re.search(rx, f["subject"] or "", re.I) for rx in _OS_NAMES.values())]
        if not fixes:
            ctx.drop("no_os_fix")
            continue
        oses = {k for k, rx in _OS_NAMES.items() if any(re.search(rx, f["subject"], re.I) for f in fixes)}
        text = _linked_text(ctx, s)
        test_refs = bool(_PLAT_TEST_RX.search(text))
        ci_lacks = not ci or not all(re.search(_OS_NAMES[o].replace(r"\bnt\b", "nt"), ci) for o in oses if o != "posix")
        if test_refs and not ci_lacks:
            ctx.drop("tests_and_ci_cover_platform")
            continue
        hits.append(Hit(
            subject=s["uid"], score=(len(fixes) + 1) * (1.5 if (not test_refs and ci_lacks) else 1),
            title=f"{s['qualname']}: platform branch ({', '.join(sorted(oses))}) fixed {len(fixes)}x, "
                  f"{'no test mentions the platform' if not test_refs else 'CI matrix lacks the OS'}",
            evidence={"platform_lines": [ln.strip()[:100] for ln in lines[:3]], "fix_subjects": [f["subject"] for f in fixes[:4]],
                      "tests_reference_platform": test_refs, "ci_lacks_os": ci_lacks, "linked_tests": _tests_of(ctx, s)[:4]},
            commits=[f["hash"] for f in fixes[:4]], diffs=[(fixes[-1]["hash"], fixes[-1]["path"])],
            paths=[s["path"]], symbols=[s["uid"]]))
    return _top(hits)


# ---------------------------------------------------------------------------- 7. pure_function_roundtrip_property_candidate
_SWAPS = [("encode", "decode"), ("dumps", "loads"), ("dump", "load"), ("serialize", "deserialize"), ("pack", "unpack"),
          ("compress", "decompress"), ("quote", "unquote"), ("escape", "unescape"), ("encrypt", "decrypt"),
          ("marshal", "unmarshal"), ("to_", "from_"), ("parse", "format"), ("parse", "render"), ("parse", "unparse"),
          ("parse", "serialize"), ("stringify", "parse"), ("encode", "parse"), ("to_json", "from_json"),
          ("to_dict", "from_dict")]
_IDEMP_RX = re.compile(r"(?i)^(normali[sz]e|canonical(i[sz]e)?|sanitize|slugify|clean|normpath|simplify)\w*$")
_IMPURE_RX = re.compile(r"\bopen\(|requests\.|httpx|urllib|socket|\btime\.|datetime\.(now|today|utcnow)|\brandom|"
                        r"uuid\.|os\.environ|os\.getenv|subprocess|\bprint\(|\binput\(|sqlite3|\.execute\(|"
                        r"Date\.now|Math\.random|fetch\(|fs\.|process\.env|logging\.|\bglobal\s")
_LOSSY_RX = re.compile(r"(?i)hash|digest|checksum|round|truncate|floor|ceil|lower|upper|summar|abbrev|redact|mask")
_PROP_RX = re.compile(r"hypothesis|@given|fast-check|fc\.assert|fc\.property|jsverify|quickcheck|proptest")


def _pure(ctx: Ctx, uid: str) -> bool:
    seen, frontier = {uid}, [uid]
    for _ in range(2):
        nxt = []
        for u in frontier:
            for v in ctx.callees(u):
                if v not in seen:
                    seen.add(v)
                    nxt.append(v)
        frontier = nxt
    for u in seen:
        s = ctx.symbols.get(u)
        if s is not None and _IMPURE_RX.search(s["body"] or ""):
            return False
    return True


@candidate("pure_function_roundtrip_property_candidate")
def pure_function_roundtrip_property_candidate(ctx: Ctx):
    """Inverse-named pairs (encode/decode, dumps/loads, to_x/from_x, parse/format ...) in the same module/class, plus
    idempotent normalise/sanitize/canonical functions. Both halves must be pure (no I/O, time, random in the call graph
    to depth 2, by body regex), with a typed input, >=1 fix on either half or >=3 callers, and no hypothesis/fast-check
    in linked tests."""
    by_scope: dict[tuple, dict[str, object]] = defaultdict(dict)
    for s in ctx.alive_symbols:
        if _prod(s) and s["kind"] == "function":
            by_scope[(s["path"], s["parent"])][s["name"]] = s
    hits, seen = [], set()

    def annotated(s) -> bool:
        ps = _sig_params_of(s["signature"])
        return bool(ps) and any(a for _, _, a in ps)

    for (path, _parent), names in by_scope.items():
        for n, s in names.items():
            partners = []
            for a, b in _SWAPS:
                if a in n:
                    cand = n.replace(a, b, 1)
                    if cand != n and cand in names:
                        partners.append(names[cand])
            for t in partners:
                key = tuple(sorted((s["uid"], t["uid"])))
                if key in seen:
                    continue
                seen.add(key)
                if _LOSSY_RX.search(s["name"]) or _LOSSY_RX.search(t["name"]):
                    ctx.drop("lossy_conversion")
                    continue
                if is_generated(path):
                    ctx.drop("generated_serializer")
                    continue
                if not (_pure(ctx, s["uid"]) and _pure(ctx, t["uid"])):
                    ctx.drop("impure_network_time_random_io")
                    continue
                fixes = _fix_counts(ctx)[s["uid"]] + _fix_counts(ctx)[t["uid"]]
                callers = len(ctx.callers(s["uid"])) + len(ctx.callers(t["uid"]))
                if fixes < 1 and callers < 3:
                    ctx.drop("no_fix_and_few_callers")
                    continue
                if not (annotated(s) or annotated(t)):
                    ctx.drop("no_input_type_hint")
                    continue
                if _PROP_RX.search(_linked_text(ctx, s) + _linked_text(ctx, t)):
                    ctx.drop("property_test_exists")
                    continue
                hits.append(Hit(
                    subject="|".join(key), score=fixes * 3 + callers * 0.5 + 1,
                    title=f"{s['name']} / {t['name']} look like a round-trip pair; pure, {fixes} fixes, no property test",
                    evidence={"law": "roundtrip", "pair": [s["qualname"], t["qualname"]], "fixes": fixes, "callers": callers,
                              "signatures": [(s["signature"] or "")[:120], (t["signature"] or "")[:120]]},
                    commits=[f["hash"] for u in (s, t) for f in ctx.fixes_by_uid.get(u["uid"], [])[:2]],
                    paths=[path], symbols=list(key)))
            if _IDEMP_RX.match(n) and (s["uid"],) not in seen:
                seen.add((s["uid"],))
                if not _pure(ctx, s["uid"]):
                    ctx.drop("impure_network_time_random_io")
                    continue
                fixes, callers = _fix_counts(ctx)[s["uid"]], len(ctx.callers(s["uid"]))
                if fixes < 1 and callers < 3:
                    ctx.drop("no_fix_and_few_callers")
                    continue
                if not annotated(s):
                    ctx.drop("no_input_type_hint")
                    continue
                if _PROP_RX.search(_linked_text(ctx, s)):
                    ctx.drop("property_test_exists")
                    continue
                hits.append(Hit(
                    subject=s["uid"], score=fixes * 3 + callers * 0.5 + 0.5,
                    title=f"{s['name']} is a pure normaliser; idempotence law f(f(x)) == f(x) untested by property",
                    evidence={"law": "idempotence", "fixes": fixes, "callers": callers, "signature": (s["signature"] or "")[:120]},
                    commits=[f["hash"] for f in ctx.fixes_by_uid.get(s["uid"], [])[:3]], paths=[path], symbols=[s["uid"]]))
    return _top(hits)


# ---------------------------------------------------------------------------- 8. contract_one_side_tested
_COMMON_WORDS = {"default", "string", "number", "object", "boolean", "content", "message", "result", "status", "value",
                 "values", "error", "errors", "success", "failure", "request", "response", "version", "options"}


@candidate("contract_one_side_tested")
def contract_one_side_tested(ctx: Ctx):
    """String contracts (env, config, event, route, endpoint, sql_table, registry, flag, cli_flag) with a producer and
    a consumer in different non-test files, >=3 usages, key >=6 chars, and no test file at HEAD containing the literal
    (the longest static segment for routes). Boost for fix touches on either side and co-change pairs."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in ctx.contracts:
        if not is_test(r["path"]):
            groups[(r["kind"], r["key"])].append(r)
    blob = _test_blob(ctx)
    fix_paths = Counter(f["path"] for f in ctx.fix_events)
    co = {frozenset((r["a"], r["b"])) for r in ctx.cochange}
    hits = []
    for (kind, key), rows in groups.items():
        prod = {r["path"] for r in rows if r["role"] == "producer"}
        cons = {r["path"] for r in rows if r["role"] == "consumer"}
        if not prod or not cons:
            continue
        if len(key) < 4 or key.lower() in _COMMON_WORDS:
            ctx.drop("short_or_common_literal")
            continue
        if len(key) < 6:
            ctx.drop("literal_under_6_chars")
            continue
        if len(rows) < 3:
            continue
        if not (prod - cons or cons - prod) and len(prod | cons) < 2:
            ctx.drop("same_file_both_sides")
            continue
        if re.search(r"\s\S+\s\S+\s", key):
            ctx.drop("log_or_i18n_text")
            continue
        needle = max(key.split("{}"), key=len).strip("/") if kind == "route" else key
        if len(needle) < 4 or needle in blob:
            if len(needle) >= 4:
                ctx.drop("literal_in_tests")
            continue
        fixes = sum(fix_paths[p] for p in prod | cons)
        cochange = any(frozenset((a, b)) in co for a in prod for b in cons)
        langs = {p.rsplit(".", 1)[-1] for p in prod | cons}
        hits.append(Hit(
            subject=f"{kind}|{key}", score=len(rows) + 4 * min(fixes, 5) + (5 if cochange else 0) + (3 if len(langs) > 1 else 0),
            title=f"{kind} contract '{key}' ({len(rows)} usages) has no test mentioning it",
            evidence={"kind": kind, "producers": sorted(prod)[:4], "consumers": sorted(cons)[:4], "usages": len(rows),
                      "fixes_on_sides": fixes, "cochange_pair": cochange},
            paths=sorted(prod | cons)[:6],
            commits=[f["hash"] for f in ctx.fix_events if f["path"] in (prod | cons)][:4],
            symbols=[r["symbol"] for r in rows if r["symbol"]][:4]))
    return _top(hits)


# ---------------------------------------------------------------------------- 9. mock_the_subject
@candidate("mock_the_subject")
def mock_the_subject(ctx: Ctx):
    """Symbols whose every (static/coverage) linked test patches the symbol itself or its module
    (mock.patch / monkeypatch.setattr / patch.object / jest.mock / spyOn). Resolved by dotted-module lookup against
    HEAD files. With per-test coverage (``covered_line_tests``, file measured) the claim is measured directly: for each
    patching test, ``ctx.lines_run_by(test, S.path)`` is intersected with S's body lines (def line excluded); the symbol
    is dropped when any patching test executed a line of S (``patching_test_executes_subject``) and is a hit only when
    none did (evidence ``measured_per_test``; tests with no recorded lines are "unknown" and defer to the run-level
    rule below). Fallback (no per-test data): the patch-text proxy, plus the run-level rule: a symbol whose body has
    any executed line is dropped (``measured_executed``) and the measured fraction is reported."""
    hits = []
    for s in ctx.alive_symbols:
        if not _prod(s):
            continue
        rows = [r for r in ctx.tests_for.get(s["uid"], []) if r["source"] in ("static", "coverage")]
        tids = sorted({r["test"] for r in rows})
        real = [t for t in tids if not t.split("::")[0].endswith("conftest.py")]
        if len(real) < len(tids):
            ctx.drop("conftest_mock")
        if not real:
            continue
        mocking, autospec, infra, order = 0, False, False, False
        raws, mockers = [], []
        for t in real:
            ps = [p for p in _test_patches(ctx, t) if s["uid"] in p["uids"] or s["path"] in p["modules"]]
            if any(p["autospec"] for p in ps):
                autospec = True
                continue
            ps = [p for p in ps if not p["autospec"]]
            if ps:
                if all(_INFRA_RX.search(".".join(p["raw"].split(".")[-2:])) and s["name"] not in p["raw"] for p in ps):
                    infra = True
                    continue
                mocking += 1
                mockers.append(t)
                raws.append(ps[0]["raw"])
                if re.search(r"assert_has_calls|call_args_list|mock_calls", _test_text(ctx, t)):
                    order = True
        if mocking == 0:
            continue
        if mocking < len(real):
            continue
        if autospec:
            ctx.drop("autospec_contract_test")
            continue
        if infra:
            ctx.drop("infra_target")
            continue
        if order:
            ctx.drop("call_order_is_the_contract")
            continue
        mcov = _measured_cov(ctx, s["uid"])
        per_test = None
        if _per_test_cov(ctx, s["path"]) and s["start_line"]:
            body = range(s["start_line"] + 1, (s["end_line"] or s["start_line"]) + 1)
            known = {t: ctx.lines_run_by(t) for t in mockers}
            known = {t: v for t, v in known.items() if v}
            if known:
                ran = [t for t in known if any(ln in body for _, ln in ctx.lines_run_by(t, s["path"]))]
                if ran:
                    ctx.drop("patching_test_executes_subject")
                    continue
                if len(known) == len(mockers):
                    per_test = {"patching_tests": len(mockers), "executing_subject": 0}
        if per_test is None and mcov is not None and mcov > 0:
            ctx.drop("measured_executed")
            continue
        callers, nfix = len(ctx.callers(s["uid"])), _fix_counts(ctx)[s["uid"]]
        hits.append(Hit(
            subject=s["uid"], score=mocking + callers * 0.5 + nfix * 2 + (2 if mcov == 0 or per_test else 0),
            title=f"{s['qualname']}: all {mocking} linked test(s) patch the symbol they are linked to",
            evidence={"patch_targets": raws[:4], "linked_tests": real[:5], "callers": callers, "fixes": nfix,
                      "measured_coverage": mcov, "measured_per_test": per_test},
            paths=[s["path"], real[0].split("::")[0]], symbols=[s["uid"]],
            commits=[f["hash"] for f in ctx.fixes_by_uid.get(s["uid"], [])[:3]]))
    return _top(hits)


# ---------------------------------------------------------------------------- 10. declared_domain_gap
_ENUM_BASE_RX = re.compile(r"\((?:[\w.]*\.)?(?:Enum|IntEnum|StrEnum|Flag|IntFlag)\b")
_NOT_BOOKKEEPING = {"verbose", "debug", "quiet", "silent", "logger", "log", "logging", "log_level", "echo"}
_DI_NAMES = {"clock", "now", "now_fn", "sleep", "sleep_fn", "time_fn", "factory", "client", "session", "opener", "fs",
             "transport", "timer", "rng", "random_fn"}


@candidate("declared_domain_gap")
def declared_domain_gap(ctx: Ctx):
    """(1) Enum members / Literal arms added after the declaring class/alias existed whose name or value appears in no
    test file at HEAD (members also need a reference in importing production code, else they are labels only).
    (2) Defaulted/Optional parameters added or changed after the linked tests were last touched, used in a body
    condition, passed by keyword by a non-test caller, and never given as a keyword in any linked test. Part (2) dates
    the parameter change from ``symbol_versions`` signature history (exact version, multi-line signatures included;
    diff_lines ``def`` lines only for files with no recorded versions)."""
    blob = _test_blob(ctx)
    hits = []
    # (1a) enums
    enum_syms = [s for s in ctx.alive_symbols if s["kind"] == "class" and _prod(s)
                 and _ENUM_BASE_RX.search(s["signature"] or (s["body"] or "").split("\n", 1)[0])]
    if enum_syms:
        rx_paths = "^(" + "|".join(re.escape(p) for p in {s["path"] for s in enum_syms}) + ")$"
        rows = ctx.grep(r"^\s+[A-Za-z_]\w*\s*=", "+", path_regex=rx_paths)
        for s in enum_syms:
            members = {m.group(1): m.group(0) for m in re.finditer(r"^\s+([A-Za-z_]\w*)\s*=\s*(.+)$", s["body"] or "", re.M)}
            for r in rows:
                if r["path"] != s["path"] or r["seq"] <= (s["first_seq"] or 0):
                    continue
                m = re.match(r"^\s+([A-Za-z_]\w*)\s*=\s*(.+)$", r["text"])
                if not m or m.group(1) not in members or m.group(1).startswith("_"):
                    continue
                if ctx.head_ts - r["ts"] > 540 * DAY:
                    ctx.drop("not_recent")
                    continue
                name, val = m.group(1), m.group(2).strip().strip("\"'")
                if re.search(rf"\b{re.escape(name)}\b", blob) or (len(val) > 2 and re.search(rf"[\"']{re.escape(val)}[\"']", blob)):
                    continue
                refs = 0
                for p in {s["path"], *ctx.imports[1].get(s["path"], set())}:
                    if is_test(p):
                        continue
                    refs += len(re.findall(rf"\.{re.escape(name)}\b", ctx.text_at_head(p) or ""))
                if refs == 0:
                    ctx.drop("enum_member_label_only")
                    continue
                nfix = _fix_counts(ctx)[s["uid"]]
                hits.append(Hit(
                    subject=f"{s['uid']}|{name}", score=3 + nfix * 2 + len(ctx.callers(s["uid"])) * 0.3 + refs * 0.2,
                    title=f"enum member {s['name']}.{name} added, no test mentions it",
                    evidence={"enum": s["qualname"], "member": name, "value": val[:40], "references_in_code": refs,
                              "added_in": subject_of(ctx.commit(r["hash"])["message"])},
                    commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"]], symbols=[s["uid"]]))
    # (1b) Literal arms
    lit_rows = ctx.grep(r"Literal\[", None, path_regex=r"\.pyi?$")
    grp: dict[tuple, dict] = defaultdict(lambda: {"+": [], "-": []})
    for r in lit_rows:
        if not is_test(r["path"]):
            grp[(r["hash"], r["path"])][r["sign"]].append(r)
    for (h, path), d in grp.items():
        if not d["-"] or not d["+"]:
            if d["+"]:
                ctx.drop("new_literal_definition")
            continue
        if ctx.head_ts - d["+"][0]["ts"] > 540 * DAY:
            ctx.drop("not_recent")
            continue
        old = set(re.findall(r"[\"']([^\"']{2,})[\"']", " ".join(r["text"] for r in d["-"])))
        new = set(re.findall(r"[\"']([^\"']{2,})[\"']", " ".join(r["text"] for r in d["+"]))) - old
        for arm in sorted(new):
            if re.search(rf"[\"']{re.escape(arm)}[\"']", blob):
                continue
            hits.append(Hit(subject=f"{path}|literal|{arm}", score=2.5,
                            title=f"Literal arm '{arm}' added in {path}, absent from every test",
                            evidence={"arm": arm, "line": d["+"][0]["text"].strip()[:140]},
                            commits=[h], diffs=[(h, path)], paths=[path]))
    # (2) defaulted / optional params
    changes = defaultdict(list)
    for e in _sig_changes(ctx):
        changes[(e["path"], e["name"])].append(e)
    for s in ctx.alive_symbols:
        if not _prod(s) or s["kind"] != "function" or (s["path"], s["name"]) not in changes:
            continue
        params = [(n, d, a) for n, d, a in _sig_params_of(s["signature"])
                  if d is not None or re.search(r"Optional|\bNone\b|\?", a or "")]
        if not params:
            continue
        body_lines = (s["body"] or "").splitlines()[1:]
        tests = _tests_of(ctx, s)
        if not tests:
            continue
        text = _linked_text(ctx, s)
        test_ts = max((ctx.by_hash[r["hash"]]["ts"] for t in tests for r in ctx.changes_by_path.get(t.split("::")[0], [])
                       if r["hash"] in ctx.by_hash), default=0)
        for n, d, a in params:
            if n in _NOT_BOOKKEEPING:
                ctx.drop("logging_flag")
                continue
            if n in _DI_NAMES:
                ctx.drop("di_only_param")
                continue
            cond = [ln for ln in body_lines if re.search(rf"\b{re.escape(n)}\b", ln)
                    and re.search(r"\b(if|elif|while|and|or|not|is|assert)\b|==|!=|[<>]", ln)]
            if not cond:
                ctx.drop("pass_through_param")
                continue
            if re.search(rf"^\s*(if\s+{re.escape(n)}\s+is\s+None\s*:\s*{re.escape(n)}\s*=|{re.escape(n)}\s*=\s*{re.escape(n)}\s+(or|if)\b)",
                         "\n".join(body_lines[:3]), re.M):
                ctx.drop("normalised_on_first_line")
                continue
            ev = [e for e in changes[(s["path"], s["name"])]
                  if n in [x[0] for x in e["new"]] and (n not in [x[0] for x in e["old"]] or
                                                          dict((x[0], x[1]) for x in e["old"]).get(n) != d)]
            if not ev or ev[-1]["ts"] <= test_ts:
                ctx.drop("param_not_newer_than_tests")
                continue
            kw = [cu for cu in ctx.callers(s["uid"]) if cu in ctx.symbols and not is_test(ctx.symbols[cu]["path"])
                  and re.search(rf"\b{re.escape(s['name'])}\s*\([^)]*\b{re.escape(n)}\s*=[^=]", ctx.symbols[cu]["body"] or "", re.S)]
            if not kw:
                ctx.drop("no_caller_passes_it")
                continue
            if re.search(rf"\b{re.escape(n)}\s*=[^=]", text):
                ctx.drop("keyword_in_linked_test")
                continue
            nfix = _fix_counts(ctx)[s["uid"]]
            hits.append(Hit(
                subject=f"{s['uid']}|param|{n}", score=2 + nfix * 2 + len(ctx.callers(s["uid"])) * 0.3,
                title=f"{s['qualname']}({n}=...) introduced after tests last changed; no test passes {n}",
                evidence={"param": n, "default": d, "branch_lines": [ln.strip()[:100] for ln in cond[:2]],
                          "callers_passing_it": kw[:3], "linked_tests": tests[:4], "fixes": nfix},
                commits=[ev[-1]["hash"]], diffs=[(ev[-1]["hash"], s["path"])], paths=[s["path"]], symbols=[s["uid"]]))
    return _top(hits)


# ---------------------------------------------------------------------------- 11. composition_root_untested
_REG_RX = (r"\bregister\w*\(|add_url_rule\(|register_blueprint\(|add_command\(|add_route\(|include_router\(|\.mount\(|"
           r"\bapp\.use\(|router\.(add|use|include)|INSTALLED_APPS|urlpatterns|\bre_path\(|\bpath\(|REGISTRY\[|registry\[|"
           r"HANDLERS\[|handlers\[|PLUGINS|plugins\[|\.add_handler\(|add_middleware\(|MIDDLEWARE|@\w+\.register|"
           r"\.connect\(|add_argument_group|subparsers\.add_parser\(")
_REG_STOP = {"self", "cls", "app", "register", "path", "name", "True", "False", "None", "str", "int", "len", "list", "dict",
             "add_url_rule", "register_blueprint", "add_command", "url_prefix", "include_router", "router", "use", "add",
             "import", "from", "return", "lambda", "include", "mount", "include_router", "handlers", "plugins"}
_ITER_RX = re.compile(r"for\s+\w+(?:\s*,\s*\w+)*\s+in\s+[\w.]*(?:registry|REGISTRY|ROUTES|routes|urlpatterns|plugins|"
                      r"handlers|blueprints|commands|url_map|apps|MIDDLEWARE)|iter_rules|url_map|\.items\(\)|\.values\(\)|"
                      r"parametrize|registered|list_routes|get_commands")


@candidate("composition_root_untested")
def composition_root_untested(ctx: Ctx):
    """Files with >=2 follow-up wiring commits (registration idioms in the added lines, in a fix commit or a diff of
    <=5 lines in that file) that register a symbol created in an earlier commit, and no test that imports the file and
    iterates its entries. Skips the 'unregistered at HEAD' check."""
    first_seq: dict[str, int] = {}
    for s in ctx.symbols.values():
        if s["first_seq"] is not None and not is_test(s["path"]):
            first_seq[s["name"]] = min(first_seq.get(s["name"], 10**9), s["first_seq"])
    fix_hashes = {f["hash"] for f in ctx.fix_events}
    added_commit = {(r["hash"], r["path"]) for rows in ctx.changes_by_commit.values() for r in rows if r["status"] == "A"}
    per_file: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in ctx.grep(_REG_RX, "+"):
        path, h = r["path"], r["hash"]
        if is_test(path) or not is_code(path) or is_vendored(path):
            continue
        if (h, path) in added_commit:
            ctx.drop("initial_scaffolding")
            continue
        if h in per_file[path]:
            per_file[path][h]["lines"].append(r["text"].strip())
            continue
        ids = [i for i in re.findall(r"[A-Za-z_]\w{2,}", r["text"]) if i not in _REG_STOP and first_seq.get(i, 10**9) < r["seq"]]
        if not ids:
            continue
        n = ctx.q1("SELECT count(*) AS n FROM diff_lines WHERE hash=? AND path=?", (h, path))["n"]
        if n > 5 and h not in fix_hashes:
            ctx.drop("large_non_fix_diff")
            continue
        per_file[path][h] = {"lines": [r["text"].strip()], "ids": ids[:3]}
    hits = []
    for path, d in per_file.items():
        if len(d) < 2:
            ctx.drop("fewer_than_two_followups")
            continue
        if is_generated(path):
            ctx.drop("generated_registry")
            continue
        importers = [t for t in ctx.imports[1].get(path, set()) if is_test(t)]
        if any(_ITER_RX.search(_test_file_text(ctx, t)) for t in importers):
            ctx.drop("registry_iterated_by_test")
            continue
        hs = sorted(d, key=lambda h: ctx.by_hash[h]["seq"])
        hits.append(Hit(
            subject=path, score=len(d) * 2 + (2 if not importers else 0),
            title=f"{path}: {len(d)} wiring follow-ups registering earlier symbols; "
                  f"{'no test imports it' if not importers else 'tests import it but never iterate entries'}",
            evidence={"followups": [{"hash": h[:10], "subject": subject_of(ctx.by_hash[h]["message"]), "line": d[h]["lines"][0][:100],
                                     "registered": d[h]["ids"]} for h in hs[:6]],
                      "test_importers": importers[:4]},
            commits=hs[:6], diffs=[(h, path) for h in hs[:3]], paths=[path]))
    return _top(hits)


# ---------------------------------------------------------------------------- 12. mock_target_signature_drift
@candidate("mock_target_signature_drift")
def mock_target_signature_drift(ctx: Ctx):
    """Full-string mock.patch / monkeypatch.setattr targets in test files at HEAD that resolve to a symbol whose
    parameter list (names/defaults, not annotations) changed in history, while the patching test file was not touched
    within 14 days after the change. Boost: no autospec; a later fix touching a caller. Parameter-list changes come
    from ``symbol_versions`` signature history (multi-line signatures included; diff_lines ``def`` lines only for files
    with no recorded versions)."""
    changes = defaultdict(list)
    for e in _sig_changes(ctx):
        if e["params_changed"]:
            changes[(e["path"], e["name"])].append(e)
        else:
            ctx.drop("annotation_only_change")
    tpaths = sorted(p for p in ctx.head_files if is_test(p) and p.endswith(".py"))
    hits = []
    for tp in tpaths:
        touches = [ctx.by_hash[r["hash"]]["ts"] for r in ctx.changes_by_path.get(tp, []) if r["hash"] in ctx.by_hash]
        for p in _patches_in(ctx, "file:" + tp, _test_file_text(ctx, tp)):
            if not p["full_string"]:
                continue
            for uid in p["uids"]:
                s = ctx.symbols.get(uid)
                if s is None or s["kind"] != "function":
                    continue
                for e in changes.get((s["path"], s["name"]), []):
                    if any(e["ts"] <= t <= e["ts"] + 14 * DAY for t in touches):
                        ctx.drop("test_touched_within_14d")
                        continue
                    later_fix = [f for f in ctx.fix_events if f["ts"] > e["ts"] and f["uid"] in ctx.callers(uid)]
                    hits.append(Hit(
                        subject=f"{tp}|{uid}|{e['hash']}",
                        score=2 + (2 if not p["autospec"] else 0) + (3 if later_fix else 0),
                        title=f"{tp} patches '{p['raw']}' but {s['name']}() params changed afterwards",
                        evidence={"patch_target": p["raw"], "autospec": p["autospec"], "old_signature": e["old_line"][:140],
                                  "new_signature": e["new_line"][:140], "test_touched_after_change": False,
                                  "later_caller_fixes": [f["subject"] for f in later_fix[:3]]},
                        commits=[e["hash"], *[f["hash"] for f in later_fix[:2]]], diffs=[(e["hash"], s["path"])],
                        paths=[tp, s["path"]], symbols=[uid]))
    return _top(hits)


# ---------------------------------------------------------------------------- 13. snapshot_blind_update
_SNAP_RX = re.compile(r"(?i)__snapshots__/|\.snap$|\.ambr$|(^|/)snapshots?/|\.approved\.|\.golden\b|(^|/)golden/")
_SNAP_REASON_RX = re.compile(r"(?i)snapshot|\bsnap\b|golden|baseline|regenerat|expected output|intentional|redesign|"
                             r"re-?record|update[sd]? (the )?(tests?|fixtures)")


@candidate("snapshot_blind_update")
def snapshot_blind_update(ctx: Ctx):
    """Main-line commits with production changes plus >=10 snapshot files or >=300 snapshot lines changed, whose
    message gives no reason, and where a fix hit the same production symbols within 30 days afterwards."""
    hits = []
    snap_by: dict[str, list] = defaultdict(list)   # includes ignored rows: snapshot files are often flagged ignored/binary
    for fc in ctx.q("SELECT hash, path, added, deleted FROM file_changes WHERE path LIKE '%snapshot%' "
                    "OR path LIKE '%.snap' OR path LIKE '%.ambr' OR path LIKE '%snapshot/%' OR path LIKE '%snapshots/%' "
                    "OR path LIKE '%.approved.%' OR path LIKE '%.golden%' OR path LIKE '%/golden/%'"):
        if _SNAP_RX.search(fc["path"]):
            snap_by[fc["hash"]].append(fc)
    for c in ctx.main_commits:
        snaps = snap_by.get(c["hash"], [])
        if not snaps:
            continue
        chs = ctx.changes(c["hash"])
        n_lines = sum((fc["added"] or 0) + (fc["deleted"] or 0) for fc in snaps)
        if len(snaps) < 10 and n_lines < 300:
            continue
        prod = [fc for fc in chs if is_code(fc["path"]) and not is_test(fc["path"]) and not _SNAP_RX.search(fc["path"])]
        if not prod:
            ctx.drop("snapshot_only_commit")
            continue
        if c["is_merge"] or len([fc for fc in chs if not _SNAP_RX.search(fc["path"])]) > 30:
            ctx.drop("bulk_or_merge")
            continue
        if _SNAP_REASON_RX.search(c["message"] or ""):
            ctx.drop("message_explains_snapshots")
            continue
        uids = {e["uid"] for e in ctx.events_by_commit.get(c["hash"], []) if e["uid"] in ctx.symbols
                and not is_test(ctx.symbols[e["uid"]]["path"])}
        later = [f for u in uids for f in ctx.fixes_by_uid.get(u, [])
                 if c["ts"] < f["ts"] <= c["ts"] + 30 * DAY and f["hash"] != c["hash"]]
        if not later:
            ctx.drop("no_followup_fix_on_same_symbols")
            continue
        top = sorted(prod, key=lambda fc: -((fc["added"] or 0) + (fc["deleted"] or 0)))[:5]
        hits.append(Hit(
            subject=c["hash"], score=len(later) * 2 + len(snaps) * 0.2 + n_lines / 300,
            title=f"{subject_of(c['message'])[:70]!r}: {len(snaps)} snapshot files ({n_lines} lines) changed with code, then fixes",
            evidence={"snapshot_files": len(snaps), "snapshot_lines": n_lines, "production_files": [fc["path"] for fc in top],
                      "followup_fixes": [{"hash": f["hash"][:10], "subject": f["subject"], "uid": f["uid"]} for f in later[:5]]},
            commits=[c["hash"], *dict.fromkeys(f["hash"] for f in later[:4])],
            diffs=[(c["hash"], fc["path"]) for fc in top], paths=[fc["path"] for fc in top],
            symbols=sorted({f["uid"] for f in later})[:5]))
    return _top(hits)


# ---------------------------------------------------------------------------- 14. platform_text_path_class_gap
_TP_LINE_RX = (r"encoding\s*=|errors\s*=|newline\s*=|\.normalize\(|replace\(\s*[\"']\\\\+[\"']\s*,\s*[\"']/[\"']|normpath|"
               r"as_posix|\\r|utf-8-sig|\bBOM\b")
_TP_CLASSES = {
    "non_ascii": (re.compile(r"encoding\s*=|errors\s*=|\.normalize\("), re.compile(r"[^\x00-\x7f]")),
    "crlf": (re.compile(r"newline\s*=|\\r"), re.compile(r"\\r|\r")),
    "bom": (re.compile(r"utf-8-sig|\bBOM\b"), re.compile(r"\ufeff|\\xef\\xbb\\xbf|utf-8-sig|\bBOM\b")),
    "backslash_path": (re.compile(r"replace\(\s*[\"']\\\\+[\"']\s*,\s*[\"']/[\"']|normpath|as_posix"),
                       re.compile(r"[A-Za-z]:\\\\|\\\\\\\\|\.\.\\\\|\w\\\\\w|ntpath|PureWindowsPath|WindowsPath")),
}
_BINARY_RX = re.compile(r"[\"']r?b[\"']|\bwb\b|\brb\b")


def _fixture_bytes_flags(ctx: Ctx) -> dict[str, bool]:
    """Class flags over non-code fixture/data files in test directories (bytes scan, capped)."""
    c = _cache(ctx)
    if "fixture_flags" in c:
        return c["fixture_flags"]
    flags = {"non_ascii": False, "crlf": False, "bom": False, "backslash_path": False}
    n = 0
    for p, oid in ctx.head_files.items():
        if not is_test(p) or p.endswith((".py", ".js", ".ts", ".tsx", ".jsx")):
            continue
        n += 1
        if n > 300:
            break
        try:
            blob = ctx.repo.get(oid)
            data = blob.data[:200_000] if blob is not None and hasattr(blob, "data") else b""
        except Exception:
            continue
        flags["non_ascii"] |= any(b > 127 for b in data)
        flags["crlf"] |= b"\r" in data
        flags["bom"] |= data.startswith(b"\xef\xbb\xbf")
        flags["backslash_path"] |= bool(re.search(rb"[A-Za-z]:\\|\.\.\\", data))
    c["fixture_flags"] = flags
    return flags


@candidate("platform_text_path_class_gap")
def platform_text_path_class_gap(ctx: Ctx):
    """Fix commits whose added lines handle encoding/newline/BOM/backslash-path input, where neither the linked tests
    nor fixture files in test directories contain that class (non-ASCII, \\r, BOM, backslash path). Measured path (file
    measured with per-test coverage): the added line is located in the HEAD body, ``ctx.tests_covering`` gives the
    tests that really execute it, and the symbol is dropped only if one of those tests' source feeds the class (a
    literal of the class, or, for fixture-borne classes, a test that reads files while fixtures carry the class);
    no covering test = hit (``measured`` evidence). Fallback (not measured / line not located): the linked-test text +
    fixture bytes scan. The function is the one ``diff_lines.symbol`` attributes the added line to (exact, even
    if the line was edited later); lines with no recorded symbol fall back to "the line appears in the HEAD body of a
    symbol the fix event touched"."""
    rows = ctx.grep(_TP_LINE_RX, "+")
    fix_by = defaultdict(list)
    for f in ctx.fix_events:
        fix_by[(f["hash"], f["path"])].append(f)
    gitattr = (ctx.text_at_head(".gitattributes") or "")
    eol_rule = bool(re.search(r"\beol=|text=auto|\btext\b", gitattr))
    fixture = _fixture_bytes_flags(ctx)
    agg: dict[tuple, dict] = {}
    for r in rows:
        path = r["path"]
        if is_test(path) or not is_code(path):
            continue
        fs = fix_by.get((r["hash"], path))
        if not fs:
            continue
        line = r["text"].strip()
        if _BINARY_RX.search(line) and not re.search(r"encoding", line):
            ctx.drop("binary_handling")
            continue
        sym_uid = r["symbol"]
        for f in fs:
            if sym_uid and f["uid"] != sym_uid:
                continue
            s = ctx.symbols.get(f["uid"])
            if s is None or not s["alive"] or (not sym_uid and line not in (s["body"] or "")):
                continue
            for cl, (rx, _) in _TP_CLASSES.items():
                if rx.search(line):
                    a = agg.setdefault((s["uid"], cl), {"s": s, "lines": [], "fixes": []})
                    a["lines"].append(line[:120])
                    a["fixes"].append(f)
    hits = []
    for (uid, cl), a in agg.items():
        s = a["s"]
        body = s["body"] or ""
        if re.search(r"(?i)markdown|\blog(ger)?\.|render|\.md$", s["path"] + " " + a["lines"][0]) and cl != "non_ascii":
            ctx.drop("logging_or_markdown")
            continue
        if re.search(r"(?i)urlparse|urljoin|urlsplit|\burl\b", body[:400]) and cl == "backslash_path":
            ctx.drop("url_handling")
            continue
        if cl == "crlf" and eol_rule:
            ctx.drop("gitattributes_eol_rule")
            continue
        if cl == "backslash_path" and "Path" in (s["signature"] or "") and not re.search(r"\bstr\(", body):
            ctx.drop("pathlib_only_param")
            continue
        tests = _tests_of(ctx, s)
        covering = None
        if _per_test_cov(ctx, s["path"]):
            ll = _locate_lines(ctx, s, a["lines"])
            if ll:
                covering = _covering_tests(ctx, s["path"], ll)
        if covering is not None:
            ctext = "\n".join(_test_text(ctx, t) for t in sorted(covering))
            if _TP_CLASSES[cl][1].search(ctext) or (fixture[cl] and re.search(
                    r"read_text|read_bytes|\bopen\(|fixture|data/|\.json|\.txt|\.csv", ctext)):
                ctx.drop("class_present_in_covering_tests")
                continue
        else:
            text = _linked_text(ctx, s)
            if _TP_CLASSES[cl][1].search(text) or fixture[cl]:
                ctx.drop("class_present_in_tests_or_fixtures")
                continue
        nfix = len(set(f["hash"] for f in a["fixes"]))
        sites = len(re.findall(r"\bopen\(|read_text|read_bytes|\.read\(", body))
        hits.append(Hit(
            subject=f"{uid}|{cl}", score=(nfix * 2 + sites) * (1 if tests else 0.5),
            title=f"{s['qualname']}: fix added {cl} handling; no test or fixture exercises that input class",
            evidence={"class": cl, "lines": list(dict.fromkeys(a["lines"]))[:4], "fix_subjects": [f["subject"] for f in a["fixes"][:3]],
                      "file_read_sites": sites, "linked_tests": tests[:4],
                      "measured": covering is not None,
                      "covering_tests": sorted(covering)[:5] if covering is not None else None},
            commits=list(dict.fromkeys(f["hash"] for f in a["fixes"]))[:4],
            diffs=[(a["fixes"][-1]["hash"], s["path"])], paths=[s["path"]], symbols=[uid]))
    return _top(hits)


# ---------------------------------------------------------------------------- 15. strictness_relaxation
_STRICT_PATH_RX = (r"(?i)(tsconfig[^/]*\.json|mypy\.ini|setup\.cfg|pyproject\.toml|tox\.ini|\.eslintrc[^/]*|"
                   r"eslint\.config\.[cm]?js|\.flake8|ruff\.toml|\.pylintrc|Makefile|CMakeLists\.txt|"
                   r"\.github/workflows/[^/]+\.ya?ml|pytest\.ini|\.bandit|\.golangci\.ya?ml|pyrightconfig\.json)$")
_STRICT_KEYS_TRUE = {"strict", "disallow_untyped_defs", "disallow_any_generics", "disallow_incomplete_defs", "check_untyped_defs",
                     "warn_unused_ignores", "warn_return_any", "no_implicit_optional", "noimplicitany", "strictnullchecks",
                     "noimplicitreturns", "strictfunctiontypes", "noimplicitthis", "nouncheckedindexedaccess",
                     "disallow_untyped_calls", "warn_redundant_casts", "strictbindcallapply", "disallow_subclassing_any"}
_LOOSE_KEYS_TRUE = {"ignore_errors", "ignore_missing_imports", "allow_untyped_defs", "allow_untyped_calls", "allowjs",
                    "skiplibcheck", "allow_redefinition", "follow_imports_skip"}
_STYLE_RX = re.compile(r"(?i)line[-_]length|max-?line|quotes|indent|semi|format|trailing|whitespace|prettier|comma|spacing|"
                       r"naming|docstring|import-order|sort")
_SUPPRESS_RX = r"#\s*type:\s*ignore|#\s*noqa|#\s*nosec|eslint-disable|@ts-ignore|@ts-expect-error|#\s*pylint:\s*disable|#\s*pyright:\s*ignore|NOSONAR|//\s*nolint"
_SEC_SUPPRESS_RX = re.compile(r"nosec|noqa:\s*S\d{3}|noqa:.*\bB\d{3}\b|eslint-disable[^\n]*security|nolint:\s*gosec|NOSONAR")


def _kv(text: str):
    m = re.match(r"""^\s*["']?([\w.-]+)["']?\s*[:=]\s*["']?([^"',#\s]+)["']?\s*,?\s*(?:#.*)?$""", text)
    return (m.group(1).lower().replace("-", "_"), m.group(2).lower()) if m else None


@candidate("strictness_relaxation")
def strictness_relaxation(ctx: Ctx):
    """(a) Config loosening from before/after line pairs in tsconfig/mypy/pyproject/eslint/CI/Makefile: strict-type keys
    flipped to false, ignore_errors-style keys flipped to true, -Werror removed or -Wno-error added, eslint rules turned
    off, mypy override blocks and exclude lists added. (b) one commit adding >=20 suppression markers, or any security
    suppression (nosec, noqa: S###, B###). Initial config commits, vendored/test files and style rules are dropped."""
    hits = []
    created = {(r["hash"], r["path"]) for rows in ctx.changes_by_commit.values() for r in rows if r["status"] == "A"}
    rows = ctx.grep(r".", None, path_regex=_STRICT_PATH_RX)
    d: dict[tuple, dict] = defaultdict(lambda: {"+": [], "-": []})
    for r in rows:
        if is_vendored(r["path"]) or is_test(r["path"]):
            continue
        d[(r["hash"], r["path"])][r["sign"]].append(r["text"])
    for (h, path), sides in d.items():
        if (h, path) in created:
            ctx.drop("initial_config")
            continue
        old = {k: v for k, v in filter(None, map(_kv, sides["-"]))}
        new = {k: v for k, v in filter(None, map(_kv, sides["+"]))}
        events = []
        for k, v in new.items():
            ov = old.get(k)
            if k in _STRICT_KEYS_TRUE and v in ("false", "0", "no", "off") and ov in ("true", "1", "yes", "on"):
                events.append((k, f"{ov} -> {v}"))
            elif k in _LOOSE_KEYS_TRUE and v in ("true", "1", "yes", "on") and ov not in ("true", "1", "yes", "on"):
                events.append((k, f"{ov} -> {v}"))
            elif v in ("off", "0") and ov in ("error", "warn", "2", "1") and not path.endswith(".py"):
                if _STYLE_RX.search(k):
                    ctx.drop("style_rule")
                else:
                    events.append((k, f"{ov} -> {v}"))
            elif k == "filterwarnings":
                pass
        minus, plus = "\n".join(sides["-"]), "\n".join(sides["+"])
        if re.search(r"-Werror\b", minus) and not re.search(r"-Werror\b", plus):
            events.append(("-Werror", "removed"))
        if re.search(r"-Wno-error|--no-warn", plus) and not re.search(r"-Wno-error", minus):
            events.append(("-Wno-error", "added"))
        if re.search(r"""["']?error["']?\s*$|"error"|\berror\b""", minus) and re.search(r"filterwarnings|-W", minus + plus) \
                and re.search(r"\b(ignore|default)\b", plus) and "filterwarnings" in (minus + plus):
            events.append(("filterwarnings", "error -> ignore/default"))
        if re.search(r"\[\[tool\.mypy\.overrides\]\]", plus) and "overrides" not in minus:
            events.append(("mypy.overrides", "override block added"))
        if re.search(r"(?i)\b(per-file-ignores|extend-ignore|extend_ignore)\b", plus):
            events.append(("lint ignore list", "ignore list extended"))
        if not events:
            continue
        c = ctx.commit(h)
        hits.append(Hit(
            subject=f"{h}|{path}|{events[0][0]}", score=2 + len(events) * 1.5 + (2 if re.search(r"(?i)strict|mypy|werror|tsconfig", path + events[0][0]) else 0),
            title=f"{path}: strictness loosened ({', '.join(e[0] for e in events[:3])}) — {subject_of(c['message'])[:60]}",
            evidence={"changes": [{"key": k, "change": v} for k, v in events[:6]], "commit_subject": subject_of(c["message"])},
            commits=[h], diffs=[(h, path)], paths=[path]))
    # (b) suppression waves
    per_commit: dict[str, list] = defaultdict(list)
    for r in ctx.grep(_SUPPRESS_RX, "+"):
        if is_test(r["path"]) or is_vendored(r["path"]) or is_generated(r["path"]) or is_docs(r["path"]):
            continue
        per_commit[r["hash"]].append(r)
    for h, rs in per_commit.items():
        sec = [r for r in rs if _SEC_SUPPRESS_RX.search(r["text"])]
        if len(rs) < 20 and not sec:
            continue
        c = ctx.commit(h)
        hits.append(Hit(
            subject=f"{h}|suppress", score=2 + len(rs) / 10 + 4 * min(len(sec), 3),
            title=f"{len(rs)} suppression markers added in one commit" + (f", {len(sec)} security" if sec else ""),
            evidence={"markers": len(rs), "security_markers": [r["text"].strip()[:90] for r in sec[:4]],
                      "files": sorted({r["path"] for r in rs})[:6], "commit_subject": subject_of(c["message"])},
            commits=[h], diffs=[(h, (sec or rs)[0]["path"])], paths=sorted({r["path"] for r in rs})[:6]))
    return _top(hits)


# ---------------------------------------------------------------------------- 16. test_erosion_changepoint
def _quarter(ts: int) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.year * 4 + (d.month - 1) // 3


def _test_tokens(path: str) -> set[str]:
    toks = set()
    for seg in path.lower().split("/"):
        seg = seg.rsplit(".", 1)[0]
        seg = re.sub(r"^(test_|tests_)|(_test|_tests|\.test|\.spec)$", "", seg)
        if seg not in ("tests", "test", "__tests__", "spec", "specs", "", "conftest"):
            toks.add(seg)
    return toks


@candidate("test_erosion_changepoint")
def test_erosion_changepoint(ctx: Ctx):
    """Per top-level module and quarter: share of non-bulk production commits that edit a test file mapped to that
    module (by name) or a generic test file that maps to no module. Emit when the share falls >=0.3 between consecutive
    4-quarter windows with >=10 commits each. Dropped when many test files were renamed in the later window. Docs and
    config-only commits are ignored."""
    mods = {_pkg(p).split("/")[-1].lower() for p in ctx.head_files if is_code(p) and not is_test(p)}
    per: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0, []]))
    renames: dict[int, int] = Counter()
    for c in ctx.main_commits:
        if c["is_bulk"] or c["is_merge"]:
            continue
        chs = ctx.changes(c["hash"])
        q = _quarter(c["ts"])
        tests = [fc for fc in chs if is_test(fc["path"]) and is_code(fc["path"])]
        renames[q] += sum(1 for fc in tests if (fc["status"] or "").startswith("R") or (fc["old_path"] and fc["old_path"] != fc["path"]))
        prod_mods = {_pkg(fc["path"]) for fc in chs if is_code(fc["path"]) and not is_test(fc["path"])}
        for m in prod_mods:
            name = m.split("/")[-1].lower()
            covered = any((_test_tokens(t["path"]) & mods == set()) or name in _test_tokens(t["path"]) for t in tests)
            cell = per[m][q]
            cell[0] += 1
            cell[1] += covered
            cell[2].append((c["hash"], covered))
    hits = []
    for m, qs in per.items():
        if not qs:
            continue
        lo, hi = min(qs), max(qs)
        best = None
        for q0 in range(lo, hi - 6):
            w1, w2 = range(q0, q0 + 4), range(q0 + 4, q0 + 8)
            n1, t1 = sum(qs[q][0] for q in w1 if q in qs), sum(qs[q][1] for q in w1 if q in qs)
            n2, t2 = sum(qs[q][0] for q in w2 if q in qs), sum(qs[q][1] for q in w2 if q in qs)
            if n1 < _MIN_WINDOW or n2 < _MIN_WINDOW:
                continue
            drop = t1 / n1 - t2 / n2
            if drop >= _MIN_DROP and (best is None or drop > best[0]):
                best = (drop, q0, n1, t1, n2, t2)
        if not best:
            ctx.drop("no_changepoint")
            continue
        drop, q0, n1, t1, n2, t2 = best
        w2 = range(q0 + 4, q0 + 8)
        if sum(renames[q] for q in w2) >= 3:
            ctx.drop("test_layout_change")
            continue
        c1 = [h for q in range(q0, q0 + 4) if q in qs for h, cov in qs[q][2] if cov]
        c2 = [(h, cov) for q in w2 if q in qs for h, cov in qs[q][2]]
        after_no_test = [h for h, cov in c2 if not cov]
        fixes_after = [h for h in after_no_test if ctx.is_fix(ctx.by_hash[h])]
        ts_lo = min(ctx.by_hash[h]["ts"] for h, _ in c2)
        by_uid = Counter(f["uid"] for f in ctx.fix_events if f["ts"] >= ts_lo and _pkg(f["path"]) == m)
        weak = [u for u, n in by_uid.items() if n >= 2 and ctx.best_test_weight(u) < 0.6]
        table = [{"quarter": f"{q // 4}Q{q % 4 + 1}", "commits": qs[q][0], "with_tests": qs[q][1]} for q in sorted(qs) if q0 <= q < q0 + 8]
        hits.append(Hit(
            subject=f"{m}|{q0}", score=drop * 10 + math.log(1 + n2) + len(fixes_after),
            title=f"{m}: test-touching commits fell {t1 / n1:.0%} -> {t2 / n2:.0%} (windows of {n1}/{n2} commits)",
            evidence={"module": m, "share_before": round(t1 / n1, 2), "share_after": round(t2 / n2, 2), "quarters": table,
                      "fixes_without_test_after": len(fixes_after), "symbols_with_repeat_fixes_and_test_gap": weak[:5]},
            commits=[*c1[-3:], *after_no_test[:3], *fixes_after[:2]], paths=[m],
            symbols=weak[:5]))
    return _top(hits)


# ---------------------------------------------------------------------------- 17. benign_gap_not_worth_testing
_BRANCHY_RX = re.compile(r"\b(if|elif|else|for|while|try|except|and|or|match|case)\b|\?")
_LOG_ROOT = {"log", "logger", "logging", "print", "console", "_log", "LOG", "LOGGER", "warnings"}


def _decorators_above(ctx: Ctx, s) -> list[str]:
    lines = _file_lines(ctx, s["path"])
    out, i = [], (s["start_line"] or 0) - 2
    while 0 <= i < len(lines) and lines[i].strip().startswith("@"):
        out.append(lines[i].strip())
        i -= 1
    return out


def _benign_class(ctx: Ctx, s, decs: list[str]) -> str | None:
    body = s["body"] or ""
    name = s["name"]
    if is_generated(s["path"]) or re.search(r"(?i)generated|do not edit|auto-?generated", "\n".join(_file_lines(ctx, s["path"])[:6])):
        return "generated"
    if name in ("__repr__", "__str__", "__hash__", "__eq__") and s["kind"] == "function":
        return "repr_str"
    if s["kind"] == "class" and (any("dataclass" in d for d in decs) or "dataclass" in body[:200]):
        return "dataclass"
    if s["kind"] != "function":
        return None
    abstract = any("abstractmethod" in d for d in decs) or re.search(r"raise\s+NotImplementedError|^\s*\.\.\.\s*$|assert_never", body, re.M)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    tree = _parse_fn(body)
    if abstract and len(lines) <= 6:
        return "abstract_stub"
    if tree is not None:
        stmts = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
        if stmts and all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and _call_root(n.value) in _LOG_ROOT for n in stmts):
            return "logging_only"
        if len(lines) <= 3 and not any(isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.IfExp, ast.Match))
                                       for n in ast.walk(tree)):
            return "tiny_no_branch"
    elif len(lines) <= 3:
        cx = ctx.complexity(s["uid"])
        if cx == 1 or (cx is None and not _BRANCHY_RX.search("\n".join(lines[1:]))):
            return "tiny_no_branch"
    return None


def _parse_fn(body: str):
    lines = body.splitlines()
    if not lines or not re.match(r"\s*(async\s+)?def\s", lines[0]):
        return None
    for cand in ("\n".join(lines), lines[0].strip() + "\n" + textwrap.indent(textwrap.dedent("\n".join(lines[1:])), "    ")):
        try:
            mod = ast.parse(textwrap.dedent(cand))
            fn = mod.body[0]
            return fn if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        except SyntaxError:
            continue
    return None


def _call_root(call: ast.Call) -> str:
    f = call.func
    while isinstance(f, ast.Attribute):
        f = f.value
    return f.id if isinstance(f, ast.Name) else ""


@candidate("benign_gap_not_worth_testing")
def benign_gap_not_worth_testing(ctx: Ctx):
    """Per directory: untested symbols (no symbol-level test_map row; when the sandbox measured the symbol, "untested"
    means 0 executed body lines instead) falling into a deterministic benign class:
    generated, __repr__/__str__, dataclass, tiny branch-free body, logging-only body, abstract/NotImplementedError
    stub, or dead (no callers, not exported/registered, untouched 12+ months). Never benign: fix history, public API
    (api_surface symbols that are in __all__ or re-exported by an __init__, since api_surface alone lists nearly everything),
    string-contract participation, any finding. Emitted when a directory has >=5 benign symbols or they hold >=15% of
    its untested lines (and at least 2 symbols). A non-Python body of <=3 lines is "tiny_no_branch" when its recorded
    AST complexity is 1 (branch-keyword regex only if no complexity was recorded)."""
    api = {r["uid"] for r in ctx.api_surface}
    contract_syms = {r["symbol"] for r in ctx.contracts if r["symbol"]}
    finding_syms = {r["symbol"] for r in ctx.findings if r["symbol"]}
    last_touch: dict[str, int] = {}
    for evs in ctx.events_by_commit.values():
        for e in evs:
            if e["ts"] and e["ts"] > last_touch.get(e["uid"], 0):
                last_touch[e["uid"]] = e["ts"]
    manifest = "\n".join((ctx.text_at_head(p) or "") for p in ("pyproject.toml", "package.json", "setup.cfg", "setup.py"))
    untested: dict[str, list] = defaultdict(list)
    for s in ctx.alive_symbols:
        if not _prod(s) or s["kind"] not in ("function", "class"):
            continue
        mcov = _measured_cov(ctx, s["uid"])
        if mcov is not None:
            if mcov > 0:
                continue
        elif s["uid"] in ctx.tests_for:
            continue
        untested[s["path"].rsplit("/", 1)[0] if "/" in s["path"] else "."].append(s)
    hits = []
    for d, syms in untested.items():
        benign: list[tuple] = []
        for s in syms:
            decs = _decorators_above(ctx, s)
            public = s["uid"] in api and (
                bool(re.search(rf"__all__[^\]]*[\"']{re.escape(s['name'])}[\"']", ctx.text_at_head(s["path"]) or "", re.S))
                or any(i.endswith("__init__.py") for i in ctx.imports[1].get(s["path"], ())))
            if _fix_counts(ctx)[s["uid"]] or public or s["uid"] in contract_syms or s["uid"] in finding_syms:
                ctx.drop("never_benign_fix_api_contract_or_finding")
                continue
            cls = _benign_class(ctx, s, decs)
            if cls is None and not ctx.callers(s["uid"]) and not decs and s["name"] not in manifest:
                exported = re.search(rf"__all__[^\]]*[\"']{re.escape(s['name'])}[\"']", ctx.text_at_head(s["path"]) or "", re.S)
                if not exported and ctx.head_ts - last_touch.get(s["uid"], s["last_ts"] or 0) > 365 * DAY and not s["name"].startswith("__"):
                    cls = "dead"
            if cls:
                benign.append((s, cls))
        if not benign:
            continue
        lines_total = sum(s["loc"] or 0 for s in syms) or 1
        lines_benign = sum(s["loc"] or 0 for s, _ in benign)
        if len(benign) < 2 or (len(benign) < 5 and lines_benign / lines_total < 0.15):
            ctx.drop("below_directory_threshold")
            continue
        classes = Counter(c for _, c in benign)
        hits.append(Hit(
            subject=d, score=len(benign) + 10 * lines_benign / lines_total,
            title=f"{d}: {len(benign)} of {len(syms)} untested symbols are benign ({', '.join(f'{k} {v}' for k, v in classes.most_common(3))})",
            evidence={"classes": dict(classes), "benign_symbols": len(benign), "untested_symbols": len(syms),
                      "benign_line_share": round(lines_benign / lines_total, 2), "samples": [s["qualname"] for s, _ in benign[:8]]},
            paths=sorted({s["path"] for s, _ in benign})[:6], symbols=[s["uid"] for s, _ in benign[:10]]))
    return _top(hits)
