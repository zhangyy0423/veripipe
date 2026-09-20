#!/usr/bin/env bash
set -euo pipefail

# Product-neutrality gate.
#
# This scanner is itself public, so it MUST NOT embed any origin-specific
# tokens (doing so would leak them). It enforces neutrality two ways:
#   1. A generic rule that fails on any real, non-allowlisted hostname URL
#      anywhere in the tracked tree (intranet hosts must never ship publicly).
#   2. An OPTIONAL, gitignored private denylist ($VERIPIPE_NEUTRALITY_DENYLIST,
#      default tests/contract/neutrality.denylist) whose ERE patterns are
#      applied to the tree. Absent in the public repo -> that rule is skipped;
#      a private mirror can drop it in to enforce origin-token absence.
# It then proves neutrality by construction: the core engine drives a neutral
# `sample` fixture end-to-end and must emit exactly one L2-backed queue event.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
SAMPLE_DIR="${ROOT}/tests/fixtures/products/sample"
SELF_REL="tests/contract/portability.sh"
DENYLIST="${VERIPIPE_NEUTRALITY_DENYLIST:-${ROOT}/tests/contract/neutrality.denylist}"

python3 - "$ROOT" "$SELF_REL" "$DENYLIST" <<'PY'
import os
import re
import subprocess
import sys

root, self_rel, denylist_path = sys.argv[1], sys.argv[2], sys.argv[3]

tracked = subprocess.run(
    ["git", "-C", root, "ls-files", "-z"],
    capture_output=True, check=True,
).stdout.decode("utf-8")
files = [f for f in tracked.split("\0") if f]
# Neutral host allowlist. No origin-specific tokens live here by design.
ALLOWED_EXACT = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "example.com", "example.net", "example.org",
    "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
    "codeload.github.com", "api.github.com", "docs.github.com",
    "pypi.org", "files.pythonhosted.org",
    "python.org", "www.python.org", "docs.python.org",
    "anthropic.com", "www.anthropic.com", "claude.com",
    "json-schema.org", "spdx.org", "opensource.org",
    "playwright.dev", "nodejs.org",
}
ALLOWED_SUFFIX = (
    ".example", ".test", ".invalid", ".localhost",
    ".example.com", ".example.net", ".example.org",
    ".github.com", ".githubusercontent.com",
)
URL_RE = re.compile(
    r"https?://([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
)

def host_allowed(host: str) -> bool:
    host = host.lower()
    if host in ALLOWED_EXACT:
        return True
    return any(host.endswith(suffix) for suffix in ALLOWED_SUFFIX)

def product_owned(rel: str) -> bool:
    parts = rel.split("/")
    return "products" in parts or "adapters" in parts

deny_patterns = []
if os.path.isfile(denylist_path):
    with open(denylist_path, encoding="utf-8") as handle:
        for raw in handle:
            token = raw.strip()
            if token and not token.startswith("#"):
                deny_patterns.append(re.compile(token))
denylist_abs = os.path.abspath(denylist_path)
violations = []
for rel in files:
    if rel == self_rel:
        continue
    absolute = os.path.abspath(os.path.join(root, rel))
    if absolute == denylist_abs:
        continue
    try:
        with open(absolute, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (UnicodeDecodeError, OSError):
        continue  # binary or unreadable file -> nothing textual to leak
    owned = product_owned(rel)
    for number, line in enumerate(lines, 1):
        for match in URL_RE.finditer(line):
            if not host_allowed(match.group(1)):
                violations.append(
                    f"{rel}:{number}: non-allowlisted host "
                    f"{match.group(1)!r} in {match.group(0)!r}"
                )
        if not owned:
            for pattern in deny_patterns:
                if pattern.search(line):
                    violations.append(
                        f"{rel}:{number}: matches private denylist /{pattern.pattern}/"
                    )

if violations:
    sys.stderr.write("portability FAIL: product-neutrality violations:\n")
    for item in violations:
        sys.stderr.write(f"  {item}\n")
    sys.exit(1)

summary = (
    f"{len(deny_patterns)} private denylist pattern(s)"
    if deny_patterns else "no private denylist present"
)
print(f"portability OK: whole-tree neutrality scan passed ({summary})")
PY

for required in \
  "$SAMPLE_DIR/adapter.config.json" \
  "$SAMPLE_DIR/semantic-map.yaml" \
  "$SAMPLE_DIR/product/web/playwright.config.ts" \
  "$SAMPLE_DIR/product/web/tests/integration/sample-contract.spec.ts"; do
  if [ ! -f "$required" ]; then
    printf 'portability FAIL: missing sample product fixture: %s\n' "${required#${ROOT}/}" >&2
    exit 1
  fi
done

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pipeline-v2-sample.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}" \
python3 -m pipeline_v2.orchestrator \
  --ledger "$TMP_ROOT/ledger.sqlite" \
  --queue "$TMP_ROOT/queue.jsonl" \
  --brief "$TMP_ROOT/brief.md" \
  --executor playwright \
  --adapter "$SAMPLE_DIR/adapter.config.json" \
  --semantic-map "$SAMPLE_DIR/semantic-map.yaml" \
  --product-cwd "$SAMPLE_DIR/product" \
  --dry-run \
  --report "${ROOT}/tests/fixtures/pipeline_v2/playwright/failure-report.json" \
  > "$TMP_ROOT/result.json"

python3 - "$TMP_ROOT/result.json" "$TMP_ROOT/queue.jsonl" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
queue = [json.loads(line) for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()]
assert result["reported_count"] == 1, result
assert result["oracle_levels"] == ["L2"], result
assert queue[0]["payload"]["semantic_map_entry_id"] == "sample.skill.contract", queue[0]
assert queue[0]["event_id"].startswith("pub-"), queue[0]
PY

printf 'portability OK: core is product-neutral\n'
