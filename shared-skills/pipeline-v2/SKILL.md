# Pipeline V2 Core

This core implements the first converged pipeline-v2 batch:

- SQLite ledger with `cases`, `batches`, and `hits`
- evidence schema gates for `oracle_level=L1-L3` only
- structural fingerprints for agent-tested products
- reproduction verification channels
- executable L2 terminal-state oracles from adapter semantic maps
- observation-zone state machine
- sentinel-first triage funnel
- one-page batch brief and redline writeback
- command adapters for issue-tracker/wiki publication and triage sync
- M3 orchestrator skeleton with an executor protocol, mock executor, and
  offline dry-run queue
- M5 Playwright JSON reporter executor for adapter-provided spec runs

The core is product-neutral. Product repositories provide runtime signals,
adapter configuration, and publication command wrappers.

## L2 Oracle Contract

`pipeline_v2.oracle_l2` evaluates one semantic-map entry against a structured
`observed_state` dict produced by an executor. The executor is intentionally
behind `ExecutorProtocol`; the shipped Playwright executor supplies
spec-as-oracle results from adapter-provided specs, while MockExecutor remains
the offline contract fixture.

Each `expected_terminal_state` must include a machine-comparable check:

```json
{
  "assertion": "current_mode == target_mode",
  "assertion_type": "mode_state",
  "observable": "structured terminal mode",
  "machine_check": {
    "field": "current_mode",
    "op": "eq",
    "value_from": "target_mode"
  }
}
```

The oracle only compares structured terminal state. It does not compare agent
output text. Missing `machine_check`, unregistered `assertion_type`, missing
observed fields, and text-based checks return `inconclusive`; they must not be
filed as L2 failures.

## Orchestrator Contract

`pipeline_v2.orchestrator` wires the existing modules in the approved process
bus order:

sentinel check -> signal generation -> L4 suggestion isolation -> channel
assignment -> reproduction verification -> contract freshness -> evidence
integrity -> LLM veto -> reporting / observation exits -> ledger brief.

The executor boundary is `ExecutorProtocol.run(test_cmd, scenario)`, returning
structured `observed_state` plus the raw failure capture. `MockExecutor`
remains available for offline tests. `PlaywrightExecutor` runs an
adapter-provided spec with the JSON reporter and maps spec pass/fail to L2
when `ExecutionScenario.oracle_strategy == "playwright_spec"`. The
orchestrator must not import product drivers.

Offline dry-run writes publication intent to a JSONL queue instead of pushing
to an external issue tracker or wiki:

```bash
python3 -m pipeline_v2.orchestrator \
  --ledger /tmp/pipeline-v2.sqlite \
  --queue /tmp/pipeline-v2-queue.jsonl \
  --brief /tmp/pipeline-v2-brief.md
```

Adapter-driven Playwright dry-run reads `adapter.playwright.spec_whitelist`,
maps each item to an `ExecutionScenario`, and parses a stored JSON reporter
fixture without requiring the product service:

```bash
python3 -m pipeline_v2.orchestrator \
  --ledger /tmp/pipeline-v2-adapter.sqlite \
  --queue /tmp/pipeline-v2-adapter-queue.jsonl \
  --brief /tmp/pipeline-v2-adapter-brief.md \
  --executor playwright \
  --adapter products/<product>/adapter.config.json \
  --product-cwd /path/to/product \
  --dry-run \
  --report tests/fixtures/pipeline_v2/playwright/failure-report.json
```

Adapter-driven preflight checks runtime prerequisites without creating
ledger/queue/brief files. If an adapter declares
`playwright.required_browsers`, preflight runs
`npx playwright install --dry-run <browser>` from the adapter-provided
`web_subdir` and checks the reported install locations. This verifies the
managed browser bundle required by that Playwright version, not merely a
system browser installation.

For real Playwright runs, keep service URLs and auth/session coordinates in
adapter/runtime input. If the product's SSO or cookie domain rejects localhost,
export the product-approved test URL in the shell before invoking the
orchestrator; do not hard-code product domains in core.

```bash
python3 -m pipeline_v2.orchestrator \
  --preflight \
  --executor playwright \
  --adapter products/<product>/adapter.config.json \
  --product-cwd /path/to/product
```

Playwright executor dry-run parses a stored JSON reporter fixture without
requiring the product service:

```bash
python3 -m pipeline_v2.playwright_executor \
  --product-cwd /path/to/product \
  --web-subdir web \
  --config playwright.go-integration.config.ts \
  --spec manage-context.spec.ts \
  --dry-run \
  --report tests/fixtures/pipeline_v2/playwright/failure-report.json
```

## Model Routing Budget Contract

Model routing remains disabled unless the caller supplies both
`--enable-model-routing` and an absolute `--model-budget-overlay` JSON path.
The runtime overlay defines per-batch call/token/cost limits, per-call timeout,
consecutive-error stop, and mandatory telemetry. It must not contain provider
credentials. Missing/invalid telemetry, prompt-version drift, unknown verdicts,
provider errors, and exhausted budgets fail closed; the model can only return
`allow`, `veto`, or `request-more-evidence`.
