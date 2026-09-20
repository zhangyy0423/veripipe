# Testing & gate strategy

`veripipe` is verified entirely offline with the Python standard library. One
entry point runs every gate:

```bash
bash scripts/ci/run-gates.sh
```

The script exports `PYTHONPATH` to the scripts root so both `pipeline_v2` and
`pipeline_v2_mcp` import cleanly, then runs the gates below in order. Any gate
failure stops the run (`set -euo pipefail`).

## Gates

| Gate | What it checks |
| --- | --- |
| `bash-syntax` | `bash -n` on every tracked `*.sh` file |
| `python-syntax` | `py_compile` on every tracked `*.py` file |
| `secret-scan` | `scripts/ci/check-secrets.py` — no credentials/tokens committed |
| `contract` | Every `tests/contract/*.sh` script (see below) |
| `unit` | Every directory containing `test_*.py` under `tests/unit` |

### Contract suites (`tests/contract/`)

- **`pipeline_v2.sh`** — drives the Playwright executor in `--dry-run` against a
  stored JSON reporter fixture and asserts it honours the report contract with
  no product service running.
- **`pipeline_v2_mcp.sh`** — spawns a real `python3 -m pipeline_v2_mcp`
  subprocess, performs the MCP handshake, lists tools, runs a mock dry-run
  batch, and queries the resulting ledger — proving the stdio transport and the
  tool surface end to end.
- **`portability.sh`** — greps the product-neutral core for product names and
  identifiers and then runs the orchestrator against the neutral `sample`
  fixture. This gate is what keeps the public repo product-neutral.

### Unit suites (`tests/unit/`)

The unit gate discovers **every** directory that holds a `test_*.py`, because
`unittest discover -s tests/unit` alone does not recurse into non-package
subdirectories. The suites are:

- `tests/unit/test_installer.py` — the manifest-driven installer.
- `tests/unit/pipeline_v2/` — the engine (oracles, ledger, funnel, publishing,
  orchestrator coverage, model routing, and more).
- `tests/unit/pipeline_v2_mcp/` — the MCP server handshake, tool list, tool
  dispatch, and transport behaviour.

## Held-out product-bound suites

Four suites are intentionally **not** part of this public repository because
they carry product-specific semantic maps, fixtures, and identifiers that must
stay private:

- `test_orchestrator.py` — end-to-end orchestration bound to a real adapter.
- `test_adapter_config.py` — validation of a real product's adapter config.
- `test_code_map.py` — code-map generation over a real product tree.
- `test_oracle_l2.py` — L2 terminal-state oracle against a real semantic map.

These run in the **private** product/knowledge repository against real adapter
inputs. The public repo keeps the neutral equivalents: the engine behaviour is
covered by `tests/unit/pipeline_v2/` and the neutral `sample` fixture, while the
product-specific bindings are exercised privately. This split is what lets the
core stay fully tested in the open without leaking product knowledge.

## Rationale

- **Fail-closed under test, too.** Contract and unit suites assert that missing
  evidence, drift, and unknown verdicts resolve to "do not file," matching the
  runtime contract.
- **No network, no service.** Every gate runs against fixtures, so CI is
  hermetic and reproducible on a stdlib-only Python 3.9+.
- **Neutrality is a gate, not a guideline.** `portability.sh` fails the build if
  a product name or identifier leaks into the core.
