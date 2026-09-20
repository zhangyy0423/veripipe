#!/usr/bin/env bash
set -euo pipefail

# A third, differently-shaped neutral product ("svc") driven end-to-end through
# the same core. It uses an all_true multi-field check (health across db/cache/
# queue) plus an eq release check — different oracle shapes from the sample,
# external, and notes fixtures. One contract fails (queue not healthy), so the
# core files exactly one L2 finding. No product source, hosts, or credentials.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
FX="${ROOT}/tests/fixtures/products/svc"

for required in "$FX/adapter.config.json" "$FX/semantic-map.yaml" "$FX/observed-states.json"; do
  if [ ! -f "$required" ]; then
    printf 'third-product FAIL: missing fixture: %s\n' "${required#${ROOT}/}" >&2
    exit 1
  fi
done

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-v2-svc.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}" \
python3 -m pipeline_v2.orchestrator \
  --ledger "$TMP_ROOT/ledger.sqlite" \
  --queue "$TMP_ROOT/queue.jsonl" \
  --brief "$TMP_ROOT/brief.md" \
  --executor external \
  --adapter "$FX/adapter.config.json" \
  --semantic-map "$FX/semantic-map.yaml" \
  --dry-run \
  --report "$FX/observed-states.json" \
  > "$TMP_ROOT/result.json"

python3 - "$TMP_ROOT/result.json" "$TMP_ROOT/queue.jsonl" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
queue = [json.loads(line) for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()]
assert result["executed_count"] == 2, result
assert result["reported_count"] == 1, result
assert result["oracle_levels"] == ["L2"], result
bug = next(e for e in queue if e["kind"] == "bug_report")
assert bug["payload"]["semantic_map_entry_id"] == "svc.health.all-green", bug
PY

printf 'third-product OK: an all_true-shaped neutral product runs end-to-end (1 pass, 1 filed)\n'
