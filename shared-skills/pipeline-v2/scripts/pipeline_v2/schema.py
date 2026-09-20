"""Shared schema types and validation constants for pipeline v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


ORACLE_LEVELS = {"L1", "L2", "L3"}
CASE_STATUSES = {
    "candidate",
    "observing",
    "reported",
    "intermittent",
    "expired",
    "environment-event",
    "vetoed",
}
TRIAGE_RESULTS = {
    "confirmed",
    "false-positive",
    "contract-mismatch-doc",
    "contract-mismatch-code",
}
SENTINEL_STATUSES = {"passed", "triggered", "skipped"}


class SchemaError(ValueError):
    """Raised when an in-memory event violates the pipeline schema."""


@dataclass(frozen=True)
class FailureSignal:
    """A structured failure signal.

    The free-form output fields are preserved for evidence rendering, but the
    fingerprint module deliberately ignores them. Agent products may produce
    different wording on every run; stable identity must come from structure.
    """

    scenario_id: str
    step_id: str
    failure_type: str
    oracle_level: str
    llm_involvement: str
    expected_state: Dict[str, Any] = field(default_factory=dict)
    actual_state: Dict[str, Any] = field(default_factory=dict)
    output_text: str = ""
    error_text: str = ""
    stack_anchor: Optional[str] = None
    exit_code: Optional[int] = None
    nondeterministic_sources: List[str] = field(default_factory=list)
    static_certified: bool = False
    semantic_map_entry_id: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def validate_case_schema(self) -> None:
        """Validate fields that are persisted into the case ledger."""

        if self.oracle_level not in ORACLE_LEVELS:
            raise SchemaError(f"oracle_level must be one of L1-L3: {self.oracle_level}")
        if not self.llm_involvement or not self.llm_involvement.strip():
            raise SchemaError("llm_involvement is required")
        if self.oracle_level in ORACLE_LEVELS and not (self.semantic_map_entry_id or "").strip():
            raise SchemaError(f"semantic_map_entry_id is required for {self.oracle_level} evidence")

    def failure_summary(self) -> str:
        """Return a compact structural summary safe for hits and reports."""

        parts = [
            f"scenario={self.scenario_id}",
            f"step={self.step_id}",
            f"type={self.failure_type}",
            f"oracle={self.oracle_level}",
        ]
        if self.stack_anchor:
            parts.append(f"anchor={self.stack_anchor}")
        if self.semantic_map_entry_id:
            parts.append(f"semantic={self.semantic_map_entry_id}")
        return " | ".join(parts)
