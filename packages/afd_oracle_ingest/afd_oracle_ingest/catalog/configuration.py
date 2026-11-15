"""Catalogue family: configuration and deployment settings (TLS, defaults, limits, cookies, key lifecycles, siblings).

All builders search history through the ``diff_lines`` index; the few that need HEAD state read a bounded set of
config / code blobs. Where the spec asks for data Oracle does not keep (per-commit string contracts, file modes) the
builder docstring names the proxy that is used instead.
"""
from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from typing import Any, Callable

import pygit2

from .base import (DAY, DEP_MANIFEST_RX, LOCKFILE_RX, MIGRATION_RX, Ctx, Hit, candidate, flatten, is_ci, is_code,
                   is_config, is_docs, is_fix_message, is_generated, is_test, is_vendored, parse_config,
                   subject_of)

# ---------------------------------------------------------------------------- shared private helpers
_FIXTURE_RX = re.compile(r"(?i)(^|/)(fixtures?|testdata|test_data|examples?|samples?|snapshots?|__snapshots__|mocks?)/")
_DEVCFG_RX = re.compile(r"(?i)(^|[/._-])(dev|development|local|e2e|example|sample|test|testing)([/._-]|$)")
_STRUCT_CFG_RX = re.compile(r"(?i)\.(ya?ml|json|toml|ini|cfg|properties)$|(^|/)\.env[^/]*$|\.env$")
_NOT_APP_CFG_RX = re.compile(r"(?i)(package(-lock)?\.json|tsconfig[^/]*\.json|\.eslintrc|\.prettierrc|pyproject\.toml|"
                             r"renovate|dependabot|babel|jest\.config|\.vscode/|(^|/)\.github/|openapi|swagger|"
                             r"manifest\.json|composer\.json|\.example$|\.sample$|\.dist$|\.template$|\.tmpl$|"
                             r"(^|/)(locales?|i18n|translations?)/|\.schema\.json$|(^|/)\.devcontainer/)")
_EXAMPLE_CFG_RX = re.compile(r"(?i)\.(example|sample|dist|template|tmpl)$|[._-](example|sample|template)\b")
_COMMENT_RX = re.compile(r"^\s*(#|//|/\*|\*|--|;)")
_VAL = r"""(?:"([^"]*)"|'([^']*)'|([^\s,;)\]}]+(?:\s*\*\s*[\w.]+)*))"""
_ENV_TOKEN = r"(dev|development|staging|stage|stg|prod|production|prd|test|qa|uat|local|preprod|sandbox|canary)"


def _noise_path(path: str) -> str | None:
    if is_test(path):
        return "test_path"
    if is_docs(path):
        return "docs_path"
    if is_vendored(path) or is_generated(path):
        return "vendored_or_generated"
    if _FIXTURE_RX.search(path):
        return "fixture_or_example"
    return None


def _group(rows) -> dict[tuple[str, str], tuple[list[str], list[str]]]:
    """grep rows -> {(hash, path): (added texts, removed texts)}."""
    g: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for r in rows:
        a, d = g.setdefault((r["hash"], r["path"]), ([], []))
        (a if r["sign"] == "+" else d).append(r["text"])
    return g


def kv_extractor(key_rx: str) -> Callable[[str], tuple[str, str] | None]:
    """Extractor of (normalised key, value) from a config / code line: ``KEY = v``, ``key: v``, ``key v;``, ``["KEY"] = v``."""
    rx = re.compile(rf"""(?i)(?<!\w)({key_rx})["']?\]?\s*(?:[:=]|\s)\s*{_VAL}""")

    def extract(text: str) -> tuple[str, str] | None:
        if _COMMENT_RX.match(text):
            return None
        m = rx.search(text)
        if not m:
            return None
        val = next((g for g in m.groups()[1:] if g is not None), "")
        return m.group(1).lower(), val.strip()
    return extract


def _kv_events(ctx: Ctx, grep_pat: str, extract: Callable[[str], tuple[str, str] | None], path_regex: str | None = None,
               path_like: str | None = None, limit: int = 60000) -> list[dict]:
    """Per (commit, path, key): old value (removed side) and new value (added side). Oldest first."""
    out: list[dict] = []
    for (h, p), (added, removed) in _group(ctx.grep(grep_pat, None, path_like, path_regex, limit)).items():
        c = ctx.by_hash.get(h)
        if c is None:
            continue
        am: dict[str, str] = {}
        dm: dict[str, str] = {}
        for t in added:
            kv = extract(t)
            if kv:
                am[kv[0]] = kv[1]
        for t in removed:
            kv = extract(t)
            if kv:
                dm.setdefault(kv[0], kv[1])
        for k in sorted(am.keys() | dm.keys()):
            out.append({"hash": h, "seq": c["seq"], "ts": c["ts"], "path": p, "key": k, "old": dm.get(k),
                        "new": am.get(k), "msg": c["message"] or ""})
    out.sort(key=lambda e: (e["seq"], e["path"], e["key"]))
    return out


def _lit(v: Any) -> bool | float | None:
    """Bool or numeric literal (also ``60 * 60`` products); None for anything else."""
    if v is None:
        return None
    s = str(v).strip().strip("\"'").replace("_", "")
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if re.fullmatch(r"-?\d+(\.\d+)?(\s*\*\s*\d+(\.\d+)?)*", s):
        out = 1.0
        for f in s.split("*"):
            out *= float(f)
        return out
    return None


def _size(v: Any) -> float | None:
    """Bytes of ``10m`` / ``16 * 1024 * 1024`` / ``1048576`` (None when not a size)."""
    if v is None:
        return None
    s = str(v).strip().strip("\"'").replace("_", "")
    total, first = 1.0, True
    for f in s.split("*"):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt])?i?b?\s*", f, re.I)
        if not m:
            return None
        n = float(m.group(1))
        if m.group(2) and first:
            n *= 1024 ** ("kmgt".index(m.group(2).lower()) + 1)
        total *= n
        first = False
    return total


def _cache(ctx: Ctx) -> dict:
    return ctx.__dict__.setdefault("_cfg_family_cache", {})


def _commit_paths(ctx: Ctx, sha: str) -> list[str]:
    return [r["path"] for r in ctx.changes(sha)]


def _bulk_or_missing(ctx: Ctx, sha: str) -> bool:
    c = ctx.by_hash.get(sha)
    return c is None or bool(c["is_bulk"])


def _head_config_files(ctx: Ctx) -> list[str]:
    cache = _cache(ctx)
    if "cfg_files" not in cache:
        out = []
        for p in sorted(ctx.head_files):
            if not _STRUCT_CFG_RX.search(p) or _noise_path(p) or _NOT_APP_CFG_RX.search(p):
                continue
            if DEP_MANIFEST_RX.search(p) or LOCKFILE_RX.search(p) or is_ci(p) or _DEVCFG_RX.search(p):
                continue
            out.append(p)
        cache["cfg_files"] = out[:200]
    return cache["cfg_files"]


def _head_config_keys(ctx: Ctx) -> dict[str, list[tuple[str, str]]]:
    """path -> [(flattened key, last segment)] for scalar keys of the parseable config files at HEAD."""
    cache = _cache(ctx)
    if "cfg_keys" not in cache:
        out: dict[str, list[tuple[str, str]]] = {}
        for p in _head_config_files(ctx):
            flat = flatten(parse_config(p, ctx.text_at_head(p)), max_items=2000)
            keys = []
            for k in flat:
                last = k.rsplit(".", 1)[-1]
                if last.endswith("]"):
                    continue
                keys.append((k, last))
            out[p] = keys
        cache["cfg_keys"] = out
    return cache["cfg_keys"]


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s).lower()


def _code_index(ctx: Ctx) -> dict:
    """Identifier counts (normalised: lower-case, no separators) and quoted literals over production code at HEAD."""
    cache = _cache(ctx)
    if "code_idx" not in cache:
        idents: Counter[str] = Counter()
        quoted: dict[str, list[str]] = defaultdict(list)
        budget = 60_000_000
        for p in sorted(ctx.head_files):
            if not is_code(p) or is_test(p) or is_docs(p) or budget <= 0:
                continue
            text = ctx.text_at_head(p)
            if not text or len(text) > 400_000:
                continue
            budget -= len(text)
            for w in re.findall(r"\w+", text):
                if len(w) > 3:
                    idents[_norm(w)] += 1
            for s in set(re.findall(r"""["'`]([^"'`\n]{3,80})["'`]""", text)):
                lst = quoted[s]
                if len(lst) < 6:
                    lst.append(p)
            for s in set(re.findall(r"[\w][\w.\-]{4,}", text)):
                if "." in s or "-" in s:
                    idents[_norm(s)] += 1
        cache["code_idx"] = {"idents": idents, "quoted": quoted}
    return cache["code_idx"]


_GENERIC_KEYS = {"name", "version", "description", "enabled", "debug", "timeout", "default", "value", "values",
                 "type", "path", "paths", "host", "port", "url", "user", "username", "password", "level", "format",
                 "options", "settings", "config", "environment", "namespace", "labels", "annotations", "image",
                 "command", "args", "env", "title", "mode", "language", "source", "target", "include", "exclude"}

_SIZE_KEY = (r"client_max_body_size|max_?content_?length|max_?form_?memory_?size|data_upload_max_\w+|file_upload_max_\w+|"
             r"max_?body_?(?:size|length|bytes)?|body_?(?:size_?)?limit|max_?payload(?:_?size)?|payload_?(?:size_?)?limit|"
             r"max_?request_?(?:body_?)?size|max_?message_?(?:size|bytes|length)|message\.max\.bytes|max\.request\.size|"
             r"fetch\.max\.bytes|max\.partition\.fetch\.bytes|replica\.fetch\.max\.bytes|"
             r"max_?(?:send|recv|receive)_?message_?(?:length|size)|max-(?:inbound|outbound)-message-size|"
             r"max_?body_?length|max_?upload_?size|grpc[\w.]*max[\w.]*(?:size|length)|limit_request_body")


# ---------------------------------------------------------------------------- 1. TLS / HSTS
_HSTS_LINE = re.compile(r"(?i)strict-transport-security|hsts")
_MAXAGE = re.compile(r"(?i)(?:max-age\s*=\s*|HSTS_SECONDS\s*[=:]\s*|hsts[\w_]*(?:max_?age|seconds)[\"']?\s*[=:]\s*)(\d+)")
_TLS_LINE = re.compile(r"(?i)ssl_protocols|ssl_min_protocol|min_?protocol|min_?tls|tls_?min|minimum_?protocol|"
                       r"sslprotocol|ssl_?version|tls_?version|minversion|ssl_options")
_TLS_VER = re.compile(r"(?i)TLS_?v?_?1(?:[._]?([0-3]))?(?!\d)")
_TLS_PAT = (r"(?i)strict-transport-security|hsts|ssl_protocols|ssl_min_protocol|min_?protocol|min_?tls|tls_?min|"
            r"minimum_?protocol|sslprotocol|tls_?version|minversion")


def _hsts_state(lines: list[str]) -> dict:
    ages, floors, sub, pre = [], [], False, False
    for t in lines:
        if _HSTS_LINE.search(t):
            ages += [int(x) for x in _MAXAGE.findall(t)]
            if re.search(r"(?i)include_?subdomains\s*(?:[:=]\s*)?(?:true|[\"']?1)?|includeSubDomains", t) and \
                    not re.search(r"(?i)(subdomains\w*\s*[:=]\s*(false|0))", t):
                sub = True
            if re.search(r"(?i)\bpreload\b", t) and not re.search(r"(?i)preload\w*\s*[:=]\s*(false|0)", t):
                pre = True
        if _TLS_LINE.search(t):
            vs = [int(m.group(1) or 0) for m in _TLS_VER.finditer(t)]
            if vs:
                floors.append(min(vs))
    return {"age": max(ages) if ages else None, "floor": min(floors) if floors else None, "sub": sub, "pre": pre}


@candidate("tls_hsts_policy_ratchet")
def tls_hsts_policy_ratchet(ctx: Ctx):
    """HSTS ``max-age`` raised >=10x, includeSubDomains / preload added, TLS floor lowered (min of TLSv1.x tokens on
    ssl_protocols / min-version lines), or a raise undone within 30 commits (a later event on the same path lowering
    the age again). Per (commit, path) state comparison; numeric parse of max-age only on HSTS lines."""
    events: list[dict] = []
    for (h, p), (added, removed) in _group(ctx.grep(_TLS_PAT, None, limit=30000)).items():
        c = ctx.by_hash.get(h)
        if c is None:
            continue
        reason = _noise_path(p)
        if reason or _DEVCFG_RX.search(p) or re.search(r"(?i)\.(pem|crt|key)$", p):
            ctx.drop(reason or "dev_or_test_config")
            continue
        a, d = _hsts_state(added), _hsts_state(removed)
        kinds = []
        if a["age"] and d["age"] and a["age"] >= 10 * d["age"]:
            kinds.append(f"max-age raised {d['age']} -> {a['age']}")
        if a["sub"] and not d["sub"]:
            kinds.append("includeSubDomains added")
        if a["pre"] and not d["pre"]:
            kinds.append("preload added")
        if a["floor"] is not None and d["floor"] is not None and a["floor"] < d["floor"]:
            kinds.append(f"TLS floor lowered 1.{d['floor']} -> 1.{a['floor']}")
        events.append({"hash": h, "seq": c["seq"], "ts": c["ts"], "path": p, "a": a, "d": d, "kinds": kinds})
    events.sort(key=lambda e: (e["seq"], e["path"]))
    for i, e in enumerate(events):
        if not e["kinds"]:
            continue
        reverted = None
        if e["a"]["age"] and e["d"]["age"]:
            reverted = next((f for f in events[i + 1:] if f["path"] == e["path"] and f["seq"] - e["seq"] <= 30
                             and f["a"]["age"] and f["d"]["age"] and f["a"]["age"] < f["d"]["age"]), None)
        lowered = any(k.startswith("TLS floor lowered") for k in e["kinds"])
        score = 3.0 * lowered + 2.0 * (not lowered) + (3.0 if reverted else 0) + 0.5 * len(e["kinds"])
        yield Hit(
            subject=f"{e['hash']}|{e['path']}", score=score,
            title=f"{e['path']}: {'; '.join(e['kinds'])}" + (" (raise undone shortly after)" if reverted else ""),
            evidence={"changes": e["kinds"], "before": e["d"], "after": e["a"],
                      "reverted_in": reverted["hash"] if reverted else None},
            commits=[e["hash"]] + ([reverted["hash"]] if reverted else []),
            diffs=[(e["hash"], e["path"])] + ([(reverted["hash"], e["path"])] if reverted else []), paths=[e["path"]])


# ---------------------------------------------------------------------------- 2. dead_config_key
_LIMITY = re.compile(r"(?i)limit|max|min|timeout|ttl|size|threshold|retr(y|ies)|interval|quota|rate|capacity|"
                     r"concurren|workers|batch|expir|window|budget|cap$|cap_")


@candidate("dead_config_key")
def dead_config_key(ctx: Ctx):
    """A limit-like or project-prefixed key defined in a HEAD config file whose normalised name appears nowhere in
    production code at HEAD, although a code line mentioning it was removed earlier (the former consumer), by a commit
    that did not touch the config file. Keys whose parent section is referenced in code are treated as dynamically
    looked up and dropped (proxy for "dynamic key construction")."""
    keys = _head_config_keys(ctx)
    idx = _code_index(ctx)
    idents: Counter = idx["idents"]
    proj = [t for t in re.split(r"[-_.\s]+", ctx.repo_id.lower()) if len(t) >= 4]
    cand: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # normalised last -> [(path, flat, last)]
    for p, ks in keys.items():
        for flat, last in ks:
            n = _norm(last)
            if len(n) < 5 or last.lower() in _GENERIC_KEYS:
                if n:
                    ctx.drop("generic_name")
                continue
            if not (_LIMITY.search(last) or any(last.lower().startswith(t) for t in proj)):
                ctx.drop("not_limit_like_or_project_key")
                continue
            if idents.get(n) or idents.get(_norm(flat)):
                ctx.drop("consumer_present")
                continue
            parent = flat.rsplit(".", 1)[0] if "." in flat else ""
            if parent and idents.get(_norm(parent.rsplit(".", 1)[-1])):
                ctx.drop("dynamic_lookup_prefix")
                continue
            cand[n].append((p, flat, last))
    if not cand:
        return
    removed_last: dict[str, tuple[int, str, str]] = {}
    for r in ctx.stream("SELECT hash, seq, path, text FROM diff_lines WHERE sign='-' ORDER BY seq"):
        if not is_code(r["path"]) or is_test(r["path"]) or is_docs(r["path"]):
            continue
        for w in set(re.findall(r"[\w][\w.\-]*", r["text"])):
            n = _norm(w)
            if n in cand:
                removed_last[n] = (r["seq"], r["hash"], r["path"])
    for n, items in cand.items():
        if n not in removed_last:
            ctx.drop("never_had_consumer", len(items))
            continue
        seq, h, code_path = removed_last[n]
        if _bulk_or_missing(ctx, h):
            ctx.drop("bulk_removal", len(items))
            continue
        touched = set(_commit_paths(ctx, h))
        for p, flat, last in items:
            if p in touched:
                ctx.drop("config_touched_with_consumer_removal")
                continue
            c = ctx.by_hash[h]
            age_days = (ctx.head_ts - c["ts"]) / DAY
            score = 2.0 + min(3.0, age_days / 180) + (1.0 if _LIMITY.search(last) else 0)
            yield Hit(
                subject=f"{p}|{flat}", score=score,
                title=f"{p}: key {flat!r} has no reader at HEAD (last consumer removed in {h[:8]})",
                evidence={"key": flat, "config": p, "last_consumer_removed_in": code_path,
                          "removal_subject": subject_of(c["message"]), "days_since_removal": round(age_days)},
                commits=[h], diffs=[(h, code_path)], paths=[p, code_path])


# ---------------------------------------------------------------------------- 3. env var contract break
_ENV_READ = re.compile(r"""(?:os\.environ\[\s*|os\.(?:environ\.get|getenv)\(\s*|process\.env\.|process\.env\[\s*|"""
                       r"""import\.meta\.env\.|os\.Getenv\(\s*|System\.getenv\(\s*|ENV\[\s*|ENV\.fetch\(\s*)["']?"""
                       r"""([A-Z][A-Z0-9_]{2,})""")
_ENV_DENY = re.compile(r"^(PATH|HOME|USER|SHELL|PWD|CI|PORT|HOST|HOSTNAME|DEBUG|LANG|LC_\w+|TERM|TMPDIR|TEMP|TMP|TZ|"
                       r"NODE_ENV|PYTHONPATH|PYTHONUNBUFFERED|VIRTUAL_ENV|EDITOR|DISPLAY|COLORTERM|NO_COLOR|FORCE_COLOR|"
                       r"APPDATA|LOCALAPPDATA|USERPROFILE|SYSTEMROOT|COMSPEC|LOGNAME|OLDPWD|XDG_\w+|GITHUB_\w+|"
                       r"RUNNER_\w+|PYTEST_\w+|CIRCLECI|TRAVIS|GITLAB_\w+|JENKINS_\w+|BUILD_\w+|KUBERNETES_\w+|"
                       r"AWS_REGION|AWS_DEFAULT_REGION|HTTPS?_PROXY|NO_PROXY|ENV|ENVIRONMENT|APP_ENV|FLASK_\w+)$")
_ENV_DEFAULTED = re.compile(r"(?i)(getenv|environ\.get|\.fetch|Getenv)\([^)]*,\s*\S|\|\||\?\?|\bor\b|\bdefault\b|\bif\b")
_PRODUCER_PATH = re.compile(r"(?i)(^|/)\.env[^/]*$|\.env$|docker-compose|compose\.ya?ml|Dockerfile|\.github/workflows/|"
                            r"\.gitlab-ci|helm|k8s|kubernetes|deploy|values[^/]*\.ya?ml|\.tf$|\.tfvars$|Procfile|"
                            r"app\.ya?ml|serverless\.ya?ml|\.(ini|toml|cfg)$")
_ENV_FIXMSG = re.compile(r"(?i)\b(env(ironment)?\b|keyerror|missing|not set|unset|undefined|config)")


@candidate("env_var_config_contract_break")
def env_var_config_contract_break(ctx: Ctx):
    """(a) A commit that introduces an env-var consumer in production code without touching any producer / deploy
    file, followed within 30 commits and 14 days by a fix commit that mentions the variable (in its patch or its
    message) or an env / KeyError / missing message; (c) a consumer at HEAD (contracts table) with no producer and no
    default in the read expression, when the repo has env producer files. Platform variables are denylisted."""
    produced = {c["key"] for c in ctx.contracts if c["kind"] == "env" and c["role"] == "producer"}
    # (a) history
    first_add: dict[str, dict] = {}
    for r in ctx.grep(r"os\.environ|os\.getenv|process\.env|import\.meta\.env|os\.Getenv|System\.getenv|ENV\[|ENV\.fetch",
                      "+", limit=40000):
        p = r["path"]
        if not is_code(p) or is_test(p):
            continue
        for m in _ENV_READ.finditer(r["text"]):
            key = m.group(1)
            if key not in first_add:
                first_add[key] = {"hash": r["hash"], "seq": r["seq"], "ts": r["ts"], "path": p, "text": r["text"].strip()}
    for key, e in sorted(first_add.items(), key=lambda kv: kv[1]["seq"]):
        if _ENV_DENY.match(key) or key.startswith("TEST_"):
            ctx.drop("platform_or_test_var")
            continue
        if _ENV_DEFAULTED.search(e["text"]):
            ctx.drop("defaulted_optional")
            continue
        if any(_PRODUCER_PATH.search(p) for p in _commit_paths(ctx, e["hash"])) or _bulk_or_missing(ctx, e["hash"]):
            ctx.drop("producer_touched_in_same_commit")
            continue
        fix = None
        for c in _commits_after(ctx, e["seq"])[:30]:
            if c["ts"] - e["ts"] > 14 * DAY:
                break
            msg = c["message"] or ""
            if not is_fix_message(msg) and not _ENV_FIXMSG.search(subject_of(msg)):
                continue
            wired = key in msg or any(key in ln["text"] for ln in ctx.lines_of(c["hash"]))
            if wired or re.search(r"(?i)keyerror|env(ironment)? var|missing env", msg):
                fix = c
                break
        if fix is None:
            continue
        yield Hit(
            subject=f"a|{key}|{e['hash']}", score=4.0 + (1.0 if key in (fix["message"] or "") else 0),
            title=f"{key} read in {e['path']} without a producer; wiring/fix followed in {fix['hash'][:8]}",
            evidence={"variant": "consumer_added_without_producer_then_fix", "key": key, "consumer_line": e["text"][:160],
                      "fix_subject": subject_of(fix["message"]), "commits_between": fix["seq"] - e["seq"]},
            commits=[e["hash"], fix["hash"]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])
    # (c) HEAD consumers with no producer and no default
    if not produced:
        return
    by_key: dict[str, list] = defaultdict(list)
    for c in ctx.contracts:
        if c["kind"] == "env" and c["role"] == "consumer":
            by_key[c["key"]].append(c)
    for key, cons in sorted(by_key.items()):
        if key in produced:
            continue
        if _ENV_DENY.match(key) or key.startswith("TEST_"):
            ctx.drop("platform_or_test_var")
            continue
        real = [c for c in cons if not is_test(c["path"])]
        if not real:
            ctx.drop("test_only_var")
            continue
        undefaulted = []
        for c in real:
            lines = (ctx.text_at_head(c["path"]) or "").splitlines()
            line = lines[c["line"] - 1] if 0 < c["line"] <= len(lines) else ""
            if _ENV_DEFAULTED.search(line):
                continue
            undefaulted.append(c)
        if not undefaulted:
            ctx.drop("defaulted_optional")
            continue
        yield Hit(
            subject=f"c|{key}", score=2.0 + min(3.0, 0.5 * len(undefaulted)),
            title=f"{key} is read at {undefaulted[0]['path']}:{undefaulted[0]['line']} but no file produces it",
            evidence={"variant": "consumer_without_producer_or_default", "key": key,
                      "consumers": [f"{c['path']}:{c['line']}" for c in undefaulted[:6]],
                      "producer_files_in_repo": len(produced)},
            paths=sorted({c["path"] for c in undefaulted})[:6])


# ---------------------------------------------------------------------------- 4. config_default_flip
_DEFAULT_PATS = [
    re.compile(r"""(?:getenv|environ\.get|environ\.setdefault|Getenv|env\.get|env_int|env_float|env_bool)\(\s*["']([A-Za-z_][\w.]*)["']\s*,\s*([^,)]+)"""),
    re.compile(r"""process\.env\.(\w+)\s*(?:\|\||\?\?)\s*([^;,)\s]+)"""),
    re.compile(r"""add_argument\(\s*["'](--[\w-]+)["'][^)]*?\bdefault\s*=\s*([^,)]+)"""),
    re.compile(r"""\b(\w+)\s*(?::[^=]+)?=\s*Field\(\s*(?:default\s*=\s*)?([^,)]+)"""),
    re.compile(r"""SetDefault\(\s*["']([\w.]+)["']\s*,\s*([^)]+)\)"""),
]
_DEFAULTS_FILE = re.compile(r"(?i)(^|/)(values|defaults?|default[_-]?config|config\.default|settings\.default)[^/]*\.(ya?ml|json|toml)$")
_DEF_GREP = (r"getenv|environ\.get|environ\.setdefault|Getenv|env\.get|env_int|env_float|env_bool|process\.env\.\w+\s*(\|\||\?\?)|"
             r"add_argument|Field\(|SetDefault")


def _default_extract(path: str) -> Callable[[str], tuple[str, str] | None]:
    def extract(text: str) -> tuple[str, str] | None:
        if _COMMENT_RX.match(text):
            return None
        for rx in _DEFAULT_PATS:
            m = rx.search(text)
            if m:
                return m.group(1), m.group(2).strip()
        return None
    return extract


@candidate("config_default_flip")
def config_default_flip(ctx: Ctx):
    """A numeric / bool default literal changed (getenv default, argparse default, pydantic Field, viper SetDefault,
    values/defaults files) by >=2x or flipped, for a key with >=2 read-sites (contracts consumers or identifier
    occurrences in HEAD code) or living in a shipped defaults file. Skips format-only edits; boosts commits that touched
    no docs/changelog. Moves between files never pair (pairing is per commit and path)."""
    events: list[dict] = []
    for (h, p), (added, removed) in _group(ctx.grep(_DEF_GREP, None, limit=60000)).items():
        events += _pair_defaults(ctx, h, p, added, removed, _default_extract(p))
    rows = ctx.q("SELECT * FROM diff_lines WHERE path REGEXP ? ORDER BY seq, path, line",
                 (r"(?i)(^|/)(values|defaults?|default[_-]?config|config\.default|settings\.default)[^/]*\.(ya?ml|json|toml)$",))
    kv = kv_extractor(r"[A-Za-z_][\w.\-]*")
    for (h, p), (added, removed) in _group(rows).items():
        events += _pair_defaults(ctx, h, p, added, removed, kv)
    consumers = Counter(c["key"] for c in ctx.contracts if c["role"] == "consumer" and c["kind"] in ("env", "config", "flag"))
    idents = _code_index(ctx)["idents"]
    for e in events:
        p = e["path"]
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        if _bulk_or_missing(ctx, e["hash"]):
            ctx.drop("bulk_commit")
            continue
        old, new = _lit(e["old"]), _lit(e["new"])
        if old is None or new is None:
            continue
        if old == new:
            ctx.drop("format_only")
            continue
        if isinstance(old, bool) != isinstance(new, bool):
            continue
        if isinstance(old, bool):
            ratio = 2.0
            kind = "bool flip"
        else:
            lo, hi = sorted((abs(old), abs(new)))
            ratio = float("inf") if lo == 0 else hi / lo
            kind = f"{old:g} -> {new:g}"
            if ratio < 2:
                ctx.drop("small_numeric_change")
                continue
        shipped = bool(_DEFAULTS_FILE.search(p))
        key = e["key"]
        sites = max(consumers.get(key.upper(), 0), consumers.get(key, 0), idents.get(_norm(key.lstrip("-")), 0))
        if sites < 2 and not shipped:
            ctx.drop("fewer_than_two_read_sites")
            continue
        docs = any(is_docs(x) for x in _commit_paths(ctx, e["hash"]))
        score = (2.0 + min(3.0, 0.3 * sites) + (1.5 if shipped else 0)) * (1.0 if docs else 1.5) \
            * (1.0 + min(1.0, (min(ratio, 100.0)) / 100))
        yield Hit(
            subject=f"{e['hash']}|{p}|{key}", score=score,
            title=f"Default of {key} changed ({kind}) in {p}",
            evidence={"key": key, "old": e["old"], "new": e["new"], "read_sites": sites, "shipped_defaults_file": shipped,
                      "docs_touched": docs},
            commits=[e["hash"]], diffs=[(e["hash"], p)], paths=[p])


def _pair_defaults(ctx: Ctx, h: str, p: str, added: list[str], removed: list[str], extract) -> list[dict]:
    c = ctx.by_hash.get(h)
    if c is None:
        return []
    am: dict[str, str] = {}
    dm: dict[str, str] = {}
    for t in added:
        kv = extract(t)
        if kv:
            am[kv[0]] = kv[1]
    for t in removed:
        kv = extract(t)
        if kv:
            dm.setdefault(kv[0], kv[1])
    return [{"hash": h, "seq": c["seq"], "path": p, "key": k, "old": dm[k], "new": am[k]}
            for k in sorted(am.keys() & dm.keys()) if am[k] != dm[k]]


# ---------------------------------------------------------------------------- 5. payload / message size limits
_SIZE_EX = kv_extractor(_SIZE_KEY)
_BODYPARSER = re.compile(r"""(?i)\b(?:json|urlencoded|raw|text)\(\s*\{[^}]*\blimit\s*:\s*["']?([\w.]+)""")
_SIZE_FIXMSG = re.compile(r"(?i)\b413\b|payload too large|request entity too large|truncat|too large|message too large")
_SIZE_FAMILIES = {
    "kafka": re.compile(r"(?i)(message\.max\.bytes|max\.request\.size|fetch\.max\.bytes|max\.partition\.fetch\.bytes|replica\.fetch\.max\.bytes)"),
    "http_body": re.compile(r"(?i)(client_max_body_size|max_?content_?length|data_upload_max|bodyparser_limit|max_?body|limit_request_body)"),
}


def _size_extract(text: str) -> tuple[str, str] | None:
    m = _BODYPARSER.search(text)
    if m:
        return "bodyparser_limit", m.group(1)
    return _SIZE_EX(text)


@candidate("payload_and_message_size_limit_drift")
def payload_and_message_size_limit_drift(ctx: Ctx):
    """(a) A fix commit (413 / payload too large / truncat ...) that changes a size-limit key, or a limit key whose
    effective value (units normalised) changed in >=2 commits; (b) at HEAD, two or more limit keys of one family
    (Kafka message sizes, HTTP body limits across nginx / app config) that disagree. Same-effective-value edits and
    test / docs paths are dropped. Family membership is a name heuristic."""
    events = _kv_events(ctx, r"(?i)" + _SIZE_KEY + r"|\blimit\s*:", _size_extract, limit=40000)
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        reason = _noise_path(e["path"])
        if reason:
            ctx.drop(reason)
            continue
        if e["old"] is None or e["new"] is None:
            continue
        so, sn = _size(e["old"]), _size(e["new"])
        if (so is not None and so == sn) or e["old"] == e["new"]:
            ctx.drop("same_effective_value")
            continue
        e["bytes"] = (so, sn)
        by_key[(e["path"], e["key"])].append(e)
    emitted: set[str] = set()
    for e in events:
        if "bytes" not in e or not _SIZE_FIXMSG.search(subject_of(e["msg"])):
            continue
        emitted.add(e["hash"])
        yield Hit(subject=f"fix|{e['hash']}|{e['path']}|{e['key']}", score=5.0,
                  title=f"Fix commit changes {e['key']} ({e['old']} -> {e['new']}): {subject_of(e['msg'])[:80]}",
                  evidence={"variant": "fix_touches_limit", "key": e["key"], "old": e["old"], "new": e["new"]},
                  commits=[e["hash"]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])
    for (path, key), evs in sorted(by_key.items()):
        if len({e["hash"] for e in evs}) < 2:
            continue
        yield Hit(subject=f"churn|{path}|{key}", score=2.0 + len(evs),
                  title=f"{key} in {path} changed {len(evs)} times: " + " -> ".join(str(e["new"]) for e in evs[-4:]),
                  evidence={"variant": "limit_changed_repeatedly", "key": key,
                            "history": [(e["hash"][:8], e["old"], e["new"]) for e in evs[-6:]]},
                  commits=[e["hash"] for e in evs[-4:]], diffs=[(e["hash"], path) for e in evs[-2:]], paths=[path])
    # (b) cross-layer disagreement at HEAD, only for keys seen in history paths
    paths = sorted({e["path"] for e in events if not _noise_path(e["path"]) and e["path"] in ctx.head_files})
    head: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for p in paths[:80]:
        for ln in (ctx.text_at_head(p) or "").splitlines():
            kv = _size_extract(ln)
            if not kv or _size(kv[1]) is None:
                continue
            for fam, rx in _SIZE_FAMILIES.items():
                if rx.search(kv[0]):
                    head[fam].append((p, kv[0], _size(kv[1])))  # type: ignore[arg-type]
    for fam, items in head.items():
        if len({k for _, k, _ in items}) >= 2 and len({v for _, _, v in items}) >= 2:
            yield Hit(subject=f"head|{fam}", score=2.5,
                      title=f"{fam} size limits disagree at HEAD: " + ", ".join(f"{k}={v:g}" for _, k, v in items[:4]),
                      evidence={"variant": "cross_layer_disagreement", "values": [(p, k, v) for p, k, v in items[:8]]},
                      paths=sorted({p for p, _, _ in items})[:6])


# ---------------------------------------------------------------------------- 6. cookie attributes
_COOKIE_KEYS = (r"(?:SESSION|CSRF|LANGUAGE|REMEMBER)_COOKIE_(?:NAME|DOMAIN|SAMESITE|SECURE|HTTPONLY|PATH|AGE)|"
                r"PERMANENT_SESSION_LIFETIME|SESSION_COOKIE\w*|cookie_?name|cookie_?domain|cookie_?samesite|"
                r"same_?site|secure|http_?only|domain|name|max_?age")
_COOKIE_EX = kv_extractor(_COOKIE_KEYS)
_SETCOOKIE = re.compile(r"""(?i)(?:set_cookie|setcookie|res\.cookie|response\.cookie|SetCookie)\(\s*["']([\w\-]+)["']""")
_AUTHY = re.compile(r"(?i)sess|auth|token|sid|jwt|csrf")
_SESSION_MW = re.compile(r"(?i)cookie-session|express-session|SessionMiddleware|session\s*\(\s*\{|SessionInterface|cookie_?session")
_BARE = {"secure", "http_only", "httponly", "same_site", "samesite", "domain", "name", "max_age", "maxage"}


@candidate("cookie_attribute_change")
def cookie_attribute_change(ctx: Ctx):
    """Value change of an auth-cookie setting: SESSION_/CSRF_COOKIE_{NAME,DOMAIN,SAMESITE,SECURE,...}, session
    middleware options (bare samesite/secure/domain/name accepted only in files that configure a session / cookie), or
    a set_cookie-style call for a cookie named sess|auth|token|sid|jwt|csrf whose line changed. Lifetime keys and
    non-auth cookies are dropped. Pairing is per (commit, path); first additions are not changes."""
    events = _kv_events(ctx, r"(?i)cookie|samesite|same_site|secure|httponly|session|domain|\bname\b|max_?age",
                        _COOKIE_EX, limit=50000)
    for e in events:
        reason = _noise_path(e["path"])
        if reason:
            ctx.drop(reason)
            continue
        if e["old"] is None or e["new"] is None or e["old"] == e["new"]:
            continue
        key = e["key"]
        if re.search(r"(?i)age|lifetime|expire", key):
            ctx.drop("lifetime_tweak")
            continue
        if key in _BARE:
            text = ctx.text_at(e["hash"], e["path"]) or ""
            ok = bool(_SESSION_MW.search(text)) if key in ("name", "domain") else "cookie" in text.lower()
            if not ok:
                ctx.drop("not_session_context")
                continue
        sev = {"samesite": 3, "same_site": 3, "secure": 3, "domain": 2, "name": 2}.get(key.split("_cookie_")[-1], 2)
        if key.endswith(("samesite", "secure")):
            sev = 3
        yield Hit(
            subject=f"{e['hash']}|{e['path']}|{key}", score=2.0 + sev,
            title=f"Cookie setting {key} changed {e['old']} -> {e['new']} in {e['path']}",
            evidence={"key": key, "old": e["old"], "new": e["new"], "subject": subject_of(e["msg"])},
            commits=[e["hash"]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])
    rows = ctx.grep(r"(?i)set_cookie|setcookie|\.cookie\(|SetCookie", None, limit=20000)
    for (h, p), (added, removed) in _group(rows).items():
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        a = {m.group(1): t.strip() for t in added for m in [_SETCOOKIE.search(t)] if m}
        d = {m.group(1): t.strip() for t in removed for m in [_SETCOOKIE.search(t)] if m}
        for name in sorted(a.keys() & d.keys()):
            if a[name] == d[name]:
                continue
            if not _AUTHY.search(name):
                ctx.drop("non_auth_cookie")
                continue
            yield Hit(subject=f"{h}|{p}|set_cookie:{name}", score=3.5,
                      title=f"set_cookie({name!r}) attributes changed in {p}",
                      evidence={"cookie": name, "before": d[name][:200], "after": a[name][:200]},
                      commits=[h], diffs=[(h, p)], paths=[p])


# ---------------------------------------------------------------------------- 7. config_key_rename_orphan
_KEYLINE = re.compile(r"""^\s*["']?([A-Za-z_][\w.\-]*)["']?\s*[:=]""")


@candidate("config_key_rename_orphan")
def config_key_rename_orphan(ctx: Ctx):
    """A structured config key (dotted, or >8 chars; not an UPPER_SNAKE env var) removed from a config file by some
    commit, absent from every HEAD config file, while a quoted literal of exactly that key is still read in production
    code at HEAD in another file. No rename pairing is attempted (the analyst decides whether it was renamed)."""
    quoted = _code_index(ctx)["quoted"]
    head_last = {_norm(last) for ks in _head_config_keys(ctx).values() for _, last in ks}
    last_removed: dict[tuple[str, str], dict] = {}
    for r in ctx.q("SELECT hash, seq, path, text FROM diff_lines WHERE sign='-' AND path REGEXP ? ORDER BY seq",
                   (r"(?i)\.(ya?ml|json|toml|ini|cfg|properties)$|(^|/)\.env[^/]*$",)):
        p = r["path"]
        m = _KEYLINE.match(r["text"])
        if not m:
            continue
        key = m.group(1)
        if _noise_path(p) or _NOT_APP_CFG_RX.search(p) or DEP_MANIFEST_RX.search(p) or LOCKFILE_RX.search(p):
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", key) or not (len(key) > 8 or key.count(".") >= 1):
            continue
        if key.lower() in _GENERIC_KEYS:
            ctx.drop("generic_name")
            continue
        last_removed[(p, key)] = {"hash": r["hash"], "seq": r["seq"]}
    for (p, key), e in sorted(last_removed.items(), key=lambda kv: kv[1]["seq"]):
        if _norm(key.rsplit(".", 1)[-1]) in head_last:
            ctx.drop("key_still_defined")
            continue
        readers = [x for x in quoted.get(key, []) + quoted.get(key.rsplit(".", 1)[-1], []) if x != p]
        if not readers:
            continue
        if _bulk_or_missing(ctx, e["hash"]):
            ctx.drop("bulk_commit")
            continue
        if any(l["sign"] == "+" and re.match(rf"""\s*["']?{re.escape(key)}["']?\s*[:=]""", l["text"])
               for l in ctx.lines_of(e["hash"])):
            ctx.drop("moved_to_other_file")
            continue
        c = ctx.by_hash[e["hash"]]
        yield Hit(
            subject=f"{p}|{key}", score=3.0 + min(3.0, 0.5 * len(set(readers))),
            title=f"Config key {key!r} removed from {p} but still read in {readers[0]}",
            evidence={"key": key, "removed_from": p, "readers_at_head": sorted(set(readers))[:5],
                      "removal_subject": subject_of(c["message"])},
            commits=[e["hash"]], diffs=[(e["hash"], p)], paths=[p, *sorted(set(readers))[:3]])


# ---------------------------------------------------------------------------- 8. sibling config drift
_SIB_EXCL = re.compile(r"(?i)host|url|uri|endpoint|replica|account|region|secret|password|passwd|token|api_?key|"
                       r"access_?key|private_?key|domain|dsn|cluster|namespace|bucket|arn|image|tag|version")
_SIB_KV = re.compile(r"""^\s*["']?([A-Za-z_][\w.\-]*)["']?\s*[:=]\s*(.+?)\s*,?\s*$""")
_PROD_TOKEN = re.compile(r"(?i)(^|[/._-])(prod|production|prd)([/._-]|$)")


def _stem(path: str) -> tuple[str, str] | None:
    m = re.search(rf"(?i)(?<![A-Za-z]){_ENV_TOKEN}(?![A-Za-z])", path)
    if not m:
        return None
    return path[:m.start()] + "{env}" + path[m.end():], m.group(1).lower()


def _kv_map(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in (text or "").splitlines():
        if _COMMENT_RX.match(ln):
            continue
        m = _SIB_KV.match(ln)
        if m and m.group(2) not in ("", "|", ">"):
            out[m.group(1)] = m.group(2).strip("\"'")
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


@candidate("sibling_config_drift")
def sibling_config_drift(ctx: Ctx):
    """Config files that differ only by an env token in the path (config.dev/staging/prod.yaml ...) and look alike
    (line Jaccard >=0.7; >=3 files, or 2 highly similar). Emits (a) a key whose value changed in one member while the
    siblings still hold the old value 90+ days later, and (c) a prod-only edit with no sibling change in the prior 30
    days. Fix/revert within 7 days boosts. host/url/replica/account/region/secret-like keys are excluded."""
    groups: dict[str, dict[str, str]] = defaultdict(dict)
    for p in ctx.head_files:
        if not _STRUCT_CFG_RX.search(p) or _noise_path(p) or LOCKFILE_RX.search(p) or DEP_MANIFEST_RX.search(p):
            continue
        s = _stem(p)
        if s:
            groups[s[0]][p] = s[1]
    for stem, members in sorted(groups.items()):
        if len(members) < 2 or len(set(members.values())) < 2:
            ctx.drop("single_env")
            continue
        lines = {p: {ln.strip() for ln in (ctx.text_at_head(p) or "").splitlines() if ln.strip() and not _COMMENT_RX.match(ln)}
                 for p in members}
        similar = [p for p in members if any(_jaccard(lines[p], lines[q]) >= 0.7 for q in members if q != p)]
        if len(similar) < 2:
            ctx.drop("dissimilar_group")
            continue
        sibs = sorted(similar)
        head_kv = {p: _kv_map(ctx.text_at_head(p)) for p in sibs}
        rows = []
        for i in range(0, len(sibs), 50):
            chunk = sibs[i:i + 50]
            rows += ctx.q(f"SELECT * FROM diff_lines WHERE path IN ({','.join('?' * len(chunk))}) ORDER BY seq, path, line", chunk)
        change_seq: dict[str, list[int]] = defaultdict(list)
        for p in sibs:
            change_seq[p] = [ctx.by_hash[r["hash"]]["seq"] for r in ctx.changes_by_path.get(p, []) if r["hash"] in ctx.by_hash]
        change_ts = {p: [ctx.by_hash[r["hash"]]["ts"] for r in ctx.changes_by_path.get(p, []) if r["hash"] in ctx.by_hash] for p in sibs}
        emitted: set[tuple[str, str]] = set()
        for (h, p), (added, removed) in _group(rows).items():
            c = ctx.by_hash.get(h)
            if c is None:
                continue
            am, dm = _kv_map("\n".join(added)), _kv_map("\n".join(removed))
            keys = [k for k in sorted(am.keys() | dm.keys()) if not _SIB_EXCL.search(k)]
            others = [q for q in sibs if q != p]
            for k in keys:
                old, new = dm.get(k), am.get(k)
                if old is None or new is None or old == new:
                    continue
                if (ctx.head_ts - c["ts"]) < 90 * DAY or head_kv[p].get(k) != new:
                    continue
                holders = [q for q in others if head_kv[q].get(k) == old]
                if not holders:
                    continue
                fix = _fix_within(ctx, c, p, 7)
                emitted.add((h, p))
                yield Hit(
                    subject=f"a|{h}|{p}|{k}", score=3.0 + 0.5 * len(holders) + (2.0 if fix else 0),
                    title=f"{p}: {k} {old} -> {new}; {len(holders)} sibling(s) still on {old}",
                    evidence={"variant": "key_changed_in_one_sibling", "key": k, "old": old, "new": new,
                              "siblings_with_old_value": holders[:5], "group": stem,
                              "fix_or_revert_within_7d": fix["hash"] if fix else None},
                    commits=[h] + ([fix["hash"]] if fix else []), diffs=[(h, p)], paths=[p, *holders[:3]])
            if _PROD_TOKEN.search(p) and (h, p) not in emitted and keys and (am or dm):
                if any(_PROD_TOKEN.search(q) is None for q in others):
                    recent = any(0 <= c["ts"] - t <= 30 * DAY and s2 != c["seq"] for q in others
                                 for s2, t in zip(change_seq[q], change_ts[q]))
                    same_commit = any(c["seq"] in change_seq[q] for q in others)
                    if recent or same_commit:
                        continue
                    changed = [k for k in keys if am.get(k) != dm.get(k)]
                    if not changed:
                        continue
                    fix = _fix_within(ctx, c, p, 7)
                    yield Hit(
                        subject=f"c|{h}|{p}", score=2.0 + (2.0 if fix else 0) + min(2.0, 0.3 * len(changed)),
                        title=f"prod-only config edit in {p}: {', '.join(changed[:4])}",
                        evidence={"variant": "prod_only_edit", "keys": changed[:8], "group": stem,
                                  "siblings": [q for q in others][:5], "fix_or_revert_within_7d": fix["hash"] if fix else None},
                        commits=[h] + ([fix["hash"]] if fix else []), diffs=[(h, p)], paths=[p])


def _commits_after(ctx: Ctx, seq: int):
    """Commits with a higher seq, oldest first (bisect on a cached seq list)."""
    cache = _cache(ctx)
    if "seqs" not in cache:
        cache["seqs"] = [c["seq"] for c in ctx.commits]
    i = bisect.bisect_right(cache["seqs"], seq)
    return ctx.commits[i:]


def _fix_within(ctx: Ctx, c, path: str, days: int):
    for n in _commits_after(ctx, c["seq"]):
        if n["ts"] - c["ts"] > days * DAY:
            break
        if n["is_revert"] or (is_fix_message(n["message"]) and path in _commit_paths(ctx, n["hash"])):
            return n
    return None


# ---------------------------------------------------------------------------- 9. asset cache busting
_ASSET_PATH = re.compile(r"(?i)(webpack|vite|rollup|next\.config|nuxt\.config|vue\.config|angular\.json|craco|parcel|"
                         r"esbuild|snowpack|rspack|gulpfile|(^|/)sw\.js$|service-?worker|workbox|storage)")
_HASH_TOKEN = re.compile(r"\[(?:content|chunk|full)?hash(?::\d+)?\]|\[contenthash\]|\{hash\}|\.\[hash\]")
_ASSET_KEY = kv_extractor(r"publicPath|assetPrefix|STATICFILES_STORAGE|DEFAULT_FILE_STORAGE|STORAGES|assetsDir|assetsPublicPath")
_CACHE_BUMP = re.compile(r"(?i)CACHE_NAME|CACHE_VERSION|cacheName|CACHE_KEY|cache_version")
_ASSET_FIXMSG = re.compile(r"(?i)ChunkLoadError|stale (bundle|chunk|asset|js|css|cache)|cache[- ]?bust|loading chunk|"
                           r"old (bundle|chunk) (served|cached)")
_SINGLE_FILE = re.compile(r"inlineDynamicImports|vite-plugin-singlefile|maxChunks\s*:\s*1|LimitChunkCountPlugin")


@candidate("asset_cache_busting_change")
def asset_cache_busting_change(ctx: Ctx):
    """Bundler / service-worker / storage config where the hash token left a filename pattern, publicPath /
    assetPrefix / storage backend changed, or skipWaiting / clients.claim was added or removed; plus (independent
    trigger) commits whose message names ChunkLoadError / stale bundle / cache bust. Routine CACHE_NAME bumps,
    dev-server config and single-file bundles are dropped."""
    pat = (r"hash\]|\{hash\}|publicPath|assetPrefix|STATICFILES_STORAGE|DEFAULT_FILE_STORAGE|STORAGES|assetsDir|"
           r"skipWaiting|clients\.claim|CACHE_NAME|CACHE_VERSION|cacheName")
    rows = [r for r in ctx.grep(pat, None, limit=30000) if _ASSET_PATH.search(r["path"]) or r["path"].endswith("settings.py")]
    for (h, p), (added, removed) in _group(rows).items():
        c = ctx.by_hash.get(h)
        reason = _noise_path(p)
        if c is None or reason:
            ctx.drop(reason or "missing_commit")
            continue
        if re.search(r"(?i)(dev|development)\b", p) and "prod" not in p.lower():
            ctx.drop("dev_server_config")
            continue
        sig = []
        gone = sum(bool(_HASH_TOKEN.search(t)) for t in removed) - sum(bool(_HASH_TOKEN.search(t)) for t in added)
        if gone > 0:
            sig.append("hash token removed from filename pattern")
        am = {kv[0]: kv[1] for t in added if (kv := _ASSET_KEY(t))}
        dm = {kv[0]: kv[1] for t in removed if (kv := _ASSET_KEY(t))}
        sig += [f"{k}: {dm[k]} -> {am[k]}" for k in sorted(am.keys() & dm.keys()) if am[k] != dm[k]]
        sw_a = {m for t in added for m in re.findall(r"skipWaiting|clients\.claim", t)}
        sw_d = {m for t in removed for m in re.findall(r"skipWaiting|clients\.claim", t)}
        if sw_a != sw_d:
            sig.append(f"service worker takeover toggled ({', '.join(sorted(sw_a ^ sw_d))})")
        if not sig:
            if any(_CACHE_BUMP.search(t) for t in added) and any(_CACHE_BUMP.search(t) for t in removed):
                ctx.drop("routine_cache_bump")
            continue
        text = ctx.text_at(h, p) or ""
        if "devServer" in "\n".join(added + removed):
            ctx.drop("dev_server_config")
            continue
        if _SINGLE_FILE.search(text):
            ctx.drop("single_file_bundle")
            continue
        yield Hit(subject=f"{h}|{p}", score=3.0 + len(sig),
                  title=f"{p}: {sig[0]}", evidence={"changes": sig, "subject": subject_of(c["message"])},
                  commits=[h], diffs=[(h, p)], paths=[p])
    for c in ctx.commits:
        if _ASSET_FIXMSG.search(c["message"] or "") and not c["is_bulk"]:
            paths = [x for x in _commit_paths(ctx, c["hash"]) if not _noise_path(x)]
            yield Hit(subject=f"fix|{c['hash']}", score=3.5,
                      title=f"Stale-asset fix: {subject_of(c['message'])[:100]}",
                      evidence={"variant": "fix_message", "files": paths[:6]}, commits=[c["hash"]],
                      diffs=[(c["hash"], x) for x in paths[:2]], paths=paths[:4])


# ---------------------------------------------------------------------------- 10. base path / prefix
_PREFIX_KEYS = (r"homepage|publicPath|basePath|FORCE_SCRIPT_NAME|SCRIPT_NAME|APPLICATION_ROOT|root_path|assetPrefix|"
                r"urlPrefix|url_prefix|baseUrl|base_url|base")
_PREFIX_EX = kv_extractor(_PREFIX_KEYS)
_PATHISH = re.compile(r"""^(/[\w\-./]*|\.?/?|https?://\S*|)$""")
_PREFIX_FIXMSG = re.compile(r"(?i)base ?path|prefix|sub-?path|sub-?dir|404|static|asset|redirect|script_name|root.?path|homepage|mount")


@candidate("base_path_prefix_change")
def base_path_prefix_change(ctx: Ctx):
    """>=2 distinct prefix settings (homepage, publicPath, basePath, root_path, FORCE_SCRIPT_NAME, ...) whose values
    changed within 10 commits, or a single change followed within 14 days by a fix-style message about paths / assets.
    Values must look like path prefixes; bare ``base`` counts only in build-tool config; dist/build output and
    CDN-only STATIC_URL are out of scope."""
    events = []
    for e in _kv_events(ctx, rf"(?i)\b({_PREFIX_KEYS})\b", _PREFIX_EX, limit=30000):
        reason = _noise_path(e["path"])
        if reason:
            ctx.drop(reason)
            continue
        if e["old"] is None or e["new"] is None or e["old"] == e["new"]:
            continue
        if not (_PATHISH.match(e["old"]) and _PATHISH.match(e["new"])):
            continue
        if e["key"] == "base" and not re.search(r"(?i)vite|vue|nuxt|svelte|astro|\.html$", e["path"]):
            ctx.drop("generic_base_key")
            continue
        if re.search(r"(?i)(^|/)(dist|build|out)/", e["path"]):
            ctx.drop("build_tool_output")
            continue
        events.append(e)
    used: set[int] = set()
    for i, e in enumerate(events):
        grp = [f for f in events[i:] if f["seq"] - e["seq"] <= 10]
        keys = {f["key"] for f in grp}
        if len(keys) >= 2 and i not in used:
            for f in grp:
                used.add(events.index(f))
            shas = list(dict.fromkeys(f["hash"] for f in grp))
            yield Hit(subject="cluster|" + "|".join(sorted(keys)) + "|" + e["hash"], score=3.0 + len(keys),
                      title=f"Prefix settings changed together: " + ", ".join(f"{f['key']} {f['old']!r}->{f['new']!r}" for f in grp[:3]),
                      evidence={"variant": "multiple_prefix_settings", "changes": [(f["key"], f["old"], f["new"], f["path"]) for f in grp[:6]]},
                      commits=shas[:6], diffs=[(f["hash"], f["path"]) for f in grp[:3]],
                      paths=sorted({f["path"] for f in grp})[:5])
    for i, e in enumerate(events):
        if i in used:
            continue
        c = ctx.by_hash[e["hash"]]
        fix = next((n for n in _commits_after(ctx, c["seq"]) if n["ts"] - c["ts"] <= 14 * DAY
                    and _PREFIX_FIXMSG.search(subject_of(n["message"])) and is_fix_message(n["message"])), None)
        if fix:
            yield Hit(subject=f"single|{e['hash']}|{e['key']}", score=3.0,
                      title=f"{e['key']} changed {e['old']!r}->{e['new']!r}, then fix: {subject_of(fix['message'])[:70]}",
                      evidence={"variant": "single_prefix_then_fix", "key": e["key"], "old": e["old"], "new": e["new"]},
                      commits=[e["hash"], fix["hash"]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])


# ---------------------------------------------------------------------------- 11. file mode flips
_MODE_MSG = re.compile(r"(?i)permission denied|chmod|exec format error|not executable|executable bit|make .* executable|\+x\b")
_RUNNER_FILE = re.compile(r"(?i)(^|/)(Dockerfile[^/]*|Containerfile[^/]*|Makefile|justfile|docker-compose[^/]*\.ya?ml)$|"
                          r"\.github/workflows/[^/]+\.ya?ml$|\.gitlab-ci\.ya?ml$|\.sh$")
_MODE_DATA = re.compile(r"(?i)\.(md|rst|txt|json|csv|png|jpe?g|gif|svg|ico|pdf|lock|ya?ml|xml)$|node_modules/\.bin/")


def _mode_flips(ctx: Ctx) -> list[dict]:
    """Real mode changes (both modes present and different) from the enriched ``file_modes`` table."""
    out = []
    for m in ctx.file_modes:
        om, nm = m["old_mode"], m["new_mode"]
        if om and nm and om != nm and m["hash"] in ctx.by_hash:
            out.append({"hash": m["hash"], "path": m["path"], "old": oct(om), "new": oct(nm)})
    return out


@candidate("file_mode_flip_scars")
def file_mode_flip_scars(ctx: Ctx):
    """A git mode change (100644 / 100755 / 120000) on a file that Dockerfile / CI / Makefile / shell scripts at HEAD
    reference, or whose commit message says permission denied / chmod / exec format error, or that flips again within
    60 days. Uses ctx.file_modes (every recorded mode change, including ones that coincide with content edits and ones
    in bulk commits); no heuristic candidate selection or message-word pre-filter. Bare flips are not emitted."""
    flips = _mode_flips(ctx)
    if not flips:
        return
    runner_text = "\n".join((ctx.text_at_head(p) or "") for p in sorted(ctx.head_files) if _RUNNER_FILE.search(p))[:3_000_000]
    by_path: dict[str, list[dict]] = defaultdict(list)
    for f in flips:
        f["seq"] = ctx.by_hash[f["hash"]]["seq"]
        f["ts"] = ctx.by_hash[f["hash"]]["ts"]
        by_path[f["path"]].append(f)
    for p, fs in sorted(by_path.items()):
        reason = _noise_path(p)
        if reason or _MODE_DATA.search(p) and not p.endswith((".sh", ".py")):
            ctx.drop(reason or "data_or_docs_file")
            continue
        base = p.rsplit("/", 1)[-1]
        referenced = bool(p in runner_text or re.search(rf"(?<![\w.-]){re.escape(base)}(?![\w.-])", runner_text))
        fs.sort(key=lambda f: f["seq"])
        for i, f in enumerate(fs):
            msg = ctx.by_hash[f["hash"]]["message"] or ""
            reflip = any(abs(g["ts"] - f["ts"]) <= 60 * DAY and g is not f for g in fs)
            why = []
            if referenced:
                why.append("referenced by Dockerfile/CI/Makefile/script")
            if _MODE_MSG.search(msg):
                why.append("message mentions permissions/exec")
            if reflip:
                why.append("flipped again within 60 days")
            if not why:
                ctx.drop("bare_flip")
                continue
            yield Hit(subject=f"{f['hash']}|{p}", score=2.0 + 1.5 * len(why),
                      title=f"Mode of {p} changed {f['old']} -> {f['new']} ({'; '.join(why)})",
                      evidence={"old_mode": f["old"], "new_mode": f["new"], "reasons": why,
                                "subject": subject_of(msg), "total_flips": len(fs),
                                "bulk_commit": bool(ctx.by_hash[f["hash"]]["is_bulk"])},
                      commits=[f["hash"]], diffs=[(f["hash"], p)], paths=[p])


# ---------------------------------------------------------------------------- 12. flag name recycled
_FLAG_ACCESS = re.compile(r"""(?:getenv|environ(?:\.get)?|process\.env|config(?:\.get)?|is_enabled|feature_flag|"""
                          r"""flag_enabled|isEnabled|getFlag|useFlag|settings\.get|conf\.get)\s*[\(\[\.]\s*["']?([A-Za-z][\w.\-]{5,})""")
_FLAG_GENERIC = re.compile(r"(?i)^(log_?level|debug|environment|secret_key|database_url|node_env|app_env|time_?zone)$")


def _contract_recycles(ctx: Ctx) -> list[Hit]:
    """Key lifecycle from the string contracts rebuilt at release tags (``ctx.contracts_at``): env/config/flag key
    present at a tag, absent at one or more later tags, present again >=7 days later with a different consumer set."""
    refs = [t for t in ctx.tags if ctx.contracts_at(t["name"])]
    if len(refs) < 3:
        return []
    per_ref: list[dict[str, dict[str, set[str]]]] = []
    for t in refs:
        keys: dict[str, dict[str, set[str]]] = {}
        for r in ctx.contracts_at(t["name"]):
            k = r["key"] or ""
            if r["kind"] not in ("env", "config", "flag") or len(k) < 6 or not re.search(r"[_.\-]", k) \
                    or _FLAG_GENERIC.match(k) or is_test(r["path"] or ""):
                continue
            keys.setdefault(k, {"consumer": set(), "producer": set()})[
                "consumer" if r["role"] == "consumer" else "producer"].add(r["path"] or "")
        per_ref.append(keys)
    out: list[Hit] = []
    all_keys = sorted(set().union(*[set(k) for k in per_ref]))
    for key in all_keys:
        present = [key in keys for keys in per_ref]
        for j in range(2, len(refs)):
            if not present[j] or present[j - 1]:
                continue
            i = max((x for x in range(j - 1) if present[x]), default=None)
            if i is None:
                continue
            gap = refs[j]["ts"] - refs[i]["ts"]
            if gap < 7 * DAY:
                continue
            before, after = per_ref[i][key], per_ref[j][key]
            old_c, new_c = before["consumer"] | before["producer"], after["consumer"] | after["producer"]
            if before["consumer"] == after["consumer"] and (old_c & new_c):
                ctx.drop("same_consumers_after_gap")
                continue
            # the commit that brought it back: first added accessor line of the key between the two tags
            add = next((r for r in ctx.grep(re.escape(key), "+", limit=400)
                        if refs[i]["ts"] < r["ts"] <= refs[j]["ts"] and is_code(r["path"]) and not is_test(r["path"])), None)
            commits = [refs[i]["commit_hash"], *([add["hash"]] if add else []), refs[j]["commit_hash"]]
            out.append(Hit(
                subject=f"{key}|{refs[j]['name']}", score=3.0 + min(3.0, gap / (365 * DAY) * 3)
                + (1.5 if not (old_c & new_c) else 0),
                title=f"Flag {key!r} absent in {refs[j - 1]['name']}, back in {refs[j]['name']} with different consumers",
                evidence={"key": key, "source": "contracts_at", "present_at": refs[i]["name"],
                          "absent_at": [refs[x]["name"] for x in range(i + 1, j)], "back_at": refs[j]["name"],
                          "days_between": gap // DAY, "old_consumers": sorted(old_c)[:6],
                          "new_consumers": sorted(new_c)[:6], "disjoint_consumers": not (old_c & new_c)},
                commits=_uniq(commits), diffs=[(add["hash"], add["path"])] if add else [],
                paths=sorted(new_c)[:4], symbols=[add["symbol"]] if add and add["symbol"] else []))
            if len(out) >= 120:
                return out
    return out


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


@candidate("flag_name_recycled")
def flag_name_recycled(ctx: Ctx):
    """A flag / env / config key that lost its last reference and was re-added >=7 days later (>=365 days = dormant
    revival) with different consumers / a different default / parse expression. Primary source: the key lifecycle
    across release tags from ``ctx.contracts_at`` (env/config/flag key present at a tag, absent at the next tag(s),
    present again with a different consumer set; evidence ``source: contracts_at``), used when >=3 tags have contract
    snapshots. Fallback (and for keys the tag snapshots did not flag, e.g. short-lived removals between tags): the
    lifecycle is rebuilt from diff_lines by counting added minus removed accessor lines per key and comparing the text
    after the key literal (quote-normalised). The re-adding line's enclosing symbol (diff_lines.symbol) is attached to
    the hit, with ``same_symbol`` evidence saying whether the removed and re-added lines sat in the same function.
    Keys must be >=6 chars with a separator; tests, generic names and same-expression re-adds are dropped."""
    covered: set[str] = set()
    for h in _contract_recycles(ctx):
        covered.add(h.evidence["key"])
        yield h
    rows = ctx.grep(r"getenv|environ|process\.env|config|is_enabled|feature_flag|flag_enabled|isEnabled|getFlag|useFlag|settings\.get",
                    None, limit=80000)
    per_key: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        p = r["path"]
        if not is_code(p) or is_test(p):
            continue
        for m in _FLAG_ACCESS.finditer(r["text"]):
            key = m.group(1)
            if not re.search(r"[_.\-]", key) or _FLAG_GENERIC.match(key):
                continue
            rest = re.sub(r"""["'`\s]""", "", r["text"][m.end():])
            per_key[key][r["hash"]].append((r["sign"], r["seq"], r["ts"], rest, p, r["symbol"] or ""))
    for key, by_commit in sorted(per_key.items()):
        if key in covered:
            continue
        count = 0
        removed_at = None
        last_removed_rest = ""
        last_removed_sym = ""
        for h, evs in sorted(by_commit.items(), key=lambda kv: kv[1][0][1]):
            net = sum(1 if s == "+" else -1 for s, *_ in evs)
            seq, ts = evs[0][1], evs[0][2]
            adds = [e for e in evs if e[0] == "+"]
            dels = [e for e in evs if e[0] == "-"]
            if count == 0 and removed_at is not None and net > 0:
                gap = ts - removed_at[1]
                if gap >= 7 * DAY:
                    new_rest, path, new_sym = adds[0][3], adds[0][4], adds[0][5]
                    if new_rest == last_removed_rest:
                        ctx.drop("same_expression")
                    elif _bulk_or_missing(ctx, h) or ctx.by_hash[h]["is_revert"]:
                        ctx.drop("revert_or_bulk")
                    else:
                        yield Hit(
                            subject=f"{key}|{h}", score=3.0 + min(3.0, gap / (365 * DAY) * 3),
                            title=f"Flag {key!r} re-added after {gap // DAY} days with a different expression",
                            evidence={"key": key, "days_dormant": gap // DAY, "dormant_year": gap >= 365 * DAY,
                                      "old_expression": last_removed_rest[:80], "new_expression": new_rest[:80],
                                      "same_symbol": bool(new_sym and new_sym == last_removed_sym)},
                            commits=[removed_at[0], h], diffs=[(h, path)], paths=[path],
                            symbols=[new_sym] if new_sym else [])
                removed_at = None
            count = max(0, count + net)
            if count == 0 and dels:
                removed_at = (h, ts)
                last_removed_rest = dels[-1][3]
                last_removed_sym = dels[-1][5]


# ---------------------------------------------------------------------------- 13. isolation / autocommit
_ISO_SQL = re.compile(r"(?i)ISOLATION\s+LEVEL\s+(READ\s+UNCOMMITTED|READ\s+COMMITTED|REPEATABLE\s+READ|SERIALIZABLE)")
_ISO_KV = kv_extractor(r"isolation_?level|ATOMIC_REQUESTS|autocommit|default_transaction_isolation|transaction_isolation")
_LOCK_RX = re.compile(r"(?i)\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b|\bSKIP\s+LOCKED\b|\bFOR\s+SHARE\b|\bNOWAIT\b|select_for_update|"
                      r"with_for_update|with_lockmode")
_LOCK_MSG = re.compile(r"(?i)deadlock|lost update|race condition|\brace\b|concurren|double[- ]?(spend|book|charge)")


def _iso_extract(text: str) -> tuple[str, str] | None:
    if _COMMENT_RX.match(text):
        return None
    m = _ISO_SQL.search(text)
    if m:
        return "isolation level", re.sub(r"\s+", " ", m.group(1).upper())
    kv = _ISO_KV(text)
    return (kv[0], kv[1].lower()) if kv else None


@candidate("isolation_or_autocommit_change")
def isolation_or_autocommit_change(ctx: Ctx):
    """Value change of isolation_level / ISOLATION LEVEL / ATOMIC_REQUESTS / autocommit /
    default_transaction_isolation (initial additions are dropped); lock clauses (FOR UPDATE, SKIP LOCKED, NOWAIT,
    select_for_update ...) only when added/removed in a commit whose message mentions deadlock / lost update / race, or
    in a file that has >=3 lock-clause commits in history."""
    for e in _kv_events(ctx, r"(?i)isolation|atomic_requests|autocommit", _iso_extract, limit=30000):
        reason = _noise_path(e["path"])
        if reason:
            ctx.drop(reason)
            continue
        if e["old"] is None or e["new"] is None:
            ctx.drop("initial_addition_or_removal")
            continue
        if e["old"] == e["new"]:
            continue
        yield Hit(subject=f"{e['hash']}|{e['path']}|{e['key']}", score=4.0,
                  title=f"{e['key']} changed {e['old']} -> {e['new']} in {e['path']}",
                  evidence={"key": e["key"], "old": e["old"], "new": e["new"], "subject": subject_of(e["msg"])},
                  commits=[e["hash"]], diffs=[(e["hash"], e["path"])], paths=[e["path"]])
    groups = _group(ctx.grep(_LOCK_RX.pattern, None, limit=30000))
    per_path: dict[str, list[str]] = defaultdict(list)
    for (h, p), (a, d) in groups.items():
        if any(_LOCK_RX.search(t) for t in a + d):
            per_path[p].append(h)
    for (h, p), (added, removed) in sorted(groups.items(), key=lambda kv: ctx.by_hash[kv[0][0]]["seq"] if kv[0][0] in ctx.by_hash else 0):
        c = ctx.by_hash.get(h)
        reason = _noise_path(p)
        if c is None or reason:
            ctx.drop(reason or "missing_commit")
            continue
        la = sorted(m.group(0).upper() for t in added for m in [_LOCK_RX.search(t)] if m)
        ld = sorted(m.group(0).upper() for t in removed for m in [_LOCK_RX.search(t)] if m)
        if la == ld:
            continue
        msg = _LOCK_MSG.search(c["message"] or "")
        busy = len(per_path[p]) >= 3
        if not (msg or busy):
            ctx.drop("lock_change_without_signal")
            continue
        yield Hit(subject=f"lock|{h}|{p}", score=2.5 + (2.0 if msg else 0) + (1.0 if busy else 0),
                  title=f"Lock clause change in {p}: +{la[:2]} -{ld[:2]}",
                  evidence={"variant": "lock_clause", "added": la[:4], "removed": ld[:4], "message_signal": bool(msg),
                            "lock_commits_in_file": len(per_path[p]), "subject": subject_of(c["message"])},
                  commits=[h], diffs=[(h, p)], paths=[p])


# ---------------------------------------------------------------------------- 14. environment branching in code
_ENV_CMP = re.compile(r"""(?i)^\s*(?:\}?\s*else\s+)?(?:if|elif|unless|when|case)\b[^\n]*\benv(?:ironment)?\w*\b[^\n]*"""
                      r"""["'](prod|production|prd|live)["']|^\s*(?:if|elif)\b[^\n]*["'](prod|production|prd)["'][^\n]*\benv""")
_ENV_PAT = r"(?i)env[^\n]*[\"'](prod|production|prd|live)[\"']|[\"'](prod|production|prd)[\"'][^\n]*env"
_LOGGY = re.compile(r"(?i)\b(log(ger|ging)?|print|console\.|warn(ing)?s?\.)")


def _guarded_block(lines: list[str], i: int) -> list[str]:
    """Body of the block opened at ``lines[i]`` (indent- or brace-delimited)."""
    head = lines[i].rstrip()
    body: list[str] = []
    if head.endswith("{"):
        depth = head.count("{") - head.count("}")
        for ln in lines[i + 1:]:
            depth += ln.count("{") - ln.count("}")
            if depth <= 0:
                break
            body.append(ln)
    elif head.endswith(":"):
        base = len(head) - len(head.lstrip())
        for ln in lines[i + 1:]:
            if ln.strip() and len(ln) - len(ln.lstrip()) <= base:
                break
            body.append(ln)
    return [b for b in body if b.strip()]


@candidate("environment_branching_in_code")
def environment_branching_in_code(ctx: Ctx):
    """A production-code branch comparing an env/environment value to a prod name whose guarded block is >=10 lines and
    has no mapped test coverage on the enclosing symbol or has fix commits on that symbol. "Fix on guarded lines" is read
    from diff_lines.symbol: a fix commit counts as guarded when one of its changed lines inside the enclosing symbol is
    textually one of the block's HEAD lines (``fix_lines_in_guarded_block`` evidence, score boost). Fix events on the
    enclosing symbol remain the fallback for the keep/drop decision (old line numbers do not map to HEAD).
    Logging-heavy blocks, one-line default selection and test helpers are dropped."""
    files = sorted({r["path"] for r in ctx.grep(_ENV_PAT, "+", limit=20000) if is_code(r["path"])})
    for p in files:
        if is_test(p) or is_docs(p) or re.search(r"(?i)(^|/)(testing|fixtures?|conftest|mocks?)", p):
            ctx.drop("test_helper")
            continue
        text = ctx.text_at_head(p)
        if not text:
            continue
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if not _ENV_CMP.search(ln):
                continue
            body = _guarded_block(lines, i)
            if len(body) < 10:
                ctx.drop("short_block_or_default_selection")
                continue
            if sum(bool(_LOGGY.search(b)) for b in body) >= 0.6 * len(body) or re.search(r"(?i)\bdebug\b", ln):
                ctx.drop("logging_or_debug_tooling")
                continue
            sym = ctx.symbol_at_line(p, i + 1)
            weight = ctx.best_test_weight(sym["uid"]) if sym else 0.0
            fixes = ctx.fixes_by_uid.get(sym["uid"], []) if sym else []
            if weight > 0 and not fixes:
                ctx.drop("covered_and_stable")
                continue
            guarded = 0
            if sym and fixes:
                body_norm = {n for n in ({_norm(b) for b in body} | {_norm(ln)}) if len(n) >= 6}
                fix_hashes = {f["hash"] for f in fixes}
                guarded = len({r["hash"] for r in ctx.lines_in_symbol(sym["uid"])
                               if r["hash"] in fix_hashes and _norm(r["text"]) in body_norm})
            yield Hit(
                subject=f"{p}:{ln.strip()[:80]}", score=2.0 + min(3.0, len(body) / 20) + (1.5 if fixes else 0) + (1.5 if weight == 0 else 0)
                + (1.0 if guarded else 0),
                title=f"{p}:{i + 1} prod-only branch of {len(body)} lines ({ln.strip()[:60]})",
                evidence={"condition": ln.strip()[:120], "guarded_lines": len(body), "symbol": sym["qualname"] if sym else None,
                          "best_test_weight": weight, "fix_events_on_symbol": len(fixes), "fix_lines_in_guarded_block": guarded},
                commits=[f["hash"] for f in fixes[:3]], diffs=[(f["hash"], p) for f in fixes[:2]], paths=[p],
                symbols=[sym["uid"]] if sym else [])


# ---------------------------------------------------------------------------- 15. timezone / encoding
_TZ_KV = kv_extractor(r"TIME_ZONE|USE_TZ|TZ|charset|character_set\w*|collat\w+|LANG|LC_ALL|LC_CTYPE|client_encoding|"
                      r"default_charset|DEFAULT CHARSET|PGCLIENTENCODING|PYTHONIOENCODING")
_TZ_SQL = re.compile(r"(?i)CONVERT\s+TO\s+CHARACTER\s+SET|ALTER\s+DATABASE[^\n]*COLLATE|\bCOLLATE\b|\bCHARACTER\s+SET\b|"
                     r"\btimestamptz\b|WITH(OUT)?\s+TIME\s+ZONE|DEFAULT\s+CHARSET|\butf8mb4\b|AT\s+TIME\s+ZONE")
_TZ_FIXMSG = re.compile(r"(?i)\b(time ?zones?|tz|dst|daylight|utc|mojibake|utf-?8|encoding|charset|collat\w+|naive datetime)\b")


@candidate("timezone_encoding_change")
def timezone_encoding_change(ctx: Ctx):
    """Value change of TIME_ZONE / USE_TZ / TZ / charset / collation / LANG / LC_ALL in settings, config, Docker or
    deploy files (pairs per commit and path), or SQL in migrations / .sql files that converts character sets, alters
    collations or moves timestamp columns to timestamptz. Generic application code is never scanned. A later fix whose
    message names timezone / DST / encoding within 180 days boosts the score."""
    def in_scope(p: str) -> bool:
        return bool(is_config(p) or MIGRATION_RX.search(p) or p.endswith(("settings.py", ".sql", "Dockerfile"))
                    or "Dockerfile" in p or "/settings/" in p)

    fix_ts = sorted(c["ts"] for c in ctx.commits if _TZ_FIXMSG.search(subject_of(c["message"])) and is_fix_message(c["message"]))
    fix_by_ts = {c["ts"]: c["hash"] for c in ctx.commits if _TZ_FIXMSG.search(subject_of(c["message"])) and is_fix_message(c["message"])}

    def later_fix(ts: int) -> str | None:
        i = bisect.bisect_right(fix_ts, ts)
        return fix_by_ts[fix_ts[i]] if i < len(fix_ts) and fix_ts[i] - ts <= 180 * DAY else None

    emitted: set[tuple[str, str]] = set()
    for e in _kv_events(ctx, r"(?i)\b(TIME_ZONE|USE_TZ|TZ|charset|character_set\w*|collat\w+|LANG|LC_ALL|LC_CTYPE|client_encoding|PGCLIENTENCODING)\b",
                        _TZ_KV, limit=30000):
        p = e["path"]
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        if not in_scope(p):
            ctx.drop("not_config_scope")
            continue
        if e["old"] is None or e["new"] is None or e["old"] == e["new"]:
            continue
        fx = later_fix(e["ts"])
        emitted.add((e["hash"], p))
        yield Hit(subject=f"{e['hash']}|{p}|{e['key']}", score=3.0 + (2.0 if fx else 0),
                  title=f"{e['key']} changed {e['old']} -> {e['new']} in {p}",
                  evidence={"key": e["key"], "old": e["old"], "new": e["new"], "later_fix": fx},
                  commits=[e["hash"]] + ([fx] if fx else []), diffs=[(e["hash"], p)], paths=[p])
    for (h, p), (added, removed) in _group(ctx.grep(_TZ_SQL.pattern, None, limit=20000)).items():
        c = ctx.by_hash.get(h)
        reason = _noise_path(p)
        if c is None or reason or not (MIGRATION_RX.search(p) or p.endswith((".sql", ".py"))) or (h, p) in emitted:
            if reason:
                ctx.drop(reason)
            continue
        if p.endswith(".py") and not MIGRATION_RX.search(p) and "migration" not in p.lower():
            continue
        stmts = [t.strip()[:120] for t in added if _TZ_SQL.search(t)]
        if not stmts:
            continue
        fx = later_fix(c["ts"])
        yield Hit(subject=f"sql|{h}|{p}", score=3.0 + (2.0 if fx else 0),
                  title=f"Migration changes charset/collation/time zone type in {p}",
                  evidence={"statements": stmts[:4], "later_fix": fx, "subject": subject_of(c["message"])},
                  commits=[h] + ([fx] if fx else []), diffs=[(h, p)], paths=[p])


# ---------------------------------------------------------------------------- 16. proxy_pass trailing slash
_PROXY_LINE = re.compile(r"(?i)\b(proxy_pass|rewrite|StripPrefix|strip-?prefix|ProxyPass|replacePathRegex|stripprefix)\b")


def _strip_slash(t: str) -> str:
    t = t.strip()
    t = re.sub(r"/+(?=\s*[;\"'`,)\]]|\s*$)", "", t)
    return re.sub(r"\s+", " ", t)


@candidate("proxy_pass_uri_trailing_slash")
def proxy_pass_uri_trailing_slash(ctx: Ctx):
    """A proxy_pass / rewrite / StripPrefix / ProxyPass line removed and re-added identical except for a trailing
    slash (which changes how nginx / Apache rewrite the URI), or such a toggle that flips back within 20 commits.
    Changes of host / port only (lines still differ after slash stripping) are dropped."""
    events: list[dict] = []
    for (h, p), (added, removed) in _group(ctx.grep(_PROXY_LINE.pattern, None, limit=20000)).items():
        c = ctx.by_hash.get(h)
        reason = _noise_path(p)
        if c is None or reason:
            ctx.drop(reason or "missing_commit")
            continue
        adds = [t for t in added if _PROXY_LINE.search(t) and not _COMMENT_RX.match(t)]
        dels = [t for t in removed if _PROXY_LINE.search(t) and not _COMMENT_RX.match(t)]
        for d in dels:
            for a in adds:
                if d.strip() == a.strip():
                    continue
                if _strip_slash(d) == _strip_slash(a):
                    events.append({"hash": h, "seq": c["seq"], "path": p, "old": d.strip(), "new": a.strip()})
                elif _PROXY_LINE.search(d) and d.split()[:1] == a.split()[:1]:
                    ctx.drop("host_or_port_only")
    events.sort(key=lambda e: (e["seq"], e["path"]))
    for i, e in enumerate(events):
        back = next((f for f in events[i + 1:] if f["path"] == e["path"] and f["seq"] - e["seq"] <= 20
                     and _strip_slash(f["old"]) == _strip_slash(e["new"]) and f["new"] == e["old"]), None)
        yield Hit(subject=f"{e['hash']}|{e['path']}", score=3.0 + (3.0 if back else 0),
                  title=f"Trailing slash toggled in {e['path']}: {e['old'][:60]} -> {e['new'][:60]}"
                        + (" (toggled back soon after)" if back else ""),
                  evidence={"before": e["old"], "after": e["new"], "reverted_in": back["hash"] if back else None},
                  commits=[e["hash"]] + ([back["hash"]] if back else []),
                  diffs=[(e["hash"], e["path"])] + ([(back["hash"], e["path"])] if back else []), paths=[e["path"]])
