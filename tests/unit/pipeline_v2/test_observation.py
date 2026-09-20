import tempfile
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger
from pipeline_v2.observation import ObservationStateMachine


class ObservationStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "pipeline.sqlite")
        self.ledger.init_schema()
        self.machine = ObservationStateMachine(self.ledger, upgrade_batches=3, expire_batches=10, expire_days=14)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_three_distinct_batches_upgrade_to_intermittent(self):
        for batch_id in ("b001", "b002", "b003"):
            self.machine.record_observation(
                fingerprint="fp-intermittent",
                batch_id=batch_id,
                oracle_level="L3",
                llm_involvement="none",
                failure_summary="same structural failure",
            )

        row = self.ledger.get_case("fp-intermittent")
        self.assertEqual(row["status"], "intermittent")

    def test_observation_expires_after_batch_gap(self):
        self.machine.record_observation(
            fingerprint="fp-old",
            batch_id="b001",
            oracle_level="L2",
            llm_involvement="none",
            failure_summary="old failure",
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
        )

        expired = self.machine.expire_stale(current_batch_id="b012")

        self.assertEqual(expired, ["fp-old"])
        self.assertEqual(self.ledger.get_case("fp-old")["status"], "expired")

    def test_same_window_batch_flaky_is_environment_event(self):
        for idx in range(3):
            self.machine.record_observation(
                fingerprint=f"fp-env-{idx}",
                batch_id="b100",
                oracle_level="L3",
                llm_involvement="none",
                failure_summary=f"unrelated {idx}",
            )

        event = self.machine.detect_batch_environment_event("b100", min_unrelated_fingerprints=3)

        self.assertTrue(event)
        for idx in range(3):
            self.assertEqual(self.ledger.get_case(f"fp-env-{idx}")["status"], "environment-event")

    def test_reported_cases_are_not_reclassified_as_environment_event(self):
        for idx in range(3):
            fingerprint = f"fp-reported-{idx}"
            self.ledger.upsert_case(
                fingerprint=fingerprint,
                status="reported",
                oracle_level="L3",
                llm_involvement="none",
                batch_id="b200",
            )
            self.ledger.record_hit(
                fingerprint=fingerprint,
                batch_id="b200",
                failure_summary=f"reported {idx}",
            )

        event = self.machine.detect_batch_environment_event("b200", min_unrelated_fingerprints=3)

        self.assertFalse(event)
        for idx in range(3):
            self.assertEqual(self.ledger.get_case(f"fp-reported-{idx}")["status"], "reported")


if __name__ == "__main__":
    unittest.main()
