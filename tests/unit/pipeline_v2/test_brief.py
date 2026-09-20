import json
import tempfile
import unittest
from pathlib import Path

from pipeline_v2.brief import BriefGenerator
from pipeline_v2.ledger import Ledger


class BriefGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "pipeline.sqlite")
        self.ledger.init_schema()
        self.generator = BriefGenerator(self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def add_batch(self, batch_id, reported, sentinel="passed", recall=None):
        self.ledger.upsert_batch(
            batch_id=batch_id,
            product_version="abc",
            environment_digest="env",
            signal_count=1,
            reported_count=reported,
            sentinel_status=sentinel,
            injected_recall_rate=recall,
        )

    def test_sentinel_trigger_short_circuits_markdown(self):
        self.add_batch("b1", reported=0, sentinel="triggered")

        markdown = self.generator.render("b1")

        self.assertIn("sentinel_status: triggered", markdown)
        self.assertIn("environment event", markdown)
        self.assertNotIn("| stage | count |", markdown)

    def test_redlines_are_detected_and_written_back(self):
        self.add_batch("b1", 0, recall=0.9)
        self.add_batch("b2", 0, recall=0.7)
        self.add_batch("b3", 0, recall=0.6)

        flags = self.generator.detect_redlines("b3", observation_size=51)

        self.assertIn("zero_report_output_3_batches", flags)
        self.assertIn("observation_size_over_soft_limit", flags)
        self.assertIn("injected_recall_drop", flags)
        row = self.ledger.get_batch("b3")
        self.assertEqual(json.loads(row["redline_flags"]), flags)

    def test_external_brief_omits_observation_details(self):
        self.ledger.upsert_batch(
            batch_id="b4",
            product_version="abc",
            environment_digest="env",
            signal_count=1,
            observation_entered_count=1,
            sentinel_status="passed",
        )
        self.ledger.upsert_case(
            fingerprint="sensitive-observation-fingerprint",
            status="observing",
            oracle_level="L3",
            llm_involvement="none",
            batch_id="b4",
        )

        markdown = self.generator.render("b4")

        self.assertIn("observation entered: 1", markdown)
        self.assertNotIn("sensitive-observation-fingerprint", markdown)


if __name__ == "__main__":
    unittest.main()
