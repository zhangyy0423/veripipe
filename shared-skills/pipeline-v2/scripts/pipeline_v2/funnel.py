"""Three-way triage funnel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .fingerprint import failure_fingerprint
from .ledger import Ledger
from .observation import ObservationStateMachine
from .schema import FailureSignal
from .verification import VerificationPolicy, run_reproduction_validation


@dataclass(frozen=True)
class FreshnessResult:
    """Contract freshness check result."""

    fresh: bool = True
    contract_mismatch: bool = False
    reason: str = ""


@dataclass(frozen=True)
class TriageInput:
    """Input for one batch through the funnel."""

    batch_id: str
    product_version: str
    environment_digest: str
    sentinel_status: str
    signals: List[FailureSignal]
    token_cost: dict = field(default_factory=dict)
    duration: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FunnelResult:
    """Compact batch outcome."""

    status: str
    reported: List[str]
    observed_count: int
    redline_flags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class VetoResult:
    """LLM veto gate outcome.

    The gate is asymmetric: it can only block a candidate after machine
    evidence exists. It cannot create or promote a report.
    """

    vetoed: bool = False
    reason: str = ""


class Funnel:
    """Serial funnel: sentinel -> reproduction -> freshness -> case report."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        verification_runner: Optional[Callable[[], Optional[FailureSignal]]] = None,
        verification_runner_factory: Optional[Callable[[FailureSignal], Callable[[], Optional[FailureSignal]]]] = None,
        freshness_checker: Optional[Callable[[FailureSignal], FreshnessResult]] = None,
        veto_checker: Optional[Callable[[dict, FailureSignal], VetoResult]] = None,
        publisher: Optional[Callable[[dict, FailureSignal], Optional[str]]] = None,
        policy: Optional[VerificationPolicy] = None,
        observation: Optional[ObservationStateMachine] = None,
    ):
        self.ledger = ledger
        self.verification_runner = verification_runner
        self.verification_runner_factory = verification_runner_factory
        self.freshness_checker = freshness_checker or (lambda _signal: FreshnessResult())
        self.veto_checker = veto_checker or (lambda _case, _signal: VetoResult())
        self.publisher = publisher
        self.policy = policy or VerificationPolicy()
        self.observation = observation or ObservationStateMachine(ledger)

    def process_batch(self, item: TriageInput) -> FunnelResult:
        """Process one batch through the first-batch pipeline v2 funnel."""

        if item.sentinel_status == "triggered":
            self.ledger.upsert_batch(
                batch_id=item.batch_id,
                product_version=item.product_version,
                environment_digest=item.environment_digest,
                signal_count=len(item.signals),
                sentinel_blocked_count=len(item.signals),
                token_cost=item.token_cost,
                duration=item.duration,
                sentinel_status="triggered",
                redline_flags=["sentinel_environment_event"],
            )
            return FunnelResult(status="environment-event", reported=[], observed_count=0)

        counters = {
            "oracle_l1_count": 0,
            "oracle_l2_count": 0,
            "oracle_l3_count": 0,
            "verification_passed_count": 0,
            "verification_blocked_count": 0,
            "freshness_mismatch_count": 0,
            "llm_vetoed_count": 0,
            "reported_count": 0,
            "observation_entered_count": 0,
            "observation_upgraded_count": 0,
        }
        reported: List[str] = []

        for signal in item.signals:
            signal.validate_case_schema()
            counters[f"oracle_{signal.oracle_level.lower()}_count"] += 1
            fingerprint = failure_fingerprint(signal)
            if self.verification_runner_factory:
                runner = self.verification_runner_factory(signal)
            else:
                runner = self.verification_runner or (lambda current=signal: current)
            verification = run_reproduction_validation(signal, runner, self.policy)
            if not verification.passed:
                status = self.observation.record_observation(
                    fingerprint=fingerprint,
                    batch_id=item.batch_id,
                    oracle_level=signal.oracle_level,
                    llm_involvement=signal.llm_involvement,
                    failure_summary=signal.failure_summary(),
                    semantic_map_entry_id=signal.semantic_map_entry_id,
                    verify_entry=verification.as_history(),
                )
                counters["verification_blocked_count"] += 1
                counters["observation_entered_count"] += 1
                if status == "intermittent":
                    counters["observation_upgraded_count"] += 1
                continue

            counters["verification_passed_count"] += 1
            freshness = self.freshness_checker(signal)
            triage_result = None
            if freshness.contract_mismatch or not freshness.fresh:
                counters["freshness_mismatch_count"] += 1
                triage_result = "contract-mismatch-doc"

            case = {"fingerprint": fingerprint}
            veto = self.veto_checker(case, signal)
            if veto.vetoed:
                self.observation.record_observation(
                    fingerprint=fingerprint,
                    batch_id=item.batch_id,
                    oracle_level=signal.oracle_level,
                    llm_involvement=signal.llm_involvement,
                    failure_summary=f"{signal.failure_summary()} | veto={veto.reason}",
                    semantic_map_entry_id=signal.semantic_map_entry_id,
                    verify_entry=verification.as_history(),
                )
                self.ledger.set_case_status(fingerprint, "vetoed")
                counters["llm_vetoed_count"] += 1
                counters["observation_entered_count"] += 1
                continue

            self.ledger.upsert_case(
                fingerprint=fingerprint,
                status="reported",
                oracle_level=signal.oracle_level,
                llm_involvement=signal.llm_involvement,
                batch_id=item.batch_id,
                verify_entry=verification.as_history(),
                triage_result=triage_result,
                semantic_map_entry_id=signal.semantic_map_entry_id,
            )
            self.ledger.record_hit(
                fingerprint=fingerprint,
                batch_id=item.batch_id,
                failure_summary=signal.failure_summary(),
            )
            case = self.ledger.get_case(fingerprint) or {"fingerprint": fingerprint}
            report_ref = self.publisher(case, signal) if self.publisher else None
            if report_ref:
                self.ledger.update_case_refs(fingerprint, report_ref=report_ref)
            counters["reported_count"] += 1
            reported.append(fingerprint)

        self.ledger.upsert_batch(
            batch_id=item.batch_id,
            product_version=item.product_version,
            environment_digest=item.environment_digest,
            signal_count=len(item.signals),
            token_cost=item.token_cost,
            duration=item.duration,
            sentinel_status=item.sentinel_status,
            **counters,
        )
        return FunnelResult(
            status="processed",
            reported=reported,
            observed_count=counters["observation_entered_count"],
        )
