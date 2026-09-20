# -*- coding: utf-8 -*-
"""Offline tests for the product-neutral `external` executor."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import (
    ExecutionScenario,
    ExternalExecutor,
    PipelineV2Orchestrator,
    _external_result_from_payload,
    build_adapter_external_run,
)
from pipeline_v2.schema import SchemaError

ROOT = Path(__file__).resolve().parents[3]
SAMPLE_MAP = ROOT / "tests" / "fixtures" / "pipeline_v2" / "external" / "semantic-map.yaml"
FX = ROOT / "tests" / "fixtures" / "pipeline_v2" / "external"
ADAPTER = FX / "adapter.external.json"
REPORT = FX / "observed-states.json"


def _scenario(entry):
    return ExecutionScenario(
        scenario_id="s1", step_id="terminal", test_cmd="c",
        semantic_map_entry=entry, oracle_level="L2", oracle_strategy="machine_check",
    )


class PayloadTest(unittest.TestCase):
    def test_wrapper_and_bare_forms(self):
        wrapped = _external_result_from_payload(
            {"observed_state": {"passed": False}, "exit_code": 1, "raw_failure": "x"}
        )
        self.assertEqual(wrapped.observed_state, {"passed": False})
        self.assertEqual(wrapped.exit_code, 1)
        bare = _external_result_from_payload({"passed": True})
        self.assertEqual(bare.observed_state, {"passed": True})


class ExecutorTest(unittest.TestCase):
    def test_dry_run_lookup_and_fallback_and_missing(self):
        ex = ExternalExecutor([], dry_run_states={"e1": {"compacted": True}})
        entry = {"id": "e1"}
        self.assertEqual(ex.run(test_cmd="c", scenario=_scenario(entry)).observed_state,
                         {"compacted": True})
        # missing -> environment_unavailable (fail-closed, not a false pass)
        miss = ex.run(test_cmd="c", scenario=_scenario({"id": "nope"}))
        self.assertTrue(miss.environment_unavailable)

    def test_live_subprocess_json_stdout(self):
        cmd = ["python3", "-c",
               "import sys,json;json.dump({'observed_state':{'passed':False}},sys.stdout)"]
        res = ExternalExecutor(cmd).run(test_cmd="c", scenario=_scenario({"id": "e1"}))
        self.assertEqual(res.observed_state, {"passed": False})

    def test_live_nonzero_no_stdout_is_environment(self):
        cmd = ["python3", "-c", "import sys;sys.stderr.write('boom');sys.exit(3)"]
        res = ExternalExecutor(cmd).run(test_cmd="c", scenario=_scenario({"id": "e1"}))
        self.assertTrue(res.environment_unavailable)

    def test_live_non_json_stdout_is_environment(self):
        cmd = ["python3", "-c", "print('not json')"]
        res = ExternalExecutor(cmd).run(test_cmd="c", scenario=_scenario({"id": "e1"}))
        self.assertTrue(res.environment_unavailable)


class BuilderTest(unittest.TestCase):
    def test_missing_external_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.json"
            p.write_text(json.dumps({"product": "x", "production_write_default": False}))
            with self.assertRaises(SchemaError):
                build_adapter_external_run(adapter_path=p, semantic_map_path=SAMPLE_MAP)

    def test_dry_run_builds_scenarios_bound_to_map(self):
        ex, scenarios = build_adapter_external_run(
            adapter_path=ADAPTER, semantic_map_path=SAMPLE_MAP, dry_run_report_path=REPORT,
        )
        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0].semantic_map_entry["id"], "sample.external.contract")
        self.assertEqual(scenarios[0].oracle_strategy, "machine_check")

    def test_unknown_entry_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.json"
            p.write_text(json.dumps({
                "product": "sample-product", "production_write_default": False,
                "external": {"command": ["true"],
                             "scenarios": [{"semantic_map_entry_id": "no.such.entry"}]},
            }))
            with self.assertRaises(SchemaError):
                build_adapter_external_run(adapter_path=p, semantic_map_path=SAMPLE_MAP)


class EndToEndTest(unittest.TestCase):
    def test_external_dry_run_reports_one_l2_finding(self):
        ex, scenarios = build_adapter_external_run(
            adapter_path=ADAPTER, semantic_map_path=SAMPLE_MAP, dry_run_report_path=REPORT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "q.jsonl"
            ledger = Ledger(Path(tmp) / "l.sqlite")
            ledger.init_schema()
            try:
                orch = PipelineV2Orchestrator(
                    ledger=ledger, executor=ex, queue_path=queue,
                    brief_path=Path(tmp) / "b.md",
                )
                result = orch.run_batch(
                    batch_id="ext-1", product_version="p@offline",
                    environment_digest="offline", sentinel_scenarios=[],
                    scenarios=scenarios,
                )
            finally:
                ledger.close()
            self.assertEqual(result.reported_count, 1)
            self.assertEqual(result.oracle_levels, ["L2"])
            events = [json.loads(x) for x in queue.read_text().splitlines()]
            bug = next(e for e in events if e["kind"] == "bug_report")
            self.assertEqual(bug["payload"]["semantic_map_entry_id"], "sample.external.contract")


if __name__ == "__main__":
    unittest.main()
