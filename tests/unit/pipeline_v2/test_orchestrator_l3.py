# -*- coding: utf-8 -*-
"""orchestrator L3 端到端：用 MockExecutor 注入多步终态，验证差分/蜕变立案走通漏斗。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import (
    ExecutionResult,
    ExecutionScenario,
    PipelineV2Orchestrator,
    build_adapter_http_run,
)
from pipeline_v2.schema import SchemaError


SEMANTIC_ENTRY = {"id": "demo.l3.relation"}


class StepExecutor:
    """按 test_cmd（=步标识）返回预置的结构化终态。"""
    def __init__(self, by_cmd):
        self._by_cmd = by_cmd

    def run(self, *, test_cmd, scenario):
        state = self._by_cmd.get(test_cmd, {"terminal_state": {}, "event_sequence": []})
        return ExecutionResult(observed_state=state, exit_code=0)


def st(d):
    return {"terminal_state": d, "event_sequence": ["skill_state", "command_result"]}


class OrchestratorL3Test(unittest.TestCase):
    def _run(self, executor, scenario):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite")
        ledger.init_schema()
        orch = PipelineV2Orchestrator(
            ledger=ledger,
            executor=executor,
            queue_path=tmp / "q.jsonl",
            brief_path=tmp / "b.md",
        )
        return orch.run_batch(
            batch_id="l3-001",
            product_version="p@test",
            environment_digest="offline",
            sentinel_scenarios=[],
            scenarios=[scenario],
        )

    def test_differential_inconsistency_reported(self):
        ex = StepExecutor({
            "pathA": st({"mode": "exploration"}),
            "pathB": st({"mode": "modify_ontology"}),
        })
        sc = ExecutionScenario(
            scenario_id="diff:mode", step_id="ws", test_cmd="pathA",
            oracle_level="L3", oracle_strategy="differential",
            llm_involvement="clue_source",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_steps=["pathA", "pathB"], l3_path_labels=["A", "B"],
        )
        res = self._run(ex, sc)
        self.assertEqual(res.reported_count, 1)

    def test_metamorphic_hold_no_report(self):
        ex = StepExecutor({
            "base": st({"hidden": False, "items": 3}),
            "restored": st({"hidden": False, "items": 3}),
        })
        sc = ExecutionScenario(
            scenario_id="meta:restore", step_id="ws", test_cmd="base",
            oracle_level="L3", oracle_strategy="metamorphic",
            llm_involvement="clue_source", l3_relation="restored",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_steps=["base", "restored"], l3_relation_id="hide-unhide",
        )
        res = self._run(ex, sc)
        self.assertEqual(res.reported_count, 0)

    def test_metamorphic_break_reported(self):
        ex = StepExecutor({
            "base": st({"mode": "exploration"}),
            "plus_noise": st({"mode": "write_functions"}),
        })
        sc = ExecutionScenario(
            scenario_id="meta:noise", step_id="ws", test_cmd="base",
            oracle_level="L3", oracle_strategy="metamorphic",
            llm_involvement="clue_source", l3_relation="equal",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_steps=["base", "plus_noise"], l3_relation_id="add-irrelevant-step",
        )
        res = self._run(ex, sc)
        self.assertEqual(res.reported_count, 1)

    def test_l3_signal_uses_weak_verification_and_lands_ledger_queue_and_brief(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite")
        ledger.init_schema()
        queue_path = tmp / "q.jsonl"
        brief_path = tmp / "b.md"
        ex = StepExecutor({
            "pathA": st({"mode": "exploration"}),
            "pathB": st({"mode": "modify_ontology"}),
        })
        orch = PipelineV2Orchestrator(
            ledger=ledger,
            executor=ex,
            queue_path=queue_path,
            brief_path=brief_path,
        )
        sc = ExecutionScenario(
            scenario_id="diff:mode", step_id="ws", test_cmd="pathA",
            oracle_level="L3", oracle_strategy="differential",
            llm_involvement="clue_source",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_steps=["pathA", "pathB"], l3_path_labels=["A", "B"],
        )

        res = orch.run_batch(
            batch_id="l3-weak-001",
            product_version="p@test",
            environment_digest="offline",
            sentinel_scenarios=[],
            scenarios=[sc],
        )

        self.assertEqual(res.reported_count, 1)
        self.assertEqual(res.signal_count, 1)
        batch = ledger.get_batch("l3-weak-001")
        self.assertEqual(batch["oracle_l3_count"], 1)
        self.assertEqual(batch["verification_passed_count"], 1)
        case = ledger.list_cases(status="reported")[0]
        self.assertEqual(case["oracle_level"], "L3")
        verify_history = json.loads(case["verify_history"])
        self.assertEqual(verify_history[0]["channel"], "weak")
        self.assertEqual(verify_history[0]["required_runs"], 3)
        self.assertEqual(verify_history[0]["matching_runs"], 3)

        queue_events = [
            json.loads(line)
            for line in queue_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(queue_events[0]["kind"], "bug_report")
        self.assertEqual(queue_events[0]["payload"]["oracle_level"], "L3")
        self.assertEqual(queue_events[1]["kind"], "batch_brief")

        brief = brief_path.read_text(encoding="utf-8")
        self.assertIn("oracle L3", brief)
        self.assertIn("report_ref=queue://bug/", brief)


if __name__ == "__main__":
    unittest.main()


class StatefulMetamorphicExecutor:
    """模拟同会话序列执行器：baseline_step 单跑；run_session_sequence 返回末步终态。"""
    def __init__(self, baseline, last_terminal):
        self._baseline = baseline
        self._last = last_terminal
    def run(self, *, test_cmd, scenario):
        return ExecutionResult(observed_state=st(self._baseline), exit_code=0)
    def run_session_sequence(self, drive_messages):
        return ExecutionResult(observed_state=st(self._last), exit_code=0)


class OrchestratorStatefulL3Test(unittest.TestCase):
    def _run(self, executor, scenario):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(ledger=ledger, executor=executor,
                                      queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md")
        return orch.run_batch(batch_id="l3s-001", product_version="p@t",
                              environment_digest="offline", sentinel_scenarios=[], scenarios=[scenario])

    def _scenario(self):
        return ExecutionScenario(
            scenario_id="l3.stateful", step_id="ws", test_cmd="base",
            oracle_level="L3", oracle_strategy="metamorphic_stateful",
            llm_involvement="clue_source", l3_relation="restored",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_baseline_step="base", l3_steps=["A", "B", "A"], l3_relation_id="roundtrip",
        )

    def test_roundtrip_restores_no_report(self):
        # 基线 {mode:exploration,tc:26}，末步也恢复到同值 -> restored 成立 -> 不立案
        ex = StatefulMetamorphicExecutor({"mode": "exploration", "tc": 26}, {"mode": "exploration", "tc": 26})
        self.assertEqual(self._run(ex, self._scenario()).reported_count, 0)

    def test_roundtrip_not_restored_reported(self):
        # 末步没恢复（tc 漂移）-> restored 破坏 -> 立案
        ex = StatefulMetamorphicExecutor({"mode": "exploration", "tc": 26}, {"mode": "exploration", "tc": 19})
        self.assertEqual(self._run(ex, self._scenario()).reported_count, 1)


class SiblingExecutor:
    """按 test_cmd 返回预置 observed_state（terminal_event_type=command_result/error）。"""
    def __init__(self, by_cmd):
        self._by_cmd = by_cmd
    def run(self, *, test_cmd, scenario):
        return ExecutionResult(observed_state=self._by_cmd[test_cmd], exit_code=0)


def cr(success=True):
    return {"terminal_event_type": "command_result", "terminal_state": {"success": success}, "passed": True}

def err():
    return {"terminal_event_type": "error", "terminal_state": {"message": "rejected"}, "passed": False}


class OrchestratorSiblingTest(unittest.TestCase):
    def _run(self, executor, scenario):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(ledger=ledger, executor=executor,
                                      queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md")
        return orch.run_batch(batch_id="sib-001", product_version="p@t",
                              environment_digest="offline", sentinel_scenarios=[], scenarios=[scenario])

    def _scenario(self):
        return ExecutionScenario(
            scenario_id="sib:state-setters", step_id="ws", test_cmd="m",
            oracle_level="L3", oracle_strategy="sibling_consistency", llm_involvement="clue_source",
            semantic_map_entry=SEMANTIC_ENTRY,
            l3_relation_id="state-setters-reject-invalid",
            l3_steps=["m", "mo", "re"], l3_sibling_labels=["set_mode", "set_model", "set_reasoning_effort"],
        )

    def test_asymmetry_reported(self):
        # set_mode/set_model 拒绝(error)，set_reasoning_effort 接受(success) → 立案
        ex = SiblingExecutor({"m": err(), "mo": err(), "re": cr(success=True)})
        self.assertEqual(self._run(ex, self._scenario()).reported_count, 1)

    def test_all_reject_no_report(self):
        ex = SiblingExecutor({"m": err(), "mo": err(), "re": err()})
        self.assertEqual(self._run(ex, self._scenario()).reported_count, 0)


class AdapterL3BuilderTest(unittest.TestCase):
    def _write_semantic_map(self, tmp: Path):
        semantic_map = tmp / "semantic-map.yaml"
        semantic_map.write_text(
            "entries:\n- id: demo.l3.relation\n  expected_terminal_state:\n    machine_check:\n      field: ok\n",
            encoding="utf-8",
        )
        return semantic_map

    def _adapter(self, scenario):
        return {
            "http_driver": {
                "base_url": "http://127.0.0.1:8799",
                "ws_base_url": "ws://127.0.0.1:8799",
                "create_session_path": "/api/sessions",
                "ws_path_template": "/api/chat/ws/{session_id}",
                "scenarios": [scenario],
            }
        }

    def test_adapter_l3_rejects_missing_semantic_entry_id(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        adapter.write_text(
            json.dumps(
                self._adapter(
                    {
                        "scenario_id": "l3.missing-anchor",
                        "strategy": "metamorphic",
                        "steps": ["base", "transformed"],
                    }
                )
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SchemaError, "semantic_map_entry_id is required"):
            build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)

    def test_adapter_l3_rejects_unknown_semantic_entry_id(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        adapter.write_text(
            json.dumps(
                self._adapter(
                    {
                        "scenario_id": "l3.unknown-anchor",
                        "strategy": "metamorphic",
                        "steps": ["base", "transformed"],
                        "semantic_map_entry_id": "demo.missing",
                    }
                )
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SchemaError, "not found in semantic map: demo.missing"):
            build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)

    def test_adapter_l3_ignores_owner_gate_env_when_gate_absent(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        adapter.write_text(
            json.dumps(
                self._adapter(
                    {
                        "scenario_id": "l3.non-owner-gated",
                        "strategy": "metamorphic",
                        "relation": "equal",
                        "steps": ["base", "transformed"],
                        "semantic_map_entry_id": "demo.l3.relation",
                    }
                )
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "wrong-secret"}, clear=True):
            _executor, scenarios = build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)

        self.assertEqual([scenario.scenario_id for scenario in scenarios], ["l3.non-owner-gated"])

    def test_adapter_l3_honors_http_driver_timeout_knobs(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        data = self._adapter(
            {
                "scenario_id": "l3.timeout-knobs",
                "strategy": "metamorphic",
                "relation": "equal",
                "steps": ["base", "transformed"],
                "semantic_map_entry_id": "demo.l3.relation",
            }
        )
        data["http_driver"]["recv_timeout_sec"] = 1.5
        data["http_driver"]["drain_initial_events"] = 0
        data["http_driver"]["max_events"] = 3
        adapter.write_text(json.dumps(data), encoding="utf-8")

        executor, scenarios = build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)

        self.assertEqual([scenario.scenario_id for scenario in scenarios], ["l3.timeout-knobs"])
        self.assertEqual(executor._config.drain_initial_events, 0)
        self.assertEqual(executor._config.max_events, 3)
        self.assertEqual(executor._transport._recv_timeout, 1.5)

    def test_adapter_l3_rejects_invalid_http_driver_timeout_knobs(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        data = self._adapter(
            {
                "scenario_id": "l3.bad-timeout",
                "strategy": "metamorphic",
                "relation": "equal",
                "steps": ["base", "transformed"],
                "semantic_map_entry_id": "demo.l3.relation",
            }
        )
        data["http_driver"]["recv_timeout_sec"] = 0
        adapter.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(SchemaError, "recv_timeout_sec"):
            build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)

    def test_adapter_l3_enforces_owner_gate_before_direct_execution(self):
        tmp = Path(tempfile.mkdtemp())
        adapter = tmp / "adapter.json"
        semantic_map = self._write_semantic_map(tmp)
        adapter.write_text(
            json.dumps(
                {
                    **self._adapter(
                        {
                            "scenario_id": "l3.owner-gated",
                            "strategy": "metamorphic",
                            "relation": "equal",
                            "steps": ["base", "transformed"],
                            "semantic_map_entry_id": "demo.l3.relation",
                        }
                    ),
                    "owner_gate": {
                        "id": "destructive-sandbox",
                        "required_env": "SAMPLE_PIPELINE_OWNER_GATE",
                    },
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "wrong-secret"}, clear=True):
            with self.assertRaisesRegex(SchemaError, "value is not printed") as raised:
                build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)
        self.assertNotIn("wrong-secret", str(raised.exception))

        with mock.patch.dict(os.environ, {"SAMPLE_PIPELINE_OWNER_GATE": "destructive-sandbox"}, clear=True):
            _executor, scenarios = build_adapter_http_run(adapter_path=adapter, semantic_map_path=semantic_map)
        self.assertEqual([scenario.scenario_id for scenario in scenarios], ["l3.owner-gated"])
