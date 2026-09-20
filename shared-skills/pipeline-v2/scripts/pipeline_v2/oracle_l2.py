"""Executable L2 oracle assertions for semantic-map terminal states.

The real product executor is intentionally out of scope here. Callers provide
an ``observed_state`` dict collected after a run reaches terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .schema import FailureSignal


ASSERTION_TYPE_REGISTRY = {
    "context_state",
    "file_created",
    "file_state",
    "interaction_state",
    "mode_state",
    "permission_state",
    "record_count",
    "release_state",
    "resource_state",
    "skill_state",
    "tool_payload_state",
    "validation_result",
    "workflow_state",
}

MACHINE_CHECK_OPS = {
    "all_true",
    "any_true",
    "contains_key",
    "count_ge",
    "eq",
    "present",
}

TEXT_STATE_KEYS = {
    "actual_text",
    "error_text",
    "message",
    "output",
    "output_text",
    "response",
    "stderr",
    "stdout",
    "text",
}


@dataclass(frozen=True)
class OracleL2Result:
    """Tri-state L2 oracle result.

    ``failure_signal`` is populated only when the structured terminal state is
    measured and violates the semantic-map contract.
    """

    status: str
    passed: Optional[bool]
    reason: str
    semantic_map_entry_id: str
    assertion_type: str
    expected_state: Dict[str, Any] = field(default_factory=dict)
    actual_state: Dict[str, Any] = field(default_factory=dict)
    diff: Dict[str, Any] = field(default_factory=dict)
    failure_signal: Optional[FailureSignal] = None

    def as_evidence(self) -> Dict[str, Any]:
        """Return JSON-ready evidence without nesting the FailureSignal."""

        return {
            "status": self.status,
            "passed": self.passed,
            "reason": self.reason,
            "semantic_map_entry_id": self.semantic_map_entry_id,
            "assertion_type": self.assertion_type,
            "expected_state": self.expected_state,
            "actual_state": self.actual_state,
            "diff": self.diff,
        }


def load_entry_by_id(semantic_map: Mapping[str, Any], entry_id: str) -> Mapping[str, Any]:
    """Load one semantic-map entry by id."""

    for entry in semantic_map.get("entries", []):
        if isinstance(entry, Mapping) and entry.get("id") == entry_id:
            return entry
    raise KeyError(f"semantic-map entry not found: {entry_id}")


def validate_machine_check(machine_check: Any) -> List[str]:
    """Validate the machine-comparable terminal-state assertion shape."""

    if not isinstance(machine_check, Mapping):
        return ["machine_check"]
    errors: List[str] = []
    op = machine_check.get("op")
    if op not in MACHINE_CHECK_OPS:
        errors.append("machine_check.op")
        return errors
    if op in {"eq", "present", "contains_key", "count_ge"}:
        field = str(machine_check.get("field", "")).strip()
        if not field:
            errors.append("machine_check.field")
        elif _is_text_field(field):
            errors.append("machine_check.field.text")
    if op == "eq" and "value" not in machine_check and "value_from" not in machine_check:
        errors.append("machine_check.value")
    if op == "contains_key" and "value" not in machine_check:
        errors.append("machine_check.value")
    if op == "count_ge":
        expected = machine_check.get("value")
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            errors.append("machine_check.value")
    if op in {"all_true", "any_true"}:
        fields = machine_check.get("fields")
        if not isinstance(fields, list) or not fields:
            errors.append("machine_check.fields")
        else:
            for index, field in enumerate(fields):
                field_name = str(field).strip()
                if not field_name:
                    errors.append(f"machine_check.fields[{index}]")
                elif _is_text_field(field_name):
                    errors.append(f"machine_check.fields[{index}].text")
    value_from = machine_check.get("value_from")
    if isinstance(value_from, str) and _is_text_field(value_from):
        errors.append("machine_check.value_from.text")
    return errors


def evaluate_l2_oracle(
    entry: Mapping[str, Any],
    observed_state: Mapping[str, Any],
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str,
    nondeterministic_sources: Optional[List[str]] = None,
    static_certified: bool = False,
) -> OracleL2Result:
    """Evaluate one semantic-map entry against one observed terminal state."""

    entry_id = str(entry.get("id", "")).strip()
    expected_terminal = entry.get("expected_terminal_state")
    if not isinstance(expected_terminal, Mapping):
        return _inconclusive(entry_id, "", {}, {}, "expected_terminal_state")

    assertion_type = str(expected_terminal.get("assertion_type", "")).strip()
    if assertion_type not in ASSERTION_TYPE_REGISTRY:
        return _inconclusive(entry_id, assertion_type, {}, {}, "expected_terminal_state.assertion_type")
    machine_check = expected_terminal.get("machine_check")
    check_errors = validate_machine_check(machine_check)
    if check_errors:
        reason = "text-field-not-allowed" if any(error.endswith(".text") for error in check_errors) else ",".join(
            f"expected_terminal_state.{error}" for error in check_errors
        )
        return _inconclusive(
            entry_id,
            assertion_type,
            _expected_state(expected_terminal, machine_check, None),
            {},
            reason,
        )
    if not isinstance(observed_state, Mapping):
        return _inconclusive(
            entry_id,
            assertion_type,
            _expected_state(expected_terminal, machine_check, None),
            {},
            "observed_state",
        )

    outcome, expected, actual, reason = _run_machine_check(machine_check, observed_state)
    expected_state = _expected_state(expected_terminal, machine_check, expected)
    actual_state = _actual_state(machine_check, actual)
    if outcome == "inconclusive":
        return _inconclusive(entry_id, assertion_type, expected_state, actual_state, reason)
    diff = {} if outcome == "pass" else _diff(machine_check, expected, actual)
    result = OracleL2Result(
        status=outcome,
        passed=outcome == "pass",
        reason="assertion-passed" if outcome == "pass" else "assertion-failed",
        semantic_map_entry_id=entry_id,
        assertion_type=assertion_type,
        expected_state=expected_state,
        actual_state=actual_state,
        diff=diff,
    )
    if outcome == "fail":
        failure_signal = FailureSignal(
            scenario_id=scenario_id,
            step_id=step_id,
            failure_type=assertion_type,
            oracle_level="L2",
            llm_involvement=llm_involvement,
            expected_state=expected_state,
            actual_state=actual_state,
            nondeterministic_sources=nondeterministic_sources or [],
            static_certified=static_certified,
            semantic_map_entry_id=entry_id,
            evidence={"oracle_l2": result.as_evidence()},
        )
        result = OracleL2Result(
            status=result.status,
            passed=result.passed,
            reason=result.reason,
            semantic_map_entry_id=result.semantic_map_entry_id,
            assertion_type=result.assertion_type,
            expected_state=result.expected_state,
            actual_state=result.actual_state,
            diff=result.diff,
            failure_signal=failure_signal,
        )
    return result


def _run_machine_check(machine_check: Mapping[str, Any], observed_state: Mapping[str, Any]) -> Tuple[str, Any, Any, str]:
    op = machine_check["op"]
    if op == "eq":
        found, actual = _get_path(observed_state, str(machine_check["field"]))
        if not found:
            return "inconclusive", _expected_value(machine_check, observed_state), None, "observed_state.field"
        found_expected, expected = _resolve_expected(machine_check, observed_state)
        if not found_expected:
            return "inconclusive", None, actual, "observed_state.value_from"
        return ("pass" if actual == expected else "fail"), expected, actual, ""
    if op == "present":
        found, actual = _get_path(observed_state, str(machine_check["field"]))
        if not found:
            return "inconclusive", "present", None, "observed_state.field"
        return ("pass" if _is_present(actual) else "fail"), "present", actual, ""
    if op == "contains_key":
        found, actual = _get_path(observed_state, str(machine_check["field"]))
        if not found:
            return "inconclusive", machine_check["value"], None, "observed_state.field"
        if not isinstance(actual, Mapping):
            return "inconclusive", machine_check["value"], actual, "observed_state.not_mapping"
        key = str(machine_check["value"])
        return ("pass" if key in actual else "fail"), {"contains_key": key}, {"keys": sorted(str(item) for item in actual.keys())}, ""
    if op == "count_ge":
        found, actual = _get_path(observed_state, str(machine_check["field"]))
        if not found:
            return "inconclusive", machine_check["value"], None, "observed_state.field"
        actual_count = _count(actual)
        if actual_count is None:
            return "inconclusive", machine_check["value"], actual, "observed_state.count"
        expected = machine_check["value"]
        return ("pass" if actual_count >= expected else "fail"), {"count_ge": expected}, actual_count, ""
    if op in {"all_true", "any_true"}:
        values: Dict[str, Any] = {}
        for field in machine_check["fields"]:
            found, value = _get_path(observed_state, str(field))
            if not found:
                return "inconclusive", op, values, "observed_state.field"
            values[str(field)] = value
        expected = {str(field): True for field in machine_check["fields"]}
        if op == "all_true":
            return ("pass" if all(value is True for value in values.values()) else "fail"), expected, values, ""
        return ("pass" if any(value is True for value in values.values()) else "fail"), expected, values, ""
    return "inconclusive", None, None, "machine_check.op"


def _expected_state(expected_terminal: Mapping[str, Any], machine_check: Any, expected: Any) -> Dict[str, Any]:
    return {
        "assertion": expected_terminal.get("assertion", ""),
        "observable": expected_terminal.get("observable", ""),
        "assertion_type": expected_terminal.get("assertion_type", ""),
        "machine_check": dict(machine_check) if isinstance(machine_check, Mapping) else machine_check,
        "expected": expected,
    }


def _actual_state(machine_check: Mapping[str, Any], actual: Any) -> Dict[str, Any]:
    return {
        "machine_check": dict(machine_check),
        "actual": actual,
    }


def _diff(machine_check: Mapping[str, Any], expected: Any, actual: Any) -> Dict[str, Any]:
    return {
        "machine_check": dict(machine_check),
        "expected": expected,
        "actual": actual,
    }


def _inconclusive(
    entry_id: str,
    assertion_type: str,
    expected_state: Dict[str, Any],
    actual_state: Dict[str, Any],
    reason: str,
) -> OracleL2Result:
    return OracleL2Result(
        status="inconclusive",
        passed=None,
        reason=reason,
        semantic_map_entry_id=entry_id,
        assertion_type=assertion_type,
        expected_state=expected_state,
        actual_state=actual_state,
        diff={},
        failure_signal=None,
    )


def _get_path(data: Mapping[str, Any], field_path: str) -> Tuple[bool, Any]:
    current: Any = data
    for part in field_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _resolve_expected(machine_check: Mapping[str, Any], observed_state: Mapping[str, Any]) -> Tuple[bool, Any]:
    if "value" in machine_check:
        return True, machine_check["value"]
    value_from = str(machine_check.get("value_from", "")).strip()
    if not value_from:
        return False, None
    return _get_path(observed_state, value_from)


def _expected_value(machine_check: Mapping[str, Any], observed_state: Mapping[str, Any]) -> Any:
    _found, expected = _resolve_expected(machine_check, observed_state)
    return expected


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _count(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple, dict, set)):
        return float(len(value))
    return None


def _is_text_field(field_path: str) -> bool:
    return any(part.lower() in TEXT_STATE_KEYS for part in field_path.split("."))
