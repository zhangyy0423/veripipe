#!/usr/bin/env bash
set -euo pipefail

# Contract: the product-neutral `external` executor drives one adapter-provided
# command's structured observed_state through the standard L2 machine_check
# oracle and emits exactly one L2-backed queue event. Neutral fixtures only:
# no product source, hosts, or credentials.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
FX="${ROOT}/tests/fixtures/pipeline_v2/external"

for required in \
  "$FX/adapter.external.json" \
  "$FX/semantic-map.yaml" \
  "$FX/observed-states.json"; do
  if [ ! -f "$required" ]; then
    printf 'external FAIL: missing fixture: %s\n' "${required#${ROOT}/}" >&2
    exit 1
  fi
done

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-v2-external.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}" \
python3 -m pipeline_v2.orchestrator \
  --ledger "$TMP_ROOT/ledger.sqlite" \
  --queue "$TMP_ROOT/queue.jsonl" \
  --brief "$TMP_ROOT/brief.md" \
  --executor external \
  --adapter "$FX/adapter.external.json" \
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
assert result["reported_count"] == 1, result
assert result["oracle_levels"] == ["L2"], result
bug = next(e for e in queue if e["kind"] == "bug_report")
assert bug["payload"]["semantic_map_entry_id"] == "sample.external.contract", bug
assert bug["payload"]["oracle_level"] == "L2", bug
assert bug["event_id"].startswith("pub-"), bug
PY

printf 'external OK: external executor emits one L2-backed queue event\n'
