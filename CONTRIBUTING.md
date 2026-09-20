# Contributing to veripipe

Thanks for looking. veripipe is early (alpha); the engine and its interfaces can
still change. Small, focused changes with tests are the easiest to accept.

## Develop

```bash
git clone <this-repo> veripipe && cd veripipe
export PYTHONPATH="$PWD/shared-skills/pipeline-v2/scripts:$PYTHONPATH"
bash scripts/ci/run-gates.sh    # bash-syntax, python-syntax, secret-scan, contract, unit
```

Everything runs offline with the standard library plus PyYAML. CI runs the same
gates across Python 3.9–3.12; keep them green.

## The one rule for the public tree

The engine is **product-neutral**. No product names, endpoints, intranet
addresses, credentials, or unreviewed product knowledge in tracked files — the
`portability` gate scans for this and will fail the build. Product-specific
adapters, semantic maps, and transports live in a **separate private
repository**, not here. When in doubt, keep it out.

## Making a change

- Add or update tests. New executor paths get a `tests/contract/*.sh`; logic gets
  unit tests under `tests/unit/`.
- Run the gates locally before opening a PR.
- Keep commits focused and messages plain (what changed and why).
- Docs live in `docs/`; new capabilities should be linked from the README.

## Adding an adapter or executor

See [`docs/adapters.md`](docs/adapters.md) for the adapter layout and the
executor matrix, and [`docs/methodology.md`](docs/methodology.md) for what the
oracle will and will not accept.

## Reporting issues

Use the issue templates. For a bug, include how to reproduce it, what you
expected, and what happened. For a proposal, describe the problem before the
solution.
