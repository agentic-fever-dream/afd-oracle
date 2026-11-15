"""Catalogue family: observability (log levels, alert / SLO thresholds, metric label cardinality)."""
from __future__ import annotations

import bisect
import re
from collections import defaultdict

from .base import (DAY, HOTFIX_RX, Ctx, Hit, candidate, is_code, is_config, is_docs, is_fix_message, is_generated,
                   is_test, is_vendored, subject_of)

_FIXTURE_RX = re.compile(r"(?i)(^|/)(fixtures?|testdata|test_data|examples?|samples?|snapshots?|__snapshots__|mocks?)/")
_COMMENT_RX = re.compile(r"^\s*(#|//|/\*|\*|--|;)")


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
    g: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for r in rows:
        a, d = g.setdefault((r["hash"], r["path"]), ([], []))
        (a if r["sign"] == "+" else d).append(r["text"])
    return g


def _ordered(ctx: Ctx, groups: dict) -> list:
    return sorted(((k, v) for k, v in groups.items() if k[0] in ctx.by_hash),
                  key=lambda kv: (ctx.by_hash[kv[0][0]]["seq"], kv[0][1]))


# ---------------------------------------------------------------------------- log verbosity
_LEVELS = {"critical": 5, "fatal": 5, "error": 4, "exception": 4, "warn": 3, "warning": 3, "info": 2, "debug": 1,
           "trace": 0}
_LOG_CALL = re.compile(r"""(?i)\b(?:log(?:ger)?|logging|LOG|console|slog|_log|self\.log(?:ger)?|\w*logger)\.
                           (critical|fatal|error|exception|warn(?:ing)?|info|debug|trace)\(\s*
                           (?:f|b|r)?(["'`])((?:(?!\2).){8,}?)\2""", re.X)
_LOG_KEY = re.compile(r"""(?i)^\s*["']?((?:[\w.\-]*log[_.\-]?level[\w.\-]*|logging\.level[\w.\-]*|level))["']?\s*[:=]\s*["']?(\w+)""")
_CLI_PATH = re.compile(r"(?i)(^|/)(cli|cmd|bin|scripts?|tools?|__main__|manage)(/|\.py$|$)|(^|/)(dev|demo|example)s?/")
_PROD_PATH = re.compile(r"(?i)prod|production|deploy|k8s|helm|chart|manifests?|compose|\.env")
_ALERT_PATH = re.compile(r"(?i)(alert|rules?|monitor|slo|sli|prometheus|alertmanager|datadog|nagios|cloudwatch|"
                         r"pagerduty|opsgenie|grafana)")


def _norm_msg(m: str) -> str:
    return re.sub(r"\{[^}]*\}|%[sdrif]|\$\{[^}]*\}", "{}", m).strip().lower()


@candidate("log_verbosity_flip")
def log_verbosity_flip(ctx: Ctx):
    """(a) The same log message literal demoted to a lower level (error/critical/warn -> lower) within one commit and
    file, boosted when an alert / rule file at HEAD mentions the message text; (b) a config log-level key changed >=3
    times, or switched to debug in a prod-looking path. Tests, dev / CLI paths and commits that demote more than 5
    messages at once (logging refactors) are dropped."""
    rows = ctx.grep(r"(?i)(log|logger|logging|console|slog)\.(critical|fatal|error|exception|warn|warning|info|debug|trace)\(",
                    None, limit=60000)
    per_commit: dict[str, list[dict]] = defaultdict(list)
    for (h, p), (added, removed) in _ordered(ctx, _group(rows)):
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        if not is_code(p):
            continue
        if _CLI_PATH.search(p):
            ctx.drop("cli_or_dev_path")
            continue
        old = {}
        for t in removed:
            m = _LOG_CALL.search(t)
            if m:
                old[_norm_msg(m.group(3))] = (m.group(1).lower(), m.group(3))
        for t in added:
            m = _LOG_CALL.search(t)
            if not m:
                continue
            key = _norm_msg(m.group(3))
            if key in old:
                lo, new = old[key][0], m.group(1).lower()
                if _LEVELS[new] < _LEVELS[lo] and _LEVELS[lo] >= 3:
                    per_commit[h].append({"path": p, "msg": m.group(3), "old": lo, "new": new})
    alert_text = ""
    alert_files = [p for p in ctx.head_files if _ALERT_PATH.search(p) and is_config(p)]
    if alert_files:
        alert_text = "\n".join((ctx.text_at_head(p) or "") for p in alert_files[:60]).lower()
    for h, items in per_commit.items():
        c = ctx.by_hash[h]
        if c["is_bulk"] or len(items) > 5:
            ctx.drop("bulk_logging_refactor", len(items))
            continue
        for it in items:
            alerted = bool(alert_text) and it["msg"].lower()[:40] in alert_text
            yield Hit(
                subject=f"{h}|{it['path']}|{_norm_msg(it['msg'])[:60]}",
                score=3.0 + (1.5 if it["old"] in ("error", "critical", "exception", "fatal") else 0.5) + (2.0 if alerted else 0),
                title=f"Log {it['old']} -> {it['new']}: {it['msg'][:70]!r} in {it['path']}",
                evidence={"variant": "message_demoted", "message": it["msg"][:120], "old_level": it["old"],
                          "new_level": it["new"], "alert_rule_mentions_message": alerted,
                          "subject": subject_of(c["message"])},
                commits=[h], diffs=[(h, it["path"])], paths=[it["path"]])
    # (b) config key churn
    hist: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for (h, p), (added, removed) in _ordered(ctx, _group(ctx.grep(r"(?i)log[_.\-]?level|logging\.level|^\s*[\"']?level[\"']?\s*[:=]", None,
                                                                   limit=30000))):
        reason = _noise_path(p)
        if reason or not (is_config(p) or p.endswith((".env", ".properties", ".conf"))):
            ctx.drop(reason or "not_config_file")
            continue
        if re.search(r"(?i)(^|[/._-])(dev|development|local|test|e2e)([/._-]|$)", p):
            ctx.drop("dev_config")
            continue
        am = {m.group(1).lower(): m.group(2).lower() for t in added for m in [_LOG_KEY.match(t)] if m}
        dm = {m.group(1).lower(): m.group(2).lower() for t in removed for m in [_LOG_KEY.match(t)] if m}
        if "level" in am and not re.search(r"(?i)log", p) and not any("log" in k for k in am):
            am.pop("level", None)
            dm.pop("level", None)
        for k in am.keys() & dm.keys():
            if am[k] != dm[k]:
                hist[(p, k)].append({"hash": h, "old": dm[k], "new": am[k]})
    for (p, k), evs in sorted(hist.items()):
        to_debug = [e for e in evs if e["new"] in ("debug", "trace", "all") and _PROD_PATH.search(p)]
        if len(evs) < 3 and not to_debug:
            continue
        shown = evs[-4:]
        yield Hit(
            subject=f"cfg|{p}|{k}", score=2.0 + len(evs) + (3.0 if to_debug else 0),
            title=f"Log level key {k} in {p} changed {len(evs)}x ({' -> '.join(e['new'] for e in shown)})",
            evidence={"variant": "config_level_changes", "key": k, "changes": [(e["hash"][:8], e["old"], e["new"]) for e in shown],
                      "debug_in_prod_path": bool(to_debug)},
            commits=[e["hash"] for e in shown], diffs=[(e["hash"], p) for e in shown[-2:]], paths=[p])


# ---------------------------------------------------------------------------- alerts / SLOs
_ALERT_FILE = re.compile(r"(?i)\.(ya?ml|json|tf|tfvars|jsonnet|libsonnet|rules|hcl)$")
_ALERT_GREP = (r"(?i)\balert\b|\bfor\s*:|threshold|evaluation_?periods|datapoints_to_alarm|severity|priority|objective|"
               r"\btarget\s*[:=]|silence|mute|\bexpr\b|comparison_?operator|enabled|disabled")
_CMP = re.compile(r"(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)")
_DUR = re.compile(r"^(\d+)(ms|s|m|h|d|w)?$")
_SEV = {"critical": 4, "page": 4, "p1": 4, "sev1": 4, "high": 3, "error": 3, "p2": 3, "warning": 2, "warn": 2, "medium": 2,
        "ticket": 1, "info": 1, "low": 1, "p3": 1, "none": 0}


def _dur(v: str) -> float | None:
    m = _DUR.match(v.strip().strip("\"'"))
    if not m:
        return None
    return int(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, None: 1}[m.group(2)]


def _alert_changes(added: list[str], removed: list[str], file_text: str) -> list[str]:
    out: list[str] = []
    lessthan = bool(re.search(r"(?i)LessThan|less_than|comparison_operator\s*[:=]\s*[\"']?<", file_text))
    # expression comparators
    ca = [(m.group(1), float(m.group(2))) for t in added if re.search(r"\bexpr\b|\bquery\b|\bif\b|\bcondition\b", t, re.I) for m in _CMP.finditer(t)]
    cd = [(m.group(1), float(m.group(2))) for t in removed if re.search(r"\bexpr\b|\bquery\b|\bif\b|\bcondition\b", t, re.I) for m in _CMP.finditer(t)]
    for (oa, va), (od, vd) in zip(ca, cd):
        if oa == od and va != vd:
            if (oa.startswith(">") and va > vd) or (oa.startswith("<") and va < vd):
                out.append(f"threshold {od} {vd:g} -> {va:g} (looser)")
    kv = lambda lines, rx: [float(m.group(1)) for t in lines for m in [re.match(rx, t.strip().strip(",-").strip())] if m]  # noqa: E731
    ta, td = kv(added, r"(?i)[\"']?threshold[\"']?\s*[:=]\s*(-?\d+(?:\.\d+)?)"), kv(removed, r"(?i)[\"']?threshold[\"']?\s*[:=]\s*(-?\d+(?:\.\d+)?)")
    for va, vd in zip(ta, td):
        if va != vd and ((va < vd) if lessthan else (va > vd)):
            out.append(f"threshold {vd:g} -> {va:g} (looser)")
    oa_, od_ = kv(added, r"(?i)[\"']?(?:objective|target|slo)[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)"), \
        kv(removed, r"(?i)[\"']?(?:objective|target|slo)[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)")
    for va, vd in zip(oa_, od_):
        if va < vd:
            out.append(f"SLO objective {vd:g} -> {va:g} (lower target)")
    for key in (r"for", r"evaluation_?periods", r"datapoints_to_alarm"):
        pa = [m.group(1) for t in added for m in [re.match(rf"""(?i)[\s\-]*["']?{key}["']?\s*[:=]\s*["']?([\w.]+)""", t)] if m]
        pd = [m.group(1) for t in removed for m in [re.match(rf"""(?i)[\s\-]*["']?{key}["']?\s*[:=]\s*["']?([\w.]+)""", t)] if m]
        for a, d in zip(pa, pd):
            da, dd = (_dur(a), _dur(d)) if key == "for" else (float(a) if a.replace(".", "").isdigit() else None,
                                                             float(d) if d.replace(".", "").isdigit() else None)
            if da is not None and dd is not None and da > dd:
                out.append(f"{key} {d} -> {a} (slower to fire)")
    sa = [m.group(1).lower() for t in added for m in [re.search(r"(?i)\b(?:severity|priority)[\"']?\s*[:=]\s*[\"']?(\w+)", t)] if m]
    sd = [m.group(1).lower() for t in removed for m in [re.search(r"(?i)\b(?:severity|priority)[\"']?\s*[:=]\s*[\"']?(\w+)", t)] if m]
    for a, d in zip(sa, sd):
        if a in _SEV and d in _SEV and _SEV[a] < _SEV[d]:
            out.append(f"severity {d} -> {a}")
    names_a = {m.group(1) for t in added for m in [re.search(r"\balert\s*:\s*[\"']?([\w.\- ]+)", t)] if m}
    names_d = {m.group(1) for t in removed for m in [re.search(r"\balert\s*:\s*[\"']?([\w.\- ]+)", t)] if m}
    gone = names_d - names_a
    if gone and not names_a:
        out.append(f"alert removed ({', '.join(sorted(gone)[:2])})")
    if any(re.search(r"(?i)\b(silence|mute|inhibit)\w*\b|enabled\s*[:=]\s*false|disabled\s*[:=]\s*true", t) for t in added) and \
            not any(re.search(r"(?i)\b(silence|mute|inhibit)\w*\b|enabled\s*[:=]\s*false|disabled\s*[:=]\s*true", t) for t in removed):
        out.append("alert silenced / disabled")
    return out


@candidate("alert_or_slo_loosened")
def alert_or_slo_loosened(ctx: Ctx):
    """Alert / rule / monitor / SLO files where a threshold (comparator-aware: > raised or < lowered), ``for:`` duration,
    evaluation_periods, SLO objective or severity moved in the loosening direction, or an alert was removed / silenced.
    Boost: an incident-style commit (hotfix / outage / rollback) within 90 days after whose message shares a word of
    the alert / file name (component join). Label, dashboard and rename-only edits produce no loosening and are dropped."""
    rows = [r for r in ctx.grep(_ALERT_GREP, None, limit=60000)
            if _ALERT_PATH.search(r["path"]) and _ALERT_FILE.search(r["path"])]
    incidents = [c for c in ctx.commits if HOTFIX_RX.search(c["message"] or "")]
    inc_ts = [c["ts"] for c in incidents]
    for (h, p), (added, removed) in _ordered(ctx, _group(rows)):
        c = ctx.by_hash[h]
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        if re.search(r"(?i)dashboards?", p):
            ctx.drop("dashboard_file")
            continue
        text = ctx.text_at(h, p) or ""
        changes = _alert_changes(added, removed, text)
        if not changes:
            ctx.drop("label_or_rename_only")
            continue
        names = re.findall(r"(?i)alert\s*:\s*[\"']?([A-Za-z]\w*)", text + "\n" + "\n".join(added + removed))
        camel = [w for n in names for w in re.findall(r"[A-Z][a-z]{3,}|[a-z]{4,}", n)]
        words = {w.lower() for w in camel} | {n.lower() for n in names} | set(re.findall(r"[a-z]{4,}", p.lower()))
        lo = bisect.bisect_left(inc_ts, c["ts"])
        hi = bisect.bisect_right(inc_ts, c["ts"] + 90 * DAY)
        joined = [i for i in incidents[lo:hi]
                  if i["hash"] != h and words & set(re.findall(r"[a-z]{4,}", (i["message"] or "").lower())) - {"alert", "alerts", "rule", "rules", "fix", "this", "that", "with", "from"}]
        yield Hit(
            subject=f"{h}|{p}", score=3.0 + len(changes) + (3.0 if joined else 0),
            title=f"{p}: {changes[0]}" + (f" (+{len(changes) - 1} more)" if len(changes) > 1 else ""),
            evidence={"changes": changes[:6], "incident_commits_within_90d": [subject_of(i["message"])[:80] for i in joined[:3]],
                      "subject": subject_of(c["message"])},
            commits=[h] + [i["hash"] for i in joined[:2]], diffs=[(h, p)], paths=[p])


# ---------------------------------------------------------------------------- metric label cardinality
_METRIC_CTX = re.compile(r"(?i)\b(Counter|Gauge|Histogram|Summary|labelnames|label_names|labelNames|labels\(|labels\s*[:=]|"
                         r"WithLabelValues|CounterVec|GaugeVec|HistogramVec|SummaryVec|metric|statsd|tags\s*[:=]|Prometheus|prom)")
_HIGH_CARD = re.compile(r"(?i)^[\w]*(user|account|customer|tenant|session|request|trace|email|ip|uri|url)[\w]*$")
_ENUMISH = re.compile(r"(?i)(type|role|tier|kind|status|state|plan|class|group|category|agent_family|count|enabled|name_template)$|^(user_?agent)$")
_CARD_FIXMSG = re.compile(r"(?i)cardinality|too many (time ?)?series|labeldrop|label explosion|series explosion")


@candidate("metric_label_cardinality")
def metric_label_cardinality(ctx: Ctx):
    """Newly added metric label names that look unbounded (user, account, customer, tenant, session, request, trace,
    email, ip, uri, url; bare id/path are not matched) on lines that define or use a metric; plus commits whose message
    says cardinality / too many series / labeldrop. Enum-like suffixes (user_type, tenant_tier ...), tests, and labels
    already present on a removed line of the same file are dropped."""
    rows = ctx.grep(r"""(?i)label|Counter|Gauge|Histogram|Summary|tags""", None, limit=60000)
    for (h, p), (added, removed) in _ordered(ctx, _group(rows)):
        reason = _noise_path(p)
        if reason:
            ctx.drop(reason)
            continue
        if not (is_code(p) or p.endswith((".yml", ".yaml", ".libsonnet", ".jsonnet", ".json"))):
            continue
        before = {s for t in removed if _METRIC_CTX.search(t) for s in re.findall(r"""["'`]([A-Za-z_][\w.]*)["'`]""", t)}
        names_first = set()
        for t in added:
            m0 = re.search(r"""(?i)(?:Counter|Gauge|Histogram|Summary)(?:Vec)?(?:\.new|\.build)?\(\s*(?:name\s*[:=]\s*)?["'`]([\w.]+)["'`]""", t)
            if m0:
                names_first.add(m0.group(1))
        new = []
        for t in added:
            if not _METRIC_CTX.search(t) or _COMMENT_RX.match(t):
                continue
            for s in re.findall(r"""["'`]([A-Za-z_][\w.]*)["'`]""", t):
                if s in before or s in new or s in names_first or not re.fullmatch(r"[a-z][a-z0-9_]*", s)                         or re.search(r"_(total|seconds|bytes|count|sum|bucket|info|duration|latency)$", s) or not _HIGH_CARD.match(s):
                    continue
                if _ENUMISH.search(s):
                    ctx.drop("enum_like_label")
                    continue
                new.append(s)
        if not new:
            continue
        c = ctx.by_hash[h]
        yield Hit(
            subject=f"{h}|{p}|{','.join(sorted(new))}", score=3.0 + len(new) + (1.0 if c["message"] and _CARD_FIXMSG.search(c["message"]) else 0),
            title=f"High-cardinality metric label(s) {', '.join(new[:3])} added in {p}",
            evidence={"labels": new[:6], "subject": subject_of(c["message"])},
            commits=[h], diffs=[(h, p)], paths=[p])
    for c in ctx.commits:
        if _CARD_FIXMSG.search(c["message"] or "") and not c["is_bulk"]:
            files = [r["path"] for r in ctx.changes(c["hash"]) if not _noise_path(r["path"])]
            yield Hit(subject=f"fix|{c['hash']}", score=4.0,
                      title=f"Cardinality fix: {subject_of(c['message'])[:100]}",
                      evidence={"variant": "fix_message", "files": files[:6]}, commits=[c["hash"]],
                      diffs=[(c["hash"], f) for f in files[:2]], paths=files[:4])
