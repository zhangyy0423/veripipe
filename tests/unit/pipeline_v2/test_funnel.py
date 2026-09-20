import tempfile
import unittest
from pathlib import Path

from pipeline_v2.funnel import Funnel, FreshnessResult, TriageInput
from pipeline_v2.ledger import Ledger
from pipeline_v2.schema import FailureSignal, SchemaError
from pipeline_v2.verification import VerificationPolicy


def failure(oracle_level="L2", failure_type="assertion", static=False):
    return FailureSignal(
        scenario_id="guide:create-doc",
        step_id="assert-file",
        failure_type=failure_type,
        oracle_level=oracle_level,
        llm_involvement="material_generator",
        expected_state={"created": True},
        actual_state={"created": False},
        static_certified=static,
        semantic_map_entry_id="skill.create-doc.001",
    )


class FunnelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "pipeline.sqlite")
        self.ledger.init_schema()
        self.published = []

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_sentinel_failure_short_circuits_whole_batch(self):
        funnel = Funnel(self.ledger, publisher=lambda case, signal: self.published.append(case))
        result = funnel.process_batch(
            TriageInput(
                batch_id="b1",
                product_version="abc",
                environment_digest="env",
                sentinel_status="triggered",
                signals=[failure()],
            )
        )

        self.assertEqual(result.status, "environment-event")
        self.assertEqual(result.reported, [])
        self.assertEqual(self.ledger.list_cases(), [])
        self.assertEqual(self.ledger.get_batch("b1")["sentinel_status"], "triggered")

    def test_stale_l2_contract_is_reported_as_contract_mismatch(self):
        def freshness(_signal):
            return FreshnessResult(fresh=True, contract_mismatch=True, reason="recent intentional change")

        funnel = Funnel(
            self.ledger,
            verification_runner=lambda: failure(static=True),
            freshness_checker=freshness,
            publisher=lambda case, signal: self.published.append((case, signal)),
            policy=VerificationPolicy(),
        )
        result = funnel.process_batch(
            TriageInput(
                batch_id="b2",
                product_version="abc",
                environment_digest="env",
                sentinel_status="passed",
                signals=[failure(static=True)],
            )
        )

        self.assertEqual(len(result.reported), 1)
        row = self.ledger.get_case(result.reported[0])
        self.assertEqual(row["status"], "reported")
        self.assertEqual(row["triage_result"], "contract-mismatch-doc")
        self.assertEqual(len(self.published), 1)

    def test_flaky_verification_enters_observation_without_publish(self):
        calls = []

        def runner():
            calls.append(1)
            return failure(oracle_level="L3", failure_type="different")

        funnel = Funnel(
            self.ledger,
            verification_runner=runner,
            publisher=lambda case, signal: self.published.append((case, signal)),
            policy=VerificationPolicy(agent_under_test=False),
        )
        result = funnel.process_batch(
            TriageInput(
                batch_id="b3",
                product_version="abc",
                environment_digest="env",
                sentinel_status="passed",
                signals=[failure(oracle_level="L3")],
            )
        )

        self.assertEqual(result.observed_count, 1)
        self.assertEqual(result.reported, [])
        self.assertEqual(self.published, [])

    def test_l2_signal_without_semantic_map_entry_is_rejected(self):
        signal = failure()
        signal = FailureSignal(
            scenario_id=signal.scenario_id,
            step_id=signal.step_id,
            failure_type=signal.failure_type,
            oracle_level=signal.oracle_level,
            llm_involvement=signal.llm_involvement,
            expected_state=signal.expected_state,
            actual_state=signal.actual_state,
        )
        funnel = Funnel(self.ledger, verification_runner=lambda: signal)

        with self.assertRaises(SchemaError):
            funnel.process_batch(
                TriageInput(
                    batch_id="b4",
                    product_version="abc",
                    environment_digest="env",
                    sentinel_status="passed",
                    signals=[signal],
                )
            )


if __name__ == "__main__":
    unittest.main()
