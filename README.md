# veripipe

**An anti-false-positive verification pipeline for AI agents that test web/HTTP products.**

AI agents are good at finding candidate bugs but not at proving them.
Unchecked, they file confident, well-formed reports for behaviour that is in
fact correct, and triaging those often costs more than the real defects.
`veripipe` passes a candidate finding only when structured, reproducible
evidence survives a fail-closed oracle chain.

The core is Python with a single runtime dependency — **PyYAML**, used to load
semantic/code maps — and ships with a manifest-driven installer and a
zero-dependency MCP server so any MCP host can drive it.

**Who it's for:** teams running AI agents against web/HTTP products who are
tired of triaging confident-but-wrong bug reports.

**Try it in one command** (offline, no install, no product):

```bash
python3 examples/demo.py
```

One contract, three agent runs — a real success, a false "done," and an
unbacked claim — and only the machine-verified failure is filed.

---

## The problem it solves

A raw agent loop tends to fail in three ways:

1. **Text-matching oracles.** "The page says error" is not a bug. Comparing
   agent prose or rendered text produces confident nonsense.
2. **Unreproducible one-shots.** A finding that cannot be reproduced is not a
   finding.
3. **LLM self-confirmation.** An LLM asked "is this a bug?" will happily agree
   with itself.

`veripipe` answers each of these with a specific mechanism, described next.

## Core concepts

- **L1–L4 oracle model.** Findings are graded by the strength of the oracle
  that judged them. Only **L1–L3** (structural / terminal-state / reproduction
  evidence) may be filed. **L4** (suggestion / heuristic) is isolated and can
  never become a report on its own.
- **Structured terminal-state oracles (L2).** The oracle compares
  *machine-comparable terminal state* from a semantic map, never agent output
  text. Missing `machine_check`, unknown assertion types, missing observed
  fields, or text-based checks return `inconclusive` — they are not failures.
- **Asymmetric LLM veto.** When a model is in the loop it may only return
  `allow`, `veto`, or `request-more-evidence`. **It can veto a finding but can
  never confirm one.** Confirmation always comes from deterministic evidence.
- **Fail-closed everywhere.** Missing telemetry, drifted contracts, unknown
  verdicts, provider errors, and exhausted budgets all resolve to "do not
  file," not "file anyway."
- **SQLite ledger.** Cases, batches, and hits are persisted so runs are
  auditable and queryable after the fact.
- **Guarded two-phase publishing.** Offline dry-run writes publication *intent*
  to a JSONL queue. It never pushes to an external tracker or wiki by itself.
- **Pluggable executors.** Built-in `mock`, `playwright`, `http`, and `l1-fuzz`
  executors, plus a generic **`external`** executor that lets a product plug any
  transport — it just returns structured `observed_state` — through the standard
  CLI, without adding product code to the neutral core.
- **GitHub publishing skill.** `publish-github` turns machine-verified findings
  into GitHub Discussion/Issue drafts: dry-run by default, verified-only,
  deduped, each with an automated-disclosure footer. See
  [`docs/publish-github.md`](docs/publish-github.md).
- **Product-neutral by contract.** The core imports no product drivers and
  contains no product names or identifiers; a CI gate greps for leaks and runs
  the engine against a neutral `sample` fixture.

## Architecture

```
          product repo (private)                     veripipe (this repo, public)
   ┌────────────────────────────────┐        ┌────────────────────────────────────┐
   │ adapter.config.json            │        │ pipeline_v2/      product-neutral   │
   │ semantic-map.yaml              │  ───▶  │   orchestrator, oracles L1–L3,      │
   │ Playwright specs / web app     │ signals│   ledger, funnel, publishing, …     │
   │ runtime URLs, auth (never here)│        │ pipeline_v2_mcp/  MCP stdio server  │
   └────────────────────────────────┘        │ scripts/installer manifest installer│
                                              └────────────────────────────────────┘
```

The product repository supplies runtime signals, adapter configuration, and
publication command wrappers. This repository supplies the neutral engine, the
MCP surface, the installer, and the tests. Product source, internal prompts,
intranet addresses, project identifiers, run logs, queue payloads, credentials,
and unreviewed product knowledge never live here.

## Requirements

- Python **3.9+**. The core needs **PyYAML** (its only runtime dependency);
  the MCP server is standard-library only.
- Optional, only for real browser runs: Node.js + Playwright in the *product*
  repo. `veripipe` shells out to the product's Playwright install; it does not
  bundle a browser.

## Install

From source (recommended while unpublished):

```bash
git clone <this-repo> veripipe
cd veripipe
# The core is importable directly from the scripts root:
export PYTHONPATH="$PWD/shared-skills/pipeline-v2/scripts:$PYTHONPATH"
python3 -m pipeline_v2.orchestrator --help
```

As a package (once published):

```bash
pip install veripipe          # installs pipeline_v2 + pipeline_v2_mcp (pulls PyYAML)
```

## Quickstart

Everything below is **offline and side-effect-free** (`--dry-run` writes intent
to a queue file instead of pushing anywhere).

See the method in ~60 seconds on the neutral fixtures (no product, no network):

```bash
python3 examples/demo.py
```

It walks one contract through three agent runs — a real success, a false "done,"
and an unbacked claim — and shows that only the machine-verified failure is
filed. See [`docs/methodology.md`](docs/methodology.md) for the reasoning.

Run the orchestrator against the mock executor:

```bash
python3 -m pipeline_v2.orchestrator \
  --ledger /tmp/pipeline-v2.sqlite \
  --queue /tmp/pipeline-v2-queue.jsonl \
  --brief /tmp/pipeline-v2-brief.md
```

Adapter-driven Playwright dry-run (parses a stored JSON reporter fixture, no
product service required):

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

Preflight an adapter's runtime prerequisites without writing any ledger/queue
files:

```bash
python3 -m pipeline_v2.orchestrator \
  --preflight \
  --executor playwright \
  --adapter products/<product>/adapter.config.json \
  --product-cwd /path/to/product
```

Model routing stays **disabled** unless you pass both `--enable-model-routing`
and an absolute `--model-budget-overlay` JSON path. The overlay defines
per-batch call/token/cost limits and must not contain provider credentials.

## MCP server

Any MCP host (Ducc, Claude Code, Codex, or your own client) can drive the
pipeline over stdio. The server is **zero-dependency** — it implements the
JSON-RPC 2.0 stdio transport directly, so it runs anywhere the core runs.

```bash
python3 -m pipeline_v2_mcp
```

- Server name: `veripipe-pipeline-v2`.
- Protocol versions: `2025-06-18` (default), `2025-03-26`, `2024-11-05`.
- Transport: newline-delimited JSON-RPC on stdin/stdout; logs go to stderr.

### Tools

| Tool | Purpose | Default safety |
| --- | --- | --- |
| `preflight` | Check an adapter's runtime prerequisites | read-only |
| `run_batch` | Run one verification batch | `dry_run=true` |
| `run_loop` | Run the multi-iteration loop runner | `dry_run=true` |
| `knowledge_audit` | Audit exported knowledge for gaps/drift | read-only |
| `knowledge_export` | Export engine knowledge (no product data) | read-only |
| `code_map_validate` | Validate a code map, fail-closed on drift | read-only |
| `change_impact` | Compute change impact from a code map | read-only |
| `query_ledger` | Query batches/cases/hits from a ledger | read-only |

`run_batch` and `run_loop` default to `dry_run=true`; publishing requires an
explicit opt-out and still only writes to the local queue. `query_ledger`
refuses to run if the ledger file does not already exist — it never creates an
empty database as a side effect.

## Product adapters & the installer

A product plugs in by providing an **adapter** (`adapter.config.json`,
`semantic-map.yaml`, spec whitelist) and optional agent *thin-shells*. The
manifest-driven installer copies a product's thin-shells into the product
working directory and tracks every managed file by SHA-256 so upgrades and
uninstalls are safe:

```bash
python3 -m scripts.installer.installer --product <product> --product-cwd /path/to/product
python3 -m scripts.installer.installer doctor --product <product> --product-cwd /path/to/product
```

The installer is **fail-closed**: it rejects path traversal and symlink escape,
refuses to touch its own sibling checkout, protects locally modified files
(overwrite needs `--force`), removes only files it recorded in the manifest, and
never deletes unmanaged runtime evidence.

Agent thin-shells today target **Claude Code** (`.claude/commands`,
`.claude/skills`) and **Codex** (`.codex/skills`). A **Ducc / dsh** thin-shell
mirrors the same symmetric design (`.dsh/skills`). See
[`docs/adapters.md`](docs/adapters.md) for the adapter layout and host mapping,
and [`docs/dsh-plugin.md`](docs/dsh-plugin.md) to wire the skills and MCP
server into a dsh host.

> **Engine knowledge travels; product knowledge does not.** The `code_map`,
> `knowledge_audit`, and `knowledge_export` capabilities are part of the neutral
> engine and move with the framework. Any product-specific graph, map, or
> knowledge content is generated in the product repo and stays private.

## Documentation

- [`docs/methodology.md`](docs/methodology.md) — the anti-false-positive method
  the whole project is built on (readable on its own).
- [`docs/adoption.md`](docs/adoption.md) — try it, wire it into a host, point it at your product.
- [`docs/adapters.md`](docs/adapters.md) — adapter layout, host mapping, the
  executor matrix, and the bring-your-own-transport pattern.
- [`docs/publish-github.md`](docs/publish-github.md) — filing machine-verified
  findings to GitHub Discussions/Issues (dry-run first, verified-only).
- [`docs/github-action.md`](docs/github-action.md) — a composite Action that summarizes a run in CI.
- [`docs/testing.md`](docs/testing.md) — the gate suite and the product-bound
  test hold-out strategy.
- [`docs/dsh-plugin.md`](docs/dsh-plugin.md) — wiring the skills and the MCP
  server into a dsh host.

## Testing & CI

All gates run offline. PyYAML is the only third-party dependency (it loads the
semantic/code maps); everything else is the standard library:

```bash
bash scripts/ci/run-gates.sh
```

Gates: `bash-syntax`, `python-syntax`, `secret-scan`, `contract` (Playwright
dry-run + MCP server smoke + product-neutrality scan), and `unit`
(installer + engine + MCP server suites). A handful of product-bound suites are
intentionally held out of this public repo; see
[`docs/testing.md`](docs/testing.md) for the strategy.

## Security & confidentiality

This repository is deliberately free of product source, internal prompts,
intranet addresses, project identifiers, run logs, queue payloads, and
credentials. A `secret-scan` gate and a product-neutrality gate enforce the
boundary in CI. Internal knowledge belongs in a separate private knowledge
repository with its own clean history.

## License

[MIT](LICENSE) © 2026 veripipe contributors.
