# -*- coding: utf-8 -*-
"""提报闭环 dry-run 验证（B1）：立案 -> 建卡 -> 简报 -> triage 状态回写，全程 mock。

明确标注：这是用 mock publisher 验证「提报链路活着」，**不立任何真实产品 bug**。
真实 bug 提报等轨道 2 挖到真信号后再走（且仍需人工 triage）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger
from pipeline_v2.orchestrator import ExecutionResult, ExecutionScenario, PipelineV2Orchestrator
from pipeline_v2.publishing import CommandPublisher, flush_reviewed_queue


class StepExecutor:
    def __init__(self, by_cmd):
        self._by_cmd = by_cmd
    def run(self, *, test_cmd, scenario):
        return ExecutionResult(observed_state=self._by_cmd[test_cmd], exit_code=0)


def st(d):
    return {"terminal_state": d, "event_sequence": ["command_result"]}


class ReportingLoopTest(unittest.TestCase):
    def test_batch_rejects_external_command_publisher(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "reject.sqlite")
        ledger.init_schema()
        publisher = CommandPublisher(
            runner=lambda _command, _payload: {"card_id": "must-not-run"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(ValueError, "batch.*queue"):
            PipelineV2Orchestrator(
                ledger=ledger,
                executor=StepExecutor({}),
                queue_path=tmp / "queue.jsonl",
                brief_path=tmp / "brief.md",
                publisher=publisher,
            )
        ledger.close()

    def test_l3_case_queues_before_explicit_owner_reviewed_flush(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = Ledger(tmp / "l.sqlite"); ledger.init_schema()

        sent = []
        def runner(command, payload):
            sent.append(command)
            if command == ["tracker", "create"]:
                return {
                    "card_id": "SAMPLE-DRYRUN-1",
                    "_publication": dict(payload["_publication"]),
                }
            if command == ["wiki", "publish"]:
                return {
                    "page_id": "wiki-1",
                    "_publication": dict(payload["_publication"]),
                }
            if command == ["tracker", "status"]:
                # 模拟人工 triage 把卡判为 false-positive（闭环演示，非真实裁决）
                return {"cards": [{"report_ref": "SAMPLE-DRYRUN-1", "triage_result": "false-positive"}]}
            return {}

        # 用一个必破坏的 L3 蜕变（两步不同 mode）触发立案，验证它真的走到 publisher
        ex = StepExecutor({"a": st({"mode": "exploration"}), "b": st({"mode": "modify_ontology"})})
        orch = PipelineV2Orchestrator(
            ledger=ledger, executor=ex,
            queue_path=tmp / "q.jsonl", brief_path=tmp / "b.md",
        )
        sc = ExecutionScenario(
            scenario_id="loop:demo", step_id="ws", test_cmd="a",
            oracle_level="L3", oracle_strategy="metamorphic", llm_involvement="clue_source",
            semantic_map_entry={"id": "demo.l3.publish"},
            l3_relation="equal", l3_steps=["a", "b"], l3_relation_id="demo-break",
        )
        res = orch.run_batch(batch_id="loop-001", product_version="dryrun@x",
                             environment_digest="offline", sentinel_scenarios=[], scenarios=[sc])
        # batch 只落本地 queue，不执行任何外部 command。
        self.assertEqual(res.reported_count, 1)
        self.assertEqual(sent, [])
        self.assertTrue(res.report_refs)
        events = [json.loads(line) for line in (tmp / "q.jsonl").read_text(encoding="utf-8").splitlines()]
        bug_event = next(event for event in events if event["kind"] == "bug_report")

        publisher = CommandPublisher(
            runner=runner,
            create_bug_command=["tracker", "create"],
        )
        flushed = flush_reviewed_queue(
            queue_path=tmp / "q.jsonl",
            review_manifest={
                "schema_version": 1,
                "owner": "owner@example",
                "owner_approved": True,
                "target_environment": "sandbox",
                "target_id": "publishing-loop-sandbox",
                "approved_events": [
                    {
                        "event_id": bug_event["event_id"],
                        "payload_sha256": bug_event["payload_sha256"],
                    }
                ],
            },
            adapter={
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-loop-sandbox",
                },
            },
            publisher=publisher,
            production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
            yes=True,
            receipt_path=tmp / "receipt.jsonl",
        )

        self.assertEqual(flushed["flushed_count"], 1)
        self.assertEqual(sent, [["tracker", "create"]])
        ledger.close()


if __name__ == "__main__":
    unittest.main()
