# Changelog

All notable changes to this project are documented here. The format is based on
the "Keep a Changelog" convention and the project aims to follow semantic
versioning.

## [Unreleased]

### Added
- **`external` executor.** A product-neutral `--executor external` that runs a
  product-provided command (from the owner-gated `adapter.external` block)
  returning structured `observed_state` JSON; judged by the standard L2
  `machine_check` oracle. A `--dry-run --report` fixture replays recorded states
  offline. Lets a product with a custom transport (e.g. a WebSocket RPC mux)
  join the pipeline through the standard CLI without adding product code to the
  core.
- **`publish-github` skill.** Turns machine-verified queue `bug_report` events
  into GitHub Discussion/Issue drafts. Dry-run by default (posting needs
  `--post` and `--yes`); eligible findings are `bug_report` with
  `oracle_level ∈ {L1,L2,L3}` and `llm_involvement == none`; deduped by
  fingerprint, capped, neutral bodies, automated-disclosure footer. The `gh`
  boundary is injectable and covered by offline unit tests.
- **Methodology.** `docs/methodology.md` writes up the anti-false-positive
  method on its own terms (the one rule, the L1–L3 ladder with L4 quarantined,
  veto-only models, fail-closed, structured-state-not-text, neutral engine,
  low-noise publishing, worked examples).
- **Runnable demo.** `examples/demo.py` walks one contract through three agent
  runs (pass / machine-verified failure / inconclusive) on neutral fixtures,
  with an offline test.
- **Funnel summary.** `pipeline_v2.fp_report` aggregates orchestrator result
  JSON(s) into an auditable number: candidate checks executed, filed as
  machine-verified findings, and withheld (with rates).
- **GitHub Action.** A composite `action.yml` writes the funnel summary to the
  CI job summary and can fail a step on any filed finding (`fail-on-filed`).
- **Second neutral product.** A `notes` fixture and `second-product.sh` contract
  drive a second, unrelated product end-to-end through the same core.
- **Docs.** `docs/adapters.md` documents the executor matrix and the
  bring-your-own-transport pattern; `docs/publish-github.md` and
  `docs/github-action.md` added; README carries a Documentation index and a
  60-second demo in the Quickstart.
- **Adoption guide.** `docs/adoption.md` (try / install / run / wire an MCP host
  / point at a product / gate CI) plus a `CONTRIBUTING.md` and issue/PR
  templates; the README first screen states value, audience, and a one-command
  try.
- **Demo material.** `examples/demo.cast` (an asciinema recording of the real
  demo) and `examples/record-demo.sh`.
- **Funnel trend.** `pipeline_v2.fp_report --trend` shows filed/withheld per
  batch over time with an aggregate row.
- **CI.** Contract gates `tests/contract/external.sh`,
  `tests/contract/second-product.sh`, and `tests/contract/third-product.sh`
  exercise the `external` executor end-to-end against three differently-shaped
  neutral fixtures (`notes`: count/contains_key; `svc`: all_true/eq).

### Changed
- Declared **PyYAML** as the single runtime dependency (used to load
  semantic/code maps) and install it in CI; corrected the earlier
  "standard-library only (core)" wording. The MCP server remains
  standard-library only (zero third-party dependencies).

## [0.1.0]

### Added
- Product-neutral verification core (`pipeline_v2`): L1–L3 oracle chain,
  structured L2 terminal-state oracle, asymmetric LLM veto, fail-closed
  behaviour, SQLite ledger, funnel, one-page batch brief.
- Guarded two-phase publishing to an offline JSONL queue.
- Zero-dependency MCP stdio server (`pipeline_v2_mcp`).
- Manifest-driven, fail-closed installer with symmetric Claude Code / Codex /
  dsh host thin-shells.
- Executors: `mock`, `playwright` (spec-as-oracle), `http` (L3 + bounded L1),
  `l1-fuzz`.
- CI gates: bash-syntax, python-syntax, secret-scan, contract, unit; GitHub
  Actions matrix across Python 3.9–3.12.
