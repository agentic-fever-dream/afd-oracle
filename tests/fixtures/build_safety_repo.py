"""Deterministic repository with planted change-safety facts (one per detector).

Planted:
  security   app/core.py run(): subprocess(shell=True); app/db.py: SQL f-string; web/client.ts: innerHTML
  race       app/core.py: module dict `_cache` written in store() (module starts threads); lazy global get_client();
             class-level mutable Registry.items
  logic bomb app/maint.py: date-gated rmtree; exec(b64decode(...))
  contracts  env SERVICE_URL read, never set (orphan); env DB_URL set in .env.example and read in app/db.py;
             config TIMEOUT defined in DEFAULTS and read; config MISSING_KEY read only (orphan);
             event user.created emitted in app/events.py and handled in web/listener.ts; order.paid handled only;
             route /api/items served in app/views.py and fetched in web/client.ts
  api        app/api.py: __all__ = [fetch, Client]; fetch(key, timeout=10); Client.get(path, retries=3); tag v1.0
  lineage    commit "Fix crash when key is None (#12)" adds the None guard to fetch + test_fetch_none;
             commit "Fix XSS in render: escape user input" adds escape() to views.render (security zone)
  coupling   app/schema.py and web/forms.ts change together 3 times with no import link
  overrides  app/shapes.py: Square(Shape) overrides area()
  tests      tests/test_api.py calls fetch
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BASE_TS = 1_700_000_000


class B:
    def __init__(self, root: Path):
        self.root = root
        self.n = 0
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.facts: dict = {"commits": {}}

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
               "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
               "GIT_AUTHOR_DATE": f"{BASE_TS + self.n * 86400} +0000",
               "GIT_COMMITTER_DATE": f"{BASE_TS + self.n * 86400} +0000"}
        return subprocess.run(["git", "-C", str(self.root), "-c", "core.autocrlf=false", *args], check=True,
                              capture_output=True, text=True, env=env).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text.lstrip("\n"), encoding="utf-8", newline="\n")

    def commit(self, key: str, msg: str) -> str:
        self.n += 1
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        sha = self.git("rev-parse", "HEAD")
        self.facts["commits"][key] = sha
        return sha


API_V1 = '''
from app.core import load

__all__ = ["fetch", "Client"]


def fetch(key, timeout=10):
    """Fetch a value."""
    return load(key)


class Client:
    def __init__(self, base):
        self.base = base

    def get(self, path, retries=3):
        return fetch(path)


def _helper():
    return 1
'''

API_FIXED = API_V1.replace('''    """Fetch a value."""
    return load(key)''', '''    """Fetch a value."""
    if key is None:
        raise ValueError("key is required")
    return load(key)''')

CORE = '''
import os
import subprocess
import threading

_cache = {}
_client = None


def load(name):
    return _cache.get(name)


def store(name, value):
    _cache[name] = value


def get_client():
    global _client
    if _client is None:
        _client = object()
    return _client


def run(cmd):
    return subprocess.run(cmd, shell=True)


def api_url():
    return os.environ["SERVICE_URL"]


def worker():
    threading.Thread(target=store, args=("a", 1)).start()


class Registry:
    items = []

    def add(self, x):
        self.items.append(x)
'''

DB = '''
import os
import sqlite3

SCHEMA = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"


def connect():
    return sqlite3.connect(os.getenv("DB_URL", ":memory:"))


def find(conn, uid):
    return conn.execute(f"SELECT * FROM users WHERE id = {uid}").fetchone()
'''

CONFIG = '''
DEFAULTS = {
    "TIMEOUT": 10,
    "FEATURE_X": False,
}


def timeout(app):
    return app.config["TIMEOUT"]


def missing(app):
    return app.config["MISSING_KEY"]
'''

EVENTS = '''
class Bus:
    def emit(self, name, payload=None):
        return name, payload


bus = Bus()


def user_created(user):
    bus.emit("user.created", user)
'''

VIEWS_V1 = '''
from flask import Flask

app = Flask(__name__)


@app.route("/api/items")
def items():
    return []


def render(text):
    return "<p>" + text + "</p>"
'''

VIEWS_FIXED = VIEWS_V1.replace('''def render(text):
    return "<p>" + text + "</p>"''', '''def render(text):
    from markupsafe import escape
    return "<p>" + escape(text) + "</p>"''')

MAINT = '''
import base64
import datetime
import shutil


def cleanup():
    if datetime.date.today() > datetime.date(2027, 1, 1):
        shutil.rmtree("/var/data")


def boot(payload):
    exec(base64.b64decode(payload))
'''

SHAPES = '''
class Shape:
    def area(self):
        return 0


class Square(Shape):
    def __init__(self, side):
        self.side = side

    def area(self):
        return self.side * self.side
'''

CLIENT_TS = '''
export async function loadItems(el: HTMLElement): Promise<void> {
  const res = await fetch("/api/items");
  const data = await res.text();
  el.innerHTML = data;
}
'''

LISTENER_TS = '''
import { bus } from "./bus";

export function wire(): void {
  bus.on("user.created", (u: unknown) => console.log(u));
  bus.on("order.paid", (o: unknown) => console.log(o));
}
'''

TEST_API = '''
from app.api import fetch


def test_fetch():
    assert fetch("a") is None
'''

TEST_API_FIXED = TEST_API + '''

def test_fetch_none():
    import pytest
    with pytest.raises(ValueError):
        fetch(None)
'''


def schema(n: int) -> str:
    fields = ["email", "name", "zip", "phone"][:n]
    return "FIELDS = [" + ", ".join(f'"{f}"' for f in fields) + "]\n"


def forms(n: int) -> str:
    fields = ["email", "name", "zip", "phone"][:n]
    return "export const FIELDS = [" + ", ".join(f'"{f}"' for f in fields) + "];\n"


def build(root: Path) -> dict:
    b = B(root)
    b.write("app/__init__.py", "")
    b.write("app/core.py", CORE)
    b.write("app/api.py", API_V1)
    b.write("app/db.py", DB)
    b.write("app/config.py", CONFIG)
    b.write("app/events.py", EVENTS)
    b.write("app/views.py", VIEWS_V1)
    b.write("app/maint.py", MAINT)
    b.write("app/shapes.py", SHAPES)
    b.write("app/schema.py", schema(1))
    b.write("web/client.ts", CLIENT_TS)
    b.write("web/listener.ts", LISTENER_TS)
    b.write("web/forms.ts", forms(1))
    b.write("tests/__init__.py", "")
    b.write("tests/test_api.py", TEST_API)
    b.write(".env.example", "DB_URL=sqlite:///app.db\n")
    b.write("requirements.txt", "flask==3.0.0\n")
    b.commit("c1_initial", "Initial application")
    b.write("app/api.py", API_FIXED)
    b.write("tests/test_api.py", TEST_API_FIXED)
    b.commit("c2_fix_none", "Fix crash when key is None (#12)")
    b.write("app/views.py", VIEWS_FIXED)
    b.commit("c3_fix_xss", "Fix XSS in render: escape user input")
    b.git("tag", "v1.0")
    for n in (2, 3, 4):
        b.write("app/schema.py", schema(n))
        b.write("web/forms.ts", forms(n))
        b.commit(f"c{n + 2}_fields", f"Add field {n} to signup")
    facts = {**b.facts, "head": b.git("rev-parse", "HEAD")}
    (root.parent / "safety-facts.json").write_text(json.dumps(facts, indent=2))
    return facts


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "out" / "safety-repo")
    print(json.dumps(build(out), indent=2))
