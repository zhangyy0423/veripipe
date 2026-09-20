import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from pipeline_v2.ledger import Ledger
from pipeline_v2 import publishing as publishing_module
from pipeline_v2.publishing import (
    CommandPublisher,
    PublishingCommandError,
    PublishingSafetyError,
    TriageSync,
    build_command_publisher_from_adapter,
    build_queue_event,
    flush_reviewed_queue,
    subprocess_json_runner,
)
from pipeline_v2.schema import FailureSignal


class PublishingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "pipeline.sqlite")
        self.ledger.init_schema()
        self.commands = []

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def runner(self, command, payload):
        self.commands.append((command, payload))
        if command == ["tracker", "create"]:
            return {"card_id": "SAMPLE-123"}
        if command == ["wiki", "publish"]:
            return {"page_id": "wiki-456"}
        if command == ["tracker", "status"]:
            return {"cards": [{"report_ref": "SAMPLE-123", "triage_result": "confirmed"}]}
        return {}

    def test_bug_report_creation_returns_report_ref(self):
        publisher = CommandPublisher(
            runner=self.runner,
            create_bug_command=["tracker", "create"],
            publish_wiki_command=["wiki", "publish"],
            status_command=["tracker", "status"],
        )
        signal = FailureSignal(
            scenario_id="s",
            step_id="step",
            failure_type="assertion",
            oracle_level="L2",
            llm_involvement="none",
            expected_state={"ok": True},
            actual_state={"ok": False},
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
        )

        report_ref = publisher.create_bug_report({"fingerprint": "fp1"}, signal)
        wiki_ref = publisher.publish_brief("b1", "# brief")

        self.assertEqual(report_ref, "SAMPLE-123")
        self.assertEqual(wiki_ref, "wiki-456")
        self.assertEqual(self.commands[0][0], ["tracker", "create"])
        self.assertEqual(self.commands[1][0], ["wiki", "publish"])

    def test_batch_start_status_sync_writes_triage_result(self):
        self.ledger.upsert_case(
            fingerprint="fp1",
            status="reported",
            oracle_level="L2",
            llm_involvement="none",
            batch_id="b1",
            semantic_map_entry_id="sample.skill.change_mode.intent-to-mode",
            report_ref="SAMPLE-123",
        )
        publisher = CommandPublisher(
            runner=self.runner,
            create_bug_command=["tracker", "create"],
            publish_wiki_command=["wiki", "publish"],
            status_command=["tracker", "status"],
        )

        updated = TriageSync(self.ledger, publisher).sync()

        self.assertEqual(updated, 1)
        self.assertEqual(self.ledger.get_case("fp1")["triage_result"], "confirmed")

    def test_queue_event_has_stable_identity_and_payload_hash(self):
        payload = {"fingerprint": "fp1", "failure_summary": "stable"}

        first = build_queue_event("bug_report", payload)
        second = build_queue_event("bug_report", payload)

        self.assertEqual(first, second)
        self.assertRegex(first["event_id"], r"^pub-[0-9a-f]{24}$")
        self.assertRegex(first["payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["schema_version"], 1)

    def test_guarded_flush_requires_env_yes_owner_and_safe_adapter(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-gated"})
        queue_path = Path(self.tmp.name) / "queue.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "owner": "owner@example",
            "owner_approved": True,
            "target_environment": "sandbox",
            "target_id": "publishing-test-sandbox",
            "approved_events": [
                {"event_id": event["event_id"], "payload_sha256": event["payload_sha256"]}
            ],
        }
        adapter = {
            "production_write_default": False,
            "publishing": {
                "enabled": True,
                "target_environment": "sandbox",
                "target_id": "publishing-test-sandbox",
            },
        }
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload)) or {"card_id": "CARD-1"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "VERIPIPE_PRODUCTION_WRITE"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest=manifest,
                adapter=adapter,
                publisher=publisher,
                production_env={},
                yes=True,
            )
        with self.assertRaisesRegex(PublishingSafetyError, "--yes"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest=manifest,
                adapter=adapter,
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=False,
            )
        unsafe_adapter = dict(adapter)
        unsafe_adapter["production_write_default"] = True
        with self.assertRaisesRegex(PublishingSafetyError, "production_write_default"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest=manifest,
                adapter=unsafe_adapter,
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
            )
        unapproved = dict(manifest)
        unapproved["owner_approved"] = False
        with self.assertRaisesRegex(PublishingSafetyError, "owner_approved"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest=unapproved,
                adapter=adapter,
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
            )

        self.assertEqual(calls, [])

    def test_guarded_flush_sends_only_exact_owner_approved_events(self):
        approved = build_queue_event("bug_report", {"fingerprint": "fp-approved"})
        skipped = build_queue_event("batch_brief", {"batch_id": "b-skipped", "markdown": "# skip"})
        queue_path = Path(self.tmp.name) / "queue-approved.jsonl"
        queue_path.write_text(
            "\n".join(json.dumps(item) for item in [approved, skipped]) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "owner": "owner@example",
            "owner_approved": True,
            "target_environment": "sandbox",
            "target_id": "publishing-test-sandbox",
            "approved_events": [
                {"event_id": approved["event_id"], "payload_sha256": approved["payload_sha256"]}
            ],
        }
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {
                "card_id": "CARD-APPROVED",
                "_publication": dict(payload["_publication"]),
            },
            create_bug_command=["tracker", "create"],
            publish_wiki_command=["wiki", "publish"],
        )

        result = flush_reviewed_queue(
            queue_path=queue_path,
            review_manifest=manifest,
            adapter={
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-test-sandbox",
                },
            },
            publisher=publisher,
            production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
            yes=True,
        )

        self.assertEqual(result["flushed_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(calls[0][0], ["tracker", "create"])
        self.assertEqual(calls[0][1]["fingerprint"], "fp-approved")
        self.assertEqual(calls[0][1]["_publication"]["event_id"], approved["event_id"])
        self.assertEqual(calls[0][1]["_publication"]["target_environment"], "sandbox")
        self.assertEqual(calls[0][1]["_publication"]["target_id"], "publishing-test-sandbox")
        self.assertEqual(result["target_environment"], "sandbox")
        self.assertEqual(result["target_id"], "publishing-test-sandbox")

    def test_guarded_flush_receipt_blocks_repeat_publication(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-once"})
        queue_path = Path(self.tmp.name) / "queue-once.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        receipt_path = Path(self.tmp.name) / "published.jsonl"
        manifest = {
            "schema_version": 1,
            "owner": "owner@example",
            "owner_approved": True,
            "target_environment": "sandbox",
            "target_id": "publishing-test-sandbox",
            "approved_events": [
                {"event_id": event["event_id"], "payload_sha256": event["payload_sha256"]}
            ],
        }
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {
                "card_id": "CARD-ONCE",
                "_publication": dict(payload["_publication"]),
            },
            create_bug_command=["tracker", "create"],
        )
        args = {
            "queue_path": queue_path,
            "review_manifest": manifest,
            "adapter": {
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-test-sandbox",
                },
            },
            "publisher": publisher,
            "production_env": {"VERIPIPE_PRODUCTION_WRITE": "1"},
            "yes": True,
            "receipt_path": receipt_path,
        }

        first = flush_reviewed_queue(**args)
        self.assertEqual(first["flushed_count"], 1)
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 3)
        self.assertEqual(receipt["evidence_level"], "owner-approved-sandbox-pilot")
        self.assertEqual(receipt["target_environment"], "sandbox")
        self.assertEqual(receipt["target_id"], "publishing-test-sandbox")

        with self.assertRaisesRegex(PublishingSafetyError, "already published"):
            flush_reviewed_queue(**args)

        self.assertEqual(len(calls), 1)

    def test_guarded_flush_rejects_non_sandbox_target_before_command(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-production"})
        queue_path = Path(self.tmp.name) / "queue-production.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {"card_id": "MUST-NOT-RUN"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "sandbox|test target"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "production",
                    "target_id": "tracker-production-space",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "production",
                        "target_id": "tracker-production-space",
                    },
                },
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
            )

        self.assertEqual(calls, [])

    def test_guarded_flush_rejects_secret_fields_in_runtime_documents(self):
        mutations = (
            ("adapter", lambda adapter, manifest, payload: adapter.update({"api_key": "secret"})),
            ("manifest", lambda adapter, manifest, payload: manifest.update({"cookie": "secret"})),
            ("queue", lambda adapter, manifest, payload: payload.update({"authorization": "secret"})),
        )

        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                payload = {"fingerprint": f"fp-secret-{label}"}
                adapter = {
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "publishing-secret-sandbox",
                    },
                }
                manifest = {
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-secret-sandbox",
                }
                mutate(adapter, manifest, payload)
                event = build_queue_event("bug_report", payload)
                manifest["approved_events"] = [
                    {
                        "event_id": event["event_id"],
                        "payload_sha256": event["payload_sha256"],
                    }
                ]
                queue_path = Path(temporary) / "queue.jsonl"
                queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
                calls = []
                publisher = CommandPublisher(
                    runner=lambda command, command_payload: calls.append(
                        (command, command_payload)
                    )
                    or {"card_id": "MUST-NOT-RUN"},
                    create_bug_command=["tracker", "create"],
                )

                with self.assertRaisesRegex(PublishingSafetyError, "secret|credential"):
                    flush_reviewed_queue(
                        queue_path=queue_path,
                        review_manifest=manifest,
                        adapter=adapter,
                        publisher=publisher,
                        production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                        yes=True,
                    )

                self.assertEqual(calls, [])

    def test_command_adapter_rejects_credential_arguments(self):
        with self.assertRaisesRegex(PublishingSafetyError, "secret|credential"):
            build_command_publisher_from_adapter(
                {
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "publishing-command-sandbox",
                        "create_bug_command": [
                            "/opt/publisher",
                            "--token=must-not-be-here",
                        ],
                        "publish_wiki_command": [],
                        "status_command": [],
                    },
                }
            )

    def test_command_adapter_rejects_explicit_shell_wrapper(self):
        with self.assertRaisesRegex(PublishingSafetyError, "shell"):
            build_command_publisher_from_adapter(
                {
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "publishing-command-sandbox",
                        "create_bug_command": [
                            "/usr/bin/env",
                            "bash",
                            "-c",
                            "publisher --json",
                        ],
                        "publish_wiki_command": [],
                        "status_command": [],
                    },
                }
            )

    def test_guarded_flush_rejects_publisher_credential_arguments(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-publisher-secret"})
        queue_path = Path(self.tmp.name) / "queue-publisher-secret.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {"card_id": "MUST-NOT-RUN"},
            create_bug_command=["/opt/publisher", "--api-key", "must-not-be-here"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "secret|credential"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-command-sandbox",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "publishing-command-sandbox",
                    },
                },
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
            )

        self.assertEqual(calls, [])

    def test_guarded_flush_rejects_legacy_unscoped_receipt(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-legacy-receipt"})
        queue_path = Path(self.tmp.name) / "queue-legacy-receipt.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        receipt_path = Path(self.tmp.name) / "legacy-receipt.jsonl"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": event["event_id"],
                    "payload_sha256": event["payload_sha256"],
                    "owner": "owner@example",
                    "ref": "legacy-ref",
                    "published_at": "2026-07-11T08:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        publisher = CommandPublisher(
            runner=lambda _command, _payload: {"card_id": "MUST-NOT-RUN"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "receipt schema_version 3"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "owner-approved-sandbox",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "owner-approved-sandbox",
                    },
                },
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
                receipt_path=receipt_path,
            )

    def test_receipt_v2_without_evidence_level_is_legacy_and_blocked(self):
        receipt_path = Path(self.tmp.name) / "legacy-v2-receipt.jsonl"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "event_id": "legacy-event",
                    "payload_sha256": "a" * 64,
                    "owner": "owner@example",
                    "target_environment": "sandbox",
                    "target_id": "legacy-sandbox",
                    "ref": "legacy-ref",
                    "published_at": "2026-07-11T08:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PublishingSafetyError, "schema_version 3"):
            publishing_module._load_published_receipts(
                receipt_path,
                target_environment="sandbox",
                target_id="legacy-sandbox",
                evidence_level="owner-approved-sandbox-pilot",
            )

    def test_fixture_receipt_cannot_satisfy_real_sandbox_evidence(self):
        receipt_path = Path(self.tmp.name) / "fixture-receipt.jsonl"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "evidence_level": "owner-approved-test-fixture",
                    "event_id": "fixture-event",
                    "payload_sha256": "a" * 64,
                    "owner": "owner@example",
                    "target_environment": "sandbox",
                    "target_id": "fixture-sandbox",
                    "ref": "fixture-ref",
                    "published_at": "2026-07-11T08:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PublishingSafetyError, "evidence_level"):
            publishing_module._load_published_receipts(
                receipt_path,
                target_environment="sandbox",
                target_id="fixture-sandbox",
                evidence_level="owner-approved-sandbox-pilot",
            )

    def test_authority_json_object_rejects_duplicate_keys(self):
        manifest_path = Path(self.tmp.name) / "duplicate-manifest.json"
        manifest_path.write_text(
            '{"owner_approved":false,"owner_approved":true}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PublishingSafetyError, "duplicate JSON key"):
            publishing_module._load_json_object(manifest_path, "review manifest")

    def test_external_command_response_rejects_duplicate_keys(self):
        command_path = Path(self.tmp.name) / "duplicate-response.py"
        command_path.write_text(
            "print('{\"status\":\"first\",\"status\":\"second\"}')\n",
            encoding="utf-8",
        )

        with self.assertRaises(PublishingCommandError) as caught:
            subprocess_json_runner([sys.executable, str(command_path)], {})

        self.assertEqual(caught.exception.classification, "invalid-response")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("JSON object", str(caught.exception))

    def test_queue_rejects_duplicate_event_identity_keys(self):
        queue_path = Path(self.tmp.name) / "duplicate-queue.jsonl"
        queue_path.write_text(
            '{"schema_version":1,"event_id":"first","event_id":"second",'
            '"kind":"bug_report","payload_sha256":"' + "a" * 64 + '","payload":{}}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PublishingSafetyError, "duplicate JSON key"):
            publishing_module._load_validated_queue(queue_path)

    def test_receipt_rejects_duplicate_target_or_evidence_keys(self):
        receipt_path = Path(self.tmp.name) / "duplicate-receipt.jsonl"
        receipt_path.write_text(
            '{"schema_version":3,"evidence_level":"owner-approved-test-fixture",'
            '"evidence_level":"owner-approved-sandbox-pilot","event_id":"event",'
            '"payload_sha256":"' + "a" * 64 + '","owner":"owner@example",'
            '"target_environment":"sandbox","target_id":"target",'
            '"ref":"ref","published_at":"2026-07-11T08:00:00Z"}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PublishingSafetyError, "duplicate JSON key"):
            publishing_module._load_published_receipts(
                receipt_path,
                target_environment="sandbox",
                target_id="target",
                evidence_level="owner-approved-sandbox-pilot",
            )

    def test_guarded_flush_rejects_owner_target_drift_before_command(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-target-drift"})
        queue_path = Path(self.tmp.name) / "queue-target-drift.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {"card_id": "MUST-NOT-RUN"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "target"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "owner-approved-sandbox",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "different-sandbox",
                    },
                },
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
            )

        self.assertEqual(calls, [])

    def test_guarded_flush_rejects_receipt_from_different_target(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-receipt-target"})
        queue_path = Path(self.tmp.name) / "queue-receipt-target.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        receipt_path = Path(self.tmp.name) / "receipt-other-target.jsonl"
        receipt_path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "evidence_level": "owner-approved-sandbox-pilot",
                    "event_id": event["event_id"],
                    "payload_sha256": event["payload_sha256"],
                    "owner": "owner@example",
                    "target_environment": "sandbox",
                    "target_id": "different-sandbox",
                    "ref": "sandbox-existing",
                    "published_at": "2026-07-11T08:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload))
            or {"card_id": "MUST-NOT-RUN"},
            create_bug_command=["tracker", "create"],
        )

        with self.assertRaisesRegex(PublishingSafetyError, "receipt target"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "owner-approved-sandbox",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "owner-approved-sandbox",
                    },
                },
                publisher=publisher,
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
                receipt_path=receipt_path,
            )

        self.assertEqual(calls, [])

    def test_guarded_flush_rejects_mismatched_response_attestation_without_receipt(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-response-target"})
        queue_path = Path(self.tmp.name) / "queue-response-target.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        receipt_path = Path(self.tmp.name) / "response-target-receipt.jsonl"

        calls = []

        def runner(_command, payload):
            calls.append(payload["_publication"]["event_id"])
            publication = dict(payload["_publication"])
            publication["target_id"] = "different-sandbox"
            return {
                "card_id": "sandbox-wrong-target",
                "_publication": publication,
            }

        result = flush_reviewed_queue(
            queue_path=queue_path,
            review_manifest={
                "schema_version": 1,
                "owner": "owner@example",
                "owner_approved": True,
                "target_environment": "sandbox",
                "target_id": "owner-approved-sandbox",
                "approved_events": [
                    {
                        "event_id": event["event_id"],
                        "payload_sha256": event["payload_sha256"],
                    }
                ],
            },
            adapter={
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "owner-approved-sandbox",
                },
            },
            publisher=CommandPublisher(
                runner=runner,
                create_bug_command=["sandbox", "create"],
            ),
            production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
            yes=True,
            receipt_path=receipt_path,
        )

        self.assertEqual(result["status"], "partial-failure")
        self.assertEqual(result["failures"][0]["classification"], "invalid-response")
        self.assertFalse(result["failures"][0]["event_retry_allowed"])
        self.assertFalse(receipt_path.exists())
        self.assertTrue(Path(result["failures"][0]["claim_path"]).is_file())

        with self.assertRaisesRegex(PublishingSafetyError, "unresolved publication claim"):
            flush_reviewed_queue(
                queue_path=queue_path,
                review_manifest={
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "owner-approved-sandbox",
                    "approved_events": [
                        {
                            "event_id": event["event_id"],
                            "payload_sha256": event["payload_sha256"],
                        }
                    ],
                },
                adapter={
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "owner-approved-sandbox",
                    },
                },
                publisher=CommandPublisher(
                    runner=runner,
                    create_bug_command=["sandbox", "create"],
                ),
                production_env={"VERIPIPE_PRODUCTION_WRITE": "1"},
                yes=True,
                receipt_path=receipt_path,
            )

        self.assertEqual(calls, [event["event_id"]])

    def test_partial_failure_claim_blocks_unsafe_automatic_retry(self):
        first = build_queue_event("bug_report", {"fingerprint": "fp-first"})
        second = build_queue_event("bug_report", {"fingerprint": "fp-second"})
        queue_path = Path(self.tmp.name) / "queue-partial.jsonl"
        queue_path.write_text(
            json.dumps(first) + "\n" + json.dumps(second) + "\n",
            encoding="utf-8",
        )
        receipt_path = Path(self.tmp.name) / "partial-receipt.jsonl"
        manifest = {
            "schema_version": 1,
            "owner": "owner@example",
            "owner_approved": True,
            "target_environment": "sandbox",
            "target_id": "publishing-test-sandbox",
            "approved_events": [
                {"event_id": item["event_id"], "payload_sha256": item["payload_sha256"]}
                for item in (first, second)
            ],
        }
        attempts = []
        fail_second_once = {"value": True}

        def runner(_command, payload):
            event_id = payload["_publication"]["event_id"]
            attempts.append(event_id)
            if event_id == second["event_id"] and fail_second_once["value"]:
                fail_second_once["value"] = False
                raise PublishingCommandError(
                    "publisher command timed out; output suppressed",
                    classification="timeout",
                    retryable=True,
                )
            return {
                "card_id": f"CARD-{event_id[-6:]}",
                "_publication": dict(payload["_publication"]),
            }

        publisher = CommandPublisher(
            runner=runner,
            create_bug_command=["tracker", "create"],
        )
        args = {
            "queue_path": queue_path,
            "review_manifest": manifest,
            "adapter": {
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-test-sandbox",
                },
            },
            "publisher": publisher,
            "production_env": {"VERIPIPE_PRODUCTION_WRITE": "1"},
            "yes": True,
            "receipt_path": receipt_path,
        }

        partial = flush_reviewed_queue(**args)
        self.assertEqual(partial["status"], "partial-failure")
        self.assertEqual(partial["flushed_count"], 1)
        self.assertEqual(partial["failed_count"], 1)
        self.assertEqual(partial["failures"][0]["classification"], "timeout")
        self.assertTrue(partial["failures"][0]["retryable"])
        self.assertFalse(partial["failures"][0]["event_retry_allowed"])

        self.assertTrue(Path(partial["failures"][0]["claim_path"]).is_file())

        with self.assertRaisesRegex(PublishingSafetyError, "unresolved publication claim"):
            flush_reviewed_queue(**args)

        self.assertEqual(attempts.count(first["event_id"]), 1)
        self.assertEqual(attempts.count(second["event_id"]), 1)

    def test_guarded_flush_rejects_tamper_and_duplicate_before_commands(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-original"})
        tampered = dict(event)
        tampered["payload"] = {"fingerprint": "fp-tampered"}
        queue_path = Path(self.tmp.name) / "queue-tampered.jsonl"
        queue_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "owner": "owner@example",
            "owner_approved": True,
            "target_environment": "sandbox",
            "target_id": "publishing-test-sandbox",
            "approved_events": [
                {"event_id": event["event_id"], "payload_sha256": event["payload_sha256"]}
            ],
        }
        calls = []
        publisher = CommandPublisher(
            runner=lambda command, payload: calls.append((command, payload)) or {},
            create_bug_command=["tracker", "create"],
        )
        args = {
            "review_manifest": manifest,
            "adapter": {
                "production_write_default": False,
                "publishing": {
                    "enabled": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-test-sandbox",
                },
            },
            "publisher": publisher,
            "production_env": {"VERIPIPE_PRODUCTION_WRITE": "1"},
            "yes": True,
        }

        with self.assertRaisesRegex(PublishingSafetyError, "payload hash"):
            flush_reviewed_queue(queue_path=queue_path, **args)

        queue_path.write_text(
            json.dumps(event) + "\n" + json.dumps(event) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PublishingSafetyError, "duplicate"):
            flush_reviewed_queue(queue_path=queue_path, **args)

        self.assertEqual(calls, [])

    def test_flush_cli_keeps_disabled_adapter_blocked_without_commands(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-cli"})
        queue_path = Path(self.tmp.name) / "queue-cli.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        manifest_path = Path(self.tmp.name) / "review.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-cli-sandbox",
                    "approved_events": [
                        {"event_id": event["event_id"], "payload_sha256": event["payload_sha256"]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        adapter_path = Path(self.tmp.name) / "adapter.json"
        adapter_path.write_text(
            json.dumps(
                {
                    "production_write_default": False,
                    "publishing": {
                        "enabled": False,
                        "create_bug_command": [],
                        "publish_wiki_command": [],
                        "status_command": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"VERIPIPE_PRODUCTION_WRITE": "1"}, clear=True), redirect_stdout(stdout):
            rc = publishing_module.main(
                [
                    "flush",
                    "--queue",
                    str(queue_path),
                    "--review-manifest",
                    str(manifest_path),
                    "--adapter",
                    str(adapter_path),
                    "--yes",
                ]
            )

        self.assertEqual(rc, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("publishing.enabled", result["error"])

    def test_flush_cli_runs_sandbox_json_command_without_shell(self):
        event = build_queue_event("bug_report", {"fingerprint": "fp-sandbox"})
        queue_path = Path(self.tmp.name) / "queue-sandbox.jsonl"
        queue_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        manifest_path = Path(self.tmp.name) / "review-sandbox.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "owner": "owner@example",
                    "owner_approved": True,
                    "target_environment": "sandbox",
                    "target_id": "publishing-cli-sandbox",
                    "approved_events": [
                        {"event_id": event["event_id"], "payload_sha256": event["payload_sha256"]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        sandbox_command = Path(__file__).resolve().parents[2] / "fixtures/publishing/sandbox-command.py"
        adapter_path = Path(self.tmp.name) / "adapter-sandbox.json"
        adapter_path.write_text(
            json.dumps(
                {
                    "production_write_default": False,
                    "publishing": {
                        "enabled": True,
                        "target_environment": "sandbox",
                        "target_id": "publishing-cli-sandbox",
                        "create_bug_command": [sys.executable, str(sandbox_command), "create-bug"],
                        "publish_wiki_command": [],
                        "status_command": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        receipt_path = Path(self.tmp.name) / "sandbox-receipt.jsonl"
        blocked_stdout = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"VERIPIPE_PRODUCTION_WRITE": "1"},
            clear=True,
        ), redirect_stdout(blocked_stdout):
            blocked_rc = publishing_module.main(
                [
                    "flush",
                    "--queue",
                    str(queue_path),
                    "--review-manifest",
                    str(manifest_path),
                    "--adapter",
                    str(adapter_path),
                    "--receipt",
                    str(receipt_path),
                    "--yes",
                ]
            )

        self.assertEqual(blocked_rc, 1)
        blocked = json.loads(blocked_stdout.getvalue())
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("fixture", blocked["error"].lower())
        self.assertFalse(receipt_path.exists())

        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"VERIPIPE_PRODUCTION_WRITE": "1"},
            clear=True,
        ), redirect_stdout(stdout):
            rc = publishing_module.main(
                [
                    "flush",
                    "--queue",
                    str(queue_path),
                    "--review-manifest",
                    str(manifest_path),
                    "--adapter",
                    str(adapter_path),
                    "--receipt",
                    str(receipt_path),
                    "--fixture-only",
                    "--yes",
                ]
            )

        self.assertEqual(rc, 0, stdout.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "flushed")
        self.assertEqual(result["evidence_level"], "owner-approved-test-fixture")
        self.assertEqual(result["refs"][0]["ref"], f"sandbox-{event['event_id']}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 3)
        self.assertEqual(receipt["evidence_level"], "owner-approved-test-fixture")
        self.assertEqual(receipt["event_id"], event["event_id"])
        self.assertEqual(receipt["target_environment"], "sandbox")
        self.assertEqual(receipt["target_id"], "publishing-cli-sandbox")
        claim_files = list(Path(str(receipt_path) + ".claims").glob("*.json"))
        self.assertEqual(len(claim_files), 1)
        claim = json.loads(claim_files[0].read_text(encoding="utf-8"))
        self.assertEqual(claim["schema_version"], 2)
        self.assertEqual(claim["evidence_level"], "owner-approved-test-fixture")


if __name__ == "__main__":
    unittest.main()
