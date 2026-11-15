"""Catalogue family: dependencies and build (pins, CI gates, base images, generated/vendored code, version literals,
build flags, cache keys).

Dependency specifiers are parsed from the ``diff_lines`` rows of manifest files (``DEP_MANIFEST_RX``: requirements,
pyproject, package.json, go.mod, Cargo.toml, Gemfile ...) with a small per-line parser; lockfiles are only consulted
where a builder says so. Merge and bulk commits are not in ``diff_lines`` (so bulk re-vendors are measured from blobs).
"""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass

from .base import (DAY, DEP_MANIFEST_RX, LOCKFILE_RX, REVERT_RX, Ctx, Hit, candidate, is_ci, is_code, is_docs,
                   is_generated, is_test, is_vendored, subject_of)


# ====================================================================================== shared helpers
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _msg(ctx: Ctx, sha: str) -> str:
    c = ctx.by_hash.get(sha)
    return (c["message"] or "") if c else ""


def _rows_for_paths(ctx: Ctx, paths: list[str], sign: str | None = None) -> list:
    """diff_lines rows of exactly these paths (uses the path index), ordered by seq, path, line."""
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


def _cached(ctx: Ctx, key: str, fn):
    cache = ctx.__dict__.setdefault("_depbuild_cache", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _head_has(ctx: Ctx, path: str, text: str) -> bool:
    lines = _cached(ctx, "headlines", dict)
    if path not in lines:
        head = ctx.text_at_head(path)
        lines[path] = frozenset(ln.strip() for ln in head.splitlines()) if head else frozenset()
    return text.strip() in lines[path]


# ---------------------------------------------------------------------------- version parsing
_VER_RX = re.compile(r"(\d+(?:\.\d+)*)(?:[.\-_]?(a|b|c|rc|alpha|beta|dev|pre|preview)\d*)?", re.I)


def _ver(spec: str) -> tuple[tuple[int, ...], bool] | None:
    """First version number in a specifier -> (int tuple, is_prerelease)."""
    m = _VER_RX.search(spec or "")
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split(".")), bool(m.group(2))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


# ---------------------------------------------------------------------------- manifest line parsing
_REQ_FILE_RX = re.compile(r"(?i)(^|/)(requirements[^/]*\.(txt|in)|constraints[^/]*\.txt)$")
_REQ_RX = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*((?:[<>=!~]=?=?|===)[^;#]*)?(?:;.*)?(?:#.*)?$")
_QUOTED_RX = re.compile(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*((?:[<>=!~]=?=?|===)[^"';]*)["']""")
_POETRY_RX = re.compile(r"""^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*["']([\^~<>=!*\d][^"']*)["']""")
_CARGO_RX = re.compile(r"""^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*\{[^}]*\bversion\s*=\s*"([^"]+)\"""")
_NPM_RX = re.compile(r'''^\s*"(@?[A-Za-z0-9][\w./-]*)"\s*:\s*"((?:[~^<>=]|>=|<=)?\s*v?\d[^"]*|\*|latest|[~^]?\d.*)"\s*,?\s*$''')
_GO_RX = re.compile(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-]+/[\w./\-]+)\s+(v\d[^\s]*)")
_GEM_RX = re.compile(r"""^\s*gem\s+["']([\w.\-]+)["']((?:\s*,\s*["'][^"']+["'])*)""")
_NOT_DEPS = {"version", "name", "python", "edition", "rust-version", "license", "authors", "description", "readme",
             "requires-python", "node", "npm", "yarn", "pnpm", "main", "module", "types", "homepage", "keywords",
             "classifiers", "channel", "target-version", "line-length", "ruby", "engines", "target", "python-version"}


def _parse_dep(path: str, text: str) -> tuple[str, str] | None:
    """One manifest line -> (normalised package, specifier text) or None when it is not a dependency line."""
    s = text.strip()
    if not s or s.startswith(("#", "//", "-", "[", "{", "}", "]", ")")):
        return None
    base = path.rsplit("/", 1)[-1].lower()
    m = None
    if _REQ_FILE_RX.search(path):
        m = _REQ_RX.match(text)
        if m and not re.match(r"^[A-Za-z0-9]", s):
            m = None
        if m:
            nm = _norm(m.group(1))
            return (nm, (m.group(2) or "").strip()) if nm not in _NOT_DEPS else None
        return None
    if base == "go.mod":
        m = _GO_RX.match(text)
        return (_norm(m.group(1)), m.group(2)) if m else None
    if base == "gemfile":
        m = _GEM_RX.match(text)
        return (_norm(m.group(1)), re.sub(r"""["'\s]""", "", m.group(2) or "")) if m else None
    if base == "package.json":
        m = _NPM_RX.match(text)
        if m and m.group(1).lower() not in _NOT_DEPS:
            return _norm(m.group(1)), m.group(2).strip()
        return None
    if base == "cargo.toml":
        m = _CARGO_RX.match(text) or _POETRY_RX.match(text)
        if m and m.group(1).lower() not in _NOT_DEPS:
            return _norm(m.group(1)), m.group(2)
        return None
    # pyproject / setup.py / setup.cfg / Pipfile / pom / gradle: quoted PEP 508 strings or poetry-style tables
    m = _QUOTED_RX.search(text)
    if m and m.group(1).lower() not in _NOT_DEPS:
        return _norm(m.group(1)), m.group(2).strip()
    m = _POETRY_RX.match(text)
    if m and m.group(1).lower() not in _NOT_DEPS and base in ("pyproject.toml", "pipfile"):
        return _norm(m.group(1)), m.group(2)
    return None


@dataclass
class Ev:
    hash: str
    seq: int
    ts: int
    path: str
    name: str
    kind: str          # add | remove | change
    old: str
    new: str
    old_text: str
    new_text: str


def _manifest_paths(ctx: Ctx) -> list[str]:
    return [p for p in ctx.changes_by_path
            if DEP_MANIFEST_RX.search(p) and not is_test(p) and not is_vendored(p)]


def _manifest_rows(ctx: Ctx) -> list:
    return _cached(ctx, "manifest_rows", lambda: _rows_for_paths(ctx, _manifest_paths(ctx)))


def _dep_events(ctx: Ctx) -> list[Ev]:
    def build() -> list[Ev]:
        per: dict[tuple[str, str], dict[str, dict[str, tuple[str, str]]]] = {}
        meta: dict[tuple[str, str], tuple[int, int]] = {}
        for r in _manifest_rows(ctx):
            p = _parse_dep(r["path"], r["text"])
            if not p:
                continue
            key = (r["hash"], r["path"])
            per.setdefault(key, {"+": {}, "-": {}})[r["sign"]][p[0]] = (p[1], r["text"].strip())
            meta[key] = (r["seq"], r["ts"])
        evs: list[Ev] = []
        for (h, path), d in per.items():
            for name in set(d["+"]) | set(d["-"]):
                a, b = d["+"].get(name), d["-"].get(name)
                kind = "change" if a and b else "add" if a else "remove"
                evs.append(Ev(h, meta[(h, path)][0], meta[(h, path)][1], path, name, kind,
                              b[0] if b else "", a[0] if a else "", b[1] if b else "", a[1] if a else ""))
        evs.sort(key=lambda e: (e.seq, e.path, e.name))
        return evs
    return _cached(ctx, "dep_events", build)


_PY_ALIASES = {"yaml": "pyyaml", "pil": "pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4",
               "sklearn": "scikit-learn", "dateutil": "python-dateutil", "jwt": "pyjwt", "git": "gitpython",
               "dotenv": "python-dotenv", "attr": "attrs", "serial": "pyserial", "magic": "python-magic"}


def _pkg_candidates(mod: str) -> set[str]:
    n = _norm(mod)
    out = {n, "python-" + n, "py" + n, n + "-python"}
    if n in _PY_ALIASES:
        out.add(_PY_ALIASES[n])
    return out


_IMPORT_PY = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)")
_IMPORT_JS = re.compile(r"""(?:from\s*|require\(\s*|import\s*)["'](@?[\w][\w.\-]*(?:/[\w.\-]+)?)["']""")


def _import_index(ctx: Ctx) -> dict[str, list[tuple[int, int, str]]]:
    """module root (normalised) -> [(ts, seq, path)] where an import of it was added in non-test code."""
    def build():
        out: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        rx = r"^\s*(from|import)\s+[A-Za-z_@'\"]|require\(|\bfrom\s+['\"]"
        for r in ctx.grep(rx, "+", limit=200000):
            p = r["path"]
            if not is_code(p) or is_test(p):
                continue
            m = _IMPORT_PY.match(r["text"]) if p.endswith(".py") else _IMPORT_JS.search(r["text"])
            if m:
                out[_norm(m.group(1))].append((r["ts"], r["seq"], p))
        return out
    return _cached(ctx, "imports", build)


def _imports_of_pkg(ctx: Ctx, pkg: str) -> list[tuple[int, int, str]]:
    idx = _import_index(ctx)
    out: list[tuple[int, int, str]] = []
    for mod, rows in idx.items():
        if pkg in _pkg_candidates(mod) or mod == pkg:
            out.extend(rows)
    return out


_BOTISH = ("bump", "update dependency", "update dependencies", "dependabot", "renovate", "pre-commit autoupdate")


def _bot_commit(ctx: Ctx, sha: str) -> bool:
    c = ctx.by_hash.get(sha)
    return bool(c) and ctx.is_bot(c)


def _fmt_age(days: float) -> int:
    return int(days // 30)


# ====================================================================================== pinned_dependency_with_reason
PIN_REASON_RX = re.compile(r"(?i)\b(pin(ned|ning)?|cap(ped|ping)?|lock(ed)?|downgrade[sd]?|break(s|ing|age)?|broke(n)?|"
                           r"incompatib\w+|workaround|cve-\d+|vulnerab\w+|regression|compat(ibility)?)\b")
PIN_OP_RX = re.compile(r"(==|~=|!=|<=?(?!=))")
OVERRIDE_RX = re.compile(r'(?i)^\s*("(resolutions|overrides)"\s*:|replace\s|\[patch\b|\[tool\.uv\.override|override-dependencies|'
                         r'constraint-dependencies|\[tool\.uv\.sources\])')
LOCAL_REPLACE_RX = re.compile(r"=>\s*\.{1,2}/|path\s*=|file:|\.\./")


def _is_pin(path: str, spec: str, old: str) -> str | None:
    s = spec.strip()
    if not s:
        return None
    if path.lower().endswith("package.json"):
        if re.match(r"^v?\d+\.\d+\.\d+", s) and not re.match(r"^[~^]", s):
            return "exact"
        if re.search(r"<", s):
            return "upper_cap"
        return None
    if path.lower().endswith("go.mod"):
        return None
    if "==" in s and "!=" not in s.replace("==", ""):
        return "exact"
    if re.search(r"(?<![<>=!~])<=?(?!=)", s):
        return "upper_cap"
    if "~=" in s:
        return "compatible_release"
    if "!=" in s:
        return "exclusion"
    return None


@candidate("pinned_dependency_with_reason")
def pinned_dependency_with_reason(ctx: Ctx):
    """A dependency was capped/pinned (``==``, ``<``, ``~=``, ``!=``, overrides/resolutions/replace) with a stated
    reason in the commit message or an adjacent comment, or a range was tightened to a pin by a fix commit; still
    present at HEAD. Rank by age (>12 months), reason strength and how many files import the package (proxy for
    callers: files that ever added an import of it)."""
    rows = _manifest_rows(ctx)
    by_cp: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_cp[(r["hash"], r["path"])].append(r)
    seen: set[str] = set()
    cands: list[tuple] = []

    for e in _dep_events(ctx):
        if e.kind == "remove":
            continue
        pin = _is_pin(e.path, e.new, e.old)
        if not pin:
            continue
        if e.kind == "change" and _is_pin(e.path, e.old, "") == pin and e.old == e.new:
            continue
        cands.append((e, pin, e.new_text, e.name))
    for (h, path), rs in by_cp.items():
        for r in rs:
            if r["sign"] == "+" and OVERRIDE_RX.search(r["text"]):
                ev = Ev(h, r["seq"], r["ts"], path, r["text"].strip()[:60], "add", "", r["text"].strip(), "",
                        r["text"].strip())
                cands.append((ev, "override", r["text"].strip(), r["text"].strip()[:60]))

    for e, pin, line_text, name in cands:
        key = f"{e.hash}|{e.path}|{name}"
        if key in seen:
            continue
        seen.add(key)
        c = ctx.by_hash[e.hash]
        if ctx.is_bot(c):
            ctx.drop("bot_bump")
            continue
        if LOCAL_REPLACE_RX.search(line_text):
            ctx.drop("local_replace_path")
            continue
        msg = c["message"] or ""
        comment = ""
        for r in by_cp.get((e.hash, e.path), []):
            if r["sign"] == "+" and r["text"].strip().startswith(("#", "//")) and abs(r["line"] - _line_of(by_cp, e)) <= 3:
                comment += " " + r["text"].strip()
        if "#" in line_text:
            comment += " " + line_text.split("#", 1)[1]
        reason = PIN_REASON_RX.search(msg) or PIN_REASON_RX.search(comment)
        tightened = e.kind == "change" and pin == "exact" and _is_pin(e.path, e.old, "") is None and ctx.is_fix(c)
        if not reason and not tightened:
            ctx.drop("no_stated_reason")
            continue
        if not _head_has(ctx, e.path, line_text):
            ctx.drop("pin_gone_at_head")
            continue
        age_m = _fmt_age(ctx.head_ts - e.ts)
        users = {p for _, _, p in _imports_of_pkg(ctx, e.name)} if pin != "override" else set()
        score = 1 + (2 if age_m >= 12 else 0) + min(age_m, 60) / 30 + min(len(users), 20) * 0.3 \
            + (1.5 if reason and reason.group(0).lower().startswith(("cve", "vulner", "break", "incompat")) else 0)
        yield Hit(
            subject=f"{e.path}|{name}|{e.hash[:12]}",
            score=score,
            title=f"{name} {pin} in {e.path}: {subject_of(msg)[:80]}",
            evidence={"package": name, "spec": e.new or line_text, "previous_spec": e.old, "kind": pin,
                      "reason": (reason.group(0) if reason else "range tightened by a fix commit"),
                      "comment": comment.strip()[:160], "age_months": age_m, "importing_files": len(users)},
            commits=[e.hash], diffs=[(e.hash, e.path)], paths=[e.path],
        )


def _line_of(by_cp: dict, e: Ev) -> int:
    for r in by_cp.get((e.hash, e.path), []):
        if r["sign"] == "+" and r["text"].strip() == (e.new_text or "").strip():
            return r["line"]
    return -999


# ====================================================================================== ci_gate_weakening
_GATE_RX = re.compile(r"(?i)(continue-on-error\s*:\s*(true|\$\{\{)|allow_failure\s*:\s*(true|\{)|(^|\s)if\s*:\s*(false|0|'false'|\"false\")\s*(#.*)?$|"
                      r"\|\|\s*true\b|--cov-fail-under|fail[_-]under|\bignore-?errors?\b|--exit-zero|\bcontinue_on_error\b|"
                      r"\bskip[_-]?(tests?|lint)\b\s*[:=]\s*true)")
_STEP_RX = re.compile(r"(?i)(test|pytest|tox|lint|flake8|ruff|pylint|eslint|mypy|pyright|type-?check|tsc|scan|codeql|"
                      r"audit|bandit|trivy|snyk|safety|coverage|cov\b|codecov|sonar|check)")
_WEAKEN_MSG = re.compile(r"(?i)\b(skip|unblock|temporar\w+|flak\w+|disable[sd]?|ignore[sd]?|silence|allow fail\w*|"
                         r"workaround|for now|lower(ed)?|relax\w*)\b")
_FAIL_UNDER = re.compile(r"fail[_-]under\s*[=:\s]\s*(\d+(?:\.\d+)?)", re.I)
_NIGHTLY = re.compile(r"(?i)(nightly|experimental|canary|allowed[-_ ]failures?|prerelease|pre-release|\bdev\b|"
                      r"\bnext\b|unstable|continue-on-error:\s*\$\{\{\s*matrix)")


def _step_block(text: str, lineno: int) -> str:
    lines = text.splitlines()
    i = min(max(lineno - 1, 0), len(lines) - 1)
    if i < 0:
        return ""
    cur = len(lines[i]) - len(lines[i].lstrip())
    s = i
    while s > 0:
        ln = lines[s]
        ind = len(ln) - len(ln.lstrip())
        if ln.lstrip().startswith("- ") and ind <= cur:
            break
        if ln.strip() and ind < cur - 6:
            break
        s -= 1
    e = i + 1
    start_ind = len(lines[s]) - len(lines[s].lstrip())
    while e < len(lines):
        ln = lines[e]
        ind = len(ln) - len(ln.lstrip())
        if ln.strip() and ind <= start_ind and (ln.lstrip().startswith("- ") or ind < start_ind):
            break
        e += 1
    return "\n".join(lines[max(s - 1, 0):e])


@candidate("ci_gate_weakening")
def ci_gate_weakening(ctx: Ctx):
    """CI step made non-blocking (continue-on-error / allow_failure / ``if: false`` / ``|| true`` / ``--exit-zero``) or
    a coverage ``fail_under`` lowered, in a step that runs tests, lint, type checks, scans or coverage. Rank by
    message wording, not-restored-at-HEAD and repeated weakening of the same file."""
    paths = [p for p in ctx.changes_by_path if is_ci(p) or re.search(r"(?i)(setup\.cfg|pyproject\.toml|\.coveragerc|codecov\.ya?ml|\.codecov)$", p)]
    rows = _rows_for_paths(ctx, paths)
    by_cp: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_cp[(r["hash"], r["path"])].append(r)
    found: list[dict] = []
    for (h, path), rs in by_cp.items():
        c = ctx.by_hash[h]
        status = next((fc["status"] for fc in ctx.changes(h) if fc["path"] == path), "M")
        olds = [float(m.group(1)) for r in rs if r["sign"] == "-" for m in [_FAIL_UNDER.search(r["text"])] if m]
        for r in rs:
            if r["sign"] != "+":
                continue
            t = r["text"]
            m = _GATE_RX.search(t)
            if not m or t.strip().startswith("#"):
                continue
            marker = m.group(0).strip().lower()
            if "fail" in marker and "under" in marker.replace("_", "").replace("-", ""):
                fm = _FAIL_UNDER.search(t)
                new = float(fm.group(1)) if fm else None
                if new is None or not olds or new >= min(olds):
                    continue
                marker = f"fail_under {min(olds):g}->{new:g}"
            text_c = ctx.text_at(h, path) or ""
            block = _step_block(text_c, r["line"]) if is_ci(path) and path.endswith((".yml", ".yaml")) else t
            if "fail_under" not in marker and not _STEP_RX.search(block + " " + path):
                ctx.drop("not_a_quality_step")
                continue
            if status == "A":
                ctx.drop("initial_scaffolding")
                continue
            if _NIGHTLY.search(block) or _NIGHTLY.search(path):
                ctx.drop("nightly_or_experimental")
                continue
            if re.search(r"(?i)github\.event\.pull_request\.head\.repo\.fork|fork\b", block):
                ctx.drop("fork_only")
                continue
            if ctx.is_bot(c):
                ctx.drop("bot_commit")
                continue
            found.append({"sha": h, "path": path, "marker": marker, "line": t.strip(), "ts": r["ts"],
                          "msg": c["message"] or "", "step": block[:200]})
    per_file = defaultdict(int)
    for f in found:
        per_file[(f["path"], re.sub(r"\d+(\.\d+)?", "N", f["marker"]))] += 1
    seen: set[str] = set()
    for f in found:
        subj = f"{f['sha']}|{f['path']}|{f['marker']}"
        if subj in seen:
            continue
        seen.add(subj)
        restored = not _head_has(ctx, f["path"], f["line"]) and "fail_under" not in f["marker"]
        reps = per_file[(f["path"], re.sub(r"\d+(\.\d+)?", "N", f["marker"]))]
        wm = _WEAKEN_MSG.search(f["msg"])
        score = 1 + (2 if wm else 0) + (0 if restored else 3) + min(reps, 5) + (1 if "fail_under" in f["marker"] else 0)
        yield Hit(
            subject=subj, score=score,
            title=f"{f['marker']} added in {f['path']}: {subject_of(f['msg'])[:80]}",
            evidence={"marker": f["marker"], "line": f["line"][:160], "step": f["step"], "message_hint": wm.group(0) if wm else "",
                      "restored_or_removed_since": restored, "weakenings_of_this_gate_in_file": reps},
            commits=[f["sha"]], diffs=[(f["sha"], f["path"])], paths=[f["path"]],
        )


# ====================================================================================== dockerfile_base_image_aftermath
DOCKERFILE_RX = re.compile(r"(?i)(^|/)(Dockerfile|Containerfile)[^/]*$|\.dockerfile$")
FROM_RX = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I)
_BUILDFIX_MSG = re.compile(r"(?i)\b(docker|image|build|libc|musl|glibc|ssl|openssl|cert(ificate)?s?|wheel|apk|apt|package|"
                           r"install|dependenc\w+|segfault|alpine|debian|ubuntu|slim|locale|timezone|runtime)\b")


def _family(image: str) -> str:
    s = image.lower()
    for fam, rx in (("alpine", r"alpine"), ("distroless", r"distroless"), ("ubuntu", r"ubuntu|focal|jammy|noble|bionic"),
                    ("rhel", r"centos|rhel|rocky|almalinux|ubi\d|fedora|amazonlinux"),
                    ("debian", r"debian|bookworm|bullseye|buster|stretch|slim")):
        if re.search(rx, s):
            return fam
    return "default"


def _image_major(image: str) -> tuple[str, int | None]:
    name, _, tag = image.partition(":")
    m = re.match(r"v?(\d+)", tag)
    return name, int(m.group(1)) if m else None


@candidate("dockerfile_base_image_aftermath")
def dockerfile_base_image_aftermath(ctx: Ctx):
    """A Dockerfile ``FROM`` was switched back within 180 days, or the distro family / major version changed and >=2
    build/runtime/package fix commits followed within 30 days. (COPY-before-install secondary signal not computed.)
    Unchanged by the symbol/blame enrichment on purpose: ``line_origins`` only blames lines of fixes to files that
    hold symbols, so Dockerfile lines have no blame, and fix attribution stays message-based."""
    paths = [p for p in ctx.changes_by_path if DOCKERFILE_RX.search(p)]
    per: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in _rows_for_paths(ctx, paths):
        m = FROM_RX.match(r["text"])
        if not m:
            continue
        d = per[r["path"]].setdefault(r["hash"], {"+": [], "-": [], "seq": r["seq"], "ts": r["ts"]})
        d[r["sign"]].append(m.group(1))
    fix_ts = [c["ts"] for c in ctx.commits if ctx.is_fix(c) and _BUILDFIX_MSG.search(c["message"] or "")]
    fix_hash = [c["hash"] for c in ctx.commits if ctx.is_fix(c) and _BUILDFIX_MSG.search(c["message"] or "")]
    for path, commits in per.items():
        if is_test(path) or re.search(r"(?i)test", path.rsplit("/", 1)[-1]):
            ctx.drop("test_dockerfile")
            continue
        evs = sorted(((v["seq"], h, v) for h, v in commits.items()), key=lambda x: x[0])
        changes = []
        for seq, h, v in evs:
            old, new = v["-"], v["+"]
            if not old or not new or set(old) == set(new):
                continue
            changes.append({"seq": seq, "hash": h, "ts": v["ts"], "old": old[-1], "new": new[-1]})
        for i, ch in enumerate(changes):
            if _bot_commit(ctx, ch["hash"]):
                ctx.drop("bot_bump")
                continue
            back = next((j for j in changes[i + 1:] if j["ts"] - ch["ts"] <= 180 * DAY and j["new"] == ch["old"]
                         and j["old"] == ch["new"]), None)
            fam_changed = _family(ch["old"]) != _family(ch["new"])
            on, omaj = _image_major(ch["old"])
            nn, nmaj = _image_major(ch["new"])
            major = on == nn and omaj is not None and nmaj is not None and omaj != nmaj
            lo = bisect.bisect_left(fix_ts, ch["ts"] + 1)
            hi = bisect.bisect_right(fix_ts, ch["ts"] + 30 * DAY)
            fixes = fix_hash[lo:hi]
            if not back and not ((fam_changed or major) and len(fixes) >= 2):
                continue
            yield Hit(
                subject=f"{path}|{ch['hash'][:12]}",
                score=1 + (4 if back else 0) + len(fixes) * 0.7 + (1 if fam_changed else 0) + (1 if major else 0),
                title=f"{path}: FROM {ch['old']} -> {ch['new']}" + (f", switched back in {back['hash'][:8]}" if back else "")
                + f", {len(fixes)} build/runtime fixes within 30d",
                evidence={"from": ch["old"], "to": ch["new"], "family_change": fam_changed, "major_change": major,
                          "switched_back_by": back["hash"] if back else None,
                          "fixes_within_30d": [{"hash": f, "subject": subject_of(_msg(ctx, f))} for f in fixes[:8]]},
                commits=[ch["hash"], *([back["hash"]] if back else []), *fixes[:5]],
                diffs=[(ch["hash"], path), *([(back["hash"], path)] if back else [])], paths=[path],
            )


# ====================================================================================== dependency_downgrade_or_revert
@candidate("dependency_downgrade_or_revert")
def dependency_downgrade_or_revert(ctx: Ctx):
    """A direct manifest dependency bumped A->B and then lowered back to <=A within 120 days, or a revert commit that
    lowers a manifest version. Pre-releases, range-only widenings (same first version), bot-only churn and lockfile
    transitives are excluded. Majors weigh more."""
    by_key: dict[tuple[str, str], list[Ev]] = defaultdict(list)
    for e in _dep_events(ctx):
        if e.kind == "change":
            by_key[(e.path, e.name)].append(e)
    for (path, name), evs in by_key.items():
        for i, e in enumerate(evs):
            ov, nv = _ver(e.old), _ver(e.new)
            if not ov or not nv:
                continue
            if ov[1] or nv[1]:
                ctx.drop("prerelease")
                continue
            if _cmp(ov[0], nv[0]) == 0:
                ctx.drop("range_change_same_version")
                continue
            is_rev = bool(REVERT_RX.search(subject_of(_msg(ctx, e.hash)))) and _cmp(nv[0], ov[0]) < 0
            down = None
            if _cmp(nv[0], ov[0]) > 0:
                for j in evs[i + 1:]:
                    if j.ts - e.ts > 120 * DAY:
                        break
                    jn = _ver(j.new)
                    if jn and not jn[1] and _cmp(jn[0], ov[0]) <= 0:
                        down = j
                        break
            elif is_rev:
                down = e
            if down is None:
                continue
            bump = e if down is not e else None
            if _bot_commit(ctx, down.hash) and (bump is None or _bot_commit(ctx, bump.hash)):
                ctx.drop("bot_only")
                continue
            if down is e and not is_rev:
                continue
            major = (ov[0][0] != nv[0][0]) if down is not e else False
            hi = max(ov[0], nv[0])
            dn = _ver(down.new)
            major = major or (dn is not None and dn[0][0] < hi[0])
            yield Hit(
                subject=f"{path}|{name}|{down.hash[:12]}",
                score=2 + (3 if major else 0) + (2 if is_rev else 0) + (1 if bump and down.ts - bump.ts < 30 * DAY else 0),
                title=f"{name}: {e.old} -> {e.new}" + (f" then {down.new} in {down.hash[:8]}" if bump else " reverted") + f" ({path})",
                evidence={"package": name, "bumped_from": e.old, "bumped_to": e.new, "lowered_to": down.new,
                          "days_between": round((down.ts - e.ts) / DAY, 1) if bump else 0, "major": major,
                          "revert_message": is_rev, "downgrade_message": subject_of(_msg(ctx, down.hash))},
                commits=[x for x in [e.hash if bump else None, down.hash] if x],
                diffs=[(e.hash, path)] + ([(down.hash, path)] if bump else []), paths=[path],
            )


# ====================================================================================== generated_code_hand_edited
_REGEN_MSG = re.compile(r"(?i)^(bump|update|upgrade|pin|regenerate|recompile)\b|\b(update|bump|upgrade)\s+(the\s+)?(dev\s+|python-)?(requirements?|dependenc\w*|deps|packages?|lock\w*)\b|pip-compile|pre-commit autoupdate|\bregenerat\w*")
GEN_HEADER_RX = r"(?i)(do not edit|generated by|@generated|auto-?generated|code generated|autogenerated|this file is generated)"
GEN_NAME_RX = re.compile(r"(?i)(_pb2(_grpc)?\.pyi?|\.pb\.go|\.pb\.cc|\.pb\.h|\.generated\.\w+|_generated\.\w+|\.g\.dart|"
                         r"_gen\.go|\.gen\.\w+|\.designer\.cs)$")
GEN_STRIP = re.compile(r"(?i)(_pb2_grpc|_pb2|\.pb|\.generated|_generated|\.g|_gen|\.gen|\.designer)$")
INPUT_EXT = {".proto", ".thrift", ".graphql", ".gql", ".yaml", ".yml", ".json", ".xml", ".avsc", ".sql", ".in", ".tmpl",
             ".j2", ".mako", ".idl", ".fbs", ".y", ".l", ".xsd", ".wsdl", ".toml", ".jinja", ".template", ".capnp"}
MANUAL_MSG = re.compile(r"(?i)\b(fix|manual(ly)?|patch|hand|hack|tweak|workaround)\b")


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return GEN_STRIP.sub("", base).lower()


@candidate("generated_code_hand_edited")
def generated_code_hand_edited(ctx: Ctx):
    """A generated file (header regex or generated filename) was edited by a non-regenerating commit: none of its
    inputs (paths named in the header, or same-stem sibling files with input extensions) changed, and the diff is
    small (<40 lines) or the message says fix/manual/patch. Where inputs cannot be resolved the input set is empty
    (proxy). Ranks higher when a later regeneration commit removed lines the manual edit added."""
    gen_paths: dict[str, str] = {}
    for r in ctx.grep(GEN_HEADER_RX, "+", limit=50000):
        if r["line"] <= 15 and not is_test(r["path"]) and not is_vendored(r["path"]):
            gen_paths.setdefault(r["path"], r["text"])
    for p in ctx.changes_by_path:
        if GEN_NAME_RX.search(p) and not is_vendored(p):
            gen_paths.setdefault(p, "")
    stems: dict[str, list[str]] = defaultdict(list)
    for p in ctx.head_files:
        stems[_stem(p)].append(p)
    for path, header in sorted(gen_paths.items()):
        if re.search(r"(?i)(\.lock$|-lock\.|__snapshots__|\.snap$|\.min\.|package-lock)", path):
            ctx.drop("lockfile_or_snapshot")
            continue
        inputs: set[str] = set()
        head_txt = ctx.text_at_head(path)
        if head_txt:
            header = "\n".join(head_txt.splitlines()[:15])
            if not re.search(GEN_HEADER_RX, header) and not GEN_NAME_RX.search(path):
                ctx.drop("not_generated_at_head")  # a marker that once appeared in history; not a generated file now
                continue
        for m in re.finditer(r"[\w./\-]+\.\w{1,8}", header or ""):
            for p2 in ctx.head_files:
                if p2.endswith(m.group(0)) and p2 != path:
                    inputs.add(p2)
        for p2 in stems.get(_stem(path), []):
            if p2 != path and "." + p2.rsplit(".", 1)[-1].lower() in INPUT_EXT:
                inputs.add(p2)
        chs = ctx.changes_by_path.get(path, [])
        manual: list = []
        for fc in chs[1:]:
            h = fc["hash"]
            c = ctx.by_hash.get(h)
            if c is None or c["is_bulk"] or c["is_merge"] or fc["status"] in ("A", "D"):
                continue
            touched = {x["path"] for x in ctx.changes(h)}
            if touched & inputs:
                ctx.drop("pure_regeneration")
                continue
            size = (fc["added"] or 0) + (fc["deleted"] or 0)
            msg = c["message"] or ""
            if ctx.is_bot(c) or _REGEN_MSG.search(subject_of(msg)):
                ctx.drop("dependency_update_regeneration")  # pip-compile / lock regeneration, not a hand edit
                continue
            if size >= 40 and not MANUAL_MSG.search(subject_of(msg)):
                ctx.drop("large_diff_looks_regenerated")
                continue
            if len(touched) > 25:
                ctx.drop("bulk_touch")
                continue
            manual.append((fc, c))
        for fc, c in manual:
            added, _ = ctx.added_removed(c["hash"], path)
            mine = {ln.strip() for ln in added if len(ln.strip()) > 10}
            lost = 0
            lost_by = None
            for later in chs:
                lc = ctx.by_hash.get(later["hash"])
                if lc is None or lc["seq"] <= c["seq"] or not mine:
                    continue
                before, after = ctx.text_before(later["hash"], path), ctx.text_at(later["hash"], path)
                if before is None or after is None:
                    continue
                gone = (mine & {ln.strip() for ln in before.splitlines()}) - {ln.strip() for ln in after.splitlines()}
                if gone:
                    lost, lost_by = len(gone), later["hash"]
                    break
            yield Hit(
                subject=f"{c['hash']}|{path}",
                score=1 + (4 if lost else 0) + (1 if MANUAL_MSG.search(subject_of(c["message"])) else 0)
                + (1 if inputs else 0),
                title=f"hand edit to generated {path}: {subject_of(c['message'])[:80]}",
                evidence={"header": (header or "").strip()[:120], "inputs": sorted(inputs)[:6], "diff_size": (fc["added"] or 0) + (fc["deleted"] or 0),
                          "manual_lines_lost_in_regeneration": lost, "regenerated_by": lost_by},
                commits=[c["hash"], *([lost_by] if lost_by else [])],
                diffs=[(c["hash"], path), *([(lost_by, path)] if lost_by else [])], paths=[path, *sorted(inputs)[:3]],
            )


# ====================================================================================== vendored_copy_local_patch
VENDOR_DIR_RX = re.compile(r"(?i)^((?:[^/]+/)*?(?:vendor|vendored|third_party|third-party|thirdparty|ext|deps|extern|external|_vendor)/[^/]+)/")


def _added_lines(ctx: Ctx, sha: str, path: str) -> set[str]:
    """Stripped lines present in ``path`` after ``sha`` but not before (blob based; works for ignored/bulk files)."""
    after, before = ctx.text_at(sha, path), ctx.text_before(sha, path) or ""
    if after is None:
        return set()
    old = {x.strip() for x in before.splitlines()}
    return {x.strip() for x in after.splitlines() if len(x.strip()) > 8 and x.strip() not in old}


def _aux_lines(ctx: Ctx, sha: str, path: str, sign: str) -> set[str] | None:
    """Stripped lines (>8 chars) a commit added/removed in an ignored file, from ``diff_lines_aux``; None when the
    aux index has no rows for the file in that commit (not indexed, or only whitespace changed)."""
    idx = _cached(ctx, "aux_vendor", lambda: _aux_vendor_index(ctx))
    d = idx.get((sha, path))
    if d is None:
        return None
    return d[sign]


def _aux_vendor_index(ctx: Ctx) -> dict[tuple[str, str], dict[str, set[str]]]:
    out: dict[tuple[str, str], dict[str, set[str]]] = {}
    for r in ctx.q("SELECT hash, path, sign, text FROM diff_lines_aux WHERE path REGEXP ?", (VENDOR_DIR_RX.pattern,)):
        t = r["text"].strip()
        d = out.setdefault((r["hash"], r["path"]), {"+": set(), "-": set()})
        if len(t) > 8:
            d[r["sign"]].add(t)
    return out


@candidate("vendored_copy_local_patch")
def vendored_copy_local_patch(ctx: Ctx):
    """A vendored library directory (vendor|third_party|ext|deps ...) added in a bulk commit and later patched by >=2
    non-bulk local commits. Ranked by a re-vendor commit that deleted lines earlier local commits had added. Line
    sets come from ``diff_lines_aux`` (vendored files are ignored by core ingestion and bulk commits are not in
    ``diff_lines``, but both are in the aux index); a file the aux index does not hold (or capped) falls back to
    comparing blobs. Manifest-managed vendor dirs and node_modules are excluded."""
    roots: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    # file_changes includes ignored rows (``vendor/**`` is ignored by default, so changes_by_commit would miss it)
    for fc in ctx.q("SELECT hash, path FROM file_changes WHERE path REGEXP ?", (VENDOR_DIR_RX.pattern,)):
        m = VENDOR_DIR_RX.match(fc["path"])
        if m and fc["hash"] in ctx.by_hash and "node_modules" not in fc["path"] and not re.search(r"\.min\.", fc["path"]):
            roots[m.group(1)][fc["hash"]].append(fc["path"])
    go_managed = "vendor/modules.txt" in ctx.head_files
    for root, by_commit in roots.items():
        order = sorted(by_commit, key=lambda h: ctx.by_hash[h]["seq"])
        first = order[0]
        fc0 = ctx.by_hash[first]
        if not (fc0["is_bulk"] or len(by_commit[first]) >= 5):
            ctx.drop("no_bulk_first_commit")
            continue
        if (go_managed and root.startswith("vendor/")) or ctx.head_files.get(f"{root}/modules.txt"):
            ctx.drop("manifest_managed")
            continue
        later = order[1:]
        local = [h for h in later if not ctx.by_hash[h]["is_bulk"] and len(by_commit[h]) <= 6]
        revendor = [h for h in later if ctx.by_hash[h]["is_bulk"] or len(by_commit[h]) >= 10]
        if len(local) < 2:
            ctx.drop("fewer_than_two_local_patches")
            continue
        local = [h for h in local if not REVERT_RX.search(subject_of(_msg(ctx, h))) or True]
        best_lost, best_v, lost_ex = 0, None, []
        for v in revendor:
            vs = ctx.by_hash[v]["seq"]
            lost = 0
            for lh in local:
                if ctx.by_hash[lh]["seq"] > vs:
                    continue
                for p in by_commit[lh]:
                    if p not in by_commit[v]:
                        continue
                    mine = _aux_lines(ctx, lh, p, "+")
                    if mine is None:
                        mine = _added_lines(ctx, lh, p)
                    removed, readded = _aux_lines(ctx, v, p, "-"), _aux_lines(ctx, v, p, "+")
                    if removed is None or readded is None:
                        bf, af = ctx.text_before(v, p), ctx.text_at(v, p)
                        if bf is None:
                            continue
                        removed, readded = {x.strip() for x in bf.splitlines()}, {x.strip() for x in (af or "").splitlines()}
                    if not mine:
                        continue
                    gone = (mine & removed) - readded
                    lost += len(gone)
                    lost_ex.extend(sorted(gone)[:2])
            if lost > best_lost:
                best_lost, best_v = lost, v
        if not local and not revendor:
            continue
        yield Hit(
            subject=root,
            score=len(local) * 1.5 + best_lost * 0.5 + (3 if best_v else 0) + (1 if revendor else 0),
            title=f"vendored {root}: imported in bulk, {len(local)} local patches" + (f", re-vendor {best_v[:8]} dropped {best_lost} patched lines" if best_v else ""),
            evidence={"root": root, "first_commit": first, "local_patch_commits": len(local), "revendor_commits": len(revendor),
                      "lines_lost_in_revendor": best_lost, "examples": lost_ex[:4]},
            commits=[first, *local[:5], *([best_v] if best_v else [])],
            diffs=[(h, by_commit[h][0]) for h in local[:3]], paths=sorted({p for h in local[:5] for p in by_commit[h]})[:8],
        )


# ====================================================================================== version_literal_multifile_sync_lag
_VERSION_CTX = re.compile(r"""(?ix)(?:(?:__version__|\bversion|\bVERSION|app_?version|appVersion|release|\bARG\s+\w*VERSION)\s*["']?\s*[=:]\s*|
                              \bARG\s+\w*VERSION=)["']?(\d+\.\d+(?:\.\d+)?(?:[-.+]?[A-Za-z0-9.]+)?)["']?""")
_NOT_VERSION_LINE = re.compile(r"(?i)(==|>=|<=|~=|\^|depend|require|python_requires|engines|node|rev\s*:|image\s*:|uses\s*:|"
                               r"api_?version|apiVersion|schema|protocol|syntax)")


@candidate("version_literal_multifile_sync_lag")
def version_literal_multifile_sync_lag(ctx: Ctx):
    """A release bumps the project version literal (``__version__``, ``version =``, ``"version"``, ``ARG VERSION``,
    ``appVersion``) in several files; a recurring version-file set (>=2 releases) exists, and one release's bump
    covered only a strict subset, either repaired later by follow-up commits or never completed. Dependency lines,
    lockfiles, tests and docs are excluded."""
    rows = ctx.grep(r"(?i)(version|release)", None, limit=200000)
    per: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    meta: dict[str, tuple[int, int]] = {}
    for r in rows:
        p = r["path"]
        if is_generated(p) or LOCKFILE_RX.search(p) or is_test(p) or is_docs(p) or is_vendored(p):
            continue
        if _NOT_VERSION_LINE.search(r["text"]) and not re.search(r"\bARG\b|__version__", r["text"]):
            continue
        base = p.rsplit("/", 1)[-1].lower()
        if base == "pyproject.toml" and not re.match(r"^\s*version\s*=", r["text"]):
            continue
        if base == "package.json" and not re.match(r'^\s*"version"\s*:', r["text"]):
            continue
        m = _VERSION_CTX.search(r["text"])
        if not m:
            continue
        per[(r["hash"], p)][r["sign"]] = m.group(1)
        meta[r["hash"]] = (r["seq"], r["ts"])
    bumps: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)   # commit -> path -> (old,new)
    for (h, p), d in per.items():
        if "+" in d and "-" in d and d["+"] != d["-"]:
            bumps[h][p] = (d["-"], d["+"])
    by_release: dict[str, list[str]] = defaultdict(list)
    for h in sorted(bumps, key=lambda x: meta[x][0]):
        for new in {n for _, n in bumps[h].values()}:
            by_release[new].append(h)
    file_releases: dict[str, set[str]] = defaultdict(set)
    for new, hs in by_release.items():
        for h in hs:
            for p, (_, n) in bumps[h].items():
                if n == new:
                    file_releases[p].add(new)
    recurring = {p for p, rel in file_releases.items() if len(rel) >= 2}
    if len(recurring) < 2:
        return
    for new, hs in by_release.items():
        first = hs[0]
        covered_first = {p for p, (_, n) in bumps[first].items() if n == new}
        covered_all = {p for h in hs for p, (_, n) in bumps[h].items() if n == new}
        if not (covered_first & recurring):
            continue
        in_scope = {p for p in recurring if ctx.text_at(first, p) is not None}
        missing_first = in_scope - covered_first
        if not missing_first or len(covered_first & recurring) < 1:
            continue
        if len(in_scope) < 2:
            ctx.drop("single_file_set")
            continue
        repaired = covered_all - covered_first
        never = in_scope - covered_all
        ts0 = meta[first][1]
        follow = [h for h in hs[1:] if meta[h][1] - ts0 <= 14 * DAY]
        if not repaired and not never:
            continue
        yield Hit(
            subject=f"{new}|{first[:12]}",
            score=2 + len(never) * 2 + len(repaired) + len(recurring) * 0.3,
            title=f"release {new}: version bumped in {len(covered_first & recurring)}/{len(in_scope)} usual files"
            + (f", {len(never)} never updated" if never else "") + (f", {len(repaired)} fixed later" if repaired else ""),
            evidence={"version": new, "covered_in_release_commit": sorted(covered_first)[:8],
                      "missed_then_repaired": sorted(repaired)[:8], "never_updated": sorted(never)[:8],
                      "recurring_version_files": sorted(recurring)[:10]},
            commits=[first, *follow[:3]], diffs=[(first, p) for p in sorted(covered_first)[:3]],
            paths=sorted(recurring)[:10],
        )


# ====================================================================================== dependency_adopt_then_remove
_DEV_TOOLS = {"pytest", "black", "flake8", "mypy", "ruff", "isort", "pylint", "coverage", "pytest-cov", "tox", "nox",
              "eslint", "prettier", "jest", "mocha", "webpack", "babel", "typescript", "sphinx", "pre-commit", "bandit",
              "nodemon", "ts-node", "husky", "lint-staged", "twine", "wheel", "setuptools", "build", "pip", "poetry",
              "hatchling", "flit", "flit-core", "pytest-mock", "pytest-asyncio", "types-requests", "autopep8", "yapf"}
_WHY_MSG = re.compile(r"(?i)\b(conflict\w*|alpine|licen[sc]e\w*|vulnerab\w*|cve-\d+|incompatib\w+|bloat\w*|slow|memory|"
                      r"unmaintained|deprecated|abandon\w*|replace[sd]?|instead of|drop(ped)?|no longer|remove[sd]?|"
                      r"break\w*|bug\w*|crash\w*|size)\b")


@candidate("dependency_adopt_then_remove")
def dependency_adopt_then_remove(ctx: Ctx):
    """A direct dependency was added and removed within <=180 days / <=200 commits (or cycled >=2 times) with an
    explanatory message on either side, an import of it in between, or a replacement dependency added in the same
    commit. Dev tools, moves between manifests, renames (add+remove in one commit) and bot churn are excluded."""
    per_commit: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"add": set(), "remove": set(), "change": set()})
    for e in _dep_events(ctx):
        per_commit[(e.hash, e.name)][e.kind].add(e.path)
    names: dict[str, list[tuple[int, str, str]]] = defaultdict(list)   # name -> [(seq, 'add'|'remove', hash)]
    added_in: dict[str, list[str]] = defaultdict(list)
    for (h, name), d in per_commit.items():
        seq = ctx.by_hash[h]["seq"]
        if d["add"] and d["remove"] and not (d["add"] - d["remove"]) and not (d["remove"] - d["add"]):
            continue
        if d["add"] and d["remove"]:
            ctx.drop("moved_between_manifests")
            continue
        if d["change"]:
            continue
        if d["add"]:
            names[name].append((seq, "add", h))
            added_in[h].append(name)
        elif d["remove"]:
            names[name].append((seq, "remove", h))
    for name, evs in names.items():
        evs.sort()
        cycles: list[tuple[str, str]] = []
        open_add = None
        for seq, kind, h in evs:
            if kind == "add":
                open_add = h
            elif kind == "remove" and open_add:
                cycles.append((open_add, h))
                open_add = None
        for add_h, rem_h in cycles:
            a, r = ctx.by_hash[add_h], ctx.by_hash[rem_h]
            if name in _DEV_TOOLS:
                ctx.drop("dev_tool")
                continue
            if ctx.is_bot(a) or ctx.is_bot(r):
                ctx.drop("bot_churn")
                continue
            fast = r["ts"] - a["ts"] <= 180 * DAY or r["seq"] - a["seq"] <= 200
            if not (fast or len(cycles) >= 2):
                continue
            between = [(ts, seq, p) for ts, seq, p in _imports_of_pkg(ctx, name) if a["seq"] <= seq <= r["seq"]]
            why = [m.group(0) for h in (add_h, rem_h) for m in _WHY_MSG.finditer(_msg(ctx, h))]
            replacement = [n for n in added_in.get(rem_h, []) if n != name]
            if not why and not between and not replacement:
                ctx.drop("no_explanation_or_usage")
                continue
            yield Hit(
                subject=f"{name}|{add_h[:12]}|{rem_h[:12]}",
                score=2 + 3 * (len(cycles) - 1) + len(set(why)) * 1.5 + (1 if between else 0) + (2 if replacement else 0)
                + (1 if r["ts"] - a["ts"] < 60 * DAY else 0),
                title=f"{name} adopted then removed after {round((r['ts'] - a['ts']) / DAY)}d: {subject_of(r['message'])[:70]}",
                evidence={"package": name, "days_in_use": round((r["ts"] - a["ts"]) / DAY), "cycles": len(cycles),
                          "importing_files_in_between": len({p for _, _, p in between}), "reasons": sorted(set(why))[:6],
                          "replacement": replacement[:3], "add_subject": subject_of(a["message"]),
                          "remove_subject": subject_of(r["message"])},
                commits=[add_h, rem_h],
                diffs=[(add_h, p) for p in sorted(per_commit[(add_h, name)]["add"])[:1]]
                + [(rem_h, p) for p in sorted(per_commit[(rem_h, name)]["remove"])[:1]],
                paths=sorted(per_commit[(add_h, name)]["add"] | per_commit[(rem_h, name)]["remove"]),
            )


# ====================================================================================== monkeypatch_dependency_bump_overlap
_ASSIGN_RX = r"^\s*[A-Za-z_]\w*(\.\w+)+\s*=[^=]"
_ASSIGN_PARSE = re.compile(r"^\s*([A-Za-z_]\w*)((?:\.\w+)+)\s*=(?!=)")
_PATCH_SKIP = {"self", "cls", "os", "sys", "app", "config", "settings", "args", "ns", "options", "opts", "request", "g",
               "this", "module", "exports", "window", "document", "process", "np", "state", "result", "data", "ctx"}
_PATCH_DOC = re.compile(r"(?i)(monkey|patch|workaround|upstream|see https?://|issue|bug|until|hack|work around)")


def _py_alias_table(text: str) -> dict[str, str]:
    import ast
    out: dict[str, str] = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[(a.asname or a.name.split(".")[0])] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                out[a.asname or a.name] = n.module.split(".")[0]
    return out


def _js_alias_table(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"""import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+["'](@?[\w][\w.\-]*(?:/[\w.\-]+)?)["']""", text):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*["'](@?[\w][\w.\-]*(?:/[\w.\-]+)?)["']""", text):
        out[m.group(1)] = m.group(2)
    return out


_LOCK_LINE_RX = (r'^\s*("version"\s*:|version\s*=|version\s+"|[A-Za-z0-9][\w.\-]*==)')
_LOCK_VERSION = re.compile(r'^\s*"?version"?\s*[=:]?\s*"[^"\s]+"')
_LOCK_PIN = re.compile(r"^\s*([A-Za-z0-9][\w.\-]*)==([^\s;#]+)")


def _lock_name(line: str) -> str | None:
    """Package name from the line a lockfile prints directly above ``version`` (poetry/uv/Cargo ``name = "x"``,
    npm ``"node_modules/x": {`` or ``"x": {``, yarn ``"x@^1.0":`` / ``x@^1.0:``)."""
    t = line.strip()
    m = re.match(r'name\s*=\s*"([^"]+)"', t)
    if m:
        return _norm(m.group(1))
    m = re.match(r'"(?:[^"]*node_modules/)?(@?[\w.\-]+(?:/[\w.\-]+)?)"\s*:\s*\{', t)
    if m:
        return _norm(m.group(1))
    m = re.match(r'"?(@?[\w.\-]+(?:/[\w.\-]+)?)@[^"\s,]*"?(?:\s*,.*)?:\s*$', t)
    if m:
        return _norm(m.group(1))
    return None


def _lock_bumps(ctx: Ctx) -> dict[str, list[tuple[int, str]]]:
    """package -> [(seq, hash)] of commits that changed its resolved version in a lockfile. Lockfiles are ignored by
    core ingestion, so this reads ``diff_lines_aux`` (changed lines of every lockfile commit, whole history): a
    ``name==ver`` line replaced in the same commit is a bump outright; for ``version`` lines the commit must both
    remove and add a version line, the package is the name printed on the line above (an added line there means the
    package is new, not bumped, and is skipped). Only that line is looked up in the new blob, for at most the 600 newest
    candidate commits."""
    def build():
        groups: dict[tuple, dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
        for r in ctx.grep_aux(_LOCK_LINE_RX, None, limit=300000):
            if r["hash"] in ctx.by_hash and LOCKFILE_RX.search(r["path"]) and not is_vendored(r["path"]):
                groups[(r["seq"], r["hash"], r["path"])][r["sign"]].append(r)
        out: dict[str, list[tuple[int, str]]] = defaultdict(list)
        todo = []
        for (seq, h, path), g in sorted(groups.items()):
            old = {}
            for r in g["-"]:
                m = _LOCK_PIN.match(r["text"])
                if m:
                    old[_norm(m.group(1))] = m.group(2)
            for r in g["+"]:
                m = _LOCK_PIN.match(r["text"])
                if m and old.get(_norm(m.group(1)), m.group(2)) != m.group(2):
                    out[_norm(m.group(1))].append((seq, h))
            vplus = [r["line"] for r in g["+"] if _LOCK_VERSION.match(r["text"]) and r["line"]]
            if vplus and any(_LOCK_VERSION.match(r["text"]) for r in g["-"]):
                todo.append((seq, h, path, vplus))
        for seq, h, path, vplus in todo[-600:]:
            added = {r["line"] for r in ctx.q("SELECT line FROM diff_lines_aux WHERE hash=? AND path=? AND sign='+'", (h, path))}
            fc = ctx.q1("SELECT new_blob FROM file_changes WHERE hash=? AND path=?", (h, path))
            text = ctx.blob_text(fc["new_blob"], 4_000_000) if fc and fc["new_blob"] else None
            if not text:
                continue
            lines = text.splitlines()
            for ln in vplus:
                if ln - 1 in added or not 1 < ln <= len(lines) + 1:
                    continue
                name = _lock_name(lines[ln - 2])
                if name:
                    out[name].append((seq, h))
        return out
    return _cached(ctx, "lock_bumps", build)


@candidate("monkeypatch_dependency_bump_overlap")
def monkeypatch_dependency_bump_overlap(ctx: Ctx):
    """Attribute assignment on an imported module (``requests.adapters.X = ...``) whose root resolves, through the
    file's import alias table at HEAD, to a manifest package, where the dependency's version changed after the patch's
    last edit: a lockfile commit found in the ``diff_lines_aux`` lockfile line history (whole history; see
    :func:`_lock_bumps`) or a manifest major-version change. Ranked by number of bumps.
    Tests, comments justifying the patch, and patches edited after the latest bump are dropped."""
    known = {e.name for e in _dep_events(ctx)}
    if not known:
        return
    by_path: dict[str, list] = defaultdict(list)
    for r in ctx.grep(_ASSIGN_RX, "+", limit=100000):
        p = r["path"]
        if not is_code(p):
            continue
        if is_test(p) or re.search(r"(?i)(mock|fixture|conftest)", p):
            ctx.drop("tests_or_mocks")
            continue
        by_path[p].append(r)
    manifest_bumps: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for e in _dep_events(ctx):
        if e.kind == "change" and _ver(e.old) and _ver(e.new) and _ver(e.old)[0] != _ver(e.new)[0]:
            manifest_bumps[e.name].append((e.seq, e.hash))
    lock = None
    for path, rs in sorted(by_path.items()):
        head = ctx.text_at_head(path)
        if not head:
            continue
        table = _py_alias_table(head) if path.endswith(".py") else _js_alias_table(head)
        if not table:
            continue
        seen: dict[str, list] = defaultdict(list)
        for r in rs:
            seen[r["text"].strip()].append(r)
        for text, group in seen.items():
            m = _ASSIGN_PARSE.match(text)
            if not m or m.group(1) in _PATCH_SKIP or text not in head:
                continue
            mod = table.get(m.group(1))
            if not mod:
                continue
            pkgs = {n for n in _pkg_candidates(mod) if n in known}
            if not pkgs:
                continue
            last = max(group, key=lambda r: r["seq"])
            lines = head.splitlines()
            idx = next((i for i, ln in enumerate(lines) if ln.strip() == text), None)
            if idx is not None and idx > 0 and any(_PATCH_DOC.search(lines[k]) and lines[k].strip().startswith(("#", "//", '"""', "*"))
                                                    for k in range(max(0, idx - 3), idx)):
                ctx.drop("documented_patch")
                continue
            lock = lock if lock is not None else _lock_bumps(ctx)
            pkg = sorted(pkgs)[0]
            bumps = sorted({(s, h) for q in pkgs for s, h in (lock.get(q, []) + manifest_bumps.get(q, [])) if s > last["seq"]})
            if not bumps:
                allb = [s for q in pkgs for s, _ in (lock.get(q, []) + manifest_bumps.get(q, []))]
                ctx.drop("patch_edited_after_latest_bump" if allb else "no_version_change_seen")
                continue
            yield Hit(
                subject=f"{path}|{text[:100]}",
                score=len(bumps) * 2 + min(5, (ctx.head_ts - last["ts"]) / (180 * DAY)),
                title=f"{text[:80]} patches {pkg} internals; {pkg} bumped {len(bumps)}x since the patch last changed",
                evidence={"assignment": text[:160], "package": pkg, "patch_last_edit": last["hash"],
                          "patch_age_days": round((ctx.head_ts - last["ts"]) / DAY),
                          "bumps_after": [{"hash": h, "seq": s} for s, h in bumps[:6]]},
                commits=[last["hash"], *[h for _, h in bumps[:4]]],
                diffs=[(last["hash"], path)], paths=[path],
            )


# ====================================================================================== build_flag_toggle_churn
BUILD_FILE_RX = re.compile(r"(?i)((^|/)(Makefile|GNUmakefile|CMakeLists\.txt|Cargo\.toml|build\.gradle(\.kts)?|setup\.py|"
                           r"setup\.cfg|tox\.ini|pyproject\.toml|BUILD(\.bazel)?|\.bazelrc|meson\.build|tsconfig[^/]*\.json|"
                           r"Package\.swift|Dockerfile[^/]*|build\.rs|\.cargo/config(\.toml)?|webpack[^/]*\.js|"
                           r"configure\.ac|Rakefile|pom\.xml|go\.mod|\.golangci\.ya?ml)$|\.(cmake|mk|bzl)$)")
FLAG_RX = re.compile(r"""(?x)(?<![\w.-])(
        -W[a-z][\w=,+-]* | -Werror(?:=[\w-]+)? | -O[0-3sgz] | -f(?:no-)?[a-z][\w=,+-]* | -D[A-Z_]\w*(?:=\S+)? |
        -std=\S+ | -march=\S+ | -mtune=\S+ | -race | --strict\b | --no-[a-z][\w-]* | -g[0-3]?\b |
        CGO_ENABLED=\d | opt-level\s*=\s*\S+ | lto\s*=\s*\S+ | panic\s*=\s*"?\w+"? | debug-assertions\s*=\s*\w+ |
        overflow-checks\s*=\s*\w+ | "?strict"?\s*[:=]\s*(?:true|false) | "?noImplicitAny"?\s*[:=]\s*(?:true|false) |
        CMAKE_BUILD_TYPE=\w+ | -DCMAKE_\w+(?:=\S+)? )""")
_FLAG_REASON = re.compile(r"(?i)\b(crash\w*|miscompil\w*|segfault\w*|flak\w*|slow\w*|ub\b|undefined behou?r|sanitiz\w+|"
                          r"ice\b|internal compiler|link(er)? error|too slow|memory|oom|false positive)\b")
_RELEASE_MSG = re.compile(r"(?i)\b(release|bump|version|prepare)\b")


@candidate("build_flag_toggle_churn")
def build_flag_toggle_churn(ctx: Ctx):
    """A compiler/build flag token (``-Werror``, ``-O2``, ``-fsanitize=...``, ``strict``, ``opt-level`` ...) added then
    removed (or removed then re-added) in a build/config file within 180 days, or removed by a commit whose message
    cites crash/miscompile/segfault/flaky/slow/UB. Comment-only lines and release/version bumps are skipped."""
    paths = [p for p in ctx.changes_by_path if BUILD_FILE_RX.search(p) and not is_test(p) and not is_vendored(p)]
    events: dict[tuple[str, str], list[tuple[int, int, str, str]]] = defaultdict(list)
    per: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"+": set(), "-": set()})
    metas: dict[str, tuple[int, int]] = {}
    for r in _rows_for_paths(ctx, paths):
        t = r["text"].strip()
        if t.startswith(("#", "//", ";", "<!--", "*")) or re.search(r"(?i)\bversion\b", t):
            continue
        for m in FLAG_RX.finditer(t):
            per[(r["hash"], r["path"])][r["sign"]].add(re.sub(r"\s+", "", m.group(1)))
        metas[r["hash"]] = (r["seq"], r["ts"])
    for (h, p), d in per.items():
        if _RELEASE_MSG.search(subject_of(_msg(ctx, h))) and len(d["+"]) + len(d["-"]) > 6:
            ctx.drop("release_bump")
            continue
        for f in d["+"] - d["-"]:
            events[(p, f)].append((metas[h][0], metas[h][1], "+", h))
        for f in d["-"] - d["+"]:
            events[(p, f)].append((metas[h][0], metas[h][1], "-", h))
    for (p, flag), evs in events.items():
        evs.sort()
        toggles = [(a, b) for a, b in zip(evs, evs[1:]) if a[2] != b[2] and b[1] - a[1] <= 180 * DAY]
        reason_removals = [e for e in evs if e[2] == "-" and _FLAG_REASON.search(_msg(ctx, e[3]))]
        if not toggles and not reason_removals:
            continue
        involved = []
        for a, b in toggles:
            involved += [a[3], b[3]]
        involved += [e[3] for e in reason_removals]
        involved = list(dict.fromkeys(involved))
        if all(_bot_commit(ctx, h) for h in involved):
            ctx.drop("bot_only")
            continue
        yield Hit(
            subject=f"{p}|{flag}",
            score=len(toggles) * 2 + len(reason_removals) * 3 + (1 if len(evs) >= 4 else 0),
            title=f"{flag} toggled {len(evs)}x in {p}" + (f" ({subject_of(_msg(ctx, reason_removals[0][3]))[:60]})" if reason_removals else ""),
            evidence={"flag": flag, "history": [{"hash": e[3][:10], "change": "added" if e[2] == "+" else "removed"} for e in evs[:8]],
                      "toggles_within_180d": len(toggles), "removal_reasons": [subject_of(_msg(ctx, e[3])) for e in reason_removals[:3]]},
            commits=involved[:6], diffs=[(h, p) for h in involved[:3]], paths=[p],
        )


# ====================================================================================== cache_key_design_and_bust_churn
_CACHE_TOKEN = re.compile(r"(?i)\b(?P<pre>v|ver|version|cache[-_]?v?(?:ersion)?[-_]?)(?P<n>\d+)\b")
_CACHE_LINE = re.compile(r"(?i)(cache[-_ ]?(key|version|bust|ver|prefix)|restore-keys|\bkey\s*:\s*\S*(v\d+|cache)|CACHE_VERSION)")
_CACHE_FIX_MSG = re.compile(r"(?i)\bcache\w*\b.*\b(fix|stale|bust|invalidat\w+|bump|corrupt\w*|clear|break|broke|poison\w*)\b|"
                            r"\b(fix|stale|bust|invalidat\w+|bump|clear|poison\w*)\b.*\bcache\w*\b")


@candidate("cache_key_design_and_bust_churn")
def cache_key_design_and_bust_churn(ctx: Ctx):
    """Hand-bumped cache version tokens (``key: foo-v1`` -> ``v2`` -> ``v3``, ``CACHE_VERSION``) >=2 times within 12
    months for one key, or >=2 cache-related fix/bust commits touching cache-key lines (restore-keys changes after a
    break included). A key without ``hashFiles`` at HEAD is a boost. Single introductions and tool caches keyed by
    hashFiles are dropped. (``restore-keys`` loosening is approximated by cache fix messages on cache-key lines.)"""
    ci = [p for p in ctx.changes_by_path if is_ci(p) or re.search(r"(?i)(^|/)(\.circleci|\.github)/", p)]
    rows = [r for r in _rows_for_paths(ctx, ci) if _CACHE_LINE.search(r["text"])]
    rows += [r for r in ctx.grep(r"CACHE_VERSION|CACHE_KEY_PREFIX|cache_version|CACHE_BUST", None, limit=20000)
             if is_code(r["path"]) and not is_test(r["path"])]
    per: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: {"+": [], "-": []})
    for r in rows:
        per[(r["hash"], r["path"])][r["sign"]].append(r)
    bumps: dict[tuple[str, str], list[tuple[int, int, str, str, str]]] = defaultdict(list)
    fixes: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (h, p), d in per.items():
        c = ctx.by_hash[h]
        if ctx.is_bot(c):
            ctx.drop("bot_commit")
            continue
        for ra in d["+"]:
            ta = _CACHE_TOKEN.search(ra["text"])
            for rr in d["-"]:
                tr = _CACHE_TOKEN.search(rr["text"])
                if ta and tr and ta.group("n") != tr.group("n"):
                    norm = _CACHE_TOKEN.sub("<v>", ra["text"]).strip()
                    if norm == _CACHE_TOKEN.sub("<v>", rr["text"]).strip():
                        bumps[(p, norm)].append((c["seq"], c["ts"], h, tr.group("n"), ta.group("n")))
                        break
        if _CACHE_FIX_MSG.search(c["message"] or "") and (d["+"] or d["-"]):
            fixes[p].append((c["ts"], h))
    seen: set[str] = set()
    for (p, norm), evs in bumps.items():
        evs = sorted(set(evs))
        best, window = 0, []
        for i, e in enumerate(evs):
            w = [x for x in evs[i:] if x[1] - e[1] <= 365 * DAY]
            if len(w) > best:
                best, window = len(w), w
        if best < 2:
            continue
        head = ctx.text_at_head(p) or ""
        no_hash = bool(head) and "hashFiles" not in head and is_ci(p)
        seen.add(p)
        yield Hit(
            subject=f"{p}|{norm[:100]}", score=best * 2 + (1 if no_hash else 0) + len(fixes.get(p, [])),
            title=f"cache key version bumped {best}x by hand in {p}: {norm[:70]}",
            evidence={"key_line": norm[:160], "bumps": [{"hash": e[2][:10], "from": e[3], "to": e[4]} for e in window[:8]],
                      "no_hashFiles_at_head": no_hash, "cache_fix_commits": len(fixes.get(p, []))},
            commits=[e[2] for e in window[:6]], diffs=[(e[2], p) for e in window[:3]], paths=[p],
        )
    for p, fs in fixes.items():
        fs = sorted(set(fs))
        if p in seen:
            continue
        if len(fs) < 2:
            ctx.drop("single_cache_fix")
            continue
        yield Hit(
            subject=f"{p}|cache_fixes", score=len(fs) * 1.5,
            title=f"{len(fs)} cache-related fix commits touching cache keys in {p}",
            evidence={"fixes": [{"hash": h[:10], "subject": subject_of(_msg(ctx, h))} for _, h in fs[:8]]},
            commits=[h for _, h in fs[:6]], diffs=[(h, p) for _, h in fs[:3]], paths=[p],
        )

