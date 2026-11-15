"""Run the dashboard as a per-user OS service: starts at login, restarts on failure, independent of any terminal or
Claude Code session.

* Windows: a Task Scheduler task (logon trigger, restart on failure, no time limit, runs hidden via pythonw).
* macOS: a launchd agent in ~/Library/LaunchAgents with KeepAlive.
* Linux: a systemd user unit with Restart=always.

In every case the service runs ``python -m afd_oracle_ui.serve`` (the supervisor), which restarts the server itself
and never competes with a dashboard that is already running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import setup
from .serve import answering, log_dir

NAME = "afd-oracle-dashboard"
MAC_LABEL = "dev.agenticfeverdream.oracle.dashboard"
PORT = 8765


def _python() -> str:
    """The workspace interpreter; on Windows the windowless one, so no console pops up at login."""
    root = setup.project_root()
    if os.name == "nt":
        for name in ("pythonw.exe", "python.exe"):
            p = root / ".venv" / "Scripts" / name
            if p.exists():
                return str(p)
    else:
        p = root / ".venv" / "bin" / "python"
        if p.exists():
            return str(p)
    return sys.executable


def command(port: int = PORT) -> list[str]:
    return [_python(), "-m", "afd_oracle_ui.serve", "--port", str(port)]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------------------------------------------------------------------- Windows
def _task_xml(port: int) -> str:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    exe, *args = command(port)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Oracle dashboard (afd-oracle): http://127.0.0.1:{port}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
    <!-- Watchdog: Task Scheduler's RestartOnFailure only covers failures to start, not a supervisor that is killed
         later. Re-running every minute with MultipleInstancesPolicy=IgnoreNew is a no-op while the supervisor
         lives and brings it back within a minute if it doesn't. -->
    <TimeTrigger><Enabled>true</Enabled><StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(exe)}</Command>
    <Arguments>{escape(subprocess.list2cmdline(args))}</Arguments>
    <WorkingDirectory>{escape(str(setup.project_root()))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def _win_installed() -> bool:
    return _run(["schtasks", "/Query", "/TN", NAME]).returncode == 0


# ---------------------------------------------------------------------------- macOS / Linux
def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{NAME}.service"


def _plist(port: int) -> str:
    args = "".join(f"<string>{escape(a)}</string>" for a in command(port))
    logf = escape(str(log_dir() / "dashboard.launchd.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{MAC_LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>WorkingDirectory</key><string>{escape(str(setup.project_root()))}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logf}</string>
  <key>StandardErrorPath</key><string>{logf}</string>
</dict></plist>
"""


def _unit(port: int) -> str:
    return f"""[Unit]
Description=Oracle dashboard (afd-oracle) on http://127.0.0.1:{port}

[Service]
WorkingDirectory={setup.project_root()}
ExecStart={subprocess.list2cmdline(command(port)) if os.name == "nt" else " ".join(command(port))}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


# ---------------------------------------------------------------------------- public API
def installed() -> bool:
    if os.name == "nt":
        return _win_installed()
    if sys.platform == "darwin":
        return _plist_path().exists()
    return _unit_path().exists()


def install(port: int = PORT, start_now: bool = True) -> dict:
    if os.name == "nt":
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(_task_xml(port))
            xml = f.name
        try:
            r = _run(["schtasks", "/Create", "/TN", NAME, "/XML", xml, "/F"])
        finally:
            os.unlink(xml)
        if r.returncode:
            raise RuntimeError(f"could not register the scheduled task: {(r.stderr or r.stdout).strip()}")
    elif sys.platform == "darwin":
        _plist_path().parent.mkdir(parents=True, exist_ok=True)
        _plist_path().write_text(_plist(port))
        _run(["launchctl", "unload", str(_plist_path())])
    else:
        _unit_path().parent.mkdir(parents=True, exist_ok=True)
        _unit_path().write_text(_unit(port))
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", NAME])
    if start_now:
        start()
    return status(port)


def start() -> None:
    if os.name == "nt":
        _run(["schtasks", "/Run", "/TN", NAME])
    elif sys.platform == "darwin":
        _run(["launchctl", "load", "-w", str(_plist_path())])
    else:
        _run(["systemctl", "--user", "start", NAME])


def stop() -> None:
    if os.name == "nt":
        _run(["schtasks", "/End", "/TN", NAME])
        pid_file = log_dir() / "dashboard.pid"
        if pid_file.exists():  # End stops the supervisor; make sure its server child goes too
            _run(["taskkill", "/T", "/F", "/PID", pid_file.read_text().strip()])
            pid_file.unlink(missing_ok=True)
    elif sys.platform == "darwin":
        _run(["launchctl", "unload", str(_plist_path())])
    else:
        _run(["systemctl", "--user", "stop", NAME])


def uninstall() -> dict:
    stop()
    if os.name == "nt":
        _run(["schtasks", "/Delete", "/TN", NAME, "/F"])
    elif sys.platform == "darwin":
        _plist_path().unlink(missing_ok=True)
    else:
        _run(["systemctl", "--user", "disable", NAME])
        _unit_path().unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    return status()


def status(port: int = PORT) -> dict:
    return {"installed": installed(), "running": answering(port), "url": f"http://127.0.0.1:{port}",
            "log": str(log_dir() / "dashboard.log"), "command": command(port)}
