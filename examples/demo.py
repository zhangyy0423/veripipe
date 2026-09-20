#!/usr/bin/env python3
"""A 60-second, product-neutral demo of the anti-false-positive method.

Uses only the neutral fixtures shipped in this repo (no product, no network).
It shows the three verdicts the oracle can reach and why only one is filed.

    python3 examples/demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared-skills" / "pipeline-v2" / "scripts"))

import yaml  # noqa: E402
from pipeline_v2.oracle_l2 import evaluate_l2_oracle, load_entry_by_id  # noqa: E402

MAP = yaml.safe_load((ROOT / "tests" / "fixtures" / "pipeline_v2" / "external"
                      / "semantic-map.yaml").read_text(encoding="utf-8"))
ENTRY = "sample.external.contract"  # contract: ready == true


def verdict(label: str, observed: dict, claim: str) -> None:
    r = evaluate_l2_oracle(load_entry_by_id(MAP, ENTRY), observed,
                           scenario_id="demo", step_id="terminal", llm_involvement="none")
    filed = "FILED" if r.passed is False else ("passed, nothing to file" if r.passed else "NOT filed")
    print(f"  {label}")
    print(f"    agent says     : {claim}")
    print(f"    observed state : {observed or '(none)'}")
    print(f"    oracle         : status={r.status} -> {filed}\n")


def main() -> int:
    print("veripipe demo — one contract: the workflow's terminal state must have ready == true.")
    print("The oracle judges structured state only; the agent's wording is never evidence.\n")

    print("1) Real success — structured evidence backs the claim:")
    verdict("run A", {"ready": True}, "\"done, it's ready\"")

    print("2) False 'done' — the claim is confident but the state disagrees:")
    verdict("run B", {"ready": False}, "\"all set, ready to go\"")

    print("3) Unbacked claim — words only, no structured state:")
    verdict("run C", {}, "\"I've made it ready\"")

    print("Only run B is filed: a machine-verified failure. Run A passes (no defect); "
          "run C is inconclusive and filed nowhere. That is the whole point — evidence "
          "decides, prose does not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
