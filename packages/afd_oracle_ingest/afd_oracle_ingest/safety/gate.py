"""The change-safety gate: `assess_change` before an edit, `check_diff` after it.

Both read the safety tables written by `step.run_safety`; `check_diff` also re-runs the detectors on the changed code.
Severity contract: "block" items must be fixed or explicitly acknowledged by the user; "warn" items need a look.
"""
from __future__ import annotations

import json
import subprocess
from collections import deque
from pathlib import Path

import pygit2

from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store, loads

from ..git.walker import default_branch
from . import api, concurrency, contracts, deps, diffparse, rules
from .contracts import CONSUMER, EXTERNAL_ENV, PRODUCER
from .rules import Finding
from .source import SourceFile, from_text, is_test_path

SEV_ORDER = {"block": 0, "warn": 1, "info": 2}


class Gate:
    def __init__(self, store: Store, repo: pygit2.Repository):
        self.store, self.repo, self.rid = store, repo, store.repo_id
        self.prefix = f"sym:{self.rid}:"

    # ------------------------------------------------------------------ helpers
    def path_of(self, uid: str) -> str:
        return uid[len(self.prefix):].rsplit(":", 2)[0] if uid.startswith(self.prefix) else uid

    def resolve_targets(self, paths: list[str] | None, symbols: list[str] | None) -> tuple[list[dict], list[str]]:
        rows, files = [], set(paths or [])
        for s in symbols or []:
            if s.startswith("sym:"):
                r = self.store.q1("SELECT uid, path, qualname, kind FROM symbols WHERE uid=?", (s,))
            else:  # "path::Qual.name", "path:qual" or a bare qualified name
                path, _, qual = s.replace("::", ":").partition(":") if (":" in s) else ("", "", s)
                q = "SELECT uid, path, qualname, kind FROM symbols WHERE alive=1 AND qualname=?"
                args = [qual or path]
                if qual:
                    q += " AND path=?"
                    args.append(path)
                r = self.store.q1(q + " LIMIT 1", args)
            if r:
                rows.append(dict(r))
                files.add(r["path"])
        for p in paths or []:
            for r in self.store.q("SELECT uid, path, qualname, kind FROM symbols WHERE alive=1 AND path=? AND "
                                  "kind IN ('function','class','interface')", (p,)):
                rows.append(dict(r))
        uniq = {r["uid"]: r for r in rows}
        return list(uniq.values()), sorted(files)

    def callers(self, uids: list[str], depth: int = 2, limit: int = 200) -> list[dict]:
        seen, out = set(uids), []
        frontier = deque((u, 0) for u in uids)
        while frontier and len(out) < limit:
            u, d = frontier.popleft()
            if d >= depth:
                continue
            for r in self.store.q("SELECT src, kind, props FROM edges WHERE dst=? AND kind IN ('CALLS','OVERRIDES')", (u,)):
                if r["src"] in seen:
                    continue
                seen.add(r["src"])
                conf = loads(r["props"], {}).get("confidence")
                out.append({"uid": r["src"], "path": self.path_of(r["src"]), "via": r["kind"], "depth": d + 1,
                            "confidence": conf, "test": is_test_path(self.path_of(r["src"]))})
                frontier.append((r["src"], d + 1))
            # a change to a base-class method reaches its overrides too
            for r in self.store.q("SELECT dst FROM edges WHERE src=? AND kind='OVERRIDES'", (u,)):
                if r["dst"] not in seen:
                    seen.add(r["dst"])
                    out.append({"uid": r["dst"], "path": self.path_of(r["dst"]), "via": "OVERRIDDEN_BY",
                                "depth": d + 1, "confidence": "exact", "test": False})
        return out

    def tests_for(self, uids: list[str], files: list[str], caller_uids: list[str]) -> list[dict]:
        best: dict[str, dict] = {}
        targets = [(u, "direct") for u in uids] + [(f, "file") for f in files] + [(c, "via caller") for c in caller_uids]
        for t, how in targets:
            for r in self.store.q("SELECT test, source, weight FROM test_map WHERE target=?", (t,)):
                cur = best.get(r["test"])
                score = r["weight"] * {"direct": 1.0, "file": 0.7, "via caller": 0.5}[how]
                if cur is None or score > cur["score"]:
                    best[r["test"]] = {"test": r["test"], "how": f"{r['source']} ({how})", "score": round(score, 3)}
        return sorted(best.values(), key=lambda x: -x["score"])

    def hidden_partners(self, files: list[str], exclude: set[str] | None = None, min_jaccard: float = 0.25) -> list[dict]:
        out = []
        for f in files:
            for r in self.store.q("SELECT a, b, count, jaccard, cross_dir, structural_link FROM cochange "
                                  "WHERE (a=? OR b=?) AND jaccard>=? ORDER BY jaccard DESC LIMIT 8", (f, f, min_jaccard)):
                other = r["b"] if r["a"] == f else r["a"]
                if exclude and other in exclude or is_test_path(other):
                    continue
                out.append({"file": f, "partner": other, "jaccard": round(r["jaccard"], 3), "count": r["count"],
                            "hidden": not r["structural_link"], "cross_dir": bool(r["cross_dir"])})
        return out

    def contracts_for(self, files: list[str]) -> list[dict]:
        out = []
        for f in files:
            for r in self.store.q("SELECT DISTINCT kind, key, role FROM contracts WHERE path=?", (f,)):
                other_role = CONSUMER if r["role"] == PRODUCER else PRODUCER
                others = sorted({x["path"] for x in self.store.q(
                    "SELECT path FROM contracts WHERE kind=? AND key=? AND role=? AND path<>?",
                    (r["kind"], r["key"], other_role, f))})
                if others or r["role"] == PRODUCER:
                    out.append({"file": f, "kind": r["kind"], "key": r["key"], "role": r["role"],
                                "counterparts": others[:12]})
        return out

    def history(self, uids: list[str]) -> list[dict]:
        out = []
        for u in uids:
            rows = self.store.q("SELECT hash, kinds, guards, tests, ts, subject FROM fix_events WHERE uid=? "
                                "ORDER BY ts DESC", (u,))
            if not rows:
                continue
            kinds: dict[str, int] = {}
            for r in rows:
                for k in json.loads(r["kinds"]):
                    kinds[k] = kinds.get(k, 0) + 1
            guards = [g for r in rows for g in json.loads(r["guards"])][:6]
            out.append({"uid": u, "fixes": len(rows), "kinds": kinds, "zones": [k for k in ("security", "race",
                        "performance", "regression") if k in kinds],
                        "recent": [{"hash": r["hash"][:10], "subject": r["subject"]} for r in rows[:4]],
                        "guards": guards,
                        "regression_tests": sorted({t for r in rows for t in json.loads(r["tests"])})[:8]})
        return out

    def insights(self, files: list[str], uids: list[str]) -> list[dict]:
        out = []
        for r in self.store.q("SELECT uid, label, kind, severity, title, citations FROM insights WHERE status='verified'"):
            cit = loads(r["citations"], {})
            fpaths = {f.get("path") for f in cit.get("files", []) if isinstance(f, dict)}
            syms = {s["uid"] if isinstance(s, dict) else s for s in cit.get("symbols", [])}
            if fpaths & set(files) or syms & set(uids):
                out.append({"id": r["uid"], "label": r["label"], "kind": r["kind"], "severity": r["severity"],
                            "title": r["title"]})
        return out

    # ------------------------------------------------------------------ assess
    def assess_change(self, paths: list[str] | None = None, symbols: list[str] | None = None, depth: int = 2) -> dict:
        targets, files = self.resolve_targets(paths, symbols)
        uids = [t["uid"] for t in targets]
        if not targets and not files:
            return {"error": "no matching files or symbols in the knowledge base (check paths relative to the repo "
                             "root, or update the repository)"}
        api_rows = {r["uid"]: r for r in self.store.q("SELECT uid, signature, deprecated FROM api_surface WHERE ref='HEAD'")}
        for t in targets:
            a = api_rows.get(t["uid"])
            t["public"] = a is not None
            t["deprecated"] = a["deprecated"] if a else None
        callers = self.callers(uids, depth)
        prod_callers = [c for c in callers if not c["test"]]
        partners = self.hidden_partners(files, exclude=set(files))
        contr = self.contracts_for(files)
        tests = self.tests_for(uids, files, [c["uid"] for c in prod_callers[:40]])
        hist = self.history(uids)
        marks = ",".join("?" * len(files)) or "''"
        hazards = [dict(r) for r in self.store.q(
            f"SELECT category, rule, severity, path, line, symbol, message FROM findings WHERE path IN ({marks}) "
            "AND severity IN ('block','warn') ORDER BY CASE severity WHEN 'block' THEN 0 ELSE 1 END LIMIT 60", files)]
        risk_rows = {r["subject"]: (r["score"], loads(r["factors"], {})) for r in self.store.q(
            f"SELECT subject, score, factors FROM risk WHERE subject IN ({','.join('?' * (len(files) + len(uids))) or "''"})",
            [*files, *uids])}
        must, should = [], []
        public = [t for t in targets if t.get("public")]
        if public:
            must.append(f"Keep the public API compatible: {', '.join(t['qualname'] for t in public[:8])}"
                        f"{' …' if len(public) > 8 else ''} (external callers are not visible here)")
        for p in partners:
            if p["hidden"] and p["jaccard"] >= 0.3:
                should.append(f"`{p['partner']}` changes with `{p['file']}` {p['count']} times "
                              f"(Jaccard {p['jaccard']}) without an import link: check whether it needs the same change")
        for c in contr:
            if c["counterparts"]:
                must.append(f"{c['kind']} `{c['key']}` ({c['role']} here) is shared with: "
                            f"{', '.join(c['counterparts'][:5])}: keep both sides in step")
        for h in hist:
            if h["zones"]:
                must.append(f"`{h['uid'].rsplit(':', 2)[-2]}` is a {'/'.join(h['zones'])} zone "
                            f"({h['fixes']} fixes; e.g. {h['recent'][0]['subject']!r}): preserve its guards")
            elif h["fixes"] >= 3:
                should.append(f"`{h['uid'].rsplit(':', 2)[-2]}` is fragile ({h['fixes']} past fixes)")
        if tests:
            must.append(f"Run the affected tests ({len(tests)}): start with {', '.join(t['test'] for t in tests[:5])}")
        else:
            must.append("No test exercises this code: add tests that pin the current behaviour before changing it")
        vulns = self.vulnerabilities(files)
        crit = [v for v in vulns if v["severity"] in ("critical", "high")]
        if crit:
            should.append(f"{len(crit)} known critical/high scanner finding(s) in these files "
                          f"(e.g. {crit[0]['scanner']} {crit[0]['rule']} at {crit[0]['path']}:{crit[0]['line'] or ''}); "
                          "don't make them worse, and fix them if the change touches that code")
        level_score = max([s for s, _ in risk_rows.values()] + [0])
        level = "high" if level_score >= 70 or any(h["severity"] == "block" for h in hazards) or public else (
            "medium" if level_score >= 40 or prod_callers or partners else "low")
        return {
            "repo": self.rid, "risk_level": level, "max_risk_score": level_score,
            "targets": targets[:60], "files": files,
            "blast_radius": {"callers": len(prod_callers), "test_callers": len(callers) - len(prod_callers),
                             "top": prod_callers[:25]},
            "hidden_coupling": partners, "contracts": contr, "public_api": public[:30], "tests": tests[:40],
            "hazards": hazards, "vulnerabilities": vulns[:40], "history": hist,
            "insights": self.insights(files, uids),
            "risk": {k: {"score": s, "factors": f} for k, (s, f) in risk_rows.items()},
            "must": must, "should": should,
        }

    def vulnerabilities(self, files: list[str]) -> list[dict]:
        """Scanner findings (afd-oracle scan) in these files, most severe first."""
        if not files:
            return []
        try:
            rows = self.store.q(
                f"SELECT scanner, rule, severity, title, path, line, package, version, fixed, cve, symbol "
                f"FROM vuln_findings WHERE path IN ({','.join('?' * len(files))}) ORDER BY CASE severity "
                "WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, path, line",
                files)
        except Exception:  # the scan tables do not exist until the first scan
            return []
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ check_diff
    def _old_text(self, path: str, workdir: Path | None) -> str | None:
        if workdir is not None:
            r = subprocess.run(["git", "-C", str(workdir), "show", f"HEAD:{path}"], capture_output=True, stdin=subprocess.DEVNULL)
            return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
        try:
            _, head = default_branch(self.repo)
            obj = self.repo.revparse_single(f"{head}:{path}")
            return obj.data.decode("utf-8", "replace")
        except Exception:
            return None

    def check_diff(self, diff: str | None = None, workdir: str | None = None) -> dict:
        wd = Path(workdir) if workdir else None
        untracked: list[str] = []
        if diff is None:
            if wd is None:
                return {"error": "pass a unified diff, or the path of a working copy to diff against HEAD"}
            diff = subprocess.run(["git", "-C", str(wd), "diff", "HEAD", "--no-color", "-M"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL).stdout
            untracked = [p for p in subprocess.run(["git", "-C", str(wd), "ls-files", "--others", "--exclude-standard"],
                                                   capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.splitlines() if p.strip()]
        files = diffparse.parse(diff)
        for p in untracked:  # new files not yet added to git
            try:
                text = (wd / p).read_text(encoding="utf-8", errors="replace")  # type: ignore[operator]
            except OSError:
                continue
            fd = diffparse.FileDiff(None, p, [diffparse.Hunk(0, 0, 1, len(text.splitlines()),
                                                             [("+", ln) for ln in text.splitlines()])])
            files.append(fd)
        findings: list[Finding] = []
        changed_uids: dict[str, dict] = {}
        changed_files = {fd.path for fd in files}
        new_sources: dict[str, SourceFile] = {}
        removed_guard_lines: dict[str, list[tuple[int, str]]] = {}
        db_contracts = self.store.q("SELECT kind, key, role, path FROM contracts")
        for fd in files:
            if fd.binary:
                continue
            path = fd.path
            old_text = self._old_text(fd.old_path, wd) if fd.old_path else None
            if fd.new_path is None:
                new_text = None
            elif wd is not None and (wd / fd.new_path).is_file():
                new_text = (wd / fd.new_path).read_text(encoding="utf-8", errors="replace")
            else:
                new_text = diffparse.apply(old_text, fd)
            old_src = from_text(fd.old_path, old_text) if (fd.old_path and old_text is not None) else None
            new_src = from_text(fd.new_path, new_text) if (fd.new_path and new_text is not None) else None
            if new_src:
                new_sources[path] = new_src
            added = fd.added()
            added_lines = {n for n, _ in added}
            # changed symbols (new side for added lines, old side for removed lines)
            if new_src:
                for n in added_lines:
                    s = new_src.enclosing(n)
                    if s:
                        u = symbol_uid(self.rid, new_src.path, s.qualname, s.kind)
                        changed_uids.setdefault(u, {"uid": u, "path": new_src.path, "qualname": s.qualname,
                                                    "kind": s.kind, "change": "modified"})
            if old_src:
                for n, text in fd.removed():
                    s = old_src.enclosing(n)
                    if s:
                        u = symbol_uid(self.rid, old_src.path, s.qualname, s.kind)
                        changed_uids.setdefault(u, {"uid": u, "path": old_src.path, "qualname": s.qualname,
                                                    "kind": s.kind, "change": "modified"})
                        removed_guard_lines.setdefault(u, []).append((n, text.strip()))
            if new_src and old_src:
                old_q = {s.qualname for s in old_src.symbols()}
                for u, c in changed_uids.items():
                    if c["path"] == new_src.path and c["qualname"] not in old_q:
                        c["change"] = "added"
            # detectors on the added lines
            if new_src:
                findings += rules.scan(new_src, self.rid, only_lines=added_lines, diff_mode=True)
                findings += concurrency.scan(new_src, self.rid, only_lines=added_lines)
            findings += deps.diff_findings(path, added)
            # breaking changes
            if old_src or new_src:
                dep = {r["qualname"]: r["deprecated"] for r in self.store.q(
                    "SELECT qualname, deprecated FROM api_surface WHERE ref='HEAD' AND path=? AND deprecated IS NOT NULL",
                    (fd.old_path or path,))}
                findings += api.classify(old_src, new_src, self.rid, dep)
            # contract deltas
            old_c = {(c.kind, c.key, c.role) for c in (contracts.extract(old_src, self.rid) if old_src else [])}
            new_c_list = contracts.extract(new_src, self.rid) if new_src else []
            new_c = {(c.kind, c.key, c.role) for c in new_c_list}
            for kind, key, role in old_c - new_c:
                if role != PRODUCER:
                    continue
                other_prod = any(r["kind"] == kind and r["key"] == key and r["role"] == PRODUCER and r["path"] != path
                                 for r in db_contracts)
                consumers = sorted({r["path"] for r in db_contracts if r["kind"] == kind and r["key"] == key and
                                    r["role"] == CONSUMER and r["path"] not in changed_files})
                if consumers and not other_prod:
                    findings.append(Finding("contract", "contract.producer_removed", "block", path, 1,
                                            f"{kind} `{key}` is no longer defined here, but {len(consumers)} file(s) "
                                            f"still use it: {', '.join(consumers[:5])}", None,
                                            {"kind": kind, "key": key, "consumers": consumers[:10]}))
            for c in new_c_list:
                if c.role != CONSUMER or (c.kind, c.key, c.role) in old_c:
                    continue
                if c.kind == "env" and c.key in EXTERNAL_ENV:
                    continue
                has_prod = any(r["kind"] == c.kind and r["key"] == c.key and r["role"] == PRODUCER for r in db_contracts)
                has_prod = has_prod or any((c.kind, c.key, PRODUCER) in {(x.kind, x.key, x.role) for x in
                                           contracts.extract(s, self.rid)} for s in new_sources.values())
                if not has_prod and c.kind in ("config", "event", "endpoint", "registry", "env", "flag"):
                    findings.append(Finding("contract", "contract.consumer_without_producer",
                                            "info" if c.kind == "env" else "warn", path, c.line,
                                            f"new use of {c.kind} `{c.key}`, which nothing in the repository defines",
                                            c.symbol, {"kind": c.kind, "key": c.key}))
        # removed guards that earlier fixes added
        for u, lines in removed_guard_lines.items():
            guards = [g for r in self.store.q("SELECT hash, guards, subject FROM fix_events WHERE uid=?", (u,))
                      for g in [dict(x, hash=r["hash"], subject=r["subject"]) for x in json.loads(r["guards"])]]
            for n, text in lines:
                g = next((g for g in guards if g["line"] and g["line"] == text), None)
                if g:
                    findings.append(Finding("regression", "regression.guard_removed", "block", self.path_of(u), n,
                                            f"removes a {g['kind']} guard added by fix {g['hash'][:10]} "
                                            f"({g['subject']!r})", u, {"text": text[:200]}))
        uids = list(changed_uids)
        hist = {h["uid"]: h for h in self.history(uids)}
        for u, h in hist.items():
            if h["zones"]:
                findings.append(Finding("history", "history.sensitive_zone", "warn", self.path_of(u), 0,
                                        f"`{changed_uids[u]['qualname']}` is a {'/'.join(h['zones'])} zone "
                                        f"({h['fixes']} past fixes): review against {h['recent'][0]['hash']} "
                                        f"{h['recent'][0]['subject']!r}", u, {"guards": h["guards"][:3]}))
        # hidden partners not included in the change
        partners = [p for p in self.hidden_partners(sorted(changed_files), exclude=changed_files)
                    if p["hidden"] and p["jaccard"] >= 0.3]
        for p in partners:
            findings.append(Finding("coupling", "coupling.partner_not_changed", "warn", p["file"], 0,
                                    f"`{p['partner']}` changed together with `{p['file']}` in {p['count']} commits "
                                    f"(Jaccard {p['jaccard']}, no import link) but is not part of this change", None, p))
        # blast radius + tests
        prod_uids = [u for u in uids if not is_test_path(self.path_of(u))]
        callers = self.callers(prod_uids, depth=1)
        tests = self.tests_for(prod_uids, sorted(f for f in changed_files if not is_test_path(f)),
                               [c["uid"] for c in callers if not c["test"]])
        tests_in_change = sorted(f for f in changed_files if is_test_path(f))
        untested = []
        for u in prod_uids:
            c = changed_uids[u]
            if c["kind"] != "function":
                continue
            mapped = self.store.q1("SELECT 1 FROM test_map WHERE target IN (?, ?) LIMIT 1", (u, c["path"]))
            module = c["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if not mapped and not any(module in t for t in tests_in_change):
                untested.append(c["qualname"])
        if untested:
            findings.append(Finding("tests", "tests.untested_change", "warn", "", 0,
                                    f"no test exercises the changed code: {', '.join(untested[:10])}"
                                    f"{' …' if len(untested) > 10 else ''}", None, {"symbols": untested[:30]}))
        uniq = {f.id: f for f in findings}
        ordered = sorted(uniq.values(), key=lambda f: (SEV_ORDER[f.severity], f.category, f.path, f.line))
        counts = {s: sum(1 for f in ordered if f.severity == s) for s in SEV_ORDER}
        verdict = "block" if counts["block"] else ("warn" if counts["warn"] else "pass")
        return {
            "repo": self.rid, "verdict": verdict, "counts": counts,
            "files": [{"path": fd.path, "status": fd.status, "added": len(fd.added()), "removed": len(fd.removed())}
                      for fd in files],
            "changed_symbols": list(changed_uids.values())[:80],
            "findings": [f.as_dict() for f in ordered],
            "tests_to_run": tests[:50], "tests_in_change": tests_in_change,
            "callers_affected": [c for c in callers if not c["test"]][:40],
            "next_steps": _next_steps(verdict, ordered, tests),
        }


def _next_steps(verdict: str, findings: list[Finding], tests: list[dict]) -> list[str]:
    steps = []
    if verdict == "block":
        steps.append("Resolve every 'block' finding, or get the user's explicit acknowledgement for each one.")
    if any(f.rule.startswith("api.") and f.severity == "block" for f in findings):
        steps.append("Breaking public API change: keep a compatible signature (default the new parameter, keep the "
                     "old name as a deprecated alias) or document the break.")
    if any(f.category == "coupling" for f in findings):
        steps.append("Check the co-changing partner files listed above and update them if they share the contract.")
    if tests:
        steps.append("Run: " + " ".join(t["test"] for t in tests[:12]))
    if any(f.rule == "tests.untested_change" for f in findings):
        steps.append("Add tests for the untested changed code (or run `afd-oracle mutate` to measure test adequacy).")
    return steps
