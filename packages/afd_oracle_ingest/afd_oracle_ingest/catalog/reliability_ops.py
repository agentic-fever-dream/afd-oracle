"""Catalogue family: reliability and operations (retries, timeouts, deploy/config/ops lineage, release and hotfix chains).

Everything here searches history through the ``diff_lines`` index (``ctx.grep``) and the commit / file_change tables.
Where the spec needs data Oracle does not hold (blame, linked PR titles, resolved retry semantics, tag deletions) the
builder's docstring says which proxy it uses.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from .base import (CI_RX, DAY, DEP_MANIFEST_RX, IAC_RX, K8S_RX, LOCKFILE_RX, MIGRATION_RX, Ctx, Hit, candidate,
                   is_ci, is_code, is_config, is_docs, is_fix_message, is_generated, is_test, is_vendored,
                   subject_of)

# ---------------------------------------------------------------------------- shared helpers
BUMP_RX = re.compile(r"(?i)^\s*(\[?(bump|release|prepare release|version|chore\(release\))\b|v?\d+\.\d+(\.\d+)?\s*$)")
SCRIPT_DIR_RX = re.compile(r"(?i)(^|/)(scripts?|tools?|bin|examples?|benchmarks?|docs?|contrib|hack|ci)/")
INFRA_DEV_RX = re.compile(r"(?i)(^|/)(dev|test|tests|local|ci|e2e|sandbox)[^/]*/|(^|[/._-])(dev|test|local|ci|e2e)[\w.-]*\.(ya?ml|dockerfile)$|"
                          r"dockerfile[.-](dev|test|local|ci)")
NET_RX = re.compile(r"(?i)\b(requests|httpx|aiohttp|urllib3?|urlopen|socket|boto3?|botocore|redis|kafka|pika|amqp|celery|"
                    r"publish|enqueue|send_task|\.send\(|http|fetch|axios|grpc|client\.|session\.|psycopg|pymysql|"
                    r"sqlalchemy|\.execute\(|smtplib|sqs|sns|connect\()")
DB_HTTP_RX = re.compile(r"(?i)\b(requests|httpx|aiohttp|urllib3?|urlopen|psycopg2?|pymysql|sqlalchemy|session\.|\.execute\(|"
                        r"redis|boto3|cursor|db\.|\.query\(|\.objects\.|http|fetch|axios|psql|curl|wget)")


def _ro(ctx: Ctx) -> dict:
    """Per-run scratch cache attached to the ctx."""
    d = getattr(ctx, "_ro_cache", None)
    if d is None:
        d = ctx._ro_cache = {}  # type: ignore[attr-defined]
    return d


def _idx(ctx: Ctx) -> dict[str, int]:
    c = _ro(ctx)
    if "idx" not in c:
        c["idx"] = {x["hash"]: i for i, x in enumerate(ctx.commits)}
    return c["idx"]


def _near(ctx: Ctx, sha: str, k: int):
    i = _idx(ctx).get(sha)
    if i is None:
        return []
    return ctx.commits[max(0, i - k): i + k + 1]


def _ok_commit(ctx: Ctx, c) -> bool:
    return not c["is_merge"] and not ctx.is_bot(c)


def _is_fixish(ctx: Ctx, c) -> bool:
    return bool(c["is_revert"]) or ctx.is_fix(c) or bool(re.match(r"(?i)revert\b", subject_of(c["message"])))


def _syms_in(ctx: Ctx, sha: str, path: str):
    out = []
    for e in ctx.events_by_commit.get(sha, []):
        if e["path"] == path and e["change"] in ("created", "modified", "moved"):
            s = ctx.symbols.get(e["uid"])
            if s is not None:
                out.append(s)
    return out


def _head_lines(ctx: Ctx, path: str) -> list[str]:
    t = ctx.text_at_head(path)
    return t.splitlines() if t else []


def _sym_text(ctx: Ctx, s, pad: int = 6) -> str:
    lines = _head_lines(ctx, s["path"])
    if not lines or not s["start_line"]:
        return s["body"] or ""
    return "\n".join(lines[max(0, s["start_line"] - 1 - pad): s["end_line"] or s["start_line"]])


def _sym(r):
    """diff_lines.symbol of a row: uid of the innermost function/class holding the line, '' for module level / not code,
    None when the index predates enrichment (callers then fall back to file-level reasoning)."""
    try:
        return r["symbol"]
    except (IndexError, KeyError):
        return None


def _row_syms(ctx: Ctx, rows, sha: str, path: str) -> list[str]:
    """Exact uids of the symbols that contain the given changed lines (diff_lines.symbol); falls back to every symbol the
    commit touched in the file when the rows carry no symbol value."""
    vals = [_sym(r) for r in rows]
    if any(v is None for v in vals):
        return [s["uid"] for s in _syms_in(ctx, sha, path)]
    return _dedup([v for v in vals if v])


def _tags(ctx: Ctx) -> list[tuple[str, str, int]]:
    """(name, commit hash, ts) of release tags that point at an ingested commit, oldest first. Source: ``ctx.tags``
    (enrichment table). Fallback, only when that table is empty/missing: read the refs with pygit2."""
    c = _ro(ctx)
    if "tags" in c:
        return c["tags"]
    out = []
    try:
        out = [(t["name"], t["commit_hash"], t["ts"]) for t in ctx.tags if t["commit_hash"] in ctx.by_hash]
    except Exception:
        out = []
    if out:
        out.sort(key=lambda t: (t[2], t[0]))
        c["tags"] = out
        return out
    try:
        for ref in ctx.repo.references:
            if not ref.startswith("refs/tags/"):
                continue
            try:
                obj = ctx.repo.references[ref].peel()
                sha = str(obj.id)
            except Exception:
                continue
            row = ctx.by_hash.get(sha)
            if row is not None:
                out.append((ref[len("refs/tags/"):], sha, row["ts"]))
    except Exception:
        pass
    out.sort(key=lambda t: (t[2], t[0]))
    c["tags"] = out
    return out


def _paths_of(ctx: Ctx, sha: str) -> list[str]:
    return [fc["path"] for fc in ctx.changes(sha)]


def _dedup(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ---------------------------------------------------------------------------- 1. keep-alive / 502
KA_RX = r"(?i)keep-?alive(_?timeout)?|headersTimeout|idle_?timeout|keepalive_timeout|proxy_read_timeout|tcp_?keepalive|" \
        r"--keep-alive"
SYMPTOM_RX = re.compile(r"(?i)\b(502|504|econnreset|bad gateway|gateway time-?out|connection reset|socket hang ?up|"
                        r"upstream (prematurely )?closed|epipe)\b")


@candidate("app_keepalive_502_fix_lineage")
def app_keepalive_502_fix_lineage(ctx: Ctx):
    """A keep-alive / idle-timeout key changed within +/-5 commits of a 502/504/ECONNRESET-worded commit message, or
    of a commit linked (``ctx.discussions_for``) to a PR/issue whose title or body has such wording. Linked PR/issue
    text needs the opt-in ``afd-oracle discussions`` sync; without it only neighbouring commit messages are used
    (the original behaviour). "Dropped by a framework migration" is approximated by a removal of the key with no
    re-add in the same commit."""
    by_commit: dict[str, list] = defaultdict(list)
    for r in ctx.grep(KA_RX, None, limit=40000):
        by_commit[r["hash"]].append(r)
    for sha, rows in by_commit.items():
        c = ctx.by_hash[sha]
        paths = {r["path"] for r in rows}
        if all(is_test(p) or is_docs(p) for p in paths):
            ctx.drop("tests_or_docs")
            continue
        rows = [r for r in rows if not (is_test(r["path"]) or is_docs(r["path"]) or DEP_MANIFEST_RX.search(r["path"])
                                         or LOCKFILE_RX.search(r["path"]) or is_generated(r["path"]))]
        if not rows:
            ctx.drop("dep_bump_only")
            continue
        near = _near(ctx, sha, 5)
        sym = [n for n in near if SYMPTOM_RX.search(n["message"] or "")]
        prs: dict[int, object] = {}
        if ctx.discussions:
            for n in near:
                for d in ctx.discussions_for(n["hash"]):
                    if d["number"] not in prs and SYMPTOM_RX.search(f"{d['title'] or ''}\n{d['body'] or ''}"):
                        prs[d["number"]] = d
        if not sym and not prs:
            continue
        added = [r for r in rows if r["sign"] == "+"]
        removed = [r for r in rows if r["sign"] == "-"]
        dropped = bool(removed) and not added
        yield Hit(
            subject=sha,
            score=len(sym) * 2 + len(rows) * 0.2 + (1.5 if dropped else 0) + min(len(prs), 3) * 2,
            title=f"Keep-alive setting changed near 502/ECONNRESET talk: {subject_of(c['message'])[:90]}",
            evidence={"lines": [f"{r['sign']} {r['path']}: {r['text'].strip()[:120]}" for r in rows[:8]],
                      "symptom_commits": [{"hash": n["hash"], "subject": subject_of(n["message"])} for n in sym[:5]],
                      "setting_removed_without_replacement": dropped,
                      "symptom_prs": [{"number": d["number"], "kind": d["kind"], "title": (d["title"] or "")[:100],
                                       "state": d["state"]} for d in list(prs.values())[:4]]},
            commits=_dedup([sha, *[n["hash"] for n in sym[:4]]]),
            diffs=[(sha, p) for p in sorted({r["path"] for r in rows})[:3]],
            paths=sorted({r["path"] for r in rows})[:6],
        )


# ---------------------------------------------------------------------------- 2. release workflow fix chains
RELEASE_RX = re.compile(r"(?i)(^|/)(\.github/workflows/[^/]*(release|publish|deploy|pypi|dist|tag|package)[^/]*\.ya?ml|"
                        r"\.goreleaser[^/]*|release[^/]*\.(ya?ml|sh|py|cfg)|publish[^/]*\.(ya?ml|sh|py)|"
                        r"\.gitlab-ci\.ya?ml|Jenkinsfile|(tools|scripts)/release[^/]*|MANIFEST\.in|\.releaserc[^/]*)$")
RETAG_RX = re.compile(r"(?i)\b(re-?tag|re-?push(ed)? tag|move tag|delete[d]? tag|force[- ]push tag|tag again)\b")


def _iso_ts(v) -> int | None:
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _release_anomalies(ctx: Ctx, lo: int, hi: int) -> list[dict]:
    """GitHub releases created/published in [lo, hi] that look re-done: their tag is gone from ``ctx.tags`` (deleted
    after publishing), or the tag's commit is newer than the release's creation (tag moved / release re-created)."""
    rels = ctx.github_releases
    if not rels:
        return []
    known = {t["name"]: t for t in ctx.tags} or {t[0]: {"ts": t[2]} for t in _tags(ctx)}
    out = []
    for r in rels:
        created, pub = _iso_ts(r["created_at"]), _iso_ts(r["published_at"])
        stamps = [x for x in (created, pub) if x is not None]
        if not stamps or not any(lo <= x <= hi for x in stamps):
            continue
        t = known.get(r["tag"])
        if t is None:
            out.append({"tag": r["tag"], "problem": "tag_deleted_after_release", "draft": bool(r["draft"])})
        elif created is not None and t["ts"] > created + 3600:
            out.append({"tag": r["tag"], "problem": "tag_commit_newer_than_release",
                        "hours": round((t["ts"] - created) / 3600, 1), "draft": bool(r["draft"])})
    return out


@candidate("release_workflow_edit_fix_chain")
def release_workflow_edit_fix_chain(ctx: Ctx):
    """>=3 fix-message commits to one release file within 24h (sliding window). Boosters: a release tag within the window
    (from ``ctx.tags``; pygit2 refs only if that table is empty), version-bump commit afterwards, and GitHub release
    evidence (``ctx.github_releases``, opt-in sync): a release created/published around the chain whose tag no longer
    exists in ``ctx.tags`` (published then deleted), or whose tag now sits on a commit newer than the release's
    creation (tag moved / release re-created). Without release rows, re-tag wording in commit messages is the fallback
    proxy for deleted/re-pushed tags (it is still a booster when releases exist)."""
    tags = _tags(ctx)
    tag_ts = [t[2] for t in tags]
    for path in sorted(p for p in ctx.changes_by_path if RELEASE_RX.search(p)):
        cs = [c for c in ctx.path_commits(path)]
        if len(cs) < 2:
            continue
        first = cs[0]["hash"]
        fixes = []
        for c in cs:
            if c["hash"] == first:
                ctx.drop("initial_creation")
            elif ctx.is_bot(c):
                ctx.drop("bot")
            elif BUMP_RX.search(subject_of(c["message"])):
                ctx.drop("version_bump")
            elif is_fix_message(c["message"]) or re.search(r"(?i)\b(retry|again|oops|typo in|try(ing)? to)\b",
                                                          subject_of(c["message"])):
                fixes.append(c)
        best: list = []
        j = 0
        for i in range(len(fixes)):
            while fixes[i]["ts"] - fixes[j]["ts"] > DAY:
                j += 1
            if i - j + 1 > len(best):
                best = fixes[j:i + 1]
        if len(best) < 3:
            continue
        lo, hi = best[0]["ts"], best[-1]["ts"]
        near_tags = [t[0] for t in tags if lo - DAY <= t[2] <= hi + DAY]
        bump_after = [c for c in ctx.commits if hi < c["ts"] <= hi + 2 * DAY and BUMP_RX.search(subject_of(c["message"]))]
        retag = [c for c in best if RETAG_RX.search(c["message"] or "")]
        rel_anom = _release_anomalies(ctx, lo - DAY, hi + 2 * DAY)
        yield Hit(
            subject=f"{path}|{best[0]['hash'][:12]}",
            score=len(best) * 2 + (2 if near_tags else 0) + (1.5 if bump_after else 0) + (2 if retag else 0)
            + min(len(rel_anom), 2) * 2.5,
            title=f"{len(best)} fix commits to {path} within {round((hi - lo) / 3600, 1)}h"
                  + (f" around tag {near_tags[0]}" if near_tags else ""),
            evidence={"commits": [{"hash": c["hash"], "subject": subject_of(c["message"])} for c in best[:8]],
                      "span_hours": round((hi - lo) / 3600, 1), "tags_nearby": near_tags[:4],
                      "version_bump_after": [subject_of(c["message"]) for c in bump_after[:2]],
                      "retag_wording": [c["hash"] for c in retag], "github_release_anomalies": rel_anom[:4]},
            commits=[c["hash"] for c in best[:8]], diffs=[(c["hash"], path) for c in best[:3]], paths=[path],
        )


# ---------------------------------------------------------------------------- 3. retry without backoff
RETRY_ADD_RX = (r"@retry\b|\btenacity\b|@backoff\.|backoff\.on_exception|\bretry\(|max_retries|retries\s*=|"
                r"for\s+\w+\s+in\s+range\(\s*(max_)?(retries|attempts?|tries|\d+)|autoretry_for|while\s+.*(retry|attempt|tries)|"
                r"\bretry_on\b|Retry\(")
BACKOFF_WORD_RX = re.compile(r"(?i)backoff|jitter|exponential|wait_random|wait_exponential|\*\*\s*(attempt|retry|i\b|n\b)|"
                             r"\*=\s*2|random\.|max_delay|retry_after|min\(.*sleep|sleep\(.*\*\*")


@candidate("retry_without_backoff_jitter")
def retry_without_backoff_jitter(ctx: Ctx):
    """A commit adds a retry construct inside a symbol that calls a network/queue client; the HEAD symbol text lacks
    backoff/jitter/cap words; and either a later fix touched the symbol or backoff was added later in the same function.
    The symbol is the one containing the added retry line (``diff_lines.symbol``), not every symbol the commit touched in
    the file, and "later backoff" must carry that symbol's uid. Fallback to commit-touched symbols / same file only for
    rows without a symbol value (index not enriched)."""
    later_backoff: dict[str, list] = defaultdict(list)
    for r in ctx.grep(r"(?i)backoff|jitter|exponential|wait_random", "+", limit=30000):
        later_backoff[r["path"]].append(r)
    seen = set()
    for r in ctx.grep(RETRY_ADD_RX, "+", limit=30000):
        path = r["path"]
        if not is_code(path):
            continue
        if is_test(path) or SCRIPT_DIR_RX.search(path) or is_ci(path):
            ctx.drop("tests_scripts_or_ci")
            continue
        rs = _sym(r)
        cands = _syms_in(ctx, r["hash"], path) if rs is None else ([ctx.symbols[rs]] if rs in ctx.symbols else [])
        for s in cands:
            if (s["uid"], r["hash"]) in seen or not s["alive"] or s["kind"] not in ("function", "method"):
                continue
            seen.add((s["uid"], r["hash"]))
            text = _sym_text(ctx, s)
            if not re.search(RETRY_ADD_RX, text):
                continue
            if not NET_RX.search(text):
                ctx.drop("no_network_client" if not re.search(r"(?i)lock|flock|acquire", text) else "local_lock")
                continue
            if BACKOFF_WORD_RX.search(text):
                ctx.drop("has_backoff")
                continue
            fixes = [f for f in ctx.fixes_by_uid.get(s["uid"], []) if f["ts"] > r["ts"]]
            lb = [x for x in later_backoff.get(path, []) if x["seq"] > r["seq"]
                  and (rs is None or _sym(x) in (None, s["uid"]))]
            if not fixes and not lb:
                ctx.drop("no_later_fix_or_backoff")
                continue
            yield Hit(
                subject=f"{s['uid']}|{r['hash'][:12]}",
                score=2 + len(fixes) * 2 + (3 if lb else 0) + 0.2 * len(ctx.callers(s["uid"])),
                title=f"{s['qualname']}: retry added without backoff/jitter"
                      + ("; backoff added later in the same function" if lb else "; later fixed"),
                evidence={"retry_line": r["text"].strip()[:140], "added": r["hash"],
                          "later_fixes": [{"hash": f["hash"], "subject": f["subject"]} for f in fixes[:4]],
                          "later_backoff_commits": _dedup([x["hash"] for x in lb])[:3]},
                commits=_dedup([r["hash"], *[f["hash"] for f in fixes[:3]], *[x["hash"] for x in lb[:2]]]),
                diffs=[(r["hash"], path)], paths=[path], symbols=[s["uid"]],
            )


# ---------------------------------------------------------------------------- 4. swallow masks later fix
SWALLOW_LINE_RX = (r"except\s*(\([^)]*\)|[\w.]+)?(\s+as\s+\w+)?\s*:\s*(pass|continue)\s*$|^\s*except\s*:\s*$|"
                   r"except\s+(Exception|BaseException)\b.*:\s*$|catch\s*(\([^)]*\))?\s*\{\s*\}|\.catch\(\s*\(\)\s*=>\s*(\{\s*\}|null|undefined)\s*\)|"
                   r"contextlib\.suppress|\|\|\s*true\b|set\s+\+e\b|2>\s*/dev/null|-auto-approve|continue-on-error:\s*true|"
                   r"ignore_errors\s*[:=]\s*(true|True)|--force\b|^\s*-\s*\S*\s*\|\|\s*exit\s+0")
SWALLOW_NOISE_RX = re.compile(r"(?i)except\s*\(?\s*(ImportError|ModuleNotFoundError|KeyError|AttributeError|StopIteration|"
                              r"NotImplementedError|FileNotFoundError)|\brm\s+-[a-z]*f|\btrap\b|\bgrep\b|\bcleanup\b|"
                              r"which\s|command\s+-v|\bmkdir\s+-p|git\s+branch\s+-D")
ERR_WORD_RX = re.compile(r"(?i)\b(error|errors|exception|fail(s|ed|ure|ing)?|crash(es|ed)?|silent(ly)?|swallow(ed|ing)?|"
                         r"ignor(e|ed|ing)|hid(e|den|ing)|mask(ed|ing)?|bug|broken)\b")


@candidate("swallow_introduced_masks_later_fix")
def swallow_introduced_masks_later_fix(ctx: Ctx):
    """An added swallow (empty/pass/continue handler, ``|| true``, ``set +e``, ``2>/dev/null``, ``--force`` ...) paired
    with a fix-classified commit 90-365 days later that touched the same symbol (code) or removed a swallow from the same
    file (scripts/CI), where the fix's message references an error. The symbol is the one containing the swallow line
    (``diff_lines.symbol``); commit-touched symbols are only a fallback for unenriched rows. Proxy: bare 'except:' and
    'except Exception:' lines count as swallows only when the next added line is pass/continue."""
    nxt: dict[tuple, str] = {}
    for r in ctx.grep(r"^\s*(pass|continue|return None|\.\.\.)\s*$", "+", path_regex=r"\.(py)$", limit=60000):
        nxt[(r["hash"], r["path"], r["line"])] = r["text"]
    removed_by_path: dict[str, list] = defaultdict(list)
    for r in ctx.grep(SWALLOW_LINE_RX, "-", limit=30000):
        removed_by_path[r["path"]].append(r)
    for r in ctx.grep(SWALLOW_LINE_RX, "+", limit=30000):
        path, text = r["path"], r["text"]
        if is_test(path) or is_docs(path):
            ctx.drop("tests_or_docs")
            continue
        if SWALLOW_NOISE_RX.search(text):
            ctx.drop("idiomatic_cleanup_or_optional")
            continue
        t = text.strip()
        if re.match(r"(except\s*:|except\s+(Exception|BaseException)\b.*:)\s*$", t) and \
                nxt.get((r["hash"], path, r["line"] + 1), "").strip() not in ("pass", "continue"):
            ctx.drop("handler_not_empty")
            continue
        if is_code(path):
            ss = _sym(r)
            cands = _syms_in(ctx, r["hash"], path) if ss is None else ([ctx.symbols[ss]] if ss in ctx.symbols else [])
            for s in cands:
                fx = [f for f in ctx.fixes_by_uid.get(s["uid"], [])
                      if 90 * DAY <= f["ts"] - r["ts"] <= 365 * DAY and ERR_WORD_RX.search(f["subject"] or "")]
                if not fx:
                    continue
                yield Hit(
                    subject=f"{s['uid']}|{r['hash'][:12]}",
                    score=3 + len(fx) * 1.5,
                    title=f"Swallow added in {s['qualname']} ({t[:50]}), fixed {round((fx[0]['ts'] - r['ts']) / DAY)}d later",
                    evidence={"swallow": t[:140], "added": r["hash"],
                              "fixes": [{"hash": f["hash"], "subject": f["subject"],
                                         "days_later": round((f["ts"] - r["ts"]) / DAY)} for f in fx[:4]]},
                    commits=_dedup([r["hash"], *[f["hash"] for f in fx[:3]]]),
                    diffs=[(r["hash"], path), (fx[0]["hash"], path)], paths=[path], symbols=[s["uid"]],
                )
        else:
            fx = [x for x in removed_by_path.get(path, []) if 90 * DAY <= x["ts"] - r["ts"] <= 365 * DAY
                  and is_fix_message(ctx.by_hash[x["hash"]]["message"])
                  and ERR_WORD_RX.search(ctx.by_hash[x["hash"]]["message"] or "")]
            if not fx:
                continue
            yield Hit(
                subject=f"{path}|{r['hash'][:12]}|{t[:40]}",
                score=3 + len(fx),
                title=f"{t[:60]} added in {path}, later removed by a fix {round((fx[0]['ts'] - r['ts']) / DAY)}d on",
                evidence={"swallow": t[:140], "added": r["hash"],
                          "fixes": [{"hash": x["hash"], "subject": subject_of(ctx.by_hash[x["hash"]]["message"])}
                                    for x in fx[:3]]},
                commits=_dedup([r["hash"], fx[0]["hash"]]), diffs=[(r["hash"], path), (fx[0]["hash"], path)],
                paths=[path],
            )


# ---------------------------------------------------------------------------- 5. migration placement
MIG_CMD_RX = (r"alembic\s+upgrade|manage\.py\s+migrate|django-admin\s+migrate|flask\s+db\s+upgrade|rake\s+db:migrate|"
              r"rails\s+db:migrate|prisma\s+migrate|flyway|knex\s+migrate|sequelize.*db:migrate|\bgoose\b.*\bup\b|"
              r"liquibase|migrate\s+up|typeorm\s+migration:run|db:migrate|npm run migrate|yarn migrate|make migrate")
WAIT_DB_RX = r"(?i)wait-for-it|wait-for|wait_for_db|pg_isready|dockerize\s+-wait|advisory_lock|lock_timeout|mysqladmin ping|nc -z"


def _surface(path: str) -> str | None:
    p = path.lower()
    if INFRA_DEV_RX.search(path) and "compose" in p:
        return None
    if re.search(r"(^|/)(docker-)?entrypoint[^/]*|(^|/)(start|run|boot|prestart|release)[\w-]*\.sh$|(^|/)procfile$|(^|/)app\.json$", p):
        return "entrypoint"
    if re.search(r"(^|/)(docker|container)file[^/]*$", p):
        return "dockerfile"
    if re.search(r"hook|job|migrat", p) and K8S_RX.search(path):
        return "hook_or_job"
    if K8S_RX.search(path) or re.search(r"(^|/)(helm|charts?)/", p):
        return "k8s_deploy"
    if re.search(r"docker-compose", p):
        return "compose"
    if is_ci(path):
        return "ci_step"
    if re.search(r"(^|/)(makefile|fabfile\.py|tasks\.py|deploy[\w-]*\.(sh|ya?ml))$", p):
        return "deploy_script"
    return None


@candidate("migration_on_boot_placement_change")
def migration_on_boot_placement_change(ctx: Ctx):
    """A migration command added on one deploy surface and removed from another within 20 commits (or the same commit),
    or a migration-command change near a wait-for-db / lock fix within ~20 commits. Library repos (no Dockerfile, k8s
    or deploy files at HEAD) are dropped."""
    if not any(_surface(p) for p in ctx.head_files):
        ctx.drop("library_repo")
        return
    events: list[dict] = []
    for r in ctx.grep(MIG_CMD_RX, None, limit=20000):
        surf = _surface(r["path"])
        if surf is None:
            if INFRA_DEV_RX.search(r["path"]):
                ctx.drop("dev_test_compose")
            continue
        events.append({"hash": r["hash"], "seq": r["seq"], "surface": surf, "sign": r["sign"], "path": r["path"],
                       "text": r["text"].strip()[:120]})
    idx = _idx(ctx)
    emitted = set()
    for e in events:
        if e["sign"] != "+":
            continue
        for o in events:
            if o["sign"] == "-" and o["surface"] != e["surface"] and abs(o["seq"] - e["seq"]) <= 20:
                key = (e["hash"], o["hash"], e["surface"], o["surface"])
                if key in emitted:
                    continue
                emitted.add(key)
                hs = _dedup([o["hash"], e["hash"]])
                yield Hit(
                    subject=f"{o['surface']}->{e['surface']}|{e['hash'][:12]}",
                    score=4 + (2 if o["hash"] == e["hash"] else 0),
                    title=f"Migration command moved from {o['surface']} ({o['path']}) to {e['surface']} ({e['path']})",
                    evidence={"removed": {"hash": o["hash"], "path": o["path"], "line": o["text"]},
                              "added": {"hash": e["hash"], "path": e["path"], "line": e["text"]}},
                    commits=hs, diffs=[(o["hash"], o["path"]), (e["hash"], e["path"])],
                    paths=_dedup([o["path"], e["path"]]),
                )
    wait_rows = ctx.grep(WAIT_DB_RX, "+", limit=10000)
    for w in wait_rows:
        c = ctx.by_hash[w["hash"]]
        if not is_fix_message(c["message"]) or not _surface(w["path"]):
            continue
        near = [e for e in events if abs(e["seq"] - w["seq"]) <= 20]
        if not near:
            continue
        key = ("wait", w["hash"])
        if key in emitted:
            continue
        emitted.add(key)
        yield Hit(
            subject=f"waitfix|{w['hash'][:12]}",
            score=3 + len(near) * 0.5,
            title=f"wait-for-db/lock fix near migration command changes: {subject_of(c['message'])[:80]}",
            evidence={"wait_line": w["text"].strip()[:120], "path": w["path"],
                      "nearby_migration_changes": [{"hash": e["hash"], "path": e["path"], "sign": e["sign"],
                                                    "line": e["text"]} for e in near[:4]]},
            commits=_dedup([w["hash"], *[e["hash"] for e in near[:3]]]), diffs=[(w["hash"], w["path"])],
            paths=_dedup([w["path"], *[e["path"] for e in near[:2]]]),
        )


# ---------------------------------------------------------------------------- 6. resource leak lineage
LEAK_MSG_RX = re.compile(r"(?i)\bleak(s|ed|ing|y)?\b|unclosed|not (being )?(closed|released)|never (closed|released)|"
                         r"forgot(ten)? to (close|release)|too many open|file descriptors?|\bfds?\b|ResourceWarning|"
                         r"left open|exhaust(ed|s)? (the )?(pool|connections|sockets)")
GUARD_ADD_RX = r"\bwith\s+.+:|\bfinally\b|\.close\(|\bdefer\b|\.dispose\(|\.release\(|\bclosing\(|\.shutdown\(|try-with|using\s*\("
CLI_RX = re.compile(r"(?i)(^|/)(cli|__main__|main|commands?)\.py$|(^|/)scripts?/|(^|/)bin/")


@candidate("resource_leak_fix_lineage")
def resource_leak_fix_lineage(ctx: Ctx):
    """>=2 leak-worded commits in the same directory whose diffs add with/finally/close/defer. Evidence adds ulimit/pool
    config lines and, for Python, HEAD files in the module that open a resource without a context manager (cap 5)."""
    guard_by_commit: dict[tuple[str, str], int] = defaultdict(int)
    for r in ctx.grep(GUARD_ADD_RX, "+", limit=60000):
        guard_by_commit[(r["hash"], r["path"])] += 1
    groups: dict[str, list] = defaultdict(list)
    for c in ctx.commits:
        if c["is_merge"] or ctx.is_bot(c) or not LEAK_MSG_RX.search(c["message"] or ""):
            continue
        for fc in ctx.changes(c["hash"]):
            p = fc["path"]
            if not is_code(p) or is_test(p):
                continue
            if guard_by_commit.get((c["hash"], p)):
                groups[p.rsplit("/", 1)[0] if "/" in p else "."].append((c, p))
    pool = ctx.grep(r"(?i)ulimit|nofile|pool_size|max_connections|pool_recycle|max_overflow|MaxIdleConns", "+", limit=300)
    for mod, items in sorted(groups.items()):
        commits = _dedup([c["hash"] for c, _ in items])
        if len(commits) < 2:
            continue
        if all(CLI_RX.search(p) for _, p in items):
            ctx.drop("short_cli")
            continue
        unguarded = []
        for p in sorted(ctx.head_files):
            if len(unguarded) >= 5:
                break
            if not p.startswith(mod + "/") and mod != ".":
                continue
            if p.endswith(".py") and not is_test(p):
                for n, ln in enumerate(_head_lines(ctx, p), 1):
                    if re.match(r"\s*\w+\s*=\s*(open|socket\.socket|urlopen|sqlite3\.connect|psycopg2\.connect)\(", ln):
                        unguarded.append(f"{p}:{n}: {ln.strip()[:90]}")
                        break
        yield Hit(
            subject=mod,
            score=len(commits) * 2 + len(unguarded) * 0.5,
            title=f"{len(commits)} resource-leak fixes in {mod}/ each adding a guard (with/finally/close)",
            evidence={"fixes": [{"hash": c["hash"], "subject": subject_of(c["message"]), "path": p}
                                for c, p in items[:8]],
                      "pool_or_ulimit_config": [f"{x['path']}: {x['text'].strip()[:100]}" for x in pool
                                                if x["path"].startswith(mod) or x["path"].endswith((".yml", ".yaml", ".sh"))][:4],
                      "head_unguarded_opens": unguarded},
            commits=commits[:8], diffs=[(c["hash"], p) for c, p in items[:3]],
            paths=_dedup([p for _, p in items])[:6],
        )


# ---------------------------------------------------------------------------- 7. timeout added after hang
HANG_MSG_RX = re.compile(r"(?i)\b(hang(s|ing)?|hung|stuck|deadlock(s|ed)?|time-?outs?|freez(e|es|ing)|block(s|ed|ing)? forever|"
                         r"never (returns?|finish(es)?)|infinite)\b")
CALL_TIMEOUT_RX = re.compile(r"\b((?:requests|httpx|urllib\.request|session|client|http|axios|aiohttp|conn|self\.\w*(?:session|client))"
                             r"(?:\.[a-z_]+)?\.(?:get|post|put|delete|patch|head|request|urlopen|connect|fetch|stream)|urlopen|"
                             r"psycopg2\.connect|pymysql\.connect|socket\.create_connection)\s*\([^)]*\btimeout\s*[=:]")
HTTP_CALL_RX = re.compile(r"\b(requests\.(?:get|post|put|delete|patch|head|request)|httpx\.(?:get|post|put|delete|patch|head|request)|"
                          r"urlopen|urllib\.request\.urlopen|psycopg2\.connect|pymysql\.connect|socket\.create_connection)\s*\(")
SESSION_DEFAULT_RX = re.compile(r"(?i)(Client|Session|AsyncClient|HTTPAdapter)\([^)]*timeout|\.timeout\s*=|setdefaulttimeout|"
                                r"axios\.defaults\.timeout|axios\.create\([^)]*timeout")


@candidate("timeout_added_after_hang_siblings_unbounded")
def timeout_added_after_hang_siblings_unbounded(ctx: Ctx):
    """A hang/stuck/deadlock/timeout-worded fix adds ``timeout=`` to a known HTTP/DB client call; at HEAD >=2 other
    non-test call sites of the same callee carry no timeout on the call line or the three lines after it (line-local
    proxy for 'no timeout'), and no session-level default exists in the file."""
    callee_added: dict[str, list] = defaultdict(list)   # callee -> add rows
    for r in ctx.grep(r"timeout\s*[=:]", "+", path_regex=r"\.(py|js|jsx|ts|tsx|mjs)$", limit=30000):
        if is_test(r["path"]) or is_vendored(r["path"]):
            continue
        c = ctx.by_hash[r["hash"]]
        if not HANG_MSG_RX.search(c["message"] or "") or not _is_fixish(ctx, c):
            continue
        m = CALL_TIMEOUT_RX.search(r["text"])
        if not m:
            continue
        callee_added[m.group(1)].append(r)
    if not callee_added:
        return
    # HEAD scan: one pass over code files for HTTP/DB call sites
    calls: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)
    for p in sorted(ctx.head_files):
        if not p.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".mjs")) or is_test(p) or is_vendored(p) or SCRIPT_DIR_RX.search(p):
            continue
        t = ctx.text_at_head(p)
        if not t or not re.search(r"requests|httpx|urlopen|connect|socket|fetch|axios|client|session", t):
            continue
        lines = t.splitlines()
        default = bool(SESSION_DEFAULT_RX.search(t))
        for n, ln in enumerate(lines):
            for m in re.finditer(r"\b([\w.]+)\s*\(", ln):
                name = m.group(1)
                if name in callee_added or HTTP_CALL_RX.match(ln[m.start():]):
                    has = "timeout" in "\n".join(lines[n:n + 4])
                    calls[name].append((f"{p}", n + 1, has or default))
    for callee, rows in callee_added.items():
        sites = calls.get(callee, [])
        unbounded = [s for s in sites if not s[2]]
        if len(unbounded) < 2:
            if sites and len(unbounded) < 2 and any(s[2] for s in sites):
                ctx.drop("siblings_have_timeout_or_session_default")
            continue
        first = rows[0]
        c = ctx.by_hash[first["hash"]]
        yield Hit(
            subject=f"{callee}|{first['hash'][:12]}",
            score=len(unbounded) * 1.5 + len(rows),
            title=f"timeout added to {callee}() after a hang fix; {len(unbounded)} sibling calls still unbounded",
            evidence={"fix": subject_of(c["message"]), "added_line": first["text"].strip()[:140],
                      "unbounded_siblings": [f"{p}:{n}" for p, n, _ in unbounded[:10]]},
            commits=_dedup([r["hash"] for r in rows[:4]]), diffs=[(first["hash"], first["path"])],
            paths=_dedup([first["path"], *[p for p, _, _ in unbounded[:5]]]),
        )


# ---------------------------------------------------------------------------- 8. config change fix chain
KEY_RX = re.compile(r"^\s*[\"']?([A-Za-z_][\w.\-/]*)[\"']?\s*[:=]")


def _keys(rows) -> set[str]:
    out = set()
    for r in rows:
        m = KEY_RX.match(r["text"])
        if m:
            out.add(m.group(1))
    return out


@candidate("config_change_fix_chain")
def config_change_fix_chain(ctx: Ctx):
    """Config commit C, then a fix/revert D touching the same file within 72h or <=10 commits, with an overlapping key
    (or, lacking keys, overlapping line numbers). Same author boosts. Bots, version bumps, lockfiles, generated files and
    docs-only follow-ups are excluded."""
    idx = _idx(ctx)
    for path in sorted(p for p in ctx.changes_by_path if is_config(p)):
        if LOCKFILE_RX.search(path) or DEP_MANIFEST_RX.search(path) or is_docs(path) or is_test(path):
            continue
        cs = ctx.path_commits(path)
        for i, c in enumerate(cs[:-1]):
            if c["is_merge"] or ctx.is_bot(c):
                continue
            if BUMP_RX.search(subject_of(c["message"])):
                ctx.drop("version_bump")
                continue
            ck = None
            for d in cs[i + 1:i + 6]:
                if d["is_merge"] or ctx.is_bot(d):
                    continue
                gap_commits = idx[d["hash"]] - idx[c["hash"]]
                if d["ts"] - c["ts"] > 3 * DAY and gap_commits > 10:
                    break
                if not _is_fixish(ctx, d):
                    continue
                if BUMP_RX.search(subject_of(d["message"])):
                    continue
                if ck is None:
                    ck = (_keys(ctx.lines_of(c["hash"], path)), {r["line"] for r in ctx.lines_of(c["hash"], path)})
                dl = ctx.lines_of(d["hash"], path)
                if not dl:
                    continue
                shared = ck[0] & _keys(dl)
                near_lines = bool(ck[1]) and any(any(abs(r["line"] - x) <= 5 for x in ck[1]) for r in dl[:60]) \
                    if not ck[0] else False
                if not shared and not near_lines:
                    ctx.drop("no_key_overlap")
                    continue
                same = c["author_email"] == d["author_email"]
                yield Hit(
                    subject=f"{c['hash']}|{d['hash']}|{path}",
                    score=3 + (2 if same else 0) + (2 if d["is_revert"] else 0) + len(shared) * 0.5
                          + (1 if d["ts"] - c["ts"] < DAY else 0),
                    title=f"{path}: config change followed by {'revert' if d['is_revert'] else 'fix'} "
                          f"{round((d['ts'] - c['ts']) / 3600)}h later",
                    evidence={"change": subject_of(c["message"]), "followup": subject_of(d["message"]),
                              "shared_keys": sorted(shared)[:8], "same_author": same,
                              "hours_apart": round((d["ts"] - c["ts"]) / 3600, 1), "commits_apart": gap_commits},
                    commits=[c["hash"], d["hash"]], diffs=[(c["hash"], path), (d["hash"], path)], paths=[path],
                )
                break


# ---------------------------------------------------------------------------- 9. emergency commits
EMERGENCY_RX = re.compile(r"(?i)\b(hot-?fix(es)?|emergency|sev-?[0-4]|p0|outage|rollback|roll(ed)? back|stop-?gap|incident)\b")
TEMP_RX = r"(?i)\b(temporary|temporarily|workaround|stop-?gap|for now|remove (this )?(after|once|when)|hack|xxx|todo)\b"


@candidate("emergency_commit_followup_chain")
def emergency_commit_followup_chain(ctx: Ctx):
    """Emergency-worded commit (hotfix/emergency/sev/p0/outage/rollback/stopgap; plain 'urgent' ignored) followed within
    72h by >=1 fix/revert on a file it touched. Ranked by chain length, revert at the end and temporary markers."""
    temp_rows = {(r["hash"]): r for r in ctx.grep(TEMP_RX, "+", limit=30000)}
    covered: set[str] = set()   # follow-ups of an earlier chain do not start their own
    for c in ctx.commits:
        if c["is_merge"] or ctx.is_bot(c) or not EMERGENCY_RX.search(subject_of(c["message"])):
            continue
        if c["hash"] in covered:
            ctx.drop("followup_of_earlier_chain")
            continue
        files = [p for p in _paths_of(ctx, c["hash"]) if not is_docs(p) and not LOCKFILE_RX.search(p)]
        if not files:
            ctx.drop("docs_only")
            continue
        fset = set(files)
        chain = []
        i = _idx(ctx)[c["hash"]]
        for d in ctx.commits[i + 1:i + 400]:
            if d["ts"] - c["ts"] > 3 * DAY:
                break
            if d["is_merge"] or ctx.is_bot(d) or not _is_fixish(ctx, d):
                continue
            if fset & set(_paths_of(ctx, d["hash"])):
                chain.append(d)
        if not chain:
            continue
        covered.update(d["hash"] for d in chain)
        revert_end = bool(chain[-1]["is_revert"]) or bool(re.match(r"(?i)revert\b", subject_of(chain[-1]["message"])))
        tmp = temp_rows.get(c["hash"])
        yield Hit(
            subject=c["hash"],
            score=len(chain) * 2 + (3 if revert_end else 0) + (2 if tmp else 0),
            title=f"Emergency commit '{subject_of(c['message'])[:70]}' followed by {len(chain)} fix/revert commits in 72h",
            evidence={"emergency": subject_of(c["message"]), "files": files[:6],
                      "followups": [{"hash": d["hash"], "subject": subject_of(d["message"]),
                                     "hours_later": round((d["ts"] - c["ts"]) / 3600, 1)} for d in chain[:6]],
                      "ends_in_revert": revert_end, "temporary_marker": tmp["text"].strip()[:100] if tmp else None},
            commits=[c["hash"], *[d["hash"] for d in chain[:5]]],
            diffs=[(c["hash"], files[0]), *[(d["hash"], files[0]) for d in chain[:2] if files[0] in _paths_of(ctx, d["hash"])]],
            paths=files[:6],
        )


# ---------------------------------------------------------------------------- 10. graceful shutdown drift
SHUTDOWN_INFRA_RX = (r"^\s*(CMD|ENTRYPOINT|STOPSIGNAL)\b|\b(tini|dumb-init|tini-static)\b|^\s*exec\s|terminationGracePeriodSeconds|"
                     r"preStop|stop_grace_period|stop_signal|init:\s*true")
SHUTDOWN_CODE_RX = (r"signal\.signal\(\s*signal\.SIG(TERM|INT)|process\.on\(\s*['\"]SIG(TERM|INT)|on_event\(\s*['\"]shutdown|"
                    r"\bSIGTERM\b|graceful(ly)?[_ ]?(shutdown|stop)|server\.shutdown\(|http\.Server.*Shutdown|"
                    r"signal\.Notify|atexit\.register")
SHUTDOWN_MSG_RX = re.compile(r"(?i)\b(shutdown|sigterm|sigkill|zombie|graceful(ly)?|signal(s)?|terminat(e|ion)|pid ?1|"
                             r"killed|exit code 137|drain)\b")
DOCKERFILE_RX = re.compile(r"(?i)(^|/)(docker|container)file[^/]*$|\.dockerfile$")


@candidate("graceful_shutdown_signal_drift")
def graceful_shutdown_signal_drift(ctx: Ctx):
    """Commits changing CMD/ENTRYPOINT form, tini/dumb-init/exec wrappers, grace-period / preStop / STOPSIGNAL settings or
    adding/removing SIGTERM handlers; shutdown-worded neighbouring commits (+/-10) boost. Requires a long-running server
    signal anywhere in history (gunicorn/uvicorn/listen/ListenAndServe...). Multi-stage 'final stage' detection is not
    attempted; Dockerfiles named dev/test and ``exec "$@"`` entrypoints are dropped."""
    if not ctx.grep(r"gunicorn|uvicorn|\.listen\(|app\.run\(|createServer|ListenAndServe|runserver|waitress|daphne|"
                    r"hypercorn|nginx|serve\b", None, limit=1):
        ctx.drop("not_long_running_server")
        return
    by_commit: dict[str, list] = defaultdict(list)
    for r in ctx.grep(SHUTDOWN_INFRA_RX, None, path_regex=r"(?i)docker|compose|ya?ml$|entrypoint|\.sh$|Procfile|supervisor",
                      limit=30000):
        if INFRA_DEV_RX.search(r["path"]) or CI_RX.search(r["path"]) or is_test(r["path"]):
            ctx.drop("dev_test_or_ci_file")
            continue
        if re.search(r'exec\s+"\$@"|exec\s+\$@', r["text"]):
            ctx.drop("exec_passthrough_entrypoint")
            continue
        if re.match(r"\s*(CMD|ENTRYPOINT)", r["text"]) and not DOCKERFILE_RX.search(r["path"]):
            continue
        by_commit[r["hash"]].append(r)
    for r in ctx.grep(SHUTDOWN_CODE_RX, None, limit=20000):
        if is_code(r["path"]) and not is_test(r["path"]) and not SCRIPT_DIR_RX.search(r["path"]):
            by_commit[r["hash"]].append(r)
    for sha, rows in by_commit.items():
        c = ctx.by_hash[sha]
        if c["is_merge"] or ctx.is_bot(c):
            continue
        msgs = [n for n in _near(ctx, sha, 10) if SHUTDOWN_MSG_RX.search(n["message"] or "")]
        yield Hit(
            subject=sha,
            score=len(rows) * 0.5 + len(msgs) * 2 + (2 if SHUTDOWN_MSG_RX.search(c["message"] or "") else 0),
            title=f"Shutdown/signal handling changed: {subject_of(c['message'])[:90]}",
            evidence={"lines": [f"{r['sign']} {r['path']}: {r['text'].strip()[:110]}" for r in rows[:8]],
                      "shutdown_worded_neighbours": [{"hash": n["hash"], "subject": subject_of(n["message"])} for n in msgs[:4]]},
            commits=_dedup([sha, *[n["hash"] for n in msgs[:3]]]),
            diffs=[(sha, p) for p in _dedup([r["path"] for r in rows])[:3]],
            paths=_dedup([r["path"] for r in rows])[:6],
        )


# ---------------------------------------------------------------------------- 11. external call in transaction
ONCOMMIT_RX = r"on_commit|afterCommit|after_commit|AFTER_COMMIT|outbox|TransactionSynchronization|run_after_commit"
PUBLISH_RX = (r"\.publish\(|\.delay\(|\.apply_async\(|send_task\(|\.enqueue\(|producer\.send|\.send_message\(|requests\.post|"
              r"httpx\.post|\.send_mail\(|send_email\(|\.produce\(|sns\.|sqs\.|\.emit\(")
TXN_MSG_RX = re.compile(r"(?i)on_?commit|after[ _]?commit|ghost|not yet committed|before (the )?commit|outbox|"
                        r"(inside|within|in) (a |the )?transaction|rolled back (but|yet)|transaction (rolled|rollback)")


TXN_OPEN_RX = re.compile(r"(?i)transaction\.atomic|\batomic\(|@transactional|@transaction\b|session\.begin|\.begin\(\)|"
                         r"begin_transaction|beginTransaction|db\.transaction|\.transaction\(|\bBEGIN\b|"
                         r"with\s+\w*\.?(conn|connection|engine|db)\w*\.begin")


def _unfixed_txn_siblings(ctx: Ctx) -> list:
    """Live non-test functions at HEAD that call out to an external system inside a transaction scope without any
    on_commit/outbox mechanism. Cached per run."""
    c = _ro(ctx)
    if "txn_siblings" in c:
        return c["txn_siblings"]
    pub_rx, oc_rx = re.compile(PUBLISH_RX), re.compile(ONCOMMIT_RX)
    out = []
    for s in ctx.alive_symbols:
        if s["kind"] not in ("function", "method") or not is_code(s["path"]) or is_test(s["path"]):
            continue
        body = s["body"] or ""
        if len(body) > 20000 or not TXN_OPEN_RX.search(body) or not pub_rx.search(body) or oc_rx.search(body):
            continue
        out.append(s)
    out.sort(key=lambda s: (s["path"], s["start_line"] or 0))
    c["txn_siblings"] = out
    return out


@candidate("external_call_inside_transaction")
def external_call_inside_transaction(ctx: Ctx):
    """Diff adds on_commit/afterCommit/outbox next to a removed publish/enqueue/HTTP-post in the same function (or the
    reverse), or a commit message about on_commit / ghost jobs / 'not yet committed' touching publish lines. "Same
    function" is exact: both lines carry the same ``diff_lines.symbol`` (module-level matches module-level). Only rows
    without a symbol value (index not enriched) fall back to same file. HEAD sibling scan: every other live
    non-test function/method at HEAD whose body opens a transaction (``atomic``, ``session.begin``, ``@transactional``,
    ``BEGIN`` ...) and makes the same kind of external call (publish/enqueue/HTTP post/mail) with no on_commit/outbox
    mechanism is listed as ``unfixed_head_siblings`` (same file ones first) and raises the score; computed once per
    run. Commits touching >15 files and tests are dropped; an added publish that is itself wrapped in on_commit is a
    guarded call and dropped."""
    oc = ctx.grep(ONCOMMIT_RX, None, limit=30000)
    pub = ctx.grep(PUBLISH_RX, None, limit=40000)
    pub_by: dict[tuple, list] = defaultdict(list)
    for r in pub:
        pub_by[(r["hash"], r["path"])].append(r)
    seen = set()
    for r in oc:
        key = (r["hash"], r["path"])
        if key in seen:
            continue
        path = r["path"]
        if is_test(path) or not is_code(path):
            continue
        c = ctx.by_hash[r["hash"]]
        if len(ctx.changes(r["hash"])) > 15:
            ctx.drop("bulk_commit")
            continue
        rsym = _sym(r)

        def _scoped(x, rsym=rsym):
            xs = _sym(x)
            return rsym is None or xs is None or xs == rsym

        opposite = "-" if r["sign"] == "+" else "+"
        others = [x for x in pub_by.get(key, []) if x["sign"] == opposite and _scoped(x)]
        if r["sign"] == "-":
            others = [x for x in pub_by.get(key, []) if x["sign"] == "+" and not re.search(ONCOMMIT_RX, x["text"])
                      and _scoped(x)]
        msg = bool(TXN_MSG_RX.search(c["message"] or ""))
        scoped_all = [x for x in pub_by.get(key, []) if _scoped(x)]
        if not others and not (msg and scoped_all):
            continue
        seen.add(key)
        used = others or scoped_all
        syms = _row_syms(ctx, [r, *used], r["hash"], path)[:3]
        sibs = [s for s in _unfixed_txn_siblings(ctx) if s["uid"] not in syms]
        sibs.sort(key=lambda s: s["path"] != path)
        yield Hit(
            subject=f"{r['hash']}|{path}",
            score=3 + len(used) * 0.5 + (2 if msg else 0) + (1 if r["sign"] == "+" else 0) + min(len(sibs), 4) * 0.75,
            title=f"{'Moved publish after commit' if r['sign'] == '+' else 'Moved publish into transaction'} "
                  f"in {path}: {subject_of(c['message'])[:70]}",
            evidence={"commit_hook_line": f"{r['sign']} {r['text'].strip()[:120]}",
                      "publish_lines": [f"{x['sign']} {x['text'].strip()[:110]}" for x in used[:4]],
                      "message_mentions_commit_semantics": msg,
                      "unfixed_head_siblings": [s["uid"] for s in sibs[:6]], "unfixed_head_sibling_count": len(sibs)},
            commits=[r["hash"]], diffs=[(r["hash"], path)], paths=[path], symbols=[*syms, *[s["uid"] for s in sibs[:2]]],
        )


# ---------------------------------------------------------------------------- 12. protective control removed
PROTECT_RX = (r"(?i)circuit[_ -]?breaker|rate[_ -]?limit|throttl(e|ing)|max_?size|max_?connections|\btimeout\s*[=:]|resources:|"
              r"limits:|livenessProbe|readinessProbe|startupProbe|PodDisruptionBudget|kill[_-]?switch|maxmemory|"
              r"pool_size|max_?retries|backpressure|semaphore|bulkhead")


def _pkey(text: str) -> str:
    m = re.search(PROTECT_RX, text)
    return m.group(0).lower() if m else ""


@candidate("protective_control_removed")
def protective_control_removed(ctx: Ctx):
    """A removed line from a narrow protective vocabulary whose origin was a fix-classified commit, with no equivalent
    control re-added in the same commit. Origin = ``ctx.line_origin`` (git blame of the removed line) whenever blame exists
    for it (blame is computed for lines removed by bug-fix commits); otherwise the proxy is the latest earlier commit that
    added the identical line to the same file. A blame origin that is not a fix drops the hit even if the proxy would have
    found a fix. Symbols come from ``diff_lines.symbol``. Deleted files, tests and bulk commits are dropped."""
    adds: dict[tuple[str, str], list] = defaultdict(list)
    add_by_commit: dict[str, set[str]] = defaultdict(set)
    for r in ctx.grep(PROTECT_RX, "+", limit=80000):
        adds[(r["path"], r["text"].strip())].append(r)
        add_by_commit[r["hash"]].add(_pkey(r["text"]))
    deleted = {(fc["hash"], fc["path"]) for rows in ctx.changes_by_commit.values() for fc in rows if fc["status"] == "D"}
    for r in ctx.grep(PROTECT_RX, "-", limit=80000):
        path, t = r["path"], r["text"].strip()
        if is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path) or len(t) < 8:
            continue
        if (r["hash"], path) in deleted:
            ctx.drop("deleted_file")
            continue
        ob = ctx.line_origin(r["hash"], path, r["line"]) if r["line"] is not None else None
        if ob is not None and ob in ctx.by_hash:
            oc = ctx.by_hash[ob]
            o = {"hash": ob}
            if ob == r["hash"]:
                continue
        else:
            ob = None
            origins = [a for a in adds.get((path, t), []) if a["seq"] < r["seq"]]
            if not origins:
                continue
            o = origins[-1]
            oc = ctx.by_hash[o["hash"]]
        if not _is_fixish(ctx, oc):
            continue
        if _pkey(t) in add_by_commit.get(r["hash"], set()):
            ctx.drop("equivalent_readded")
            continue
        c = ctx.by_hash[r["hash"]]
        if c["is_merge"] or ctx.is_bot(c):
            continue
        yield Hit(
            subject=f"{r['hash']}|{path}|{t[:60]}",
            score=3 + (2 if _is_fixish(ctx, oc) else 0) + (1 if is_config(path) else 0),
            title=f"Protective control removed in {path}: {t[:80]}",
            evidence={"removed_line": t[:160], "removed_in": subject_of(c["message"]),
                      "originally_added_by_fix": {"hash": o["hash"], "subject": subject_of(oc["message"])},
                      "origin_source": "blame" if ob is not None else "identical-line proxy"},
            commits=[r["hash"], o["hash"]], diffs=[(r["hash"], path), (o["hash"], path)], paths=[path],
            symbols=_row_syms(ctx, [r], r["hash"], path)[:2],
        )


# ---------------------------------------------------------------------------- 13. time bug hotspot
TIME_MSG_RX = re.compile(r"(?i)\b(time-?zones?|tz|utc|dst|daylight|leap|naive|aware|datetime|timestamps?|epoch|clock|expir\w*|"
                         r"midnight|duration|ttl|timedelta|date|dates|timeout|monotonic|strftime|local ?time|wall[- ]clock)\b")
TIME_API_RX = (r"datetime|time\.time|time\.monotonic|timedelta|utcnow|utcfromtimestamp|tzinfo|pytz|zoneinfo|Date\.now|new Date|"
               r"moment\(|dayjs|time\.Now|Instant\.|strptime|mktime|timegm|\.now\(|fromtimestamp|timezone|ZonedDateTime")
WALL_ARITH_RX = re.compile(r"time\.time\(\)\s*[-+]|utcnow\(\)\s*[-+]|now\(\)\s*[-+]|Date\.now\(\)\s*[-+]|[-+]\s*timedelta\(|"
                           r"timedelta\([^)]*\)\s*[<>]|\*\s*(86400|3600|24\s*\*\s*60)|time\.Now\(\)\.(Add|Sub)")
TIME_FORMAT_ONLY_RX = re.compile(r"(?i)strftime|isoformat|logging|logger\.|print\(|\.format\(|f['\"]|%[YmdHMS]|fixtures?")


@candidate("time_bug_hotspot")
def time_bug_hotspot(ctx: Ctx):
    """Fix commits with time-worded messages whose diff touches datetime/time APIs, counted per file: >=2 such commits, or
    1 plus wall-clock duration arithmetic. Display-only changes (strftime/isoformat/log lines), fixtures and tests are
    excluded. Attached symbols are the ones containing the counted time-API lines (``diff_lines.symbol``), not every
    symbol the commits touched in the file."""
    per_path: dict[str, list] = defaultdict(list)
    seen = set()
    for r in ctx.grep(TIME_API_RX, None, limit=60000):
        path = r["path"]
        if not is_code(path):
            continue
        if is_test(path) or re.search(r"(?i)fixtures?/|/testdata/", path):
            ctx.drop("test_or_fixture")
            continue
        c = ctx.by_hash[r["hash"]]
        if c["is_merge"] or ctx.is_bot(c) or not ctx.is_fix(c) or not TIME_MSG_RX.search(c["message"] or ""):
            continue
        per_path[path].append(r)
    for path, rows in sorted(per_path.items()):
        by_c: dict[str, list] = defaultdict(list)
        for r in rows:
            by_c[r["hash"]].append(r)
        real = {}
        for sha, rs in by_c.items():
            core = [x for x in rs if not TIME_FORMAT_ONLY_RX.search(x["text"])]
            if core:
                real[sha] = core
            else:
                ctx.drop("format_or_log_only")
        if not real:
            continue
        arith = [x for rs in real.values() for x in rs if WALL_ARITH_RX.search(x["text"])]
        if len(real) < 2 and not arith:
            continue
        shas = list(real)
        syms = _dedup([u for sha in shas[:4] for u in _row_syms(ctx, real[sha], sha, path)])[:3]
        yield Hit(
            subject=path,
            score=len(real) * 2 + (2 if arith else 0),
            title=f"{path}: {len(real)} time-related fixes" + (" incl. wall-clock arithmetic" if arith else ""),
            evidence={"fixes": [{"hash": s, "subject": subject_of(ctx.by_hash[s]["message"]),
                                 "line": real[s][0]["text"].strip()[:100]} for s in shas[:6]],
                      "duration_arithmetic": [x["text"].strip()[:100] for x in arith[:3]]},
            commits=shas[:6], diffs=[(s, path) for s in shas[:3]], paths=[path], symbols=syms,
        )


# ---------------------------------------------------------------------------- 14. post-release hotfix cluster
@candidate("post_release_hotfix_cluster")
def post_release_hotfix_cluster(ctx: Ctx):
    """For every release marker (``ctx.tags``; pygit2 refs if that table is empty; then version-bump commits) take the files changed since the previous
    marker; flag >=3 fix commits within 14 days after it touching those files, or a following patch tag (same major.minor)
    within 14 days. rc/pre-release tags without fixes, and docs/dependency-only fixes, are dropped."""
    markers = [(n, sha, ts) for n, sha, ts in _tags(ctx)]
    if len(markers) < 2:
        markers = [(subject_of(c["message"])[:40], c["hash"], c["ts"]) for c in ctx.main_commits
                   if BUMP_RX.search(subject_of(c["message"])) and re.search(r"\d+\.\d+", subject_of(c["message"]))]
    idx = _idx(ctx)
    prev_i = -1
    for k, (name, sha, ts) in enumerate(markers):
        i = idx.get(sha)
        if i is None:
            continue
        rel_files: set[str] = set()
        for c in ctx.commits[prev_i + 1:i + 1]:
            rel_files.update(_paths_of(ctx, c["hash"]))
        prev_i = max(prev_i, i)
        fixes = []
        for c in ctx.commits[i + 1:]:
            if c["ts"] - ts > 14 * DAY:
                if c["ts"] - ts > 20 * DAY:
                    break
                continue
            if c["is_merge"] or ctx.is_bot(c) or not ctx.is_fix(c):
                continue
            ps = [p for p in _paths_of(ctx, c["hash"]) if p in rel_files]
            if not ps:
                continue
            if all(is_docs(p) or DEP_MANIFEST_RX.search(p) or LOCKFILE_RX.search(p) for p in ps):
                ctx.drop("docs_or_dep_only")
                continue
            fixes.append((c, ps))
        mm = re.match(r"v?(\d+)\.(\d+)", name)
        patch = [t for t in markers[k + 1:] if t[2] - ts <= 14 * DAY and mm and
                 re.match(rf"v?{mm.group(1)}\.{mm.group(2)}\.\d+", t[0]) and t[0] != name]
        if len(fixes) < 3 and not (patch and fixes):
            if re.search(r"(?i)rc|alpha|beta|dev", name) and not fixes:
                ctx.drop("prerelease_without_fixes")
            continue
        risky = [p for _, ps in fixes for p in ps if is_config(p) or MIGRATION_RX.search(p) or DOCKERFILE_RX.search(p)]
        yield Hit(
            subject=f"{name}|{sha[:12]}",
            score=len(fixes) * 1.5 + (3 if patch else 0) + min(4, len(risky)),
            title=f"Release {name}: {len(fixes)} fix commits within 14 days" + (f", patch {patch[0][0]} followed" if patch else ""),
            evidence={"release": name, "fixes": [{"hash": c["hash"], "subject": subject_of(c["message"]),
                                                  "days_after": round((c["ts"] - ts) / DAY, 1), "files": ps[:3]}
                                                 for c, ps in fixes[:8]],
                      "patch_tags": [p[0] for p in patch[:3]], "config_migration_docker_fixes": _dedup(risky)[:5]},
            commits=_dedup([sha, *[c["hash"] for c, _ in fixes[:6]]]),
            diffs=[(c["hash"], ps[0]) for c, ps in fixes[:3]], paths=_dedup([ps[0] for _, ps in fixes])[:6],
        )


# ---------------------------------------------------------------------------- 15. synchronized schedules
CRON5_RX = re.compile(r"""(?P<m>[\d*/,\-]+)\s+(?P<h>[\d*/,\-]+)\s+[\d*/,\-?LW]+\s+[\d*/,\-A-Za-z?]+\s+[\d*/,\-A-Za-z?#L]+""")
JITTER_RX = re.compile(r"(?i)\b(jitter|stagger(ed)?|splay|singleflight|single[- ]flight|randomi[sz]ed_?delay|thundering[- ]herd|"
                       r"stampede|cache[- ]stampede|dogpile)\b")


def _cron_slots(ctx: Ctx) -> dict[str, list[dict]]:
    slots: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(ctx.head_files):
        if is_ci(p) or is_test(p) or is_vendored(p) or p.startswith(".github/"):
            continue
        low = p.lower()
        if not (low.endswith((".yaml", ".yml", ".py", ".java", ".kt", ".js", ".ts", ".go", ".cfg", ".conf", ".txt"))
                or "crontab" in low):
            continue
        t = ctx.text_at_head(p)
        if not t or not re.search(r"(?i)cron|schedule|beat|crontab", t):
            continue
        suspended = bool(re.search(r"suspend:\s*true", t))
        for m in re.finditer(r"""schedule:\s*["']?(?P<c>[^"'\n#]+)""", t):
            if "CronJob" in t:
                cm = CRON5_RX.match(m.group("c").strip())
                if cm:
                    slots[f"{cm['m']} {cm['h']}"].append({"path": p, "kind": "k8s CronJob", "suspended": suspended,
                                                          "text": t})
        for m in re.finditer(r"crontab\(([^)]*)\)", t):
            args = m.group(1)
            mi = re.search(r"minute\s*=\s*['\"]?([\w*/,\-]+)", args)
            hr = re.search(r"hour\s*=\s*['\"]?([\w*/,\-]+)", args)
            line_no = t[:m.start()].count("\n")
            ctxt = "\n".join(t.splitlines()[max(0, line_no - 3): line_no + 4])
            slots[f"{mi.group(1) if mi else '*'} {hr.group(1) if hr else '*'}"].append(
                {"path": p, "kind": "celery beat", "suspended": False, "text": ctxt + "\n" + t})
        for m in re.finditer(r"@Scheduled\(\s*cron\s*=\s*\"(?P<c>[^\"]+)\"", t):
            f = m.group("c").split()
            if len(f) >= 6:
                slots[f"{f[1]} {f[2]}"].append({"path": p, "kind": "@Scheduled", "suspended": False, "text": t})
        if "crontab" in low or low.endswith(".cron"):
            for ln in t.splitlines():
                cm = CRON5_RX.match(ln.strip())
                if cm and not ln.strip().startswith("#"):
                    slots[f"{cm['m']} {cm['h']}"].append({"path": p, "kind": "crontab", "suspended": False, "text": ln + t})
    return slots


@candidate("synchronized_schedules_and_ttl_herd")
def synchronized_schedules_and_ttl_herd(ctx: Ctx):
    """(a) HEAD cron slots (k8s CronJob, celery beat crontab(), crontab files, @Scheduled) holding >=4 jobs at one
    minute/hour slot where some job file touches DB/HTTP clients (file-level proxy; CI schedules, suspended jobs and
    every-minute slots excluded). (b) Always: commits adding jitter/stagger/splay/singleflight or whose message names a
    thundering herd / stampede."""
    for slot, jobs in _cron_slots(ctx).items():
        if slot.startswith("* *"):
            continue
        live = [j for j in jobs if not j["suspended"]]
        if len(live) < len(jobs):
            ctx.drop("suspended_job", len(jobs) - len(live))
        paths = _dedup([j["path"] for j in live])
        if len(live) < 4:
            if live:
                ctx.drop("tiny_slot")
            continue
        touching = [j["path"] for j in live if DB_HTTP_RX.search(j["text"])]
        if not touching:
            ctx.drop("no_db_or_http_job")
            continue
        yield Hit(
            subject=f"slot:{slot}",
            score=len(live) + len(touching) * 0.5,
            title=f"{len(live)} scheduled jobs share cron slot '{slot}' (minute hour)",
            evidence={"slot": slot, "jobs": [{"path": j["path"], "kind": j["kind"]} for j in live[:10]],
                      "db_or_http_files": _dedup(touching)[:6]},
            paths=paths[:8],
        )
    adds = ctx.grep(JITTER_RX.pattern, "+", limit=20000)
    by_c: dict[str, list] = defaultdict(list)
    for r in adds:
        if not is_test(r["path"]) and not is_docs(r["path"]) and not is_generated(r["path"]) and \
                not is_vendored(r["path"]):
            by_c[r["hash"]].append(r)
    for c in ctx.commits:
        msg = JITTER_RX.search(c["message"] or "")
        rows = by_c.get(c["hash"], [])
        if not (msg or rows) or c["is_merge"] or ctx.is_bot(c):
            continue
        yield Hit(
            subject=f"herd|{c['hash']}",
            score=(3 if msg else 0) + min(4, len(rows)),
            title=f"Herd mitigation: {subject_of(c['message'])[:90]}",
            evidence={"message_mentions": msg.group(0) if msg else None,
                      "added_lines": [f"{r['path']}: {r['text'].strip()[:100]}" for r in rows[:5]]},
            commits=[c["hash"]], diffs=[(c["hash"], p) for p in _dedup([r["path"] for r in rows])[:2]],
            paths=_dedup([r["path"] for r in rows])[:4],
        )


# ---------------------------------------------------------------------------- 16. embedded expiry
EXPIRY_KEY_RX = (r"(?i)[\"']?(valid_until|not_after|notafter|expires?(_at|_on)?|expiry(_date)?|expiration(_date)?)[\"']?\s*[:=]"
                 r"\s*[\"']?\d{4}-\d{2}-\d{2}")
PIN_RX = r"(?i)sha256/[A-Za-z0-9+/=]{20,}|fingerprint\s*[:=]\s*[\"']?[0-9a-fA-F:]{40,}|pin-?sha256|ssl_assert_fingerprint|public_key_pins"
PUBLIC_CA_RX = re.compile(r"(?i)cacert|ca-bundle|ca_bundle|certifi|cacerts|roots?\.pem|trusted[-_]?(ca|roots)|mozilla")
FIXTURE_RX = re.compile(r"(?i)(^|/)(tests?|fixtures?|testdata|examples?|samples?|docs?|demo)/|test[_-]|[_-]test\b|dummy|snakeoil|selfsigned_test")


def _cert_not_after(text: str):
    from datetime import timezone
    try:
        from cryptography import x509
        m = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
        if not m:
            return None
        cert = x509.load_pem_x509_certificate(m.group(0).encode())
        try:
            return cert.not_valid_after_utc.timestamp()
        except AttributeError:
            return cert.not_valid_after.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


@candidate("embedded_expiry_literal")
def embedded_expiry_literal(ctx: Ctx):
    """(1) PEM certificates in the tree whose notAfter is within 365 days of the latest commit or already past; (2) pin /
    fingerprint strings changed >=2 times in one file; (3) cert files replaced >=2 times; (4) date literals on expiry keys
    that are near/past the latest commit and still at HEAD. Fixtures, public CA bundles and vendored files are dropped;
    repos with cert-manager/ACME/certbot config suppress the cert-file findings."""
    import datetime as dt
    acme = bool(ctx.grep(r"(?i)cert-manager|certbot|letsencrypt|acme\.|ACME", None, limit=1))
    now = ctx.head_ts
    for p in sorted(ctx.head_files):
        if not p.lower().endswith((".pem", ".crt", ".cer", ".cert", ".yaml", ".yml", ".json", ".py", ".env", ".txt", ".conf")):
            continue
        if is_vendored(p):
            ctx.drop("vendored")
            continue
        t = ctx.text_at_head(p) if p.lower().endswith((".pem", ".crt", ".cer", ".cert")) or True else None
        if not t or "BEGIN CERTIFICATE" not in t:
            continue
        if FIXTURE_RX.search(p):
            ctx.drop("fixture")
            continue
        if PUBLIC_CA_RX.search(p) or t.count("BEGIN CERTIFICATE") > 5:
            ctx.drop("public_ca_bundle")
            continue
        na = _cert_not_after(t)
        if na is None or na - now > 365 * DAY:
            continue
        if acme:
            ctx.drop("acme_managed")
            continue
        hist = [c["hash"] for c in ctx.path_commits(p)]
        yield Hit(
            subject=f"cert|{p}",
            score=5 + (3 if na < now else 0) + len(hist) * 0.3,
            title=f"Embedded certificate {p} {'expired' if na < now else 'expires'} "
                  f"{dt.datetime.fromtimestamp(na, dt.timezone.utc):%Y-%m-%d}",
            evidence={"not_after": f"{dt.datetime.fromtimestamp(na, dt.timezone.utc):%Y-%m-%d}",
                      "days_from_latest_commit": round((na - now) / DAY), "history_commits": len(hist)},
            commits=hist[-3:], diffs=[(h, p) for h in hist[-2:]], paths=[p],
        )
    # cert files replaced >= 2 times
    for p, rows in sorted(ctx.changes_by_path.items()):
        if not re.search(r"(?i)\.(pem|crt|cer|cert)$", p) or FIXTURE_RX.search(p) or PUBLIC_CA_RX.search(p) or is_vendored(p):
            continue
        mods = [r for r in rows if r["status"] != "A"]
        if len(mods) >= 2:
            if acme:
                ctx.drop("acme_managed")
                continue
            hs = [r["hash"] for r in mods]
            yield Hit(subject=f"certfile|{p}", score=2 + len(mods),
                      title=f"Certificate file {p} replaced {len(mods)} times",
                      evidence={"replacements": [{"hash": h, "subject": subject_of(ctx.by_hash[h]["message"])}
                                                 for h in hs[:6] if h in ctx.by_hash]},
                      commits=hs[-4:], diffs=[(h, p) for h in hs[-2:]], paths=[p])
    pins: dict[str, list] = defaultdict(list)
    for r in ctx.grep(PIN_RX, "+", limit=5000):
        if FIXTURE_RX.search(r["path"]) or is_vendored(r["path"]) or is_docs(r["path"]) or PUBLIC_CA_RX.search(r["path"]):
            continue
        pins[r["path"]].append(r)
    for p, rows in sorted(pins.items()):
        hs = _dedup([r["hash"] for r in rows])
        if len(hs) >= 2:
            yield Hit(subject=f"pin|{p}", score=2 + len(hs), title=f"Pinned fingerprint in {p} replaced {len(hs)} times",
                      evidence={"lines": [r["text"].strip()[:90] for r in rows[-3:]]},
                      commits=hs[-4:], diffs=[(h, p) for h in hs[-2:]], paths=[p])
    for r in ctx.grep(EXPIRY_KEY_RX, "+", limit=5000):
        p = r["path"]
        if FIXTURE_RX.search(p) or is_vendored(p) or is_docs(p) or is_generated(p) or LOCKFILE_RX.search(p):
            ctx.drop("fixture_or_docs")
            continue
        m = re.search(r"\d{4}-\d{2}-\d{2}", r["text"])
        try:
            ts = dt.datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            continue
        if ts - now > 365 * DAY:
            continue
        t = ctx.text_at_head(p)
        if not t or r["text"].strip() not in t:
            continue
        yield Hit(subject=f"date|{p}|{m.group(0)}", score=3 + (3 if ts < now else 0),
                  title=f"Hard-coded expiry {m.group(0)} in {p}",
                  evidence={"line": r["text"].strip()[:120], "days_from_latest_commit": round((ts - now) / DAY)},
                  commits=[r["hash"]], diffs=[(r["hash"], p)], paths=[p])


# ---------------------------------------------------------------------------- 17. retry on non-idempotent write
RETRY_MARK_RX = re.compile(r"autoretry_for|@retry\b|@backoff\.|\bretry\(|max_retries|self\.retry\(|tenacity|"
                           r"@task\([^)]*retry|retry_on|Retry\(|acks_late|reject_on_worker_lost")
BROAD_RETRY_RX = re.compile(r"autoretry_for\s*=\s*\(?[^)\n]*(Exception|Timeout|ConnectionError|OSError|\bException\b)|"
                            r"retry_on_exception|retry=retry_if_exception_type\(Exception|acks_late\s*=\s*True")
SINK_RX = re.compile(r"requests\.(post|put|patch)|httpx\.(post|put|patch)|\.post\(|INSERT\s+INTO|\.objects\.create\(|"
                     r"\.create\(|stripe\.|\.send_mail\(|sendmail|send_email\(|\.publish\(|send_message|\.put_object\(|"
                     r"\.send_task\(|\.insert_one\(|\.insert\(|charge|sns\.|sqs\.send|payments?\.", re.I)
IDEMP_RX = re.compile(r"(?i)idempoten|Idempotency-Key|ON CONFLICT|get_or_create|update_or_create|INSERT\s+OR\s+(IGNORE|REPLACE)|"
                      r"dedup|setnx|nx\s*=\s*True|upsert|if_not_exists|ConditionExpression|unique|already[_ ](sent|processed|exists)|"
                      r"message_id|request_id|client_token")
DUP_MSG_RX = re.compile(r"(?i)\b(duplicate[sd]?|twice|double[- ](send|charge|post|insert|submit)|more than once|sent (again|multiple))\b")


@candidate("retry_on_non_idempotent_write")
def retry_on_non_idempotent_write(ctx: Ctx):
    """Alive symbols (or their decorator lines, up to 6 above) with a retry / at-least-once marker whose body or callees
    (CALLS depth <=3) hit curated write sinks (HTTP POST, INSERT, create, stripe, send/publish) and show no idempotency
    token anywhere on the path. Broad retry triggers (Exception/timeouts, acks_late) and later duplicate/twice-worded
    commits touching the file boost. Connect-only retries, reads, upserts and tests are dropped."""
    dup_by_path: dict[str, list] = defaultdict(list)
    for c in ctx.commits:
        if DUP_MSG_RX.search(c["message"] or ""):
            for p in _paths_of(ctx, c["hash"]):
                dup_by_path[p].append(c)
    for s in ctx.alive_symbols:
        p = s["path"]
        if s["kind"] not in ("function", "method") or not p.endswith((".py", ".js", ".ts")) or is_test(p) or is_vendored(p):
            continue
        txt = _sym_text(ctx, s)
        if not RETRY_MARK_RX.search(txt):
            continue
        # BFS over callees to depth 3
        seen, frontier, bodies = {s["uid"]}, [s["uid"]], [(s, txt)]
        for _ in range(3):
            nxt = []
            for u in frontier:
                for v in ctx.callees(u):
                    if v in seen or v not in ctx.symbols:
                        continue
                    seen.add(v)
                    nxt.append(v)
                    vs = ctx.symbols[v]
                    if vs["alive"] and not is_test(vs["path"]):
                        bodies.append((vs, vs["body"] or ""))
            frontier = nxt
        all_text = "\n".join(b for _, b in bodies)
        sinks = [(bs, m.group(0)) for bs, b in bodies for m in [SINK_RX.search(b)] if m]
        if not sinks:
            ctx.drop("no_write_sink")
            continue
        if IDEMP_RX.search(all_text):
            ctx.drop("idempotency_token_or_upsert")
            continue
        if re.search(r"(?i)connect|reconnect|wait_for|ping\(", s["name"] or "") and not re.search(r"(?i)\.post\(|INSERT", all_text):
            ctx.drop("connect_only_retry")
            continue
        dups = [c for c in dup_by_path.get(p, []) + [c for bs, _ in sinks for c in dup_by_path.get(bs["path"], [])]]
        broad = bool(BROAD_RETRY_RX.search(txt))
        yield Hit(
            subject=s["uid"],
            score=3 + (2 if broad else 0) + len(dups) * 2 + 0.3 * len(ctx.callers(s["uid"])) + (1 if len(sinks) > 1 else 0),
            title=f"{s['qualname']}: retried scope reaches write '{sinks[0][1]}' without an idempotency token",
            evidence={"retry_marker": RETRY_MARK_RX.search(txt).group(0), "broad_trigger": broad,
                      "sinks": [{"symbol": bs["qualname"], "sink": sk} for bs, sk in sinks[:5]],
                      "duplicate_worded_commits": [{"hash": c["hash"], "subject": subject_of(c["message"])} for c in dups[:3]]},
            commits=_dedup([c["hash"] for c in dups[:3]]), paths=_dedup([p, *[bs["path"] for bs, _ in sinks[:3]]]),
            symbols=_dedup([s["uid"], *[bs["uid"] for bs, _ in sinks[:3]]]),
        )


# ---------------------------------------------------------------------------- 18. config bulk blast radius
OVERLAY_RX = re.compile(r"(?i)(^|/)(overlays?|envs?|environments?|regions?|clusters?|stages?|tenants?|stacks?|sites?)/[^/]+/|"
                        r"values[-.](prod|production|staging|stage|dev|qa|us|eu|ap)[\w.-]*\.ya?ml$|"
                        r"(^|/)(prod|production|staging|stage|qa|dev|us-[\w-]+|eu-[\w-]+|ap-[\w-]+)\.(ya?ml|json|tfvars|env|toml)$")
BASE_RX = re.compile(r"(?i)(^|/)(base|common|shared)/|(^|/)values\.ya?ml$")
IMAGE_BUMP_RX = re.compile(r"(?i)^\s*[+-]?\s*(-\s*)?(image|tag|newTag|digest|appVersion|version)\s*[:=]")
FORMAT_MSG_RX = re.compile(r"(?i)\b(format(ting)?|reformat|lint|whitespace|prettier|indent|rename|renamed|move[sd]?)\b")


@candidate("config_bulk_blast_radius")
def config_bulk_blast_radius(ctx: Ctx):
    """A commit changing >=3 env/region/service overlay files (or a shared base file whose directory has >=3 overlay
    siblings at HEAD) that is followed within 7 days by a revert/fix touching the same files. Bots, image-tag bumps,
    renames and formatting commits are dropped. 'Dependents' of a base are approximated by sibling overlay directories."""
    overlays_at_head = [p for p in ctx.head_files if OVERLAY_RX.search(p)]
    by_dir: dict[str, int] = defaultdict(int)
    for p in overlays_at_head:
        by_dir[p.split("/overlays/")[0] if "/overlays/" in p else p.rsplit("/", 2)[0]] += 1
    idx = _idx(ctx)
    for c in ctx.commits:
        if c["is_merge"] or ctx.is_bot(c) or c["is_bulk"]:
            continue
        fcs = ctx.changes(c["hash"])
        ov = [fc for fc in fcs if OVERLAY_RX.search(fc["path"]) and is_config(fc["path"])]
        base = [fc for fc in fcs if BASE_RX.search(fc["path"]) and is_config(fc["path"]) and not OVERLAY_RX.search(fc["path"])
                and any(n >= 3 for d, n in by_dir.items() if fc["path"].startswith(d.rsplit("/", 1)[0] if "/" in d else ""))]
        if len(ov) < 3 and not base:
            continue
        if any(fc["status"] and fc["status"].startswith("R") for fc in ov):
            ctx.drop("rename")
            continue
        if FORMAT_MSG_RX.search(subject_of(c["message"])):
            ctx.drop("formatting_or_rename")
            continue
        touched = {fc["path"] for fc in ov + base}
        lines = [r for p in list(touched)[:12] for r in ctx.lines_of(c["hash"], p)]
        if lines and all(IMAGE_BUMP_RX.match(r["text"]) for r in lines):
            ctx.drop("image_tag_bump")
            continue
        follow = []
        for d in ctx.commits[idx[c["hash"]] + 1:idx[c["hash"]] + 600]:
            if d["ts"] - c["ts"] > 7 * DAY:
                break
            if d["is_merge"] or ctx.is_bot(d) or not _is_fixish(ctx, d):
                continue
            common = touched & set(_paths_of(ctx, d["hash"]))
            if common:
                follow.append((d, common))
        if not follow:
            continue
        yield Hit(
            subject=c["hash"],
            score=len(ov) + len(base) * 2 + len(follow) * 2 + (2 if any(d["is_revert"] for d, _ in follow) else 0),
            title=f"{len(ov)} overlay files{' + shared base' if base else ''} changed at once, then "
                  f"{len(follow)} fix/revert within 7d",
            evidence={"change": subject_of(c["message"]), "overlay_files": [fc["path"] for fc in ov][:8],
                      "base_files": [fc["path"] for fc in base][:3],
                      "followups": [{"hash": d["hash"], "subject": subject_of(d["message"]), "files": sorted(cm)[:3]}
                                    for d, cm in follow[:4]]},
            commits=[c["hash"], *[d["hash"] for d, _ in follow[:4]]],
            diffs=[(c["hash"], p) for p in sorted(touched)[:3]], paths=sorted(touched)[:8],
        )


# ---------------------------------------------------------------------------- 19. layered retry amplification
ATTEMPT_RXS = [re.compile(p) for p in (r"stop_after_attempt\(\s*(\d+)", r"max_retries\s*=\s*(\d+)", r"\bretries\s*=\s*(\d+)",
                                       r"\btries\s*=\s*(\d+)", r"max_tries\s*=\s*(\d+)", r"attempts?\s*=\s*(\d+)",
                                       r"range\(\s*(\d+)\s*\)", r"\bretry\(\s*(\d+)", r"Retry\(\s*(?:total\s*=\s*)?(\d+)",
                                       r"retries=(\d+)")]
UNCAPPED_RX = re.compile(r"stop_never|retry_forever|max_retries\s*=\s*None|while\s+True:[\s\S]{0,200}sleep|tries\s*=\s*-1|"
                         r"max_tries\s*=\s*None")


def _retry_info(txt: str):
    if not RETRY_MARK_RX.search(txt) and not re.search(r"(?i)for\s+\w+\s+in\s+range\([^)]*\):[\s\S]{0,300}(sleep|except)", txt):
        return None
    if UNCAPPED_RX.search(txt):
        return None, True
    nums = [int(m.group(1)) for rx in ATTEMPT_RXS for m in [rx.search(txt)] if m]
    nums = [n for n in nums if 1 < n <= 1000]
    if "autoretry_for" in txt and not nums:
        nums = [3]   # celery default max_retries
    return (max(nums) if nums else None), False


@candidate("layered_retry_amplification")
def layered_retry_amplification(ctx: Ctx):
    """Retry-bearing symbols connected by CALLS edges (path of 2-6 nodes) where >=2 layers retry and the chain's literal
    attempt counts multiply to >=9 or one layer is uncapped; the chain must touch a network/queue client. Attempt counts
    are regex-extracted literals (celery default 3 for autoretry_for). Also reports k8s Job ``backoffLimit`` >=3 whose
    container command names a module/script containing a retrying symbol. Tests are ignored."""
    info: dict[str, tuple] = {}
    for s in ctx.alive_symbols:
        if s["kind"] not in ("function", "method") or is_test(s["path"]) or is_vendored(s["path"]):
            continue
        txt = _sym_text(ctx, s)
        if "retry" not in txt.lower() and "range(" not in txt and "tenacity" not in txt and "backoff" not in txt:
            continue
        ri = _retry_info(txt)
        if ri:
            info[s["uid"]] = (ri[0], ri[1], bool(NET_RX.search(txt)))
    seen = set()
    for start, (a0, unc0, net0) in info.items():
        parent = {start: None}
        depth = {start: 0}
        prod = {start: (a0 or 1, unc0 or a0 is None)}
        layers = {start: 1}
        net = {start: net0}
        queue = [start]
        while queue:
            u = queue.pop(0)
            if depth[u] >= 5 or len(parent) > 400:
                continue
            for v in sorted(ctx.callees(u)):
                if v in parent or v not in ctx.symbols:
                    continue
                parent[v], depth[v] = u, depth[u] + 1
                vi = info.get(v)
                p, unc = prod[u]
                layers[v] = layers[u] + (1 if vi else 0)
                if vi:
                    p, unc = p * (vi[0] or 1), unc or vi[1] or vi[0] is None
                prod[v] = (p, unc)
                net[v] = net[u] or (vi[2] if vi else bool(NET_RX.search(ctx.symbols[v]["body"] or "")))
                queue.append(v)
                if vi and depth[v] >= 1 and layers[v] >= 2 and net[v] and (p >= 9 or unc):
                    key = (start, v)
                    if key in seen:
                        continue
                    seen.add(key)
                    chain, x = [], v
                    while x is not None:
                        chain.append(x)
                        x = parent[x]
                    chain.reverse()
                    names = [ctx.symbols[x]["qualname"] for x in chain]
                    yield Hit(
                        subject=f"{start}>{v}",
                        score=2 + min(10, p) * 0.5 + (4 if unc else 0) + layers[v],
                        title=f"Retry layers multiply: {' -> '.join(names)} (~{p} attempts{', uncapped layer' if unc else ''})",
                        evidence={"chain": [{"symbol": ctx.symbols[x]["qualname"], "path": ctx.symbols[x]["path"],
                                             "attempts": (info[x][0] if x in info else None),
                                             "uncapped": bool(info[x][1]) if x in info else False} for x in chain],
                                  "attempt_product": p, "uncapped": unc, "retry_layers": layers[v]},
                        paths=_dedup([ctx.symbols[x]["path"] for x in chain]), symbols=chain[:8],
                    )
    # k8s Job backoffLimit combined with an in-code retrying module
    retry_paths = {ctx.symbols[u]["path"] for u in info}
    for p in sorted(ctx.head_files):
        if not K8S_RX.search(p) and not p.endswith((".yaml", ".yml")):
            continue
        t = ctx.text_at_head(p)
        if not t or "kind: Job" not in t and "kind: CronJob" not in t:
            continue
        m = re.search(r"backoffLimit:\s*(\d+)", t)
        if not m or int(m.group(1)) < 3:
            continue
        for cm in re.finditer(r"(?:python\d?\s+(?:-m\s+)?|manage\.py\s+|node\s+)([\w./\-]+)", t):
            tok = cm.group(1).replace(".", "/")
            hit = next((rp for rp in retry_paths if tok.rstrip("/").split("/")[-1] and
                        rp.rsplit(".", 1)[0].endswith(tok.rstrip("/").split("/")[-1])), None)
            if hit:
                yield Hit(subject=f"job|{p}|{hit}", score=4 + int(m.group(1)),
                          title=f"Job {p} backoffLimit={m.group(1)} reruns code that already retries ({hit})",
                          evidence={"backoffLimit": int(m.group(1)), "command_token": cm.group(1), "retrying_file": hit},
                          paths=[p, hit])
                break


# ---------------------------------------------------------------------------- 20. duplicate effect idempotency fix
DUP_FIX_MSG_RX = re.compile(r"(?i)\b(duplicat(e|es|ed|ion)|twice|idempoten\w*|replay(ed)?|double[- ](send|charge|post|insert|submit|"
                            r"write|process)|more than once|re-?deliver\w*|dedup\w*)\b")
DUP_NOISE_MSG_RX = re.compile(r"(?i)(remove|removed|drop|dedup(licate)?|clean ?up|merge)\s+duplicate\s+(code|imports?|lines?|"
                              r"tests?|definitions?|entries|docs?|methods?|keys?|files?|functions?|dependenc\w+|declarations?)|"
                              r"duplicate\s+(react\s+)?keys?\b|duplicate\s+(code|import|warning|label|id)\b")
DEDUPE_ADD_RX = (r"UNIQUE\b|unique_together|UniqueConstraint|unique\s*=\s*True|ON CONFLICT|SETNX|setnx|nx\s*=\s*True|idempoten|"
                 r"INSERT OR IGNORE|get_or_create|update_or_create|on_conflict|upsert|if_not_exists|already[_ ](processed|sent|exists)|"
                 r"dedup|ConditionExpression")


@candidate("duplicate_effect_idempotency_fix")
def duplicate_effect_idempotency_fix(ctx: Ctx):
    """Fix commit with duplicate/twice/idempotency/replay wording whose diff adds a dedupe construct (UNIQUE, ON CONFLICT,
    SETNX, idempotency key ...). 'Remove duplicate code' and UI duplicate-key messages are dropped. The fixed function is
    the one containing the added dedupe line (``diff_lines.symbol``; commit-touched symbols only as a fallback for
    unenriched rows). Siblings (best effort): alive symbols in the same class/file with the same write sink and no dedupe
    token at HEAD, cap 5."""
    adds_by: dict[tuple, list] = defaultdict(list)
    for r in ctx.grep(DEDUPE_ADD_RX, "+", limit=40000):
        adds_by[(r["hash"], r["path"])].append(r)
    done = set()
    for (sha, path), rows in sorted(adds_by.items(), key=lambda kv: ctx.by_hash[kv[0][0]]["seq"]):
        c = ctx.by_hash[sha]
        msg = c["message"] or ""
        if c["is_merge"] or ctx.is_bot(c) or not DUP_FIX_MSG_RX.search(msg) or not (ctx.is_fix(c) or "idempot" in msg.lower()):
            continue
        if DUP_NOISE_MSG_RX.search(msg):
            ctx.drop("duplicate_code_or_ui_keys")
            continue
        if path.endswith((".jsx", ".tsx", ".vue", ".css", ".html")) or is_test(path) or is_docs(path):
            ctx.drop("ui_tests_or_docs")
            continue
        if sha in done:
            continue
        done.add(sha)
        syms = [ctx.symbols[u] for u in _row_syms(ctx, rows, sha, path) if u in ctx.symbols]
        sibs = []
        for s in syms[:2]:
            for o in ctx.alive_symbols:
                if o["uid"] == s["uid"] or o["path"] != s["path"] or o["parent"] != s["parent"] or o["kind"] not in ("function", "method"):
                    continue
                b = o["body"] or ""
                if SINK_RX.search(b) and not IDEMP_RX.search(b):
                    sibs.append(o)
        sibs = sibs[:5]
        yield Hit(
            subject=sha,
            score=3 + len(rows) * 0.5 + min(4, len(sibs)) + (1 if re.search(r"(?i)migrat|\.sql$", path) else 0),
            title=f"Idempotency fix: {subject_of(c['message'])[:90]}",
            evidence={"dedupe_lines": [r["text"].strip()[:110] for r in rows[:5]],
                      "sibling_writers_without_dedupe": [{"symbol": o["qualname"], "path": o["path"]} for o in sibs]},
            commits=[sha], diffs=[(sha, path)], paths=_dedup([path, *[o["path"] for o in sibs]]),
            symbols=_dedup([s["uid"] for s in syms[:3]] + [o["uid"] for o in sibs]),
        )


# ---------------------------------------------------------------------------- 21. fail-open fallback
SECURITY_NAME_RX = re.compile(r"(?i)auth|permission|verify|validate|check|rate|limit|quota|allow|acl|csrf|token|signature|"
                              r"credential|login|access|policy|guard")
SECURITY_PATH_RX = re.compile(r"(?i)auth|permission|security|acl|csrf|session|login|token|policy|middleware")
EXCEPT_LINE_RX = re.compile(r"^\s*(except\b.*:\s*|\}?\s*catch\s*(\([^)]*\))?\s*\{?\s*|rescue\b.*)$")
OPEN_RET_RX = re.compile(r"^\s*(return\s+(True|\[\]|None|\{\}|true|null|nil|allow\w*|Allow\w*|\"\"|0)\b|allowed\s*=\s*True|"
                         r"return\s+\[\]|return\s*;?$)")
OPTIONAL_EXC_RX = re.compile(r"(?i)(ImportError|ModuleNotFoundError|KeyError|AttributeError|DoesNotExist|StopIteration|"
                             r"FileNotFoundError|LookupError|NotImplementedError)")
INFRA_OPEN_RX = r"(?i)fail[_-]?open|failure_?mode_?deny\s*[:=]\s*false|failOpen|failurePolicy:\s*Ignore|fail-open|on_failure:\s*allow"


@candidate("fail_open_fallback_added")
def fail_open_fallback_added(ctx: Ctx):
    """An added ``return True/[]/None/allow`` on the line(s) right after an added except/catch handler, inside a symbol
    named auth/permission/verify/validate/check/rate/limit/quota/... or a security-path file; plus infra lines enabling
    fail-open (``fail_open``, ``failure_mode_deny: false``, ``failurePolicy: Ignore``). Handler-then-return adjacency is
    checked by the diff line numbers of the added block. The named symbol is the one containing the handler line
    (``diff_lines.symbol``), so an unrelated auth-named function edited in the same commit no longer qualifies; only
    unenriched rows fall back to commit-touched symbols. UI code, optional-lookup exceptions and tests are dropped."""
    adds: dict[tuple, dict[int, str]] = defaultdict(dict)
    hsym: dict[tuple, str | None] = {}
    for r in ctx.grep(r"^\s*(except\b.*:|\}?\s*catch\b.*|rescue\b.*|return\b.*|allowed\s*=\s*True)\s*$", "+",
                      path_regex=r"\.(py|js|jsx|ts|tsx|go|rb|java|kt|php|cs)$", limit=120000):
        adds[(r["hash"], r["path"])][r["line"]] = r["text"]
        hsym[(r["hash"], r["path"], r["line"])] = _sym(r)
    for (sha, path), lines in adds.items():
        if is_test(path) or is_vendored(path):
            continue
        for n, t in sorted(lines.items()):
            if not EXCEPT_LINE_RX.match(t):
                continue
            ret = next((lines[n + k] for k in (1, 2, 3) if n + k in lines and OPEN_RET_RX.match(lines[n + k])), None)
            if ret is None:
                continue
            if OPTIONAL_EXC_RX.search(t):
                ctx.drop("optional_lookup")
                continue
            if re.search(r"\.(jsx|tsx|vue)$", path) or re.search(r"(?i)(^|/)(components?|ui|views?|pages?)/", path):
                ctx.drop("ui_default")
                continue
            hs = hsym.get((sha, path, n))
            pool_ = _syms_in(ctx, sha, path) if hs is None else ([ctx.symbols[hs]] if hs in ctx.symbols else [])
            syms = [s for s in pool_ if SECURITY_NAME_RX.search(s["name"] or "")]
            zone = bool(SECURITY_PATH_RX.search(path))
            if not syms and not zone:
                continue
            c = ctx.by_hash[sha]
            yield Hit(
                subject=f"{sha}|{path}|{n}",
                score=3 + (2 if syms else 0) + (1 if zone else 0) + (1 if ctx.is_fix(c) else 0)
                      + (2 if re.match(r"\s*return\s+(True|true|allow)", ret) else 0),
                title=f"Fail-open fallback ({ret.strip()[:30]}) added in {syms[0]['qualname'] if syms else path}",
                evidence={"handler": t.strip()[:100], "fallback": ret.strip()[:60], "commit_subject": subject_of(c["message"]),
                          "named_symbols": [s["qualname"] for s in syms[:3]], "security_path": zone},
                commits=[sha], diffs=[(sha, path)], paths=[path], symbols=[s["uid"] for s in syms[:3]],
            )
    for r in ctx.grep(INFRA_OPEN_RX, "+", limit=5000):
        if is_test(r["path"]) or is_docs(r["path"]) or is_vendored(r["path"]):
            continue
        c = ctx.by_hash[r["hash"]]
        yield Hit(subject=f"infra|{r['hash']}|{r['path']}", score=3,
                  title=f"Fail-open setting added in {r['path']}: {r['text'].strip()[:60]}",
                  evidence={"line": r["text"].strip()[:140], "commit_subject": subject_of(c["message"])},
                  commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"]])


# ---------------------------------------------------------------------------- 22. flag as rollback valve
FLAG_ADD_RX = (r"(os\.environ(\.get)?\b|os\.getenv|\bgetenv\(|process\.env\.|env\.(bool|get)\(|config\.get(bool|boolean)?\(|"
               r"settings\.[A-Z_]+|ENABLE_[A-Z_]+|DISABLE_[A-Z_]+|[A-Z_]*(KILL_?SWITCH|SAFE_MODE|FEATURE)[A-Z_]*|"
               r"flag\s*[:=]|feature_flag|\.is_enabled\()")
FLAG_NAME_RX = re.compile(r"(?i)enable|disable|use_|skip_|legacy|flag|kill|switch|safe|fallback|feature|toggle|bypass|"
                          r"allow_|new_|old_")
VALVE_MSG_RX = re.compile(r"(?i)kill[- ]?switch|disable via|safe[- ]mode|escape hatch|rollback (valve|flag)|opt[- ]out|"
                          r"feature flag|toggle|guard behind")
SAFE_DEFAULT_RX = re.compile(r"(?i)(,\s*|default\s*=\s*)[\"']?(0|1|true|false|on|off|yes|no)[\"']?\s*\)|(==|!=)\s*[\"'](1|true|0|false)[\"']|"
                             r"bool\(|\bbool\b|in\s*\(\s*[\"'](1|true)")


@candidate("flag_added_as_rollback_valve")
def flag_added_as_rollback_valve(ctx: Ctx):
    """Within 14 days after a fix/revert R touching file X, a non-bulk, non-initial commit F adds a boolean env/config read
    with a safe default (or says kill switch / disable via / safe mode) in X. New files, test toggles and tests are
    dropped. 'Safe default' is approximated by literal boolean/0/1 defaults or a bool() wrapper. The attached symbol is
    the one containing the flag line (``diff_lines.symbol``)."""
    for r in ctx.grep(FLAG_ADD_RX, "+", path_regex=r"\.(py|js|jsx|ts|tsx|go|rb|java|kt|yaml|yml|toml|cfg|ini|env)$", limit=80000):
        path = r["path"]
        if is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path):
            continue
        c = ctx.by_hash[r["hash"]]
        if c["is_merge"] or c["is_bulk"] or ctx.is_bot(c):
            continue
        valve_msg = bool(VALVE_MSG_RX.search(c["message"] or ""))
        txt = r["text"]
        flaggy = FLAG_NAME_RX.search(txt) and (SAFE_DEFAULT_RX.search(txt) or re.search(r"(?i)true|false", txt))
        if not (valve_msg or flaggy):
            continue
        if re.search(r"\b(TEST|TESTING|DEBUG|PYTEST|CI)\b", txt):
            ctx.drop("test_toggle")
            continue
        hist = ctx.path_commits(path)
        if not hist or hist[0]["hash"] == c["hash"]:
            ctx.drop("initial_or_new_file")
            continue
        prior = [h for h in hist if h["ts"] < c["ts"] and c["ts"] - h["ts"] <= 14 * DAY and _is_fixish(ctx, h)
                 and h["hash"] != c["hash"]]
        if not prior:
            continue
        R = prior[-1]
        yield Hit(
            subject=f"{r['hash']}|{path}|{txt.strip()[:50]}",
            score=3 + (2 if valve_msg else 0) + (1.5 if R["is_revert"] else 0) + (1 if is_config(path) else 0),
            title=f"Flag added {round((c['ts'] - R['ts']) / DAY, 1)}d after fix/revert on {path}: {txt.strip()[:70]}",
            evidence={"flag_line": txt.strip()[:140], "flag_commit": subject_of(c["message"]),
                      "preceding_fix": {"hash": R["hash"], "subject": subject_of(R["message"])}, "valve_wording": valve_msg},
            commits=[R["hash"], r["hash"]], diffs=[(R["hash"], path), (r["hash"], path)], paths=[path],
            symbols=_row_syms(ctx, [r], r["hash"], path)[:2],
        )


# ---------------------------------------------------------------------------- 23. hook and init ordering
ORDER_RX = (r"helm\.sh/hook-weight|argocd\.argoproj\.io/sync-wave|sync-wave|hook-weight|condition:\s*service_(healthy|started|"
            r"completed_successfully)|wait-for-it|wait-for|wait_for_(db|it|service)|dockerize\s+-wait|initContainers|"
            r"helm\.sh/hook\b|depends_on:\s*\{?.*condition")
DEPLOY_PATH_RX = re.compile(r"(?i)(^|/)(k8s|kubernetes|manifests?|deploy(ment)?s?|charts?|helm|kustomize|overlays|argocd|"
                            r"docker|compose)[^/]*(/|$)|docker-compose|Dockerfile|entrypoint|(^|/)values[^/]*\.ya?ml$|\.sh$")


@candidate("hook_and_init_ordering_change")
def hook_and_init_ordering_change(ctx: Ctx):
    """Deploy-path changes to hook weights / sync waves, depends_on conditions, wait-for scripts and initContainers.
    Emitted per commit when a weight/wave number changes, a condition or wait-for is added/removed; the score grows when
    the same file saw >=3 such commits in the trailing 90 days. Plain depends_on and ``nc -z`` probes, test/CI compose
    files and non-deploy paths are dropped."""
    rows_by_path: dict[str, list] = defaultdict(list)
    for r in ctx.grep(ORDER_RX, None, limit=30000):
        p = r["path"]
        if INFRA_DEV_RX.search(p) or is_test(p) or CI_RX.search(p) or is_docs(p):
            ctx.drop("test_or_ci_or_dev_file")
            continue
        if not DEPLOY_PATH_RX.search(p) and not K8S_RX.search(p):
            ctx.drop("not_deploy_path")
            continue
        if re.search(r"\bnc\s+-z\b", r["text"]):
            ctx.drop("plain_nc_probe")
            continue
        rows_by_path[p].append(r)
    for p, rows in sorted(rows_by_path.items()):
        by_c: dict[str, list] = defaultdict(list)
        for r in rows:
            by_c[r["hash"]].append(r)
        order = sorted(by_c, key=lambda h: ctx.by_hash[h]["seq"])
        for h in order:
            c = ctx.by_hash[h]
            if c["is_merge"] or ctx.is_bot(c):
                continue
            rs = by_c[h]
            signs = {x["sign"] for x in rs}
            weight = [x for x in rs if re.search(r"hook-weight|sync-wave", x["text"])]
            nums = {x["sign"]: re.findall(r'-?\d+', x["text"]) for x in weight}
            weight_changed = bool(weight) and (len(signs) == 2 and nums.get("+") != nums.get("-") or len(signs) == 1)
            cond = [x for x in rs if re.search(r"condition|wait-for|wait_for|dockerize|initContainers", x["text"])]
            window = [o for o in order if 0 <= c["ts"] - ctx.by_hash[o]["ts"] <= 90 * DAY]
            churn = len(window) >= 3
            if not (weight_changed or cond or churn):
                continue
            yield Hit(
                subject=f"{p}|{h}",
                score=2 + (3 if weight_changed else 0) + (2 if cond else 0) + (3 if churn else 0),
                title=f"Init/hook ordering changed in {p}: {subject_of(c['message'])[:70]}",
                evidence={"lines": [f"{x['sign']} {x['text'].strip()[:110]}" for x in rs[:6]],
                          "same_file_ordering_commits_90d": len(window)},
                commits=[h, *[o for o in window[-3:] if o != h]][:4], diffs=[(h, p)], paths=[p],
            )


# ---------------------------------------------------------------------------- 24. max_requests as leak mask
KNOB_RX = (r"(?i)max[_-]?requests(_jitter)?|--max-requests(-jitter)?|maxRequestsPerChild|MaxRequestsPerChild|max_tasks_per_child|"
           r"worker_max_tasks_per_child|maxtasksperchild|worker_max_memory_per_child|pm\.max_requests|max-requests|"
           r"MALLOC_ARENA_MAX|worker_recycle|recycle_workers")
LEAK_NEAR_RX = re.compile(r"(?i)\bleak(s|ed|ing|y)?\b|\boom\b|out of memory|memory (growth|usage|bloat|pressure|creep)|\brss\b|"
                          r"\bkilled\b|memory|OOMKill")
TEMPLATE_RX = re.compile(r"(?i)(^|/)(templates?|cookiecutter[^/]*|scaffold[^/]*|bootstrap[^/]*|boilerplate)/|\.(tmpl|j2|jinja)$")


@candidate("max_requests_leak_mask_and_herd")
def max_requests_leak_mask_and_herd(ctx: Ctx):
    """A worker-recycling knob (max_requests, maxRequestsPerChild, max_tasks_per_child, ...) added, changed or removed within
    +/-5 commits of a leak/OOM/memory-growth message. Evidence records whether a jitter setting exists at HEAD (a fact for the
    analyst, never a filter). Values >=1e6 and bootstrap templates are dropped."""
    jitter_at_head = bool(ctx.grep(r"(?i)max[_-]?requests[_-]?jitter", None, limit=1))
    by_commit: dict[str, list] = defaultdict(list)
    for r in ctx.grep(KNOB_RX, None, limit=20000):
        p = r["path"]
        if is_test(p) or is_docs(p) or is_vendored(p) or is_generated(p):
            continue
        if TEMPLATE_RX.search(p):
            ctx.drop("bootstrap_template")
            continue
        nums = [int(x) for x in re.findall(r"\d+", r["text"].split("jitter")[-1] if "jitter" in r["text"].lower() else r["text"])]
        if nums and max(nums) >= 1_000_000:
            ctx.drop("huge_value")
            continue
        by_commit[r["hash"]].append(r)
    for sha, rows in by_commit.items():
        c = ctx.by_hash[sha]
        if c["is_merge"] or ctx.is_bot(c):
            continue
        near = [n for n in _near(ctx, sha, 5) if LEAK_NEAR_RX.search(n["message"] or "")]
        if not near:
            continue
        removed = [r for r in rows if r["sign"] == "-"]
        added = [r for r in rows if r["sign"] == "+"]
        yield Hit(
            subject=sha,
            score=3 + len(near) * 1.5 + (1.5 if removed and not added else 0) + (0 if jitter_at_head else 1),
            title=f"Worker-recycle knob {'removed' if removed and not added else 'changed'} near memory-leak talk: "
                  f"{subject_of(c['message'])[:80]}",
            evidence={"lines": [f"{r['sign']} {r['path']}: {r['text'].strip()[:100]}" for r in rows[:6]],
                      "leak_worded_neighbours": [{"hash": n["hash"], "subject": subject_of(n["message"])} for n in near[:4]],
                      "jitter_configured_anywhere": jitter_at_head},
            commits=_dedup([sha, *[n["hash"] for n in near[:3]]]),
            diffs=[(sha, p) for p in _dedup([r["path"] for r in rows])[:3]], paths=_dedup([r["path"] for r in rows])[:5],
        )
