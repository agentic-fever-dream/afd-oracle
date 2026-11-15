"""Build a deterministic fixture repository with known oracle facts.

Planted facts (all asserted by the test-suite):
  * ``legacy_parse`` created (c01), modified (c04), deleted (c13)
  * ``experimental_hash`` created (c14) and deleted (c15) three days later  -> short-lived symbol
  * explicit ``git revert`` of "Add caching layer" (c06 -> c07)
  * silent revert: "Clean up client" (c11) removes exactly what "Add retry decorator to client" (c08) added
  * abandoned branch ``experiment/graphql`` (tip > 90 days older than the latest commit)
  * merged branch ``feature/login`` (--no-ff merge commit)
  * rename ``util/strings.py`` -> ``util/text.py`` (c10)
  * ``api/schema.py`` <-> ``web/forms.ts`` co-changed repeatedly, different top-level dirs, no import/call link
  * import cycle core/a.py -> core/b.py -> core/c.py -> core/a.py
  * god class ``core/engine.py::Engine`` with 40 methods

Usage:  uv run python tests/fixtures/build_fixture_repo.py [dest]
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(days=5)
AUTHORS = [("Alice Archer", "alice@example.com"), ("Bob Builder", "bob@example.com"),
           ("Carol Coder", "carol@example.com")]


class Builder:
    def __init__(self, root: Path):
        self.root = root
        self.n = 0
        self.facts: dict[str, str] = {}

    def git(self, *args: str, when: datetime | None = None, author: int = 0) -> str:
        env = dict(os.environ)
        name, email = AUTHORS[author % len(AUTHORS)]
        stamp = (when or START).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name,
                   GIT_COMMITTER_EMAIL=email, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        out = subprocess.run(["git", "-C", str(self.root), *args], env=env, check=True, capture_output=True, text=True)
        return out.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()

    def commit(self, key: str, message: str, when: datetime | None = None) -> str:
        self.n += 1
        when = when or START + STEP * self.n
        self.git("add", "-A", when=when)
        self.git("commit", "-q", "-m", message, when=when, author=self.n)
        sha = self.git("rev-parse", "HEAD")
        self.facts[key] = sha
        self._last_when = when
        return sha


# ---------------------------------------------------------------- file templates
def schema_py(fields: list[str]) -> str:
    body = "".join(f"    {f}: str\n" for f in fields)
    checks = "".join(f"    if not data.get({f!r}):\n        errors.append({f!r})\n" for f in fields)
    return (f'"""Server-side payload schema."""\n\n\nclass Schema:\n{body}\n\n'
            f"def validate_schema(data):\n    errors = []\n{checks}    return errors\n")


def forms_ts(fields: list[str]) -> str:
    checks = "".join(f"  if (!data.{f}) errors.push('{f}');\n" for f in fields)
    return ("/** Validates the signup form before submit. */\n"
            "export function validateForm(data: Record<string, string>): string[] {\n"
            f"  const errors: string[] = [];\n{checks}  return errors;\n}}\n")


def helpers_py(legacy: int | None, experimental: bool = False) -> str:
    out = '"""Misc helpers."""\n\n\ndef normalize(value):\n    return value.strip().lower()\n'
    if legacy == 1:
        out += "\n\ndef legacy_parse(raw):\n    return raw.split(',')\n"
    elif legacy == 2:
        out += "\n\ndef legacy_parse(raw):\n    return [p.strip() for p in raw.split(',')]\n"
    if experimental:
        out += "\n\ndef experimental_hash(value):\n    return hash(value) % 997\n"
    return out


CLIENT_BASE = ('"""HTTP client."""\nimport urllib.request\n\n\n'
               "def fetch(url):\n    with urllib.request.urlopen(url) as r:\n        return r.read()\n")
CLIENT_RETRY = CLIENT_BASE + ("\n\ndef with_retry(fn, attempts=3):\n    last = None\n    for _ in range(attempts):\n"
                              "        try:\n            return fn()\n        except OSError as e:\n"
                              "            last = e\n    raise last\n")


def engine_py(extra: str = "") -> str:
    methods = "".join(f"\n    def step_{i:02d}(self, x):\n        return self.base + x * {i}\n" for i in range(40))
    return (f'"""The do-everything engine."""\n\n\nclass Engine:\n    def __init__(self):\n        self.base = 1{extra}\n'
            f"{methods}")


def build(dest: Path) -> dict:
    if dest.exists():
        shutil.rmtree(dest, onerror=lambda f, p, _: (os.chmod(p, stat.S_IWRITE), f(p)))
    dest.mkdir(parents=True)
    b = Builder(dest)
    b.git("init", "-q", "-b", "main")
    b.git("config", "core.autocrlf", "false")
    b.git("config", "commit.gpgsign", "false")

    fields = ["name"]
    b.write("README.md", "# Fixture\n")
    b.write("api/__init__.py", "")
    b.write("api/schema.py", schema_py(fields))
    b.write("api/client.py", CLIENT_BASE)
    b.write("web/forms.ts", forms_ts(fields))
    b.write("core/__init__.py", "")
    b.write("core/engine.py", engine_py())
    b.write("util/__init__.py", "")
    b.write("util/helpers.py", helpers_py(1))
    b.write("util/strings.py", '"""String utils."""\n\n\ndef slugify(s):\n    return "-".join(s.lower().split())\n')
    b.commit("c01_initial", "Initial project layout")

    b.write("core/a.py", "from core.b import beta\n\n\ndef alpha():\n    return beta()\n")
    b.write("core/b.py", "from core.c import gamma\n\n\ndef beta():\n    return gamma()\n")
    b.write("core/c.py", "from core.a import alpha\n\n\ndef gamma():\n    return 1 if alpha else 0\n")
    b.commit("c02_cycle", "Add core pipeline stages")
    base_for_graphql = b.facts["c02_cycle"]

    fields.append("email")
    b.write("api/schema.py", schema_py(fields))
    b.write("web/forms.ts", forms_ts(fields))
    b.commit("c03_pair1", "Add email field to signup")

    b.write("util/helpers.py", helpers_py(2))
    b.commit("c04_legacy_modified", "Trim whitespace in legacy_parse")

    fields.append("phone")
    b.write("api/schema.py", schema_py(fields))
    b.write("web/forms.ts", forms_ts(fields))
    b.commit("c05_pair2", "Add phone field to signup")

    b.write("feature/__init__.py", "")
    b.write("feature/cache.py", '"""Read-through cache."""\n\n_CACHE = {}\n\n\ndef cached(key, compute):\n'
                                "    if key not in _CACHE:\n        _CACHE[key] = compute()\n    return _CACHE[key]\n")
    b.commit("c06_cache", "Add caching layer")

    b.n += 1
    when = START + STEP * b.n
    b.git("revert", "--no-edit", b.facts["c06_cache"], when=when, author=b.n)
    b.facts["c07_revert_cache"] = b.git("rev-parse", "HEAD")

    b.write("api/client.py", CLIENT_RETRY)
    b.commit("c08_retry", "Add retry decorator to client")

    fields.append("address")
    b.write("api/schema.py", schema_py(fields))
    b.write("web/forms.ts", forms_ts(fields))
    b.commit("c09_pair3", "Add address field to signup")

    b.n += 1
    when = START + STEP * b.n
    b.git("mv", "util/strings.py", "util/text.py", when=when)
    b.git("commit", "-q", "-m", "Rename strings util to text", when=when, author=b.n)
    b.facts["c10_rename"] = b.git("rev-parse", "HEAD")

    b.write("api/client.py", CLIENT_BASE)
    b.commit("c11_silent_revert", "Clean up client")

    fields.append("zip")
    b.write("api/schema.py", schema_py(fields))
    b.write("web/forms.ts", forms_ts(fields))
    b.commit("c12_pair4", "Add zip code to signup")

    b.write("util/helpers.py", helpers_py(None))
    b.commit("c13_legacy_deleted", "Remove legacy_parse")

    b.write("util/helpers.py", helpers_py(None, experimental=True))
    b.commit("c14_experimental", "Try experimental hash")

    b.write("util/helpers.py", helpers_py(None))
    b.facts["c15_experimental_deleted"] = b.commit("c15_experimental_deleted", "Drop experimental hash",
                                                   when=b._last_when + timedelta(days=3))

    fields.append("country")
    b.write("api/schema.py", schema_py(fields))
    b.write("web/forms.ts", forms_ts(fields))
    b.commit("c16_pair5", "Add country field to signup")

    # merged feature branch
    b.git("checkout", "-q", "-b", "feature/login")
    b.write("web/login.ts", "import { validateForm } from './forms';\n\n"
                            "export function login(data: Record<string, string>): boolean {\n"
                            "  return validateForm(data).length === 0;\n}\n")
    b.write("api/auth.py", '"""Auth."""\n\n\ndef check_password(user, password):\n    return bool(user and password)\n')
    b.commit("l1_login", "Add login form")
    b.write("api/auth.py", '"""Auth."""\n\n\ndef check_password(user, password):\n'
                           "    return bool(user and password and len(password) > 8)\n")
    b.commit("l2_login", "Require long passwords")
    b.git("checkout", "-q", "main")
    b.write("README.md", "# Fixture\n\nSignup and login demo.\n")
    b.commit("c19_readme", "Update README")
    b.n += 1
    when = START + STEP * b.n
    b.git("merge", "--no-ff", "-q", "-m", "Merge branch 'feature/login'", "feature/login", when=when, author=b.n)
    b.facts["m1_merge_login"] = b.git("rev-parse", "HEAD")

    # padding commits to push the abandoned branch past the 90-day threshold
    b.write("core/engine.py", engine_py("\n        self.mode = 'fast'"))
    b.commit("c21_engine", "Add engine mode")
    b.write("core/engine.py", engine_py("\n        self.mode = 'safe'"))
    b.commit("c22_engine", "Default engine to safe mode")

    # abandoned branch created early, with old dates
    b.git("checkout", "-q", "-b", "experiment/graphql", base_for_graphql)
    b.write("api/graphql.py", '"""GraphQL prototype."""\n\nSCHEMA = "type Query { user: User }"\n')
    b.commit("g1_graphql", "Prototype GraphQL schema", when=START + timedelta(days=12))
    b.write("api/resolvers.py", "def resolve_user(obj, info):\n    return {'name': 'x'}\n")
    b.write("api/graphql.py", '"""GraphQL prototype."""\n\nSCHEMA = "type Query { user: User, users: [User] }"\n')
    b.commit("g2_graphql", "Add GraphQL resolvers", when=START + timedelta(days=13))
    b.git("checkout", "-q", "main")

    facts = {
        "commits": b.facts,
        "default_branch": "refs/heads/main",
        "pair": ["api/schema.py", "web/forms.ts"],
        "cycle": ["core/a.py", "core/b.py", "core/c.py"],
        "god_class": "Engine",
        "lifecycle_symbol": {"path": "util/helpers.py", "qualname": "legacy_parse",
                             "created": b.facts["c01_initial"], "modified": b.facts["c04_legacy_modified"],
                             "deleted": b.facts["c13_legacy_deleted"]},
        "short_lived": {"path": "util/helpers.py", "qualname": "experimental_hash"},
        "explicit_revert": {"reverter": b.facts["c07_revert_cache"], "reverted": b.facts["c06_cache"]},
        "silent_revert": {"reverter": b.facts["c11_silent_revert"], "reverted": b.facts["c08_retry"]},
        "rename": {"old": "util/strings.py", "new": "util/text.py", "commit": b.facts["c10_rename"]},
        "abandoned_branch": "experiment/graphql",
        "merged_branch": "feature/login",
    }
    (dest.parent / "fixture-facts.json").write_text(json.dumps(facts, indent=2))
    return facts


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out" / "fixture-repo"
    f = build(target)
    print(json.dumps({"repo": str(target), "commits": len(f["commits"])}, indent=2))
