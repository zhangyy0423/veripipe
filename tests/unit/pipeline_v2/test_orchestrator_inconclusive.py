# -*- coding: utf-8 -*-
"""Orchestrator withholds L2 inconclusive outcomes with a scoped reason.

The fail-closed path must be *legible*: an operator who sees a batch with
no findings and no passes needs to know whether the semantic map is broken
(bucket ``contract``) or the executor never observed the field
(bucket ``observation``), instead of staring at ``no_signal``.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import (
    ExecutionResult,
    ExecutionScenario,
    PipelineV2Orchestrator,
)

ENTRY = {
    "id": "sample.external.contract",
    "skill": {"name": "sample_external", "description": "fixture"},
    "expected_terminal_state": {
        "assertion": "ready == true",
        "observable": "structured terminal state",
        "assertion_type": "workflow_state",
        "machine_check": {"field": "ready", "op": "eq", "value": True},
    },
}


class FixedExecutor:
    def __init__(self, observed, observed_at=None):
        self._observed = observed
        self._observed_at = observed_at

    def run(self, *, test_cmd, scenario):
        return ExecutionResult(observed_state=self._observed, exit_code=0,
                               observed_at=self._observed_at)


class OrchestratorInconclusiveTest(unittest.TestCase):
    def _scenario(self):
        return ExecutionScenario(
            scenario_id="hop-empty",
            step_id="terminal",
            test_cmd="hop",
            oracle_level="L2",
            oracle_strategy="machine_check",
            llm_involvement="none",
            semantic_map_entry=ENTRY,
        )

    def _run(self, executor, scenario):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite")
        ledger.init_schema()
        orch = PipelineV2Orchestrator(ledger=ledger, executor=executor,
                                      queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md")
        result = orch.run_batch(
            batch_id="hop-001",
            product_version="p@t",
            environment_digest="offline",
            sentinel_scenarios=[],
            scenarios=[scenario],
        )
        return result, tmp

    def test_missing_observed_field_is_inconclusive_with_observation_reason(self):
        out, _tmp = self._run(FixedExecutor({}), self._scenario())
        self.assertEqual(out.executed_count, 1)
        self.assertEqual(out.pass_count, 0)
        self.assertEqual(out.inconclusive_count, 1)
        self.assertEqual(out.reported_count, 0)
        self.assertEqual(out.frustration_reasons["hop-empty"], "observation: observed_state.field")

    def test_passing_state_is_a_pass_not_a_reason(self):
        out, _tmp = self._run(FixedExecutor({"ready": True}), self._scenario())
        self.assertEqual(out.executed_count, 1)
        self.assertEqual(out.pass_count, 1)
        self.assertEqual(out.inconclusive_count, 0)
        self.assertEqual(out.frustration_reasons, {})

    def test_failing_state_still_files(self):
        out, _tmp = self._run(FixedExecutor({"ready": False}), self._scenario())
        self.assertEqual(out.reported_count, 1)
        self.assertEqual(out.inconclusive_count, 0)
        self.assertEqual(out.frustration_reasons, {})

    def test_json_round_trip_carries_both_new_fields(self):
        out, _tmp = self._run(FixedExecutor({}), self._scenario())
        blob = json.loads(json.dumps(out.as_dict()))
        self.assertEqual(blob["inconclusive_count"], 1)
        self.assertEqual(blob["frustration_reasons"]["hop-empty"],
                         "observation: observed_state.field")


if __name__ == "__main__":
    unittest.main()


class ObservedWhenContractTest(OrchestratorInconclusiveTest):
    """An entry that declares observed_when forces the sampling method."""

    def _entry(self, observed_when):
        entry = dict(ENTRY)
        entry["expected_terminal_state"] = dict(ENTRY["expected_terminal_state"])
        entry["expected_terminal_state"]["observed_when"] = observed_when
        return entry

    def test_undeclared_sampling_holds_back_with_observation_reason(self):
        out, _tmp = self._run(FixedExecutor({"ready": True}),
                              ExecutionScenario(
                                  scenario_id="hop-ts", step_id="terminal", test_cmd="hop",
                                  oracle_level="L2", oracle_strategy="machine_check",
                                  llm_involvement="none",
                                  semantic_map_entry=self._entry("turn-end-fold")))
        self.assertEqual(out.inconclusive_count, 1)
        self.assertEqual(out.frustration_reasons["hop-ts"], "observation: observed_when.undeclared")

    def test_wrong_sampling_method_holds_back_with_mismatch_reason(self):
        executor = FixedExecutor({"ready": True}, observed_at="live-projection")
        out, _tmp = self._run(executor,
                              ExecutionScenario(
                                  scenario_id="hop-ts2", step_id="terminal", test_cmd="hop",
                                  oracle_level="L2", oracle_strategy="machine_check",
                                  llm_involvement="none",
                                  semantic_map_entry=self._entry("turn-end-fold")))
        self.assertEqual(out.inconclusive_count, 1)
        self.assertIn("observed_when.mismatch", out.frustration_reasons["hop-ts2"])
