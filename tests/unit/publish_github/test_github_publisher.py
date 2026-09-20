# -*- coding: utf-8 -*-
"""Offline tests for the product-neutral GitHub publisher (no network)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared-skills" / "publish-github" / "scripts"))

import github_publisher as gp  # noqa: E402


def _event(kind="bug_report", *, fp="f" * 40, level="L2", llm="none",
           entry="p.skill.x", summary="step=playwright | anchor=a:1"):
    return {
        "event_id": "pub-" + fp[:8],
        "kind": kind,
        "payload": {
            "fingerprint": fp,
            "semantic_map_entry_id": entry,
            "oracle_level": level,
            "llm_involvement": llm,
            "failure_summary": summary,
        },
    }


class SelectTest(unittest.TestCase):
    def test_only_machine_verified_bug_reports(self):
        events = [
            _event(),                                   # eligible
            _event(kind="batch_brief"),                 # wrong kind
            _event(level="L4", fp="a" * 40),            # suggestion tier
            _event(llm="veto", fp="b" * 40),            # LLM-involved
            _event(fp=""),                              # missing fingerprint
        ]
        found = gp.select_findings(events, max_count=10)
        self.assertEqual([f.oracle_level for f in found], ["L2"])
        self.assertEqual(len(found), 1)

    def test_dedupe_and_cap(self):
        events = [_event(fp="c" * 40), _event(fp="c" * 40), _event(fp="d" * 40),
                  _event(fp="e" * 40)]
        self.assertEqual(len(gp.select_findings(events, max_count=10)), 3)  # deduped
        self.assertEqual(len(gp.select_findings(events, max_count=1)), 1)   # capped


class RenderTest(unittest.TestCase):
    def test_body_is_neutral_and_discloses_automation(self):
        finding = gp.select_findings([_event(fp="1" * 40)], max_count=1)[0]
        post = gp.render_post(finding, product_label="dsh")
        self.assertIn("[veripipe][dsh]", post.title)
        self.assertIn("11111111", post.title)
        self.assertIn("machine-verified", post.body.lower())
        self.assertIn("Filed automatically by", post.body)
        self.assertNotIn("payload", post.body.lower())


class PublishTest(unittest.TestCase):
    def test_dry_run_writes_drafts_and_never_posts(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            report = gp.publish(
                [_event(fp="2" * 40)], target="discussion", repo=None,
                product_label="dsh", dry_run=True, max_count=3,
                poster=lambda t, p: calls.append((t, p)) or "x", out_dir=Path(tmp),
            )
            self.assertTrue(report.dry_run)
            self.assertEqual(calls, [])  # poster never invoked in dry-run
            drafts = list(Path(tmp).glob("*.md"))
            self.assertEqual(len(drafts), 1)
            self.assertIn("machine-verified", drafts[0].read_text().lower())

    def test_post_uses_injected_poster(self):
        seen = []

        def fake_poster(target, post):
            seen.append((target, post.fingerprint))
            return "https://github.com/o/r/discussions/1"

        report = gp.publish(
            [_event(fp="3" * 40)], target="discussion", repo="o/r",
            product_label="dsh", dry_run=False, max_count=3, poster=fake_poster,
        )
        self.assertFalse(report.dry_run)
        self.assertTrue(report.results[0].posted)
        self.assertEqual(report.results[0].url, "https://github.com/o/r/discussions/1")
        self.assertEqual(seen, [("discussion", "3" * 40)])

    def test_bad_target_fails_closed(self):
        with self.assertRaises(gp.PublisherError):
            gp.publish([], target="wiki", repo=None, product_label="p",
                       dry_run=True, max_count=1)


class CliTest(unittest.TestCase):
    def test_post_without_yes_is_rejected(self):
        with self.assertRaises(SystemExit):
            gp.main(["--stdin", "--post", "--repo", "o/r"])

    def test_stdin_dry_run(self):
        payload = _event(fp="4" * 40)["payload"]
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            rc = gp.main(["--stdin", "--product-label", "dsh"])
        finally:
            sys.stdin = stdin
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
