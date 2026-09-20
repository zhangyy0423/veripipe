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
- **Docs.** `docs/adapters.md` now documents the executor matrix and the
  bring-your-own-transport pattern.
- **CI.** A `tests/contract/external.sh` gate exercises the `external` executor
  end-to-end against neutral fixtures.

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
