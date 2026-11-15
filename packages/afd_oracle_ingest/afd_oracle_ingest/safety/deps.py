"""Supply chain: new dependencies, look-alike (typosquat) names, install hooks, and opt-in OSV advisories."""
from __future__ import annotations

import json
import re
import urllib.request

from .rules import Finding

POPULAR_PY = {"requests", "numpy", "pandas", "django", "flask", "urllib3", "setuptools", "six", "pyyaml", "cryptography",
              "boto3", "botocore", "jinja2", "click", "werkzeug", "pytest", "attrs", "certifi", "idna", "pydantic",
              "sqlalchemy", "aiohttp", "httpx", "rich", "typer", "scipy", "matplotlib", "pillow", "psycopg2", "redis",
              "celery", "fastapi", "uvicorn", "gunicorn", "beautifulsoup4", "lxml", "selenium", "paramiko", "pymongo",
              "colorama", "python-dateutil", "pytz", "markupsafe", "itsdangerous", "packaging", "wheel", "pip",
              "tqdm", "openai", "anthropic", "tensorflow", "torch", "scikit-learn", "dotenv", "python-dotenv"}
POPULAR_JS = {"react", "react-dom", "lodash", "express", "axios", "chalk", "commander", "moment", "vue", "typescript",
              "webpack", "eslint", "jest", "next", "debug", "uuid", "dotenv", "cross-env", "request", "colors",
              "yargs", "inquirer", "body-parser", "cors", "mongoose", "socket.io", "redux", "rxjs", "zod", "vite",
              "prettier", "babel-core", "@babel/core", "node-fetch", "dayjs", "classnames", "tslib", "jquery"}
HOOK_FILES = re.compile(r"(^|/)(setup\.py|setup\.cfg|pyproject\.toml|package\.json|Makefile|Dockerfile|"
                        r"conftest\.py|noxfile\.py|tox\.ini|\.pre-commit-config\.yaml|[^/]+\.sh)$|^\.github/workflows/|"
                        r"^\.gitlab-ci\.yml$|^\.circleci/|^Jenkinsfile$|^\.husky/")


def _lev(a: str, b: str) -> int:
    """Edit distance where swapping two adjacent letters counts as one edit (`reqeusts` vs `requests`)."""
    if abs(len(a) - len(b)) > 1:
        return 2
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def lookalike(name: str, ecosystem: str) -> str | None:
    n = name.lower().replace("_", "-")
    pop = POPULAR_PY if ecosystem == "pypi" else POPULAR_JS
    if n in pop:
        return None
    for p in pop:
        if _lev(n, p) <= 1 or n.replace("-", "") == p.replace("-", "") or (n.startswith(p) and n[len(p):] in ("s", "-js", "js", "2")):
            return p
    return None


def _dep_from_line(path: str, line: str) -> tuple[str, str] | None:
    s = line.strip()
    name = path.rsplit("/", 1)[-1]
    if name.startswith("requirements") or name.endswith((".in",)) and "requirements" in path:
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[.*\])?\s*([<>=!~].*)?$", s)
        return (m.group(1), "pypi") if m and not s.startswith(("-", "#")) else None
    if name == "pyproject.toml":
        m = re.match(r"^[\"']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[.*\])?\s*([<>=!~;].*)?[\"']\s*,?$", s)
        return (m.group(1), "pypi") if m else None
    if name == "package.json":
        m = re.match(r"^[\"'](@?[a-z0-9][\w./-]*)[\"']\s*:\s*[\"'][\^~<>=*\d.xX|\s-]+[\"']\s*,?$", s)
        return (m.group(1), "npm") if m else None
    return None


def diff_findings(path: str, added: list[tuple[int, str]]) -> list[Finding]:
    out = []
    if HOOK_FILES.search(path):
        out.append(Finding("supply_chain", "sc.hook_changed", "warn", path, added[0][0] if added else 1,
                           "build/CI/install hook changed: review what now runs on install, build or CI", None, {}))
    for line_no, text in added:
        if path.endswith("package.json") and re.search(r"[\"'](pre|post)?install[\"']\s*:", text):
            out.append(Finding("supply_chain", "sc.install_script", "block", path, line_no,
                               "package install script added: runs arbitrary code on every install", None,
                               {"text": text.strip()[:200]}))
        dep = _dep_from_line(path, text)
        if not dep:
            continue
        name, eco = dep
        near = lookalike(name, eco)
        if near:
            out.append(Finding("supply_chain", "sc.typosquat", "block", path, line_no,
                               f"new dependency `{name}` looks like `{near}` (possible typosquat)", None,
                               {"text": text.strip()[:200], "package": name}))
        else:
            out.append(Finding("supply_chain", "sc.new_dependency", "warn", path, line_no,
                               f"new dependency `{name}` ({eco}): check provenance, maintenance and licence", None,
                               {"text": text.strip()[:200], "package": name}))
    return out


def osv_check(packages: list[tuple[str, str, str | None]], timeout: float = 20.0) -> list[dict]:
    """Opt-in: query osv.dev for (name, ecosystem 'PyPI'|'npm', version). Sends package names to osv.dev."""
    if not packages:
        return []
    queries = [{"package": {"name": n, "ecosystem": e}, **({"version": v} if v else {})} for n, e, v in packages]
    req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=json.dumps({"queries": queries}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoint
        results = json.loads(r.read()).get("results", [])
    out = []
    for (n, e, v), res in zip(packages, results):
        for vuln in res.get("vulns") or []:
            out.append({"package": n, "ecosystem": e, "version": v, "id": vuln.get("id")})
    return out


def pinned_packages(sources: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for path, src in sources.items():
        name = path.rsplit("/", 1)[-1]
        if name.startswith("requirements") and name.endswith(".txt"):
            for s in src.lines:
                m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([\w.]+)", s.strip())
                if m:
                    out.append((m.group(1), "PyPI", m.group(2)))
    return out
