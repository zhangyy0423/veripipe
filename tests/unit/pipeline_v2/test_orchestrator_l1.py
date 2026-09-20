# -*- coding: utf-8 -*-
"""orchestrator L1 端到端：注入崩溃结果，验证强通道立案。"""
import json
import os
import tempfile, unittest
from pathlib import Path
from unittest import mock
from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import (
    ExecutionResult,
    ExecutionScenario,
    PipelineV2Orchestrator,
    build_adapter_l1_fuzz_run,
)
from pipeline_v2.schema import SchemaError


class CrashExecutor:
    def __init__(self, result):
        self._result = result
    def run(self, *, test_cmd, scenario):
        return self._result


class OrchestratorL1Test(unittest.TestCase):
    def _run(self, executor, scenario):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(ledger=ledger, executor=executor,
                                      queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md")
        result = orch.run_batch(batch_id="l1-001", product_version="p@t",
                                environment_digest="offline", sentinel_scenarios=[], scenarios=[scenario])
        return result, ledger, tmp

    def _scenario(self):
        return ExecutionScenario(scenario_id="l1:fuzz", step_id="http", test_cmd="fuzz-1",
                                 oracle_level="L1", oracle_strategy="crash", llm_involvement="none",
                                 semantic_map_entry={"id": "demo.auth.local"})

    def test_panic_reported_via_strong_channel(self):
        res = ExecutionResult(observed_state={"passed": False}, exit_code=2,
                              stderr="panic: nil pointer dereference\n\tserver.go:88 +0x10")
        out, _ledger, _tmp = self._run(CrashExecutor(res), self._scenario())
        self.assertEqual(out.reported_count, 1)

    def test_clean_run_no_report(self):
        res = ExecutionResult(observed_state={"passed": True}, exit_code=0, stdout="ok")
        out, _ledger, _tmp = self._run(CrashExecutor(res), self._scenario())
        self.assertEqual(out.reported_count, 0)
        self.assertEqual(out.signal_count, 0)

    def test_4xx_not_reported(self):
        res = ExecutionResult(observed_state={"http_status": 404}, exit_code=0)
        out, _ledger, _tmp = self._run(CrashExecutor(res), self._scenario())
        self.assertEqual(out.reported_count, 0)

    def test_l1_semantic_entry_reaches_ledger_and_queue_payload(self):
        res = ExecutionResult(observed_state={"passed": False}, exit_code=2, stderr="panic: crash")
        scenario = ExecutionScenario(
            scenario_id="l1:fuzz",
            step_id="http",
            test_cmd="fuzz-1",
            oracle_level="L1",
            oracle_strategy="crash",
            llm_involvement="none",
            semantic_map_entry={"id": "demo.auth.local"},
        )

        out, ledger, tmp = self._run(CrashExecutor(res), scenario)

        self.assertEqual(out.reported_count, 1)
        cases = ledger.list_cases(status="reported")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["semantic_map_entry_id"], "demo.auth.local")
        queue_events = [
            json.loads(line)
            for line in (tmp / "q.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        bug_events = [item for item in queue_events if item["kind"] == "bug_report"]
        self.assertEqual(len(bug_events), 1)
        self.assertEqual(bug_events[0]["payload"]["semantic_map_entry_id"], "demo.auth.local")

    def test_adapter_l1_fuzz_scenarios_keep_semantic_entry(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = tmp / "semantic-map.yaml"
        adapter.write_text(
            json.dumps(
                {
                    "l1_fuzz": {
                        "base_url": "http://127.0.0.1:8799",
                        "cases": [
                            {
                                "id": "bad-json",
                                "method": "POST",
                                "path": "/api/sessions",
                                "raw_body": "{bad",
                                "semantic_map_entry_id": "demo.auth.local",
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        semantic_map.write_text(
            "entries:\n- id: demo.auth.local\n  expected_terminal_state:\n    machine_check:\n      field: ok\n",
            encoding="utf-8",
        )

        _executor, scenarios = build_adapter_l1_fuzz_run(
            adapter_path=adapter,
            semantic_map_path=semantic_map,
        )

        self.assertEqual(scenarios[0].oracle_level, "L1")
        self.assertEqual(scenarios[0].semantic_map_entry["id"], "demo.auth.local")

    def test_adapter_l1_fuzz_ignores_owner_gate_env_when_gate_absent(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = tmp / "semantic-map.yaml"
        adapter.write_text(
            json.dumps(
                {
                    "l1_fuzz": {
                        "base_url": "http://127.0.0.1:8799",
                        "cases": [
                            {
                                "id": "bad-json",
                                "method": "POST",
                                "path": "/api/sessions",
                                "raw_body": "{bad",
                                "semantic_map_entry_id": "demo.auth.local",
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        semantic_map.write_text("entries:\n- id: demo.auth.local\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "wrong-secret"}, clear=True):
            _executor, scenarios = build_adapter_l1_fuzz_run(
                adapter_path=adapter,
                semantic_map_path=semantic_map,
            )

        self.assertEqual([scenario.scenario_id for scenario in scenarios], ["l1:bad-json"])

    def test_adapter_l1_fuzz_rejects_missing_semantic_entry_id(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = tmp / "semantic-map.yaml"
        adapter.write_text(
            json.dumps(
                {
                    "l1_fuzz": {
                        "base_url": "http://127.0.0.1:8799",
                        "cases": [{"id": "bad-json", "method": "POST", "path": "/api/sessions"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        semantic_map.write_text("entries:\n- id: demo.auth.local\n", encoding="utf-8")

        with self.assertRaisesRegex(SchemaError, "semantic_map_entry_id must be a string"):
            build_adapter_l1_fuzz_run(adapter_path=adapter, semantic_map_path=semantic_map)

    def test_adapter_l1_fuzz_rejects_unknown_semantic_entry_id(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = tmp / "semantic-map.yaml"
        adapter.write_text(
            json.dumps(
                {
                    "l1_fuzz": {
                        "base_url": "http://127.0.0.1:8799",
                        "cases": [
                            {
                                "id": "bad-json",
                                "method": "POST",
                                "path": "/api/sessions",
                                "semantic_map_entry_id": "demo.missing",
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        semantic_map.write_text("entries:\n- id: demo.auth.local\n", encoding="utf-8")

        with self.assertRaisesRegex(SchemaError, "not found in semantic map: demo.missing"):
            build_adapter_l1_fuzz_run(adapter_path=adapter, semantic_map_path=semantic_map)

    def test_adapter_l1_fuzz_enforces_owner_gate_before_direct_execution(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = tmp / "semantic-map.yaml"
        adapter.write_text(
            json.dumps(
                {
                    "owner_gate": {
                        "id": "destructive-sandbox",
                        "required_env": "SAMPLE_PIPELINE_OWNER_GATE",
                    },
                    "l1_fuzz": {
                        "base_url": "http://127.0.0.1:8799",
                        "cases": [
                            {
                                "id": "bad-json",
                                "method": "POST",
                                "path": "/api/sessions",
                                "raw_body": "{bad",
                                "semantic_map_entry_id": "demo.auth.local",
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        semantic_map.write_text("entries:\n- id: demo.auth.local\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "wrong-secret"}, clear=True):
            with self.assertRaisesRegex(SchemaError, "value is not printed") as raised:
                build_adapter_l1_fuzz_run(adapter_path=adapter, semantic_map_path=semantic_map)
        self.assertNotIn("wrong-secret", str(raised.exception))

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "destructive-sandbox"}, clear=True):
            _executor, scenarios = build_adapter_l1_fuzz_run(
                adapter_path=adapter,
                semantic_map_path=semantic_map,
            )
        self.assertEqual([scenario.scenario_id for scenario in scenarios], ["l1:bad-json"])


if __name__ == "__main__":
    unittest.main()
