# -*- coding: utf-8 -*-
"""suggestion + coverage 单测：建议通道与 bug queue 物理隔离 + 未覆盖路径。"""
import json, tempfile, unittest
from pathlib import Path
from pipeline_v2.suggestion import (
    Suggestion, SuggestionSink, compute_coverage, coverage_gaps_to_suggestions,
)

SEM = {"entries": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]}


class SuggestionTest(unittest.TestCase):
    def test_sink_flushes_jsonl_and_marks_non_bug(self):
        tmp = Path(tempfile.mkdtemp()) / "suggestions.jsonl"
        sink = SuggestionSink(tmp)
        sink.add(Suggestion(kind="architecture", summary="耦合过紧"))
        path = sink.flush()
        self.assertEqual(path, tmp)
        line = json.loads(tmp.read_text(encoding="utf-8").strip())
        self.assertEqual(line["kind"], "suggestion")
        self.assertFalse(line["payload"]["is_bug"])  # 绝不混成 bug

    def test_memory_only_without_path(self):
        sink = SuggestionSink()
        sink.add(Suggestion(kind="clue", summary="可疑"))
        self.assertIsNone(sink.flush())
        self.assertEqual(len(sink.items()), 1)

    def test_coverage_gap(self):
        report = compute_coverage(SEM, exercised_entry_ids=["e1"])
        self.assertEqual(report.total_entries, 3)
        self.assertEqual(report.covered_count, 1)
        self.assertEqual(sorted(report.uncovered_entry_ids), ["e2", "e3"])

    def test_coverage_gaps_to_suggestions(self):
        report = compute_coverage(SEM, exercised_entry_ids=["e1", "e2"])
        sugs = coverage_gaps_to_suggestions(report)
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0].kind, "coverage_gap")
        self.assertEqual(sugs[0].semantic_map_entry_id, "e3")
        self.assertFalse(sugs[0].as_dict()["is_bug"])

    def test_full_coverage_no_gap(self):
        report = compute_coverage(SEM, exercised_entry_ids=["e1", "e2", "e3"])
        self.assertEqual(report.uncovered_entry_ids, [])
        self.assertEqual(coverage_gaps_to_suggestions(report), [])


if __name__ == "__main__":
    unittest.main()
