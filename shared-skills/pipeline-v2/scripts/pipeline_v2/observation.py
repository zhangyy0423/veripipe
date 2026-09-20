"""Observation-zone state machine."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from .ledger import Ledger


def _batch_number(batch_id: str) -> Optional[int]:
    match = re.search(r"(\d+)$", batch_id)
    return int(match.group(1)) if match else None


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


class ObservationStateMachine:
    """Fingerprint-level observation zone with three exits."""

    def __init__(self, ledger: Ledger, upgrade_batches: int = 3, expire_batches: int = 10, expire_days: int = 14):
        self.ledger = ledger
        self.upgrade_batches = upgrade_batches
        self.expire_batches = expire_batches
        self.expire_days = expire_days

    def record_observation(
        self,
        *,
        fingerprint: str,
        batch_id: str,
        oracle_level: str,
        llm_involvement: str,
        failure_summary: str,
        semantic_map_entry_id: Optional[str] = None,
        verify_entry: Optional[dict] = None,
    ) -> str:
        """Record an observed flaky or incomplete signal and evaluate upgrade."""

        self.ledger.upsert_case(
            fingerprint=fingerprint,
            status="observing",
            oracle_level=oracle_level,
            llm_involvement=llm_involvement,
            batch_id=batch_id,
            verify_entry=verify_entry,
            semantic_map_entry_id=semantic_map_entry_id,
        )
        self.ledger.record_hit(
            fingerprint=fingerprint,
            batch_id=batch_id,
            failure_summary=failure_summary,
        )
        distinct_batches = self.ledger.distinct_hit_batches(fingerprint)
        if len(set(distinct_batches)) >= self.upgrade_batches:
            self.ledger.set_case_status(fingerprint, "intermittent")
            return "intermittent"
        return "observing"

    def expire_stale(self, *, current_batch_id: str, now: Optional[datetime] = None) -> List[str]:
        """Expire observing fingerprints beyond the configured batch/day window."""

        now = now or datetime.now(timezone.utc)
        current_number = _batch_number(current_batch_id)
        expired: List[str] = []
        for case in self.ledger.list_cases(status="observing"):
            should_expire = False
            last_number = _batch_number(str(case["last_seen_batch"]))
            if current_number is not None and last_number is not None:
                should_expire = current_number - last_number > self.expire_batches
            updated_at = _parse_iso(str(case["updated_at"]))
            if updated_at is not None:
                should_expire = should_expire or (now - updated_at).days > self.expire_days
            if should_expire:
                self.ledger.set_case_status(case["fingerprint"], "expired")
                expired.append(case["fingerprint"])
        return expired

    def detect_batch_environment_event(self, batch_id: str, min_unrelated_fingerprints: int = 3) -> bool:
        """Mark a same-window batch of unrelated observing signals as environment."""

        hits = self.ledger.list_hits_for_batch(batch_id)
        fingerprints = []
        for fingerprint in sorted({hit["fingerprint"] for hit in hits}):
            case = self.ledger.get_case(fingerprint)
            if case and case.get("status") == "observing":
                fingerprints.append(fingerprint)
        if len(fingerprints) < min_unrelated_fingerprints:
            return False
        for fingerprint in fingerprints:
            self.ledger.set_case_status(fingerprint, "environment-event")
        return True

    def size(self) -> int:
        """Return current observation-zone size."""

        return self.ledger.count_observing_cases()
