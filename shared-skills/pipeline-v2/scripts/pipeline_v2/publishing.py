"""Publication and triage-status sync adapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .command_safety import explicit_shell_executable
from .ledger import Ledger
from .schema import FailureSignal, TRIAGE_RESULTS


CommandRunner = Callable[[List[str], Dict[str, object]], Dict[str, object]]
PUBLISHABLE_EVENT_KINDS = {"bug_report", "batch_brief"}
DEFAULT_PUBLISH_TIMEOUT_SEC = 300
REAL_PUBLISHING_EVIDENCE_LEVEL = "owner-approved-sandbox-pilot"
FIXTURE_PUBLISHING_EVIDENCE_LEVEL = "owner-approved-test-fixture"
PUBLISHING_EVIDENCE_LEVELS = {
    REAL_PUBLISHING_EVIDENCE_LEVEL,
    FIXTURE_PUBLISHING_EVIDENCE_LEVEL,
}
RECEIPT_SCHEMA_VERSION = 3
CLAIM_SCHEMA_VERSION = 2
SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
COMMAND_CREDENTIAL_RE = re.compile(
    r"(?:^|--?)(?:api[_-]?key|authorization|cookie|password|secret|token)=",
    re.IGNORECASE,
)
COMMAND_CREDENTIAL_FLAGS = {
    "--api-key",
    "--apikey",
    "--authorization",
    "--cookie",
    "--password",
    "--secret",
    "--token",
}


class PublishingSafetyError(ValueError):
    """Raised before any external command when publishing gates are incomplete."""


class PublishingCommandError(PublishingSafetyError):
    """Sanitized external-command failure with transport retry classification."""

    def __init__(self, message: str, *, classification: str, retryable: bool):
        super().__init__(message)
        self.classification = classification
        self.retryable = retryable


class DuplicateJsonKeyError(ValueError):
    """Raised when a publishing authority document is ambiguous JSON."""


def _strict_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads_strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_strict_json_object)


def _reject_secret_fields(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in SECRET_FIELD_NAMES:
                raise PublishingSafetyError(
                    f"secret or credential field is forbidden at {path}.{key}"
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_queue_event(kind: str, payload: Mapping[str, object]) -> Dict[str, object]:
    """Build a stable, reviewable queue event identity."""

    if kind not in PUBLISHABLE_EVENT_KINDS:
        raise PublishingSafetyError(f"unsupported publishing event kind: {kind}")
    normalized_payload = dict(payload)
    payload_sha256 = hashlib.sha256(_canonical_json(normalized_payload).encode("utf-8")).hexdigest()
    event_id = "pub-" + hashlib.sha256(f"{kind}\n{payload_sha256}".encode("utf-8")).hexdigest()[:24]
    return {
        "schema_version": 1,
        "event_id": event_id,
        "kind": kind,
        "payload_sha256": payload_sha256,
        "payload": normalized_payload,
    }


def subprocess_json_runner(command: List[str], payload: Dict[str, object]) -> Dict[str, object]:
    """Run a JSON-stdin/JSON-stdout command adapter."""

    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_PUBLISH_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishingCommandError(
            "publisher command timed out; output suppressed",
            classification="timeout",
            retryable=True,
        ) from exc
    if proc.returncode != 0:
        raise PublishingCommandError(
            f"publisher command failed with exit code {proc.returncode}; stderr suppressed",
            classification="command-exit",
            retryable=proc.returncode in {75, 111},
        )
    if not proc.stdout.strip():
        return {}
    try:
        data = _loads_strict_json(proc.stdout)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise PublishingCommandError(
            "publisher command must return a JSON object",
            classification="invalid-response",
            retryable=False,
        ) from exc
    if not isinstance(data, dict):
        raise PublishingCommandError(
            "publisher command must return a JSON object",
            classification="invalid-response",
            retryable=False,
        )
    return data


class CommandPublisher:
    """Adapter for existing issue-tracker/wiki skills or command wrappers."""

    def __init__(
        self,
        *,
        runner: Optional[CommandRunner] = None,
        create_bug_command: Optional[List[str]] = None,
        publish_wiki_command: Optional[List[str]] = None,
        status_command: Optional[List[str]] = None,
    ):
        self.runner = runner or subprocess_json_runner
        self.create_bug_command = create_bug_command or []
        self.publish_wiki_command = publish_wiki_command or []
        self.status_command = status_command or []

    def _run(self, command: List[str], payload: Dict[str, object]) -> Dict[str, object]:
        try:
            result = self.runner(command, payload)
        except PublishingCommandError:
            raise
        except PublishingSafetyError as exc:
            raise PublishingCommandError(
                str(exc),
                classification="safety-error",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise PublishingCommandError(
                f"publisher runner failed with {type(exc).__name__}; output suppressed",
                classification="runner-error",
                retryable=False,
            ) from exc
        if not isinstance(result, dict):
            raise PublishingCommandError(
                "publisher runner must return a JSON object",
                classification="invalid-response",
                retryable=False,
            )
        return result

    def create_bug_report(self, case: Dict[str, object], signal: FailureSignal) -> Optional[str]:
        """Create an issue-tracker bug report and return the card id."""

        if not self.create_bug_command:
            return None
        payload = {
            "case": case,
            "fingerprint": case.get("fingerprint"),
            "oracle_level": signal.oracle_level,
            "llm_involvement": signal.llm_involvement,
            "semantic_map_entry_id": signal.semantic_map_entry_id,
            "failure_summary": signal.failure_summary(),
            "evidence": signal.evidence,
        }
        result = self._run(self.create_bug_command, payload)
        return str(result.get("card_id") or result.get("report_ref") or "") or None

    def publish_brief(self, batch_id: str, markdown: str) -> Optional[str]:
        """Publish a batch brief to wiki and return the page id."""

        if not self.publish_wiki_command:
            return None
        result = self._run(
            self.publish_wiki_command,
            {"batch_id": batch_id, "markdown": markdown},
        )
        return str(result.get("page_id") or result.get("wiki_ref") or "") or None

    def fetch_triage_statuses(self, report_refs: Iterable[str]) -> List[Dict[str, object]]:
        """Fetch current triage statuses from the issue tracker."""

        refs = [ref for ref in report_refs if ref]
        if not refs or not self.status_command:
            return []
        result = self._run(self.status_command, {"report_refs": refs})
        cards = result.get("cards", [])
        if not isinstance(cards, list):
            raise ValueError("status command must return cards: []")
        return [card for card in cards if isinstance(card, dict)]

    def publish_queue_event(self, kind: str, payload: Dict[str, object]) -> Optional[str]:
        """Publish one pre-reviewed queue event without rebuilding its evidence."""

        expected_attestation = payload.get("_publication")
        if not isinstance(expected_attestation, Mapping):
            raise PublishingSafetyError("_publication request attestation is required")
        if kind == "bug_report":
            if not self.create_bug_command:
                raise PublishingSafetyError("create_bug_command is not configured")
            result = self._run(self.create_bug_command, payload)
            ref = str(result.get("card_id") or result.get("report_ref") or "") or None
        elif kind == "batch_brief":
            if not self.publish_wiki_command:
                raise PublishingSafetyError("publish_wiki_command is not configured")
            result = self._run(self.publish_wiki_command, payload)
            ref = str(result.get("page_id") or result.get("wiki_ref") or "") or None
        else:
            raise PublishingSafetyError(f"unsupported publishing event kind: {kind}")
        response_attestation = result.get("_publication")
        attestation_fields = (
            "event_id",
            "payload_sha256",
            "target_environment",
            "target_id",
        )
        if not isinstance(response_attestation, Mapping) or any(
            response_attestation.get(field) != expected_attestation.get(field)
            for field in attestation_fields
        ):
            raise PublishingCommandError(
                "publisher response publication attestation does not match request",
                classification="invalid-response",
                retryable=False,
            )
        return ref


def flush_reviewed_queue(
    *,
    queue_path: Path | str,
    review_manifest: Mapping[str, Any],
    adapter: Mapping[str, Any],
    publisher: CommandPublisher,
    production_env: Mapping[str, str],
    yes: bool,
    receipt_path: Optional[Path | str] = None,
    evidence_level: str = REAL_PUBLISHING_EVIDENCE_LEVEL,
) -> Dict[str, Any]:
    """Flush only exact owner-approved events after validating every gate."""

    _reject_secret_fields(adapter, "adapter")
    _reject_secret_fields(review_manifest, "review_manifest")
    for field, command in (
        ("publisher.create_bug_command", publisher.create_bug_command),
        ("publisher.publish_wiki_command", publisher.publish_wiki_command),
        ("publisher.status_command", publisher.status_command),
    ):
        if command:
            _command_list(command, field)
    if evidence_level not in PUBLISHING_EVIDENCE_LEVELS:
        raise PublishingSafetyError("publishing evidence_level is invalid")
    uses_tracked_fixture = _publisher_uses_tracked_fixture(publisher)
    if uses_tracked_fixture and evidence_level != FIXTURE_PUBLISHING_EVIDENCE_LEVEL:
        raise PublishingSafetyError(
            "tracked publishing fixture command requires explicit fixture-only evidence"
        )
    if not uses_tracked_fixture and evidence_level == FIXTURE_PUBLISHING_EVIDENCE_LEVEL:
        raise PublishingSafetyError(
            "fixture-only publishing requires a tracked publishing fixture command"
        )
    if adapter.get("production_write_default", False) is not False:
        raise PublishingSafetyError("production_write_default must remain false")
    publishing = adapter.get("publishing")
    if not isinstance(publishing, Mapping) or publishing.get("enabled") is not True:
        raise PublishingSafetyError("adapter.publishing.enabled must be true for explicit flush")
    target_environment = str(publishing.get("target_environment") or "").strip().lower()
    target_id = str(publishing.get("target_id") or "").strip()
    if target_environment not in {"sandbox", "test"} or not target_id:
        raise PublishingSafetyError(
            "explicit flush requires a named sandbox or test target"
        )
    if (
        str(review_manifest.get("target_environment") or "").strip().lower()
        != target_environment
        or str(review_manifest.get("target_id") or "").strip() != target_id
    ):
        raise PublishingSafetyError(
            "review manifest target must exactly match the sandbox/test adapter target"
        )
    if production_env.get("VERIPIPE_PRODUCTION_WRITE") != "1":
        raise PublishingSafetyError("VERIPIPE_PRODUCTION_WRITE=1 is required")
    if not yes:
        raise PublishingSafetyError("--yes is required")
    if review_manifest.get("schema_version") != 1:
        raise PublishingSafetyError("review manifest schema_version must be 1")
    if review_manifest.get("owner_approved") is not True:
        raise PublishingSafetyError("review manifest owner_approved must be true")
    if not str(review_manifest.get("owner") or "").strip():
        raise PublishingSafetyError("review manifest owner is required")

    events = _load_validated_queue(Path(queue_path))
    approved = _validated_approvals(review_manifest, events)
    selected = [event for event in events if event["event_id"] in approved]
    receipts_file = Path(receipt_path) if receipt_path else Path(queue_path).with_suffix(".published.jsonl")
    published = _load_published_receipts(
        receipts_file,
        target_environment=target_environment,
        target_id=target_id,
        evidence_level=evidence_level,
    )
    repeated = [str(event["event_id"]) for event in selected if event["event_id"] in published]
    for event in selected:
        event_id = str(event["event_id"])
        if event_id in published and published[event_id] != event["payload_sha256"]:
            raise PublishingSafetyError(f"published receipt payload hash mismatch: {event_id}")
    pending = [event for event in selected if event["event_id"] not in published]
    claim_paths = {
        str(event["event_id"]): _publication_claim_path(
            receipts_file,
            event=event,
            target_environment=target_environment,
            target_id=target_id,
        )
        for event in pending
    }
    unresolved_claims = [
        event_id for event_id, claim_path in claim_paths.items() if claim_path.exists()
    ]
    if unresolved_claims:
        raise PublishingSafetyError(
            "unresolved publication claim requires manual sandbox reconciliation: "
            + ", ".join(unresolved_claims)
        )
    if repeated and not pending:
        raise PublishingSafetyError("approved events already published: " + ", ".join(repeated))
    selected_kinds = {str(event["kind"]) for event in pending}
    if "bug_report" in selected_kinds and not publisher.create_bug_command:
        raise PublishingSafetyError("create_bug_command is not configured")
    if "batch_brief" in selected_kinds and not publisher.publish_wiki_command:
        raise PublishingSafetyError("publish_wiki_command is not configured")
    refs: List[Dict[str, Optional[str]]] = []
    failures: List[Dict[str, Any]] = []
    for event in pending:
        claim_path = _claim_publication(
            receipts_file,
            event=event,
            owner=str(review_manifest["owner"]),
            target_environment=target_environment,
            target_id=target_id,
            evidence_level=evidence_level,
        )
        command_payload = dict(event["payload"])
        command_payload["_publication"] = {
            "event_id": str(event["event_id"]),
            "payload_sha256": str(event["payload_sha256"]),
            "target_environment": target_environment,
            "target_id": target_id,
            "evidence_level": evidence_level,
        }
        try:
            ref = publisher.publish_queue_event(str(event["kind"]), command_payload)
            if not ref:
                raise PublishingCommandError(
                    "publisher command returned no external reference",
                    classification="invalid-response",
                    retryable=False,
                )
        except PublishingCommandError as exc:
            failures.append(
                {
                    "event_id": str(event["event_id"]),
                    "classification": exc.classification,
                    "retryable": exc.retryable,
                    "event_retry_allowed": False,
                    "error": str(exc),
                    "claim_path": str(claim_path),
                }
            )
            break
        refs.append({"event_id": str(event["event_id"]), "ref": ref})
        _append_published_receipt(
            receipts_file,
            event=event,
            owner=str(review_manifest["owner"]),
            ref=ref,
            target_environment=target_environment,
            target_id=target_id,
            evidence_level=evidence_level,
        )
    return {
        "status": "partial-failure" if failures else "flushed",
        "owner": str(review_manifest["owner"]),
        "target_environment": target_environment,
        "target_id": target_id,
        "evidence_level": evidence_level,
        "flushed_count": len(refs),
        "failed_count": len(failures),
        "failures": failures,
        "already_published_count": len(repeated),
        "skipped_count": len(events) - len(selected),
        "refs": refs,
        "receipt_path": str(receipts_file),
    }


def _load_validated_queue(path: Path) -> List[Dict[str, object]]:
    if not path.is_file():
        raise PublishingSafetyError(f"queue file not found: {path}")
    events: List[Dict[str, object]] = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = _loads_strict_json(line)
        except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
            raise PublishingSafetyError(
                f"queue line {line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise PublishingSafetyError(f"queue line {line_number} must be an object")
        _reject_secret_fields(event, f"queue line {line_number}")
        if event.get("schema_version") != 1:
            raise PublishingSafetyError(f"queue line {line_number} schema_version must be 1")
        kind = str(event.get("kind") or "")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise PublishingSafetyError(f"queue line {line_number} payload must be an object")
        expected = build_queue_event(kind, payload)
        if event.get("payload_sha256") != expected["payload_sha256"]:
            raise PublishingSafetyError(f"queue line {line_number} payload hash mismatch")
        if event.get("event_id") != expected["event_id"]:
            raise PublishingSafetyError(f"queue line {line_number} event id mismatch")
        event_id = str(event["event_id"])
        if event_id in seen:
            raise PublishingSafetyError(f"duplicate queue event id: {event_id}")
        seen.add(event_id)
        events.append(dict(event))
    return events


def _validated_approvals(
    review_manifest: Mapping[str, Any],
    events: List[Dict[str, object]],
) -> Dict[str, str]:
    items = review_manifest.get("approved_events")
    if not isinstance(items, list) or not items:
        raise PublishingSafetyError("review manifest approved_events must be non-empty")
    by_event_id = {str(event["event_id"]): event for event in events}
    approved: Dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise PublishingSafetyError(f"approved_events[{index}] must be an object")
        event_id = str(item.get("event_id") or "").strip()
        payload_sha256 = str(item.get("payload_sha256") or "").strip()
        if event_id in approved:
            raise PublishingSafetyError(f"duplicate approved event id: {event_id}")
        event = by_event_id.get(event_id)
        if event is None:
            raise PublishingSafetyError(f"approved event not found in queue: {event_id}")
        if payload_sha256 != event.get("payload_sha256"):
            raise PublishingSafetyError(f"approved event payload hash mismatch: {event_id}")
        approved[event_id] = payload_sha256
    return approved


def _load_published_receipts(
    path: Path,
    *,
    target_environment: str,
    target_id: str,
    evidence_level: str,
) -> Dict[str, str]:
    if not path.exists():
        return {}
    receipts: Dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = _loads_strict_json(line)
        except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
            raise PublishingSafetyError(
                f"receipt line {line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(item, Mapping):
            raise PublishingSafetyError(f"receipt line {line_number} must be an object")
        if item.get("schema_version") != RECEIPT_SCHEMA_VERSION:
            raise PublishingSafetyError(
                f"receipt schema_version {RECEIPT_SCHEMA_VERSION} is required at line {line_number}"
            )
        event_id = str(item.get("event_id") or "").strip()
        payload_sha256 = str(item.get("payload_sha256") or "").strip()
        if not event_id or not payload_sha256:
            raise PublishingSafetyError(f"receipt line {line_number} is incomplete")
        if (
            str(item.get("target_environment") or "").strip().lower()
            != target_environment
            or str(item.get("target_id") or "").strip() != target_id
        ):
            raise PublishingSafetyError(
                f"receipt target does not match current sandbox/test target at line {line_number}"
            )
        if item.get("evidence_level") != evidence_level:
            raise PublishingSafetyError(
                f"receipt evidence_level does not match current publishing run at line {line_number}"
            )
        if event_id in receipts:
            raise PublishingSafetyError(f"duplicate receipt event id: {event_id}")
        receipts[event_id] = payload_sha256
    return receipts


def _append_published_receipt(
    path: Path,
    *,
    event: Mapping[str, object],
    owner: str,
    ref: Optional[str],
    target_environment: str,
    target_id: str,
    evidence_level: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "evidence_level": evidence_level,
        "event_id": str(event["event_id"]),
        "payload_sha256": str(event["payload_sha256"]),
        "owner": owner,
        "target_environment": target_environment,
        "target_id": target_id,
        "ref": ref,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(receipt) + "\n")


def _publication_claim_path(
    receipt_path: Path,
    *,
    event: Mapping[str, object],
    target_environment: str,
    target_id: str,
) -> Path:
    identity = {
        "event_id": str(event["event_id"]),
        "payload_sha256": str(event["payload_sha256"]),
        "target_environment": target_environment,
        "target_id": target_id,
    }
    identity_sha256 = hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()
    claim_directory = Path(str(receipt_path) + ".claims")
    return claim_directory / f"{identity_sha256}.json"


def _claim_publication(
    receipt_path: Path,
    *,
    event: Mapping[str, object],
    owner: str,
    target_environment: str,
    target_id: str,
    evidence_level: str,
) -> Path:
    claim_path = _publication_claim_path(
        receipt_path,
        event=event,
        target_environment=target_environment,
        target_id=target_id,
    )
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "status": "claimed",
        "evidence_level": evidence_level,
        "event_id": str(event["event_id"]),
        "payload_sha256": str(event["payload_sha256"]),
        "owner": owner,
        "target_environment": target_environment,
        "target_id": target_id,
        "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        descriptor = os.open(
            claim_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise PublishingSafetyError(
            "unresolved publication claim requires manual sandbox reconciliation: "
            + str(event["event_id"])
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_canonical_json(claim) + "\n")
    return claim_path


def _load_json_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishingSafetyError(f"cannot load {label}: {path}") from exc
    try:
        data = _loads_strict_json(text)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise PublishingSafetyError(f"cannot load {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise PublishingSafetyError(f"{label} must be a JSON object")
    return data


def _command_list(raw: Any, field: str) -> List[str]:
    if not isinstance(raw, list) or not all(isinstance(part, str) and part.strip() for part in raw):
        raise PublishingSafetyError(f"{field} must be a non-empty string list")
    if any(
        COMMAND_CREDENTIAL_RE.search(part)
        or part.strip().lower() in COMMAND_CREDENTIAL_FLAGS
        for part in raw
    ):
        raise PublishingSafetyError(
            f"secret or credential command arguments are forbidden in {field}"
        )
    shell_executable = explicit_shell_executable(raw)
    if shell_executable:
        raise PublishingSafetyError(
            f"explicit shell command is forbidden in {field}: {shell_executable}"
        )
    return list(raw)


def build_command_publisher_from_adapter(adapter: Mapping[str, Any]) -> CommandPublisher:
    _reject_secret_fields(adapter, "adapter")
    publishing = adapter.get("publishing")
    if not isinstance(publishing, Mapping) or publishing.get("enabled") is not True:
        raise PublishingSafetyError("adapter.publishing.enabled must be true for explicit flush")
    create_bug_command = publishing.get("create_bug_command", [])
    publish_wiki_command = publishing.get("publish_wiki_command", [])
    status_command = publishing.get("status_command", [])
    if create_bug_command:
        create_bug_command = _command_list(create_bug_command, "publishing.create_bug_command")
    if publish_wiki_command:
        publish_wiki_command = _command_list(publish_wiki_command, "publishing.publish_wiki_command")
    if status_command:
        status_command = _command_list(status_command, "publishing.status_command")
    return CommandPublisher(
        create_bug_command=create_bug_command,
        publish_wiki_command=publish_wiki_command,
        status_command=status_command,
    )


def _publisher_uses_tracked_fixture(publisher: CommandPublisher) -> bool:
    fixture_root = (
        Path(__file__).resolve().parents[4]
        / "tests"
        / "fixtures"
        / "publishing"
    ).resolve()
    for command in (
        publisher.create_bug_command,
        publisher.publish_wiki_command,
        publisher.status_command,
    ):
        for part in command:
            candidate = Path(part).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(fixture_root)
            except (FileNotFoundError, ValueError):
                continue
            return True
    return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded pipeline-v2 publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    flush_parser = subparsers.add_parser("flush", help="Flush exact owner-approved queue events")
    flush_parser.add_argument("--queue", type=Path, required=True)
    flush_parser.add_argument("--review-manifest", type=Path, required=True)
    flush_parser.add_argument("--adapter", type=Path, required=True)
    flush_parser.add_argument("--receipt", type=Path)
    flush_parser.add_argument("--fixture-only", action="store_true")
    flush_parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    try:
        adapter = _load_json_object(args.adapter, "adapter")
        manifest = _load_json_object(args.review_manifest, "review manifest")
        publisher = build_command_publisher_from_adapter(adapter)
        result = flush_reviewed_queue(
            queue_path=args.queue,
            review_manifest=manifest,
            adapter=adapter,
            publisher=publisher,
            production_env=os.environ,
            yes=args.yes,
            receipt_path=args.receipt,
            evidence_level=(
                FIXTURE_PUBLISHING_EVIDENCE_LEVEL
                if args.fixture_only
                else REAL_PUBLISHING_EVIDENCE_LEVEL
            ),
        )
    except PublishingSafetyError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.get("status") == "partial-failure" else 0


class TriageSync:
    """Pull issue-tracker card statuses before a batch and write cases.triage_result."""

    def __init__(self, ledger: Ledger, publisher: CommandPublisher):
        self.ledger = ledger
        self.publisher = publisher

    def sync(self) -> int:
        cases = self.ledger.cases_with_report_refs()
        by_ref = {case["report_ref"]: case for case in cases}
        cards = self.publisher.fetch_triage_statuses(by_ref.keys())
        updated = 0
        for card in cards:
            report_ref = str(card.get("report_ref") or card.get("card_id") or "")
            triage_result = str(card.get("triage_result") or "")
            if report_ref not in by_ref or triage_result not in TRIAGE_RESULTS:
                continue
            self.ledger.update_triage_result(by_ref[report_ref]["fingerprint"], triage_result)
            updated += 1
        return updated


if __name__ == "__main__":
    raise SystemExit(main())
