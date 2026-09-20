import unittest

from pipeline_v2.fingerprint import failure_fingerprint, fingerprint_source
from pipeline_v2.schema import FailureSignal


class FingerprintTest(unittest.TestCase):
    def test_agent_fingerprint_uses_structure_not_output_text(self):
        base = FailureSignal(
            scenario_id="skill:create-report",
            step_id="write-summary",
            failure_type="state-mismatch",
            oracle_level="L2",
            llm_involvement="none",
            expected_state={"file_created": True, "schema": {"title": "string"}},
            actual_state={"file_created": False, "schema": {"title": "string"}},
            output_text="first wording from an agent",
            error_text="human readable text one",
        )
        changed_text = FailureSignal(
            scenario_id=base.scenario_id,
            step_id=base.step_id,
            failure_type=base.failure_type,
            oracle_level=base.oracle_level,
            llm_involvement=base.llm_involvement,
            expected_state=base.expected_state,
            actual_state=base.actual_state,
            output_text="completely different wording",
            error_text="human readable text two",
        )
        changed_structure = FailureSignal(
            scenario_id=base.scenario_id,
            step_id="publish-summary",
            failure_type=base.failure_type,
            oracle_level=base.oracle_level,
            llm_involvement=base.llm_involvement,
            expected_state=base.expected_state,
            actual_state=base.actual_state,
        )

        self.assertEqual(failure_fingerprint(base), failure_fingerprint(changed_text))
        self.assertNotEqual(failure_fingerprint(base), failure_fingerprint(changed_structure))
        source = fingerprint_source(base)
        self.assertNotIn("first wording", source)
        self.assertNotIn("human readable text", source)

    def test_playwright_fingerprint_ignores_duration_and_line_column_drift(self):
        base = FailureSignal(
            scenario_id="spec:version-consistency.spec.ts",
            step_id="playwright",
            failure_type="release_state",
            oracle_level="L2",
            llm_involvement="none",
            expected_state={"playwright_spec": "passed", "spec": "version-consistency.spec.ts"},
            actual_state={
                "spec": "version-consistency.spec.ts",
                "passed": False,
                "duration": 1284,
                "failed_assertions": [
                    {
                        "title": "reports version",
                        "message": "backend version mismatch",
                        "location": "tests/integration/version-consistency.spec.ts:21:9",
                    }
                ],
            },
            stack_anchor="tests/integration/version-consistency.spec.ts:21:9",
            semantic_map_entry_id="sample.release.version-stamp-consistency",
        )
        changed_timing_and_anchor = FailureSignal(
            scenario_id=base.scenario_id,
            step_id=base.step_id,
            failure_type=base.failure_type,
            oracle_level=base.oracle_level,
            llm_involvement=base.llm_involvement,
            expected_state=base.expected_state,
            actual_state={
                "spec": "version-consistency.spec.ts",
                "passed": False,
                "duration": 9811,
                "failed_assertions": [
                    {
                        "title": "reports version",
                        "message": "different wording is ignored",
                        "location": "tests/integration/version-consistency.spec.ts:22:17",
                    }
                ],
            },
            stack_anchor="tests/integration/version-consistency.spec.ts:22:17",
            semantic_map_entry_id=base.semantic_map_entry_id,
        )

        self.assertEqual(failure_fingerprint(base), failure_fingerprint(changed_timing_and_anchor))


if __name__ == "__main__":
    unittest.main()
