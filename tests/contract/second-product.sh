#!/usr/bin/env bash
set -euo pipefail

# Product-neutrality by demonstration: a SECOND, unrelated neutral fixture
# product ("notes") is driven end-to-end through the same core via the external
# executor. Two contracts run; one passes (note_count >= 1) and one fails
# (tags must contain "urgent"), so the core files exactly one L2 finding for the
# failing contract. No product source, hosts, or credentials.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
FX="${ROOT}/tests/fixtures/products/notes"

for required in "$FX/adapter.config.json" "$FX/semantic-map.yaml" "$FX/observed-states.json"; do
  if [ ! -f "$required" ]; then
    printf 'second-product FAIL: missing fixture: %s\n' "${required#${ROOT}/}" >&2
    exit 1
  fi
done

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-v2-notes.XXXXXX")"
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
assert bug["payload"]["semantic_map_entry_id"] == "notes.note.tagged", bug
PY

printf 'second-product OK: a second neutral product runs end-to-end (1 pass, 1 filed)\n'
