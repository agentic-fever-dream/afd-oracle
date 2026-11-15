"""Catalogue family: infrastructure impact (proxy trust, CI gaps, Terraform, k8s, containers, ingress, retention).

Everything here is text-first: manifests, Terraform, Dockerfiles and CI files are plain text in history, so builders
search the ``diff_lines`` index and only parse (YAML) a capped number of before/after versions for the commits that
touch such files. Where a spec asks for data Oracle lacks (e.g. Terraform plans, real controller semantics) the
builder's docstring names the proxy it uses.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .base import (CI_RX, DAY, IAC_RX, K8S_RX, Ctx, Hit, candidate, flatten, is_docs, is_fix_message, is_generated,
                   is_test, is_vendored, subject_of)

DEV_RX = re.compile(r"(?i)(^|/)(dev|develop(ment)?|test|tests|testing|local|sandbox(es)?|examples?|samples?|demos?|"
                    r"docs?|fixtures?|staging|stage|qa|playground)(/|$)|[-_.](dev|local|test|staging|stage|sandbox|"
                    r"example|override|demo)[-_.]|\.(example|sample|dist)$|docker-compose\.override|"
                    r"(^|/)examples?[-_]")
DOCKERFILE_PATH = r"(?i)(^|/)(Dockerfile|Containerfile)[^/]*$"
COMPOSE_PATH = r"(?i)(^|/)(docker-)?compose[^/]*\.ya?ml$"
COMMENT_PFX = ("#", "//", "*", "/*", ";", "--", "<!--")
ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".map", ".pdf")


def _pr(*pats: str) -> str:
    """Combine path regexes (inline (?i) flags stripped; whole pattern is case-insensitive)."""
    return "(?i)(?:" + "|".join(p.replace("(?i)", "") for p in pats) + ")"


def _dev(path: str) -> bool:
    return bool(DEV_RX.search(path))


def _comment(text: str) -> bool:
    return text.strip().startswith(COMMENT_PFX)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _group(rows) -> dict[tuple[str, str], dict[str, Any]]:
    """diff_lines rows -> {(hash, path): {'+': [...], '-': [...], 'seq': n, 'ts': n}} (comment lines removed)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if _comment(r["text"]):
            continue
        g = out.setdefault((r["hash"], r["path"]), {"+": [], "-": [], "seq": r["seq"], "ts": r["ts"]})
        g[r["sign"]].append(r["text"])
    return out


def _later(ctx: Ctx, path: str, seq: int, rx: str | None = None, fix: bool = False, within: int | None = None):
    """Commits touching ``path`` after ``seq`` (optionally message-filtered / within N seq numbers)."""
    pat = re.compile(rx) if rx else None
    out = []
    for c in ctx.path_commits(path):
        if c["seq"] <= seq or (within is not None and c["seq"] > seq + within):
            continue
        msg = c["message"] or ""
        if pat and not pat.search(msg):
            continue
        if fix and not is_fix_message(msg):
            continue
        out.append(c)
    return out


def _docs(path: str, text: str | None) -> list[dict]:
    """Parse YAML text into mapping documents; [] when templated / unparsable."""
    if not text or "{{" in text:
        return []
    try:
        import yaml
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except Exception:
        return []


_QTY = re.compile(r"^\s*([\d.]+)\s*(Ki|Mi|Gi|Ti|k|K|M|G|T|m)?[iB]*\s*$")
_MUL = {None: 1.0, "Ki": 1024.0, "Mi": 1024.0 ** 2, "Gi": 1024.0 ** 3, "Ti": 1024.0 ** 4, "k": 1e3, "K": 1e3,
        "M": 1e6, "G": 1e9, "T": 1e12, "m": 1e-3}


def _qty(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        m = _QTY.match(v)
        if m:
            try:
                return float(m.group(1)) * _MUL[m.group(2)]
            except (ValueError, KeyError):
                return None
    return None


# ---------------------------------------------------------------------------------------------------------------
@candidate("trusted_proxy_trust_setting_change")
def trusted_proxy_trust_setting_change(ctx: Ctx):
    """Changed lines naming a proxy-trust setting whose value token differs between removed and added line.
    Ranked by later fix commits touching the same file with proxy/loop/ip/https wording."""
    rx = (r"(?i)trust[_ ]?proxy|FORWARDED_ALLOW_IPS|ProxyFix|SECURE_PROXY_SSL_HEADER|set_real_ip_from|KnownProxies|"
          r"TRUSTED_PROXIES|real_ip_recursive|forwarded[-_]allow[-_]ips|proxy_headers|num_proxies")
    tok = re.compile(r"(?i)\btrue\b|\bfalse\b|\*|\d+\.\d+\.\d+\.\d+(?:/\d+)?|[0-9a-f:]+/\d+|\b\d+\b|'[^']*'|\"[^\"]*\"")
    for (sha, path), g in _group(ctx.grep(rx, None)).items():
        if is_test(path) or is_docs(path):
            ctx.drop("tests_or_docs")
            continue
        if _dev(path):
            ctx.drop("dev_settings")
            continue
        if not g["+"] or not g["-"]:
            ctx.drop("no_value_change")
            continue
        old = sorted(t.lower() for ln in g["-"] for t in tok.findall(ln))
        new = sorted(t.lower() for ln in g["+"] for t in tok.findall(ln))
        if old == new:
            ctx.drop("no_value_change")
            continue
        c = ctx.commit(sha)
        later_fix = _later(ctx, path, c["seq"], r"(?i)proxy|loop|redirect|\bip\b|https|forwarded|scheme|client.?addr",
                           fix=True)
        later_any = _later(ctx, path, c["seq"], rx)
        key = re.search(rx, g["+"][0]).group(0).lower()
        yield Hit(subject=f"{sha}|{path}|{key}", score=1 + 3 * len(later_fix) + 0.5 * len(later_any),
                  title=f"Proxy trust setting '{key}' changed in {path}: {old[:2]} -> {new[:2]}",
                  evidence={"removed": [_norm(x) for x in g["-"][:4]], "added": [_norm(x) for x in g["+"][:4]],
                            "later_fixes": [subject_of(x["message"]) for x in later_fix[:4]],
                            "later_changes": len(later_any)},
                  commits=[sha] + [x["hash"] for x in later_fix[:3]], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
def _gh_glob(pat: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile("^" + out + "$")


def _matches(globs: list[str], path: str) -> bool:
    return any(_gh_glob(g).match(path) or _gh_glob(g.rstrip("/") + "/**").match(path)
               for g in globs if not g.startswith("!"))


@candidate("ci_path_filter_new_dir_gap")
def ci_path_filter_new_dir_gap(ctx: Ctx):
    """Directories whose changed files match no ``paths``/``paths-ignore`` filter of any test-like push/PR workflow.
    Proxy: only GitHub workflows at HEAD are parsed (not their history, not GitLab ``rules:changes``)."""
    test_rx = re.compile(r"(?i)test|pytest|jest|tox|nox|cargo test|go test|mvn|gradle|rspec|vitest|unittest|ctest")
    wfs: list[dict] = []
    for p in ctx.match(r"^\.github/workflows/[^/]+\.ya?ml$"):
        docs = _docs(p, ctx.text_at_head(p))
        if not docs:
            continue
        d = docs[0]
        on = d.get("on", d.get(True))
        trig: dict[str, Any] = {}
        if isinstance(on, str):
            trig = {on: {}}
        elif isinstance(on, list):
            trig = {k: {} for k in on}
        elif isinstance(on, dict):
            trig = {k: (v or {}) for k, v in on.items()}
        trig = {k: v for k, v in trig.items() if k in ("push", "pull_request", "pull_request_target")}
        if not trig:
            continue
        blob = p + " " + str(d.get("name", "")) + " " + " ".join(
            str(s.get("run", "")) + str(s.get("name", "")) for j in (d.get("jobs") or {}).values()
            if isinstance(j, dict) for s in (j.get("steps") or []) if isinstance(s, dict))
        if not test_rx.search(blob):
            continue
        paths, ignore, filt = [], [], False
        for v in trig.values():
            if isinstance(v, dict) and (v.get("paths") or v.get("paths-ignore")):
                filt = True
                paths += list(v.get("paths") or [])
                ignore += list(v.get("paths-ignore") or [])
        wfs.append({"path": p, "paths": paths, "ignore": ignore, "filtered": filt})
    if not wfs:
        return
    if any(not w["filtered"] for w in wfs):
        ctx.drop("unfiltered_test_workflow")
        return

    def covered(f: str) -> bool:
        for w in wfs:
            if w["paths"] and _matches(w["paths"], f) and not (w["ignore"] and _matches(w["ignore"], f)):
                return True
            if not w["paths"] and w["ignore"] and not _matches(w["ignore"], f):
                return True
        return False

    gaps: dict[str, dict[str, Any]] = {}
    for c in ctx.main_commits:
        if c["is_merge"] or ctx.is_bot(c):
            continue
        for fc in ctx.changes(c["hash"]):
            f = fc["path"]
            if fc["status"] == "D":
                continue
            if is_docs(f) or is_generated(f) or is_vendored(f) or f.lower().endswith(ASSET_EXT) or f.startswith("."):
                ctx.drop("docs_assets_generated")
                continue
            if covered(f):
                continue
            parts = f.split("/")
            d = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")
            g = gaps.setdefault(d, {"commits": [], "files": set(), "added_in": defaultdict(int)})
            if c["hash"] not in g["commits"]:
                g["commits"].append(c["hash"])
            g["files"].add(f)
            if fc["status"] == "A":
                g["added_in"][c["hash"]] += 1
    for d, g in gaps.items():
        most_added = max(g["added_in"].values(), default=0)
        if len(g["commits"]) < 3 and most_added < 3:
            ctx.drop("too_few_commits")
            continue
        first = ctx.commit(g["commits"][0])
        edits = [x for w in wfs for x in _later(ctx, w["path"], first["seq"])]
        yield Hit(subject=f"gap|{d}", score=len(g["commits"]) + len(g["files"]) / 3 + (2 if edits else 0),
                  title=f"No test workflow runs for changes under {d} ({len(g['commits'])} commits)",
                  evidence={"directory": d, "uncovered_files": sorted(g["files"])[:8],
                            "workflows": [{"path": w["path"], "paths": w["paths"][:8], "paths_ignore": w["ignore"][:8]}
                                          for w in wfs],
                            "later_filter_edits": [x["hash"][:10] for x in edits[:4]]},
                  commits=g["commits"][:6] + [x["hash"] for x in edits[:2]],
                  paths=[w["path"] for w in wfs] + sorted(g["files"])[:4])


# ---------------------------------------------------------------------------------------------------------------
_STATEFUL = (r"aws_(?:db_instance|rds_cluster|rds_cluster_instance|s3_bucket|dynamodb_table|elasticache_\w+|efs_file_system|"
             r"ebs_volume|kms_key|rds_global_cluster|redshift_cluster|elasticsearch_domain|opensearch_domain|"
             r"docdb_cluster|neptune_cluster|mq_broker|msk_cluster|ecr_repository)|"
             r"google_(?:sql_database_instance|storage_bucket|bigtable_instance|spanner_instance|filestore_instance|"
             r"compute_disk|redis_instance)|azurerm_(?:mssql_\w+|postgresql_\w+|mysql_\w+|storage_account|cosmosdb_account|"
             r"managed_disk|redis_cache|key_vault)|kubernetes_persistent_volume\w*")
_HDR = re.compile(r'^\s*resource\s+"(%s)"\s+"([^"]+)"' % _STATEFUL)


@candidate("terraform_replace_forcing_change")
def terraform_replace_forcing_change(ctx: Ctx):
    """Stateful Terraform resource headers removed+added (same type, new name = destroy/create) or a replace-forcing
    attribute (identifier/bucket/engine/...) edited inside a stateful block. Proxy for a terraform plan.
    Suppressed by moved/import blocks or 'state mv|import' in the message."""
    rows = ctx.grep(r'^\s*resource\s+"[^"]+"\s+"[^"]+"', None, path_regex=r"\.tf$")
    keyrows = ctx.grep(r"^\s*(identifier|cluster_identifier|bucket|engine|snapshot_identifier)\s*=", None,
                       path_regex=r"\.tf$")
    force = re.compile(r"^\s*(identifier|cluster_identifier|bucket|engine|snapshot_identifier)\s*=\s*(.+)$")
    moved = {r["hash"] for r in ctx.grep(r"^\s*(moved|import)\s*\{", "+", path_regex=r"\.tf$")}
    per: dict[str, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in rows:
        m = _HDR.match(r["text"])
        if m:
            per[r["hash"]][r["sign"]].append((m.group(1), m.group(2), r["path"]))
    for sha, g in per.items():
        c = ctx.commit(sha)
        msg = c["message"] or ""
        for typ, name, path in g["-"]:
            adds = [a for a in g["+"] if a[0] == typ and a[1] != name]
            if not adds:
                continue
            if _dev(path):
                ctx.drop("dev_or_ephemeral")
                continue
            if sha in moved or re.search(r"(?i)state mv|terraform import|moved block|\bmoved\b", msg):
                ctx.drop("moved_or_import")
                continue
            yield Hit(subject=f"{sha}|{typ}.{name}", score=5 + 0.2 * len(ctx.path_commits(path)),
                      title=f"Stateful {typ} '{name}' replaced by '{adds[0][1]}' with no moved block",
                      evidence={"type": typ, "removed": name, "added": adds[0][1], "message": subject_of(msg)},
                      commits=[sha], diffs=[(sha, path)], paths=[path])
    for (sha, path), g in _group(keyrows).items():
        if not g["+"] or not g["-"]:
            continue
        o = {m.group(1): m.group(2).strip() for ln in g["-"] if (m := force.match(ln))}
        n = {m.group(1): m.group(2).strip() for ln in g["+"] if (m := force.match(ln))}
        changed = {k: (o[k], n[k]) for k in o if k in n and o[k] != n[k]}
        if not changed:
            continue
        if _dev(path):
            ctx.drop("dev_or_ephemeral")
            continue
        text = ctx.text_at(sha, path) or ""
        if not re.search(r'resource\s+"(%s)"' % _STATEFUL, text):
            ctx.drop("stateless_type")
            continue
        if sha in moved:
            ctx.drop("moved_or_import")
            continue
        yield Hit(subject=f"{sha}|{path}|attr", score=4,
                  title=f"Replace-forcing attribute edited in stateful block of {path}: {', '.join(changed)}",
                  evidence={"changed": {k: list(v) for k, v in changed.items()}},
                  commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_GUARDS = [  # (key regex, protective value regex, unprotective value regex)
    (r"deletion_protection(?:_enabled)?", r"true", r"false"),
    (r"prevent_destroy", r"true", r"false"),
    (r"skip_final_snapshot", r"false", r"true"),
    (r"force_destroy", r"false", r"true"),
    (r"publicly_accessible|associate_public_ip_address|map_public_ip_on_launch", r"false", r"true"),
    (r"storage_encrypted|encrypted|enable_key_rotation|kms_encrypted", r"true", r"false"),
    (r"enable_versioning|versioning_enabled|enable_log_file_validation|multi_az", r"true", r"false"),
    (r"block_public_acls|block_public_policy|ignore_public_acls|restrict_public_buckets", r"true", r"false"),
]


@candidate("iac_guardrail_removal")
def iac_guardrail_removal(ctx: Ctx):
    """Terraform/IaC guard flips from protective to unprotective (deletion_protection, prevent_destroy, encryption,
    public access ...) and newly added ``ignore_changes``. Boost: the file goes away or is touched within 3 commits,
    or the protective value is not present at HEAD. A same-day restore by the same author drops the hit."""
    keyrx = "|".join(g[0] for g in _GUARDS) + r"|ignore_changes"
    rows = ctx.grep(rf"(?i)\b({keyrx})\b", None, path_regex=IAC_RX.pattern)
    for (sha, path), g in _group(rows).items():
        if _dev(path) or is_test(path):
            ctx.drop("dev_test_sandbox")
            continue
        flips: list[tuple[str, str]] = []
        for key, prot, unprot in _GUARDS:
            for ln in g["-"]:
                m1 = re.search(rf"(?i)\b({key})\b\s*=\s*({prot})\b", ln)
                if not m1:
                    continue
                if any(re.search(rf"(?i)\b{m1.group(1)}\b\s*=\s*({unprot})\b", ln2) for ln2 in g["+"]):
                    flips.append((m1.group(1), prot))
        new_ignore = any("ignore_changes" in ln for ln in g["+"]) and not any("ignore_changes" in ln for ln in g["-"])
        if not flips and not new_ignore:
            continue
        c = ctx.commit(sha)
        if flips:
            k0 = flips[0][0]
            restored = [x for x in _later(ctx, path, c["seq"]) if x["ts"] - c["ts"] <= DAY
                        and x["author_email"] == c["author_email"]
                        and any(re.search(rf"(?i)\b{re.escape(k0)}\b\s*=\s*{flips[0][1]}\b", t)
                                for t in ctx.added_removed(x["hash"], path)[0])]
            if restored:
                ctx.drop("same_pr_toggle")
                continue
        near = _later(ctx, path, c["seq"], within=3)
        gone = path not in ctx.head_files
        head = ctx.text_at_head(path) or ""
        unrestored = bool(flips) and not any(
            re.search(rf"(?i)\b{re.escape(k)}\b\s*=\s*{prot}\b", head) for k, prot in flips[:1])
        names = sorted({k for k, _ in flips}) or ["ignore_changes"]
        score = 2 + (2 if gone else 0) + (1 if near else 0) + (1.5 if unrestored else 0) + (1 if new_ignore else 0)
        yield Hit(subject=f"{sha}|{path}|{','.join(names)}", score=score,
                  title=f"IaC guard lifted in {path}: {', '.join(names)}",
                  evidence={"flipped": names, "new_ignore_changes": new_ignore, "file_gone_at_head": gone,
                            "touched_within_3_commits": [subject_of(x["message"]) for x in near[:3]],
                            "protective_value_not_at_head": unrestored},
                  commits=[sha] + [x["hash"] for x in near[:2]], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
def _head_blob(ctx: Ctx, rx: str, cap: int = 300) -> str:
    cache = getattr(ctx, "_infra_headblob", None)
    if cache is None:
        cache = ctx._infra_headblob = {}  # type: ignore[attr-defined]
    if rx not in cache:
        cache[rx] = "\n".join(ctx.text_at_head(p) or "" for p in ctx.match(rx)[:cap] if not _dev(p))
    return cache[rx]


_DATA_ENV = r"PGDATA|MYSQL_DATADIR|MONGO_DATA|data[-_]dir|DATA_DIR"


@candidate("persistent_state_mount_change")
def persistent_state_mount_change(ctx: Ctx):
    """Named volume / PVC claim removed or renamed (old name absent from every compose/k8s file at HEAD), a changed
    mountPath, or a data-dir env change. Proxy: line-level pairing in compose / k8s files (no full service graph).
    Suppressed by a migration/backup/restore file or message in the same commit."""
    rows = ctx.grep(rf"claimName\s*:|mountPath\s*:|^\s*-\s*[A-Za-z_][\w.\-]*:/[\w/.\-]+(:\w+)?\s*$|{_DATA_ENV}", None,
                    path_regex=_pr(K8S_RX.pattern, COMPOSE_PATH))
    allhead = _head_blob(ctx, _pr(K8S_RX.pattern, COMPOSE_PATH))
    for (sha, path), g in _group(rows).items():
        if _dev(path) or is_test(path) or is_docs(path):
            ctx.drop("dev_or_test_file")
            continue
        vol = re.compile(r"^\s*-\s*([A-Za-z_][\w.\-]*):/")
        claim = re.compile(r"claimName\s*:\s*[\"']?([\w.\-]+)")
        mpr = re.compile(r"mountPath\s*:\s*[\"']?(\S+?)[\"']?\s*$")
        old_src = {m.group(1) for ln in g["-"] if (m := vol.match(ln))}
        new_src = {m.group(1) for ln in g["+"] if (m := vol.match(ln))}
        old_claim = {m.group(1) for ln in g["-"] if (m := claim.search(ln))}
        new_claim = {m.group(1) for ln in g["+"] if (m := claim.search(ln))}
        gone = [n for n in sorted((old_src - new_src) | (old_claim - new_claim))
                if not re.search(rf"(?<![\w.\-]){re.escape(n)}(?![\w.\-])", allhead)]
        old_mp = {m.group(1) for ln in g["-"] if (m := mpr.search(ln))}
        new_mp = {m.group(1) for ln in g["+"] if (m := mpr.search(ln))}
        mp_changed = bool(old_mp and new_mp and old_mp != new_mp)
        d_old = [ln for ln in g["-"] if re.search(_DATA_ENV, ln)]
        d_new = [ln for ln in g["+"] if re.search(_DATA_ENV, ln)]
        data_changed = bool(d_old and d_new and sorted(map(_norm, d_old)) != sorted(map(_norm, d_new)))
        if not gone and not mp_changed and not data_changed:
            continue
        if any("tmpfs" in ln or "emptyDir" in ln for ln in g["-"] + g["+"]) and not gone:
            ctx.drop("tmpfs_or_emptydir")
            continue
        c = ctx.commit(sha)
        if re.search(r"(?i)migrat|backup|restore|dump", c["message"] or "") or any(
                re.search(r"(?i)migrat|restore|backup|dump", fc["path"]) for fc in ctx.changes(sha)):
            ctx.drop("migration_in_commit")
            continue
        bits = ([f"volume/claim gone: {','.join(gone)}"] if gone else []) + (
            [f"mountPath {','.join(sorted(old_mp))} -> {','.join(sorted(new_mp))}"] if mp_changed else []) + (
            ["data dir setting changed"] if data_changed else [])
        yield Hit(subject=f"{sha}|{path}", score=3 + 2 * len(gone) + mp_changed + data_changed,
                  title=f"Persistent mount changed in {path}: {'; '.join(bits)}",
                  evidence={"old_volumes": sorted(old_src | old_claim), "new_volumes": sorted(new_src | new_claim),
                            "old_mount_path": sorted(old_mp), "new_mount_path": sorted(new_mp),
                            "old_data_env": [_norm(x) for x in d_old[:3]], "new_data_env": [_norm(x) for x in d_new[:3]],
                            "name_absent_at_head": gone},
                  commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
@candidate("k8s_availability_knob_reduction")
def k8s_availability_knob_reduction(ctx: Ctx):
    """replicas/minReplicas/replicaCount cut to 1 or by >=2x, deleted PodDisruptionBudget, removed anti-affinity or
    topologySpread, on prod-looking k8s/helm paths. Dropped when an HPA is introduced in the same commit or the file
    is renamed (chart refactor). Boosted by incident/cost/drain messages."""
    rx = (r"^\s*(replicas|minReplicas|replicaCount|minAvailable)\s*:|PodDisruptionBudget|podAntiAffinity|"
          r"topologySpreadConstraints|HorizontalPodAutoscaler")
    renamed = {(r["hash"], r["path"]) for r in ctx.store.q("SELECT hash, path FROM file_changes WHERE status LIKE 'R%'")}
    num = re.compile(r"^\s*(replicas|minReplicas|replicaCount)\s*:\s*[\"']?(\d+)")
    for (sha, path), g in _group(ctx.grep(rx, None, path_regex=K8S_RX.pattern)).items():
        if _dev(path) or is_docs(path):
            ctx.drop("dev_staging_examples")
            continue
        reasons: list[str] = []
        old = {m.group(1): int(m.group(2)) for ln in g["-"] if (m := num.match(ln))}
        new = {m.group(1): int(m.group(2)) for ln in g["+"] if (m := num.match(ln))}
        for k, o in old.items():
            n = new.get(k)
            if n is not None and n < o and (n == 1 or n * 2 <= o):
                reasons.append(f"{k} {o} -> {n}")
        if any("kind: PodDisruptionBudget" in ln for ln in g["-"]) and not any("kind: PodDisruptionBudget" in ln for ln in g["+"]):
            reasons.append("PodDisruptionBudget deleted")
        for kw in ("podAntiAffinity", "topologySpreadConstraints"):
            if any(kw in ln for ln in g["-"]) and not any(kw in ln for ln in g["+"]):
                reasons.append(f"{kw} removed")
        if not reasons:
            continue
        if any("kind: HorizontalPodAutoscaler" in ln for ln in g["+"]) or any(
                "kind: HorizontalPodAutoscaler" in (ctx.text_at(sha, fc["path"]) or "") and fc["status"] == "A"
                for fc in ctx.changes(sha) if K8S_RX.search(fc["path"])):
            ctx.drop("hpa_added")
            continue
        if (sha, path) in renamed:
            ctx.drop("chart_refactor")
            continue
        c = ctx.commit(sha)
        boost = bool(re.search(r"(?i)incident|outage|cost|drain|save|reduc|scale.?down|downsiz", c["message"] or ""))
        yield Hit(subject=f"{sha}|{path}", score=2 + len(reasons) + (2 if boost else 0),
                  title=f"Availability reduced in {path}: {'; '.join(reasons)}",
                  evidence={"reductions": reasons, "message": subject_of(c["message"])},
                  commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_RES_KEY = re.compile(r"(^|\.)resources\.(limits|requests)\.(memory|cpu)$")
_AUTO_KEY = re.compile(r"(minReplicas|maxReplicas|targetCPUUtilizationPercentage|targetMemoryUtilizationPercentage|"
                       r"averageUtilization)$")


def _resource_map(path: str, text: str | None) -> dict[str, Any] | None:
    docs = _docs(path, text)
    if not docs:
        return None
    out: dict[str, Any] = {}
    for i, d in enumerate(docs):
        for k, v in flatten(d, f"d{i}").items():
            if _RES_KEY.search(k) or _AUTO_KEY.search(k):
                out[k] = v
    return out


_WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod"}


def _rendered_resources(manifest: str | None) -> dict[str, Any] | None:
    """Per-container requests/limits (``Kind/name/container.limits.memory``) and HPA parameters of a rendered
    (``helm template``) multi-document manifest; None when it does not parse."""
    if not manifest:
        return None
    try:
        import yaml
        docs = [d for d in yaml.safe_load_all(manifest) if isinstance(d, dict)]
    except Exception:
        return None
    out: dict[str, Any] = {}
    for d in docs:
        kind = d.get("kind")
        name = (d.get("metadata") or {}).get("name", "?")
        if kind in _WORKLOADS:
            flat = flatten(d, "x")
            for k, v in flat.items():
                m = re.search(r"containers\[(\d+)\]\.resources\.(limits|requests)\.(memory|cpu)$", k)
                if m:
                    cname = flat.get(k.split(".resources.")[0] + ".name", m.group(1))
                    out[f"{kind}/{name}/{cname}.{m.group(2)}.{m.group(3)}"] = v
        elif kind == "HorizontalPodAutoscaler":
            for k, v in flatten(d.get("spec") or {}, "").items():
                if _AUTO_KEY.search(k):
                    out[f"HPA/{name}.{k}"] = v
    return out


def _helm_resource_events(ctx: Ctx) -> tuple[dict[str, list[dict]], list[str]]:
    """Resource/autoscaler changes between consecutive rendered manifests of each chart (``ctx.helm_renders()``)."""
    events: dict[str, list[dict]] = defaultdict(list)
    charts: list[str] = []
    by_chart: dict[str, list] = defaultdict(list)
    for r in ctx.helm_renders():
        by_chart[r["chart"]].append(r)
    oom = re.compile(r"(?i)\boom\b|out of memory|\b137\b|throttl|memory limit|OOMKill")
    for chart, rs in by_chart.items():
        rs.sort(key=lambda r: r["ts"])
        maps = [(r, _rendered_resources(r["manifest"])) for r in rs]
        maps = [(r, m) for r, m in maps if m is not None]
        if len(maps) < 2:
            continue
        charts.append(chart)
        for (_, old), (r, new) in zip(maps, maps[1:]):
            c = ctx.commit(r["commit_hash"])
            ev: list[str] = []
            for k, ov in old.items():
                nv = new.get(k)
                if k.startswith("HPA/"):
                    if nv is not None and nv != ov:
                        ev.append(f"autoscaler {k}: {ov} -> {nv}")
                    continue
                if nv is None and ".limits." in k:
                    ev.append(f"limit removed {k}")
                elif nv is not None and k.endswith("memory") and _qty(ov) and _qty(nv) and _qty(nv) >= 1.5 * _qty(ov):
                    ev.append(f"memory x{_qty(nv) / _qty(ov):.1f} {k}")
                elif nv is not None and ov != nv and k.endswith("cpu"):
                    ev.append(f"cpu {ov} -> {nv} {k}")
                if ".limits." in k:
                    rk = k.replace(".limits.", ".requests.")
                    if rk in old and rk in new and k in new and (old[rk] == ov) != (new[rk] == new[k]):
                        ev.append(f"request/limit equality changed {k}")
            msg = (c["message"] if c else "") or ""
            is_oom = bool(oom.search(msg))
            if c is not None and (ctx.is_bot(c) or re.search(r"(?i)\bvpa\b|vertical.?pod", msg)):
                ctx.drop("bot_or_vpa")
                continue
            if ev or is_oom:
                events[f"helm:{chart}"].append({"sha": r["commit_hash"], "ts": r["ts"], "events": ev, "oom": is_oom,
                                                "subject": subject_of(msg), "rendered": True})
    return events, charts


@candidate("k8s_resource_limit_churn")
def k8s_resource_limit_churn(ctx: Ctx):
    """Per k8s/helm/HCL file: memory raised >=1.5x, limit removed, request==limit equality changed, autoscaler param
    edits, heap/limit ratio >0.85, OOM/137/throttle messages; hit when >=2 resource changes or an OOM message.
    Rendered manifests first: when ``ctx.helm_renders()`` holds ``helm template`` output (afd-oracle sandbox helm), the
    per-container requests/limits and HPA parameters of consecutive renders of a chart are compared (``rendered: true``
    evidence; values interpolated by Helm, so templated text no longer matters) and the chart's own files are not
    re-parsed. Fallback for everything else: YAML is parsed before/after (capped at the latest 600 touching commits);
    templated or HCL files use ordered memory/cpu line pairing (worker-count > CPU limit is not checked). Dev overlays
    and VPA bots dropped."""
    rx = r"(?i)\b(memory|cpu|limits|requests|resources|minReplicas|maxReplicas|targetCPUUtilizationPercentage|Xmx)\b"
    groups = _group(ctx.grep(rx, None, path_regex=_pr(K8S_RX.pattern, IAC_RX.pattern)))
    keys = sorted(groups, key=lambda k: groups[k]["seq"])[-600:]
    events: dict[str, list[dict]] = defaultdict(list)
    helm_events, helm_charts = _helm_resource_events(ctx)
    events.update(helm_events)
    rendered_charts = [c.strip("/") for c in helm_charts if c.strip("/") and c.strip("/") != "."]
    oom = re.compile(r"(?i)\boom\b|out of memory|\b137\b|throttl|memory limit|OOMKill")
    line_rx = re.compile(r"^\s*(memory|cpu)\s*[:=]\s*[\"']?([\d.]+[A-Za-z]*)")
    for sha, path in keys:
        if _dev(path) or is_docs(path):
            ctx.drop("dev_overlay")
            continue
        if any(ch in path for ch in rendered_charts):
            ctx.drop("covered_by_helm_render")
            continue
        c = ctx.commit(sha)
        if ctx.is_bot(c) or re.search(r"(?i)\bvpa\b|vertical.?pod", c["message"] or ""):
            ctx.drop("bot_or_vpa")
            continue
        g = groups[(sha, path)]
        yml = path.endswith((".yaml", ".yml"))
        old = _resource_map(path, ctx.text_before(sha, path)) if yml else None
        new = _resource_map(path, ctx.text_at(sha, path)) if yml else None
        ev: list[str] = []
        if old is not None and new is not None:
            for k, ov in old.items():
                nv = new.get(k)
                if _RES_KEY.search(k):
                    if nv is None and ".limits." in k:
                        ev.append(f"limit removed {k}")
                    elif nv is not None and k.endswith("memory") and _qty(ov) and _qty(nv) and _qty(nv) >= 1.5 * _qty(ov):
                        ev.append(f"memory x{_qty(nv) / _qty(ov):.1f}")
                    elif nv is not None and ov != nv and k.endswith("cpu"):
                        ev.append(f"cpu {ov} -> {nv}")
                    if ".limits." in k:
                        rk = k.replace(".limits.", ".requests.")
                        if rk in old and rk in new and k in new and (old[rk] == ov) != (new[rk] == new[k]):
                            ev.append(f"request/limit equality changed {k}")
                elif nv is not None and nv != ov:
                    ev.append(f"autoscaler {k}: {ov} -> {nv}")
            for ln in g["+"]:
                m = re.search(r"-Xmx(\d+)([mMgG])", ln)
                lim = [_qty(v) for k, v in new.items() if k.endswith("limits.memory")]
                if m and lim and lim[0]:
                    heap = int(m.group(1)) * (1024 ** 3 if m.group(2) in "gG" else 1024 ** 2)
                    if heap / lim[0] > 0.85:
                        ev.append(f"heap/limit {heap / lim[0]:.2f}")
        else:
            o = [m.groups() for ln in g["-"] if (m := line_rx.match(ln))]
            n = [m.groups() for ln in g["+"] if (m := line_rx.match(ln))]
            if o and len(o) == len(n):
                for (k1, v1), (_, v2) in zip(o, n):
                    if k1 == "memory" and _qty(v1) and _qty(v2) and _qty(v2) >= 1.5 * _qty(v1):
                        ev.append(f"memory x{_qty(v2) / _qty(v1):.1f}")
                    elif v1 != v2:
                        ev.append(f"{k1} {v1} -> {v2}")
            elif len(o) > len(n):
                ev.append("limit/request lines removed")
        is_oom = bool(oom.search(c["message"] or ""))
        if ev or is_oom:
            events[path].append({"sha": sha, "ts": c["ts"], "events": ev, "oom": is_oom, "subject": subject_of(c["message"])})
    for path, evs in events.items():
        if len(evs) < 2 and not any(e["oom"] for e in evs):
            ctx.drop("single_definition")
            continue
        n365 = max(sum(1 for b in evs if 0 <= b["ts"] - a["ts"] <= 365 * DAY) for a in evs)
        rendered = path.startswith("helm:")
        hpaths = [fc["path"] for e in evs[-2:] for fc in ctx.changes(e["sha"]) if K8S_RX.search(fc["path"])][:3] \
            if rendered else [path]
        yield Hit(subject=path, score=len(evs) + 2 * sum(e["oom"] for e in evs) + (2 if n365 >= 3 else 0),
                  title=f"{path}: {len(evs)} resource/autoscaler changes"
                        f"{' incl. an OOM/throttle fix' if any(e['oom'] for e in evs) else ''}",
                  evidence={"changes": [{"commit": e["sha"][:10], "events": e["events"][:4], "msg": e["subject"]}
                                        for e in evs[-8:]], "changes_in_365d": n365, "rendered": rendered},
                  commits=[e["sha"] for e in evs[-5:]],
                  diffs=[(e["sha"], hp) for e in evs[-2:] for hp in hpaths[:1] if hp in {fc["path"] for fc in ctx.changes(e["sha"])}]
                  if rendered else [(e["sha"], path) for e in evs[-2:]], paths=hpaths)


# ---------------------------------------------------------------------------------------------------------------
def _probe_paths(text: str) -> dict[str, str]:
    """{'liveness': '/healthz', 'readiness': ...} by indentation-scanning probe blocks in YAML text."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)(liveness|readiness|startup)Probe\s*:", ln)
        if not m:
            continue
        ind = len(m.group(1))
        for ln2 in lines[i + 1:i + 14]:
            if ln2.strip() and len(ln2) - len(ln2.lstrip()) <= ind:
                break
            pm = re.match(r"^\s*path\s*:\s*[\"']?(\S+?)[\"']?\s*$", ln2)
            if pm:
                out[m.group(2)] = pm.group(1)
                break
    return out


@candidate("probe_and_healthcheck_drift")
def probe_and_healthcheck_drift(ctx: Ctx):
    """Probe / HEALTHCHECK edits: >=2 value changes, removal, failureThreshold<=1 / timeout<=1s, or liveness path ==
    readiness path (from the file text at the commit). Handler symbols are attached by name proxy (symbols named
    health/ready/live/ping), not by resolving the probe path. Boosted by CrashLoop/restart messages."""
    knobs = re.compile(r"^\s*(initialDelaySeconds|periodSeconds|timeoutSeconds|failureThreshold|successThreshold)"
                       r"\s*:\s*[\"']?(\d+)")
    flag = re.compile(r"--(interval|timeout|retries|start-period)[= ](\S+)")
    probe_kw = re.compile(r"(liveness|readiness|startup)Probe\s*:|HEALTHCHECK|healthcheck\s*:")
    rx = (r"(liveness|readiness|startup)Probe|HEALTHCHECK|healthcheck\s*:|initialDelaySeconds|periodSeconds|"
          r"timeoutSeconds|failureThreshold|successThreshold|--(interval|timeout|retries|start-period)")
    path_rx = _pr(K8S_RX.pattern, DOCKERFILE_PATH, COMPOSE_PATH)
    handlers = [s for s in ctx.alive_symbols if re.search(r"(?i)health|readiness|liveness|ready|\bping\b", s["name"] or "")
                and s["kind"] in ("function", "method") and not is_test(s["path"])][:3]
    for (sha, path), g in _group(ctx.grep(rx, None, path_regex=path_rx)).items():
        if _dev(path) or is_docs(path):
            ctx.drop("dev_or_example")
            continue
        if sorted(map(_norm, g["-"])) == sorted(map(_norm, g["+"])):
            ctx.drop("format_only")
            continue
        o = {m.group(1): int(m.group(2)) for ln in g["-"] if (m := knobs.match(ln))}
        n = {m.group(1): int(m.group(2)) for ln in g["+"] if (m := knobs.match(ln))}
        fo = {m.group(1): m.group(2) for ln in g["-"] for m in flag.finditer(ln)}
        fn = {m.group(1): m.group(2) for ln in g["+"] for m in flag.finditer(ln)}
        changes = ([(k, o[k], n[k]) for k in o if k in n and o[k] != n[k]]
                   + [(k, fo[k], fn[k]) for k in fo if k in fn and fo[k] != fn[k]])
        reasons = []
        if len(changes) >= 2:
            reasons.append(f"{len(changes)} value changes")
        if any(probe_kw.search(x) for x in g["-"]) and not any(probe_kw.search(x) for x in g["+"]):
            reasons.append("probe removed")
        if "failureThreshold" in n and n["failureThreshold"] <= 1:
            reasons.append("failureThreshold<=1")
        if "timeoutSeconds" in n and n["timeoutSeconds"] <= 1:
            reasons.append("timeout<=1s")
        pp = _probe_paths(ctx.text_at(sha, path) or "")
        if pp.get("liveness") and pp.get("liveness") == pp.get("readiness"):
            reasons.append(f"liveness path == readiness path ({pp['liveness']})")
        if not reasons:
            continue
        c = ctx.commit(sha)
        crash = bool(re.search(r"(?i)crash.?loop|restart|probe|kill|flap", c["message"] or ""))
        yield Hit(subject=f"{sha}|{path}", score=1 + len(reasons) + (2 if crash else 0),
                  title=f"Probe/healthcheck drift in {path}: {'; '.join(reasons)}",
                  evidence={"reasons": reasons, "changes": [list(map(str, x)) for x in changes[:6]],
                            "probe_paths": pp, "handler_candidates": [s["qualname"] for s in handlers]},
                  commits=[sha], diffs=[(sha, path)], paths=[path], symbols=[s["uid"] for s in handlers])


# ---------------------------------------------------------------------------------------------------------------
_DATASTORE = re.compile(r"(?i)image\s*:\s*\S*(postgres|redis|mongo|mysql|mariadb|memcached|elasticsearch|opensearch|"
                        r"rabbitmq|cassandra|etcd|influx|clickhouse)")


@candidate("listen_address_scope_flip")
def listen_address_scope_flip(ctx: Ctx):
    """Server-start lines (CMD/command/args/--host/--bind/listen/serve) whose bind address flips between loopback and
    0.0.0.0/:: in either direction, and newly published unprefixed ports in a compose file whose text names a datastore
    image (proxy: image is looked up anywhere in the file, not per service). Generic 'host:' keys are ignored."""
    start = re.compile(r"(?i)\b(CMD|ENTRYPOINT|command|args|listen|serve|run|uvicorn|gunicorn|flask|bind|runserver|"
                       r"ListenAndServe)\b|--host|--bind|\s-b\s")
    addr = re.compile(r"0\.0\.0\.0|\[::\]|127\.0\.0\.1|localhost|\[::1\]")
    wide = re.compile(r"0\.0\.0\.0|\[::\]")
    loop = re.compile(r"127\.0\.0\.1|localhost|\[::1\]")
    rows = ctx.grep(r"0\.0\.0\.0|127\.0\.0\.1|localhost|\[::\]|::1|^\s*-\s*[\"']?\d{2,5}:\d{2,5}[\"']?\s*$", None)
    for (sha, path), g in _group(rows).items():
        if is_test(path) or is_docs(path):
            ctx.drop("tests_or_docs")
            continue
        o = [ln for ln in g["-"] if start.search(ln) and addr.search(ln) and not re.match(r"^\s*host\s*:", ln)]
        n = [ln for ln in g["+"] if start.search(ln) and addr.search(ln) and not re.match(r"^\s*host\s*:", ln)]
        flip = None
        if any(loop.search(x) for x in o) and any(wide.search(x) for x in n) and not any(loop.search(x) for x in n):
            flip = "loopback -> all interfaces"
        elif any(wide.search(x) for x in o) and any(loop.search(x) for x in n) and not any(wide.search(x) for x in n):
            flip = "all interfaces -> loopback"
        if flip:
            if flip.startswith("loopback") and re.search(COMPOSE_PATH, path) and _dev(path):
                ctx.drop("dev_compose")
            else:
                yield Hit(subject=f"{sha}|{path}|bind", score=4 + (2 if flip.startswith("loopback") else 1),
                          title=f"Listen scope flipped in {path}: {flip}",
                          evidence={"direction": flip, "removed": [_norm(x) for x in o[:3]],
                                    "added": [_norm(x) for x in n[:3]]},
                          commits=[sha], diffs=[(sha, path)], paths=[path])
        if re.search(COMPOSE_PATH, path):
            if _dev(path):
                ctx.drop("dev_compose")
                continue
            ports = [m.group(1) for ln in g["+"] if (m := re.match(r"^\s*-\s*[\"']?(\d{2,5}:\d{2,5})[\"']?\s*$", ln))]
            if ports and _DATASTORE.search(ctx.text_at(sha, path) or ""):
                yield Hit(subject=f"{sha}|{path}|port", score=5,
                          title=f"Datastore port published on all interfaces in {path}: {', '.join(ports)}",
                          evidence={"ports": ports, "datastore_image_in_file": True},
                          commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_CLIENT_CTOR = (r"\b(requests\.Session|httpx\.(Async)?Client|aiohttp\.ClientSession|boto3\.(client|resource|Session)|"
                r"redis\.(Redis|StrictRedis|from_url)|psycopg2\.connect|pymongo\.MongoClient|MongoClient|Elasticsearch|"
                r"AsyncOpenAI|OpenAI|http\.Client|createClient|grpc\.insecure_channel)\s*\(")
_ROUTE_DECO = re.compile(r"@\s*\w+(\.\w+)*\.(route|get|post|put|delete|patch|websocket|api_route)\b")
_FACTORY = re.compile(r"(?i)^(_?get|_?create|_?make|_?build|_?new|_?init|_?setup|_?connect|_?open)|factory|__init__|"
                      r"^main$|bootstrap")


@candidate("per_request_client_construction")
def per_request_client_construction(ctx: Ctx):
    """(a) A fix commit moves a client constructor from function scope to module scope (removed indented constructor,
    added unindented one in the same file, or message mentions client reuse / TIME_WAIT); (b) at HEAD a non-factory
    function/method with in-degree >=3 (or a route decorator) constructs a client. Proxy for (b): constructor lines
    located in HEAD file text and mapped to symbols by HEAD line range (HEAD text and ranges are already exact, so
    unchanged by the symbol enrichment). For (a), diff_lines.symbol of the removed constructor lines gives the function
    the client used to be built in: it is attached as the hit's symbol, evidence ``removed_from_function`` is set, and
    a removal that really sat inside a function scores higher than one in a module-level block. CLIs, __main__, tests,
    factories are dropped."""
    rows = ctx.grep(_CLIENT_CTOR, None, path_regex=r"\.(py|js|ts|go|java|rb|kt)$")
    reuse_msg = re.compile(r"(?i)client reuse|reuse (the )?(client|session|connection)|TIME_WAIT|connection pool|"
                           r"share (the )?(client|session)|singleton client|(client|session) per request")
    for (sha, path), g in _group(rows).items():
        if is_test(path) or re.search(r"(?i)(^|/)(cli|__main__|scripts?|bin)\b", path):
            ctx.drop("tests_clis")
            continue
        msg = ctx.commit(sha)["message"] or ""
        removed_indented = [x for x in g["-"] if re.match(r"^\s{4,}\S", x) or x.startswith("\t")]
        added_module = [x for x in g["+"] if re.match(r"^\S", x) and "=" in x]
        if removed_indented and (added_module or reuse_msg.search(msg)):
            fsyms = list(dict.fromkeys(
                x["symbol"] for x in ctx.lines_of(sha, path)
                if x["sign"] == "-" and x["symbol"] and re.search(_CLIENT_CTOR, x["text"])
                and x["symbol"].rsplit(":", 1)[-1] in ("function", "method")))[:3]
            yield Hit(subject=f"{sha}|{path}|fix", score=5 + (2 if reuse_msg.search(msg) else 0) + (1 if fsyms else 0),
                      title=f"Client constructor moved out of function scope in {path}",
                      evidence={"removed": [_norm(x) for x in removed_indented[:3]],
                                "added": [_norm(x) for x in added_module[:3]], "message": subject_of(msg),
                                "removed_from_function": bool(fsyms)},
                      commits=[sha], diffs=[(sha, path)], paths=[path], symbols=fsyms)
    paths = sorted({r["path"] for r in rows if r["sign"] == "+" and r["path"] in ctx.head_files
                    and not is_test(r["path"]) and re.match(r"^\s{4,}\S", r["text"])})[:300]
    ctor = re.compile(_CLIENT_CTOR)
    for p in paths:
        lines = (ctx.text_at_head(p) or "").splitlines()
        for sym in ctx.symbols_by_path.get(p, []):
            if sym["kind"] not in ("function", "method") or not sym["start_line"]:
                continue
            if _FACTORY.search(sym["name"] or ""):
                ctx.drop("bootstrap_factory")
                continue
            hit = next((ln for ln in lines[sym["start_line"]:sym["end_line"]]
                        if ctor.search(ln) and not _comment(ln)), None)
            if not hit:
                continue
            deco = "\n".join(lines[max(0, sym["start_line"] - 4):sym["start_line"]])
            ind = len(ctx.callers(sym["uid"]))
            route = bool(_ROUTE_DECO.search(deco))
            if ind < 3 and not route:
                continue
            yield Hit(subject=f"{sym['uid']}|head", score=2 + min(ind, 10) * 0.4 + (2 if route else 0),
                      title=f"{sym['qualname']} constructs a client per call ({ind} callers)",
                      evidence={"constructor_line": _norm(hit), "callers": ind, "route": route},
                      paths=[p], symbols=[sym["uid"]])


# ---------------------------------------------------------------------------------------------------------------
@candidate("http_cache_header_policy_change")
def http_cache_header_policy_change(ctx: Ctx):
    """Shared-cache enabling edits (public / s-maxage / cache_page) added, or private / no-store / Vary Cookie|
    Authorization removed, in a file whose text reads session/cookie/user/Authorization; plus any proxy_cache_key
    change. Proxy: 'handler reads per-user state' is judged on the whole file at that commit. Plain max-age/ETag and
    static-asset paths are dropped."""
    enable = re.compile(r"(?i)Cache-Control[^\n]*\b(public|s-maxage)\b|cache_page|@cache_control\([^)]*public|"
                        r"public\s*=\s*True|s_maxage|@cache\.cached")
    protect = re.compile(r"(?i)\b(private|no-store|never_cache|vary_on_cookie)\b|Vary[^\n]*(Cookie|Authorization)|"
                         r"vary_on_headers")
    rows = ctx.grep(_pr(enable.pattern, protect.pattern, "proxy_cache_key"), None)
    userrx = re.compile(r"(?i)\bsession\b|request\.cookies|\bcookies?\b|request\.user|current_user|Authorization|"
                        r"g\.user|req\.user")
    for (sha, path), g in _group(rows).items():
        if is_test(path) or is_docs(path):
            ctx.drop("tests_or_docs")
            continue
        if re.search(r"(?i)(^|/)(static|assets|public|dist|media)/", path) or path.endswith((".css", ".html")):
            ctx.drop("static_asset_path")
            continue
        if any("proxy_cache_key" in x for x in g["-"] + g["+"]):
            if g["-"] and g["+"] and sorted(map(_norm, g["-"])) != sorted(map(_norm, g["+"])):
                yield Hit(subject=f"{sha}|{path}|key", score=4, title=f"proxy_cache_key changed in {path}",
                          evidence={"removed": [_norm(x) for x in g["-"][:3]], "added": [_norm(x) for x in g["+"][:3]]},
                          commits=[sha], diffs=[(sha, path)], paths=[path])
            continue
        added_enable = [x for x in g["+"] if enable.search(x)]
        removed_protect = [x for x in g["-"] if protect.search(x)]
        if not added_enable and not removed_protect:
            continue
        if removed_protect and not added_enable and any(protect.search(x) for x in g["+"]):
            continue
        if not userrx.search(ctx.text_at(sha, path) or ""):
            ctx.drop("no_per_user_state_read")
            continue
        yield Hit(subject=f"{sha}|{path}", score=3 + (2 if added_enable else 0) + (2 if removed_protect else 0),
                  title=f"Shared-cache policy loosened in {path} where the file reads per-user state",
                  evidence={"added": [_norm(x) for x in added_enable[:3]],
                            "protections_removed": [_norm(x) for x in removed_protect[:3]]},
                  commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_TIGHT = (r"(?i)^\s*USER\s+(?!root\b|0\b)\S+|runAsNonRoot\s*:\s*true|readOnlyRootFilesystem\s*:\s*true|"
          r"drop\s*:\s*\[?\s*[\"']?ALL|read_only\s*:\s*true|runAsUser\s*:\s*[1-9]\d*|cap_drop")
_FOLLOW = (r"(?i)emptyDir|tmpfs|\bchown\b|TMPDIR|XDG_CACHE_HOME|\bHOME\s*=|^\s*USER\s+root|"
           r"readOnlyRootFilesystem\s*:\s*false|runAsNonRoot\s*:\s*false|read_only\s*:\s*false")


@candidate("container_user_readonly_fs_change")
def container_user_readonly_fs_change(ctx: Ctx):
    """A hardening line (final-stage USER, runAsNonRoot, readOnlyRootFilesystem, drop ALL, read_only) is added and
    within 20 commits a writable-path workaround (emptyDir/tmpfs/chown/TMPDIR/HOME/XDG_CACHE_HOME), a port move to
    >=1024 or a revert of the hardening appears in container files. Dev overrides and non-final stages are dropped."""
    pr = _pr(K8S_RX.pattern, DOCKERFILE_PATH, COMPOSE_PATH)
    tight_rows = ctx.grep(_TIGHT, "+", path_regex=pr)
    follow = ctx.grep(_FOLLOW, "+", path_regex=pr)
    ports = ctx.grep(r"(?i)\b(EXPOSE|containerPort|targetPort)\b\s*:?\s*[\"']?\d{4,5}", "+", path_regex=pr)
    reverts = ctx.grep(_TIGHT, "-", path_regex=pr)
    seen: set[tuple[str, str]] = set()
    for r in tight_rows:
        sha, path = r["hash"], r["path"]
        if (sha, path) in seen or _comment(r["text"]):
            continue
        if _dev(path):
            ctx.drop("dev_override")
            continue
        if re.match(r"(?i)^\s*USER\b", r["text"]) and re.search(DOCKERFILE_PATH, path):
            lines = (ctx.text_at(sha, path) or "").splitlines()
            last_from = max((i for i, ln in enumerate(lines) if re.match(r"(?i)^\s*FROM\b", ln)), default=-1)
            if r["line"] - 1 < last_from:
                ctx.drop("non_final_stage")
                continue
        seen.add((sha, path))
        lo, hi = r["seq"], r["seq"] + 20
        fu = [f for f in follow if lo <= f["seq"] <= hi and f["hash"] != sha and not _dev(f["path"])]
        pt = [p for p in ports if lo < p["seq"] <= hi and not _dev(p["path"])]
        rv = [x for x in reverts if lo < x["seq"] <= hi and x["path"] == path]
        why = ([f"{f['path']}: {_norm(f['text'])[:70]}" for f in fu[:3]] + [f"port: {_norm(p['text'])[:60]}" for p in pt[:2]]
               + [f"revert: {_norm(x['text'])[:60]}" for x in rv[:1]])
        if not why:
            continue
        shas = list(dict.fromkeys([sha] + [f["hash"] for f in fu[:2]] + [p["hash"] for p in pt[:1]] + [x["hash"] for x in rv[:1]]))
        yield Hit(subject=f"{sha}|{path}", score=2 + len(fu) + len(pt) + 3 * len(rv),
                  title=f"Hardening in {path} followed by workaround within 20 commits",
                  evidence={"hardening": _norm(r["text"]), "followups": why},
                  commits=shas, diffs=[(sha, path)] + [(f["hash"], f["path"]) for f in fu[:1]], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
@candidate("proxy_buffering_streaming_upgrade_change")
def proxy_buffering_streaming_upgrade_change(ctx: Ctx):
    """Edits to buffering / Upgrade / X-Accel-Buffering / proxy_http_version (and gzip) lines while the HEAD tree holds
    streaming or websocket code (text/event-stream, StreamingResponse, EventSource, WebSocket ...). Proxy: streaming
    presence is repo-wide (files that still exist at HEAD), not per-endpoint; hits list the streaming files."""
    srx = r"text/event-stream|StreamingResponse|EventSource|stream_with_context|WebSocket|websocket|SseEmitter"
    stream_paths = sorted({r["path"] for r in ctx.grep(srx, "+")
                           if r["path"] in ctx.head_files and not is_test(r["path"]) and not is_docs(r["path"])
                           and not re.search(r"(?i)(^|/)examples?/", r["path"])
                           and not r["path"].endswith((".conf", ".yaml", ".yml"))})
    rx = (r"(?i)proxy_buffering|proxy_request_buffering|X-Accel-Buffering|proxy_set_header\s+Upgrade|\$http_upgrade|"
          r"proxy_http_version|fastcgi_buffering|\bgzip\b|nginx\.ingress\.kubernetes\.io/proxy-(buffering|request-buffering)")
    rows = _group(ctx.grep(rx, None))
    if not stream_paths:
        if rows:
            ctx.drop("no_streaming_symbols", len(rows))
        return
    syms = [s["uid"] for p in stream_paths[:5] for s in ctx.symbols_by_path.get(p, [])[:2]]
    for (sha, path), g in rows.items():
        if is_test(path) or is_docs(path) or re.search(r"(?i)(^|/)examples?/", path):
            ctx.drop("examples_or_docs")
            continue
        if path in stream_paths and not any("X-Accel" in x for x in g["+"] + g["-"]):
            continue
        if ctx.path_commits(path) and ctx.path_commits(path)[0]["hash"] == sha:
            ctx.drop("file_introduction")
            continue
        if sorted(map(_norm, g["+"])) == sorted(map(_norm, g["-"])):
            continue
        gz = all(re.search(r"(?i)\bgzip\b", x) for x in g["+"] + g["-"])
        yield Hit(subject=f"{sha}|{path}", score=3 + (0 if gz else 2) + min(len(stream_paths), 5) * 0.3,
                  title=f"Proxy buffering/upgrade settings changed in {path}; streaming code exists at HEAD",
                  evidence={"removed": [_norm(x) for x in g["-"][:3]], "added": [_norm(x) for x in g["+"][:3]],
                            "streaming_files": stream_paths[:6], "gzip_only": gz},
                  commits=[sha], diffs=[(sha, path)], paths=[path] + stream_paths[:3], symbols=syms[:4])


# ---------------------------------------------------------------------------------------------------------------
_RET_KEY = (r"retention|backup_retention_period|expiration|expire|message_retention|keepLast|revisionHistoryLimit|"
            r"history_limit|HistoryLimit|retain")
_UNIT = {"ms": 1e-3 / 86400, "s": 1 / 86400, "sec": 1 / 86400, "m": 60 / 86400, "min": 60 / 86400, "h": 3600 / 86400,
         "hr": 3600 / 86400, "hours": 3600 / 86400, "d": 1.0, "day": 1.0, "days": 1.0, "w": 7.0, "week": 7.0,
         "weeks": 7.0, "mo": 30.0, "month": 30.0, "months": 30.0, "y": 365.0, "year": 365.0, "years": 365.0}


@candidate("retention_and_backup_reduction")
def retention_and_backup_reduction(ctx: Ctx):
    """Unit-normalised decrease >=2x (or to 0/1, or the key removed) on retention/backup/expiration/keepLast/
    revisionHistoryLimit keys in config, IaC and k8s files. Proxy: line-level key/value pairing within one file and
    commit. Cache/session TTL keys, CI artifact retention, dev/test paths are dropped."""
    kv = re.compile(rf"(?i)^\s*[\"']?([\w.\-]*(?:{_RET_KEY})[\w.\-]*)[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")
    rows = ctx.grep(rf"(?i)({_RET_KEY})", None,
                    path_regex=r"(?i)\.(ya?ml|json|toml|ini|cfg|conf|tf|tfvars|hcl|properties|env)$")
    for (sha, path), g in _group(rows).items():
        if _dev(path) or is_test(path) or is_docs(path):
            ctx.drop("dev_test")
            continue
        if CI_RX.search(path):
            ctx.drop("ci_artifact_retention")
            continue
        old = {m.group(1).lower(): (float(m.group(2)), m.group(3).lower()) for ln in g["-"] if (m := kv.match(ln))}
        new = {m.group(1).lower(): (float(m.group(2)), m.group(3).lower()) for ln in g["+"] if (m := kv.match(ln))}
        for key, (ov, ou) in old.items():
            if re.search(r"(?i)cache|session|token|otp|nonce|csrf|artifact", key):
                ctx.drop("cache_or_ephemeral_ttl")
                continue
            if key in new:
                nv, nu = new[key]
                a, b = ov * _UNIT.get(ou, 1.0), nv * _UNIT.get(nu, 1.0)
                if ov == 0 or not (b * 2 <= a or (nv in (0, 1) and ov > nv)):
                    continue
                what = f"{ov:g}{ou} -> {nv:g}{nu}"
            elif not any(key in x.lower() for x in g["+"]):
                what = f"{ov:g}{ou} -> removed"
            else:
                continue
            c = ctx.commit(sha)
            yield Hit(subject=f"{sha}|{path}|{key}",
                      score=3 + (2 if "removed" in what or what.endswith(("-> 0", "-> 1")) else 0),
                      title=f"Retention '{key}' reduced in {path}: {what}",
                      evidence={"key": key, "change": what, "message": subject_of(c["message"])},
                      commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_WORKLOAD = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")


def _by_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    out = {}
    for d in docs:
        kind, name = d.get("kind"), ((d.get("metadata") or {}).get("name"))
        if kind and name:
            out[(kind, str(name))] = d
    return out


@candidate("k8s_immutable_or_deprecated_change")
def k8s_immutable_or_deprecated_change(ctx: Ctx):
    """Parsed (non-templated) manifests, before vs after: selector.matchLabels or volumeClaimTemplates changed, or a
    pod-template label removed that other HEAD k8s files (Service/NetworkPolicy/affinity/nodeSelector) the commit did
    not change still reference; apiVersion bumps are a low tier. Helm-templated files are dropped. Capped at the
    latest 500 touching commits."""
    rows = ctx.grep(r"matchLabels|volumeClaimTemplates|^\s*apiVersion\s*:|^\s*labels\s*:|^\s*[\w./\-]+\s*:\s*[\w.\-]+\s*$",
                    None, path_regex=K8S_RX.pattern)
    groups = _group(rows)
    keys = sorted(groups, key=lambda k: groups[k]["seq"])[-500:]
    heads = {p: ctx.text_at_head(p) or "" for p in ctx.match(K8S_RX)[:300]}
    for sha, path in keys:
        if _dev(path) or is_docs(path):
            ctx.drop("examples_docs")
            continue
        before, after = ctx.text_before(sha, path), ctx.text_at(sha, path)
        if (before and "{{" in before) or (after and "{{" in after):
            ctx.drop("helm_templated")
            continue
        o, n = _by_name(_docs(path, before)), _by_name(_docs(path, after))
        for key, od in o.items():
            nd = n.get(key)
            if nd is None:
                continue
            notes: list[str] = []
            deps: list[str] = []
            tier = 3
            if key[0] in _WORKLOAD:
                osel = ((od.get("spec") or {}).get("selector") or {}).get("matchLabels")
                nsel = ((nd.get("spec") or {}).get("selector") or {}).get("matchLabels")
                if osel is not None and osel != nsel:
                    notes.append(f"selector.matchLabels {osel} -> {nsel}")
                if (od.get("spec") or {}).get("volumeClaimTemplates") is not None and \
                        (od.get("spec") or {}).get("volumeClaimTemplates") != (nd.get("spec") or {}).get("volumeClaimTemplates"):
                    notes.append("volumeClaimTemplates changed")
                ol = (((od.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
                nl = (((nd.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
                changed_paths = {fc["path"] for fc in ctx.changes(sha)}
                gone = {k: v for k, v in ol.items() if k not in nl}
                for k, v in gone.items():
                    prx = re.compile(rf"(?m)^\s*{re.escape(str(k))}\s*:\s*[\"']?{re.escape(str(v))}[\"']?\s*$")
                    for hp, ht in heads.items():
                        if hp not in changed_paths and prx.search(ht) and re.search(
                                r"kind:\s*(Service|NetworkPolicy|PodDisruptionBudget|ServiceMonitor)|affinity|nodeSelector", ht):
                            deps.append(f"{hp}: {k}={v}")
                if gone and deps:
                    notes.append(f"pod label(s) {gone} removed; still referenced in {len(deps)} file(s)")
                    tier = 4
            if od.get("apiVersion") != nd.get("apiVersion") and not notes:
                notes.append(f"apiVersion {od.get('apiVersion')} -> {nd.get('apiVersion')}")
                tier = 1
            if not notes:
                continue
            if any("selector" in x or "volumeClaim" in x for x in notes):
                tier = 5
            c = ctx.commit(sha)
            yield Hit(subject=f"{sha}|{path}|{key[0]}/{key[1]}", score=tier,
                      title=f"{key[0]}/{key[1]} in {path}: {notes[0][:100]}",
                      evidence={"notes": notes, "dependents": deps[:6], "message": subject_of(c["message"])},
                      commits=[sha], diffs=[(sha, path)], paths=[path])


# ---------------------------------------------------------------------------------------------------------------
_ING = (r"nginx\.ingress|traefik\.ingress|kubernetes\.io/ingress|alb\.ingress|haproxy\.org|rewrite-target|use-regex|"
        r"\bpathType\b|ingressClassName|auth-url|allowlist|whitelist-source-range|ssl-redirect|proxy-body-size|"
        r"configuration-snippet|server-snippet")


@candidate("ingress_annotation_semantics_change")
def ingress_annotation_semantics_change(ctx: Ctx):
    """Changed controller-prefixed annotations / rewrite-target / use-regex / pathType / ingressClassName / auth-url /
    allowlist lines in Ingress manifests or chart templates (file path or text contains Ingress). Boost: controller
    prefix change, or a fix touching the file within 20 commits. TLS-secret-only edits and examples are dropped."""
    rows = ctx.grep(_ING, None, path_regex=r"(?i)\.(ya?ml|tpl)$")
    pref = re.compile(r"(nginx\.ingress|traefik\.ingress|alb\.ingress|haproxy\.org|kubernetes\.io/ingress)")
    for (sha, path), g in _group(rows).items():
        if _dev(path) or is_docs(path):
            ctx.drop("examples")
            continue
        if not (re.search(r"(?i)ingress", path) or re.search(r"kind:\s*Ingress", ctx.text_at(sha, path) or "")):
            ctx.drop("not_an_ingress_file")
            continue
        if sorted(map(_norm, g["-"])) == sorted(map(_norm, g["+"])):
            continue
        if ctx.path_commits(path) and ctx.path_commits(path)[0]["hash"] == sha:
            ctx.drop("file_introduction")
            continue
        if all("secretName" in x for x in g["-"] + g["+"]):
            ctx.drop("tls_secret_rename")
            continue
        po = {m.group(1) for x in g["-"] if (m := pref.search(x))}
        pn = {m.group(1) for x in g["+"] if (m := pref.search(x))}
        prefix_change = bool(po and pn and po != pn)
        c = ctx.commit(sha)
        later = _later(ctx, path, c["seq"], fix=True, within=20)
        yield Hit(subject=f"{sha}|{path}", score=2 + (3 if prefix_change else 0) + 2 * len(later),
                  title=f"Ingress semantics changed in {path}",
                  evidence={"removed": [_norm(x) for x in g["-"][:4]], "added": [_norm(x) for x in g["+"][:4]],
                            "controller_prefix_change": [sorted(po), sorted(pn)] if prefix_change else None,
                            "fixes_within_20": [subject_of(x["message"]) for x in later[:3]]},
                  commits=[sha] + [x["hash"] for x in later[:2]], diffs=[(sha, path)], paths=[path])
