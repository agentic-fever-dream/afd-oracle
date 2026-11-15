"""What a repository is for, from its own files at HEAD (no LLM): manifest description, README introduction,
language mix and top-level layout. Cached per (repo, head)."""
from __future__ import annotations

import json
import re
import tomllib
from collections import Counter

from afd_oracle_core.config import get_settings
from afd_oracle_core.registry import resolve_repo
from afd_oracle_core.store import Store

_CACHE: dict[tuple[str, str], dict] = {}
README_NAMES = ("README.md", "README.rst", "README.markdown", "README.txt", "README", "readme.md", "Readme.md")
LANG_NAMES = {"python": "Python", "typescript": "TypeScript", "tsx": "TypeScript (TSX)", "javascript": "JavaScript"}
EXT_LANG = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
            ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
            ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".swift": "Swift",
            ".scala": "Scala", ".tf": "Terraform", ".hcl": "HCL", ".vue": "Vue", ".svelte": "Svelte", ".sql": "SQL",
            ".sh": "Shell", ".yaml": "YAML", ".yml": "YAML", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
            ".md": "Markdown", ".rst": "reStructuredText", ".json": "JSON", ".toml": "TOML"}
DOC_LIKE = {"Markdown", "reStructuredText", "JSON", "YAML", "TOML"}


def _blob_text(repo, oid, limit: int = 300_000) -> str | None:
    try:
        b = repo.get(oid)
    except Exception:
        return None
    if b is None or getattr(b, "is_binary", True) or b.size > limit:
        return None
    return b.data.decode("utf-8", "replace")


def _manifest(files: dict, repo) -> dict:
    out: dict = {}
    if "pyproject.toml" in files:
        try:
            d = tomllib.loads(_blob_text(repo, files["pyproject.toml"]) or "")
            p = d.get("project") or (d.get("tool", {}).get("poetry") or {})
            urls = p.get("urls") or {}
            lic = p.get("license")
            out.update(name=p.get("name"), description=p.get("description"),
                       homepage=next((v for k, v in urls.items() if k.lower() in ("homepage", "documentation", "source")),
                                     p.get("homepage")),
                       license=lic.get("text") if isinstance(lic, dict) else lic, source="pyproject.toml")
        except Exception:
            pass
    if not out.get("description") and "package.json" in files:
        try:
            d = json.loads(_blob_text(repo, files["package.json"]) or "{}")
            out.update(name=out.get("name") or d.get("name"), description=d.get("description"),
                       homepage=out.get("homepage") or d.get("homepage"), license=out.get("license") or d.get("license"),
                       source="package.json")
        except Exception:
            pass
    if not out.get("description") and "setup.cfg" in files:
        m = re.search(r"(?m)^description\s*=\s*(.+)$", _blob_text(repo, files["setup.cfg"]) or "")
        if m:
            out.update(description=m.group(1).strip(), source="setup.cfg")
    if not out.get("description") and "Cargo.toml" in files:
        try:
            p = tomllib.loads(_blob_text(repo, files["Cargo.toml"]) or "").get("package") or {}
            out.update(name=p.get("name"), description=p.get("description"), homepage=p.get("homepage"),
                       license=p.get("license"), source="Cargo.toml")
        except Exception:
            pass
    if not out.get("name") and "go.mod" in files:
        m = re.search(r"(?m)^module\s+(\S+)", _blob_text(repo, files["go.mod"]) or "")
        if m:
            out.update(name=m.group(1), source=out.get("source") or "go.mod")
    return {k: v for k, v in out.items() if v}


_BADGE = re.compile(r"^\s*(\[!\[|!\[|<a\b|<img\b|<p\b|</p>|<div\b|</div>|<picture|</picture|<source|\.\. image::|"
                    r"\.\. \|.*\| image::|:target:|:alt:|\[!\[)", re.I)


SKIP_TITLES = re.compile(r"(?i)^(table of contents|contents|toc|index|sponsors?|backers|badges|status|"
                         r"installation|install|getting started|quick ?start|usage|license|contributing)$")


def _g1(m: re.Match) -> str:
    return m.group(1)


def _clean(t: str) -> str:
    """Plain text: markdown/rst links and emphasis, inline code and HTML removed."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)                 # images
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", _g1, t)              # [text](url)
    t = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", _g1, t)             # [text][ref]
    t = re.sub(r"\[([^\]]+)\](?![(:])", _g1, t)               # [text] shortcut refs
    t = re.sub(r"`([^`<]+?)\s*<[^>]+>`_+", _g1, t)            # rst `text <url>`_
    t = re.sub(r"`([^`]+)`_+", _g1, t)                        # rst `text`_
    t = re.sub(r"<[^>]*>?", "", t)
    t = re.sub(r"&[#\w]+;", " ", t)
    t = re.sub(r"(\*\*|__|\*|``)", "", t).replace("`", "")
    return re.sub(r"\s+", " ", t).strip(" :")


def _readme(text: str, rst: bool) -> dict:
    """Title and the first prose paragraphs (badges, HTML, link-only lines and tables of contents removed)."""
    lines = text.replace("\r\n", "\n").split("\n")
    title, paras, cur = None, [], []
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_rst_title = rst and s and nxt and len(set(nxt)) == 1 and nxt[0] in "=-~#*^" and len(nxt) >= len(s) - 2
        is_md_title = s.startswith("#")
        if is_rst_title or is_md_title or (rst and s and len(set(s)) == 1 and s[0] in "=-~#*^"):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            heading = s.lstrip("#").strip() if is_md_title else s
            heading = _clean(heading)
            if is_rst_title and heading and title is None and not SKIP_TITLES.match(heading):
                title = heading
                i += 2
                continue
            if is_md_title and title is None and heading and not SKIP_TITLES.match(heading):
                title = heading
            elif paras:  # stop at the first section heading after some prose
                break
            i += 1 + (1 if is_rst_title else 0)
            continue
        if not s:
            if cur:
                paras.append(" ".join(cur))
                cur = []
        elif ln.startswith(("    ", "\t")) or s.startswith((">>>", "...", "$ ")) or _BADGE.search(s) or re.search(r'\b(href|src|style|width|height|alt)=["\']|^/?>$', s) or re.fullmatch(r"(\[[^\]]*\]\([^)]*\)\s*[|·•-]?\s*)+", s) or s.startswith(("|", ">", "```", "- [", "* [")):
            pass
        else:
            cur.append(re.sub(r"<[^>]+>", "", s))
        if len(paras) >= 3:
            break
        i += 1
    if cur and len(paras) < 3:
        paras.append(" ".join(cur))
    noise = re.compile(r"(?i)sponsor|backers|this functionality is in beta|^documentation: https?://|[{}]")
    paras = [p for p in (_clean(p) for p in paras) if len(p) > 30 and not noise.search(p)][:2]
    return {"title": title or None, "intro": paras}


def about(store: Store) -> dict:
    """{name, description, homepage, license, readme: {path, title, intro}, languages, folders, files}."""
    head = store.get_meta("head") or ""
    key = (store.repo_id, head)
    if key in _CACHE:
        return _CACHE[key]
    from afd_oracle_ingest.git.walker import open_repo, tree_files
    try:
        _, path = resolve_repo(store.repo_id, get_settings())
        repo = open_repo(path)
        files = tree_files(repo, head) if head else {}
    except Exception:
        return {}
    out: dict = _manifest(files, repo)
    readme = next((n for n in README_NAMES if n in files), None)
    if readme is None:
        readme = next((p for p in files if "/" not in p and p.lower().startswith("readme")), None)
    if readme:
        text = _blob_text(repo, files[readme]) or ""
        rd = _readme(text, readme.lower().endswith(".rst"))
        rd["title"] = rd["title"] or out.get("name") or store.repo_id
        out["readme"] = {"path": readme, **rd}
    langs: Counter = Counter()
    for p in files:
        ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
        lang = EXT_LANG.get(ext)
        if lang and lang not in DOC_LIKE:
            langs[lang] += 1
    total = sum(langs.values()) or 1
    out["languages"] = [{"name": n, "files": c, "share": round(c / total, 3)} for n, c in langs.most_common(6)]
    tops: Counter = Counter(p.split("/", 1)[0] for p in files if "/" in p and not p.startswith("."))
    out["folders"] = [{"name": n, "files": c} for n, c in tops.most_common(8)]
    out["files"] = len(files)
    _CACHE[key] = out
    return out
