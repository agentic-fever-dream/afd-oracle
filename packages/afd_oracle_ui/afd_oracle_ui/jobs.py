"""Background jobs: one subprocess per job (ingest, mine, setup), never through a shell.

State lives in SQLite (data/ui.sqlite) and each job's output in data/jobs/<id>.log, so the dashboard survives a restart
and the browser can tail any job. Stdout lines that are JSON are understood:
  * `afd-oracle ingest --progress-json`: {"event": "progress", "step", "done", "total"}, {"event": "result", "result"}
  * `claude -p --output-format stream-json`: {"type": "result", "result": "...", "total_cost_usd": ...}
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, repo TEXT, status TEXT NOT NULL, title TEXT,
  args TEXT, progress TEXT DEFAULT '{}', summary TEXT, error TEXT, pid INTEGER,
  created REAL, started REAL, ended REAL
);
CREATE INDEX IF NOT EXISTS jobs_repo ON jobs(repo, created);
CREATE TABLE IF NOT EXISTS run_packets (run_id TEXT NOT NULL, repo TEXT NOT NULL, packet_id TEXT NOT NULL,
  PRIMARY KEY (run_id, packet_id));
"""
ACTIVE = ("queued", "running")


class JobManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.log_dir = self.data_dir / "jobs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.data_dir / "ui.sqlite"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}
        with self.lock:  # jobs whose process is gone (dashboard or CLI stopped mid-run) can never finish
            stale = [r["id"] for r in self.db.execute(
                "SELECT id, pid FROM jobs WHERE status IN ('queued','running','cancelling')")
                if not r["pid"] or not _pid_alive(r["pid"])]
            self.db.executemany("UPDATE jobs SET status='failed', error='stopped: its process ended', ended=? "
                                "WHERE id=?", [(time.time(), i) for i in stale])
            self.db.commit()

    # ------------------------------------------------------------------ queries
    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("args", "progress", "summary"):
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k == "progress" else None)
        return d

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self, repo: str | None = None, limit: int = 50) -> list[dict]:
        sql, params = "SELECT * FROM jobs", []
        if repo:
            sql += " WHERE repo=?"
            params.append(repo)
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY created DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._row(r) for r in rows]

    def active(self, repo: str, kinds: tuple[str, ...] | None = None) -> dict | None:
        for j in self.list(repo, 20):
            if j["status"] in ACTIVE and (kinds is None or j["kind"] in kinds):
                return j
        return None

    def log_path(self, job_id: str) -> Path:
        return self.log_dir / f"{job_id}.log"

    def _update(self, job_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v, default=str) if k in ("args", "progress", "summary") else v for k, v in fields.items()]
        with self.lock:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*vals, job_id))
            self.db.commit()

    # ------------------------------------------------------------------ run
    def start(self, kind: str, repo: str | None, title: str, cmd: list[str], cwd: Path,
              args: dict | None = None, env: dict | None = None, job_id: str | None = None,
              on_exit: Callable[[dict, int], dict | None] | None = None) -> dict:
        job_id = job_id or f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        with self.lock:
            self.db.execute("INSERT INTO jobs(id, kind, repo, status, title, args, created) VALUES (?,?,?,?,?,?,?)",
                            (job_id, kind, repo, "queued", title, json.dumps(args or {}), time.time()))
            self.db.commit()
        threading.Thread(target=self._run, args=(job_id, cmd, cwd, env, on_exit), daemon=True).start()
        return self.get(job_id)

    def _run(self, job_id: str, cmd: list[str], cwd: Path, env: dict | None, on_exit) -> None:
        log = self.log_path(job_id).open("a", encoding="utf-8")
        progress: dict[str, dict] = {}
        summary: dict | None = None
        try:
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}, **kwargs, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.write(f"could not start {cmd[0]}: {e}\n")
            log.close()
            self._update(job_id, status="failed", error=f"could not start {cmd[0]}: {e}", ended=time.time())
            return
        self.procs[job_id] = proc
        self._update(job_id, status="running", pid=proc.pid, started=time.time())
        last_flush = 0.0
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip("\n")
            event = None
            if text.startswith("{"):
                try:
                    event = json.loads(text)
                except ValueError:
                    event = None
            if isinstance(event, dict):
                if event.get("event") == "progress":
                    progress[event["step"]] = {"done": event.get("done", 0), "total": event.get("total", 0),
                                               "at": time.time()}
                    text = None
                elif event.get("event") == "log":
                    text = event.get("msg", "")
                elif event.get("event") == "result":
                    summary = event.get("result")
                    text = "ingest finished"
                elif event.get("type") == "result":  # claude -p stream-json final message
                    summary = {"result": event.get("result"), "cost_usd": event.get("total_cost_usd"),
                               "is_error": event.get("is_error"), "duration_ms": event.get("duration_ms")}
                    text = f"claude finished ({event.get('subtype')})"
                elif "type" in event:  # other stream-json chatter: keep the log readable
                    text = _describe_stream_event(event)
            if text:
                log.write(text + "\n")
                log.flush()
            if time.time() - last_flush > 0.5:
                self._update(job_id, progress=progress)
                last_flush = time.time()
        rc = proc.wait()
        log.close()
        self.procs.pop(job_id, None)
        job = self.get(job_id) or {}
        cancelled = job.get("status") == "cancelling"
        status = "cancelled" if cancelled else ("succeeded" if rc == 0 else "failed")
        if summary and summary.get("is_error"):
            status = "failed"
        self._update(job_id, progress=progress, summary=summary, status=status, ended=time.time(),
                     error=None if status == "succeeded" else (job.get("error") or f"exit code {rc}"))
        if on_exit:
            try:
                extra = on_exit(self.get(job_id) or {}, rc)
                if extra:
                    self._update(job_id, summary={**(summary or {}), **extra})
            except Exception as e:  # never lose the job record over a bookkeeping error
                self._update(job_id, error=f"post-processing failed: {e}")

    def cancel(self, job_id: str) -> dict | None:
        proc = self.procs.get(job_id)
        if proc is None:
            return self.get(job_id)
        self._update(job_id, status="cancelling")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.kill()
        return self.get(job_id)

    # ------------------------------------------------------------------ mining bookkeeping
    def record_run_packets(self, run_id: str, repo: str, packet_ids: list[str]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO run_packets VALUES (?,?,?)",
                                [(run_id, repo, p) for p in packet_ids])
            self.db.commit()

    def run_packets(self, run_id: str) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT packet_id FROM run_packets WHERE run_id=?", (run_id,))]


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # os.kill(pid, 0) would send CTRL_C_EVENT on Windows
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_and_stream(jm: "JobManager", job_id: str, out=print, poll: float = 0.5) -> dict:
    """Block until a job ends, echoing its log as it grows (used by the CLI)."""
    pos = 0
    while True:
        p = jm.log_path(job_id)
        if p.exists():
            with p.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            for line in chunk.splitlines():
                out(line)
        job = jm.get(job_id) or {}
        if job.get("status") not in (*ACTIVE, "cancelling"):
            return job
        time.sleep(poll)


def _describe_stream_event(e: dict) -> str | None:
    t = e.get("type")
    if t == "system" and e.get("subtype") == "init":
        return f"claude session started (model {e.get('model')})"
    if t == "assistant":
        parts = (e.get("message") or {}).get("content") or []
        out = []
        for p in parts:
            if p.get("type") == "text" and p.get("text", "").strip():
                out.append(p["text"].strip()[:400])
            elif p.get("type") == "tool_use":
                out.append(f"-> {p.get('name')}")
        return " ".join(out) or None
    return None
