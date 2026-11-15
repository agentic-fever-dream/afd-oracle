"""Dashboard API: add + ingest a repo through a real subprocess job, estimate, mine via a stub `claude`, release."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from conftest import git

H = {"X-AFD-Oracle-UI": "1"}
STUB = Path(__file__).with_name("stub_claude.py")


@pytest.fixture(scope="module")
def client(env, tmp_path_factory):
    from fastapi.testclient import TestClient

    from afd_oracle_ui import app as ui_app

    mp = pytest.MonkeyPatch()
    mp.setenv("AFD_ORACLE_UI_INGEST_FLAGS", "--no-graph --no-vectors")
    if sys.platform == "win32":  # an executable stand-in for the claude CLI
        stub = tmp_path_factory.mktemp("bin") / "claude.cmd"
        stub.write_text(f'@"{sys.executable}" "{STUB}" %*\n')
    else:
        stub = tmp_path_factory.mktemp("bin") / "claude"
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" "$@"\n')
        stub.chmod(0o755)
    mp.setenv("AFD_ORACLE_CLAUDE_BIN", str(stub))
    ui_app._jobs = None
    yield TestClient(ui_app.app)
    ui_app._jobs = None
    mp.undo()


def wait(client, job_id: str, timeout: float = 180) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running", "cancelling"):
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def test_mutations_need_the_ui_header(client):
    assert client.post("/api/repos", json={"source": "x"}).status_code == 403
    assert client.post("/api/setup/skill").status_code == 403


def test_add_ingest_mine_release(client, fixture_repo):
    path, _ = fixture_repo
    r = client.post("/api/repos", json={"source": str(path), "repo_id": "uifix"}, headers=H)
    assert r.status_code == 200, r.text
    job = wait(client, r.json()["id"])
    assert job["status"] == "succeeded", "\n".join(client.get(f"/api/jobs/{job['id']}").json()["log"][-20:])
    assert {"history", "snapshot", "metrics", "packets"} <= set(job["progress"])
    assert job["progress"]["history"]["done"] == job["progress"]["history"]["total"] > 0

    repo = client.get("/api/repos/uifix").json()
    assert repo["commits"] == int(git(path, "rev-list", "--all", "--count").strip())
    assert repo["ingested_at"] and repo["origin"] == str(Path(path).resolve())
    assert any(r["id"] == "uifix" for r in client.get("/api/repos").json())
    assert client.post("/api/repos", json={"source": str(path), "repo_id": "uifix"}, headers=H).status_code == 409

    est = client.get("/api/repos/uifix/estimate", params={"max": 3}).json()
    assert est["packets"] == 3 and est["estimated_tokens"] == 3 * est["tokens_per_packet"]
    assert sum(est["selected"].values()) == 3 and sum(1 for v in est["selected"].values() if v) >= 2  # round-robin

    run = client.post("/api/repos/uifix/mine", json={"max": 3}, headers=H)
    assert run.status_code == 200, run.text
    done = wait(client, run.json()["id"])
    assert done["status"] == "succeeded", done
    status = client.get(f"/api/repos/uifix/runs/{done['id']}").json()
    assert status["packets"] == {"done": 3} and status["insights"] == {"proposed": 1}
    assert done["summary"]["insights"] == {"proposed": 1}

    import os
    os.environ["STUB_CLAUDE_FAIL"] = "1"
    try:
        bad = wait(client, client.post("/api/repos/uifix/mine", json={"max": 2}, headers=H).json()["id"])
    finally:
        os.environ.pop("STUB_CLAUDE_FAIL")
    assert bad["status"] == "failed"
    assert client.get(f"/api/repos/uifix/runs/{bad['id']}").json()["packets"] == {"claimed": 2}
    assert client.post(f"/api/jobs/{bad['id']}/release", headers=H).json() == {"released": 2}
    assert client.get(f"/api/repos/uifix/runs/{bad['id']}").json()["packets"] == {"open": 2}

    ins = client.get("/api/repos/uifix/insights", params={"status": "proposed"}).json()
    assert [i["uid"] for i in ins] == [f"insight:stub-{done['id']}"]


def test_repo_id_and_folder_validation(client, tmp_path):
    assert client.post("/api/repos", json={"source": str(tmp_path / "nope")}, headers=H).status_code == 400
    assert client.post("/api/repos", json={"source": str(tmp_path), "repo_id": "bad id!"}, headers=H).status_code == 400
    assert client.get("/api/reports/..%2Fsecret.md").status_code in (400, 404)


def test_env_file_gets_secrets(tmp_path, monkeypatch):
    from afd_oracle_ui import setup
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / ".env.example").write_text(
        "NEO4J_AUTH=neo4j/change-me-please\nQDRANT_API_KEY=\nAFD_ORACLE_NEO4J_PASSWORD=change-me-please\n"
        "AFD_ORACLE_QDRANT_API_KEY=\n")
    monkeypatch.setenv("AFD_ORACLE_HOME", str(tmp_path))
    env = dict(line.split("=", 1) for line in setup.ensure_env_file().read_text().splitlines() if "=" in line)
    assert env["NEO4J_AUTH"].split("/")[1] == env["AFD_ORACLE_NEO4J_PASSWORD"] != "change-me-please"
    assert env["QDRANT_API_KEY"] and env["QDRANT_API_KEY"] == env["AFD_ORACLE_QDRANT_API_KEY"]
    before = setup.ensure_env_file().read_text()
    assert setup.ensure_env_file().read_text() == before  # idempotent: secrets are never regenerated


def test_explore_validates_input(client, fixture_repo):
    # Unknown repository and malformed uids are rejected before touching Neo4j or Qdrant.
    assert client.get("/api/repos/nope/explore/node", params={"uid": "file:nope:a.py"}).status_code == 404
    repo = client.get("/api/repos").json()[0]["id"]
    assert client.get(f"/api/repos/{repo}/explore/node", params={"uid": "bogus:x"}).status_code == 400
    assert client.get(f"/api/repos/{repo}/explore/search", params={"q": "x", "scope": "nope"}).status_code == 400
    assert client.get(f"/api/repos/{repo}/explore/neighbors",
                      params={"uid": "file:x:a.py", "type": "X]-(m) DETACH DELETE m //"}).status_code == 400
    assert client.get(f"/api/repos/{repo}/explore/similar", params={"uid": "file:x:a.py"}).status_code == 400
    assert client.get(f"/api/repos/{repo}/explore/map", params={"collection": "nope"}).status_code == 400
