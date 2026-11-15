"""Health checks and one-click setup: Docker stack, user-scope MCP registration, and the Claude skill."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from afd_oracle_core import runtime
from afd_oracle_core.config import get_settings

SKILL_NAME = "afd-oracle"
MCP_NAME = "afd-oracle"
TEMPLATE = Path(__file__).parent / "templates" / "SKILL.md"
CONTAINERS = ("afd-oracle-neo4j-1", "afd-oracle-qdrant-1")


def project_root() -> Path:
    """The afd-oracle checkout (workspace root); override with AFD_ORACLE_HOME."""
    if os.environ.get("AFD_ORACLE_HOME"):
        return Path(os.environ["AFD_ORACLE_HOME"]).resolve()
    return Path(__file__).resolve().parents[3]


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def skill_path() -> Path:
    return claude_home() / "skills" / SKILL_NAME / "SKILL.md"


def claude_bin() -> str | None:
    return os.environ.get("AFD_ORACLE_CLAUDE_BIN") or shutil.which("claude")


def docker_bin() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    win = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
    return str(win) if win.exists() else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def skill_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- checks
def check_docker() -> dict:
    d = docker_bin()
    if not d:
        return {"ok": False, "detail": "Docker is not installed", "fix": None}
    try:
        out = subprocess.run([d, "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "detail": f"docker not responding: {e}", "fix": "infra"}
    if out.returncode != 0:
        return {"ok": False, "detail": "Docker Desktop is not running", "fix": "infra"}
    status = dict(line.split("\t", 1) for line in out.stdout.splitlines() if "\t" in line)
    containers = {c: status.get(c, "missing") for c in CONTAINERS}
    ok = all(v.startswith("Up") for v in containers.values())
    out_ = {"ok": ok, "detail": "; ".join(f"{c.split('-')[2]}: {v}" for c, v in containers.items()),
            "fix": None if ok else "infra"}
    if docker_autostart() is False:
        out_["warnings"] = ["Docker Desktop does not start when you log in, so after a restart the graph and vector "
                            "databases stay down until you open it. Turn on Docker Desktop > Settings > General > "
                            "\"Start Docker Desktop when you sign in\"."]
    return out_


def docker_autostart() -> bool | None:
    """Docker Desktop's "start when you sign in" setting (None when it can't be read, e.g. on Linux)."""
    base = Path(os.environ.get("APPDATA", "")) / "Docker" if os.name == "nt" else         Path.home() / "Library" / "Group Containers" / "group.com.docker"
    for name in ("settings-store.json", "settings.json"):
        try:
            data = json.loads((base / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("AutoStart", "autoStart"):
            if key in data:
                return bool(data[key])
    return None


def check_neo4j() -> dict:
    s = get_settings()
    try:
        drv = runtime.graph_driver(s, required=True)
        with drv.session(database=s.neo4j_database) as sess:
            sess.run("RETURN 1").consume()
        return {"ok": True, "detail": s.neo4j_uri}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200], "fix": "infra"}


def check_qdrant() -> dict:
    try:
        client, _ = runtime.vectors(get_settings())
        names = sorted(c.name for c in client.get_collections().collections)
        return {"ok": True, "detail": ", ".join(names) or "no collections yet"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200], "fix": "infra"}


def check_mcp() -> dict:
    """User-scope MCP servers live in ~/.claude.json under "mcpServers"."""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    entry = (data.get("mcpServers") or {}).get(MCP_NAME)
    if not entry:
        return {"ok": False, "detail": "not registered for your user (Claude can't see the knowledge base outside "
                                       "this folder)", "fix": "mcp"}
    args = " ".join(entry.get("args") or [])
    norm = lambda s: s.lower().replace("\\", "/")  # noqa: E731
    ok = norm(str(project_root())) in norm(args)
    return {"ok": ok, "detail": f"{entry.get('command')} {args}"[:200], "fix": None if ok else "mcp"}


def check_skill() -> dict:
    p = skill_path()
    if not p.exists():
        return {"ok": False, "detail": "skill not installed", "fix": "skill"}
    current = _sha(p.read_text(encoding="utf-8")) == _sha(skill_text())
    return {"ok": current, "detail": str(p) + ("" if current else " (outdated)"), "fix": None if current else "skill"}


def check_claude() -> dict:
    b = claude_bin()
    warn = []
    settings = claude_home() / "settings.json"
    try:
        env = (json.loads(settings.read_text(encoding="utf-8")).get("env") or {})
    except Exception:
        env = {}
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
        if env.get(k) or os.environ.get(k):
            warn.append(f"{k} is set: mining will be sent/billed there, not to your Claude subscription")
    return {"ok": bool(b) and not warn, "detail": b or "claude CLI not found on PATH (needed for mining)",
            "warnings": warn, "fix": None}


def check_service() -> dict:
    from . import service
    try:
        if service.installed():
            return {"ok": True, "detail": "starts at login and restarts on failure", "fix": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"could not query the service: {e}"[:200], "fix": "service"}
    return {"ok": False, "detail": "the dashboard only runs while something keeps it open; install it as a login "
                                   "service so it starts automatically and restarts on failure", "fix": "service"}


def health() -> dict:
    checks = {"docker": check_docker(), "neo4j": check_neo4j(), "qdrant": check_qdrant(),
              "claude": check_claude(), "mcp": check_mcp(), "skill": check_skill(), "commands": check_commands(),
              "service": check_service()}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks, "root": str(project_root())}


# ---------------------------------------------------------------------------- fixes
def _token() -> str:
    return secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")


def ensure_env_file() -> Path:
    """infra/.env with a generated Neo4j password and Qdrant API key. Existing values are kept; only an empty Qdrant
    key is filled in, because the Qdrant container treats an empty key as "required" and would refuse every client."""
    root = project_root()
    env = root / "infra" / ".env"
    if not env.exists():
        text = (root / "infra" / ".env.example").read_text(encoding="utf-8").replace("change-me-please", _token())
        env.write_text(text, encoding="utf-8")
    lines = env.read_text(encoding="utf-8").splitlines()
    keys = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in lines if "=" in ln and not ln.startswith("#")}
    if not keys.get("QDRANT_API_KEY") or not keys.get("AFD_ORACLE_QDRANT_API_KEY"):
        key = keys.get("QDRANT_API_KEY") or keys.get("AFD_ORACLE_QDRANT_API_KEY") or _token()
        out = []
        for ln in lines:
            name = ln.split("=", 1)[0]
            out.append(f"{name}={key}" if name in ("QDRANT_API_KEY", "AFD_ORACLE_QDRANT_API_KEY") else ln)
        env.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env


def infra_command() -> list[list[str]]:
    d = docker_bin()
    if not d:
        raise RuntimeError("Docker is not installed. Install Docker Desktop, start it, then try again.")
    root = project_root()
    env = ensure_env_file()
    return [[d, "compose", "-f", str(root / "infra" / "docker-compose.yml"), "--env-file", str(env), "up", "-d"]]


def install_mcp() -> dict:
    b = claude_bin()
    if not b:
        raise RuntimeError("The claude CLI was not found on PATH.")
    uv = shutil.which("uv") or "uv"
    subprocess.run([b, "mcp", "remove", MCP_NAME, "-s", "user"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = subprocess.run([b, "mcp", "add", "--scope", "user", MCP_NAME, "--", uv, "run", "--directory",
                          str(project_root()), "--quiet", "afd-oracle-mcp"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or "claude mcp add failed")
    return {"registered": True, "detail": out.stdout.strip()}


TOOL = "oracle"  # this tool's name under the shared /afd router


def _command_files() -> dict[Path, str]:
    """~/.claude/commands/afd.md (shared router) and ~/.claude/afd/oracle.md (this tool's action guide)."""
    afd_dir = claude_home() / "afd"
    tdir = TEMPLATE.parent
    router = (tdir / "afd.md").read_text(encoding="utf-8").replace("{{AFD_DIR}}", afd_dir.as_posix())
    guide = (tdir / f"{TOOL}.md").read_text(encoding="utf-8").replace("{{ROOT}}", project_root().as_posix())
    return {claude_home() / "commands" / "afd.md": router, afd_dir / f"{TOOL}.md": guide}


def check_commands() -> dict:
    files = _command_files()
    missing = [p for p in files if not p.exists()]
    if missing:
        return {"ok": False, "detail": f"/afd {TOOL} commands not installed", "fix": "skill"}
    current = all(_sha(p.read_text(encoding="utf-8")) == _sha(t) for p, t in files.items())
    return {"ok": current, "detail": f"/afd {TOOL} <action>" + ("" if current else " (outdated)"),
            "fix": None if current else "skill"}


def install_skill() -> dict:
    """Installs the skill and the /afd commands together (one 'Claude integration' step)."""
    p = skill_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(skill_text(), encoding="utf-8")
    written = [str(p)]
    for path, text in _command_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    return {"installed": written}
