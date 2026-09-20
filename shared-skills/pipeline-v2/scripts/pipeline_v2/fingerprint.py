"""Structural fingerprinting for agent-tested products."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .schema import FailureSignal


TEXT_KEYS = {
    "actual_text",
    "content",
    "error",
    "error_text",
    "message",
    "output",
    "output_text",
    "response",
    "stderr",
    "stdout",
    "text",
}

VOLATILE_KEYS = {
    "duration",
    "duration_ms",
    "elapsed",
    "elapsed_ms",
    "started_at",
    "timestamp",
    "updated_at",
}

ANCHOR_KEYS = {
    "anchor",
    "location",
    "stack_anchor",
}


def _normalize_anchor(value: str) -> str:
    value = re.sub(r":\d+:\d+", ":<line>:<column>", value)
    value = re.sub(r":\d+\b", ":<line>", value)
    return value


def _shape(value: Any, key: str = "") -> Any:
    """Return a JSON-serializable shape without volatile wording."""

    lowered = key.lower()
    if lowered in VOLATILE_KEYS:
        return {"type": type(value).__name__}
    if lowered in ANCHOR_KEYS and isinstance(value, str):
        return _normalize_anchor(value)
    if lowered in TEXT_KEYS:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        return {str(k): _shape(v, str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [_shape(item) for item in value[:3]],
        }
    if isinstance(value, tuple):
        return {
            "type": "tuple",
            "length": len(value),
            "items": [_shape(item) for item in value[:3]],
        }
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        if re.fullmatch(r"[A-Za-z0-9_.:/@ -]{1,120}", value):
            return value
        return {"type": "str"}
    return {"type": type(value).__name__}


def fingerprint_source(signal: FailureSignal) -> str:
    """Build the canonical fingerprint source for a failure signal."""

    payload = {
        "scenario_id": signal.scenario_id,
        "step_id": signal.step_id,
        "failure_type": signal.failure_type,
        "oracle_level": signal.oracle_level,
        "expected_shape": _shape(signal.expected_state),
        "actual_shape": _shape(signal.actual_state),
        "stack_anchor": _normalize_anchor(signal.stack_anchor or ""),
        "exit_code": signal.exit_code,
        "semantic_map_entry_id": signal.semantic_map_entry_id or "",
        "nondeterministic_sources": sorted(signal.nondeterministic_sources),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def failure_fingerprint(signal: FailureSignal) -> str:
    """Return a stable sha1 fingerprint from structural failure features."""

    return hashlib.sha1(fingerprint_source(signal).encode("utf-8")).hexdigest()
