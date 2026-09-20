"""Opt-in model routing with asymmetric, machine-gated decisions."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from .command_safety import explicit_shell_executable
from .funnel import VetoResult
from .ledger import Ledger
from .schema import FailureSignal


MODEL_ROLES = ("cheap", "standard", "strong")
MODEL_DECISIONS = {"allow", "veto", "request-more-evidence"}
MODEL_RESPONSE_FIELDS = {
    "decision",
    "reason",
    "usage",
    "validation_result",
    "prompt_version",
}
PROHIBITED_MODEL_AUTHORITY_FIELDS = {
    "bug_confirmed",
    "card_id",
    "confirm",
    "confirmed",
    "create_bug",
    "create_bug_card",
    "owner_accepted",
    "promote",
    "promoted",
    "publish",
    "published",
    "report_bug",
    "report_ref",
    "triage_result",
}
DEFAULT_MODEL_TIMEOUT_SEC = 300
MACHINE_TRIGGERS = {
    "coverage-gap",
    "machine-verified-failure",
    "machine-evidence-conflict",
    "security-risk",
    "architecture-risk",
}


class ModelRoutingError(ValueError):
    """Raised when model routing would violate the machine-evidence contract."""


@dataclass(frozen=True)
class ModelRoute:
    role: str
    model_id: str
    prompt_version: str


@dataclass(frozen=True)
class ModelBudget:
    max_calls_per_batch: int
    max_input_tokens_per_batch: int
    max_output_tokens_per_batch: int
    max_estimated_cost_per_batch: float
    per_call_timeout_sec: float
    consecutive_error_stop: int
    telemetry_required: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelBudget":
        if not isinstance(raw, Mapping):
            raise ModelRoutingError("model routing budget overlay must be an object")
        budget = cls(
            max_calls_per_batch=_positive_int(
                raw.get("max_calls_per_batch"), "max_calls_per_batch"
            ),
            max_input_tokens_per_batch=_nonnegative_int(
                raw.get("max_input_tokens_per_batch"), "max_input_tokens_per_batch"
            ),
            max_output_tokens_per_batch=_nonnegative_int(
                raw.get("max_output_tokens_per_batch"), "max_output_tokens_per_batch"
            ),
            max_estimated_cost_per_batch=_nonnegative_float(
                raw.get("max_estimated_cost_per_batch"),
                "max_estimated_cost_per_batch",
            ),
            per_call_timeout_sec=_positive_float(
                raw.get("per_call_timeout_sec"), "per_call_timeout_sec"
            ),
            consecutive_error_stop=_positive_int(
                raw.get("consecutive_error_stop"), "consecutive_error_stop"
            ),
            telemetry_required=raw.get("telemetry_required"),
        )
        if budget.telemetry_required is not True:
            raise ModelRoutingError("telemetry_required must be true for model routing")
        return budget


@dataclass(frozen=True)
class ModelRoutingPolicy:
    enabled: bool
    allowed_triggers: frozenset[str]
    routes: Mapping[str, ModelRoute]
    budget: ModelBudget

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelRoutingPolicy":
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ModelRoutingError("model_routing.enabled must be boolean")

        triggers_raw = raw.get("allowed_triggers", [])
        if not isinstance(triggers_raw, list) or not all(isinstance(item, str) for item in triggers_raw):
            raise ModelRoutingError("model_routing.allowed_triggers must be a string list")
        allowed_triggers = frozenset(item.strip() for item in triggers_raw if item.strip())
        unknown_triggers = allowed_triggers - MACHINE_TRIGGERS
        if unknown_triggers:
            raise ModelRoutingError(
                "model routing escalation requires a machine trigger; unknown triggers: "
                + ", ".join(sorted(unknown_triggers))
            )

        roles_raw = raw.get("roles", {})
        if not isinstance(roles_raw, Mapping):
            raise ModelRoutingError("model_routing.roles must be an object")
        routes: Dict[str, ModelRoute] = {}
        for role in MODEL_ROLES:
            item = roles_raw.get(role)
            if not isinstance(item, Mapping):
                raise ModelRoutingError(f"model_routing.roles.{role} is required")
            model_id = str(item.get("model_id") or "").strip()
            prompt_version = str(item.get("prompt_version") or "").strip()
            if not model_id or not prompt_version:
                raise ModelRoutingError(
                    f"model_routing.roles.{role} requires model_id and prompt_version"
                )
            routes[role] = ModelRoute(role=role, model_id=model_id, prompt_version=prompt_version)
        budget = ModelBudget.from_mapping(raw.get("budget"))
        return cls(
            enabled=enabled,
            allowed_triggers=allowed_triggers,
            routes=routes,
            budget=budget,
        )


ModelRunner = Callable[[ModelRoute, Dict[str, Any]], Mapping[str, Any]]


class SubprocessModelRunner:
    """Run an adapter-provided model command with JSON stdin and no shell."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_sec: float = DEFAULT_MODEL_TIMEOUT_SEC,
    ):
        self.command = [str(part) for part in command]
        if not self.command or any(not part.strip() for part in self.command):
            raise ModelRoutingError("model_routing.command must be a non-empty string list")
        shell_executable = explicit_shell_executable(self.command)
        if shell_executable:
            raise ModelRoutingError(
                f"model_routing.command cannot invoke an explicit shell: {shell_executable}"
            )
        self.timeout_sec = _positive_float(timeout_sec, "per_call_timeout_sec")

    def __call__(self, route: ModelRoute, payload: Dict[str, Any]) -> Mapping[str, Any]:
        request = dict(payload)
        request["route"] = {
            "role": route.role,
            "model_id": route.model_id,
            "prompt_version": route.prompt_version,
        }
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(request, ensure_ascii=False, sort_keys=True),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelRoutingError("model command timed out; output suppressed") from exc
        if completed.returncode != 0:
            raise ModelRoutingError(
                f"model command failed with exit code {completed.returncode}; stderr suppressed"
            )
        try:
            response = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ModelRoutingError("model command must return one JSON object") from exc
        if not isinstance(response, Mapping):
            raise ModelRoutingError("model command must return one JSON object")
        return response


def build_opt_in_model_router(
    *,
    adapter: Mapping[str, Any],
    ledger: Ledger,
    enable_requested: bool,
    budget_overlay: Optional[Mapping[str, Any]] = None,
    runner: Optional[ModelRunner] = None,
) -> Optional["ModelRouter"]:
    """Build routing only when the caller explicitly opts in."""

    if not enable_requested:
        return None
    raw = adapter.get("model_routing")
    if not isinstance(raw, Mapping):
        raise ModelRoutingError("adapter.model_routing is required for model routing opt-in")
    roles_raw = raw.get("roles")
    if isinstance(roles_raw, Mapping):
        unconfigured = [
            role
            for role in MODEL_ROLES
            if isinstance(roles_raw.get(role), Mapping)
            and str(roles_raw[role].get("model_id") or "").strip() == "unconfigured"
        ]
        if unconfigured:
            raise ModelRoutingError(
                "model routes are unconfigured: " + ", ".join(sorted(unconfigured))
            )
    effective = dict(raw)
    effective["enabled"] = True
    if not isinstance(budget_overlay, Mapping):
        raise ModelRoutingError("runtime model budget overlay is required before opt-in")
    effective["budget"] = dict(budget_overlay)
    policy = ModelRoutingPolicy.from_mapping(effective)
    unconfigured = [route.role for route in policy.routes.values() if route.model_id == "unconfigured"]
    if unconfigured:
        raise ModelRoutingError("model routes are unconfigured: " + ", ".join(sorted(unconfigured)))

    effective_runner = runner
    if effective_runner is None:
        command = raw.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ModelRoutingError("model_routing.command must be configured before opt-in")
        effective_runner = SubprocessModelRunner(
            command,
            timeout_sec=policy.budget.per_call_timeout_sec,
        )
    return ModelRouter(policy=policy, runner=effective_runner, ledger=ledger)


class ModelRouter:
    """Route model calls without allowing the model to create bug evidence."""

    def __init__(self, *, policy: ModelRoutingPolicy, runner: ModelRunner, ledger: Ledger):
        self.policy = policy
        self.runner = runner
        self.ledger = ledger
        self._batch_id: Optional[str] = None
        self._consecutive_errors = 0
        self._budget_exhausted = False

    def begin_batch(self, batch_id: str) -> None:
        batch_id = str(batch_id).strip()
        if not batch_id:
            raise ModelRoutingError("batch_id is required before model routing")
        self._batch_id = batch_id
        self._consecutive_errors = 0
        self._budget_exhausted = False

    def invoke(
        self,
        *,
        role: str,
        trigger: str,
        payload: Mapping[str, Any],
        machine_verification_passed: bool,
        fingerprint: Optional[str] = None,
    ) -> Mapping[str, Any]:
        if not self.policy.enabled:
            return {"decision": "allow", "reason": "model routing disabled", "disabled": True}
        if self._batch_id is None:
            raise ModelRoutingError("begin_batch must be called before model routing")
        if role not in MODEL_ROLES:
            raise ModelRoutingError(f"unknown model role: {role}")
        if trigger not in self.policy.allowed_triggers or trigger not in MACHINE_TRIGGERS:
            raise ModelRoutingError(f"model routing escalation requires a machine trigger: {trigger}")
        if role == "strong" and not machine_verification_passed:
            raise ModelRoutingError("strong review requires reproduction verification to pass")
        if self._budget_exhausted:
            raise ModelRoutingError("model budget circuit is open for this batch")
        if self._consecutive_errors >= self.policy.budget.consecutive_error_stop:
            raise ModelRoutingError("model error circuit is open for this batch")
        existing_calls = self.ledger.list_model_calls_for_batch(self._batch_id)
        if len(existing_calls) >= self.policy.budget.max_calls_per_batch:
            self._budget_exhausted = True
            raise ModelRoutingError("model call budget exhausted for this batch")

        route = self.policy.routes[role]
        request = dict(payload)
        request.update(
            {
                "role": role,
                "model_id": route.model_id,
                "prompt_version": route.prompt_version,
                "trigger": trigger,
                "machine_verification_passed": machine_verification_passed,
            }
        )
        try:
            try:
                response = self.runner(route, request)
            except ModelRoutingError:
                raise
            except Exception as exc:
                raise ModelRoutingError(
                    f"model runner failed with {type(exc).__name__}; output suppressed"
                ) from exc
            if not isinstance(response, Mapping):
                raise ModelRoutingError("model runner response must be an object")
            prohibited_paths = _prohibited_authority_paths(response)
            if prohibited_paths:
                raise ModelRoutingError(
                    "model response contains prohibited authority fields: "
                    + ", ".join(prohibited_paths)
                )
            unknown_fields = sorted(
                str(field) for field in set(response) - MODEL_RESPONSE_FIELDS
            )
            if unknown_fields:
                raise ModelRoutingError(
                    "model response contains unknown fields: "
                    + ", ".join(unknown_fields)
                )
            if "reason" in response and not isinstance(response["reason"], str):
                raise ModelRoutingError("model response reason must be a string")
            decision = str(response.get("decision") or "").strip()
            if decision not in MODEL_DECISIONS:
                raise ModelRoutingError(
                    "model decision must be allow, veto, or request-more-evidence; models cannot confirm bugs"
                )
            usage = response.get("usage")
            required_usage_fields = {
                "input_tokens",
                "output_tokens",
                "estimated_cost",
                "duration_ms",
            }
            if not isinstance(usage, Mapping) or not required_usage_fields.issubset(usage):
                raise ModelRoutingError(
                    "model usage telemetry requires input_tokens, output_tokens, "
                    "estimated_cost, and duration_ms"
                )
            input_tokens = _nonnegative_int(usage.get("input_tokens"), "input_tokens")
            output_tokens = _nonnegative_int(usage.get("output_tokens"), "output_tokens")
            duration_ms = _nonnegative_int(usage.get("duration_ms"), "duration_ms")
            estimated_cost = _nonnegative_float(
                usage.get("estimated_cost"), "estimated_cost"
            )
            validation_result = str(response.get("validation_result") or "").strip()
            if validation_result != "schema-valid":
                raise ModelRoutingError("validation_result must be schema-valid")
            response_prompt_version = str(response.get("prompt_version") or "").strip()
            if response_prompt_version != route.prompt_version:
                raise ModelRoutingError(
                    "model response prompt_version must match the configured route"
                )
        except ModelRoutingError:
            self._consecutive_errors += 1
            raise
        self._consecutive_errors = 0

        self.ledger.record_model_call(
            call_id=f"model-{uuid4().hex}",
            batch_id=self._batch_id,
            fingerprint=fingerprint,
            role=role,
            model_id=route.model_id,
            prompt_version=route.prompt_version,
            trigger=trigger,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            duration_ms=duration_ms,
            validation_result=validation_result,
            escalated_from="machine-verification" if machine_verification_passed else "machine-signal",
            decision=decision,
        )
        calls = self.ledger.list_model_calls_for_batch(self._batch_id)
        total_input_tokens = sum(int(item["input_tokens"]) for item in calls)
        total_output_tokens = sum(int(item["output_tokens"]) for item in calls)
        total_estimated_cost = sum(float(item["estimated_cost"]) for item in calls)
        if total_input_tokens > self.policy.budget.max_input_tokens_per_batch:
            self._budget_exhausted = True
            raise ModelRoutingError("model input token budget exceeded for this batch")
        if total_output_tokens > self.policy.budget.max_output_tokens_per_batch:
            self._budget_exhausted = True
            raise ModelRoutingError("model output token budget exceeded for this batch")
        if total_estimated_cost > self.policy.budget.max_estimated_cost_per_batch:
            self._budget_exhausted = True
            raise ModelRoutingError("model estimated cost budget exceeded for this batch")
        if duration_ms > self.policy.budget.per_call_timeout_sec * 1000:
            self._budget_exhausted = True
            raise ModelRoutingError("model per-call timeout budget exceeded for this batch")
        return dict(response)

    def review_verified_candidate(self, case: dict, signal: FailureSignal) -> VetoResult:
        if not self.policy.enabled:
            return VetoResult()
        fingerprint = str(case.get("fingerprint") or "").strip() or None
        try:
            response = self.invoke(
                role="strong",
                trigger="machine-verified-failure",
                payload={
                    "fingerprint": fingerprint,
                    "failure_summary": signal.failure_summary(),
                    "oracle_level": signal.oracle_level,
                    "semantic_map_entry_id": signal.semantic_map_entry_id,
                    "allowed_decisions": sorted(MODEL_DECISIONS),
                },
                machine_verification_passed=True,
                fingerprint=fingerprint,
            )
        except ModelRoutingError as exc:
            return VetoResult(vetoed=True, reason=f"model-routing-blocked: {exc}")
        decision = str(response["decision"])
        reason = str(response.get("reason") or "").strip()
        if decision == "allow":
            return VetoResult()
        if decision == "request-more-evidence":
            return VetoResult(vetoed=True, reason=f"request-more-evidence: {reason}")
        return VetoResult(vetoed=True, reason=reason or "strong model veto")

    def batch_metrics(self, batch_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        calls = self.ledger.list_model_calls_for_batch(batch_id)
        token_cost = {
            "model_routing": {
                "call_count": len(calls),
                "input_tokens": sum(int(item["input_tokens"]) for item in calls),
                "output_tokens": sum(int(item["output_tokens"]) for item in calls),
                "estimated_cost": round(sum(float(item["estimated_cost"]) for item in calls), 10),
            }
        }
        duration = {
            "model_routing": {
                "call_count": len(calls),
                "duration_ms": sum(int(item["duration_ms"]) for item in calls),
            }
        }
        return token_cost, duration


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelRoutingError(f"{field} must be a nonnegative integer")
    if value < 0:
        raise ModelRoutingError(f"{field} must be a nonnegative integer")
    return value


def _prohibited_authority_paths(value: Any, path: str = "$") -> list[str]:
    paths = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            child_path = f"{path}.{key}"
            if normalized in PROHIBITED_MODEL_AUTHORITY_FIELDS:
                paths.append(child_path)
            paths.extend(_prohibited_authority_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_prohibited_authority_paths(item, f"{path}[{index}]"))
    return paths


def _nonnegative_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRoutingError(f"{field} must be a nonnegative number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ModelRoutingError(f"{field} must be a finite nonnegative number")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    parsed = _nonnegative_int(value, field)
    if parsed <= 0:
        raise ModelRoutingError(f"{field} must be a positive integer")
    return parsed


def _positive_float(value: Any, field: str) -> float:
    parsed = _nonnegative_float(value, field)
    if parsed <= 0:
        raise ModelRoutingError(f"{field} must be a positive number")
    return parsed
