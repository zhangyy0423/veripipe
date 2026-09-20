import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger


class LedgerSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pipeline.sqlite"
        self.ledger = Ledger(self.db_path)
        self.ledger.init_schema()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_cases_schema_rejects_l4_and_requires_llm_involvement(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.upsert_case(
                fingerprint="fp-l4",
                status="candidate",
                oracle_level="L4",
                llm_involvement="clue_source",
                batch_id="b1",
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.upsert_case(
                fingerprint="fp-empty-llm",
                status="candidate",
                oracle_level="L2",
                llm_involvement="",
                batch_id="b1",
            )

    def test_case_upsert_tracks_seen_batches_and_hit_count(self):
        self.ledger.upsert_case(
            fingerprint="fp1",
            status="candidate",
            oracle_level="L2",
            llm_involvement="none",
            batch_id="b1",
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
            verify_entry={"passed": True},
        )
        self.ledger.upsert_case(
            fingerprint="fp1",
            status="reported",
            oracle_level="L2",
            llm_involvement="none",
            batch_id="b2",
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
            verify_entry={"passed": True},
            report_ref="TRACKER-1",
        )

        row = self.ledger.get_case("fp1")
        self.assertEqual(row["first_seen_batch"], "b1")
        self.assertEqual(row["last_seen_batch"], "b2")
        self.assertEqual(row["hit_count"], 2)
        self.assertEqual(row["status"], "reported")
        self.assertEqual(row["report_ref"], "TRACKER-1")
        self.assertEqual(len(json.loads(row["verify_history"])), 2)

    def test_batches_store_funnel_counts_costs_and_redlines(self):
        self.ledger.upsert_batch(
            batch_id="b1",
            product_version="abc123",
            environment_digest="mac",
            signal_count=5,
            oracle_l2_count=3,
            reported_count=1,
            token_cost={"verify": 123},
            duration={"verify": 4.5},
            sentinel_status="passed",
            redline_flags=["sentinel_environment_event"],
        )

        row = self.ledger.get_batch("b1")
        self.assertEqual(row["signal_count"], 5)
        self.assertEqual(row["oracle_l2_count"], 3)
        self.assertEqual(json.loads(row["token_cost"]), {"verify": 123})
        self.assertEqual(json.loads(row["duration"]), {"verify": 4.5})
        self.assertEqual(json.loads(row["redline_flags"]), ["sentinel_environment_event"])

    def test_model_metrics_merge_without_erasing_existing_batch_metrics(self):
        self.ledger.upsert_batch(
            batch_id="b-metrics",
            token_cost={"verification": {"attempts": 4}},
            duration={"execution": {"duration_ms": 25}},
        )

        self.ledger.update_batch_metrics(
            "b-metrics",
            token_cost={"model_routing": {"input_tokens": 10}},
            duration={"model_routing": {"duration_ms": 5}},
        )

        row = self.ledger.get_batch("b-metrics")
        self.assertEqual(
            json.loads(row["token_cost"]),
            {
                "verification": {"attempts": 4},
                "model_routing": {"input_tokens": 10},
            },
        )
        self.assertEqual(
            json.loads(row["duration"]),
            {
                "execution": {"duration_ms": 25},
                "model_routing": {"duration_ms": 5},
            },
        )

    def test_triage_result_is_four_value_enum(self):
        self.ledger.upsert_case(
            fingerprint="fp2",
            status="reported",
            oracle_level="L1",
            llm_involvement="none",
            batch_id="b1",
        )
        self.ledger.update_triage_result("fp2", "confirmed")
        self.assertEqual(self.ledger.get_case("fp2")["triage_result"], "confirmed")

        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.update_triage_result("fp2", "needs-human-release")

    def test_l2_case_requires_semantic_map_entry_id(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.upsert_case(
                fingerprint="fp-l2-no-semantic",
                status="reported",
                oracle_level="L2",
                llm_involvement="material_generator",
                batch_id="b1",
            )

        self.ledger.upsert_case(
            fingerprint="fp-l2-semantic",
            status="reported",
            oracle_level="L2",
            llm_involvement="material_generator",
            batch_id="b1",
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
        )
        self.assertEqual(
            self.ledger.get_case("fp-l2-semantic")["semantic_map_entry_id"],
            "sample.skill.change_mode.intent-to-mode",
        )


if __name__ == "__main__":
    unittest.main()
