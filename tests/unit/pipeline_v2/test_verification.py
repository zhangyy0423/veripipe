import unittest

from pipeline_v2.schema import FailureSignal
from pipeline_v2.verification import (
    VerificationError,
    VerificationPolicy,
    build_l2_oracle_runner,
    classify_channel,
    run_reproduction_validation,
)
from pipeline_v2.oracle_l2 import evaluate_l2_oracle


def signal(oracle_level="L2", failure_type="assertion", nondeterministic=False, static=False):
    return FailureSignal(
        scenario_id="scenario",
        step_id="step",
        failure_type=failure_type,
        oracle_level=oracle_level,
        llm_involvement="none",
        expected_state={"ok": True},
        actual_state={"ok": False},
        nondeterministic_sources=["llm"] if nondeterministic else [],
        static_certified=static,
    )


class VerificationTest(unittest.TestCase):
    def test_oracle_level_maps_to_channel_with_l2_downgrade(self):
        policy = VerificationPolicy(agent_under_test=False)

        self.assertEqual(classify_channel(signal("L1", failure_type="crash"), policy).required_runs, 1)
        self.assertEqual(classify_channel(signal("L3"), policy).required_runs, 3)
        self.assertEqual(classify_channel(signal("L2", nondeterministic=True), policy).name, "weak")
        self.assertEqual(classify_channel(signal("L2", static=True), policy).name, "static-exempt")

    def test_agent_product_defaults_l2_to_weak_except_crash_l1(self):
        policy = VerificationPolicy(agent_under_test=True)

        self.assertEqual(classify_channel(signal("L2"), policy).name, "weak")
        self.assertEqual(classify_channel(signal("L1", failure_type="crash"), policy).name, "strong")

    def test_l4_has_no_verification_channel(self):
        with self.assertRaises(VerificationError):
            classify_channel(signal("L4"), VerificationPolicy())

    def test_weak_channel_requires_identical_fingerprints_and_stops_early(self):
        original = signal("L3")
        calls = []

        def runner():
            calls.append(1)
            if len(calls) == 1:
                return signal("L3")
            return signal("L3", failure_type="different")

        result = run_reproduction_validation(original, runner, VerificationPolicy(agent_under_test=False))

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "fingerprint-mismatch")
        self.assertEqual(result.attempted_runs, 2)

    def test_static_certified_signal_is_exempt_from_rerun(self):
        result = run_reproduction_validation(
            signal("L2", static=True),
            runner=lambda: (_ for _ in ()).throw(AssertionError("should not run")),
            policy=VerificationPolicy(),
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.attempted_runs, 0)

    def test_l2_oracle_failure_populates_signal_states_and_keeps_agent_weak_channel(self):
        entry = {
            "id": "sample.skill.change_mode.intent-to-mode",
            "expected_terminal_state": {
                "assertion": "current_mode == target_mode",
                "observable": "structured terminal mode",
                "assertion_type": "mode_state",
                "machine_check": {"field": "current_mode", "op": "eq", "value_from": "target_mode"},
            },
        }
        oracle = evaluate_l2_oracle(
            entry,
            {"current_mode": "exploration", "target_mode": "write_functions"},
            scenario_id="scenario",
            step_id="change-mode",
            llm_involvement="none",
        )

        self.assertEqual(oracle.status, "fail")
        self.assertIsNotNone(oracle.failure_signal)
        self.assertEqual(oracle.failure_signal.oracle_level, "L2")
        self.assertEqual(oracle.failure_signal.semantic_map_entry_id, entry["id"])
        self.assertEqual(oracle.failure_signal.failure_type, "mode_state")
        self.assertEqual(oracle.failure_signal.expected_state, oracle.expected_state)
        self.assertEqual(oracle.failure_signal.actual_state, oracle.actual_state)
        self.assertEqual(oracle.failure_signal.evidence["oracle_l2"]["status"], "fail")
        self.assertEqual(classify_channel(oracle.failure_signal, VerificationPolicy(agent_under_test=True)).name, "weak")

    def test_l2_oracle_runner_rechecks_observed_terminal_state_on_each_rerun(self):
        entry = {
            "id": "sample.skill.change_mode.intent-to-mode",
            "expected_terminal_state": {
                "assertion": "current_mode == target_mode",
                "observable": "structured terminal mode",
                "assertion_type": "mode_state",
                "machine_check": {"field": "current_mode", "op": "eq", "value_from": "target_mode"},
            },
        }
        original = evaluate_l2_oracle(
            entry,
            {"current_mode": "exploration", "target_mode": "write_functions"},
            scenario_id="scenario",
            step_id="change-mode",
            llm_involvement="none",
        )
        calls = []
        runner = build_l2_oracle_runner(
            entry,
            lambda: calls.append(1) or {"current_mode": "exploration", "target_mode": "write_functions"},
            scenario_id="scenario",
            step_id="change-mode",
            llm_involvement="none",
        )

        result = run_reproduction_validation(original.failure_signal, runner, VerificationPolicy(agent_under_test=True))

        self.assertTrue(result.passed)
        self.assertEqual(result.channel, "weak")
        self.assertEqual(result.attempted_runs, 3)
        self.assertEqual(len(calls), 3)

    def test_l2_oracle_runner_returns_none_when_rerun_terminal_state_passes(self):
        entry = {
            "id": "sample.skill.change_mode.intent-to-mode",
            "expected_terminal_state": {
                "assertion": "current_mode == target_mode",
                "observable": "structured terminal mode",
                "assertion_type": "mode_state",
                "machine_check": {"field": "current_mode", "op": "eq", "value_from": "target_mode"},
            },
        }
        runner = build_l2_oracle_runner(
            entry,
            lambda: {"current_mode": "write_functions", "target_mode": "write_functions"},
            scenario_id="scenario",
            step_id="change-mode",
            llm_involvement="none",
        )

        self.assertIsNone(runner())


if __name__ == "__main__":
    unittest.main()
