"""Catalogue family: security history (CI privileges, CORS, log leaks, CSP, route guards, tenant scoping, CSRF,
access scopes, erasure paths). Everything is derived from ``diff_lines`` plus HEAD/commit blobs; there is no taint or
dataflow analysis, so several signals are textual proxies (stated per builder)."""
from __future__ import annotations

import ast
import re
from collections import defaultdict

from .base import DAY, Ctx, Hit, candidate, is_ci, is_code, is_docs, is_generated, is_test, is_vendored, subject_of, \
    IAC_RX, K8S_RX


# ====================================================================================== shared helpers
def _cached(ctx: Ctx, key: str, fn):
    cache = ctx.__dict__.setdefault("_sec_cache", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _rows_for_paths(ctx: Ctx, paths: list[str], sign: str | None = None) -> list:
    out: list = []
    for i in range(0, len(paths), 400):
        chunk = paths[i:i + 400]
        sql = f"SELECT * FROM diff_lines WHERE path IN ({','.join('?' * len(chunk))})"
        params: list = list(chunk)
        if sign:
            sql += " AND sign=?"
            params.append(sign)
        out.extend(ctx.q(sql, params))
    out.sort(key=lambda r: (r["seq"], r["path"], r["line"]))
    return out


def _msg(ctx: Ctx, sha: str) -> str:
    c = ctx.by_hash.get(sha)
    return (c["message"] or "") if c else ""


def _group(rows: list) -> dict[tuple[str, str], dict[str, list]]:
    out: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in rows:
        out[(r["hash"], r["path"])][r["sign"]].append(r)
    return out


def _head_has(ctx: Ctx, path: str, text: str) -> bool:
    lines = _cached(ctx, "headlines", dict)
    if path not in lines:
        head = ctx.text_at_head(path)
        lines[path] = frozenset(ln.strip() for ln in head.splitlines()) if head else frozenset()
    return text.strip() in lines[path]


def _commented(t: str) -> bool:
    return t.strip().startswith(("#", "//", "*", "/*", "--"))


def _prod(path: str) -> bool:
    return not (is_test(path) or is_docs(path) or is_vendored(path) or is_generated(path))


# ====================================================================================== ci_privilege_and_trigger_widening
CI_WORKFLOW_RX = re.compile(r"(?i)(^|/)\.github/workflows/[^/]+\.ya?ml$|(^|/)\.gitlab-ci\.ya?ml$|(^|/)action\.ya?ml$")
CI_RULES = [
    ("pull_request_target", re.compile(r"\bpull_request_target\b"), 5),
    ("write_all", re.compile(r"permissions\s*:\s*write-all"), 5),
    ("scoped_write", re.compile(r"^\s*(contents|actions|pull-requests|id-token|packages|issues|checks|deployments|"
                                r"statuses|security-events)\s*:\s*write\b"), 2),
    ("secrets_inherit", re.compile(r"secrets\s*:\s*inherit"), 3),
    ("self_hosted", re.compile(r"runs-on\s*:.*self-hosted|^\s*-\s*self-hosted\b"), 3),
    ("unpinned_main", re.compile(r"uses\s*:\s*[\w./-]+@(main|master)\b"), 3),
    ("event_in_run", re.compile(r"\$\{\{\s*github\.(event\.(issue|pull_request|comment|head_commit|review|pages|commits|"
                                r"workflow_run)|head_ref)[^}]*\}\}"), 3),
]
SHA_USES = re.compile(r"uses\s*:\s*([\w./-]+)@([0-9a-f]{40})")
TAG_USES = re.compile(r"uses\s*:\s*([\w./-]+)@(?![0-9a-f]{40}\b)(v?\d[\w.]*|main|master)\b")  # a 40-hex SHA is a pin, not a tag


def _in_run_block(text: str, lineno: int) -> bool:
    lines = text.splitlines()
    if not (0 < lineno <= len(lines)):
        return False
    cur = lines[lineno - 1]
    if re.match(r"^\s*(-\s*)?run\s*:", cur):
        return True
    ind = len(cur) - len(cur.lstrip())
    for k in range(lineno - 2, max(lineno - 40, -1), -1):
        ln = lines[k]
        if not ln.strip():
            continue
        i2 = len(ln) - len(ln.lstrip())
        if i2 < ind:
            return bool(re.match(r"^\s*(-\s*)?run\s*:\s*[|>]", ln))
    return False


@candidate("ci_privilege_and_trigger_widening")
def ci_privilege_and_trigger_widening(ctx: Ctx):
    """CI config gained a risky trigger or permission: ``pull_request_target``, ``write-all`` / scoped ``write``,
    ``secrets: inherit``, ``self-hosted`` runners, an action at ``@main``/``@master``, untrusted ``github.event.*``
    interpolated into a ``run:`` block, or a SHA pin replaced by a tag. ``pull_request_target`` with a PR-head checkout
    is boosted. First-party ``actions/*`` unpinned refs are dropped. When the opt-in GitHub repo facts
    (``ctx.github_repo``) say the repo is private/internal and has no forks or forking is disabled, the spec's
    "private repo without forks" noise filter drops ``pull_request_target`` hits as low risk (no outside PR author can
    reach that trigger). Unchanged when the repo facts are unknown or the repo is public/forkable."""
    repo = ctx.github_repo or {}
    private_nofork = bool(repo) and (bool(repo.get("private")) or repo.get("visibility") in ("private", "internal"))         and (repo.get("allow_forking") is False or repo.get("forks_count") == 0)
    paths = [p for p in ctx.changes_by_path if CI_WORKFLOW_RX.search(p)]
    seen: set[str] = set()
    for (h, path), d in sorted(_group(_rows_for_paths(ctx, paths)).items(), key=lambda kv: kv[1]["+"][0]["seq"] if kv[1]["+"] else 0):
        c = ctx.by_hash[h]
        if not d["+"]:
            continue
        removed_sha = {m.group(1): m.group(2) for r in d["-"] for m in [SHA_USES.search(r["text"])] if m}
        for r in d["+"]:
            t = r["text"]
            if _commented(t):
                continue
            hits: list[tuple[str, int]] = []
            for name, rx, weight in CI_RULES:
                if rx.search(t):
                    hits.append((name, weight))
            m = TAG_USES.search(t)
            if m and m.group(1) in removed_sha:
                hits = [(n, w) for n, w in hits if n != "unpinned_main"]
                hits.append(("sha_pin_reverted_to_tag", 3))
            for rule, weight in hits:
                if rule in ("unpinned_main", "sha_pin_reverted_to_tag") and (m and m.group(1).startswith("actions/")):
                    ctx.drop("first_party_action")
                    continue
                if rule == "pull_request_target" and private_nofork:
                    ctx.drop("private_repo_without_forks")
                    continue
                if rule == "event_in_run":
                    txt = ctx.text_at(h, path) or ""
                    if re.match(r"^\s*(-\s*)?if\s*:", t) or not _in_run_block(txt, r["line"]):
                        ctx.drop("event_not_in_run_block")
                        continue
                key = f"{h}|{path}|{rule}"
                if key in seen:
                    continue
                seen.add(key)
                full = ctx.text_at(h, path) or ""
                head_checkout = rule == "pull_request_target" and bool(
                    re.search(r"github\.event\.pull_request\.head\.(sha|ref)|refs/pull/|github\.head_ref", full)
                    and "checkout" in full)
                still = _head_has(ctx, path, t)
                score = weight + (4 if head_checkout else 0) + (0 if still else -3) + (0 if not ctx.is_bot(c) else -1)
                yield Hit(
                    subject=key, score=max(score, 0.5) * (1.0 if still else 0.4),
                    title=f"{rule} added in {path}: {subject_of(c['message'])[:80]}",
                    evidence={"rule": rule, "line": t.strip()[:160], "pr_head_checkout": head_checkout,
                              "still_present_at_head": still, "added_by": c["author_name"]},
                    commits=[h], diffs=[(h, path)], paths=[path],
                )


# ====================================================================================== cors_policy_change_history
CORS_LINE = r"(?i)(cors|access-control-allow|allow_?origins?|allowed_?origins?|trusted_?origins?)"
CORS_WILD = re.compile(r"""(?ix)((origins?|access-control-allow-origin)["']?\s*[:=,(]\s*[\[(]?\s*["']\*["']|
                           CORS\(\s*\w+\s*\)|origin\s*:\s*true\b|CORS_ORIGIN_ALLOW_ALL\s*=\s*True|
                           CORS_ALLOW_ALL_ORIGINS\s*=\s*True|allow_origins\s*=\s*\[\s*["']\*["'])""")
CORS_CREDS = re.compile(r"(?i)(allow[_-]?credentials|Allow-Credentials|supports_credentials|credentials)\W{0,6}true")
CORS_REFLECT = re.compile(r"""(?ix)\*|(request|req)\.(headers|META)\W.{0,30}origin|origin\s*=\s*(request|req)\.|
                              origin\)\s*;?\s*$|callback\(null,\s*true\)|regex|endswith|\.test\(""")
CORS_MATCH = re.compile(r"""(?ix)origin\w*\.(endswith|startswith|includes|match|search|test|indexOf)\(|
                            re\.(match|search|compile|fullmatch)\([^)]*origin|allow_origin_regex|new\s+RegExp\([^)]*origin|
                            origin\w*\s*=~""")
CORS_LIST = re.compile(r"(?i)(allow(ed)?_?origins?|CORS_ORIGIN\w*|cors_origins?|trusted_origins?)")
LOCAL_RX = re.compile(r"(?i)(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])")


@candidate("cors_policy_change_history")
def cors_policy_change_history(ctx: Ctx):
    """CORS policy loosened: wildcard / allow-all origin, credentials enabled in a file that reflects or wildcards the
    origin, regex/endswith/includes origin matching, or the origin allow-list edited in >=3 commits. Localhost-only
    additions are suppressed; histories made only of single-domain literal additions are dropped (they look like
    ordinary onboarding of known frontends). Public-API detection is not possible, so it is not applied."""
    rows = [r for r in ctx.grep(CORS_LINE, None, limit=100000) if _prod(r["path"]) and not _commented(r["text"])
            and (is_code(r["path"]) or re.search(r"(?i)\.(ya?ml|json|toml|ini|cfg|env|conf|properties)$|\.env", r["path"]))]
    seen: set[str] = set()
    churn: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: {"+": [], "-": []}))
    for (h, path), d in sorted(_group(rows).items(), key=lambda kv: (kv[1]["+"] or kv[1]["-"])[0]["seq"]):
        c = ctx.by_hash[h]
        for r in d["+"]:
            t = r["text"]
            kinds = []
            if CORS_WILD.search(t):
                kinds.append("wildcard_origin")
            if CORS_CREDS.search(t):
                full = ctx.text_at(h, path) or ""
                if CORS_REFLECT.search(full):
                    kinds.append("credentials_with_open_origin")
            if CORS_MATCH.search(t):
                kinds.append("pattern_origin_match")
            for kind in kinds:
                if LOCAL_RX.search(t) and kind != "wildcard_origin":
                    ctx.drop("localhost_only")
                    continue
                key = f"{h}|{path}|{kind}"
                if key in seen:
                    continue
                seen.add(key)
                still = _head_has(ctx, path, t)
                yield Hit(
                    subject=key, score={"wildcard_origin": 4, "credentials_with_open_origin": 6, "pattern_origin_match": 4}[kind]
                    * (1 if still else 0.5),
                    title=f"CORS {kind.replace('_', ' ')} in {path}: {subject_of(c['message'])[:80]}",
                    evidence={"kind": kind, "line": t.strip()[:160], "still_present_at_head": still},
                    commits=[h], diffs=[(h, path)], paths=[path],
                )
        if any(CORS_LIST.search(r["text"]) for r in d["+"] + d["-"]):
            churn[path][h] = d
    for path, commits in churn.items():
        if len(commits) < 3:
            continue
        single = all(not d["-"] and len(d["+"]) == 1 and re.search(r"""["']https?://[\w.\-:]+["']|["'][\w.\-]+\.[a-z]{2,}["']""", d["+"][0]["text"])
                     for d in commits.values())
        if single:
            ctx.drop("repeated_single_domain_additions")
            continue
        allt = " ".join(r["text"] for d in commits.values() for r in d["+"])
        if LOCAL_RX.search(allt) and "*" not in allt and not re.search(r"https?://(?!localhost|127)", allt):
            ctx.drop("localhost_only")
            continue
        order = sorted(commits, key=lambda x: ctx.by_hash[x]["seq"])
        yield Hit(
            subject=f"{path}|origin_churn", score=len(order) * 1.2,
            title=f"CORS origin list in {path} edited in {len(order)} commits",
            evidence={"edits": [{"hash": h[:10], "subject": subject_of(_msg(ctx, h))} for h in order[:8]]},
            commits=order[:6], diffs=[(h, path) for h in order[:3]], paths=[path],
        )


# ====================================================================================== pii_in_log_statement_fix
LOG_RX = r"(?i)(\b(logger|logging|log|console|LOG)\.\w+\(|\bprint\(|\blogf?\(|\.(debug|info|warning|warn|error|critical|exception)\()"
SENS_RX = re.compile(r"(?i)(password|passwd|secret|token|authorization|api[_-]?key|ssn|credit[_-]?card|cvv|cookie|bearer|"
                     r"private[_-]?key|session[_-]?id)")
WHOLE_RX = re.compile(r"(?i)(\b(request|req)\.(headers|json|data|body|form|cookies|args)\b|\bheaders\s*[),]|\bpayload\s*[),}]|"
                      r"\bheaders\)|\{request\}|dict\(request)")
MASK_RX = re.compile(r"(?i)(redact|mask|scrub|\*{3}|sanitiz|hash|\[:\d+\]|\[REDACTED\]|len\()")
SCRUB_MSG = re.compile(r"(?i)\b(redact\w*|mask\w*|scrub\w*|sanitiz\w*|stop logging|don'?t log|do not log|no longer log\w*|"
                       r"leak\w*|secrets? in logs?|remove .{0,20}from logs?)\b")


def _log_rows(ctx: Ctx) -> list:
    return _cached(ctx, "log_rows", lambda: [r for r in ctx.grep(LOG_RX, None, limit=300000)
                                              if is_code(r["path"]) and not is_test(r["path"])])


@candidate("pii_in_log_statement_fix")
def pii_in_log_statement_fix(ctx: Ctx):
    """A commit scrubbed a secret/PII identifier (password, token, authorization, api key ...) out of a log call
    (message redact/mask/scrub, or diff removing/masking the identifier in a log line); report it with sibling log
    calls at HEAD that still interpolate the same identifiers or whole request/headers/payload objects. Siblings that are
    masked or commented out are dropped; a scrub with no sibling left is dropped. (Disabled-debug detection is
    limited to commented-out lines.)"""
    rows = _log_rows(ctx)
    groups = _group(rows)
    scrubs: list[tuple[str, set[str], set[str]]] = []
    for (h, path), d in groups.items():
        c = ctx.by_hash[h]
        if ctx.is_bot(c):
            continue
        rm = [r for r in d["-"] if (SENS_RX.search(r["text"]) or WHOLE_RX.search(r["text"])) and not _commented(r["text"])]
        ad = [r for r in d["+"] if (SENS_RX.search(r["text"]) or WHOLE_RX.search(r["text"])) and not _commented(r["text"])]
        ids_rm = {m.group(1).lower() for r in rm for m in SENS_RX.finditer(r["text"])}
        ids_ad = {m.group(1).lower() for r in ad for m in SENS_RX.finditer(r["text"])
                  if not MASK_RX.search(r["text"])}
        masked_ad = {m.group(1).lower() for r in ad for m in SENS_RX.finditer(r["text"]) if MASK_RX.search(r["text"])}
        whole = bool([r for r in rm if WHOLE_RX.search(r["text"])]) and not [r for r in ad if WHOLE_RX.search(r["text"])]
        ids = (ids_rm - ids_ad) | (masked_ad if SCRUB_MSG.search(c["message"] or "") else set())
        if not ids and not whole:
            continue
        scrubs.append((h, ids, {path}))
    merged: dict[str, tuple[set[str], set[str]]] = {}
    for h, ids, paths in scrubs:
        a, b = merged.setdefault(h, (set(), set()))
        a |= ids
        b |= paths
    candidates = [r for r in rows if r["sign"] == "+" and (SENS_RX.search(r["text"]) or WHOLE_RX.search(r["text"]))]
    for h, (ids, paths) in merged.items():
        c = ctx.by_hash[h]
        if not ids and not SCRUB_MSG.search(c["message"] or ""):
            ctx.drop("not_a_scrub_commit")
            continue
        sib: dict[tuple[str, str], int] = {}
        for r in candidates:
            if r["hash"] == h or _commented(r["text"]):
                continue
            same = {m.group(1).lower() for m in SENS_RX.finditer(r["text"])} & ids
            if not same and not WHOLE_RX.search(r["text"]):
                continue
            if MASK_RX.search(r["text"]):
                ctx.drop("already_masked")
                continue
            if not _head_has(ctx, r["path"], r["text"]):
                continue
            sib.setdefault((r["path"], r["text"].strip()), r["line"])
        if not sib:
            ctx.drop("no_sibling_left")
            continue
        yield Hit(
            subject=h, score=1 + len(sib) * 2 + (1 if SCRUB_MSG.search(c["message"] or "") else 0),
            title=f"log scrub '{subject_of(c['message'])[:70]}' leaves {len(sib)} sibling log call(s) with {', '.join(sorted(ids)[:3]) or 'request objects'}",
            evidence={"scrubbed_identifiers": sorted(ids), "scrubbed_paths": sorted(paths),
                      "siblings_at_head": [{"path": p, "text": t[:140]} for (p, t) in list(sib)[:8]]},
            commits=[h], diffs=[(h, p) for p in sorted(paths)[:3]],
            paths=sorted(paths | {p for (p, _) in sib})[:10],
        )


# ====================================================================================== security_header_csp_policy_change
CSP_LINE = (r"(?i)(content-security-policy|\bcsp\b|script-src|default-src|style-src|connect-src|img-src|frame-src|"
            r"frame-ancestors|font-src|report-only|unsafe-inline|unsafe-eval)")
CSP_DIRECTIVE = re.compile(r"(?i)\b((?:default|script|style|img|connect|font|frame|media|object|child|worker|manifest|form)-src"
                           r"(?:-elem|-attr)?|frame-ancestors|base-uri|form-action)\b")
HOST_RX = re.compile(r"(?<![\w.@-])(?:https?://)?(?:\*\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?![\w-])")
DEV_PATH = re.compile(r"(?i)(dev|local|webpack|vite|vue\.config|rsbuild|devserver)")


@candidate("security_header_csp_policy_change")
def security_header_csp_policy_change(ctx: Ctx):
    """CSP weakened: ``unsafe-inline``/``unsafe-eval`` added, Report-Only switched to enforcing, a wildcard source added,
    or >=5 commits each adding hosts to one directive (same-commit additions collapse). Dev-server configs and pure
    reorders are dropped. Directives split across lines inherit the last directive seen in that file diff; hosts on
    lines without any CSP keyword are invisible to this scan."""
    rows = [r for r in ctx.grep(CSP_LINE, None, limit=100000) if _prod(r["path"]) and not _commented(r["text"])]
    seen: set[str] = set()
    host_commits: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for (h, path), d in sorted(_group(rows).items(), key=lambda kv: (kv[1]["+"] or kv[1]["-"])[0]["seq"]):
        c = ctx.by_hash[h]
        alltxt = " ".join(r["text"] for r in d["+"] + d["-"])
        if DEV_PATH.search(path.rsplit("/", 1)[-1]) or re.search(r"(?i)localhost.*(hot|webpack-dev)|webpack-dev-server|devServer", alltxt):
            ctx.drop("dev_config")
            continue
        tok = lambda rs: sorted(re.findall(r"[\w*.:/'-]+", " ".join(x["text"] for x in rs).lower()))  # noqa: E731
        if d["+"] and d["-"] and tok(d["+"]) == tok(d["-"]):
            ctx.drop("reorder_only")
            continue
        old_unsafe = bool(re.search(r"unsafe-(inline|eval)", " ".join(r["text"] for r in d["-"])))
        old_wild = any(re.search(r"""(^|[\s'"])\*(\s|;|['",]|$)""", r["text"]) for r in d["-"])
        old_hosts = {x for r in d["-"] for x in HOST_RX.findall(r["text"].lower())}
        kinds: list[tuple[str, str]] = []
        cur = ""
        for r in d["+"]:
            t = r["text"]
            dm = CSP_DIRECTIVE.search(t)
            cur = dm.group(1).lower() if dm else cur
            if re.search(r"unsafe-(inline|eval)", t) and not old_unsafe:
                kinds.append(("unsafe_directive_added", t))
            if (dm or cur) and re.search(r"""(^|[\s'"])\*(\s|;|['",]|$)""", t) and not old_wild:
                kinds.append(("wildcard_source", t))
            if cur or dm:
                for hm in HOST_RX.findall(t.lower()):
                    if hm not in old_hosts:
                        host_commits[(path, cur or "?")][h].add(hm)
        if any(re.search(r"(?i)report[-_]?only", r["text"]) for r in d["-"]) and \
                not any(re.search(r"(?i)report[-_]?only", r["text"]) for r in d["+"]):
            kinds.append(("report_only_to_enforcing", next(r["text"] for r in d["-"] if re.search(r"(?i)report[-_]?only", r["text"]))))
        for kind, t in kinds:
            key = f"{h}|{path}|{kind}"
            if key in seen:
                continue
            seen.add(key)
            yield Hit(
                subject=key, score={"unsafe_directive_added": 5, "wildcard_source": 4, "report_only_to_enforcing": 3}[kind]
                * (1 if _head_has(ctx, path, t) or kind == "report_only_to_enforcing" else 0.5),
                title=f"CSP {kind.replace('_', ' ')} in {path}: {subject_of(c['message'])[:80]}",
                evidence={"kind": kind, "line": t.strip()[:180]}, commits=[h], diffs=[(h, path)], paths=[path],
            )
    for (path, directive), by_commit in host_commits.items():
        commits = sorted((h for h, hs in by_commit.items() if hs), key=lambda x: ctx.by_hash[x]["seq"])
        if len(commits) < 5:
            continue
        hosts = sorted({x for hs in by_commit.values() for x in hs})
        yield Hit(
            subject=f"{path}|{directive}|hosts", score=len(commits) * 1.5,
            title=f"{directive} allow-list grew over {len(commits)} commits in {path} ({len(hosts)} hosts)",
            evidence={"directive": directive, "hosts": hosts[:15]}, commits=commits[:6],
            diffs=[(h, path) for h in commits[:3]], paths=[path],
        )


# ====================================================================================== route_guard_partial_rollout
ROUTE_DEC = re.compile(r"(^|\.)(route|get|post|put|patch|delete|options|head|websocket|api_route|api_view|action|"
                       r"websocket_route)$")
GUARD_DEC = re.compile(r"(?i)(login|auth|permission|perm|role|admin|require|protect|jwt|token|scope|csrf|owner|access|guard|"
                       r"staff|superuser)")
NOT_GUARD = re.compile(r"(?i)(cache|wraps|lru|property|staticmethod|classmethod|validator|validates|fixture|route|"
                       r"limiter|ratelimit|errorhandler|doc|swagger|marshal|expect|response|ns\.|api\.)")
EXCLUDED_HANDLER = re.compile(r"(?i)(health|login|logout|signin|signup|register|webhook|metrics|docs|openapi|ping|status|"
                              r"static|favicon|robots|callback|oauth|token|password_reset|index|home)")
INLINE_AUTH = re.compile(r"(?i)(current_user|is_authenticated|abort\(\s*40[13]|check_auth|verify_token|authorize|"
                         r"get_jwt|request\.user|has_perm|HTTPException\(\s*status_code\s*=\s*40[13]|Forbidden|Unauthorized)")


def _dec_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


VIEW_BASE = re.compile(r"(View|MethodView|APIView|ViewSet|ModelViewSet|Resource|Endpoint|HTTPEndpoint|Handler)$")
HTTP_VERBS = {"get", "post", "put", "patch", "delete"}
CLASS_GUARD_ATTRS = ("decorators", "permission_classes")
CLASS_GUARD_LINE = re.compile(r"^\s*(?:decorators|permission_classes)\s*(?::[^=]+)?=\s*[\[(]")


def _names_in(node: ast.expr) -> list[str]:
    """Guard-ish names mentioned by a decorator expression: the decorator itself plus, for
    ``method_decorator(login_required, name='dispatch')``-like wrappers, the wrapped names."""
    out = []
    n = _dec_name(node)
    if n:
        out.append(n)
    if isinstance(node, ast.Call):
        for a in node.args:
            out.extend(_names_in(a))
    return out


def _class_view(n: ast.ClassDef, text: str) -> tuple[set[str], str] | None:
    """(guards, source) when ``n`` is a class-based view: a View/MethodView/APIView-like base, or >=2 HTTP-verb methods.
    Guards = class decorators, ``decorators = [...]`` / ``permission_classes = [...]`` attributes and a
    ``dispatch`` method that checks auth (recorded as ``dispatch_auth_check``)."""
    bases = [_dec_name(b) for b in n.bases]
    verbs = [f.name for f in n.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in HTTP_VERBS]
    if not (any(VIEW_BASE.search(b.rsplit(".", 1)[-1]) for b in bases) or len(verbs) >= 2):
        return None
    guards: set[str] = set()
    for d in n.decorator_list:
        guards.update(_names_in(d))
    for st in n.body:
        if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id in CLASS_GUARD_ATTRS for t in st.targets) \
                and isinstance(st.value, (ast.List, ast.Tuple)):
            for e in st.value.elts:
                guards.update(_names_in(e))
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == "dispatch":
            try:
                seg = ast.get_source_segment(text, st) or ""
            except Exception:
                seg = ""
            if INLINE_AUTH.search(seg) or GUARD_DEC.search(seg):
                guards.add("dispatch_auth_check")
    try:
        body = ast.get_source_segment(text, n) or ""
    except Exception:
        body = ""
    return {g for g in guards if g and not ROUTE_DEC.search(g)}, body


def _handlers(text: str) -> dict[str, tuple[set[str], str]] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: dict[str, tuple[set[str], str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [_dec_name(d) for d in n.decorator_list]
            if any(ROUTE_DEC.search(x) for x in names):
                try:
                    body = ast.get_source_segment(text, n) or ""
                except Exception:
                    body = ""
                out[n.name] = ({x for x in names if x and not ROUTE_DEC.search(x)}, body)
        elif isinstance(n, ast.ClassDef):
            view = _class_view(n, text)
            if view:
                out[n.name] = view
    return out


def _guard_names(line: str) -> list[str]:
    """Guard names a changed line adds/removes: ``@guard`` decorators or entries of ``decorators = [..]`` /
    ``permission_classes = [..]`` class attributes."""
    m = re.match(r"^\s*@\s*([\w.]+)", line)
    if m:
        names = [m.group(1)]
        if "method_decorator" in line:
            names += re.findall(r"[\w.]+", line.split("(", 1)[1]) if "(" in line else []
        return [x for x in names if GUARD_DEC.search(x) and not NOT_GUARD.search(x) and not ROUTE_DEC.search(x)
                and x not in CLASS_GUARD_ATTRS]
    if CLASS_GUARD_LINE.match(line):
        inner = line.split("=", 1)[1]
        return [x for x in re.findall(r"[A-Za-z_][\w.]*", inner)
                if GUARD_DEC.search(x) and not NOT_GUARD.search(x) and x not in CLASS_GUARD_ATTRS]
    return []


@candidate("route_guard_partial_rollout")
def route_guard_partial_rollout(ctx: Ctx):
    """Python route handlers: a commit adds the same guard decorator (login/permission/role/token ...) to >=2 handlers
    of one file, and a handler that already existed before that commit still lacks it at HEAD (with >=2 guarded
    siblings); or a guard was removed from exactly one handler while >=2 others keep it. Health/login/webhook/metrics/
    docs handlers and handlers with inline auth are excluded. "Already existed" is decided from symbol creation times (``first_seq`` before
    the rollout commit) instead of re-reading the old file; the file text is only the fallback. Class-based views are
    modelled as handlers too: a class with a View/MethodView/APIView/ViewSet-like base or >=2 HTTP-verb methods
    (get/post/put/patch/delete), guarded by class decorators (incl. ``method_decorator(...)``), a ``decorators = [...]``
    / ``permission_classes = [...]`` class attribute, or an auth check in ``dispatch`` (counts as other auth). Non-decorator
    middleware is not modelled."""
    rows = [r for r in ctx.grep(r"^\s*(@\s*[\w.]+|(decorators|permission_classes)\s*(:[^=]+)?=\s*[\[(])", None,
                                limit=300000) if r["path"].endswith(".py") and _prod(r["path"])]
    head_cache: dict[str, dict | None] = {}

    def head_handlers(path: str):
        if path not in head_cache:
            t = ctx.text_at_head(path)
            head_cache[path] = _handlers(t) if t else None
        return head_cache[path]

    seen: set[str] = set()
    for (h, path), d in _group(rows).items():
        added: dict[str, int] = defaultdict(int)
        removed: dict[str, int] = defaultdict(int)
        for sign, bucket in (("+", added), ("-", removed)):
            for r in d[sign]:
                for g in _guard_names(r["text"]):
                    bucket[g] += 1
        for guard, n in added.items():
            if n < 2 or removed.get(guard):
                continue
            handlers = head_handlers(path)
            if not handlers:
                continue
            guarded = [k for k, (decs, _) in handlers.items() if guard in decs]
            if len(guarded) < 2:
                ctx.drop("fewer_than_two_guarded_siblings")
                continue
            seq_h = ctx.by_hash[h]["seq"]
            old_defs = {x["name"] for x in ctx.symbols_by_path.get(path, [])
                        if x["first_seq"] is not None and x["first_seq"] < seq_h}
            if not old_defs:  # no symbol creation times for this file: compare against the file text before the commit
                before = ctx.text_before(h, path) or ""
                old_defs = set(re.findall(r"(?m)^\s*(?:async\s+)?(?:def|class)\s+(\w+)", before))
            missing = []
            for name, (decs, body) in handlers.items():
                if guard in decs or name not in old_defs:
                    continue
                if EXCLUDED_HANDLER.search(name):
                    ctx.drop("excluded_handler_name")
                    continue
                if INLINE_AUTH.search(body) or any(GUARD_DEC.search(x) for x in decs):
                    ctx.drop("inline_or_other_auth")
                    continue
                missing.append(name)
            if not missing:
                continue
            key = f"{h}|{path}|{guard}"
            if key in seen:
                continue
            seen.add(key)
            c = ctx.by_hash[h]
            syms = [s["uid"] for s in ctx.symbols_by_path.get(path, []) if s["name"] in missing][:6]
            yield Hit(
                subject=key, score=len(missing) * 3 + len(guarded) * 0.5,
                title=f"@{guard} rolled out to {n} handlers in {path} but {len(missing)} older handler(s) lack it: {', '.join(missing[:4])}",
                evidence={"guard": guard, "added_to": n, "guarded_at_head": guarded[:8], "unguarded_older_handlers": missing[:8],
                          "commit_subject": subject_of(c["message"])},
                commits=[h], diffs=[(h, path)], paths=[path], symbols=syms,
            )
        for guard, n in removed.items():
            if n != 1 or added.get(guard):
                continue
            handlers = head_handlers(path)
            if not handlers:
                continue
            guarded = [k for k, (decs, _) in handlers.items() if guard in decs]
            if len(guarded) < 2:
                continue
            unguarded = [k for k, (decs, body) in handlers.items() if guard not in decs and not EXCLUDED_HANDLER.search(k)
                         and not INLINE_AUTH.search(body) and not any(GUARD_DEC.search(x) for x in decs)]
            if not unguarded:
                ctx.drop("no_unguarded_handler_left")
                continue
            key = f"{h}|{path}|{guard}|removed"
            if key in seen:
                continue
            seen.add(key)
            c = ctx.by_hash[h]
            yield Hit(
                subject=key, score=4 + len(guarded) * 0.5,
                title=f"@{guard} removed from one handler in {path} while {len(guarded)} others keep it",
                evidence={"guard": guard, "still_guarded": guarded[:8], "now_unguarded": unguarded[:6],
                          "commit_subject": subject_of(c["message"])},
                commits=[h], diffs=[(h, path)], paths=[path],
                symbols=[s["uid"] for s in ctx.symbols_by_path.get(path, []) if s["name"] in unguarded][:4],
            )


# ====================================================================================== tenant_filter_sibling_drift
IDONLY = re.compile(r"""(?ix)\.(get|get_or_404|first_or_404|filter_by|find_one|findone|findbyid|find_by_id|find_by|
                         findunique|find_unique|filter|where)\(\s*(?:[A-Z]\w*\.)?(id|pk|uuid|\w+_id|_id)\b\s*(==|=|:|\))[^,]*\)""")
LOOKUP_ANY = re.compile(r"(?i)\.(get|get_or_404|first_or_404|filter_by|find_one|findone|findbyid|find_by_id|find_by|"
                        r"findunique|find_unique|filter|where)\(")
SCOPE_WORD = re.compile(r"(?i)\b(tenant|org(anization)?|account|owner|workspace|team|company|customer|user|project)_?(id)?\b\s*(==|=|:|,|\))")
MODEL_RX = re.compile(r"""\b([A-Z]\w+)\.(?:objects|query)\b|\b([A-Z]\w+)\.(?:find\w*|get\w*|where|filter\w*)\(|"""
                      r"""\.get\(\s*([A-Z]\w+)\s*,|\b(\w+)\.(?:find_one|findOne|findUnique)\(""")
TENANT_MSG = re.compile(r"(?i)\b(idor|cross[- ]tenant|tenant (isolation|leak)|data leak\w*|other (users|tenants|orgs|accounts)|"
                        r"unauthori[sz]ed access|access control|scope (the )?(query|lookup)s?)\b")
ADMIN_CTX = re.compile(r"(?i)(admin|system|cron|celery|tasks?|jobs?|worker|management/commands|scripts?|migrat|(^|/)cli)")


def _model_of(text: str) -> str:
    m = MODEL_RX.search(text)
    return next((g.lower() for g in (m.groups() if m else ()) if g), "?")


@candidate("tenant_filter_sibling_drift")
def tenant_filter_sibling_drift(ctx: Ctx):
    """A commit turned an id-only lookup (``Model.objects.get(id=x)``) into a tenant/owner-scoped one (or its message names
    IDOR / cross-tenant); report sibling id-only lookups on the same model still at HEAD that are not scoped on adjacent
    lines. Model identity is a textual guess from the receiver. Dropped: repos with row-level-security policies, admin/
    system/task code, and tests. Each sibling is attributed to its enclosing function (``diff_lines.symbol``): it must
    still exist at HEAD and still hold the line, and the "scoped on adjacent lines" test reads that function's body
    instead of re-reading the file (file text is only the fallback for module-level lines). There is no ratio-only
    trigger."""
    rls = bool(ctx.grep(r"(?i)row level security|create policy|enable row level", "+", limit=5))
    rows = [r for r in ctx.grep(r"\.(get|get_or_404|first_or_404|filter_by|find_one|findOne|findById|find_by_id|find_by|"
                                r"findUnique|find_unique|filter|where)\(", None, limit=300000)
            if is_code(r["path"]) and not is_test(r["path"])]
    groups = _group(rows)
    idonly_plus = [r for r in rows if r["sign"] == "+" and IDONLY.search(r["text"]) and not SCOPE_WORD.search(r["text"])]
    seen: set[str] = set()
    for (h, path), d in sorted(groups.items(), key=lambda kv: (kv[1]["+"] or kv[1]["-"])[0]["seq"]):
        c = ctx.by_hash[h]
        old = [r for r in d["-"] if IDONLY.search(r["text"]) and not SCOPE_WORD.search(r["text"])]
        new = [r for r in d["+"] if LOOKUP_ANY.search(r["text"]) and SCOPE_WORD.search(r["text"])
               and re.search(r"(?i)\b(id|pk)\b", r["text"])]
        msg_hit = bool(TENANT_MSG.search(c["message"] or ""))
        if not old or not (new or msg_hit):
            continue
        models = {_model_of(r["text"]) for r in old} | {_model_of(r["text"]) for r in new}
        models.discard("?")
        if not models:
            ctx.drop("model_unidentified")
            continue
        if rls:
            ctx.drop("rls_repo")
            continue
        for model in sorted(models):
            sib: dict[tuple[str, str], int] = {}
            sib_sym: dict[tuple[str, str], str] = {}
            for r in idonly_plus:
                if r["hash"] == h or _model_of(r["text"]) != model:
                    continue
                if ADMIN_CTX.search(r["path"]):
                    ctx.drop("admin_or_system")
                    continue
                host = ctx.symbols.get(r["symbol"]) if r["symbol"] else None
                if host is not None:
                    if not host["alive"] or r["text"].strip() not in (host["body"] or ""):
                        ctx.drop("site_gone_at_head")
                        continue
                    lines = (host["body"] or "").splitlines()
                else:
                    lines = (ctx.text_at_head(r["path"]) or "").splitlines()
                    if r["text"].strip() not in {ln.strip() for ln in lines}:
                        continue
                idx = next((i for i, ln in enumerate(lines) if ln.strip() == r["text"].strip()), None)
                if idx is not None and SCOPE_WORD.search(" ".join(lines[idx:idx + 3])):
                    ctx.drop("scoped_on_adjacent_lines")
                    continue
                if host is not None:
                    sib_sym[(r["path"], r["text"].strip())] = host["qualname"]
                sib.setdefault((r["path"], r["text"].strip()), r["line"])
            if not sib:
                ctx.drop("no_sibling_left")
                continue
            key = f"{h}|{model}"
            if key in seen:
                continue
            seen.add(key)
            yield Hit(
                subject=key, score=len(sib) * 2 + (2 if msg_hit else 0),
                title=f"{model} lookup scoped in {subject_of(c['message'])[:60]!r}; {len(sib)} id-only sibling(s) remain",
                evidence={"model": model, "fixed_from": old[0]["text"].strip()[:140],
                          "fixed_to": (new[0]["text"].strip()[:140] if new else ""), "message_flags_idor": msg_hit,
                          "siblings_at_head": [{"path": p, "text": t[:140], "function": sib_sym.get((p, t))}
                                                 for (p, t) in list(sib)[:8]]},
                commits=[h], diffs=[(h, path)], paths=sorted({path} | {p for p, _ in sib})[:10],
            )


# ====================================================================================== csrf_exemption_and_trusted_origin_drift
CSRF_EXEMPT = re.compile(r"(?i)(csrf_exempt|csrf\.exempt|exempt_views|csrf_protect\s*=\s*False|WTF_CSRF_ENABLED\s*=\s*False|"
                         r"WTF_CSRF_CHECK_DEFAULT\s*=\s*False|skip_csrf|disable_csrf|csrf.{0,20}\b(disable|skip|ignore|exclude|"
                         r"exempt)|CSRF_EXEMPT|csrf_exempt_paths)")
CSRF_ORIGIN = re.compile(r"(?i)(CSRF_TRUSTED_ORIGINS|trusted_origins|csrf.{0,20}origin)")
CSRF_MSG = re.compile(r"(?i)(\b403\b|csrf|forbidden|origin)")
SIGNED_BODY = re.compile(r"(?i)(hmac|signature|verify_signature|hexdigest|compare_digest|constant_time|construct_event)")
BEARER_BODY = re.compile(r"(?i)(bearer|headers\.get\(\s*[\"']authorization|x-api-key|jwt_required|api_key_required|token_required)")


@candidate("csrf_exemption_and_trusted_origin_drift")
def csrf_exemption_and_trusted_origin_drift(ctx: Ctx):
    """The Nth (>=3) added CSRF exemption marker (``csrf_exempt``, disabled CSRF flags, exempt lists), or any
    exemption / trusted-origin edit in a commit whose message mentions 403/csrf/forbidden/origin. The handler body at
    HEAD is attached; signed (hmac/signature) handlers and bearer-token APIs are dropped, webhook-named handlers are
    demoted. Tests are excluded."""
    rows = [r for r in ctx.grep(r"(?i)csrf|trusted_origins", "+", limit=100000)
            if _prod(r["path"]) and not _commented(r["text"]) and (CSRF_EXEMPT.search(r["text"]) or CSRF_ORIGIN.search(r["text"]))]
    ordinal = 0
    seen: set[str] = set()
    for r in rows:
        is_ex = bool(CSRF_EXEMPT.search(r["text"]))
        c = ctx.by_hash[r["hash"]]
        if is_ex:
            ordinal += 1
        msg_trigger = bool(CSRF_MSG.search(c["message"] or ""))
        if not ((is_ex and ordinal >= 3) or msg_trigger):
            continue
        if ctx.is_bot(c):
            ctx.drop("bot_commit")
            continue
        key = f"{r['hash']}|{r['path']}|{r['text'].strip()[:60]}"
        if key in seen:
            continue
        seen.add(key)
        body, uid, name = "", [], ""
        head = ctx.text_at_head(r["path"]) or ""
        lines = head.splitlines()
        same = [i for i, ln in enumerate(lines) if ln.strip() == r["text"].strip()]
        idx = min(same, key=lambda i: abs(i + 1 - r["line"])) if same else None
        if idx is not None and is_ex:
            for j in range(idx, min(idx + 6, len(lines))):
                m = re.match(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", lines[j])
                if m:
                    sym = ctx.symbol_at_line(r["path"], j + 1)
                    if sym is not None:
                        body, uid, name = sym["body"] or "", [sym["uid"]], sym["qualname"]
                    break
        if body and SIGNED_BODY.search(body):
            ctx.drop("signed_webhook")
            continue
        if body and BEARER_BODY.search(body):
            ctx.drop("bearer_api")
            continue
        still = idx is not None
        score = (2 + ordinal * 0.5 if is_ex else 2) + (3 if msg_trigger else 0)
        score *= (0.5 if re.search(r"(?i)webhook", name + r["path"]) else 1) * (1 if still else 0.4)
        yield Hit(
            subject=key, score=score,
            title=f"{'exemption #%d' % ordinal if is_ex else 'trusted-origin edit'} in {r['path']}: {subject_of(c['message'])[:80]}",
            evidence={"marker": r["text"].strip()[:140], "ordinal": ordinal if is_ex else None, "message_trigger": msg_trigger,
                      "handler": name, "handler_body": body[:500], "still_present_at_head": still},
            commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"]], symbols=uid,
        )


# ====================================================================================== access_scope_oscillation
POLICY_PATH = re.compile(r"(?i)(policy|policies|iam|rbac|role|permission|acl|security[-_]?group|firewall|network|cidr|bucket|"
                         r"authoriz)[^/]*\.(json|ya?ml|tf|hcl|rego|csv)$|(^|/)rbac/")
SCOPE_LINE = re.compile(r"""(?ix)(\b[a-z0-9-]+:[A-Za-z*][\w*]*\b.*["']|"?Action"?\s*:|"?Resource"?\s*:|verbs\s*:|resources\s*:|
                            \b\d{1,3}(\.\d{1,3}){3}/\d+|cidr|\bPrincipal\b|\*|^\s*-\s*["']?[a-z0-9*:._/-]+["']?\s*,?$)""")
WIDE_RX = re.compile(r"""(?x)(["']\*["']|0\.0\.0\.0/0|::/0|\b[a-z0-9-]+:\*["']?|Resource["']?\s*:\s*["']\*)""")
DENIED_MSG = re.compile(r"(?i)(denied|\b403\b|forbidden|accessdenied|permission|cors|unauthori[sz]ed|can'?t access|cannot access)")
_TRIVIAL = re.compile(r"^[\s\[\]{}(),:;\"'-]*$")


def _norm_line(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().rstrip(",").strip())


@candidate("access_scope_oscillation")
def access_scope_oscillation(ctx: Ctx):
    """In IaC / IAM / RBAC / security-group files: an Action/verb/resource/cidr line was removed and re-added verbatim
    within 30 days (different commits), or ``*`` / ``0.0.0.0/0`` was added by a commit whose message cites
    denied/403/forbidden/permission/cors. Generated policy files and docs are excluded."""
    paths = [p for p in ctx.changes_by_path if (IAC_RX.search(p) or POLICY_PATH.search(p) or
                                                 (K8S_RX.search(p) and re.search(r"(?i)rbac|role", p)))
             and not is_generated(p) and not is_docs(p) and not is_test(p) and not is_vendored(p)]
    rows = _rows_for_paths(ctx, paths)
    removed: dict[tuple[str, str], list] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        t = _norm_line(r["text"])
        if len(t) < 8 or _TRIVIAL.match(t) or not SCOPE_LINE.search(t):
            continue
        if r["sign"] == "-":
            removed[(r["path"], t)].append(r)
            continue
        prev = [x for x in removed.get((r["path"], t), []) if x["hash"] != r["hash"] and 0 <= r["ts"] - x["ts"] <= 30 * DAY]
        if prev:
            x = prev[-1]
            key = f"{r['path']}|{t[:100]}|{x['hash'][:10]}"
            if key not in seen:
                seen.add(key)
                yield Hit(
                    subject=key, score=3 + (30 - (r["ts"] - x["ts"]) / DAY) / 10 + len(prev),
                    title=f"{t[:80]} removed in {x['hash'][:8]} and re-added {round((r['ts'] - x['ts']) / DAY)}d later in {r['path']}",
                    evidence={"line": t[:200], "removed_in": x["hash"], "readded_in": r["hash"],
                              "removal_subject": subject_of(_msg(ctx, x["hash"])), "readd_subject": subject_of(_msg(ctx, r["hash"])),
                              "oscillations": len(prev)},
                    commits=[x["hash"], r["hash"]], diffs=[(x["hash"], r["path"]), (r["hash"], r["path"])], paths=[r["path"]],
                )
        c = ctx.by_hash[r["hash"]]
        if WIDE_RX.search(t) and DENIED_MSG.search(c["message"] or ""):
            key = f"{r['hash']}|{r['path']}|wide|{t[:60]}"
            if key not in seen:
                seen.add(key)
                yield Hit(
                    subject=key, score=4, title=f"scope widened to {t[:60]} after denial: {subject_of(c['message'])[:70]}",
                    evidence={"line": t[:200], "message": subject_of(c["message"])},
                    commits=[r["hash"]], diffs=[(r["hash"], r["path"])], paths=[r["path"]],
                )


# ====================================================================================== pii_field_without_erasure_path
ERASE_NAME = re.compile(r"(?i)(erase|anonymi[sz]e|gdpr|forget|purge_?user|scrub_?user|delete_(user|account|customer|person)|"
                        r"right_to|wipe_?user|redact_?user|remove_?user_?data|delete_?personal)")
ERASE_META = re.compile(r"(?i)(metadata|sorted_tables|__table__\.columns|inspect\(|information_schema|get_fields|_meta\.|"
                        r"\.columns\b|cascade|iter_models|for\s+\w+\s+in\s+.*(tables|models|columns|fields))")
PII_NAME = re.compile(r"(?i)(email|phone|mobile|ssn|dob|birth\w*|address|first_?name|last_?name|full_?name|surname|passport|"
                      r"national_?id|ip_?addr\w*|tax_?id|iban|card_?number|gender)")
SCHEMA_PATH = re.compile(r"(?i)((^|/)(models?|schemas?|entities|entity|migrations?|alembic|db)(/|_|\.)|models?\.py$|schema|"
                         r"\.sql$|\.prisma$)")
COL_DEF = re.compile(r"""(?ix)^\s*(?:ADD\s+(?:COLUMN\s+)?)?["`']?([a-z_]\w*)["`']?\s*
                         (?:=\s*(?:db\.|models\.|sa\.|sqlalchemy\.|Column|fields\.|Field|mapped_column)|
                            :\s*(?:Mapped|str|int|Optional|date|datetime)|\s+(?:varchar|text|string|integer|int|date|timestamp|char|jsonb?)\b)""")
COL_Q = re.compile(r"""Column\(\s*["'](\w+)["']""")


@candidate("pii_field_without_erasure_path")
def pii_field_without_erasure_path(ctx: Ctx):
    """A PII-named column (email, phone, address, name ...) was added to a schema/model/migration file while an
    explicit erasure/anonymisation routine exists at HEAD that does not mention it, and history shows the routine's file
    being edited together (same commit or within 14 days) with at least one earlier PII-column addition. Routines that
    iterate metadata/columns or rely on cascade are dropped; tests and fixtures are excluded."""
    routines = []
    for s in ctx.alive_symbols:
        if s["kind"] not in ("function", "method") or is_test(s["path"]) or not ERASE_NAME.search(s["qualname"] or ""):
            continue
        body = s["body"] or ""
        explicit = len(re.findall(r"""["'][a-z_]{3,}["']""", body)) >= 2 or len(re.findall(r"\.\w+\s*=\s*(None|\"\"|'')", body)) >= 2 \
            or bool(re.search(r"(?i)UPDATE\s+\w+\s+SET\s+\w+\s*=", body))
        if not explicit:
            ctx.drop("routine_not_explicit")
            continue
        if ERASE_META.search(body):
            ctx.drop("metadata_iterating_or_cascade")
            continue
        routines.append(s)
    if not routines:
        return
    paths = [p for p in ctx.changes_by_path if SCHEMA_PATH.search(p) and _prod(p) and not re.search(r"(?i)fixture|seed|sample", p)]
    adds: list[tuple] = []
    for r in _rows_for_paths(ctx, paths, "+"):
        if _commented(r["text"]):
            continue
        m = COL_Q.search(r["text"]) or COL_DEF.match(r["text"])
        if m and PII_NAME.search(m.group(1)):
            if re.search(r"(?i)on\s*delete\s*cascade|ondelete\s*=\s*[\"']CASCADE", r["text"]):
                ctx.drop("cascade_table")
                continue
            adds.append((r["seq"], r["ts"], r["hash"], r["path"], m.group(1).lower(), r["text"].strip()))
    adds.sort()
    uniq: dict[tuple[str, str], tuple] = {}
    for a in adds:
        uniq.setdefault((a[3], a[4]), a)
    adds = sorted(uniq.values())
    seen: set[str] = set()
    for rt in routines:
        rcommits = [c["ts"] for c in ctx.path_commits(rt["path"])]
        body = (rt["body"] or "").lower()

        def coupled(a) -> bool:
            return any(0 <= t - a[1] <= 14 * DAY for t in rcommits)

        for i, a in enumerate(adds):
            if a[1] < (rt["first_ts"] or 0) or a[3] == rt["path"]:
                continue
            earlier = [b for b in adds[:i] if b[1] >= (rt["first_ts"] or 0) and coupled(b)]
            if not earlier:
                ctx.drop("no_prior_coupled_addition")
                continue
            if coupled(a):
                continue
            if a[4] in body or re.sub(r"_", "", a[4]) in re.sub(r"_", "", body):
                ctx.drop("covered_by_routine")
                continue
            if a[4] not in (ctx.text_at_head(a[3]) or "").lower():
                ctx.drop("column_gone_at_head")
                continue
            key = f"{a[2]}|{a[4]}"
            if key in seen:
                continue
            seen.add(key)
            yield Hit(
                subject=key, score=2 + len(earlier) * 1.5,
                title=f"PII column '{a[4]}' added in {a[3]} but erasure routine {rt['qualname']} never updated",
                evidence={"column": a[4], "line": a[5][:140], "routine": rt["qualname"], "routine_path": rt["path"],
                          "earlier_pii_additions_with_routine_edit": [{"column": b[4], "hash": b[2][:10]} for b in earlier[:5]]},
                commits=[a[2], *[b[2] for b in earlier[:3]]], diffs=[(a[2], a[3])], paths=[a[3], rt["path"]],
                symbols=[rt["uid"]],
            )
