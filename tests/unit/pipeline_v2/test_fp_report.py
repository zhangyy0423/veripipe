# -*- coding: utf-8 -*-
"""Offline tests for the anti-false-positive funnel summary."""
import unittest

from pipeline_v2.fp_report import render, summarize


class FpReportTest(unittest.TestCase):
    def test_aggregates_and_rates(self):
        r1 = {"executed_count": 8, "reported_count": 1, "pass_count": 4,
              "no_signal_count": 2, "observed_count": 1}
        r2 = {"executed_count": 2, "reported_count": 1, "pass_count": 1,
              "no_signal_count": 0, "observed_count": 0}
        s = summarize([r1, r2])
        self.assertEqual(s.batches, 2)
        self.assertEqual(s.executed, 10)
        self.assertEqual(s.filed, 2)
        self.assertEqual(s.withheld, 8)
        self.assertEqual(s.withheld_breakdown["passed"], 5)
        self.assertEqual(s.withheld_breakdown["inconclusive_or_no_signal"], 2)
        self.assertEqual(s.filed_rate, 0.2)
        self.assertEqual(s.withheld_rate, 0.8)

    def test_empty_is_safe(self):
        s = summarize([])
        self.assertEqual((s.executed, s.filed, s.withheld), (0, 0, 0))
        self.assertEqual(s.filed_rate, 0.0)

    def test_ignores_bad_types(self):
        s = summarize([{"executed_count": "nan", "reported_count": True}])
        self.assertEqual((s.executed, s.filed), (0, 0))

    def test_render_has_headline_numbers(self):
        out = render(summarize([{"executed_count": 4, "reported_count": 1,
                                 "pass_count": 2, "no_signal_count": 1}]))
        self.assertIn("filed as machine-verified findings: 1", out)
        self.assertIn("filed rate: 25%", out)


if __name__ == "__main__":
    unittest.main()
