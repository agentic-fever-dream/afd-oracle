"""Catalogue family: team knowledge (comments, docs and ownership that encode, or have lost, institutional memory)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .base import DAY, Ctx, Hit, candidate, is_code, is_docs, is_generated, is_test, is_vendored, subject_of
from .legacy_evolution import CODE_PATH_RX, FORMAT_MSG_RX, _is_comment, _prod

FENCE_RX = (r"(?i)do\s+not\s+(remove|change|reorder|modify|delete|touch)|don'?t\s+(remove|change|reorder|modify|delete|"
            r"touch)|order\s+matters|must\s+be\s+called\s+before|workaround\s+for|load-bearing")
ALNUM = re.compile(r"[A-Za-z0-9]")


def _is_commentish(text: str) -> bool:
    return _is_comment(text) or bool(re.search(r"(#|//)\s*\S", text))


@candidate("chesterton_fence_comment_edited")
def chesterton_fence_comment_edited(ctx: Ctx):
    """A 'do not remove / order matters / must be called before / workaround for / load-bearing' comment whose guarded
    lines (the next 5 non-blank code lines) were later changed. Tiered: change followed by a fix/revert within 90 days
    ranks highest, plain modification lower, and a fence still intact at HEAD is surfaced at the bottom."""
    unviolated = 0
    for r in ctx.grep(FENCE_RX, "+"):
        path, h, text = r["path"], r["hash"], r["text"]
        if is_vendored(path) or is_generated(path) or is_docs(path) or is_test(path) or not _is_commentish(text):
            ctx.drop("vendored_generated_docs_or_not_comment")
            continue
        if re.search(r"(?i)sort(ed|ing)?\s+order|order\s+by|alphabetical", text):
            ctx.drop("sort_order_prose")
            continue
        if re.match(r"^\W*(TODO|FIXME)\b", text.strip(), re.I):
            ctx.drop("todo_only")
            continue
        ft = ctx.text_at(h, path)
        if not ft:
            continue
        fl = ft.splitlines()
        guarded = []
        for k in range(r["line"], min(len(fl), r["line"] + 12)):
            s = fl[k].strip()
            if len(s) >= 6 and ALNUM.search(s) and not _is_comment(s):
                guarded.append(s)
            if len(guarded) >= 5:
                break
        if not guarded:
            continue
        gset = set(guarded)
        removed = ctx.q("SELECT hash, seq, ts, text FROM diff_lines WHERE path=? AND sign='-' AND seq>? ORDER BY seq",
                        (path, r["seq"]))
        changes: dict[str, list[str]] = defaultdict(list)
        fence_removed = set()
        for x in removed:
            if x["text"].strip() in gset:
                changes[x["hash"]].append(x["text"].strip())
            if x["text"].strip() == text.strip():
                fence_removed.add(x["hash"])
        if not changes:
            head = ctx.text_at_head(path)
            if head and text.strip() in head and all(g in head for g in guarded) and unviolated < 60:
                unviolated += 1
                yield Hit(
                    subject=f"{h}|{path}|{r['line']}", score=0.5,
                    title=f"Unviolated fence in {path}: {text.strip()[:80]}",
                    evidence={"fence": text.strip(), "guarded": guarded[:5], "state": "intact_at_head"},
                    commits=[h], diffs=[(h, path)], paths=[path],
                )
            continue
        first = min(changes, key=lambda x: ctx.by_hash[x]["seq"])
        if first in fence_removed:
            ctx.drop("documented_removal")
            continue
        cc = ctx.by_hash[first]
        flagged = ctx.is_fix(cc) or bool(re.match(r"(?i)revert", cc["message"] or ""))
        near = [c for c in ctx.path_commits(path) if 0 <= c["ts"] - cc["ts"] <= 90 * DAY
                and (ctx.is_fix(c) or re.match(r"(?i)revert", c["message"] or ""))]
        tier = 5 if (flagged or near) else 2
        yield Hit(
            subject=f"{h}|{path}|{r['line']}", score=tier + len(changes) * 0.2,
            title=f"Fence '{text.strip()[:70]}' in {path}: guarded lines changed in {first[:10]}"
                  + (" followed by a fix/revert" if tier == 5 else ""),
            evidence={"fence": text.strip(), "guarded": guarded[:5], "changed_in": first,
                      "change_subject": subject_of(cc["message"]), "lines_changed": changes[first][:5],
                      "fix_or_revert_within_90d": [{"hash": c["hash"], "subject": subject_of(c["message"])} for c in near[:4]]},
            commits=[h, first, *[c["hash"] for c in near[:3]]], diffs=[(h, path), (first, path)], paths=[path],
        )


SIGNAL_MSG_RX = re.compile(r"(?i)\b(workaround|hack|do not (remove|change)|must come first|otherwise it breaks|upstream bug|"
                           r"SEV[ -]?\d|outage|incident)\b")
CONSTRAINT_RX = re.compile(r"(?i)\b(do not|don'?t|must|otherwise|because|upstream|due to|breaks?|fails?|bug in|SEV[ -]?\d|"
                           r"outage|incident|p0)\b")
BOT_RX = re.compile(r"(?i)\b(bump|dependabot|renovate|upgrade|update)\b.*\bfrom\b.*\bto\b|\[bot\]")


@candidate("commit_message_signal_hack_workaround")
def commit_message_signal_hack_workaround(ctx: Ctx):
    """A commit whose message flags a workaround / hack / incident, with an explicit constraint phrase or incident token
    in the message, where >=3 added code lines survive at HEAD and no >=5-word comment explains them in code. Boosted
    for config/infra files and for later edits to the file whose messages don't cite the reason."""
    for c in ctx.commits:
        msg = c["message"] or ""
        if c["is_merge"] or c["is_bulk"] or not SIGNAL_MSG_RX.search(msg):
            continue
        if ctx.is_bot(c) or BOT_RX.search(subject_of(msg)):
            ctx.drop("bot_bump")
            continue
        if not CONSTRAINT_RX.search(msg):
            ctx.drop("no_constraint_phrase")
            continue
        if len(re.sub(r"#\d+|\bGH-\d+", "", subject_of(msg)).split()) < 4 and len(msg.splitlines()) < 3:
            ctx.drop("bare_issue_ref")
            continue
        for fc in ctx.changes(c["hash"]):
            path = fc["path"]
            if is_test(path) or is_docs(path) or fc["status"] == "D" or is_vendored(path) or is_generated(path):
                ctx.drop("tests_docs_or_deleted")
                continue
            added, _ = ctx.added_removed(c["hash"], path)
            code = [a.strip() for a in added if len(a.strip()) >= 6 and not _is_comment(a.strip())]
            if len(code) < 3:
                continue
            if any(len(a.split()) >= 5 for a in added if _is_comment(a.strip())):
                ctx.drop("explained_by_comment")
                continue
            head = ctx.text_at_head(path)
            if not head:
                continue
            hset = {x.strip() for x in head.splitlines()}
            alive = [a for a in code if a in hset]
            if len(alive) < 3:
                ctx.drop("lines_not_surviving")
                continue
            kws = {m.group(0).lower() for m in SIGNAL_MSG_RX.finditer(msg)}
            later = [x for x in ctx.path_commits(path) if x["seq"] > c["seq"] and not any(k in (x["message"] or "").lower() for k in kws)]
            infra = bool(re.search(r"(?i)\.(ya?ml|toml|ini|cfg|tf)$|Dockerfile|Makefile|\.github/", path))
            yield Hit(
                subject=f"{c['hash']}|{path}", score=len(alive) * 0.5 + 3 + (2 if infra else 0) + min(3, len(later)) * 0.5,
                title=f"{subject_of(msg)[:80]} — {len(alive)} lines in {path} survive without a code comment",
                evidence={"commit_subject": subject_of(msg), "message_excerpt": msg[:400], "surviving_lines": alive[:6],
                          "later_edits_not_citing_reason": len(later), "config_or_infra": infra},
                commits=[c["hash"], *[x["hash"] for x in later[:2]]], diffs=[(c["hash"], path)], paths=[path],
            )


SYNC_RX = [re.compile(r"(?i)(?:in\s+sync|synced|synchroni[sz]ed|sync)\s+with\s+(?P<t>.+)"),
           re.compile(r"(?i)\b(?:must\s+match|mirrors?|must\s+be\s+identical\s+to|keep\s+(?:this\s+)?(?:\w+\s+)?"
                      r"(?:identical|consistent)\s+with)\s+(?P<t>.+)")]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_]\w{2,}", text))


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _all_old_paths(ctx: Ctx) -> set[str]:
    out = {p for p in ctx.changes_by_path}
    out |= {r["old_path"] for rows in ctx.changes_by_commit.values() for r in rows if r["old_path"]}
    return out


@candidate("keep_in_sync_partner_unchanged")
def keep_in_sync_partner_unchanged(ctx: Ctx):
    """A 'keep in sync with X' / 'must match X' comment whose X resolves exactly to a partner path or unique symbol; since
    the comment the host changed in >=3 commits while the partner moved with fewer than half of them (+/-7 days), or the
    partner path dangles (never existed). High content similarity suppresses the pair."""
    names: dict[str, list] = defaultdict(list)
    for s in ctx.alive_symbols:
        if _prod(s["path"]):
            names[s["name"]].append(s)
    historic = _all_old_paths(ctx)
    seen: set[frozenset] = set()
    for rx in SYNC_RX:
        for r in ctx.grep(rx.pattern, "+"):
            host, h, text = r["path"], r["hash"], r["text"]
            if not _is_commentish(text) or is_vendored(host) or is_generated(host) or is_test(host):
                continue
            m = rx.search(text)
            target = m.group("t")
            if "://" in target or "www." in target:
                ctx.drop("external_url")
                continue
            head = ctx.text_at_head(host)
            if not head or text.strip() not in head:
                ctx.drop("comment_gone_at_head")
                continue
            partner_path = partner_sym = None
            dangling = None
            for tok in re.findall(r"`([^`]+)`|([\w./-]+\.\w{1,5})\b|\b([A-Za-z_]\w{4,})\b", target):
                t = next(x for x in tok if x).strip().rstrip("()")
                if re.search(r"\.\w{1,5}$", t) or "/" in t:
                    base = host.rsplit("/", 1)[0] + "/" + t if "/" in host else t
                    cands = [p for p in (t, base) if p in ctx.head_files]
                    if not cands:
                        cands = [p for p in ctx.head_files if p.endswith("/" + t)]
                    if len(cands) == 1:
                        partner_path = cands[0]
                        break
                    if not cands:
                        if t in historic or any(p.endswith("/" + t) for p in historic):
                            ctx.drop("partner_deleted")
                        else:
                            dangling = t
                elif t in names and len(names[t]) == 1:
                    partner_sym = names[t][0]
                    partner_path = partner_sym["path"]
                    break
            if partner_path is None and dangling is None:
                continue
            key = frozenset((host, partner_path or dangling))
            if key in seen:
                ctx.drop("already_reported_pair")
                continue
            seen.add(key)
            hostcs = [c for c in ctx.path_commits(host) if c["seq"] > r["seq"] and not c["is_bulk"]]
            if dangling and partner_path is None:
                yield Hit(
                    subject=f"{host}|{dangling}", score=3 + len(hostcs) * 0.3,
                    title=f"{host} says to keep in sync with {dangling}, which does not exist",
                    evidence={"comment": text.strip(), "partner": dangling, "state": "dangling",
                              "host_commits_since": len(hostcs)},
                    commits=[h], diffs=[(h, host)], paths=[host],
                )
                continue
            if len(hostcs) < 3 or partner_path == host:
                continue
            pts = [c["ts"] for c in ctx.path_commits(partner_path)]
            moved = sum(1 for c in hostcs if any(abs(t - c["ts"]) <= 7 * DAY for t in pts))
            if moved * 2 >= len(hostcs):
                ctx.drop("partner_moves_with_host")
                continue
            ptext = ctx.text_at_head(partner_path) or ""
            a, b = _tokens(head), _tokens(ptext)
            if partner_sym is not None and partner_sym["body"]:
                b = _tokens(partner_sym["body"])
            sim = _jaccard(a, b)
            if sim >= 0.85:
                ctx.drop("high_content_similarity")
                continue
            yield Hit(
                subject=f"{host}|{partner_sym['uid'] if partner_sym else partner_path}",
                score=len(hostcs) - moved + 2 * (1 - sim),
                title=f"{host} must stay in sync with {partner_path}: host changed {len(hostcs)}x, partner moved with {moved}",
                evidence={"comment": text.strip(), "partner": partner_path, "host_commits_since": len(hostcs),
                          "partner_moved_with": moved, "token_jaccard": round(sim, 2),
                          "only_in_partner": sorted(b - a)[:10], "only_in_host": sorted(a - b)[:10]},
                commits=[h, *[c["hash"] for c in hostcs[-3:]]], diffs=[(hostcs[-1]["hash"], host)],
                paths=[host, partner_path], symbols=[partner_sym["uid"]] if partner_sym else [],
            )


IGNORANCE_RX = (r"(?i)not\s+sure\s+why|no\s+idea\s+why|for\s+historical\s+reasons|don'?t\s+touch|do\s+not\s+touch|"
                r"nobody\s+knows|why\s+does\s+this\s+work")


@candidate("confessed_ignorance_comment_live_code")
def confessed_ignorance_comment_live_code(ctx: Ctx):
    """A 'not sure why / for historical reasons / don't touch' comment still at HEAD in code with fan-in >=3 or >=2 later
    fixes, written by an author with no commit for 12 months before HEAD."""
    last_by_author: dict[str, int] = {}
    for c in ctx.commits:
        last_by_author[c["author_email"]] = max(last_by_author.get(c["author_email"], 0), c["ts"])
    for r in ctx.grep(IGNORANCE_RX, "+"):
        path, text, h = r["path"], r["text"], r["hash"]
        if not _prod(path) or is_vendored(path) or is_generated(path):
            ctx.drop("tests_vendored_generated")
            continue
        if re.match(r"^\W*(TODO|FIXME)\b", text.strip(), re.I) or not _is_commentish(text):
            ctx.drop("todo_or_not_comment")
            continue
        code_part = re.split(r"(#|//)", text, 1)[0]
        if re.search(r"=\s*-?[\d.]+\s*$", code_part.strip()):
            ctx.drop("magic_number")
            continue
        head = ctx.text_at_head(path)
        if not head or text.strip() not in head:
            ctx.drop("gone_at_head")
            continue
        c = ctx.by_hash[h]
        if ctx.head_ts - last_by_author.get(c["author_email"], 0) < 365 * DAY:
            ctx.drop("author_still_active")
            continue
        ln = next((i for i, x in enumerate(head.splitlines(), 1) if x.strip() == text.strip()), None)
        sym = ctx.symbol_at_line(path, ln) if ln else None
        fan_in = len(ctx.callers(sym["uid"])) if sym else 0
        fixes = [f for f in (ctx.fixes_by_uid.get(sym["uid"], []) if sym else
                             [f for f in ctx.fix_events if f["path"] == path]) if f["ts"] > r["ts"]]
        if fan_in < 3 and len(fixes) < 2:
            ctx.drop("low_fan_in_and_few_fixes")
            continue
        yield Hit(
            subject=f"{path}|{text.strip()[:80]}", score=fan_in + 2 * len(fixes) + 1,
            title=f"'{text.strip()[:80]}' in {sym['qualname'] if sym else path} (fan-in {fan_in}, {len(fixes)} later fixes)",
            evidence={"comment": text.strip(), "author": c["author_name"], "written_days_ago": round((ctx.head_ts - r["ts"]) / DAY),
                      "author_last_commit_days_ago": round((ctx.head_ts - last_by_author[c["author_email"]]) / DAY),
                      "fan_in": fan_in, "later_fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:4]]},
            commits=[h, *[f["hash"] for f in fixes[:3]]], diffs=[(h, path)], paths=[path],
            symbols=[sym["uid"]] if sym else [],
        )


DOC_PRIORITY_RX = re.compile(r"(?i)runbook|playbook|ops|(^|/)readme")
ARCHIVED_RX = re.compile(r"(?i)(^|/)(archive|archived|old|deprecated)/|changelog|release[-_ ]?notes|history|news")
PATH_TOKEN_RX = re.compile(r"^[\w.\-/]+\.(py|js|jsx|ts|tsx|go|rb|java|rs|yaml|yml|toml|json|sh|sql|cfg|ini|php|cs|kt)$")


@candidate("doc_identifier_orphan")
def doc_identifier_orphan(ctx: Ctx):
    """A README / doc / runbook with backticked paths or symbols that are absent at HEAD but known to history (path in the
    rename/delete lineage, or a dead symbol) and removed after the doc's last edit. One hit per doc, ranked by orphan
    count with runbooks/READMEs first.

    Also evaluated: backticked CLI flags (``--flag``) and env vars / config keys (``UPPER_SNAKE``) via the string
    contracts. A token qualifies when no HEAD contract (``ctx.contracts``: any kind, producer or consumer) has that key
    and history shows it was removed: its last changed line outside docs is a removal, made after the doc's last edit
    (one ``diff_lines`` scan for all candidate tokens). Reported with kind ``flag`` / ``env``. Fallback: when the repo
    has no HEAD contracts at all, only the path / symbol orphans above are produced."""
    alive_names = {s["name"] for s in ctx.alive_symbols}
    dead: dict[str, object] = {}
    for s in ctx.symbols.values():
        if not s["alive"] and s["name"] not in alive_names:
            if s["name"] not in dead or (s["last_ts"] or 0) > (dead[s["name"]]["last_ts"] or 0):
                dead[s["name"]] = s
    path_gone: dict[str, int] = {}
    for p, rows in ctx.changes_by_path.items():
        if p not in ctx.head_files and rows:
            path_gone[p] = max(ctx.by_hash[r["hash"]]["ts"] for r in rows if r["hash"] in ctx.by_hash)
    for rows in ctx.changes_by_commit.values():
        for r in rows:
            if r["old_path"] and r["old_path"] not in ctx.head_files and r["hash"] in ctx.by_hash:
                path_gone[r["old_path"]] = max(path_gone.get(r["old_path"], 0), ctx.by_hash[r["hash"]]["ts"])
    docs = [p for p in ctx.head_files if re.search(r"(?i)\.(md|rst|adoc|txt)$", p) and is_docs(p)]
    head_keys = {c["key"] for c in ctx.contracts}
    pending: list[tuple[str, str, str, int]] = []      # (doc, kind, token, last_edit_ts) resolved after one scan
    per_doc: dict[str, tuple] = {}
    for doc in sorted(docs):
        if ARCHIVED_RX.search(doc):
            ctx.drop("changelog_or_archived")
            continue
        text = ctx.text_at_head(doc) or ""
        if re.search(r"(?im)status[\s:*_#\-]*?(superseded|deprecated|rejected)\b", text):
            ctx.drop("superseded_adr")
            continue
        pcs = ctx.path_commits(doc)
        if not pcs:
            continue
        last_edit = pcs[-1]
        orphans = []
        for tok in dict.fromkeys(re.findall(r"`([^`\n]{3,80})`", text)):
            t = tok.strip().rstrip("()")
            if head_keys:
                ct = re.split(r"[=\s]", t, maxsplit=1)[0]
                if re.fullmatch(r"--[a-z][\w-]{1,40}", ct) and ct not in head_keys:
                    pending.append((doc, "flag", ct, last_edit["ts"]))
                elif re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]{1,40}", ct) and ct not in head_keys:
                    pending.append((doc, "env", ct, last_edit["ts"]))
            if " " in t or t.startswith(("-", "$", "http", "~")):
                continue
            if PATH_TOKEN_RX.match(t) or ("/" in t and re.search(r"\.\w+$", t)):
                if t in ctx.head_files or any(p.endswith("/" + t) for p in ctx.head_files):
                    continue
                rel = doc.rsplit("/", 1)[0] + "/" + t if "/" in doc else t
                cand = next((p for p in (t, rel) if p in path_gone), None) or \
                    next((p for p in path_gone if p.endswith("/" + t)), None)
                if cand and path_gone[cand] > last_edit["ts"]:
                    orphans.append(("path", t, path_gone[cand]))
            elif re.fullmatch(r"[A-Za-z_]\w{4,}", t) and ("_" in t or re.search(r"[a-z][A-Z]", t)):
                s = dead.get(t)
                if s and (s["last_ts"] or 0) > last_edit["ts"] and not re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", "\n".join(
                        (ctx.text_at_head(p) or "") for p in ctx.symbols_by_path if p == s["path"])):
                    orphans.append(("symbol", t, s["last_ts"]))
        per_doc[doc] = (last_edit, orphans)
    if pending:
        toks = sorted({t for _, _, t, _ in pending})
        last_minus: dict[str, int] = {}
        last_plus: dict[str, int] = {}
        rx = "|".join(re.escape(t) + r"(?![\w-])" for t in toks)
        for r in ctx.grep("(?<![\\w-])(?:" + rx + ")", None):
            if is_docs(r["path"]):
                continue
            for t in toks:
                if re.search(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", r["text"]):
                    d = last_minus if r["sign"] == "-" else last_plus
                    d[t] = max(d.get(t, 0), r["ts"])
        for doc, kind, t, edit_ts in pending:
            rm = last_minus.get(t, 0)
            if rm > edit_ts and rm > last_plus.get(t, 0):
                per_doc[doc][1].append((kind, t, rm))
    for doc in sorted(per_doc):
        last_edit, orphans = per_doc[doc]
        if not orphans:
            continue
        weight = 2 if DOC_PRIORITY_RX.search(doc) else 1
        yield Hit(
            subject=doc, score=len(orphans) * weight + (3 if weight == 2 else 0),
            title=f"{doc} references {len(orphans)} identifiers that no longer exist",
            evidence={"doc": doc, "last_doc_edit": last_edit["hash"],
                      "orphans": [{"kind": k, "token": t, "removed_days_after_doc_edit": round((ts - last_edit["ts"]) / DAY)}
                                  for k, t, ts in orphans[:12]]},
            commits=[last_edit["hash"]], diffs=[(last_edit["hash"], doc)], paths=[doc],
        )


REF_RX = re.compile(r"(?i)\b(?:see|same\s+as|copied\s+from|defined\s+in|ported\s+from|taken\s+from|mirrors?)\s+"
                    r"[`'\"]?(?P<t>[\w./-]+\.\w{1,5}|[A-Za-z_]\w{4,}(?:\(\))?)")
LINE_REF_RX = re.compile(r"(?i)\bline\s+(\d{1,5})\b")


@candidate("comment_reference_decay")
def comment_reference_decay(ctx: Ctx):
    """A comment saying see / same as / copied from / defined in <path or symbol> where history shows that target was
    deleted or renamed away; secondary: a 'line N' reference in a file edited several times since (net line shifts).

    Closed-issue variant: an added comment that cites an issue (``#N``, ``issues/N``, ``GH-N``) with a pending marker
    (TODO/FIXME/HACK/XXX/workaround/until/temporary/blocked ...), where ``ctx.discussions[N]`` is an issue that is now
    closed, and the comment text is still at HEAD (the commented code remains). Needs the opt-in GitHub sync
    (``afd-oracle discussions``); with no discussions this part is silently skipped."""
    yield from _closed_issue_decay(ctx)
    alive_names = {s["name"] for s in ctx.alive_symbols}
    dead = {s["name"]: s for s in ctx.symbols.values() if not s["alive"] and s["name"] not in alive_names and len(s["name"] or "") >= 5}
    renamed = {}
    gone = set()
    for rows in ctx.changes_by_commit.values():
        for r in rows:
            if r["old_path"]:
                renamed[r["old_path"]] = (r["path"], r["hash"])
            if r["status"] == "D":
                gone.add((r["path"], r["hash"]))
    deleted = {p: h for p, h in gone}
    for r in ctx.grep("(?i)(?:" + REF_RX.pattern.replace("(?i)", "") + ")|(?:" + LINE_REF_RX.pattern.replace("(?i)", "") + ")", "+", path_regex=CODE_PATH_RX):
        path, text, h = r["path"], r["text"], r["hash"]
        if not _is_commentish(text) or not _prod(path) or is_vendored(path) or is_generated(path):
            ctx.drop("tests_vendored_generated_or_not_comment")
            continue
        if re.search(r"(?i)https?://|www\.|copyright|license", text):
            ctx.drop("url_or_license")
            continue
        head = ctx.text_at_head(path)
        if not head or text.strip() not in head:
            ctx.drop("comment_gone_at_head")
            continue
        m = REF_RX.search(text)
        if m:
            t = m.group("t").rstrip("()")
            gone_by = None
            if "." in t or "/" in t:
                if t in ctx.head_files or any(p.endswith("/" + t) for p in ctx.head_files):
                    continue
                full = next((p for p in list(deleted) + list(renamed) if p == t or p.endswith("/" + t)), None)
                if full in renamed:
                    gone_by = ("renamed", renamed[full][1], f"renamed to {renamed[full][0]}")
                elif full in deleted:
                    gone_by = ("deleted", deleted[full], "deleted")
            elif t in dead:
                s = dead[t]
                gone_by = ("symbol_removed", s["last_seen"], f"symbol {s['qualname']} no longer exists")
            if gone_by:
                yield Hit(
                    subject=f"{path}|{text.strip()[:80]}", score=4 + (1 if gone_by[0] == "deleted" else 0),
                    title=f"{path} points at {t}, which was {gone_by[2]}",
                    evidence={"comment": text.strip(), "target": t, "status": gone_by[0], "detail": gone_by[2],
                              "removal_commit": gone_by[1]},
                    commits=[h, gone_by[1]] if gone_by[1] in ctx.by_hash else [h], diffs=[(h, path)], paths=[path],
                )
            continue
        lm = LINE_REF_RX.search(text)
        if lm:
            later = [x for x in ctx.changes_by_path.get(path, []) if ctx.by_hash.get(x["hash"]) and ctx.by_hash[x["hash"]]["seq"] > r["seq"]
                     and (x["added"] or 0) != (x["deleted"] or 0)]
            if len(later) >= 3:
                yield Hit(
                    subject=f"{path}|{text.strip()[:80]}", score=1 + 0.3 * len(later),
                    title=f"{path} comment refers to 'line {lm.group(1)}' but the file shifted in {len(later)} later commits",
                    evidence={"comment": text.strip(), "line_reference": int(lm.group(1)), "later_shifting_commits": len(later)},
                    commits=[h, later[-1]["hash"]], diffs=[(h, path)], paths=[path],
                )


_ISSUE_REF_RX = re.compile(r"(?:#|\bissues?/|\bGH-|\bgh-)(\d{1,6})\b")
_PENDING_RX = re.compile(r"(?i)\b(todo|fixme|hack|xxx|workaround|until|temporary|temporarily|blocked|pending|waiting|"
                         r"remove\s+(?:once|when|after)|revisit|re-?enable)\b")


def _closed_issue_decay(ctx: Ctx):
    closed = {n: d for n, d in ctx.discussions.items()
              if (d["kind"] or "").lower() == "issue" and (d["state"] or "").lower() == "closed"}
    if not closed:
        return
    seen: set[tuple[str, str]] = set()
    for r in ctx.grep(r"(?:#|issues?/|GH-|gh-)\d{1,6}\b", "+", path_regex=CODE_PATH_RX):
        path, text = r["path"], r["text"]
        if not _is_commentish(text) or not _prod(path) or is_vendored(path) or is_generated(path):
            continue
        if not _PENDING_RX.search(text) or re.search(r"(?i)https?://", text):
            continue
        nums = [int(n) for n in _ISSUE_REF_RX.findall(text) if int(n) in closed]
        if not nums or (path, text.strip()) in seen:
            continue
        head = ctx.text_at_head(path)
        if not head or text.strip() not in head:
            ctx.drop("comment_gone_at_head")
            continue
        seen.add((path, text.strip()))
        d = closed[nums[0]]
        yield Hit(
            subject=f"{path}|issue#{nums[0]}|{text.strip()[:60]}", score=5 + (1 if "todo" in text.lower() else 0),
            title=f"{path} comment waits on issue #{nums[0]}, which is closed ({(d['title'] or '')[:60]})",
            evidence={"comment": text.strip(), "issue": nums[0], "issue_title": d["title"], "issue_state": d["state"],
                      "closed_at": d["closed_at"], "status": "issue_closed"},
            commits=[r["hash"]], diffs=[(r["hash"], path)], paths=[path],
        )


def _identities(ctx: Ctx) -> dict[str, str]:
    """email -> canonical identity (emails sharing a name, or a normalised local part, are merged)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for c in ctx.commits:
        e = (c["author_email"] or "").lower()
        union("e:" + e, "n:" + (c["author_name"] or "").strip().lower())
        union("e:" + e, "l:" + re.sub(r"\+.*$|[^a-z]", "", e.split("@")[0]))
    return {(c["author_email"] or "").lower(): find("e:" + (c["author_email"] or "").lower()) for c in ctx.commits}


@candidate("orphaned_ownership_legacy_core")
def orphaned_ownership_legacy_core(ctx: Ctx):
    """A well-worn file (>=20 commits or top-decile import in-degree) where one identity (aliases merged, bots skipped)
    made >=70% of commits, has been inactive >=12 months, a non-owner made a fix-class commit in the last 12 months,
    and the departed owner left >=3 non-trivial commit messages on the file."""
    humans = [c for c in ctx.commits if not ctx.is_bot(c)]
    if len(humans) < 2 or ctx.head_ts - min(c["ts"] for c in humans) < 365 * DAY:
        ctx.drop("repo_too_young")
        return
    ident = _identities(ctx)
    if len({ident[(c["author_email"] or "").lower()] for c in humans}) < 2:
        ctx.drop("single_author_repo")
        return
    last_active: dict[str, int] = {}
    for c in humans:
        i = ident[(c["author_email"] or "").lower()]
        last_active[i] = max(last_active.get(i, 0), c["ts"])
    indeg = sorted(len(v) for v in ctx.imports[1].values())
    decile = indeg[int(len(indeg) * 0.9)] if indeg else 10 ** 9
    for path in sorted(ctx.head_files):
        if not _prod(path):
            continue
        cs = [c for c in ctx.path_commits(path) if not ctx.is_bot(c) and not c["is_merge"]]
        n_in = len(ctx.imports[1].get(path, ()))
        if len(cs) < 20 and not (n_in >= decile and n_in >= 3 and len(cs) >= 8):
            ctx.drop("small_file")
            continue
        share = Counter(ident[(c["author_email"] or "").lower()] for c in cs)
        owner, n = share.most_common(1)[0]
        if n < 0.7 * len(cs):
            continue
        if ctx.head_ts - last_active[owner] < 365 * DAY:
            continue
        rec_fix = [c for c in cs if ctx.is_fix(c) and ctx.head_ts - c["ts"] <= 365 * DAY
                   and ident[(c["author_email"] or "").lower()] != owner]
        if not rec_fix:
            continue
        msgs = [c for c in cs if ident[(c["author_email"] or "").lower()] == owner and len(subject_of(c["message"])) >= 20
                and not FORMAT_MSG_RX.search(subject_of(c["message"]))]
        if len(msgs) < 3:
            ctx.drop("few_substantive_owner_messages")
            continue
        yield Hit(
            subject=path, score=len(cs) * 0.1 + 5 * n / len(cs) + 2 * len(rec_fix) + n_in * 0.2,
            title=f"{path}: {n}/{len(cs)} commits by an owner gone {round((ctx.head_ts - last_active[owner]) / DAY)} days; "
                  f"{len(rec_fix)} recent fixes by others",
            evidence={"owner": msgs[0]["author_name"], "owner_share": round(n / len(cs), 2), "commits": len(cs),
                      "owner_inactive_days": round((ctx.head_ts - last_active[owner]) / DAY), "import_in_degree": n_in,
                      "recent_non_owner_fixes": [{"hash": c["hash"], "subject": subject_of(c["message"])} for c in rec_fix[:4]],
                      "owner_messages": [subject_of(c["message"]) for c in msgs[-5:]]},
            commits=[*[c["hash"] for c in rec_fix[:3]], *[c["hash"] for c in msgs[-2:]]],
            diffs=[(c["hash"], path) for c in rec_fix[:2]], paths=[path],
        )
