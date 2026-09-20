#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}"

# Drive the real `python3 -m pipeline_v2_mcp` stdio process end-to-end: MCP
# handshake, tool discovery, an offline mock batch, and a read-only ledger query.
python3 - "$ROOT" <<'PY'
import json
import os
import subprocess
import sys
import tempfile

root = sys.argv[1]
env = dict(os.environ)

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.sqlite")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "contract", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "run_batch", "arguments": {
             "executor": "mock", "dry_run": True, "ledger": ledger,
             "queue": os.path.join(tmp, "queue.jsonl"),
             "brief": os.path.join(tmp, "brief.md"), "batch_id": "contract-001"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "query_ledger", "arguments": {
             "ledger": ledger, "op": "recent_batches", "limit": 5}}},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline_v2_mcp"],
        input=stdin, text=True, capture_output=True, env=env, cwd=root,
    )
    assert proc.returncode == 0, proc.stderr
    responses = {}
    for line in proc.stdout.splitlines():
        obj = json.loads(line)
        if obj.get("id") is not None:
            responses[obj["id"]] = obj

    init = responses[1]["result"]
    assert init["protocolVersion"] == "2025-06-18", init
    assert "tools" in init["capabilities"], init

    names = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert {"preflight", "run_batch", "run_loop", "query_ledger"} <= names, names

    batch = responses[3]["result"]
    assert batch["isError"] is False, batch
    assert batch["structuredContent"]["returncode"] == 0, batch

    query = responses[4]["result"]
    assert query["isError"] is False, query
    batch_ids = [row["batch_id"] for row in query["structuredContent"]["result"]]
    assert "contract-001" in batch_ids, batch_ids

print("pipeline_v2_mcp contract OK")
PY
