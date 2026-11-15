"""Catalogue family: API and compatibility (public surface, overrides, serialization formats, contract drift).

Release comparisons use ``api_surface`` rows stored for ``HEAD`` and the most recent tags (see safety/step.py), so
they only see the tags Oracle already snapshotted. Python gets the richest support (signatures, overrides, raise
contracts); other languages are covered where a builder works on text lines.
"""
from __future__ import annotations

import bisect
import json
import re
from collections import defaultdict

import pygit2

from ..safety.api import params
from .base import (DAY, Ctx, Hit, candidate, is_code, is_docs, is_generated, is_test, is_vendored, subject_of)

# ---------------------------------------------------------------------------- shared helpers
_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _scratch(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_api_cache", {})


def _ver(ref: str) -> tuple[int, int, int] | None:
    m = _VER.search(ref)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def _lang(path: str) -> str | None:
    return "python" if path.endswith(".py") else None


def _releases(ctx: Ctx) -> list[tuple[int, str, str, dict]]:
    """[(commit_time, ref, sha, {uid: api_surface row})] for tag refs, oldest first. Time and commit come from the
    ``tags`` table (annotated tags already peeled); falls back to resolving the ref in git when it is empty."""
    sc = _scratch(ctx)
    if "releases" in sc:
        return sc["releases"]
    refs: dict[str, dict] = defaultdict(dict)
    for r in ctx.api_surface:
        if r["ref"] != "HEAD":
            refs[r["ref"]][r["uid"]] = r
    tag_rows = {t["name"]: t for t in ctx.tags}  # enrichment: peeled commit + time for every tag
    out = []
    for ref, rows in refs.items():
        t = tag_rows.get(ref) or tag_rows.get(ref.removeprefix("refs/tags/"))
        if t is not None:
            out.append((t["ts"], ref, t["commit_hash"], rows))
            continue
        try:  # tags table not built (older database): peel the ref directly
            c = ctx.repo.references["refs/tags/" + ref].peel(pygit2.Commit)
        except Exception:
            continue
        out.append((c.commit_time, ref, str(c.id), rows))
    out.sort(key=lambda t: (t[0], t[1]))
    sc["releases"] = out
    return out


def _sig_breaks(old_sig: str, new_sig: str, lang: str | None) -> list[str]:
    po, pn = params(old_sig or "", lang), params(new_sig or "", lang)
    if po is None or pn is None:
        return []
    out = []
    new_by = {p.name: p for p in pn}
    old_by = {p.name: p for p in po}
    has_kw = any(p.kind == "var_keyword" for p in pn)
    for p in po:
        if p.kind in ("var_positional", "var_keyword"):
            if not any(q.kind == p.kind for q in pn):
                out.append(f"*{p.name} no longer accepted")
            continue
        q = new_by.get(p.name)
        if q is None:
            if not has_kw:
                out.append(f"param {p.name} removed")
        elif p.default is not None and q.default is None:
            out.append(f"param {p.name} now required")
        elif p.kind == "positional" and q.kind == "keyword_only":
            out.append(f"param {p.name} now keyword-only")
    for q in pn:
        if q.name not in old_by and q.default is None and q.kind in ("positional", "keyword_only"):
            out.append(f"new required param {q.name}")
    old_pos = [p.name for p in po if p.kind == "positional" and p.name in new_by]
    new_pos = [p.name for p in pn if p.kind == "positional" and p.name in old_by]
    if old_pos != new_pos:
        out.append("positional params reordered")
    return out


def _release_breaks(ctx: Ctx):
    """Yield dicts describing each public symbol removed/changed incompatibly between consecutive tag snapshots."""
    rel = _releases(ctx)
    for (ts0, ref0, sha0, old), (ts1, ref1, sha1, new) in zip(rel, rel[1:]):
        by_qual = {(r["qualname"], r["kind"]): r for r in new.values()}
        for uid, o in old.items():
            n = new.get(uid)
            if n is None:
                if (o["qualname"], o["kind"]) in by_qual:
                    ctx.drop("moved_or_renamed_module")
                    continue
                yield {"old": ref0, "new": ref1, "ts": ts1, "sha": sha1, "uid": uid, "row": o, "breaks": ["removed"],
                       "deprecated_before": bool(o["deprecated"])}
                continue
            b = _sig_breaks(o["signature"], n["signature"], _lang(o["path"])) if o["kind"] != "attribute" else []
            if b:
                yield {"old": ref0, "new": ref1, "ts": ts1, "sha": sha1, "uid": uid, "row": n, "breaks": b,
                       "deprecated_before": bool(o["deprecated"])}


# ---------------------------------------------------------------------------- 17. public API breaking change
@candidate("public_api_breaking_change")
def public_api_breaking_change(ctx: Ctx):
    """Compare ``api_surface`` between consecutive tag snapshots (the releases Oracle stored a surface for; tag
    time and commit come from the ``tags`` enrichment table, so annotated tags are exact): removed public
    symbols and removed/now-required/reordered parameters. A break across a major bump or in a 0.x version is dropped;
    patch and minor bumps are kept. Reverts/fixes touching the symbol's file within 14 days of the new tag and the
    symbol's HEAD caller count raise the score. Needs >=2 tags, otherwise nothing is emitted (counted as
    ``no_releases``)."""
    rel = _releases(ctx)
    if len(rel) < 2:
        ctx.drop("no_releases")
        return
    for b in _release_breaks(ctx):
        v0, v1 = _ver(b["old"]), _ver(b["new"])
        if v1 and v1[0] == 0:
            ctx.drop("pre_1_0")
            continue
        if v0 and v1 and v1[0] > v0[0]:
            ctx.drop("major_bump")
            continue
        bump = "unknown" if not (v0 and v1) else ("minor" if v1[1] > v0[1] else "patch")
        row = b["row"]
        later = [c for c in ctx.path_commits(row["path"])
                 if b["ts"] <= c["ts"] <= b["ts"] + 14 * DAY and (ctx.is_fix(c) or c["is_revert"])]
        callers = len(ctx.callers(b["uid"]))
        yield Hit(
            subject=f"{b['new']}|{b['uid']}",
            score=3 + (3 if bump == "patch" else 2 if bump == "minor" else 1) + 4 * len(later) + min(callers, 10) * 0.5
                  + (1 if "removed" in b["breaks"] else 0),
            title=f"{row['qualname']}: {', '.join(b['breaks'][:2])} between {b['old']} and {b['new']} ({bump} bump)",
            evidence={"from": b["old"], "to": b["new"], "bump": bump, "breaks": b["breaks"][:5],
                      "release_tag_message": subject_of(next((t["message"] for t in ctx.tags if t["name"] == b["new"]), "")),
                      "callers_at_head": callers, "deprecated_before_removal": b["deprecated_before"],
                      "fixed_or_reverted_within_14d": [{"hash": c["hash"][:10], "subject": subject_of(c["message"])} for c in later[:3]]},
            commits=[b["sha"], *[c["hash"] for c in later[:3]]], diffs=[(c["hash"], row["path"]) for c in later[:2]],
            paths=[row["path"]], symbols=[b["uid"]] if b["uid"] in ctx.symbols else [])


# ---------------------------------------------------------------------------- 18. fragile base class override drift
def _py_def_params(text: str, cls: str, meth: str) -> tuple[str, ...] | None:
    """Parameter names of ``def meth`` inside ``class cls`` (None when not found)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*)class\s+{re.escape(cls)}\b", ln)
        if not m:
            continue
        base_ind = len(m.group(1))
        for j in range(i + 1, len(lines)):
            body = lines[j]
            if body.strip() and len(body) - len(body.lstrip()) <= base_ind:
                break
            md = re.match(rf"^\s+(?:async\s+)?def\s+{re.escape(meth)}\s*\(", body)
            if md:
                sig = body
                k = j
                while sig.count("(") > sig.count(")") and k + 1 < len(lines) and k - j < 20:
                    k += 1
                    sig += " " + lines[k].strip()
                ps = params(sig.strip(), "python")
                return tuple(p.name for p in ps) if ps is not None else None
    return None


def _param_changes(ctx: Ctx, m, cname: str, budget: list[int]):
    """(hash, seq, old_names, new_names) for each commit that changed the parameter names of method ``m``. Uses the
    ``symbol_versions`` signature history (multi-line headers included); a method without recorded versions falls
    back to re-reading the file before/after each modifying commit (budgeted)."""
    vs = ctx.versions_by_uid.get(m["uid"])
    if vs:
        out = []
        for a, b in zip(vs, vs[1:]):
            pa, pb = params(a["signature"] or "", "python"), params(b["signature"] or "", "python")
            if pa is None or pb is None:
                continue
            na, nb = tuple(p.name for p in pa), tuple(p.name for p in pb)
            if na != nb and not ctx.by_hash[b["hash"]]["is_bulk"]:
                out.append((b["hash"], b["seq"], na, nb))
        return out[-10:]
    out = []
    for e in [e for e in ctx.symbol_events(m["uid"]) if e["change"] == "modified"
              and not ctx.by_hash[e["hash"]]["is_bulk"]][-10:]:
        budget[0] -= 1
        if budget[0] < 0:
            break
        old_t, new_t = ctx.text_before(e["hash"], m["path"]), ctx.text_at(e["hash"], m["path"])
        if not old_t or not new_t:
            continue
        po, pn = _py_def_params(old_t, cname, m["name"]), _py_def_params(new_t, cname, m["name"])
        if po is not None and pn is not None and po != pn:
            out.append((e["hash"], e["seq"], po, pn))
    return out


@candidate("fragile_base_class_override_drift")
def fragile_base_class_override_drift(ctx: Ctx):
    """A base-class method's parameter list changed (add/remove/reorder/rename, Python only) while a subclass at HEAD
    still overrides it with exactly the old parameter list and has not been modified since. The change is read from the
    method's ``symbol_versions`` signature history (exact, multi-line headers included, no blob reads); only a method
    with no recorded versions falls back to comparing file text around each modifying commit. Ranked by the number of
    stale overrides and base-method callers. Hook-call add/remove inside base bodies is not analysed. Overrides in
    test files (doubles) are dropped, as are changes where every override was updated."""
    children: dict[str, set[str]] = defaultdict(set)
    for r in ctx.q("SELECT src, dst FROM edges WHERE kind='INHERITS'"):
        children[r["dst"]].add(r["src"])
    if not children:
        return

    def descendants(uid: str) -> set[str]:
        seen, todo = set(), [uid]
        while todo:
            for ch in children.get(todo.pop(), ()):
                if ch not in seen:
                    seen.add(ch)
                    todo.append(ch)
        return seen

    budget = [2000]  # fallback file-text reads (before/after) across the whole run
    for base_uid in list(children):
        base = ctx.symbols.get(base_uid)
        if base is None or not base["alive"] or base["lang"] != "python" or is_test(base["path"]):
            continue
        desc = descendants(base_uid)
        cname = base["qualname"].rsplit(".", 1)[-1]
        methods = [s for s in ctx.symbols_by_path.get(base["path"], []) if s["parent"] in (base["qualname"], base["uid"])
                   and s["kind"] == "function"]
        for m in methods:
            for h_, seq_, po, pn in _param_changes(ctx, m, cname, budget):
                e = {"hash": h_, "seq": seq_}
                stale, updated, tests = [], 0, 0
                for d in desc:
                    ds = ctx.symbols.get(d)
                    if ds is None or not ds["alive"]:
                        continue
                    ov = next((s for s in ctx.symbols_by_path.get(ds["path"], [])
                               if s["parent"] in (ds["qualname"], ds["uid"]) and s["name"] == m["name"] and s["kind"] == "function"), None)
                    if ov is None:
                        continue
                    if is_test(ov["path"]):
                        tests += 1
                        ctx.drop("test_double")
                        continue
                    pp = params(ov["signature"] or "", "python")
                    names = tuple(p.name for p in pp) if pp is not None else None
                    touched = any(ev["seq"] >= e["seq"] and ev["change"] in ("modified", "created")
                                  for ev in ctx.symbol_events(ov["uid"]))
                    if names == po and not touched:
                        stale.append(ov)
                    else:
                        updated += 1
                if not stale:
                    if updated:
                        ctx.drop("all_overrides_updated")
                    continue
                callers = len(ctx.callers(m["uid"]))
                yield Hit(
                    subject=f"{e['hash']}|{m['uid']}",
                    score=3 + 3 * len(stale) + min(callers, 10) * 0.5,
                    title=f"{base['qualname']}.{m['name']} signature {list(po)} -> {list(pn)}; {len(stale)} override(s) still use the old one",
                    evidence={"old_params": list(po), "new_params": list(pn), "stale_overrides": [s["uid"] for s in stale[:6]],
                              "updated_overrides": updated, "base_callers": callers},
                    commits=[e["hash"]], diffs=[(e["hash"], base["path"])],
                    paths=[base["path"], *sorted({s["path"] for s in stale})[:3]], symbols=[m["uid"], *[s["uid"] for s in stale[:4]]])


# ---------------------------------------------------------------------------- 19. deprecated never removed
_REPLACE = re.compile(r"(?i)\b(use|instead|in favou?r of|replaced by|renamed to|replacement)\b[^\w]{0,6}[`'\"]?([\w.]{3,})")


@candidate("deprecated_never_removed")
def deprecated_never_removed(ctx: Ctx):
    """Public symbols still deprecated at HEAD (``api_surface`` deprecation evidence). Primary tier: >=2 callers that
    adopted the deprecated name after the deprecation commit and a named replacement in the evidence/commit message.
    A caller counts when its symbol was created after the deprecation commit, or when a line added to that caller
    after the deprecation commit calls the name (``diff_lines.symbol`` attribution), so older functions that gained a
    new call to the deprecated API are found too (up to 40 callers examined per symbol). Low tier: deprecated for more than 18 months, or (from tag
    snapshots) a public symbol removed without ever being deprecated. Vendored/generated/test files and symbols with
    zero callers are dropped."""
    for r in ctx.api_surface:
        if r["ref"] != "HEAD" or not r["deprecated"]:
            continue
        path = r["path"]
        if is_test(path) or is_vendored(path) or is_generated(path):
            ctx.drop("vendor_generated_tests")
            continue
        sym = ctx.symbols.get(r["uid"])
        callers = [c for c in ctx.callers(r["uid"]) if c in ctx.symbols and c != r["uid"]]
        if not callers:
            ctx.drop("zero_callers")
            continue
        ev = r["deprecated"].strip()
        needle = ev.replace("%", "").replace("_", "")[:60]
        row = ctx.q1("SELECT hash, seq, ts FROM diff_lines WHERE sign='+' AND path=? AND instr(text, ?) > 0 "
                     "ORDER BY seq LIMIT 1", (path, ev[:60]))
        if row is None:
            ctx.drop("deprecation_commit_unknown")
            continue
        c = ctx.by_hash[row["hash"]]
        after = [u for u in callers if ctx.symbols[u]["first_seq"] and ctx.symbols[u]["first_seq"] > row["seq"]
                 and not is_test(ctx.symbols[u]["path"])]
        short = r["qualname"].rsplit(".", 1)[-1]
        grew = []
        for u in callers[:40]:
            if u in after or is_test(ctx.symbols[u]["path"]):
                continue
            if any(x["seq"] > row["seq"] and short + "(" in x["text"] for x in ctx.lines_in_symbol(u, "+")):
                grew.append(u)
        after = after + grew
        named = _REPLACE.search(ev) or _REPLACE.search(c["message"] or "")
        age_days = (ctx.head_ts - row["ts"]) / DAY
        tier = None
        if len(after) >= 2 and named:
            tier = "primary"
        elif age_days > 18 * 30:
            tier = "low_age"
        if not tier:
            ctx.drop("not_aged_and_not_adopted")
            continue
        yield Hit(
            subject=f"{r['uid']}|{tier}",
            score=(10 if tier == "primary" else 3) + len(after) * 2 + min(age_days / 365, 3) + min(len(callers), 8) * 0.3,
            title=f"{r['qualname']} deprecated {round(age_days)} days ago, {len(after)} callers adopted it since ({tier})",
            evidence={"tier": tier, "deprecation": ev[:140], "deprecated_in": row["hash"][:10],
                      "replacement": named.group(2) if named else None, "callers": len(callers),
                      "callers_adopting_after_deprecation": [u for u in after[:6]],
                      "of_which_older_callers_with_new_call_lines": len(grew)},
            commits=[row["hash"]], diffs=[(row["hash"], path)], paths=[path],
            symbols=[r["uid"], *after[:3]] if sym is not None else after[:3])
    for b in _release_breaks(ctx):
        if "removed" in b["breaks"] and not b["deprecated_before"]:
            v1 = _ver(b["new"])
            if v1 and v1[0] == 0:
                continue
            row = b["row"]
            yield Hit(
                subject=f"{b['uid']}|removed_without_deprecation",
                score=3 + (1 if v1 and v1[2] else 0),
                title=f"{row['qualname']} removed in {b['new']} without any prior deprecation (low tier)",
                evidence={"tier": "low_removed_without_deprecation", "from": b["old"], "to": b["new"]},
                commits=[b["sha"]], paths=[row["path"]])


# ---------------------------------------------------------------------------- 20. exception message matching consumer
_MSG_LIT = re.compile(r"""(raise\s+[\w.]+\(|throw\s+new\s+\w+\(|(?:logger|logging|log)\.\w+\(|console\.(?:error|warn)\()\s*(?:[fFrRbB])?(["'])(.*?)\2""")
_PLACEHOLDER = re.compile(r"\{[^}]*\}|%[-#0 +]?\d*[sdrif]|\$\{[^}]*\}|\{\}")
_EMIT_LINE = re.compile(r"\b(raise|throw|logger|logging|log\.|console\.|print)\b")


def _fragment(lit: str) -> str:
    pieces = [p.strip() for p in _PLACEHOLDER.split(lit)]
    return max(pieces, key=len, default="")


@candidate("exception_message_matching_consumer")
def exception_message_matching_consumer(ctx: Ctx):
    """A raise/throw/log message literal was changed (paired by call prefix within one file+commit) while a file at
    HEAD still contains a >=12 character static fragment of the *old* text outside raise/log statements (typically
    ``"old text" in str(exc)``, ``pytest.raises(match=...)`` or log alerting) and lacks the new text. Test consumers
    rank lower. Only the 250 most recent message changes are searched (each is one SQL scan)."""
    by_file: dict[tuple, dict[str, dict[str, list]]] = defaultdict(lambda: {"+": defaultdict(list), "-": defaultdict(list)})
    for r in ctx.grep(r"raise\s+[\w.]+\(\s*[fFrRbB]?[\"']|throw\s+new\s+\w+\(\s*[\"']|(logger|logging|log)\.\w+\(\s*[fFrRbB]?[\"']|"
                      r"console\.(error|warn)\(\s*[\"']", None, limit=60000):
        m = _MSG_LIT.search(r["text"])
        if m and not is_test(r["path"]) and not is_docs(r["path"]) and not is_vendored(r["path"]):
            by_file[(r["hash"], r["path"], r["seq"])][r["sign"]][m.group(1).strip()].append(m.group(3))
    changes = []
    for (h, path, seq), d in by_file.items():
        for key, olds in d["-"].items():
            news = d["+"].get(key)
            if not news:
                continue
            for old in olds:
                if old in news:
                    continue
                frag = _fragment(old)
                if len(frag) < 12:
                    ctx.drop("short_fragment")
                    continue
                if any(frag in n for n in news):
                    ctx.drop("fragment_kept_in_new_text")
                    continue
                changes.append((seq, h, path, old, news[0], frag))
    changes.sort(key=lambda c: tuple(x if isinstance(x, (int, float)) else 0 for x in c[:2]), reverse=True)
    for seq, h, path, old, new, frag in changes[:250]:
        paths = [r["path"] for r in ctx.q("SELECT DISTINCT path FROM diff_lines WHERE sign='+' AND instr(text, ?) > 0", (frag,))]
        consumers = []
        for p in paths:
            if is_vendored(p) or is_generated(p) or is_docs(p):
                continue
            head = ctx.text_at_head(p)
            if not head or frag not in head:
                continue
            new_frag = _fragment(new)
            lines = [(i, ln) for i, ln in enumerate(head.splitlines(), 1) if frag in ln]
            use = [(i, ln) for i, ln in lines if not _EMIT_LINE.search(ln.split(frag)[0])]
            if not use:
                ctx.drop("only_emitting_lines")
                continue
            if new_frag and len(new_frag) >= 12 and new_frag in head:
                ctx.drop("consumer_matches_both")
                continue
            consumers.append((p, use[0][0], use[0][1].strip()[:140]))
        if not consumers:
            ctx.drop("no_head_consumer")
            continue
        prod = [c for c in consumers if not is_test(c[0])]
        yield Hit(
            subject=f"{h}|{path}|{frag[:40]}",
            score=4 + 4 * len(prod) + min(len(consumers) - len(prod), 3),
            title=f"message '{old[:60]}' changed but {len(consumers)} file(s) still match the old text",
            evidence={"old_message": old[:120], "new_message": new[:120], "fragment": frag[:80],
                      "consumers": [{"path": p, "line": ln, "text": t} for p, ln, t in consumers[:5]]},
            commits=[h], diffs=[(h, path)], paths=[path, *[c[0] for c in consumers[:3]]])


# ---------------------------------------------------------------------------- 21. serialization field renumber
def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(//|#)[^\n]*", "", text)


def _parse_idl(text: str, kind: str) -> dict:
    """-> {"messages": {name: {"fields": {num: (label, type, name)}, "res_nums": set, "res_names": set}},
            "enums": {name: {value: name}}}  for .proto and .thrift."""
    out = {"messages": {}, "enums": {}}
    stack: list[tuple[str, str]] = []
    field_rx = re.compile(r"^\s*(optional|required|repeated)?\s*(map\s*<[^>]+>|[\w.]+)\s+(\w+)\s*=\s*(\d+)\b") if kind == "proto" \
        else re.compile(r"^\s*(\d+)\s*:\s*(required|optional)?\s*([\w.]+(?:\s*<[^>]+>)?)\s+(\w+)")
    for raw in _strip_comments(text).splitlines():
        ln = raw.strip()
        m = re.match(r"(message|struct|exception|union|enum)\s+(\w+)", ln)
        if m and "{" in ln:
            qual = ".".join([n for _, n in stack] + [m.group(2)])
            stack.append((m.group(1), m.group(2)))
            if m.group(1) == "enum":
                out["enums"][qual] = {}
            else:
                out["messages"][qual] = {"fields": {}, "res_nums": set(), "res_names": set()}
            if ln.count("}") > ln.count("{") - 1 and ln.rstrip().endswith("}"):
                stack.pop()
            continue
        if ln.startswith("oneof ") and "{" in ln:
            stack.append(("oneof", ""))
            continue
        if ln.startswith("}"):
            if stack:
                stack.pop()
            continue
        if not stack:
            continue
        kind_name = next((s for s in reversed(stack) if s[0] != "oneof"), None)
        if kind_name is None:
            continue
        qual = ".".join(n for k, n in stack if k != "oneof")
        if kind_name[0] == "enum":
            mv = re.match(r"(\w+)\s*=\s*(-?\d+)", ln)
            if mv:
                out["enums"][qual][int(mv.group(2))] = mv.group(1)
            elif kind == "thrift":
                mv = re.match(r"(\w+)\s*(?:=\s*(-?\d+))?\s*[,;]?$", ln)
                if mv and mv.group(2):
                    out["enums"][qual][int(mv.group(2))] = mv.group(1)
            continue
        msg = out["messages"].get(qual)
        if msg is None:
            continue
        mr = re.match(r"reserved\s+(.*?);", ln)
        if mr:
            body = mr.group(1)
            for a, b in re.findall(r"(\d+)\s+to\s+(\d+|max)", body):
                msg["res_nums"].update(range(int(a), min(int(b) if b != "max" else int(a) + 50, int(a) + 5000) + 1))
            msg["res_nums"].update(int(x) for x in re.findall(r"(?<![\w.])(\d+)(?!\s+to)", body))
            msg["res_names"].update(re.findall(r"[\"'](\w+)[\"']", body))
            continue
        mf = field_rx.match(ln)
        if mf:
            if kind == "proto":
                label, typ, name, num = mf.groups()
            else:
                num, label, typ, name = mf.groups()
            msg["fields"][int(num)] = (label or "", re.sub(r"\s+", "", typ), name)
    return out


def _idl_breaks(old: dict, new: dict) -> list[str]:
    out = []
    for name, om in old["messages"].items():
        nm = new["messages"].get(name)
        if nm is None:
            continue
        new_names = {f[2]: n for n, f in nm["fields"].items()}
        for num, (lab, typ, fname) in om["fields"].items():
            nf = nm["fields"].get(num)
            if nf is None:
                if num not in nm["res_nums"] and fname not in nm["res_names"]:
                    moved = new_names.get(fname)
                    out.append(f"{name}.{fname}: tag {num} removed without reserved" +
                               (f" (name now on tag {moved})" if moved else ""))
            elif nf[2] != fname:
                out.append(f"{name}: tag {num} reused ({fname} -> {nf[2]})")
            elif nf[1] != typ:
                out.append(f"{name}.{fname}: type changed {typ} -> {nf[1]} on tag {num}")
        for num, (lab, typ, fname) in nm["fields"].items():
            if num not in om["fields"] and lab == "required":
                out.append(f"{name}.{fname}: required field added")
    for name, oe in old["enums"].items():
        ne = new["enums"].get(name)
        if ne is None:
            continue
        for num, vname in oe.items():
            if num not in ne:
                if vname not in ne.values():
                    out.append(f"enum {name}.{vname} removed")
            elif ne[num] != vname:
                out.append(f"enum {name}: value {num} reused ({vname} -> {ne[num]})")
    return out


def _avro_fields(text: str) -> dict[str, dict[str, tuple[str, bool]]]:
    try:
        doc = json.loads(text)
    except Exception:
        return {}
    out: dict[str, dict] = {}

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            if o.get("type") == "record" and isinstance(o.get("fields"), list):
                out[o.get("name", "?")] = {f.get("name"): (json.dumps(f.get("type"), sort_keys=True), "default" in f)
                                          for f in o["fields"] if isinstance(f, dict)}
                for f in o["fields"]:
                    walk(f.get("type"))
            else:
                walk(o.get("type"))
                walk(o.get("items"))
    walk(doc)
    return out


_VERSION_CONST = re.compile(r"""(?ix)^\s*(?:export\s+|public\s+|static\s+|final\s+|const\s+|var\s+|let\s+|private\s+|
                                    (?:int|long|string|str|u?int\d*)\s+)*
                                [\"']?((?:[A-Z0-9]+_)*(?:SCHEMA|PROTOCOL|PROTO|WIRE|FORMAT|SERIALI[SZ]\w*|ENCODING|
                                MESSAGE|DATA|PAYLOAD|CODEC|MAGIC)_?(?:\w*_)?VERSION|
                                (?:schema|protocol|wire|format|serializ\w*|encoding|payload|codec)_?version)[\"']?\s*
                                (?::\s*[\w\[\]]+\s*)?[:=]\s*[\"']?([\w.\-]+)[\"']?\s*[,;]?\s*(?:\#.*|//.*)?$""")
_SERIAL_HINT = re.compile(r"(?i)serializ|deserializ|encode|decode|pack\(|unpack|schema|protocol|wire|pickle|marshal|"
                          r"protobuf|\.proto\b|msgpack|payload|migrat\w*\(")
_NOT_WIRE = re.compile(r"(?i)(^|_)(api|app|package|python|node|docker|image|chart|release|build|lib|library|cli|"
                       r"software|git|tool|min|max)_version$|^__version__$")


def _version_value(v: str) -> tuple | None:
    parts = re.findall(r"\d+", v)
    return tuple(int(x) for x in parts) if parts else None


def _version_constant_hits(ctx: Ctx):
    """Lower-priority kind of serialization_field_renumber: changed wire/schema version constants."""
    rows = ctx.grep(r"(?i)(schema|protocol|proto|wire|format|serializ\w*|encoding|payload|codec|message|data|magic)"
                    r"[a-z0-9_]*version", None, limit=60000)
    by_file: dict[tuple, dict[str, dict[str, str]]] = defaultdict(lambda: {"+": {}, "-": {}})
    for r in rows:
        path = r["path"]
        if is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path) or path.endswith(".lock"):
            continue
        m = _VERSION_CONST.match(r["text"])
        if m and not _NOT_WIRE.search(m.group(1)):
            by_file[(r["hash"], path)][r["sign"]][m.group(1)] = m.group(2)
    n = 0
    for (h, path), d in by_file.items():
        for name, new in d["+"].items():
            old = d["-"].get(name)
            if old is None or old == new:
                continue
            if n >= 150:
                return
            n += 1
            text = ctx.text_at(h, path) or ""
            idl = [x["path"] for x in ctx.changes(h) if x["path"].endswith((".proto", ".thrift", ".avsc"))]
            serial_code = [x["path"] for x in ctx.changes(h) if x["path"] != path and is_code(x["path"])
                           and not is_test(x["path"]) and re.search(r"(?i)serializ|codec|schema|wire|proto|encod",
                                                                   x["path"])]
            if not (idl or serial_code or _SERIAL_HINT.search(text) or _SERIAL_HINT.search(path)):
                ctx.drop("version_constant_not_serialization")
                continue
            ov, nv = _version_value(old), _version_value(new)
            monotonic = bool(ov and nv and nv > ov)
            gates = ctx.grep(r"\b" + re.escape(name) + r"\b\s*(<|>|<=|>=|==|!=|in\b)|(<|>|<=|>=|==|!=)\s*\w*\.?"
                             + re.escape(name) + r"\b", "+", limit=50)
            gated = [g for g in gates if not _VERSION_CONST.match(g["text"]) and not is_test(g["path"])]
            if monotonic and gated and not idl and not serial_code:
                ctx.drop("bump_with_reader_gate")
                continue
            c = ctx.by_hash[h]
            yield Hit(
                subject=f"{h}|{path}|{name}|version",
                score=2 + 3 * (not monotonic) + 2 * (not gated) + 1.5 * bool(idl) + 1 * bool(serial_code),
                title=f"{name} changed {old} -> {new} in {path.rsplit('/', 1)[-1]}"
                      + ("" if gated else " with no reader version gate"),
                evidence={"constant": name, "old": old, "new": new, "monotonic_bump": monotonic,
                          "reader_gates": [{"path": g["path"], "hash": g["hash"][:10]} for g in gated[:4]],
                          "idl_files_in_commit": idl[:4], "serializer_files_in_commit": serial_code[:4],
                          "commit_subject": subject_of(c["message"])},
                commits=[h, *[g["hash"] for g in gated[:1] if g["hash"] != h]], diffs=[(h, path)],
                paths=[path, *idl[:2]])


@candidate("serialization_field_renumber")
def serialization_field_renumber(ctx: Ctx):
    """Wire-format schema edits (.proto, .thrift, .avsc only): a field tag reused for a different field, a field
    removed without ``reserved``, a type change on the same tag, an enum value removed/reused, an added ``required``
    field (Avro: type change, removed field, added field without default). Files are parsed before and after each
    commit that touched them (comments stripped, so comment edits yield nothing); new messages, alpha/experimental
    packages and generated/vendored directories are ignored. The spec's separate lower "version constants" kind is
    implemented too: a wire/schema/protocol/format-version constant (``SCHEMA_VERSION = 3``, ``WIRE_FORMAT_VERSION``
    ...) whose value changed in a commit, in a non-test code or config file that deals with serialization. Ranked up
    when the version went down or is not numeric-monotonic, when no reader compares the constant anywhere in the
    history (no version gate), and when an IDL file or serializer code changed in the same commit; a plain bump with a
    reader gate and no format change is dropped. Subjects end with ``|version`` so they never collide with the
    schema-edit hits."""
    yield from _version_constant_hits(ctx)
    n = 0
    for path, rows in ctx.changes_by_path.items():
        if not path.endswith((".proto", ".thrift", ".avsc")) or is_vendored(path) or is_generated(path):
            continue
        if re.search(r"(?i)alpha|beta|experimental|(^|/)internal/", path):
            ctx.drop("alpha_package")
            continue
        kind = path.rsplit(".", 1)[-1]
        for r in rows:
            if r["status"] in ("A", "D") or n > 400:
                continue
            old_t, new_t = ctx.text_before(r["hash"], path), ctx.text_at(r["hash"], path)
            if not old_t or not new_t:
                continue
            n += 1
            if kind == "avsc":
                of, nf = _avro_fields(old_t), _avro_fields(new_t)
                breaks = []
                for rec, fields in of.items():
                    if rec not in nf:
                        continue
                    for fname, (typ, dflt) in fields.items():
                        if fname not in nf[rec]:
                            breaks.append(f"{rec}.{fname}: field removed")
                        elif nf[rec][fname][0] != typ:
                            breaks.append(f"{rec}.{fname}: type changed")
                    breaks += [f"{rec}.{f}: field added without default" for f, (_, d) in nf[rec].items()
                               if f not in fields and not d]
            else:
                if kind == "proto" and re.search(r"(?m)^package\s+[\w.]*(alpha|beta|experimental)", new_t):
                    ctx.drop("alpha_package")
                    continue
                breaks = _idl_breaks(_parse_idl(old_t, kind), _parse_idl(new_t, kind))
            if not breaks:
                continue
            c = ctx.by_hash[r["hash"]]
            yield Hit(
                subject=f"{r['hash']}|{path}",
                score=3 + 2 * len(breaks[:5]) + (2 if any("reused" in b or "type changed" in b for b in breaks) else 0),
                title=f"{path.rsplit('/', 1)[-1]}: {breaks[0][:100]}",
                evidence={"breaks": breaks[:8], "commit_subject": subject_of(c["message"])},
                commits=[r["hash"]], diffs=[(r["hash"], path)], paths=[path])


# ---------------------------------------------------------------------------- 22. route & middleware order
_ENTRY = re.compile(r"""^\s*(["'][^"']+["']|[\w.]+(\([^)]*\))?|(app|router|api|bp|server|r)\.\w+\(.*\)|(re_)?path\(.*\)|url\(.*\))\s*[,;]?\s*$""")
_ORDER_KW = re.compile(r"(?i)auth|cors|csrf|session|error")
_IMPORT_LN = re.compile(r"^\s*(import|from|require|using|#include)\b")


@candidate("route_and_middleware_order_change")
def route_and_middleware_order_change(ctx: Ctx):
    """In one file+commit, >=1 entry-like line (a list entry, ``app.use``/``add_middleware``/route registrations) that
    were removed and re-added unchanged (a reorder), inside a >=3 entry block whose neighbourhood mentions
    auth/cors/csrf/session/error. Alphabetical re-sorts (new order sorted), import reorders, package.json/lock files
    and bulk commits (not indexed) are dropped. Escalated when the same file is reordered again within 15 commits or
    a fix touches it within 14 days."""
    # One ordered pass over diff_lines (a SQL self-join on a computed key took minutes on large repos): per
    # (commit, file), lines that were removed and re-added unchanged are moves.
    groups: dict[tuple, dict[int, str]] = defaultdict(dict)
    meta: dict[tuple, tuple] = {}
    key = lambda s: s.strip().rstrip(",;").strip()  # noqa: E731
    cur, removed, added = None, set(), []

    def flush() -> None:
        if cur and removed and added:
            for nl, t in added:
                if key(t) in removed:
                    groups[cur[:2]][nl] = t.strip()
                    meta[cur[:2]] = cur[2:]

    for r in ctx.stream("SELECT hash, path, seq, ts, sign, line, text FROM diff_lines "
                        "WHERE length(trim(text)) > 3 ORDER BY hash, path"):
        k = (r["hash"], r["path"], r["seq"], r["ts"])
        if k != cur:
            flush()
            cur, removed, added = k, set(), []
        if r["sign"] == "-":
            removed.add(key(r["text"]))
        else:
            added.append((r["line"], r["text"]))
    flush()
    cands = []
    for (h, path), moved in groups.items():
        if is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path) or path.endswith(".json"):
            ctx.drop("package_json_or_generated")
            continue
        ents = {nl: t for nl, t in moved.items() if _ENTRY.match(t) and not _IMPORT_LN.match(t)}
        if len(ents) < 1:
            if any(_IMPORT_LN.match(t) for t in moved.values()):
                ctx.drop("import_reorder")
            continue
        ordered = [ents[k] for k in sorted(ents)]
        lines = (ctx.text_at(h, path) or "").splitlines()
        lo = min(ents) - 1
        hi = max(ents) - 1
        while lo > 0 and _ENTRY.match(lines[lo - 1]) if lines else False:
            lo -= 1
        while hi + 1 < len(lines) and _ENTRY.match(lines[hi + 1]):
            hi += 1
        block = [ln.strip().rstrip(",;") for ln in lines[lo: hi + 1]]
        if len(block) < 3:
            ctx.drop("fewer_than_3_entries")
            continue
        if block == sorted(block, key=str.lower):
            ctx.drop("formatter_resort")
            continue
        window = lines[max(0, lo - 4): hi + 3]
        if not any(_ORDER_KW.search(ln) for ln in window):
            ctx.drop("no_order_sensitive_neighbours")
            continue
        cands.append((meta[(h, path)][0], meta[(h, path)][1], h, path, ordered))
    cands.sort()
    last_seq: dict[str, list[int]] = defaultdict(list)
    for seq, _, _, path, _ in cands:
        last_seq[path].append(seq)
    for seq, ts, h, path, ordered in cands:
        again = any(0 < s - seq <= 15 for s in last_seq[path])
        fixes = [c for c in ctx.path_commits(path) if ts < c["ts"] <= ts + 14 * DAY and ctx.is_fix(c) and c["hash"] != h]
        yield Hit(
            subject=f"{h}|{path}",
            score=3 + 2 * len(ordered) * 0.5 + 4 * again + 4 * bool(fixes),
            title=f"{len(ordered)} entries reordered in {path.rsplit('/', 1)[-1]}"
                  f"{' (reordered again soon)' if again else ''}{' (fix followed)' if fixes else ''}",
            evidence={"moved": [t[:80] for t in ordered[:6]], "reordered_again_within_15_commits": again,
                      "fixes_within_14d": [{"hash": c["hash"][:10], "subject": subject_of(c["message"])} for c in fixes[:3]]},
            commits=[h, *[c["hash"] for c in fixes[:2]]], diffs=[(h, path)], paths=[path])


# ---------------------------------------------------------------------------- 23. exception hierarchy reparenting
_CLASS_DECL = re.compile(r"^\s*(?:export\s+)?class\s+(\w*(?:Error|Exception))\s*(?:\(([^)]*)\)|extends\s+([\w.]+))")
_GENERIC_BASES = {"Exception", "BaseException", "Error", "object", "RuntimeError", "ValueError"}


@candidate("exception_hierarchy_reparenting")
def exception_hierarchy_reparenting(ctx: Ctx):
    """An ``*Error``/``*Exception`` class whose base list changed (removed and added ``class X(...)`` /
    ``extends`` in one commit; new intermediate classes appear as the new base). Catch sites (``except``/``catch``/
    ``rescue`` lines in added history that still exist at HEAD) naming a specific old or new base are collected; >=2
    sites with different handler bodies are required. Generic bases (Exception, Error, ...) are not counted as
    sites. Sites in tests, identical handlers and brand-new classes are dropped."""
    decls: dict[tuple, dict[str, dict[str, set]]] = defaultdict(lambda: {"+": {}, "-": {}})
    for r in ctx.grep(_CLASS_DECL.pattern, None, limit=20000):
        m = _CLASS_DECL.match(r["text"])
        if m:
            bases = {b.strip().split("[")[0] for b in (m.group(2) or m.group(3) or "").split(",") if b.strip() and "=" not in b}
            decls[(r["hash"], r["path"], r["seq"])][r["sign"]][m.group(1)] = bases
    reparents = []
    for (h, path, seq), d in decls.items():
        if is_test(path) or is_vendored(path):
            continue
        for cls, old in d["-"].items():
            new = d["+"].get(cls)
            if new is None or new == old:
                continue
            reparents.append((seq, h, path, cls, old, new))
    if not reparents:
        return
    sites = ctx.grep(r"^\s*(except\b|\}?\s*catch\s*\(|rescue\b)", "+", limit=40000)
    for seq, h, path, cls, old, new in sorted(reparents)[-150:]:
        names = {b.split(".")[-1] for b in old | new} - _GENERIC_BASES
        if not names:
            ctx.drop("no_specific_base")
            continue
        rx = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
        found: dict[tuple, tuple] = {}
        site_paths = sorted({x["path"] for x in sites if rx.search(x["text"])})
        for sp in site_paths:
            if is_test(sp):
                ctx.drop("test_helper_site")
                continue
            hl = (ctx.text_at_head(sp) or "").splitlines()
            for idx, ln in enumerate(hl):
                if re.match(r"\s*(except\b|\}?\s*catch\s*\(|rescue\b)", ln) and rx.search(ln):
                    body = " ".join(x.strip() for x in hl[idx + 1: idx + 4])
                    found[(sp, idx)] = (ln.strip(), body, rx.search(ln).group(1))
        if len(found) < 2:
            ctx.drop("fewer_than_2_sites")
            continue
        bodies = {b for _, b, _ in found.values()}
        if len(bodies) < 2:
            ctx.drop("identical_handlers")
            continue
        old_n = sum(1 for _, _, n in found.values() if n in {b.split(".")[-1] for b in old})
        new_n = len(found) - old_n
        yield Hit(
            subject=f"{h}|{cls}",
            score=3 + len(found) + len(bodies),
            title=f"{cls} reparented {sorted(old)} -> {sorted(new)}; {len(found)} catch sites, {len(bodies)} distinct handlers",
            evidence={"class": cls, "old_bases": sorted(old), "new_bases": sorted(new), "sites_naming_old_base": old_n,
                      "sites_naming_new_base": new_n,
                      "sites": [{"path": p, "line": t[:100]} for (p, _), (t, _, _) in list(found.items())[:6]]},
            commits=[h], diffs=[(h, path)], paths=[path, *sorted({p for p, _ in found})[:4]],
            symbols=[u for u in [next((s["uid"] for s in ctx.symbols_by_path.get(path, []) if s["name"] == cls), None)] if u])


# ---------------------------------------------------------------------------- 24. API signature change then call-site fix
_DEF_LINE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function)\s+([A-Za-z]\w*)\s*\((.*)\)")
_ERR_WORDS = re.compile(r"(?i)fix|broken|missing|typeerror|unexpected|argument|signature|adapt|call ?sites?|update (the )?call")


def _seq_at_or_before(ctx: Ctx, ts: int) -> int:
    sc = _scratch(ctx)
    if "ts_seq" not in sc:
        sc["ts_seq"] = ([c["ts"] for c in ctx.commits], [c["seq"] for c in ctx.commits])
    tss, seqs = sc["ts_seq"]
    i = bisect.bisect_right(tss, ts)
    return seqs[i - 1] if i else -1


def _signature_changes_from_lines(ctx: Ctx) -> list:
    """Fallback (no ``symbol_versions``): header lines of one commit, single-line ``def``/``function`` only."""
    defs: dict[tuple, dict[str, dict[str, list]]] = defaultdict(lambda: {"+": defaultdict(list), "-": defaultdict(list)})
    for r in ctx.grep(r"^\s*(export\s+)?(async\s+)?(def|function)\s+\w+\s*\(.*\)", None, limit=60000):
        if is_test(r["path"]) or not is_code(r["path"]):
            continue
        m = _DEF_LINE.match(r["text"])
        if m:
            defs[(r["hash"], r["path"], r["seq"], r["ts"])][r["sign"]][m.group(1)].append(m.group(2))
    cands = []
    for (h, path, seq, ts), d in defs.items():
        for name, olds in d["-"].items():
            news = d["+"].get(name)
            if not news:
                continue
            if name.startswith("_"):
                ctx.drop("private")
                continue
            if len(name) < 5:
                ctx.drop("generic_name")
                continue
            po = params(f"({olds[0]})", _lang(path))
            pn = params(f"({news[0]})", _lang(path))
            if po is None or pn is None:
                continue
            sig_o = [(p.name, p.default, p.kind) for p in po]
            sig_n = [(p.name, p.default, p.kind) for p in pn]
            if sig_o == sig_n:
                continue
            cands.append((seq, ts, h, path, name, sig_o, sig_n))
    return cands


@candidate("api_signature_change_then_callsite_fix")
def api_signature_change_then_callsite_fix(ctx: Ctx):
    """A non-private function's parameter list changed and >=2 later commits within 14 days touch lines calling it in
    files other than its definition. The change is read from the ``symbol_versions`` signature history (one row per
    version of every function/method/class, so multi-line headers, methods and any language Oracle parses are covered;
    renames of the function itself are not changes because the symbol identity differs). Only when a database has no
    ``symbol_versions`` does it fall back to pairing single-line ``def``/``function`` header lines of one commit.
    Callers are found textually (``name(``) so files outside the CALLS graph count too. Dropped when the changing
    commit already touched every file the later commits fixed, for private/test symbols and for names under 5
    characters. Error-wording messages boost."""
    cands = []
    if ctx.versions_by_uid:
        for uid, vs in ctx.versions_by_uid.items():
            if len(vs) < 2 or vs[-1]["kind"] not in ("function", "method"):
                continue
            name = vs[-1]["qualname"].rsplit(".", 1)[-1]
            for a, b in zip(vs, vs[1:]):
                if a["path"] != b["path"] or is_test(b["path"]) or not is_code(b["path"]):
                    continue
                if ctx.by_hash[b["hash"]]["is_bulk"]:
                    continue
                po, pn = params(a["signature"] or "", _lang(b["path"])), params(b["signature"] or "", _lang(b["path"]))
                if po is None or pn is None:
                    continue
                sig_o = [(p.name, p.default, p.kind) for p in po]
                sig_n = [(p.name, p.default, p.kind) for p in pn]
                if sig_o == sig_n:
                    continue
                if name.startswith("_"):
                    ctx.drop("private")
                    continue
                if len(name) < 5:
                    ctx.drop("generic_name")
                    continue
                cands.append((b["seq"], b["ts"], b["hash"], b["path"], name, sig_o, sig_n))
    else:
        cands = _signature_changes_from_lines(ctx)
    for seq, ts, h, path, name, so, sn in sorted(cands, key=lambda c: (c[0] or 0, c[1] or 0), reverse=True)[:200]:  # tuples may hold None
        hi = _seq_at_or_before(ctx, ts + 14 * DAY)
        rows = ctx.q("SELECT DISTINCT hash, path FROM diff_lines WHERE seq > ? AND seq <= ? AND instr(text, ?) > 0",
                     (seq, hi, name + "("))
        later = [r for r in rows if r["path"] != path and not is_test(r["path"]) and not is_docs(r["path"])]
        later_commits = {r["hash"] for r in later}
        if len(later_commits) < 2:
            continue
        own = {r["path"] for r in ctx.q("SELECT DISTINCT path FROM diff_lines WHERE hash=? AND instr(text, ?) > 0",
                                        (h, name + "("))}
        if {r["path"] for r in later} <= own:
            ctx.drop("callers_updated_in_same_commit")
            continue
        errs = [c for c in later_commits if _ERR_WORDS.search(ctx.by_hash[c]["message"] or "")]
        yield Hit(
            subject=f"{h}|{path}|{name}",
            score=3 + 2 * len(later_commits) + 3 * len(errs),
            title=f"{name}({', '.join(p[0] for p in sn)}) changed; {len(later_commits)} follow-up commits fixed callers",
            evidence={"function": name, "old_params": [p[0] for p in so], "new_params": [p[0] for p in sn],
                      "followup_commits": sorted(c[:10] for c in later_commits)[:6],
                      "files_fixed_later": sorted({r["path"] for r in later})[:6], "error_wording": len(errs)},
            commits=[h, *sorted(later_commits, key=lambda c: ctx.by_hash[c]["seq"])[:4]],
            diffs=[(h, path), *[(r["hash"], r["path"]) for r in later[:2]]],
            paths=[path, *sorted({r["path"] for r in later})[:3]],
            symbols=[u for u in [next((s["uid"] for s in ctx.symbols_by_path.get(path, []) if s["name"] == name), None)] if u])


# ---------------------------------------------------------------------------- 25. raise contract change, stale callers
def _py_scopes(text: str) -> list[str | None]:
    """scope[i] = dotted qualname of the innermost def enclosing line i+1 (None if the innermost scope is a class)."""
    stack: list[tuple[int, str, bool]] = []
    out: list[str | None] = []
    for ln in text.splitlines():
        st = ln.strip()
        if st and not st.startswith("#"):
            ind = len(ln) - len(ln.lstrip())
            m = re.match(r"(async\s+def|def|class)\s+(\w+)", st)
            while stack and stack[-1][0] >= ind and (m or True):
                if stack[-1][0] >= ind:
                    stack.pop()
                else:
                    break
            if m:
                stack.append((ind, m.group(2), m.group(1) != "class"))
        out.append(".".join(n for _, n, _ in stack) if stack and stack[-1][2] else None)
    return out


@candidate("raise_contract_change_stale_callers")
def raise_contract_change_stale_callers(ctx: Ctx):
    """A Python function's raised exception class changed (``raise Old`` replaced by ``raise New``), or it swapped
    ``return None`` for a raise (or back). Each changed line is attributed to its function through
    ``diff_lines.symbol`` (exact innermost function in the version the line belongs to, nested defs and methods
    included, no blob reads); only rows without a symbol fall back to re-parsing indentation of the file text.
    HEAD callers (CALLS edges, not typed calls) whose body within 15 lines of the
    call still has ``except Old`` (or an ``is None`` check for None->raise) and which were not modified since are
    listed, at most 5 by their own caller count. Broad ``except Exception``/bare except, private single-caller
    functions and changes where every caller was updated are dropped."""
    by_file: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in ctx.grep(r"\braise\s+[A-Z]\w*|\breturn\s+None\b|^\s*return\s*$", None, path_like="%.py", limit=60000):
        if not is_test(r["path"]):
            by_file[(r["hash"], r["path"], r["seq"])][r["sign"]].append(r)
    done = 0
    for (h, path, seq), d in sorted(by_file.items(), key=lambda kv: -kv[0][2]):
        if done > 300:
            break
        if not (d["+"] and d["-"]):
            continue
        done += 1
        per: dict[str, dict] = defaultdict(lambda: {"old_r": set(), "new_r": set(), "old_none": 0, "new_none": 0})
        scopes: dict[str, list] = {}

        def note(rows, side, which):
            for r in rows:
                uid_ = r["symbol"] or None
                if uid_ is None:  # no enclosing symbol recorded: fall back to indentation scopes of the file text
                    if which not in scopes:
                        t = ctx.text_before(h, path) if which == "old" else ctx.text_at(h, path)
                        scopes[which] = _py_scopes(t) if t else []
                    i = (r["line"] or 0) - 1
                    q = scopes[which][i] if 0 <= i < len(scopes[which]) else None
                    uid_ = f"sym:{ctx.repo_id}:{path}:{q}:function" if q else None
                sym_ = ctx.symbols.get(uid_) if uid_ else None
                if sym_ is None or sym_["kind"] != "function":
                    continue
                m = re.search(r"\braise\s+([A-Z]\w*)", r["text"])
                if m:
                    per[uid_][f"{side}_r"].add(m.group(1))
                else:
                    per[uid_][f"{side}_none"] += 1
        note(d["-"], "old", "old")
        note(d["+"], "new", "new")
        for uid, p in per.items():
            old_r, new_r = p["old_r"] - p["new_r"], p["new_r"] - p["old_r"]
            mode = None
            if old_r and new_r:
                mode = "class_changed"
            elif p["old_none"] and new_r and not p["new_none"]:
                mode = "none_to_raise"
            elif old_r and p["new_none"] and not new_r:
                mode = "raise_to_none"
            if not mode:
                continue
            fn = ctx.symbols.get(uid)
            qual = fn["qualname"]
            if not fn["alive"]:
                continue
            callers = sorted(ctx.callers(uid), key=lambda u: -len(ctx.callers(u)))
            if fn["name"].startswith("_") and len(callers) <= 1:
                ctx.drop("private_single_caller")
                continue
            stale, modified, broad = [], 0, 0
            for cu in callers:
                cs = ctx.symbols.get(cu)
                if cs is None or not cs["body"] or is_test(cs["path"]):
                    continue
                if any(e["seq"] > seq and e["change"] == "modified" for e in ctx.symbol_events(cu)):
                    modified += 1
                    continue
                body = cs["body"].splitlines()
                idx = next((i for i, ln in enumerate(body) if re.search(rf"\b{re.escape(fn['name'])}\s*\(", ln)), None)
                if idx is None:
                    continue
                win = "\n".join(body[max(0, idx - 15): idx + 16])
                hit = False
                if mode in ("class_changed", "raise_to_none"):
                    hit = any(re.search(rf"except\s*\(?[^:\n]*\b{o}\b", win) for o in old_r)
                    if not hit and re.search(r"except\s*:|except\s+(Base)?Exception\b", win):
                        broad += 1
                elif mode == "none_to_raise":
                    hit = bool(re.search(r"\bis\s+(not\s+)?None\b|==\s*None|!=\s*None", "\n".join(body[idx: idx + 16])))
                if hit:
                    stale.append(cs)
            if not stale:
                if broad:
                    ctx.drop("broad_except")
                elif callers and modified == len(callers):
                    ctx.drop("all_callers_modified")
                continue
            stale = stale[:5]
            yield Hit(
                subject=f"{h}|{uid}",
                score=3 + 2 * len(stale) + (1 if mode == "class_changed" else 0),
                title=f"{qual}: {mode.replace('_', ' ')} ({', '.join(sorted(old_r)) or 'None'} -> {', '.join(sorted(new_r)) or 'None'});"
                      f" {len(stale)} caller(s) not updated",
                evidence={"mode": mode, "old_raises": sorted(old_r), "new_raises": sorted(new_r),
                          "stale_callers": [s["uid"] for s in stale], "callers_total": len(callers),
                          "callers_modified_since": modified},
                commits=[h], diffs=[(h, path), *[(h, s["path"]) for s in stale[:0]]], paths=[path, *sorted({s["path"] for s in stale})[:3]],
                symbols=[uid, *[s["uid"] for s in stale]])


# ---------------------------------------------------------------------------- 26. unit change without callers
_UNIT_SFX = r"(?:ms|millis|msec|sec|secs|seconds|us|ns|bytes|kb|mb|gb|minutes|mins|hours|days)"
_UNIT_NAME = re.compile(rf"\b([A-Za-z]\w*?)_({_UNIT_SFX})\b", re.I)
_CONST_LINE = re.compile(r"""^\s*(?:export\s+)?(?:(?:const|final|static|public|private|readonly|val|var|let)\s+)*(?:[A-Za-z_][\w<>\[\]]*\s+)?["']?([A-Za-z_]\w*)["']?\s*[:=]\s*(-?\d[\d_]*(?:\.\d+)?)\s*[,;]?\s*(?:#.*|//.*)?$""")
_RATIOS = {1000, 60, 1024, 3600, 86400, 1000000}
_DISPLAY = re.compile(r"(?i)display|label|format|human|pretty|ui_|_ui\b|^port$|_port$|^port_|http_port")


def _consumer_paths(ctx: Ctx, token: str, defining: str, cache: dict) -> set[str]:
    if token not in cache:
        rows = ctx.q("SELECT DISTINCT path FROM diff_lines WHERE instr(lower(text), ?) > 0", (token.lower(),))
        cache[token] = {r["path"] for r in rows if not is_test(r["path"]) and not is_docs(r["path"])}
        cache[token] |= {r["path"] for r in ctx.contracts if r["key"] == token}
    return cache[token] - {defining}


@candidate("unit_change_without_callers")
def unit_change_without_callers(ctx: Ctx):
    """(1) A unit-suffixed identifier (``timeout_ms`` -> ``timeout_sec``) changed unit in a commit (removed/added lines
    of one file share the stem, differ in suffix); (2) a named numeric constant/default/config literal changed by a
    ratio of 1000/60/1024/3600/86400/1e6 (either direction). Both require >=2 consumer files (other files whose
    changed lines mention the name, or string contracts keyed by it); ``stale_consumers`` lists those the commit did not
    touch. Display/format names, ports and bulk commits (not indexed) are dropped; no conversion-literal heuristic.
    Only the 200 most recent candidates are checked."""
    lines = ctx.grep("(?i)" + _UNIT_NAME.pattern, None, limit=60000)
    by_file: dict[tuple, dict[str, dict[str, set]]] = defaultdict(lambda: {"+": defaultdict(set), "-": defaultdict(set)})
    for r in lines:
        if is_test(r["path"]) or is_docs(r["path"]) or is_vendored(r["path"]) or is_generated(r["path"]):
            continue
        for m in _UNIT_NAME.finditer(r["text"]):
            by_file[(r["hash"], r["path"], r["seq"])][r["sign"]][m.group(1).lower()].add(m.group(2).lower())
    cands = []
    for (h, path, seq), d in by_file.items():
        for stem, olds in d["-"].items():
            news = d["+"].get(stem)
            if not news or news == olds or (news & olds):
                continue
            if _DISPLAY.search(stem):
                ctx.drop("display_conversion")
                continue
            cands.append((seq, h, path, f"unit:{stem}", f"{stem}_{sorted(olds)[0]}", f"{stem}_{sorted(news)[0]}", stem))
    cl: dict[tuple, dict[str, dict]] = defaultdict(lambda: {"+": {}, "-": {}})
    for r in ctx.grep(r"^\s*.*[A-Za-z_]\w*[\"']?\s*[:=]\s*-?\d[\d_]*(\.\d+)?\s*[,;]?\s*(#.*|//.*)?$", None, limit=60000):
        if is_test(r["path"]) or is_docs(r["path"]) or is_vendored(r["path"]) or is_generated(r["path"]) \
                or r["path"].endswith((".lock", ".csv", ".svg")):
            continue
        m = _CONST_LINE.match(r["text"])
        if m:
            cl[(r["hash"], r["path"], r["seq"])][r["sign"]][m.group(1)] = float(m.group(2).replace("_", ""))
    for (h, path, seq), d in cl.items():
        for name, ov in d["-"].items():
            nv = d["+"].get(name)
            if nv is None or ov == nv or ov == 0 or nv == 0:
                continue
            ratio = max(nv / ov, ov / nv)
            if round(ratio, 6) not in _RATIOS:
                continue
            if _DISPLAY.search(name):
                ctx.drop("ports_or_display")
                continue
            cands.append((seq, h, path, f"const:{name}", f"{name}={ov:g}", f"{name}={nv:g}", name))
    cache: dict[str, set[str]] = {}
    for seq, h, path, kind, old, new, token in sorted(cands, key=lambda c: c[0] or 0, reverse=True)[:200]:
        if len(token) < 4:
            continue
        cons = _consumer_paths(ctx, token, path, cache)
        if len(cons) < 2:
            ctx.drop("fewer_than_2_consumers")
            continue
        touched = {fc["path"] for fc in ctx.changes(h)}
        stale = sorted(cons - touched)
        if not stale:
            ctx.drop("consumers_updated_in_same_commit")
            continue
        yield Hit(
            subject=f"{h}|{kind}",
            score=3 + 2 * min(len(stale), 5) + (2 if kind.startswith("unit:") else 1),
            title=f"{old} -> {new}: {len(stale)} of {len(cons)} consumer files not touched",
            evidence={"kind": kind.split(":")[0], "old": old, "new": new, "consumers": sorted(cons)[:8],
                      "stale_consumers": stale[:8]},
            commits=[h], diffs=[(h, path)], paths=[path, *stale[:3]])
