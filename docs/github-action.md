# GitHub Action

`veripipe` ships a composite action that summarizes a verification run in your
CI: how many candidate checks ran, how many were filed as machine-verified
findings, and how many were withheld.

## Usage

Produce an orchestrator result JSON in an earlier step, then summarize it:

```yaml
- uses: zhangyy0423/veripipe@main
  with:
    result: ./veripipe-result.json
    # fail-on-filed: "true"   # optional: fail the job when a finding is filed
```

The summary is written to the job summary. With `fail-on-filed: "true"`, the
step exits non-zero if any machine-verified finding was filed, so an agent's
run can gate a pipeline on real, evidence-backed defects only.

Inputs:

| input | default | meaning |
| --- | --- | --- |
| `result` | (required) | path to an orchestrator result JSON |
| `python-version` | `3.12` | Python to set up |
| `fail-on-filed` | `false` | fail the step if any finding was filed |
