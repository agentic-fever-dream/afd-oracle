"""Stand-in for `claude -p` in dashboard tests: reads the run args file named in the prompt, records one proposed
insight for that run (as the real workflow's analyst would), and prints a stream-json result line.
Set STUB_CLAUDE_FAIL=1 to simulate a failed run."""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

prompt = sys.argv[sys.argv.index("-p") + 1]
args_file = Path(re.search(r"Read the file (.+?\.json)", prompt).group(1))
args = json.loads(args_file.read_text())
print(json.dumps({"type": "system", "subtype": "init", "model": "stub"}), flush=True)
if os.environ.get("STUB_CLAUDE_FAIL") == "1":
    print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "boom"}), flush=True)
    sys.exit(1)
store = Path(os.environ["AFD_ORACLE_DATA_DIR"]) / "repos" / args["repo"] / "store.sqlite"
db = sqlite3.connect(store)
now = time.time()
db.execute("INSERT INTO insights(uid, label, kind, title, summary, severity, confidence, status, citations, "
           "workflow_run, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (f"insight:stub-{args['run_id']}", "AntiPattern", "hidden_coupling", "stub", "stub", "low", 0.5,
            "proposed", "{}", args["run_id"], now, now))
db.commit()
n = sum(len(args[k]) for k in ("coupling_ids", "graveyard_ids", "pattern_ids"))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                  "result": json.dumps({"packets": n})}), flush=True)
