"""Bug-fix lineage: which code was fixed, what each fix guarded, and which tests came with it.

A fix commit is a main-line, non-merge commit whose message reads like a fix (or that reverts a regression). For
each symbol its diff touches we record the fix kinds (bug / regression / security / race / performance), the guard
lines it added (None checks, raises, isinstance, locks, bounds, escaping...) and the test functions added alongside.
That yields per-symbol fragility, "known pitfalls", and the hardened / race / performance zones used by the gate.
"""
from __future__ import annotations

import json
import re

import pygit2

from afd_oracle_core.ids import symbol_uid
from afd_oracle_core.store import Store

from ..ast.extractor import language_for
from ..ast.parse_cache import ParseCache
from .source import is_test_path

FIX = re.compile(r"(?i)\b(fix(e[sd]|ing)?|bug(fix)?|regression|crash(es|ed)?|broken|hotfix|issue\s*#?\d+|"
                 r"resolve[sd]?\s+#\d+|closes?\s+#\d+|workaround|correct(s|ed)?\s+(the\s+)?(handling|behaviou?r))\b")
NOT_FIX = re.compile(r"(?i)^(fix(ed)?\s+)?(typo|typos|docs?|documentation|spelling|grammar|whitespace|lint|flake8|"
                     r"formatting|changelog|readme|tests?\s+only)\b|\btypo\b")
KINDS = {
    "security": re.compile(r"(?i)\b(security|cve-\d{4}|xss|csrf|ssrf|injection|vulnerab\w*|sanitiz\w*|escap(e|ing)|"
                           r"exploit|traversal|unsafe|auth(entication|orization)?\s+bypass|secret)\b"),
    "race": re.compile(r"(?i)\b(race|deadlock|thread[- ]?safe\w*|concurren\w*|atomic\w*|lock(ing|s)?|"
                       r"synchroni[sz]\w*|reentran\w*)\b"),
    "performance": re.compile(r"(?i)\b(perf(ormance)?|speed\s*up|faster|slow(ness|down)?|optimi[sz]\w*|"
                              r"memory\s+leak|benchmark|quadratic|n\+1)\b"),
    "regression": re.compile(r"(?i)\bregress(ion|ed)\b"),
}
GUARDS = [
    ("none_check", re.compile(r"\bis\s+(not\s+)?None\b|===?\s*(null|undefined)\b|!==?\s*(null|undefined)\b")),
    ("raise", re.compile(r"^\s*(raise|throw)\b")),
    ("type_check", re.compile(r"\bisinstance\(|\btypeof\b|\binstanceof\b")),
    ("lock", re.compile(r"\b(with\s+.*lock|\.acquire\(|Lock\(|RLock\(|Semaphore\()")),
    ("bounds", re.compile(r"\blen\(.*\)\s*[<>]=?|\b(min|max)\(|\.length\s*[<>]=?|\bclamp\(")),
    ("empty_check", re.compile(r"^\s*if\s+not\s+\w+|^\s*if\s*\(\s*!\w+")),
    ("exception_handling", re.compile(r"^\s*(try\s*:|except\b|catch\s*\()")),
    ("sanitize", re.compile(r"\b(escape|quote|sanitize|safe_join|secure_filename|normpath|realpath)\(")),
    ("assert", re.compile(r"^\s*assert\b")),
]


def classify_message(msg: str) -> list[str]:
    subject = (msg or "").splitlines()[0] if msg else ""
    kinds = [k for k, rx in KINDS.items() if rx.search(msg or "")]
    if (FIX.search(msg or "") and not NOT_FIX.search(subject)) or kinds:
        return ["bug", *kinds] if FIX.search(msg or "") else kinds
    return []


def build(repo: pygit2.Repository, store: Store, cache: ParseCache, max_commits: int = 4000) -> list[tuple]:
    rows: list[tuple] = []
    reverted_regressions = {r["reverted"] for r in store.q("SELECT reverted FROM reverts")}
    commits = store.q("SELECT hash, message, ts FROM commits WHERE on_main=1 AND is_merge=0 AND is_bulk=0 "
                      "ORDER BY seq DESC")
    n = 0
    for c in commits:
        kinds = classify_message(c["message"])
        if not kinds and c["hash"] not in reverted_regressions:
            continue
        if c["hash"] in reverted_regressions and "regression" not in kinds:
            kinds = [*kinds, "reverted"]
        n += 1
        if n > max_commits:
            break
        try:
            commit = repo.get(c["hash"])
            if not commit.parents:
                continue
            diff = repo.diff(commit.parents[0], commit, context_lines=0)
        except Exception:
            continue
        per_symbol: dict[str, dict] = {}
        tests_added: list[str] = []
        for patch in diff:
            d = patch.delta
            path = d.new_file.path
            lang = language_for(path)
            if not lang or d.is_binary:
                continue
            if is_test_path(path):
                for h in patch.hunks:
                    for ln in h.lines:
                        if ln.origin == "+":
                            m = re.match(r"\s*(?:async\s+)?def\s+(test\w*)|\s*(?:it|test)\(\s*[\"'`]([^\"'`]+)", ln.content)
                            if m:
                                tests_added.append(f"{path}::{m.group(1) or m.group(2)}")
                continue
            fp = cache.get(str(d.new_file.id), lang) if d.status_char() != "D" else None
            if fp is None:
                try:
                    from ..ast.extractor import parse_source
                    blob = repo.get(d.new_file.id)
                    fp = parse_source(lang, blob.data) if blob is not None else None
                except Exception:
                    fp = None
            if fp is None:
                continue
            for h in patch.hunks:
                for ln in h.lines:
                    if ln.origin not in "+-":
                        continue
                    lineno = ln.new_lineno if ln.origin == "+" else h.new_start
                    sym = None
                    for s in fp.symbols:
                        if s.kind != "variable" and s.start_line <= lineno <= s.end_line and (
                                sym is None or s.end_line - s.start_line < sym.end_line - sym.start_line):
                            sym = s
                    if sym is None:
                        continue
                    uid = symbol_uid(store.repo_id, path, sym.qualname, sym.kind)
                    entry = per_symbol.setdefault(uid, {"path": path, "guards": []})
                    if ln.origin == "+":
                        for name, rx in GUARDS:
                            if rx.search(ln.content):
                                entry["guards"].append({"kind": name, "line": ln.content.strip()[:160]})
                                break
        subject = (c["message"] or "").splitlines()[0][:200]
        for uid, e in per_symbol.items():
            rows.append((c["hash"], uid, e["path"], json.dumps(kinds), json.dumps(e["guards"][:8]),
                         json.dumps(tests_added[:10]), c["ts"], subject))
    return rows


def zones(store: Store) -> dict[str, dict[str, int]]:
    """uid -> {kind: count} for security / race / performance / regression fixes."""
    out: dict[str, dict[str, int]] = {}
    for r in store.q("SELECT uid, kinds FROM fix_events"):
        for k in json.loads(r["kinds"] or "[]"):
            out.setdefault(r["uid"], {}).setdefault(k, 0)
            out[r["uid"]][k] += 1
    return out
