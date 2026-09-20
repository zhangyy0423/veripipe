# -*- coding: utf-8 -*-
"""oracle_l3 单测：差分 + 蜕变，纯离线。"""
import unittest

from pipeline_v2.oracle_l3 import (
    RELATION_EQUAL,
    RELATION_RESTORED,
    RELATION_SUBSET,
    evaluate_differential,
    evaluate_metamorphic,
)


def st(d):
    return {"terminal_state": d, "event_sequence": ["a", "b"]}


class DifferentialTest(unittest.TestCase):
    def test_consistent_paths_pass(self):
        r = evaluate_differential(
            [st({"mode": "exploration"}), st({"mode": "exploration"})],
            scenario_id="s", step_id="ws", llm_involvement="clue_source",
        )
        self.assertEqual(r.status, "pass")
        self.assertIsNone(r.failure_signal)

    def test_inconsistent_paths_emit_l3_signal(self):
        r = evaluate_differential(
            [st({"mode": "exploration"}), st({"mode": "modify_ontology"})],
            scenario_id="s", step_id="ws", llm_involvement="clue_source",
            path_labels=["pathA", "pathB"], semantic_map_entry_id="e1",
        )
        self.assertEqual(r.status, "fail")
        self.assertIsNotNone(r.failure_signal)
        self.assertEqual(r.failure_signal.oracle_level, "L3")
        self.assertEqual(r.failure_signal.semantic_map_entry_id, "e1")
        r.failure_signal.validate_case_schema()

    def test_single_run_inconclusive(self):
        r = evaluate_differential([st({"x": 1})], scenario_id="s", step_id="ws", llm_involvement="none")
        self.assertEqual(r.status, "inconclusive")


class MetamorphicTest(unittest.TestCase):
    def test_restored_relation_holds(self):
        r = evaluate_metamorphic(
            relation=RELATION_RESTORED,
            base_state=st({"hidden": False, "items": 3}),
            transformed_state=st({"hidden": False, "items": 3}),
            scenario_id="s", step_id="ws", llm_involvement="clue_source",
            relation_id="hide-unhide",
        )
        self.assertEqual(r.status, "pass")

    def test_broken_equal_relation_emits_signal(self):
        r = evaluate_metamorphic(
            relation=RELATION_EQUAL,
            base_state=st({"mode": "exploration"}),
            transformed_state=st({"mode": "write_functions"}),
            scenario_id="s", step_id="ws", llm_involvement="clue_source",
            relation_id="add-irrelevant-step",
            semantic_map_entry_id="e1",
        )
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.failure_signal.oracle_level, "L3")
        self.assertTrue(r.failure_signal.failure_type.startswith("metamorphic_break:"))
        r.failure_signal.validate_case_schema()

    def test_subset_relation(self):
        ok = evaluate_metamorphic(
            relation=RELATION_SUBSET,
            base_state=st({"a": 1, "b": 2}),
            transformed_state=st({"a": 1}),
            scenario_id="s", step_id="ws", llm_involvement="none",
        )
        self.assertEqual(ok.status, "pass")
        bad = evaluate_metamorphic(
            relation=RELATION_SUBSET,
            base_state=st({"a": 1}),
            transformed_state=st({"a": 1, "c": 3}),
            scenario_id="s", step_id="ws", llm_involvement="none",
        )
        self.assertEqual(bad.status, "fail")

    def test_unknown_relation_inconclusive(self):
        r = evaluate_metamorphic(
            relation="totally-unknown",
            base_state=st({"a": 1}), transformed_state=st({"a": 1}),
            scenario_id="s", step_id="ws", llm_involvement="none",
        )
        self.assertEqual(r.status, "inconclusive")


if __name__ == "__main__":
    unittest.main()


class SiblingConsistencyTest(unittest.TestCase):
    def _obs(self, **kw):
        return {"sibling": kw["s"], "rejected": kw["r"]}

    def test_all_reject_consistent_pass(self):
        from pipeline_v2.oracle_l3 import evaluate_sibling_consistency
        r = evaluate_sibling_consistency(
            [self._obs(s="set_mode", r=True), self._obs(s="set_model", r=True), self._obs(s="set_workspace", r=True)],
            scenario_id="s", step_id="ws", llm_involvement="clue_source", group_id="state-setters",
        )
        self.assertEqual(r.status, "pass")
        self.assertIsNone(r.failure_signal)

    def test_one_accepts_among_rejectors_reported(self):
        from pipeline_v2.oracle_l3 import evaluate_sibling_consistency
        r = evaluate_sibling_consistency(
            [self._obs(s="set_mode", r=True), self._obs(s="set_model", r=True),
             self._obs(s="set_reasoning_effort", r=False)],
            scenario_id="s", step_id="ws", llm_involvement="clue_source", group_id="state-setters",
            semantic_map_entry_id="e1",
        )
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.failure_signal.oracle_level, "L3")
        self.assertEqual(r.failure_signal.failure_type, "sibling_validation_asymmetry")
        self.assertIn("set_reasoning_effort", r.detail["deviants"])
        r.failure_signal.validate_case_schema()

    def test_fewer_than_three_inconclusive(self):
        from pipeline_v2.oracle_l3 import evaluate_sibling_consistency
        r = evaluate_sibling_consistency(
            [self._obs(s="a", r=True), self._obs(s="b", r=False)],
            scenario_id="s", step_id="ws", llm_involvement="none",
        )
        self.assertEqual(r.status, "inconclusive")

    def test_missing_rejected_field_inconclusive(self):
        from pipeline_v2.oracle_l3 import evaluate_sibling_consistency
        r = evaluate_sibling_consistency(
            [{"sibling": "a"}, {"sibling": "b", "rejected": True}, {"sibling": "c", "rejected": True}],
            scenario_id="s", step_id="ws", llm_involvement="none",
        )
        self.assertEqual(r.status, "inconclusive")
