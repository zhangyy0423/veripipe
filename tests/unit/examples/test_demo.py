# -*- coding: utf-8 -*-
"""The neutral demo must run and show the three verdicts (offline)."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class DemoTest(unittest.TestCase):
    def test_demo_runs_and_shows_three_verdicts(self):
        proc = subprocess.run([sys.executable, str(ROOT / "examples" / "demo.py")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("status=pass", out)
        self.assertIn("status=fail -> FILED", out)
        self.assertIn("inconclusive -> NOT filed", out)


if __name__ == "__main__":
    unittest.main()
