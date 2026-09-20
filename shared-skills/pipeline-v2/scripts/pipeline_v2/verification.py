"""Reproduction verification channels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from .fingerprint import failure_fingerprint
from .oracle_l2 import evaluate_l2_oracle
from .schema import FailureSignal, ORACLE_LEVELS


class VerificationError(ValueError):
    """Raised when a signal has no legal verification channel."""


@dataclass(frozen=True)
class VerificationPolicy:
    """Runtime verification policy."""

    agent_under_test: bool = True
    weak_runs: int = 3
    strong_runs: int = 1
    early_stop: bool = True


@dataclass(frozen=True)
class VerificationChannel:
    """Chosen channel for a failure signal."""

    name: str
    required_runs: int


@dataclass(frozen=True)
class VerificationResult:
    """Result of reproduction validation."""

    passed: bool
    channel: str
    required_runs: int
    attempted_runs: int
    matching_runs: int
    reason: str
    fingerprints: List[str]

    def as_history(self) -> Dict[str, object]:
        return {
            "passed": self.passed,
            "channel": self.channel,
            "required_runs": self.required_runs,
            "attempted_runs": self.attempted_runs,
            "matching_runs": self.matching_runs,
            "reason": self.reason,
            "fingerprints": self.fingerprints,
        }


def classify_channel(signal: FailureSignal, policy: VerificationPolicy) -> VerificationChannel:
    """Map oracle level plus execution determinism to a verification channel."""

    if signal.oracle_level not in ORACLE_LEVELS:
        raise VerificationError(f"oracle_level has no legal channel: {signal.oracle_level}")
    if signal.static_certified and signal.oracle_level == "L2":
        return VerificationChannel("static-exempt", 0)
    if signal.oracle_level == "L1":
        return VerificationChannel("strong", policy.strong_runs)
    if signal.oracle_level == "L2":
        if policy.agent_under_test or signal.nondeterministic_sources:
            return VerificationChannel("weak", policy.weak_runs)
        return VerificationChannel("strong", policy.strong_runs)
    return VerificationChannel("weak", policy.weak_runs)


def run_reproduction_validation(
    original: FailureSignal,
    runner: Callable[[], Optional[FailureSignal]],
    policy: VerificationPolicy,
) -> VerificationResult:
    """Run the chosen reproduction channel.

    The runner must execute the smallest reproducible case and return the next
    structured failure signal. Returning None means the failure did not recur.
    """

    channel = classify_channel(original, policy)
    original_fingerprint = failure_fingerprint(original)
    fingerprints = [original_fingerprint]
    if channel.required_runs == 0:
        return VerificationResult(
            passed=True,
            channel=channel.name,
            required_runs=0,
            attempted_runs=0,
            matching_runs=0,
            reason="static-certified",
            fingerprints=fingerprints,
        )

    matching = 0
    attempted = 0
    for _ in range(channel.required_runs):
        attempted += 1
        observed = runner()
        if observed is None:
            return VerificationResult(
                passed=False,
                channel=channel.name,
                required_runs=channel.required_runs,
                attempted_runs=attempted,
                matching_runs=matching,
                reason="not-reproduced",
                fingerprints=fingerprints,
            )
        fp = failure_fingerprint(observed)
        fingerprints.append(fp)
        if fp != original_fingerprint:
            return VerificationResult(
                passed=False,
                channel=channel.name,
                required_runs=channel.required_runs,
                attempted_runs=attempted,
                matching_runs=matching,
                reason="fingerprint-mismatch",
                fingerprints=fingerprints,
            )
        matching += 1

    return VerificationResult(
        passed=True,
        channel=channel.name,
        required_runs=channel.required_runs,
        attempted_runs=attempted,
        matching_runs=matching,
        reason="fingerprint-consistent",
        fingerprints=fingerprints,
    )


def build_l2_oracle_runner(
    entry: Mapping[str, Any],
    observed_state_runner: Callable[[], Mapping[str, Any]],
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str,
    nondeterministic_sources: Optional[List[str]] = None,
    static_certified: bool = False,
) -> Callable[[], Optional[FailureSignal]]:
    """Build a reproduction runner that rechecks one L2 terminal-state oracle.

    TODO: Wire this to the future executor once the product-run interface is
    designed. For M2 the caller supplies an observed_state dict producer.
    """

    def runner() -> Optional[FailureSignal]:
        observed_state = observed_state_runner()
        result = evaluate_l2_oracle(
            entry,
            observed_state,
            scenario_id=scenario_id,
            step_id=step_id,
            llm_involvement=llm_involvement,
            nondeterministic_sources=nondeterministic_sources,
            static_certified=static_certified,
        )
        return result.failure_signal

    return runner
