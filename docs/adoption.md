# Adopting veripipe

A practical guide to trying veripipe, wiring it into a host, and pointing it at
your own product. It stays offline and side-effect-free unless you tell it
otherwise.

## What it is, and who it is for

veripipe verifies bug reports produced by AI agents before they reach a tracker.
If you run agents against a web or HTTP product and get confident reports for
behaviour that turns out to be fine, veripipe is the filter that only passes a
finding when structured, reproducible evidence backs it. See
[`methodology.md`](methodology.md) for the reasoning.

## Try it in 60 seconds

No install, no product, no network:

```bash
python3 examples/demo.py
```

It walks one contract through three agent runs — a real success, a false
"done," and an unbacked claim — and shows that only the machine-verified failure
is filed.

## Install

From source:

```bash
git clone <this-repo> veripipe && cd veripipe
export PYTHONPATH="$PWD/shared-skills/pipeline-v2/scripts:$PYTHONPATH"
python3 -m pipeline_v2.orchestrator --help
```

From GitHub (no PyPI account needed):

```bash
pip install "git+https://github.com/zhangyy0423/veripipe"
```

Or, once published: `pip install veripipe`.

## Run a batch

The offline mock executor produces a queue and a one-page brief without touching
a product:

```bash
python3 -m pipeline_v2.orchestrator \
  --ledger /tmp/v.sqlite --queue /tmp/v.jsonl --brief /tmp/v.md
```

Summarize any run into an auditable number:

```bash
python3 -m pipeline_v2.fp_report --result /tmp/result.json
```

## Wire it into an MCP host

The MCP server is standard-library only and speaks JSON-RPC over stdio, so dsh,
Claude Code, Codex, or your own client can drive it:

```bash
python3 -m pipeline_v2_mcp
```

See [`dsh-plugin.md`](dsh-plugin.md) for a host-by-host setup.

## Point it at your product

1. Write contracts for the behaviours worth checking, as machine-comparable
   terminal states in a semantic map.
2. Provide a transport that returns structured observed-state — a Playwright
   spec, an HTTP/WS driver, or any command via `--executor external`.
3. Keep product specifics in a private adapter; the engine stays neutral.

The full layout and executor matrix are in [`adapters.md`](adapters.md).

## Gate your CI

Run veripipe over an agent run and surface the funnel, optionally failing the
job on a filed finding — see [`github-action.md`](github-action.md).

## What it does not do

It does not compare agent text, it does not let a model confirm a finding, it
does not file when unsure, and it does not post anywhere by itself.

## Feedback

Issues and questions are welcome in the repository. If you build an adapter or an
integration, the semantic-map shape and the oracle model are the parts most
worth a second pair of eyes.
