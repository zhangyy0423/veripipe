#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}"

# Engine unit suites run in the dedicated unit gate; this contract asserts the
# Playwright executor honours the dry-run report contract without a service.
python3 -m pipeline_v2.playwright_executor \
  --product-cwd "${ROOT}/tests/fixtures/pipeline_v2" \
  --web-subdir "." \
  --config playwright.go-integration.config.ts \
  --spec manage-context.spec.ts \
  --dry-run \
  --report "${ROOT}/tests/fixtures/pipeline_v2/playwright/failure-report.json" >/dev/null
