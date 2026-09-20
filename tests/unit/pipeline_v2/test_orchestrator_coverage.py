# -*- coding: utf-8 -*-
"""orchestrator 覆盖率 + 建议通道端到端：未覆盖 entry 进独立 sink，不进 bug queue。"""
import json, tempfile, unittest
from pathlib import Path
from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import ExecutionResult, ExecutionScenario, PipelineV2Orchestrator

SEM = {"entries": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]}


class PassExecutor:
    def run(self, *, test_cmd, scenario):
        return ExecutionResult(observed_state={"passed": True}, exit_code=0)


class ExplodingExecutor:
    def run(self, *, test_cmd, scenario):
        raise AssertionError("L4 scenarios must not be executed as bug-filing machine oracles")


class CoverageTest(unittest.TestCase):
    def test_coverage_and_suggestion_sink_isolated_from_queue(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(
            ledger=ledger, executor=PassExecutor(),
            queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md",
            suggestion_path=tmp / "suggestions.jsonl", semantic_map=SEM,
        )
        # 只触达 e1（一个 L2 mock 场景，pass，不立案）
        sc = ExecutionScenario(scenario_id="s", step_id="x", test_cmd="c",
                               semantic_map_entry={"id": "e1", "expected_terminal_state": {}},
                               oracle_level="L2", oracle_strategy="machine_check")
        res = orch.run_batch(batch_id="cov-001", product_version="p@t",
                             environment_digest="offline", sentinel_scenarios=[], scenarios=[sc])
        # 覆盖率：3 个 entry，覆盖 1，未覆盖 e2/e3
        self.assertEqual(res.coverage["total_entries"], 3)
        self.assertEqual(res.coverage["covered_count"], 1)
        self.assertEqual(sorted(res.coverage["uncovered_entry_ids"]), ["e2", "e3"])
        # 建议写入独立 sink，且标 is_bug=False
        sug_lines = (tmp / "suggestions.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(sug_lines), 2)
        for line in sug_lines:
            payload = json.loads(line)["payload"]
            self.assertFalse(payload["is_bug"])
            self.assertEqual(payload["kind"], "coverage_gap")
        # bug queue 里绝无 suggestion 混入
        q = (tmp / "q.jsonl").read_text(encoding="utf-8") if (tmp / "q.jsonl").exists() else ""
        self.assertNotIn("coverage_gap", q)

    def test_l4_scenario_never_enters_bug_queue_even_when_semantically_anchored(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(
            ledger=ledger,
            executor=ExplodingExecutor(),
            queue_path=tmp / "q.jsonl",
            brief_path=tmp / "b.md",
            suggestion_path=tmp / "suggestions.jsonl",
            semantic_map=SEM,
        )
        sc = ExecutionScenario(
            scenario_id="l4:idea",
            step_id="review",
            test_cmd="would-fail-if-executed",
            semantic_map_entry={"id": "e1", "expected_terminal_state": {"machine_check": {"field": "ok"}}},
            oracle_level="L4",
            oracle_strategy="hypothesis",
            llm_involvement="clue_source",
        )

        res = orch.run_batch(
            batch_id="l4-001",
            product_version="p@t",
            environment_digest="offline",
            sentinel_scenarios=[],
            scenarios=[sc],
        )

        self.assertEqual(res.reported_count, 0)
        self.assertEqual(ledger.list_cases(status="reported"), [])
        q = (tmp / "q.jsonl").read_text(encoding="utf-8") if (tmp / "q.jsonl").exists() else ""
        self.assertNotIn("bug_report", q)
        suggestions = [
            json.loads(line)["payload"]
            for line in (tmp / "suggestions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        clue = [item for item in suggestions if item["kind"] == "clue"]
        self.assertEqual(len(clue), 1)
        self.assertFalse(clue[0]["is_bug"])
        self.assertEqual(clue[0]["semantic_map_entry_id"], "e1")
        self.assertEqual(res.coverage["covered_count"], 0)

    def test_no_semantic_map_no_coverage(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()
        orch = PipelineV2Orchestrator(ledger=ledger, executor=PassExecutor(),
                                      queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md")
        sc = ExecutionScenario(scenario_id="s", step_id="x", test_cmd="c",
                               semantic_map_entry={"id": "e1", "expected_terminal_state": {}})
        res = orch.run_batch(batch_id="cov-002", product_version="p@t",
                             environment_digest="offline", sentinel_scenarios=[], scenarios=[sc])
        self.assertIsNone(res.coverage)


if __name__ == "__main__":
    unittest.main()
