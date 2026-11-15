"""GitHub pull requests and issues: the "why" behind changes, linked to commits.

Opt-in per repository (dashboard toggle or `afd-oracle discussions <repo>`). Uses the GitHub REST API with a token from
GITHUB_TOKEN / GH_TOKEN or `gh auth token`. Incremental: only items updated since the last sync are fetched. Links:
a merged PR's merge commit, main-line commits whose subject ends with "(#N)" (squash merges), and commits whose
message references "#N" (fixes/closes/refs). Stored in `discussions` + `discussion_commits`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from afd_oracle_core.store import Store

API = "https://api.github.com"
BODY_MAX = 8000


def github_slug(store: Store) -> str | None:
    urls = [store.get_meta("origin") or ""] + json.loads(store.get_meta("remotes", "[]") or "[]")
    for u in urls:
        m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", u or "")
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def token() -> str | None:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    gh = shutil.which("gh")
    if gh:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


class RateLimited(Exception):
    pass


def _get(url: str, tok: str | None) -> tuple[list | dict, dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "afd-oracle",
                                               **({"Authorization": f"Bearer {tok}"} if tok else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https endpoint
            return json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            raise RateLimited(e.headers.get("X-RateLimit-Reset", "")) from e
        raise


def _labels(item: dict) -> str:
    return json.dumps([lb.get("name") for lb in item.get("labels") or [] if isinstance(lb, dict)])


def sync(store: Store, max_pages: int = 30, fetch=_get, log=print) -> dict:
    slug = github_slug(store)
    if not slug:
        return {"skipped": "origin is not a GitHub repository"}
    tok = token()
    since = store.get_meta("discussions_synced_at")
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows, stats = [], {"pulls": 0, "issues": 0, "pages": 0, "rate_limited": False, "authenticated": bool(tok)}
    try:
        # repository facts (visibility, forks) and releases: used by catalogue builders that need them
        meta, _ = fetch(f"{API}/repos/{slug}", tok)
        stats["pages"] += 1
        if isinstance(meta, dict) and meta:
            store.set_meta("github_repo", json.dumps({k: meta.get(k) for k in (
                "private", "visibility", "fork", "forks_count", "archived", "default_branch", "stargazers_count",
                "open_issues_count", "allow_forking")}))
        releases = []
        for page in range(1, 6):
            data, _ = fetch(f"{API}/repos/{slug}/releases?per_page=100&page={page}", tok)
            stats["pages"] += 1
            if not data:
                break
            releases += [(r.get("tag_name"), r.get("name"), r.get("created_at"), r.get("published_at"),
                          int(bool(r.get("draft"))), int(bool(r.get("prerelease")))) for r in data if r.get("tag_name")]
            if len(data) < 100:
                break
        if releases:
            from ..catalog.enrich import ensure
            ensure(store)
            with store.tx() as conn:
                conn.executemany("INSERT OR REPLACE INTO github_releases VALUES (?,?,?,?,?,?)", releases)
        stats["releases"] = len(releases)
        # pull requests, newest update first; stop at the last sync point
        for page in range(1, max_pages + 1):
            data, _ = fetch(f"{API}/repos/{slug}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}", tok)
            stats["pages"] += 1
            if not data:
                break
            stop = False
            for p in data:
                if since and p.get("updated_at", "") < since:
                    stop = True
                    break
                rows.append((f"pr:{p['number']}", "pr", p["number"], p.get("title") or "", (p.get("body") or "")[:BODY_MAX],
                             "merged" if p.get("merged_at") else p.get("state"), _labels(p), p.get("html_url"),
                             (p.get("user") or {}).get("login"), p.get("created_at"), p.get("closed_at"), p.get("merged_at"),
                             p.get("merge_commit_sha") if p.get("merged_at") else None))
                stats["pulls"] += 1
            if stop or len(data) < 100:
                break
        # issues (the endpoint also returns PRs: skip those)
        q = f"&since={urllib.parse.quote(since)}" if since else ""
        for page in range(1, max_pages + 1):
            data, _ = fetch(f"{API}/repos/{slug}/issues?state=all&sort=updated&direction=desc&per_page=100&page={page}{q}", tok)
            stats["pages"] += 1
            if not data:
                break
            for i in data:
                if "pull_request" in i:
                    continue
                rows.append((f"issue:{i['number']}", "issue", i["number"], i.get("title") or "",
                             (i.get("body") or "")[:BODY_MAX], i.get("state"), _labels(i), i.get("html_url"),
                             (i.get("user") or {}).get("login"), i.get("created_at"), i.get("closed_at"), None, None))
                stats["issues"] += 1
            if len(data) < 100:
                break
    except RateLimited as e:
        stats["rate_limited"] = True
        stats["rate_limit_reset"] = e.args[0] if e.args else None
    with store.tx() as conn:
        conn.executemany("INSERT OR REPLACE INTO discussions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         [r[:12] for r in rows])
        conn.executemany("INSERT OR IGNORE INTO discussion_commits VALUES (?,?)",
                         [(r[2], r[12]) for r in rows if r[12] and store.commit(r[12])])
    stats["linked_commits"] = link_commits(store)
    if not stats["rate_limited"]:
        store.set_meta("discussions_synced_at", started)
    stats["total"] = store.q1("SELECT count(*) n FROM discussions")["n"]
    return stats


def link_commits(store: Store) -> int:
    numbers = {r["number"] for r in store.q("SELECT number FROM discussions")}
    rows = []
    for c in store.q("SELECT hash, message FROM commits WHERE is_merge=0"):
        msg = c["message"] or ""
        subject = msg.splitlines()[0] if msg else ""
        m = re.search(r"\(#(\d+)\)\s*$", subject)
        refs = {int(m.group(1))} if m else set()
        refs |= {int(n) for n in re.findall(r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?|refs?|see)\s+#(\d+)", msg)}
        for n in refs & numbers:
            rows.append((n, c["hash"]))
    with store.tx() as conn:
        conn.executemany("INSERT OR IGNORE INTO discussion_commits VALUES (?,?)", rows)
    return store.q1("SELECT count(*) n FROM discussion_commits")["n"]


def for_commit(store: Store, sha: str) -> list[dict]:
    return [{"number": r["number"], "kind": r["kind"], "title": r["title"], "state": r["state"], "url": r["url"]}
            for r in store.q("SELECT d.* FROM discussion_commits dc JOIN discussions d ON d.number = dc.number "
                             "WHERE dc.hash=? ORDER BY d.kind DESC", (sha,))]


def search(store: Store, query: str, limit: int = 10, kind: str | None = None) -> list[dict]:
    words = [w for w in re.findall(r"\w{3,}", query.lower())][:8]
    if not words:
        return []
    rows = store.q("SELECT * FROM discussions" + (" WHERE kind=?" if kind else ""), (kind,) if kind else ())
    scored = []
    for r in rows:
        title, body = (r["title"] or "").lower(), (r["body"] or "").lower()
        s = sum(3 * title.count(w) + min(body.count(w), 5) for w in words)
        if s:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, r in scored[:limit]:
        commits = [x["hash"] for x in store.q("SELECT hash FROM discussion_commits WHERE number=?", (r["number"],))]
        out.append({"number": r["number"], "kind": r["kind"], "title": r["title"], "state": r["state"],
                    "labels": json.loads(r["labels"] or "[]"), "url": r["url"], "score": s,
                    "excerpt": (r["body"] or "")[:600], "commits": commits[:10]})
    return out
