"""Supervised dashboard: keeps http://127.0.0.1:<port> up for as long as the user is logged in.

Started by the OS service (``afd-oracle service install``), never by a Claude session, so the dashboard does not die
with a terminal, a session or a background-task reaper. The supervisor:

* runs the server in a child process and restarts it whenever it exits (backoff 2 s → 60 s for crash loops, reset
  after 5 minutes of healthy uptime);
* never starts a second server: while something already answers ``/api/health`` on the port (for example a
  dashboard started by hand) it only watches, and takes over within 30 s if that one goes away;
* logs to ``data/logs/dashboard.log`` (rotated at 5 MB) and records its pid in ``data/logs/dashboard.pid`` so
  ``afd-oracle service stop`` can end the whole process tree.

Usage: ``python -m afd_oracle_ui.serve [--port 8765]``.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WATCH_INTERVAL_S = 30
HEALTHY_AFTER_S = 300
MAX_LOG_BYTES = 5_000_000


def log_dir() -> Path:
    from . import setup
    d = setup.project_root() / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def answering(port: int, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _open_log() -> "object":
    path = log_dir() / "dashboard.log"
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        path.replace(path.with_suffix(".log.1"))
    return open(path, "a", buffering=1, encoding="utf-8", errors="replace")


def _say(log, msg: str) -> None:
    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [supervisor] {msg}\n")


def supervise(port: int) -> None:
    from . import setup
    log = _open_log()
    sys.stdout = sys.stderr = log  # pythonw has no console; never let a print crash the supervisor
    (log_dir() / "dashboard.pid").write_text(str(os.getpid()))
    child: subprocess.Popen | None = None

    def stop(*_):
        _say(log, "stopping")
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, stop)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    code = (f"from afd_oracle_ui.app import main; main(port={port}, open_browser=False)")
    backoff = 2.0
    _say(log, f"supervisor started (pid {os.getpid()}), port {port}")
    while True:
        if answering(port):  # someone else (e.g. a hand-started dashboard) is serving: watch, don't compete
            time.sleep(WATCH_INTERVAL_S)
            continue
        started = time.monotonic()
        _say(log, "starting dashboard server")
        child = subprocess.Popen([sys.executable, "-c", code], cwd=str(setup.project_root()), stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
        rc = child.wait()
        ran = time.monotonic() - started
        _say(log, f"dashboard server exited with code {rc} after {ran:.0f}s")
        backoff = 2.0 if ran > HEALTHY_AFTER_S else min(60.0, backoff * 2)
        time.sleep(backoff)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8765)
    supervise(p.parse_args().port)


if __name__ == "__main__":
    main()
