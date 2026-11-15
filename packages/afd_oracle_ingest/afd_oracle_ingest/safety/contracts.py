"""Implicit contracts: producers and consumers linked only by a string.

kind        producer (sets / defines / serves)                consumer (reads / calls / listens)
env         .env, compose/CI `environment:`, os.environ[X]=    os.environ[X], os.getenv(X), process.env.X
config      "KEY": in a config dict, config["KEY"] =           config["KEY"], config.get("KEY"), app.config.KEY
event       emit/send/publish/dispatch("name"), signal("x")    on/connect/subscribe/addEventListener("name")
route       @app.route("/p"), router.get("/p"), add_url_rule   fetch("/p"), axios.get("/p"), client.get("/p")
endpoint    the view function name under a route decorator     url_for("name")
sql_table   CREATE TABLE t                                     FROM/JOIN/INTO/UPDATE t inside SQL strings
registry    @x.register("k"), REGISTRY["k"] =                  REGISTRY["k"], registry.get("k")
flag        defaults in config / flags files                   is_enabled("k"), feature_flag("k")
cli_flag    add_argument("--x"), click.option("--x")           (docs/scripts that pass --x)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .source import SourceFile, is_test_path

PRODUCER, CONSUMER = "producer", "consumer"


@dataclass(frozen=True)
class Contract:
    kind: str
    key: str
    role: str
    path: str
    line: int
    symbol: str | None


def norm_route(r: str) -> str:
    r = r.split("?", 1)[0].split("#", 1)[0]
    r = re.sub(r"<[^>]+>|:\w+|\$\{[^}]+\}|\{[^}]+\}", "{}", r)
    return (r.rstrip("/") or "/").lower()


_RX = [
    # env
    ("env", CONSUMER, {"python"}, r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]{1,})[\"']\s*\](?!\s*=(?!=))"),
    ("env", CONSUMER, {"python"}, r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z][A-Z0-9_]{1,})[\"']"),
    ("env", PRODUCER, {"python"}, r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]{1,})[\"']\s*\]\s*=(?!=)"),
    ("env", PRODUCER, {"python"}, r"os\.environ\.setdefault\(\s*[\"']([A-Z][A-Z0-9_]{1,})[\"']"),
    ("env", PRODUCER, {"python"}, r"monkeypatch\.setenv\(\s*[\"']([A-Z][A-Z0-9_]{1,})[\"']"),
    ("env", CONSUMER, {"typescript", "tsx", "javascript"}, r"process\.env\.([A-Z][A-Z0-9_]{1,})\b"),
    ("env", CONSUMER, {"typescript", "tsx", "javascript"}, r"process\.env\[\s*[\"'`]([A-Z][A-Z0-9_]{1,})[\"'`]"),
    ("env", CONSUMER, {"typescript", "tsx", "javascript"}, r"import\.meta\.env\.([A-Z][A-Z0-9_]{1,})\b"),
    # config
    ("config", CONSUMER, {"python"}, r"\bconfig\[\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*\](?!\s*=(?!=))"),
    ("config", CONSUMER, {"python"}, r"\bconfig\.get\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']"),
    ("config", PRODUCER, {"python"}, r"\bconfig\[\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*\]\s*=(?!=)"),
    ("config", PRODUCER, {"python"}, r"ConfigAttribute\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']"),
    # events / signals / hooks
    ("event", PRODUCER, {"python"}, r"\.(?:emit|publish|trigger|dispatch|send_event|fire)\(\s*[\"']([\w.:/-]{2,})[\"']"),
    ("event", PRODUCER, {"python"}, r"\bsignal\(\s*[\"']([\w.:/-]{2,})[\"']"),
    ("event", CONSUMER, {"python"}, r"\.(?:on|subscribe|listen|add_listener|connect_via)\(\s*[\"']([\w.:/-]{2,})[\"']"),
    ("event", PRODUCER, {"typescript", "tsx", "javascript"}, r"\.(?:emit|dispatch|publish|trigger)\(\s*[\"'`]([\w.:/-]{2,})[\"'`]"),
    ("event", PRODUCER, {"typescript", "tsx", "javascript"}, r"new (?:Custom)?Event\(\s*[\"'`]([\w.:/-]{2,})[\"'`]"),
    ("event", CONSUMER, {"typescript", "tsx", "javascript"}, r"\.(?:on|once|addEventListener|subscribe|addListener)\(\s*[\"'`]([\w.:/-]{2,})[\"'`]"),
    # routes
    ("route", PRODUCER, {"python"}, r"@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete|websocket)\(\s*[\"'](/[^\"']*)[\"']"),
    ("route", PRODUCER, {"python"}, r"\.add_url_rule\(\s*[\"'](/[^\"']*)[\"']"),
    ("route", PRODUCER, {"python"}, r"\bpath\(\s*[\"']([^\"']*/)[\"']"),
    ("route", PRODUCER, {"typescript", "tsx", "javascript"}, r"\b(?:app|router|server)\.(?:get|post|put|patch|delete|all|use)\(\s*[\"'`](/[^\"'`]*)[\"'`]"),
    ("route", CONSUMER, {"typescript", "tsx", "javascript"}, r"\bfetch\(\s*[\"'`](/[^\"'`]*)[\"'`]"),
    ("route", CONSUMER, {"typescript", "tsx", "javascript"}, r"\b(?:axios|http|api|client)\.(?:get|post|put|patch|delete)\(\s*[\"'`](/[^\"'`]*)[\"'`]"),
    ("route", CONSUMER, {"python"}, r"\b(?:client|requests|httpx|session)\.(?:get|post|put|patch|delete|open)\(\s*[\"'](/[^\"']*)[\"']"),
    # endpoint names
    ("endpoint", CONSUMER, {"python"}, r"\burl_for\(\s*[\"']([\w.]+)[\"']"),
    ("endpoint", PRODUCER, {"python"}, r"endpoint\s*=\s*[\"']([\w.]+)[\"']"),
    # registries / flags / cli
    ("registry", PRODUCER, {"python", "typescript", "tsx", "javascript"}, r"\.register\(\s*[\"'`]([\w.:/-]{2,})[\"'`]"),
    ("registry", PRODUCER, None, r"\b[A-Z_]*(?:REGISTRY|HANDLERS|PLUGINS)\[\s*[\"']([\w.:/-]{2,})[\"']\s*\]\s*=(?!=)"),
    ("registry", CONSUMER, None, r"\b[A-Z_]*(?:REGISTRY|HANDLERS|PLUGINS)(?:\[\s*|\.get\(\s*)[\"']([\w.:/-]{2,})[\"']\s*[\])](?!\s*=(?!=))"),
    ("flag", CONSUMER, None, r"\b(?:is_enabled|feature_flag|flag_enabled|isEnabled|useFlag|getFlag)\(\s*[\"'`]([\w.:-]{2,})[\"'`]"),
    ("cli_flag", PRODUCER, {"python"}, r"(?:add_argument|click\.option|typer\.Option)\([^)]*?[\"'](--[a-z][\w-]+)[\"']"),
]
_COMPILED = [(k, r, langs, re.compile(p)) for k, r, langs, p in _RX]
_SQL_DEF = re.compile(r"(?i)\bcreate\s+(?:temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?[\"`\[]?(\w+)")
_SQL_USE = re.compile(r"(?i)\b(?:from|join|into|update)\s+[\"`\[]?([a-z_]\w*)")
_SQL_HINT = re.compile(r"(?i)\b(select|insert|update|delete)\b.*\b(from|into|set|join)\b")
_ENV_FILE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,})\s*=")
_YAML_ENV = re.compile(r"^\s*-?\s*([A-Z][A-Z0-9_]{2,})\s*[:=]")
_CONFIG_DICT_KEY = re.compile(r"^\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*:")
_ROUTE_DEC = re.compile(r"^\s*@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete)\(")


def extract(src: SourceFile, repo_id: str) -> list[Contract]:
    out: list[Contract] = []
    lines = src.lines
    name = src.path.rsplit("/", 1)[-1]
    lower = src.path.lower()
    # env producers in dotenv / compose / CI files
    if name.startswith(".env") or name.endswith(".env"):
        for i, s in enumerate(lines):
            m = _ENV_FILE.match(s)
            if m:
                out.append(Contract("env", m.group(1), PRODUCER, src.path, i + 1, None))
        return out
    if name.endswith((".yml", ".yaml")) and ("compose" in lower or ".github/" in lower or "ci" in lower or "deploy" in lower):
        in_env = False
        env_indent = -1
        for i, s in enumerate(lines):
            if re.match(r"^\s*(environment|env)\s*:", s):
                in_env, env_indent = True, len(s) - len(s.lstrip())
                continue
            if in_env:
                if s.strip() and len(s) - len(s.lstrip()) <= env_indent:
                    in_env = False
                    continue
                m = _YAML_ENV.match(s)
                if m:
                    out.append(Contract("env", m.group(1), PRODUCER, src.path, i + 1, None))
        return out
    if src.lang is None and not name.endswith(".sql"):
        return out
    in_config_dict = False
    pending_route = False
    mapping_depth = 0
    doc = src.docstring_lines()
    for i, s in enumerate(lines):
        n = i + 1
        if n in doc:  # examples in docstrings are not real producers/consumers
            continue
        sym = src.uid_at(repo_id, n) if src.parse else None
        for kind, role, langs, rx in _COMPILED:
            if langs is not None and src.lang not in langs:
                continue
            for m in rx.finditer(s):
                key = m.group(1)
                if kind == "route":
                    key = norm_route(key)
                out.append(Contract(kind, key, role, src.path, n, sym))
        # config defaults dict: `default_config = {... "KEY": ...}`
        if src.lang == "python":
            # `app.config.from_mapping(KEY=...)` / `config.update(KEY=...)`: keyword arguments define keys
            if re.search(r"\.(from_mapping|update)\(", s) and "config" in s:
                mapping_depth = s.count("(") - s.count(")")
                for m in re.finditer(r"\b([A-Z][A-Z0-9_]{2,})\s*=(?!=)", s):
                    out.append(Contract("config", m.group(1), PRODUCER, src.path, n, sym))
            elif mapping_depth > 0:
                for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=(?!=)", s):
                    out.append(Contract("config", m.group(1), PRODUCER, src.path, n, sym))
                mapping_depth += s.count("(") - s.count(")")
            # `default_config = ImmutableDict({` / `= {` / `= dict(` (the `{` may be on the next line)
            if re.search(r"\w*(config|CONFIG|settings|SETTINGS|defaults|DEFAULTS)\w*\s*(:\s*[^=]+)?=\s*(\w+\(\s*)?\{?\s*$|"
                         r"\w*(config|CONFIG|settings|SETTINGS|defaults|DEFAULTS)\w*\s*(:\s*[^=]+)?=\s*(\w+\()?\s*\{", s):
                in_config_dict = True
            if in_config_dict:
                m = _CONFIG_DICT_KEY.match(s)
                if m:
                    out.append(Contract("config", m.group(1), PRODUCER, src.path, n, sym))
                if re.match(r"^\s*\}\s*\)?\s*,?\s*$", s) or re.match(r"^\s*\)\s*$", s):
                    in_config_dict = False
            # endpoint producer: the function under a route decorator
            if _ROUTE_DEC.match(s):
                pending_route = True
            elif pending_route:
                m = re.match(r"^\s*(?:async\s+)?def\s+(\w+)", s)
                if m:
                    out.append(Contract("endpoint", m.group(1), PRODUCER, src.path, n, sym))
                    pending_route = False
                elif not s.strip().startswith("@"):
                    pending_route = False
            # settings modules define config keys as module constants
            if any(w in lower for w in ("settings", "config")) and not is_test_path(src.path):
                m = re.match(r"^([A-Z][A-Z0-9_]{2,})\s*(:\s*[^=]+)?=(?!\s*([\[{(]|dict\(|ImmutableDict))", s)
                if m:  # a constant setting, not the name of a dict/list of settings
                    out.append(Contract("config", m.group(1), PRODUCER, src.path, n, sym))
        # SQL tables (in .sql files and SQL-looking strings)
        for m in _SQL_DEF.finditer(s):
            out.append(Contract("sql_table", m.group(1).lower(), PRODUCER, src.path, n, sym))
        if name.endswith(".sql") or _SQL_HINT.search(s):
            for m in _SQL_USE.finditer(s):
                t = m.group(1).lower()
                if t not in ("select", "where", "set", "values", "the", "a", "an"):
                    out.append(Contract("sql_table", t, CONSUMER, src.path, n, sym))
    return out


def match(contracts: list[Contract]) -> dict[tuple[str, str], dict[str, list[Contract]]]:
    by: dict[tuple[str, str], dict[str, list[Contract]]] = {}
    for c in contracts:
        by.setdefault((c.kind, c.key), {PRODUCER: [], CONSUMER: []})[c.role].append(c)
    # blueprint endpoints: url_for("blog.index") is served by the `index` view registered under a blueprint
    for (kind, key), roles in list(by.items()):
        if kind == "endpoint" and "." in key and not roles[PRODUCER]:
            short = by.get(("endpoint", key.rsplit(".", 1)[1]))
            if short and short[PRODUCER]:
                roles[PRODUCER].extend(short[PRODUCER])
    return by


# Keys normally provided by the runtime / platform, not by the repository: no "orphan" warning for these.
EXTERNAL_ENV = {"HOME", "PATH", "USER", "USERNAME", "PWD", "SHELL", "TERM", "LANG", "TMPDIR", "TEMP", "TMP", "CI",
                "PYTHONPATH", "VIRTUAL_ENV", "NODE_ENV", "DEBUG", "PORT", "HOSTNAME", "APPDATA", "LOCALAPPDATA",
                "GITHUB_TOKEN", "GITHUB_ACTIONS", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "SSL_CERT_FILE", "COLUMNS"}
