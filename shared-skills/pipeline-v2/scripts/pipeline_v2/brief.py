"""Batch brief rendering and redline detection."""

from __future__ import annotations

import json
from statistics import mean
from typing import Dict, List, Optional

from .ledger import Ledger


class BriefGenerator:
    """Render one-page markdown briefs from the ledger."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def detect_redlines(self, batch_id: str, observation_size: Optional[int] = None) -> List[str]:
        """Detect the four first-batch redlines and write them to batches."""

        batch = self.ledger.get_batch(batch_id)
        if not batch:
            raise ValueError(f"batch not found: {batch_id}")
        flags: List[str] = []

        recent = self.ledger.recent_batches(3)
        if len(recent) >= 3 and all(int(item["reported_count"]) == 0 for item in recent[:3]):
            flags.append("zero_report_output_3_batches")

        if batch["sentinel_status"] == "triggered":
            flags.append("sentinel_environment_event")
            previous = self.ledger.recent_batches(2)
            if len(previous) >= 2 and all(item["sentinel_status"] == "triggered" for item in previous[:2]):
                flags.append("sentinel_environment_event_2_batches")

        size = self.ledger.count_observing_cases() if observation_size is None else observation_size
        if size > 50:
            flags.append("observation_size_over_soft_limit")

        if batch["injected_recall_rate"] is not None:
            older = [item for item in self.ledger.all_batches() if item["batch_id"] != batch_id]
            previous_rates = [item["injected_recall_rate"] for item in older if item["injected_recall_rate"] is not None]
            if previous_rates and float(batch["injected_recall_rate"]) < float(previous_rates[-1]):
                flags.append("injected_recall_drop")

        self.ledger.update_redline_flags(batch_id, flags)
        return flags

    def render(self, batch_id: str) -> str:
        """Render the one-page five-section markdown brief."""

        batch = self.ledger.get_batch(batch_id)
        if not batch:
            raise ValueError(f"batch not found: {batch_id}")
        flags = self.detect_redlines(batch_id)
        lines = [
            f"# Batch Brief {batch_id}",
            "",
            "## Status",
            f"batch_id: {batch_id}",
            f"product_version: {batch.get('product_version') or ''}",
            f"sentinel_status: {batch['sentinel_status']}",
        ]
        if batch["sentinel_status"] == "triggered":
            lines.extend(
                [
                    "",
                    "## Redlines",
                    self._format_redlines(flags),
                    "",
                    "environment event: this batch is void and no funnel data is interpreted.",
                ]
            )
            return "\n".join(lines) + "\n"

        lines.extend(
            [
                "",
                "## Redlines",
                self._format_redlines(flags),
                "",
                "## Funnel",
                "| stage | count | recent_5_avg |",
                "|---|---:|---:|",
            ]
        )
        averages = self._recent_averages()
        for field, label in [
            ("signal_count", "signals"),
            ("oracle_l1_count", "oracle L1"),
            ("oracle_l2_count", "oracle L2"),
            ("oracle_l3_count", "oracle L3"),
            ("verification_passed_count", "verification passed"),
            ("verification_blocked_count", "verification blocked"),
            ("freshness_mismatch_count", "contract mismatch"),
            ("reported_count", "reported"),
        ]:
            lines.append(f"| {label} | {int(batch[field])} | {averages.get(field, 0):.1f} |")

        lines.extend(
            [
                "",
                "## Incremental Discoveries",
            ]
        )
        reported_cases = self.ledger.list_reported_cases_for_batch(batch_id)
        if not reported_cases:
            lines.append("- reported: 0")
        for case in reported_cases:
            ref = case.get("report_ref") or "pending"
            lines.append(f"- {case['oracle_level']} {case['fingerprint']} report_ref={ref}")
        lines.append(f"- observation entered: {int(batch['observation_entered_count'])} (internal-only details omitted)")
        lines.append(f"- observation upgraded: {int(batch['observation_upgraded_count'])}")
        lines.append(f"- observation expired: {int(batch['observation_expired_count'])}")

        lines.extend(
            [
                "",
                "## Cost",
                f"token_cost: {batch['token_cost']}",
                f"duration: {batch['duration']}",
            ]
        )
        return "\n".join(lines) + "\n"

    def _format_redlines(self, flags: List[str]) -> str:
        if not flags:
            return "- none"
        return "\n".join(f"- REDLINE: {flag}" for flag in flags)

    def _recent_averages(self) -> Dict[str, float]:
        recent = self.ledger.recent_batches(5)
        if not recent:
            return {}
        fields = [
            "signal_count",
            "oracle_l1_count",
            "oracle_l2_count",
            "oracle_l3_count",
            "verification_passed_count",
            "verification_blocked_count",
            "freshness_mismatch_count",
            "reported_count",
        ]
        return {field: mean(int(item[field]) for item in recent) for field in fields}
