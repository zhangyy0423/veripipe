# examples

## `demo.py` — the method in 60 seconds

Runs entirely on the neutral fixtures in this repo. No product, no network.

```bash
python3 examples/demo.py
```

It walks one contract (`ready == true`) through three agent runs and shows the
three verdicts the oracle can reach:

- **passes** when structured evidence backs the claim (no defect),
- **files a failure** when the state disagrees with a confident "done,"
- **stays inconclusive** when the agent only produces words and no state.

Only the machine-verified failure is filed. See
[`../docs/methodology.md`](../docs/methodology.md) for the reasoning.

## Recording / sharing the demo

A pre-recorded cast (generated from the real run) is checked in:

```bash
asciinema play examples/demo.cast          # play it
agg examples/demo.cast examples/demo.gif    # optional: render a GIF
```

Re-record it yourself:

```bash
asciinema rec -c 'bash examples/record-demo.sh' examples/demo.cast
```
