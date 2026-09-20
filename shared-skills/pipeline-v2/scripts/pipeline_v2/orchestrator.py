"""End-to-end pipeline-v2 orchestration skeleton.

The orchestrator owns status flow only. Product execution, publication, and
future L1/L3/L4 engines stay behind interfaces so this core remains product
neutral.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set

from .brief import BriefGenerator
from .fingerprint import failure_fingerprint
from .funnel import FreshnessResult, Funnel, TriageInput, VetoResult
from .ledger import Ledger
from .model_routing import ModelRouter, ModelRoutingError, build_opt_in_model_router
from .observation import ObservationStateMachine
from .oracle_l2 import evaluate_l2_oracle
from .publishing import CommandPublisher, build_queue_event
from .schema import FailureSignal, SchemaError
from .verification import VerificationPolicy, classify_channel


@dataclass(frozen=True)
class ExecutionScenario:
    """One executor scenario.

    ``test_cmd`` is intentionally opaque to the core. Product adapters decide
    which concrete command or playwright target it represents. ``spec`` is the
    stable test identifier when the scenario is backed by a Playwright spec.
    """

    scenario_id: str
    step_id: str
    test_cmd: str
    spec: Optional[str] = None
    semantic_map_entry: Optional[Mapping[str, Any]] = None
    llm_involvement: str = "none"
    nondeterministic_sources: Sequence[str] = ()
    static_certified: bool = False
    is_sentinel: bool = False
    oracle_level: str = "L2"
    oracle_strategy: str = "machine_check"
    # L3 专用：参照系判定的输入。
    # ``l3_steps`` 是要依次驱动的多步（每步是一个 test_cmd，交给执行器跑出终态）；
    # 差分用全部步终态比一致，蜕变用 base/transformed 两步终态验关系。
    # ``l3_relation`` 仅蜕变用（equal/restored/subset）；``l3_path_labels`` 仅差分用。
    l3_steps: Sequence[str] = ()
    l3_relation: str = ""
    l3_path_labels: Sequence[str] = ()
    l3_relation_id: str = ""
    # 有状态蜕变（metamorphic_stateful）：基线单独跑这一步，transformed 是
    # l3_steps 在同一会话内顺序执行后的终态。
    l3_baseline_step: str = ""
    # 兄弟一致性（sibling_consistency）：每个 l3_step 是一个兄弟命令；
    # l3_sibling_labels 为各步可读名（用于证据/偏离者标注）。
    l3_sibling_labels: Sequence[str] = ()


@dataclass(frozen=True)
class ExecutionResult:
    """Observed terminal state plus raw failure captured by an executor."""

    observed_state: Mapping[str, Any]
    raw_failure: str = ""
    exit_code: int = 0
    stderr: str = ""
    stdout: str = ""
    environment_unavailable: bool = False


class EnvironmentUnavailable(RuntimeError):
    """Raised internally when execution failure is environment, not product."""


class ExecutorProtocol(Protocol):
    """Executor boundary decided in discuss/10 §1.

    Implementations may be deterministic test doubles or real command
    executors. Product-specific paths and command targets must be injected by
    callers rather than imported by the orchestrator.
    """

    def run(self, *, test_cmd: str, scenario: ExecutionScenario) -> ExecutionResult:
        """Run one scenario and return structured terminal state."""


class MockExecutor:
    """Deterministic executor for offline dry-runs and contract tests."""

    def __init__(self, results: Optional[Mapping[str, Sequence[ExecutionResult]]] = None):
        self._results = {key: list(value) for key, value in (results or {}).items()}
        self._last: Dict[str, ExecutionResult] = {}
        self._calls: Dict[str, int] = {}

    def run(self, *, test_cmd: str, scenario: ExecutionScenario) -> ExecutionResult:
        self._calls[scenario.scenario_id] = self._calls.get(scenario.scenario_id, 0) + 1
        queue = self._results.get(scenario.scenario_id, [])
        if queue:
            result = queue.pop(0)
            self._last[scenario.scenario_id] = result
            return result
        if scenario.scenario_id in self._last:
            return self._last[scenario.scenario_id]
        return ExecutionResult(observed_state={"passed": True}, exit_code=0)

    def call_count(self, scenario_id: str) -> int:
        return self._calls.get(scenario_id, 0)


@dataclass(frozen=True)
class OrchestratorResult:
    """High-level batch result suitable for CLI JSON output."""

    batch_id: str
    status: str
    signal_count: int
    selected_target_count: int
    executed_count: int
    pass_count: int
    no_signal_count: int
    reported_count: int
    observed_count: int
    expired_count: int
    environment_event: bool
    environment_reasons: List[str]
    report_refs: List[str]
    queue_path: Optional[str]
    brief_path: Optional[str]
    module_steps: List[Dict[str, str]]
    coverage: Optional[Dict[str, Any]] = None
    oracle_levels: Optional[List[str]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "signal_count": self.signal_count,
            "selected_target_count": self.selected_target_count,
            "executed_count": self.executed_count,
            "pass_count": self.pass_count,
            "no_signal_count": self.no_signal_count,
            "reported_count": self.reported_count,
            "observed_count": self.observed_count,
            "expired_count": self.expired_count,
            "environment_event": self.environment_event,
            "environment_reasons": self.environment_reasons,
            "report_refs": self.report_refs,
            "queue_path": self.queue_path,
            "brief_path": self.brief_path,
            "module_steps": self.module_steps,
            "coverage": self.coverage,
            "oracle_levels": list(self.oracle_levels or []),
        }


FreshnessChecker = Callable[[FailureSignal], FreshnessResult]
VetoChecker = Callable[[dict, FailureSignal], VetoResult]


class DryRunQueueRunner:
    """CommandPublisher runner that writes queue JSONL instead of pushing."""

    def __init__(self, queue_path: Path):
        self.queue_path = Path(queue_path)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, command: List[str], payload: Dict[str, object]) -> Dict[str, object]:
        kind = command[-1] if command else "event"
        event = build_queue_event(kind, payload)
        with self.queue_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        if kind == "bug_report":
            return {"report_ref": f"queue://bug/{payload.get('fingerprint')}"}
        if kind == "batch_brief":
            return {"wiki_ref": f"queue://brief/{payload.get('batch_id')}"}
        return {}


def _scenario_oracle_levels(scenarios: Sequence[ExecutionScenario]) -> List[str]:
    levels = {scenario.oracle_level for scenario in scenarios if scenario.oracle_level in {"L1", "L2", "L3"}}
    return [level for level in ["L1", "L2", "L3"] if level in levels]


class PipelineV2Orchestrator:
    """Serial process bus for the M3 pipeline-v2 skeleton."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        executor: ExecutorProtocol,
        queue_path: Optional[Path | str] = None,
        brief_path: Optional[Path | str] = None,
        policy: Optional[VerificationPolicy] = None,
        freshness_checker: Optional[FreshnessChecker] = None,
        veto_checker: Optional[VetoChecker] = None,
        model_router: Optional[ModelRouter] = None,
        publisher: Optional[CommandPublisher] = None,
        observation: Optional[ObservationStateMachine] = None,
        suggestion_path: Optional[Path | str] = None,
        semantic_map: Optional[Mapping[str, Any]] = None,
    ):
        self.ledger = ledger
        self.executor = executor
        self.queue_path = Path(queue_path) if queue_path else None
        self.brief_path = Path(brief_path) if brief_path else None
        # L4 建议通道：与 bug queue 物理隔离的独立产出（discuss/02 §2.1）。
        self.suggestion_path = Path(suggestion_path) if suggestion_path else None
        # semantic-map 用于覆盖率可观测（哪些 entry 未被本批触达）。
        self.semantic_map = semantic_map
        self.policy = policy or VerificationPolicy()
        self.freshness_checker = freshness_checker or (lambda _signal: FreshnessResult())
        if veto_checker is not None and model_router is not None:
            raise ValueError("veto_checker and model_router are mutually exclusive")
        if publisher is not None:
            raise ValueError(
                "batch publishing must use the local queue; run explicit reviewed flush separately"
            )
        self.model_router = model_router
        self.veto_checker = veto_checker or (
            model_router.review_verified_candidate if model_router else (lambda _case, _signal: VetoResult())
        )
        self.observation = observation or ObservationStateMachine(ledger)
        self.publisher = self._build_queue_publisher(self.queue_path)

    def run_batch(
        self,
        *,
        batch_id: str,
        product_version: str,
        environment_digest: str,
        sentinel_scenarios: Sequence[ExecutionScenario],
        scenarios: Sequence[ExecutionScenario],
    ) -> OrchestratorResult:
        """Run one offline-capable batch through the process bus."""

        if self.model_router:
            self.model_router.begin_batch(batch_id)

        environment_reasons: List[str] = []
        sentinel_status = self._run_sentinels(sentinel_scenarios, environment_reasons)
        signals: List[FailureSignal] = []
        signal_scenarios: Dict[str, ExecutionScenario] = {}
        selected_target_count = sum(
            1 for scenario in scenarios if scenario.oracle_level in {"L1", "L2", "L3"}
        )
        executed_count = 0
        pass_count = 0
        no_signal_count = 0

        if sentinel_status != "triggered":
            for scenario in scenarios:
                countable = scenario.oracle_level in {"L1", "L2", "L3"}
                if countable:
                    executed_count += 1
                try:
                    signal = self._generate_signal(scenario)
                except EnvironmentUnavailable as exc:
                    reason = str(exc).strip()
                    if reason:
                        environment_reasons.append(reason)
                    sentinel_status = "triggered"
                    signals = []
                    signal_scenarios = {}
                    break
                if signal is None:
                    if countable and scenario.oracle_level == "L1":
                        no_signal_count += 1
                    elif countable:
                        pass_count += 1
                    continue
                signal.validate_case_schema()
                classify_channel(signal, self.policy)
                signals.append(signal)
                signal_scenarios[failure_fingerprint(signal)] = scenario

        funnel = Funnel(
            self.ledger,
            verification_runner_factory=lambda signal: self._verification_runner(signal, signal_scenarios),
            freshness_checker=self.freshness_checker,
            veto_checker=self.veto_checker,
            publisher=self.publisher.create_bug_report if self.publisher else None,
            policy=self.policy,
            observation=self.observation,
        )
        funnel_result = funnel.process_batch(
            TriageInput(
                batch_id=batch_id,
                product_version=product_version,
                environment_digest=environment_digest,
                sentinel_status=sentinel_status,
                signals=signals,
            )
        )

        expired = self.observation.expire_stale(current_batch_id=batch_id)
        environment_event = funnel_result.status == "environment-event" or self.observation.detect_batch_environment_event(
            batch_id
        )
        if expired:
            self._patch_batch_observation_counts(batch_id, len(expired))
        if self.model_router:
            token_cost, duration = self.model_router.batch_metrics(batch_id)
            self.ledger.update_batch_metrics(
                batch_id,
                token_cost=token_cost,
                duration=duration,
            )

        brief = BriefGenerator(self.ledger).render(batch_id)
        if self.brief_path:
            self.brief_path.parent.mkdir(parents=True, exist_ok=True)
            self.brief_path.write_text(brief, encoding="utf-8")
        if self.publisher:
            self.publisher.publish_brief(batch_id, brief)

        report_refs = [
            str(case["report_ref"])
            for case in self.ledger.list_reported_cases_for_batch(batch_id)
            if case.get("report_ref")
        ]

        # 覆盖可观测 + L4 建议通道（与 bug queue 物理隔离）。
        coverage = self._emit_coverage_and_suggestions(scenarios)

        return OrchestratorResult(
            batch_id=batch_id,
            status=funnel_result.status,
            signal_count=len(signals),
            selected_target_count=selected_target_count,
            executed_count=executed_count,
            pass_count=pass_count,
            no_signal_count=no_signal_count,
            reported_count=len(funnel_result.reported),
            observed_count=funnel_result.observed_count,
            expired_count=len(expired),
            environment_event=environment_event,
            environment_reasons=environment_reasons,
            report_refs=report_refs,
            queue_path=str(self.queue_path) if self.queue_path else None,
            brief_path=str(self.brief_path) if self.brief_path else None,
            module_steps=self.module_steps(),
            coverage=coverage,
            oracle_levels=_scenario_oracle_levels(scenarios),
        )

    def _emit_coverage_and_suggestions(
        self, scenarios: Sequence[ExecutionScenario]
    ) -> Optional[Dict[str, Any]]:
        """计算 semantic-map 覆盖率，并把未覆盖路径作为"选靶建议"写入独立 sink。

        建议通道绝不写 bug queue：只调 SuggestionSink，从不调 create_bug_report。"""

        if self.semantic_map is None:
            return None
        from .suggestion import Suggestion, SuggestionSink, compute_coverage, coverage_gaps_to_suggestions

        exercised = [
            str(s.semantic_map_entry["id"])
            for s in scenarios
            if isinstance(s.semantic_map_entry, Mapping)
            and s.semantic_map_entry.get("id")
            and s.oracle_level != "L4"
        ]
        report = compute_coverage(self.semantic_map, exercised)
        sink = SuggestionSink(self.suggestion_path)
        sink.extend(coverage_gaps_to_suggestions(report))
        sink.extend(
            [
                Suggestion(
                    kind="clue",
                    summary=f"L4 clue requires explicit L1/L2/L3 oracle before bug filing: {scenario.scenario_id}",
                    semantic_map_entry_id=str((scenario.semantic_map_entry or {}).get("id") or ""),
                    detail={
                        "scenario_id": scenario.scenario_id,
                        "step_id": scenario.step_id,
                        "oracle_level": "L4",
                    },
                )
                for scenario in scenarios
                if scenario.oracle_level == "L4"
            ]
        )
        sink.flush()
        return report.as_dict()

    @staticmethod
    def module_steps() -> List[Dict[str, str]]:
        """Return the design-to-module map used by the skeleton."""

        return [
            {"step": "sentinel check", "module": "pipeline_v2.orchestrator + ExecutorProtocol"},
            {"step": "signal generation", "module": "pipeline_v2.oracle_l2"},
            {"step": "L4 suggestion isolation", "module": "pipeline_v2.suggestion; no L4 filing path"},
            {"step": "channel assignment", "module": "pipeline_v2.verification.classify_channel"},
            {"step": "reproduction verification", "module": "pipeline_v2.verification.run_reproduction_validation"},
            {"step": "contract freshness", "module": "pipeline_v2.funnel.FreshnessResult callback"},
            {"step": "evidence integrity", "module": "pipeline_v2.schema.FailureSignal.validate_case_schema"},
            {"step": "LLM veto gate", "module": "pipeline_v2.funnel.VetoResult callback"},
            {"step": "reporting", "module": "pipeline_v2.publishing.CommandPublisher"},
            {"step": "observation exits", "module": "pipeline_v2.observation.ObservationStateMachine"},
            {"step": "ledger and brief", "module": "pipeline_v2.ledger + pipeline_v2.brief"},
        ]

    def _run_sentinels(self, sentinels: Sequence[ExecutionScenario], environment_reasons: List[str]) -> str:
        if not sentinels:
            return "skipped"
        for scenario in sentinels:
            result = self.executor.run(test_cmd=scenario.test_cmd, scenario=scenario)
            if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
                reason = result.raw_failure or str(result.observed_state.get("environment_reason") or "")
                if reason:
                    environment_reasons.append(reason)
                return "triggered"
            if result.exit_code != 0 or result.observed_state.get("passed") is False:
                reason = result.raw_failure or result.stderr or f"sentinel failed: {scenario.scenario_id}"
                if reason:
                    environment_reasons.append(reason)
                return "triggered"
        return "passed"

    def _generate_signal(self, scenario: ExecutionScenario) -> Optional[FailureSignal]:
        if scenario.oracle_level == "L4":
            return None
        if scenario.oracle_level == "L1":
            return self._l1_signal(scenario)
        if scenario.oracle_level == "L3":
            return self._l3_signal(scenario)
        if scenario.oracle_level != "L2":
            raise SchemaError(f"signal generation is not implemented for oracle_level={scenario.oracle_level}")
        if not scenario.semantic_map_entry:
            raise SchemaError("L2 scenario requires semantic_map_entry")

        result = self.executor.run(test_cmd=scenario.test_cmd, scenario=scenario)
        if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
            raise EnvironmentUnavailable(result.raw_failure)
        if scenario.oracle_strategy == "playwright_spec":
            return self._playwright_spec_signal(scenario, result)
        if scenario.oracle_strategy != "machine_check":
            raise SchemaError(f"unknown oracle_strategy={scenario.oracle_strategy}")
        oracle = evaluate_l2_oracle(
            scenario.semantic_map_entry,
            result.observed_state,
            scenario_id=scenario.scenario_id,
            step_id=scenario.step_id,
            llm_involvement=scenario.llm_involvement,
            nondeterministic_sources=list(scenario.nondeterministic_sources),
            static_certified=scenario.static_certified,
        )
        if oracle.failure_signal is None:
            return None
        return self._attach_executor_evidence(oracle.failure_signal, result)

    def _playwright_spec_signal(
        self,
        scenario: ExecutionScenario,
        result: ExecutionResult,
    ) -> Optional[FailureSignal]:
        if result.observed_state.get("passed") is not False:
            return None
        entry = scenario.semantic_map_entry or {}
        entry_id = str(entry.get("id", "")).strip()
        expected_terminal = entry.get("expected_terminal_state") if isinstance(entry, Mapping) else {}
        expected_terminal = expected_terminal if isinstance(expected_terminal, Mapping) else {}
        assertion_type = str(expected_terminal.get("assertion_type") or "playwright_spec").strip()
        failed_assertions = result.observed_state.get("failed_assertions")
        first_failure = failed_assertions[0] if isinstance(failed_assertions, list) and failed_assertions else {}
        stack_anchor = None
        if isinstance(first_failure, Mapping):
            stack_anchor = str(first_failure.get("location") or "").strip() or None
        signal = FailureSignal(
            scenario_id=scenario.scenario_id,
            step_id=scenario.step_id,
            failure_type=assertion_type,
            oracle_level="L2",
            llm_involvement=scenario.llm_involvement,
            expected_state={
                "playwright_spec": "passed",
                "spec": scenario.spec or result.observed_state.get("spec") or scenario.test_cmd,
                "assertion": expected_terminal.get("assertion", ""),
                "observable": expected_terminal.get("observable", ""),
                "assertion_type": assertion_type,
            },
            actual_state=dict(result.observed_state),
            stack_anchor=stack_anchor,
            nondeterministic_sources=list(scenario.nondeterministic_sources),
            static_certified=scenario.static_certified,
            semantic_map_entry_id=entry_id,
            evidence={
                "playwright_spec": {
                    "strategy": "spec-as-oracle",
                    "spec": scenario.spec or result.observed_state.get("spec") or scenario.test_cmd,
                    "passed": result.observed_state.get("passed"),
                    "failed_assertions": failed_assertions or [],
                    "duration": result.observed_state.get("duration"),
                }
            },
        )
        return self._attach_executor_evidence(signal, result)

    def _anchor_l4_signal(self, scenario: ExecutionScenario) -> Optional[FailureSignal]:
        """L4 线索锚定。

        L4 永不直接立案（discuss/02 §2）：线索必须锚到 L1-L3 的机器证据。
        orchestrator 不做隐式降级；如果选靶阶段已经找到机器 oracle，必须显式构造
        L1/L2/L3 scenario 后再进入 funnel。纯 L4 只进 suggestion sink。
        """

        return None

    def _l1_signal(self, scenario: ExecutionScenario) -> Optional[FailureSignal]:
        """L1 崩溃信号：跑执行器，把硬失败（崩溃/超时/非零/5xx）判为 L1。

        L1 在任意输入下都安全（discuss/06）：执行器只要跑出进程/HTTP 硬指标即可。
        但进入 funnel/cases 前仍必须有 semantic_map_entry，保证 bug queue 能解释选靶来源。
        环境不可用仍短路为 environment event。"""

        from .oracle_l1 import evaluate_l1_from_result

        result = self.executor.run(test_cmd=scenario.test_cmd, scenario=scenario)
        if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
            raise EnvironmentUnavailable(result.raw_failure)
        l1 = evaluate_l1_from_result(
            result,
            scenario_id=scenario.scenario_id,
            step_id=scenario.step_id,
            llm_involvement=scenario.llm_involvement,
            semantic_map_entry_id=(scenario.semantic_map_entry or {}).get("id"),
        )
        if l1.failure_signal is None:
            return None
        return self._attach_executor_evidence(l1.failure_signal, result)

    def _run_l3_step(self, scenario: ExecutionScenario, test_cmd: str) -> Mapping[str, Any]:
        """跑 L3 的一步，返回结构化 observed_state；环境不可用则抛 EnvironmentUnavailable。"""

        step_scenario = replace(scenario, test_cmd=test_cmd)
        result = self.executor.run(test_cmd=test_cmd, scenario=step_scenario)
        if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
            raise EnvironmentUnavailable(result.raw_failure)
        return result.observed_state

    @staticmethod
    def _is_rejected(state: Mapping[str, Any]) -> bool:
        """判断一步是否"拒绝了非法输入"（用于兄弟一致性）。

        拒绝 = 产出 error 事件 / passed is False / 终态 success is False。"""

        if not isinstance(state, Mapping):
            return False
        if state.get("terminal_event_type") == "error":
            return True
        if state.get("passed") is False:
            return True
        terminal = state.get("terminal_state")
        if isinstance(terminal, Mapping) and terminal.get("success") is False:
            return True
        return False

    def _run_l3_stateful_sequence(self, scenario: ExecutionScenario) -> Mapping[str, Any]:
        """在同一会话内顺序驱动 l3_steps，返回最后一步终态。

        用于有状态蜕变（如 hide->unhide / A->B->A）：仅执行器支持
        run_session_sequence 时可用。环境不可用抛 EnvironmentUnavailable。"""

        import json as _json

        run_seq = getattr(self.executor, "run_session_sequence", None)
        if run_seq is None:
            raise SchemaError("stateful L3 sequence requires an executor with run_session_sequence")
        messages = []
        for raw in scenario.l3_steps:
            try:
                messages.append(_json.loads(raw))
            except (TypeError, ValueError):
                messages.append({"type": "raw", "payload": raw})
        result = run_seq(messages)
        if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
            raise EnvironmentUnavailable(result.raw_failure)
        return result.observed_state

    def _l3_signal(self, scenario: ExecutionScenario) -> Optional[FailureSignal]:
        """L3 参照系信号：差分 / 蜕变 / 兄弟一致性。机器验证，LLM 只提关系。"""

        from .oracle_l3 import evaluate_differential, evaluate_metamorphic, evaluate_sibling_consistency

        if scenario.oracle_strategy == "sibling_consistency":
            # 兄弟一致性：每个 l3_step 是一个兄弟命令（喂同样的非法输入）。
            # 跑每步，把"是否拒绝了非法输入"归一成 {sibling, rejected}。
            # rejected = 该步产出 error 事件 / passed is False / success is False。
            observations = []
            for index, cmd in enumerate(scenario.l3_steps):
                state = self._run_l3_step(scenario, cmd)
                label = scenario.l3_sibling_labels[index] if index < len(scenario.l3_sibling_labels) else f"sib{index}"
                observations.append({"sibling": label, "rejected": self._is_rejected(state)})
            result = evaluate_sibling_consistency(
                observations,
                scenario_id=scenario.scenario_id,
                step_id=scenario.step_id,
                llm_involvement=scenario.llm_involvement,
                group_id=scenario.l3_relation_id,
                semantic_map_entry_id=(scenario.semantic_map_entry or {}).get("id")
                if isinstance(scenario.semantic_map_entry, Mapping) else None,
            )
            return result.failure_signal

        if scenario.oracle_strategy == "metamorphic_stateful":
            # 有状态蜕变：l3_steps 在同一会话内顺序执行，最后一步终态是
            # transformed；基线由 l3_baseline_step 单独跑出。比较 base vs transformed。
            if not scenario.l3_baseline_step:
                raise SchemaError("metamorphic_stateful requires l3_baseline_step")
            base_state = self._run_l3_step(scenario, scenario.l3_baseline_step)
            transformed_state = self._run_l3_stateful_sequence(scenario)
            result = evaluate_metamorphic(
                relation=scenario.l3_relation,
                base_state=base_state,
                transformed_state=transformed_state,
                scenario_id=scenario.scenario_id,
                step_id=scenario.step_id,
                llm_involvement=scenario.llm_involvement,
                relation_id=scenario.l3_relation_id,
                semantic_map_entry_id=(scenario.semantic_map_entry or {}).get("id")
                if isinstance(scenario.semantic_map_entry, Mapping) else None,
            )
            return result.failure_signal

        steps = list(scenario.l3_steps) or [scenario.test_cmd]
        states = [self._run_l3_step(scenario, cmd) for cmd in steps]

        if scenario.oracle_strategy == "differential":
            result = evaluate_differential(
                states,
                scenario_id=scenario.scenario_id,
                step_id=scenario.step_id,
                llm_involvement=scenario.llm_involvement,
                semantic_map_entry_id=(scenario.semantic_map_entry or {}).get("id")
                if isinstance(scenario.semantic_map_entry, Mapping) else None,
                path_labels=scenario.l3_path_labels,
            )
        elif scenario.oracle_strategy == "metamorphic":
            if len(states) < 2:
                raise SchemaError("metamorphic L3 requires at least two l3_steps (base + transformed)")
            result = evaluate_metamorphic(
                relation=scenario.l3_relation,
                base_state=states[0],
                transformed_state=states[1],
                scenario_id=scenario.scenario_id,
                step_id=scenario.step_id,
                llm_involvement=scenario.llm_involvement,
                relation_id=scenario.l3_relation_id,
                semantic_map_entry_id=(scenario.semantic_map_entry or {}).get("id")
                if isinstance(scenario.semantic_map_entry, Mapping) else None,
            )
        else:
            raise SchemaError(f"unknown L3 oracle_strategy={scenario.oracle_strategy}")
        return result.failure_signal

    def _verification_runner(
        self,
        signal: FailureSignal,
        signal_scenarios: Mapping[str, ExecutionScenario],
    ) -> Callable[[], Optional[FailureSignal]]:
        fingerprint = failure_fingerprint(signal)
        scenario = signal_scenarios.get(fingerprint)
        if scenario is None:
            return lambda current=signal: current
        if scenario.oracle_level == "L3":
            def l3_runner() -> Optional[FailureSignal]:
                try:
                    return self._l3_signal(scenario)
                except EnvironmentUnavailable:
                    return None
            return l3_runner
        if scenario.oracle_level == "L1":
            def l1_runner() -> Optional[FailureSignal]:
                try:
                    return self._l1_signal(scenario)
                except EnvironmentUnavailable:
                    return None
            return l1_runner
        if scenario.oracle_level != "L2" or not scenario.semantic_map_entry:
            return lambda current=signal: current

        def runner() -> Optional[FailureSignal]:
            result = self.executor.run(test_cmd=scenario.test_cmd, scenario=scenario)
            if result.environment_unavailable or result.observed_state.get("environment_unavailable") is True:
                return None
            if scenario.oracle_strategy == "playwright_spec":
                return self._playwright_spec_signal(scenario, result)
            oracle = evaluate_l2_oracle(
                scenario.semantic_map_entry,
                result.observed_state,
                scenario_id=scenario.scenario_id,
                step_id=scenario.step_id,
                llm_involvement=scenario.llm_involvement,
                nondeterministic_sources=list(scenario.nondeterministic_sources),
                static_certified=scenario.static_certified,
            )
            if oracle.failure_signal is None:
                return None
            return self._attach_executor_evidence(oracle.failure_signal, result)

        return runner

    def _attach_executor_evidence(self, signal: FailureSignal, result: ExecutionResult) -> FailureSignal:
        evidence = dict(signal.evidence)
        evidence["executor"] = {
            "exit_code": result.exit_code,
            "raw_failure": result.raw_failure,
            "spec": result.observed_state.get("spec"),
            "duration": result.observed_state.get("duration"),
            "failed_assertions": result.observed_state.get("failed_assertions", []),
            "environment_unavailable": result.environment_unavailable
            or result.observed_state.get("environment_unavailable") is True,
        }
        return replace(
            signal,
            output_text=result.stdout,
            error_text=result.stderr or result.raw_failure,
            exit_code=result.exit_code,
            evidence=evidence,
        )

    def _patch_batch_observation_counts(self, batch_id: str, expired_count: int) -> None:
        batch = self.ledger.get_batch(batch_id)
        if not batch:
            return
        self.ledger.upsert_batch(
            batch_id=batch_id,
            product_version=str(batch.get("product_version") or ""),
            environment_digest=str(batch.get("environment_digest") or ""),
            signal_count=int(batch["signal_count"]),
            sentinel_blocked_count=int(batch["sentinel_blocked_count"]),
            oracle_l1_count=int(batch["oracle_l1_count"]),
            oracle_l2_count=int(batch["oracle_l2_count"]),
            oracle_l3_count=int(batch["oracle_l3_count"]),
            verification_passed_count=int(batch["verification_passed_count"]),
            verification_blocked_count=int(batch["verification_blocked_count"]),
            freshness_mismatch_count=int(batch["freshness_mismatch_count"]),
            llm_vetoed_count=int(batch["llm_vetoed_count"]),
            reported_count=int(batch["reported_count"]),
            observation_entered_count=int(batch["observation_entered_count"]),
            observation_upgraded_count=int(batch["observation_upgraded_count"]),
            observation_expired_count=expired_count,
            token_cost=json.loads(batch["token_cost"] or "{}"),
            duration=json.loads(batch["duration"] or "{}"),
            sentinel_status=str(batch["sentinel_status"]),
            redline_flags=json.loads(batch["redline_flags"] or "[]"),
            injected_recall_rate=batch["injected_recall_rate"],
        )

    def _build_queue_publisher(self, queue_path: Optional[Path]) -> Optional[CommandPublisher]:
        if queue_path is None:
            return None
        runner = DryRunQueueRunner(queue_path)
        return CommandPublisher(
            runner=runner,
            create_bug_command=["dry-run", "bug_report"],
            publish_wiki_command=["dry-run", "batch_brief"],
        )


@dataclass(frozen=True)
class SampleDryRun:
    """Ready-to-run M4 offline sample."""

    orchestrator: PipelineV2Orchestrator
    sentinels: List[ExecutionScenario]
    scenarios: List[ExecutionScenario]


def build_adapter_playwright_run(
    *,
    adapter_path: Path | str,
    semantic_map_path: Optional[Path | str] = None,
    target_plan_path: Optional[Path | str] = None,
    product_cwd: Path | str = Path("."),
    dry_run_report_path: Optional[Path | str] = None,
    output_dir: Optional[Path | str] = None,
    timeout_sec: int = 1800,
) -> tuple[ExecutorProtocol, List[ExecutionScenario]]:
    """Build a product-neutral Playwright executor from adapter config."""

    from .playwright_executor import PlaywrightExecutionConfig, PlaywrightExecutor

    adapter_file = Path(adapter_path)
    adapter = _load_json_mapping(adapter_file)
    _enforce_owner_gate(adapter)
    playwright = adapter.get("playwright")
    if not isinstance(playwright, Mapping):
        raise SchemaError("adapter.playwright is required for --executor playwright")

    whitelist = playwright.get("spec_whitelist")
    if not isinstance(whitelist, list) or not whitelist:
        raise SchemaError("adapter.playwright.spec_whitelist must be a non-empty list")

    web_subdir = _adapter_relative_path(
        playwright,
        key="web_subdir",
        field_name="adapter.playwright.web_subdir",
        allow_current=True,
    )
    config_path = _adapter_relative_path(
        playwright,
        key="config_path",
        field_name="adapter.playwright.config_path",
        allow_current=False,
        root_label="web_subdir",
    )
    env = _string_mapping(playwright.get("env"), field_name="adapter.playwright.env")
    testdata_gates = _playwright_testdata_gates(playwright)

    semantic_file = _resolve_semantic_map_path(
        adapter_path=adapter_file,
        adapter=adapter,
        semantic_map_path=Path(semantic_map_path) if semantic_map_path else None,
    )
    semantic_entries = _load_semantic_entries_by_id(semantic_file)

    scenarios: List[ExecutionScenario] = []
    for index, item in enumerate(whitelist):
        if not isinstance(item, Mapping):
            raise SchemaError(f"adapter.playwright.spec_whitelist[{index}] must be an object")
        spec_value = item.get("spec")
        entry_id_value = item.get("semantic_map_entry_id")
        if spec_value is not None and not isinstance(spec_value, str):
            raise SchemaError(f"adapter.playwright.spec_whitelist[{index}].spec must be a string")
        if entry_id_value is not None and not isinstance(entry_id_value, str):
            raise SchemaError(
                f"adapter.playwright.spec_whitelist[{index}].semantic_map_entry_id must be a string"
            )
        spec = str(spec_value or "").strip()
        entry_id = str(entry_id_value or "").strip()
        if not spec:
            raise SchemaError(f"adapter.playwright.spec_whitelist[{index}].spec is required")
        spec_error = _adapter_relative_path_error(
            spec,
            field_name=f"adapter.playwright.spec_whitelist[{index}].spec",
            allow_current=False,
            root_label="spec_dir",
        )
        if spec_error:
            raise SchemaError(spec_error)
        if not entry_id:
            raise SchemaError(
                f"adapter.playwright.spec_whitelist[{index}].semantic_map_entry_id is required"
            )
        entry = semantic_entries.get(entry_id)
        if entry is None:
            raise SchemaError(f"semantic_map_entry_id not found in semantic map: {entry_id}")
        scenarios.append(
            ExecutionScenario(
                scenario_id=f"spec:{spec}",
                step_id="playwright",
                test_cmd=spec,
                spec=spec,
                semantic_map_entry=entry,
                llm_involvement="none",
                oracle_strategy="playwright_spec",
            )
        )

    whitelisted_specs = {str(scenario.spec or "") for scenario in scenarios}
    for gate_index, gate in enumerate(testdata_gates):
        gate_entry_id = str(gate.get("semantic_map_entry_id") or "").strip()
        if gate_entry_id and gate_entry_id not in semantic_entries:
            raise SchemaError(
                f"adapter.playwright.testdata_gates[{gate_index}].semantic_map_entry_id "
                f"not found in semantic map: {gate_entry_id}"
            )
        for spec in gate["specs"]:
            if spec not in whitelisted_specs:
                raise SchemaError(
                    f"adapter.playwright.testdata_gates[{gate_index}].specs contains "
                    f"non-whitelisted spec: {spec}"
                )

    scenarios = _filter_scenarios_by_target_plan(
        scenarios,
        target_plan_path=target_plan_path,
        oracle_level="L2",
    )

    executor = PlaywrightExecutor(
        PlaywrightExecutionConfig(
            product_cwd=Path(product_cwd),
            web_subdir=web_subdir,
            config_path=config_path,
            dry_run_report_path=Path(dry_run_report_path) if dry_run_report_path else None,
            output_dir=Path(output_dir) if output_dir else None,
            timeout_sec=timeout_sec,
            env=env,
            required_env=_playwright_required_env(playwright, testdata_gates=testdata_gates),
        )
    )
    return executor, scenarios


def build_adapter_http_run(
    *,
    adapter_path: Path | str,
    semantic_map_path: Optional[Path | str] = None,
    target_plan_path: Optional[Path | str] = None,
) -> tuple[ExecutorProtocol, List[ExecutionScenario]]:
    """从 adapter 的可选 ``http_driver`` 段构造产品中立的 HTTP+WS 执行器 + L3 场景。

    adapter.http_driver 形态（全部产品专属，由 adapter 提供，core 不内置任何端点）：
      {
        "base_url": "http://127.0.0.1:8799",
        "ws_base_url": "ws://127.0.0.1:8799",
        "create_session_path": "/api/sessions",
        "ws_path_template": "/api/chat/ws/{session_id}",
        "create_session_body": {},                 # 可选
        "terminal_event_types": [...],             # 可选，默认 tool_call_result/command_result
        "scenarios": [                             # L3 场景清单
          {
            "scenario_id": "...", "strategy": "differential|metamorphic",
            "llm_involvement": "clue_source",
            "steps": ["<JSON编码的WS驱动消息>", ...],
            "relation": "equal|restored|subset",   # 仅 metamorphic
            "relation_id": "...",                  # 仅 metamorphic
            "path_labels": [...],                  # 仅 differential
            "semantic_map_entry_id": "..."         # 必填，且必须命中 semantic-map
          }
        ]
      }
    """

    from .http_driver import HttpAgentExecutor, HttpDriverConfig, UrllibWsTransport

    adapter = _load_json_mapping(Path(adapter_path))
    _enforce_owner_gate(adapter)
    http = adapter.get("http_driver")
    if not isinstance(http, Mapping):
        raise SchemaError("adapter.http_driver is required for --executor http")

    base_url = str(http.get("base_url") or "").strip()
    ws_base_url = str(http.get("ws_base_url") or "").strip()
    create_path = str(http.get("create_session_path") or "").strip()
    ws_template = str(http.get("ws_path_template") or "").strip()
    if not (base_url and ws_base_url and create_path and ws_template):
        raise SchemaError("adapter.http_driver requires base_url/ws_base_url/create_session_path/ws_path_template")

    terminal_types = _string_list(http.get("terminal_event_types"), field_name="adapter.http_driver.terminal_event_types")
    config_kwargs: Dict[str, Any] = {
        "create_session_path": create_path,
        "ws_path_template": ws_template,
    }
    if isinstance(http.get("create_session_body"), Mapping):
        config_kwargs["create_session_body"] = dict(http["create_session_body"])
    if terminal_types:
        config_kwargs["terminal_event_types"] = tuple(terminal_types)
    if "drain_initial_events" in http:
        config_kwargs["drain_initial_events"] = _positive_int(
            http.get("drain_initial_events"),
            field_name="adapter.http_driver.drain_initial_events",
            allow_zero=True,
        )
    if "max_events" in http:
        config_kwargs["max_events"] = _positive_int(
            http.get("max_events"),
            field_name="adapter.http_driver.max_events",
            allow_zero=False,
        )
    driver_config = HttpDriverConfig(**config_kwargs)

    recv_timeout_sec = _positive_float(
        http.get("recv_timeout_sec", 20.0),
        field_name="adapter.http_driver.recv_timeout_sec",
    )
    transport = UrllibWsTransport(
        base_url=base_url,
        ws_base_url=ws_base_url,
        recv_timeout_sec=recv_timeout_sec,
    )
    executor = HttpAgentExecutor(transport, driver_config)

    semantic_entries: Mapping[str, Mapping[str, Any]] = {}
    sem_file = Path(semantic_map_path) if semantic_map_path else None
    if sem_file is None:
        try:
            sem_file = _resolve_semantic_map_path(adapter_path=Path(adapter_path), adapter=adapter, semantic_map_path=None)
        except SchemaError:
            sem_file = None
    if sem_file is not None and Path(sem_file).is_file():
        semantic_entries = _load_semantic_entries_by_id(Path(sem_file))

    raw_scenarios = http.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise SchemaError("adapter.http_driver.scenarios must be a non-empty list")

    scenarios: List[ExecutionScenario] = []
    for index, item in enumerate(raw_scenarios):
        if not isinstance(item, Mapping):
            raise SchemaError(f"adapter.http_driver.scenarios[{index}] must be an object")
        strategy = str(item.get("strategy") or "").strip()
        if strategy not in ("differential", "metamorphic", "metamorphic_stateful", "sibling_consistency"):
            raise SchemaError(
                f"scenarios[{index}].strategy must be differential|metamorphic|metamorphic_stateful|sibling_consistency"
            )
        steps = _string_list(item.get("steps"), field_name=f"scenarios[{index}].steps")
        if not steps:
            raise SchemaError(f"scenarios[{index}].steps is required")
        baseline_step = str(item.get("baseline_step") or "").strip()
        if strategy == "metamorphic_stateful" and not baseline_step:
            raise SchemaError(f"scenarios[{index}].baseline_step is required for metamorphic_stateful")
        sibling_labels = _string_list(item.get("sibling_labels"), field_name=f"scenarios[{index}].sibling_labels")
        if strategy == "sibling_consistency" and len(steps) < 3:
            raise SchemaError(f"scenarios[{index}].steps needs >=3 siblings for sibling_consistency")
        entry_id = str(item.get("semantic_map_entry_id") or "").strip()
        if not entry_id:
            raise SchemaError(f"adapter.http_driver.scenarios[{index}].semantic_map_entry_id is required")
        entry = semantic_entries.get(entry_id)
        if not entry:
            raise SchemaError(
                f"adapter.http_driver.scenarios[{index}].semantic_map_entry_id "
                f"not found in semantic map: {entry_id}"
            )
        scenarios.append(
            ExecutionScenario(
                scenario_id=str(item.get("scenario_id") or f"l3-{index}"),
                step_id="ws",
                test_cmd=steps[0],
                semantic_map_entry=entry,
                llm_involvement=str(item.get("llm_involvement") or "clue_source"),
                oracle_level="L3",
                oracle_strategy=strategy,
                l3_steps=tuple(steps),
                l3_relation=str(item.get("relation") or ""),
                l3_relation_id=str(item.get("relation_id") or ""),
                l3_baseline_step=baseline_step,
                l3_sibling_labels=tuple(sibling_labels),
                l3_path_labels=tuple(_string_list(item.get("path_labels"), field_name=f"scenarios[{index}].path_labels")),
            )
        )
    scenarios = _filter_scenarios_by_target_plan(
        scenarios,
        target_plan_path=target_plan_path,
        oracle_level="L3",
    )
    return executor, scenarios


def build_adapter_l1_fuzz_run(
    *,
    adapter_path: Path | str,
    semantic_map_path: Optional[Path | str] = None,
    target_plan_path: Optional[Path | str] = None,
) -> tuple[ExecutorProtocol, List[ExecutionScenario]]:
    """从 adapter 可选 ``l1_fuzz`` 段构造 HTTP 崩溃 fuzz 执行器 + L1 场景。

    adapter.l1_fuzz 形态（产品专属，core 不内置任何端点）：
      {
        "base_url": "http://127.0.0.1:8799",
        "cases": [
          {"id": "malformed-json-sessions", "method": "POST",
           "path": "/api/sessions", "headers": {"Content-Type": "application/json"},
           "raw_body": "{not valid json",
           "semantic_map_entry_id": "..."},
          ...
        ]
      }
    """

    from .l1_fuzz_executor import FuzzCase, L1FuzzExecutor, UrllibHttpProbe

    adapter_file = Path(adapter_path)
    adapter = _load_json_mapping(adapter_file)
    _enforce_owner_gate(adapter)
    fuzz = adapter.get("l1_fuzz")
    if not isinstance(fuzz, Mapping):
        raise SchemaError("adapter.l1_fuzz is required for --executor l1-fuzz")
    base_url = str(fuzz.get("base_url") or "").strip()
    if not base_url:
        raise SchemaError("adapter.l1_fuzz.base_url is required")
    raw_cases = fuzz.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SchemaError("adapter.l1_fuzz.cases must be a non-empty list")

    semantic_entries: Mapping[str, Mapping[str, Any]] = {}
    sem_file = Path(semantic_map_path) if semantic_map_path else None
    if sem_file is None:
        try:
            sem_file = _resolve_semantic_map_path(
                adapter_path=adapter_file,
                adapter=adapter,
                semantic_map_path=None,
            )
        except SchemaError:
            sem_file = None
    if sem_file is not None and Path(sem_file).is_file():
        semantic_entries = _load_semantic_entries_by_id(Path(sem_file))

    cases_by_id: Dict[str, Any] = {}
    scenarios: List[ExecutionScenario] = []
    for index, item in enumerate(raw_cases):
        if not isinstance(item, Mapping):
            raise SchemaError(f"adapter.l1_fuzz.cases[{index}] must be an object")
        case_id = str(item.get("id") or f"fuzz-{index}").strip()
        method = str(item.get("method") or "GET").strip().upper()
        path = str(item.get("path") or "").strip()
        if not path:
            raise SchemaError(f"adapter.l1_fuzz.cases[{index}].path is required")
        entry_id_value = item.get("semantic_map_entry_id")
        if not isinstance(entry_id_value, str):
            raise SchemaError(f"adapter.l1_fuzz.cases[{index}].semantic_map_entry_id must be a string")
        entry_id = str(entry_id_value).strip()
        if not entry_id:
            raise SchemaError(f"adapter.l1_fuzz.cases[{index}].semantic_map_entry_id is required")
        entry = semantic_entries.get(entry_id)
        if not entry:
            raise SchemaError(
                f"adapter.l1_fuzz.cases[{index}].semantic_map_entry_id "
                f"not found in semantic map: {entry_id}"
            )
        headers = item.get("headers") if isinstance(item.get("headers"), Mapping) else {}
        cases_by_id[case_id] = FuzzCase(
            method=method,
            path=path,
            headers={str(k): str(v) for k, v in headers.items()},
            raw_body=str(item.get("raw_body") or ""),
        )
        scenarios.append(
            ExecutionScenario(
                scenario_id=f"l1:{case_id}",
                step_id="http-fuzz",
                test_cmd=case_id,
                semantic_map_entry=entry,
                llm_involvement="none",
                oracle_level="L1",
                oracle_strategy="crash",
            )
        )
    scenarios = _filter_scenarios_by_target_plan(
        scenarios,
        target_plan_path=target_plan_path,
        oracle_level="L1",
    )
    allowed_case_ids = {scenario.test_cmd for scenario in scenarios}
    cases_by_id = {
        case_id: case
        for case_id, case in cases_by_id.items()
        if case_id in allowed_case_ids
    }
    executor = L1FuzzExecutor(UrllibHttpProbe(base_url=base_url), cases_by_id)
    return executor, scenarios


def _filter_scenarios_by_target_plan(
    scenarios: Sequence[ExecutionScenario],
    *,
    target_plan_path: Optional[Path | str],
    oracle_level: str,
) -> List[ExecutionScenario]:
    """Restrict execution to target-plan items for the current oracle level."""

    if target_plan_path is None:
        return list(scenarios)
    plan = _load_json_mapping(Path(target_plan_path))
    key_by_level = {
        "L1": "must_run_l1_fuzz",
        "L2": "must_run_l2_checks",
        "L3": "must_run_l3_relations",
    }
    key = key_by_level.get(oracle_level)
    if key is None:
        raise SchemaError(f"unsupported target-plan oracle level: {oracle_level}")
    targets = plan.get(key)
    if not isinstance(targets, list) or not targets:
        raise SchemaError(f"target plan has no {oracle_level} executable targets")

    selected: List[ExecutionScenario] = []
    selected_ids: Set[str] = set()
    unmatched: List[str] = []
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise SchemaError(f"target plan {key}[{index}] must be an object")
        matches = [
            scenario
            for scenario in scenarios
            if _scenario_matches_target(scenario, target, oracle_level=oracle_level)
        ]
        if not matches:
            unmatched.append(_target_label(target, oracle_level=oracle_level, index=index))
            continue
        for scenario in matches:
            if scenario.scenario_id in selected_ids:
                continue
            selected_ids.add(scenario.scenario_id)
            selected.append(scenario)
    if unmatched:
        raise SchemaError(
            f"target plan {key} contains non-executable targets for adapter: "
            + ", ".join(unmatched)
        )
    return selected


def _scenario_matches_target(
    scenario: ExecutionScenario,
    target: Mapping[str, Any],
    *,
    oracle_level: str,
) -> bool:
    target_entry = str(target.get("semantic_map_entry_id") or "").strip()
    scenario_entry = _scenario_semantic_entry_id(scenario)
    if target_entry and target_entry != scenario_entry:
        return False
    if oracle_level == "L2":
        target_spec = str(target.get("spec") or "").strip()
        target_spec_path = str(target.get("spec_path") or "").strip()
        spec = str(scenario.spec or scenario.test_cmd or "").strip()
        return bool(
            target_spec
            and target_spec == spec
            or target_spec_path
            and Path(target_spec_path).name == spec
        )
    if oracle_level == "L3":
        target_scenario_id = str(target.get("scenario_id") or "").strip()
        return bool(target_scenario_id and target_scenario_id == scenario.scenario_id)
    if oracle_level == "L1":
        target_case_id = str(target.get("case_id") or target.get("id") or "").strip()
        target_path = str(target.get("path") or "").strip()
        return bool(
            target_case_id
            and target_case_id == scenario.test_cmd
            or target_path
            and target_path == scenario.test_cmd
        )
    return False


def _scenario_semantic_entry_id(scenario: ExecutionScenario) -> str:
    if not isinstance(scenario.semantic_map_entry, Mapping):
        return ""
    return str(scenario.semantic_map_entry.get("id") or "").strip()


def _target_label(target: Mapping[str, Any], *, oracle_level: str, index: int) -> str:
    if oracle_level == "L2":
        value = str(target.get("spec") or target.get("spec_path") or "").strip()
    elif oracle_level == "L3":
        value = str(target.get("scenario_id") or "").strip()
    else:
        value = str(target.get("case_id") or target.get("id") or target.get("path") or "").strip()
    entry = str(target.get("semantic_map_entry_id") or "").strip()
    suffix = f" -> {entry}" if entry else ""
    return f"{value or f'#{index}'}{suffix}"


def run_adapter_playwright_preflight(
    *,
    adapter_path: Path | str,
    semantic_map_path: Optional[Path | str] = None,
    target_plan_path: Optional[Path | str] = None,
    product_cwd: Path | str = Path("."),
    output_dir: Optional[Path | str] = None,
    hosts_file: Path | str = Path("/etc/hosts"),
) -> Dict[str, Any]:
    """Run read-only checks before a real adapter-driven Playwright batch."""

    adapter_file = Path(adapter_path)
    checks: List[Dict[str, str]] = []
    try:
        adapter = _load_json_mapping(adapter_file)
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.config", "fail", str(exc)))
        return _preflight_result(checks)
    checks.extend(_owner_gate_preflight_checks(adapter))
    playwright = adapter.get("playwright")

    if not isinstance(playwright, Mapping):
        checks.append(_preflight_check("adapter.playwright", "fail", "adapter.playwright is required"))
        return _preflight_result(checks)

    try:
        _executor, scenarios = build_adapter_playwright_run(
            adapter_path=adapter_file,
            semantic_map_path=semantic_map_path,
            target_plan_path=target_plan_path,
            product_cwd=product_cwd,
        )
        checks.append(
            _preflight_check(
                "adapter.semantic_map",
                "ok",
                f"{len(scenarios)} whitelisted specs have semantic-map entries",
            )
        )
    except Exception as exc:
        checks.append(_preflight_check(_preflight_exception_check_name(exc), "fail", str(exc)))
        scenarios = []

    product_root = Path(product_cwd).resolve()
    checks.append(
        _preflight_check(
            "product_cwd",
            "ok" if product_root.is_dir() else "fail",
            str(product_root),
        )
    )

    if output_dir:
        resolved_output_dir = Path(output_dir).resolve()
        if resolved_output_dir == product_root or product_root in resolved_output_dir.parents:
            checks.append(
                _preflight_check(
                    "playwright.output_dir",
                    "fail",
                    f"output_dir must be outside product_cwd: {resolved_output_dir}",
                )
            )
        else:
            checks.append(_preflight_check("playwright.output_dir", "ok", str(resolved_output_dir)))

    web_subdir, web_subdir_error = _optional_adapter_relative_path(
        playwright,
        key="web_subdir",
        field_name="adapter.playwright.web_subdir",
        allow_current=True,
    )
    if web_subdir_error:
        checks.append(_preflight_check("adapter.playwright.web_subdir", "fail", web_subdir_error))
    web_dir = product_root / web_subdir if web_subdir and not web_subdir_error else product_root
    checks.append(
        _preflight_check(
            "playwright.web_subdir",
            "ok" if web_dir.is_dir() else "fail",
            str(web_dir),
        )
    )

    config_path, config_path_error = _optional_adapter_relative_path(
        playwright,
        key="config_path",
        field_name="adapter.playwright.config_path",
        allow_current=False,
        root_label="web_subdir",
    )
    if config_path_error:
        checks.append(_preflight_check("adapter.playwright.config_path", "fail", config_path_error))
    config_file = web_dir / config_path if config_path and not config_path_error else web_dir
    checks.append(
        _preflight_check(
            "playwright.config_path",
            "ok" if config_file.is_file() else "fail",
            str(config_file),
        )
    )

    try:
        required_browsers = _string_list(
            playwright.get("required_browsers"),
            field_name="adapter.playwright.required_browsers",
        )
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.required_browsers", "fail", str(exc)))
        required_browsers = []
    node_ok = True
    if web_dir.is_dir():
        node_check = _playwright_node_version_check(web_dir=web_dir)
        checks.append(node_check)
        node_ok = node_check["status"] == "ok"
    if required_browsers and web_dir.is_dir() and node_ok:
        checks.extend(_playwright_browser_install_checks(required_browsers, web_dir=web_dir))

    try:
        adapter_env = _string_mapping(playwright.get("env"), field_name="adapter.playwright.env")
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.env", "fail", str(exc)))
        adapter_env = {}
    try:
        testdata_gates = _playwright_testdata_gates(playwright)
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.testdata_gates", "fail", str(exc)))
        testdata_gates = []
    try:
        required_env = _playwright_required_env(playwright, testdata_gates=testdata_gates)
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.required_env", "fail", str(exc)))
        required_env = []
    required_env_values = {name: adapter_env.get(name, os.environ.get(name, "")) for name in required_env}
    for name in required_env:
        value = required_env_values[name]
        checks.append(
            _preflight_check(
                f"env.{name}",
                "ok" if value.strip() else "fail",
                f"{name} is {'set' if value.strip() else 'missing or empty'}",
            )
        )

    try:
        required_env_files = _playwright_required_env_files(playwright, testdata_gates=testdata_gates)
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.required_env_files", "fail", str(exc)))
        required_env_files = []
    for env_file in required_env_files:
        path_error = _adapter_relative_path_error(
            env_file,
            field_name=f"env_file.{env_file}",
            allow_current=False,
        )
        if path_error:
            checks.append(_preflight_check(f"env_file.{env_file}", "fail", path_error))
            continue
        target = (product_root / env_file).resolve()
        if product_root != target and product_root not in target.parents:
            checks.append(
                _preflight_check(
                    f"env_file.{env_file}",
                    "fail",
                    f"env_file.{env_file} must be relative and stay inside product_cwd: {env_file}",
                )
            )
            continue
        text = _read_optional_text(target)
        if not target.is_file():
            checks.append(_preflight_check(f"env_file.{env_file}", "fail", f"env file not found: {target}"))
            continue
        for name in required_env:
            declared = _env_file_declares(text, name)
            current_env_set = bool(required_env_values[name].strip())
            status = "ok" if current_env_set or declared else "fail"
            if current_env_set:
                detail = f"{name} is set in current env; {env_file} declaration is optional"
            else:
                detail = f"{name} is {'declared' if declared else 'not declared'} in {env_file}"
            checks.append(
                _preflight_check(
                    f"env_file.{env_file}.{name}",
                    status,
                    detail,
                )
            )

    try:
        preflight_paths = _mapping_list(
            playwright.get("preflight_paths"),
            field_name="adapter.playwright.preflight_paths",
        )
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.preflight_paths", "fail", str(exc)))
        preflight_paths = []
    for index, item in enumerate(preflight_paths):
        relative = _optional_string_value(
            item,
            key="path",
            field_name=f"adapter.playwright.preflight_paths[{index}].path",
        )
        path_type = str(item.get("type") or "file").strip()
        executable_value = item.get("executable", False)
        if relative is None:
            checks.append(
                _preflight_check(
                    "path",
                    "fail",
                    f"adapter.playwright.preflight_paths[{index}].path must be a string",
                )
            )
            continue
        if not relative:
            checks.append(
                _preflight_check(
                    "path",
                    "fail",
                    f"adapter.playwright.preflight_paths[{index}].path must be a non-empty string",
                )
            )
            continue
        if not isinstance(executable_value, bool):
            checks.append(
                _preflight_check(
                    f"path.{relative}",
                    "fail",
                    f"adapter.playwright.preflight_paths[{index}].executable must be boolean",
                )
            )
            continue
        executable = executable_value
        if path_type not in {"file", "dir"}:
            checks.append(
                _preflight_check(
                    f"path.{relative}",
                    "fail",
                    f"adapter.playwright.preflight_paths[{index}].type must be file or dir: {path_type}",
                )
            )
            continue
        path_error = _adapter_relative_path_error(relative, field_name=f"path.{relative}", allow_current=False)
        if path_error:
            checks.append(_preflight_check(f"path.{relative}", "fail", path_error))
            continue
        target = (product_root / relative).resolve()
        if product_root != target and product_root not in target.parents:
            checks.append(
                _preflight_check(
                    f"path.{relative}",
                    "fail",
                    f"path.{relative} must be relative and stay inside product_cwd: {relative}",
                )
            )
            continue
        if path_type == "dir":
            ok = target.is_dir()
        else:
            ok = target.is_file()
        if ok and executable:
            ok = os.access(str(target), os.X_OK)
        checks.append(_preflight_check(f"path.{relative}", "ok" if ok else "fail", str(target)))

    spec_dir, spec_dir_error = _optional_adapter_relative_path(
        playwright,
        key="spec_dir",
        field_name="adapter.playwright.spec_dir",
        allow_current=True,
    )
    if spec_dir_error:
        checks.append(_preflight_check("adapter.playwright.spec_dir", "fail", spec_dir_error))
        spec_root = None
    elif spec_dir:
        spec_root = web_dir / spec_dir
    else:
        spec_root = None
    if spec_root is not None:
        for scenario in scenarios:
            spec_path = spec_root / str(scenario.spec or scenario.test_cmd)
            checks.append(
                _preflight_check(
                    f"spec.{scenario.spec or scenario.test_cmd}",
                    "ok" if spec_path.is_file() else "fail",
                    str(spec_path),
                )
            )

    if scenarios and web_dir.is_dir() and config_file.is_file() and node_ok:
        checks.extend(
            _playwright_spec_bundle_checks(
                scenarios,
                web_dir=web_dir,
                config_path=config_path,
                env=adapter_env,
            )
        )

    whitelisted_specs = {str(scenario.spec or "") for scenario in scenarios}
    for gate in testdata_gates:
        gate_id = str(gate.get("id") or "<missing>")
        missing_specs = [spec for spec in gate.get("specs", []) if spec not in whitelisted_specs]
        checks.append(
            _preflight_check(
                f"testdata_gate.{gate_id}.specs",
                "ok" if not missing_specs else "fail",
                (
                    f"{len(gate.get('specs', []))} gated specs are whitelisted"
                    if not missing_specs
                    else f"non-whitelisted gated specs: {', '.join(missing_specs)}"
                ),
            )
        )
        for name in gate.get("required_env", []):
            value = required_env_values.get(str(name), "")
            checks.append(
                _preflight_check(
                    f"testdata_gate.{gate_id}.env.{name}",
                    "ok" if str(value).strip() else "fail",
                    f"{name} is {'set' if str(value).strip() else 'missing or empty'}",
                )
            )
        expected_properties = gate.get("expected_properties", [])
        if expected_properties:
            checks.append(
                _preflight_check(
                    f"testdata_gate.{gate_id}.expected_properties",
                    "ok",
                    f"{len(expected_properties)} declared fixture expectations",
                )
            )

    hosts_text = _read_optional_text(Path(hosts_file))
    try:
        required_hosts = _mapping_list(
            playwright.get("required_hosts"),
            field_name="adapter.playwright.required_hosts",
        )
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.playwright.required_hosts", "fail", str(exc)))
        required_hosts = []
    for index, item in enumerate(required_hosts):
        host = _required_string_field(
            item,
            key="host",
            field_name=f"adapter.playwright.required_hosts[{index}].host",
        )
        address = _required_string_field(
            item,
            key="address",
            field_name=f"adapter.playwright.required_hosts[{index}].address",
        )
        if host is None or address is None:
            missing = host is None
            field = "host" if missing else "address"
            checks.append(
                _preflight_check(
                    f"host.<invalid-{index}>",
                    "fail",
                    f"adapter.playwright.required_hosts[{index}].{field} must be a non-empty string",
                )
            )
            continue
        ok = bool(host and address and _hosts_contains(hosts_text, host=host, address=address))
        checks.append(
            _preflight_check(
                f"host.{host or '<missing>'}",
                "ok" if ok else "fail",
                f"{host} -> {address}",
            )
        )

    return _preflight_result(checks)


def run_adapter_http_preflight(
    *,
    adapter_path: Path | str,
    semantic_map_path: Optional[Path | str] = None,
    target_plan_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Validate an adapter-driven HTTP/L3 + bounded L1 run without creating product state.

    The preflight is intentionally loopback-only. It validates adapter/semantic/target
    binding through the same builders used by execution, then performs TCP reachability
    checks for the injected HTTP and WS origins. It does not create a session, open a
    WebSocket, write a ledger, or call any product-specific endpoint.
    """

    adapter_file = Path(adapter_path)
    checks: List[Dict[str, str]] = []
    try:
        adapter = _load_json_mapping(adapter_file)
    except SchemaError as exc:
        checks.append(_preflight_check("adapter.config", "fail", str(exc)))
        return _preflight_result(checks)
    checks.extend(_owner_gate_preflight_checks(adapter))

    try:
        _executor, scenarios = build_adapter_http_run(
            adapter_path=adapter_file,
            semantic_map_path=semantic_map_path,
            target_plan_path=target_plan_path,
        )
        checks.append(
            _preflight_check(
                "adapter.http_driver",
                "ok",
                f"{len(scenarios)} selected L3 scenarios",
            )
        )
    except Exception as exc:
        checks.append(_preflight_check(_preflight_exception_check_name(exc), "fail", str(exc)))

    try:
        _executor, cases = build_adapter_l1_fuzz_run(
            adapter_path=adapter_file,
            semantic_map_path=semantic_map_path,
            target_plan_path=target_plan_path,
        )
        checks.append(
            _preflight_check(
                "adapter.l1_fuzz",
                "ok",
                f"{len(cases)} selected L1 cases",
            )
        )
    except Exception as exc:
        checks.append(_preflight_check(_preflight_exception_check_name(exc), "fail", str(exc)))

    http = adapter.get("http_driver")
    if not isinstance(http, Mapping):
        return _preflight_result(checks)
    base_url = str(http.get("base_url") or "").strip()
    ws_base_url = str(http.get("ws_base_url") or "").strip()
    checks.append(_loopback_reachability_check("runtime.http_base_url", base_url, schemes={"http", "https"}))
    checks.append(_loopback_reachability_check("runtime.ws_base_url", ws_base_url, schemes={"ws", "wss"}))

    l1 = adapter.get("l1_fuzz")
    if isinstance(l1, Mapping):
        l1_base_url = str(l1.get("base_url") or "").strip()
        checks.append(
            _preflight_check(
                "runtime.l1_base_url",
                "ok" if l1_base_url == base_url else "fail",
                "matches http_driver.base_url" if l1_base_url == base_url else "must match http_driver.base_url",
            )
        )
    return _preflight_result(checks)


def _loopback_reachability_check(name: str, value: str, *, schemes: Set[str]) -> Dict[str, str]:
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise ValueError(f"URL must use one of {sorted(schemes)} and include a host")
        host = parsed.hostname
        default_port = 443 if parsed.scheme in {"https", "wss"} else 80
        port = parsed.port or default_port
        try:
            address = ipaddress.ip_address(host)
            if not address.is_loopback:
                raise ValueError("runtime preflight is loopback-only")
            connect_host = str(address)
        except ValueError as exc:
            if host.lower() != "localhost":
                raise ValueError("runtime preflight is loopback-only") from exc
            resolved = {
                item[4][0]
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
            if not resolved or any(not ipaddress.ip_address(item).is_loopback for item in resolved):
                raise ValueError("localhost resolved to a non-loopback address")
            connect_host = sorted(resolved)[0]
        connection = socket.create_connection((connect_host, port), timeout=3.0)
        connection.close()
        return _preflight_check(name, "ok", f"{connect_host}:{port} reachable")
    except (OSError, ValueError) as exc:
        return _preflight_check(name, "fail", str(exc))


def _load_json_mapping(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"adapter config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"adapter config must be valid JSON: {path}") from exc
    if not isinstance(data, Mapping):
        raise SchemaError(f"adapter config must be a JSON object: {path}")
    return data


def _owner_gate_preflight_checks(adapter: Mapping[str, Any]) -> List[Dict[str, str]]:
    gate = adapter.get("owner_gate")
    if gate is None:
        return []
    if not isinstance(gate, Mapping):
        return [_preflight_check("adapter.owner_gate", "fail", "adapter.owner_gate must be an object")]
    gate_id = _required_string_field(gate, key="id", field_name="adapter.owner_gate.id")
    required_env = _required_string_field(
        gate,
        key="required_env",
        field_name="adapter.owner_gate.required_env",
    )
    if gate_id is None:
        return [_preflight_check("adapter.owner_gate.id", "fail", "adapter.owner_gate.id must be a non-empty string")]
    if required_env is None:
        return [
            _preflight_check(
                f"owner_gate.{gate_id}",
                "fail",
                "adapter.owner_gate.required_env must be a non-empty string",
            )
        ]
    value = os.environ.get(required_env, "")
    acknowledged = value == gate_id
    return [
        _preflight_check(
            f"owner_gate.{gate_id}",
            "ok" if acknowledged else "fail",
            (
                f"{required_env} explicitly acknowledges owner gate {gate_id}"
                if acknowledged
                else f"{required_env} must equal owner gate id {gate_id}; value is not printed"
            ),
        )
    ]


def _enforce_owner_gate(adapter: Mapping[str, Any]) -> None:
    gate = adapter.get("owner_gate")
    if gate is None:
        return
    if not isinstance(gate, Mapping):
        raise SchemaError("adapter.owner_gate must be an object")
    gate_id = _required_string_field(gate, key="id", field_name="adapter.owner_gate.id")
    required_env = _required_string_field(
        gate,
        key="required_env",
        field_name="adapter.owner_gate.required_env",
    )
    if gate_id is None:
        raise SchemaError("adapter.owner_gate.id must be a non-empty string")
    if required_env is None:
        raise SchemaError("adapter.owner_gate.required_env must be a non-empty string")
    if os.environ.get(required_env, "") != gate_id:
        raise SchemaError(
            f"{required_env} must equal owner gate id {gate_id}; value is not printed"
        )


def _playwright_required_env(
    playwright: Mapping[str, Any],
    *,
    testdata_gates: Sequence[Mapping[str, Any]],
) -> List[str]:
    required = _string_list(playwright.get("required_env"), field_name="adapter.playwright.required_env")
    for gate_index, gate in enumerate(testdata_gates):
        required.extend(
            _string_list(
                gate.get("required_env"),
                field_name=f"adapter.playwright.testdata_gates[{gate_index}].required_env",
            )
        )
    return _unique_strings(required)


def _playwright_required_env_files(
    playwright: Mapping[str, Any],
    *,
    testdata_gates: Sequence[Mapping[str, Any]],
) -> List[str]:
    required = _string_list(
        playwright.get("required_env_files"),
        field_name="adapter.playwright.required_env_files",
    )
    for gate_index, gate in enumerate(testdata_gates):
        required.extend(
            _string_list(
                gate.get("required_env_files"),
                field_name=f"adapter.playwright.testdata_gates[{gate_index}].required_env_files",
            )
        )
    return _unique_strings(required)


def _playwright_testdata_gates(playwright: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    gates = _mapping_list(
        playwright.get("testdata_gates"),
        field_name="adapter.playwright.testdata_gates",
    )
    normalized: List[Mapping[str, Any]] = []
    for gate_index, gate in enumerate(gates):
        gate_id = _required_string_field(
            gate,
            key="id",
            field_name=f"adapter.playwright.testdata_gates[{gate_index}].id",
        )
        if gate_id is None:
            raise SchemaError(f"adapter.playwright.testdata_gates[{gate_index}].id must be a non-empty string")
        entry_id_value = gate.get("semantic_map_entry_id")
        if entry_id_value is not None and not isinstance(entry_id_value, str):
            raise SchemaError(
                f"adapter.playwright.testdata_gates[{gate_index}].semantic_map_entry_id must be a string"
            )
        semantic_map_entry_id = str(entry_id_value or "").strip()
        specs = _string_list(
            gate.get("specs"),
            field_name=f"adapter.playwright.testdata_gates[{gate_index}].specs",
        )
        if not specs:
            raise SchemaError(f"adapter.playwright.testdata_gates[{gate_index}].specs must be non-empty")
        for spec_index, spec in enumerate(specs):
            spec_error = _adapter_relative_path_error(
                spec,
                field_name=f"adapter.playwright.testdata_gates[{gate_index}].specs[{spec_index}]",
                allow_current=False,
                root_label="spec_dir",
            )
            if spec_error:
                raise SchemaError(spec_error)
        required_env = _string_list(
            gate.get("required_env"),
            field_name=f"adapter.playwright.testdata_gates[{gate_index}].required_env",
        )
        required_env_files = _string_list(
            gate.get("required_env_files"),
            field_name=f"adapter.playwright.testdata_gates[{gate_index}].required_env_files",
        )
        for file_index, env_file in enumerate(required_env_files):
            path_error = _adapter_relative_path_error(
                env_file,
                field_name=f"adapter.playwright.testdata_gates[{gate_index}].required_env_files[{file_index}]",
                allow_current=False,
            )
            if path_error:
                raise SchemaError(path_error)
        expected_properties = _mapping_list(
            gate.get("expected_properties"),
            field_name=f"adapter.playwright.testdata_gates[{gate_index}].expected_properties",
        )
        for property_index, item in enumerate(expected_properties):
            name = _required_string_field(
                item,
                key="name",
                field_name=(
                    f"adapter.playwright.testdata_gates[{gate_index}]"
                    f".expected_properties[{property_index}].name"
                ),
            )
            if name is None:
                raise SchemaError(
                    f"adapter.playwright.testdata_gates[{gate_index}]"
                    f".expected_properties[{property_index}].name must be a non-empty string"
                )
            source_path = str(item.get("source_path") or "").strip()
            if source_path:
                path_error = _adapter_relative_path_error(
                    source_path,
                    field_name=(
                        f"adapter.playwright.testdata_gates[{gate_index}]"
                        f".expected_properties[{property_index}].source_path"
                    ),
                    allow_current=False,
                )
                if path_error:
                    raise SchemaError(path_error)
        normalized.append(
            {
                "id": gate_id,
                "semantic_map_entry_id": semantic_map_entry_id,
                "specs": _unique_strings(specs),
                "required_env": _unique_strings(required_env),
                "required_env_files": _unique_strings(required_env_files),
                "expected_properties": list(expected_properties),
                "failure_policy": str(gate.get("failure_policy") or "blocker-only").strip() or "blocker-only",
            }
        )
    return normalized


def _unique_strings(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _load_optional_semantic_map(
    *,
    adapter_path: Optional[Path],
    semantic_map_path: Optional[Path],
) -> Optional[Mapping[str, Any]]:
    if adapter_path is None:
        return None
    try:
        semantic_file = _resolve_semantic_map_path(
            adapter_path=adapter_path,
            adapter=_load_json_mapping(adapter_path),
            semantic_map_path=semantic_map_path,
        )
        if not Path(semantic_file).is_file():
            return None
        import yaml

        data = yaml.safe_load(Path(semantic_file).read_text(encoding="utf-8"))
        return data if isinstance(data, Mapping) else None
    except Exception:
        return None


def _resolve_semantic_map_path(
    *,
    adapter_path: Path,
    adapter: Mapping[str, Any],
    semantic_map_path: Optional[Path],
) -> Path:
    if semantic_map_path:
        return semantic_map_path
    product_value = adapter.get("product")
    if product_value is not None and not isinstance(product_value, str):
        raise SchemaError("adapter.product must be a string")
    product = str(product_value or "").strip()
    if not product:
        raise SchemaError("adapter.product is required to locate semantic map")
    product_path = Path(product)
    if product_path.is_absolute() or len(product_path.parts) != 1 or product_path.parts[0] in {".", ".."}:
        raise SchemaError("adapter.product must be a single relative path segment")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", product):
        raise SchemaError("adapter.product must match [a-z0-9][a-z0-9-]*")
    try:
        repo_root = adapter_path.resolve().parents[2]
    except IndexError as exc:
        raise SchemaError("--semantic-map is required when adapter is outside products/<product>/") from exc
    return repo_root / "adapters" / product / "semantic-map.yaml"


def _load_semantic_entries_by_id(path: Path) -> Mapping[str, Mapping[str, Any]]:
    try:
        import yaml
    except ImportError as exc:
        raise SchemaError("PyYAML is required to load pipeline-v2 semantic maps") from exc

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"semantic map not found: {path}") from exc
    except OSError as exc:
        raise SchemaError(f"semantic map is not readable: {path}") from exc
    except yaml.YAMLError as exc:
        raise SchemaError(f"semantic map must be valid YAML: {path}") from exc
    if not isinstance(data, Mapping):
        raise SchemaError(f"semantic map must be a YAML object: {path}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise SchemaError(f"semantic map entries must be a list: {path}")
    by_id: Dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise SchemaError(f"semantic map entry[{index}] must be an object")
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise SchemaError(f"semantic map entry[{index}].id is required")
        by_id[entry_id] = entry
    return by_id


def _string_list(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{field_name} must be a list of strings")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SchemaError(f"{field_name}[{index}] must be a string")
        text = item.strip()
        if not text:
            raise SchemaError(f"{field_name}[{index}] must be a non-empty string")
        result.append(text)
    return result


def _positive_int(value: Any, *, field_name: str, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{field_name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise SchemaError(f"{field_name} must be >= {minimum}")
    return value


def _positive_float(value: Any, *, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SchemaError(f"{field_name} must be a number")
    result = float(value)
    if result <= 0:
        raise SchemaError(f"{field_name} must be > 0")
    return result


def _mapping_list(value: Any, *, field_name: str) -> List[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{field_name} must be a list of objects")
    result: List[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SchemaError(f"{field_name}[{index}] must be an object")
        result.append(item)
    return result


def _string_mapping(value: Any, *, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SchemaError(f"{field_name} must be an object with string values")
    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SchemaError(f"{field_name} keys must be non-empty strings")
        if not isinstance(item, str):
            raise SchemaError(f"{field_name}.{key} must be a string")
        result[key.strip()] = item
    return result


def _adapter_relative_path(
    mapping: Mapping[str, Any],
    *,
    key: str,
    field_name: str,
    allow_current: bool,
    root_label: str = "product_cwd",
) -> str:
    value = mapping.get(key)
    if value is not None and not isinstance(value, str):
        raise SchemaError(f"{field_name} must be a string")
    text = str(value or "").strip()
    error = _adapter_relative_path_error(
        text,
        field_name=field_name,
        allow_current=allow_current,
        root_label=root_label,
    )
    if error:
        raise SchemaError(error)
    return text


def _optional_adapter_relative_path(
    mapping: Mapping[str, Any],
    *,
    key: str,
    field_name: str,
    allow_current: bool,
    root_label: str = "product_cwd",
) -> tuple[str, str]:
    value = mapping.get(key)
    if value is not None and not isinstance(value, str):
        return "", f"{field_name} must be a string"
    text = str(value or "").strip()
    if not text:
        return "", ""
    error = _adapter_relative_path_error(
        text,
        field_name=field_name,
        allow_current=allow_current,
        root_label=root_label,
    )
    return text, error


def _optional_string_value(mapping: Mapping[str, Any], *, key: str, field_name: str) -> Optional[str]:
    value = mapping.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    return value.strip()


def _required_string_field(mapping: Mapping[str, Any], *, key: str, field_name: str) -> Optional[str]:
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


def _adapter_relative_path_error(
    value: str,
    *,
    field_name: str,
    allow_current: bool,
    root_label: str = "product_cwd",
) -> str:
    text = str(value or "").strip()
    if not text:
        return f"{field_name} is required"
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return f"{field_name} must be relative and stay inside {root_label}: {text}"
    if not allow_current and text == ".":
        return f"{field_name} must not be the product root"
    return ""


def _read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _hosts_contains(text: str, *, host: str, address: str) -> bool:
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[0] == address and host in parts[1:]:
            return True
    return False


def _env_file_declares(text: str, name: str) -> bool:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=", re.MULTILINE)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if pattern.match(line):
            return True
    return False


def _playwright_node_version_check(*, web_dir: Path) -> Dict[str, str]:
    check_name = "playwright.node"
    command = [*_playwright_node_command(), "--version"]
    try:
        proc = subprocess.run(
            command,
            cwd=str(web_dir.resolve()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as exc:
        return _preflight_check(check_name, "fail", f"node is not available for Playwright: {exc}")
    except subprocess.TimeoutExpired as exc:
        return _preflight_check(check_name, "fail", f"node --version timed out: {exc}")

    output = "\n".join(part for part in (proc.stdout or "", proc.stderr or "") if part)
    if proc.returncode != 0:
        return _preflight_check(check_name, "fail", f"node --version failed: {_compact_preflight_detail(output)}")
    version = _parse_node_major_version(output)
    if version is None:
        return _preflight_check(check_name, "fail", f"could not parse node version: {_compact_preflight_detail(output)}")
    minimum_major = 18
    if version < minimum_major:
        return _preflight_check(
            check_name,
            "fail",
            f"Node.js {output.strip()} is below Playwright minimum Node.js {minimum_major}",
        )
    return _preflight_check(check_name, "ok", output.strip())


def _parse_node_major_version(text: str) -> Optional[int]:
    match = re.search(r"\bv?(\d+)(?:\.\d+){0,2}\b", str(text or ""))
    return int(match.group(1)) if match else None


def _playwright_browser_install_checks(required_browsers: Sequence[str], *, web_dir: Path) -> List[Dict[str, str]]:
    checks: List[Dict[str, str]] = []
    for browser in required_browsers:
        check_name = f"playwright.browser.{browser}"
        command = _playwright_cli_command(web_dir=web_dir) + ["install", "--dry-run", browser]
        try:
            proc = subprocess.run(
                command,
                cwd=str(web_dir.resolve()),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as exc:
            checks.append(
                _preflight_check(
                    check_name,
                    "fail",
                    f"Playwright dry-run command is not available: {exc}",
                )
            )
            continue
        except subprocess.TimeoutExpired as exc:
            checks.append(_preflight_check(check_name, "fail", f"Playwright dry-run timed out: {exc}"))
            continue
        output = "\n".join(part for part in (proc.stdout or "", proc.stderr or "") if part)
        if proc.returncode != 0:
            checks.append(
                _preflight_check(
                    check_name,
                    "fail",
                    f"Playwright dry-run failed: {_compact_preflight_detail(output)}",
                )
            )
            continue
        locations = _playwright_install_locations(output)
        if not locations:
            checks.append(_preflight_check(check_name, "fail", "Playwright dry-run reported no install locations"))
            continue
        missing = [location for location in locations if not Path(location).exists()]
        if missing:
            checks.append(
                _preflight_check(
                    check_name,
                    "fail",
                    f"missing install locations: {', '.join(missing[:3])}",
                )
            )
            continue
        checks.append(_preflight_check(check_name, "ok", f"{browser}: {len(locations)} install locations present"))
    return checks


def _playwright_node_command() -> List[str]:
    node_bin = os.environ.get("PIPELINE_V2_NODE_BIN", "").strip()
    return [node_bin] if node_bin else ["node"]


def _playwright_cli_command(*, web_dir: Path) -> List[str]:
    playwright_cli = web_dir / "node_modules" / "playwright" / "cli.js"
    if playwright_cli.exists():
        return _playwright_node_command() + [str(playwright_cli.resolve())]
    return ["npx", "playwright"]


def _playwright_spec_bundle_checks(
    scenarios: Sequence[ExecutionScenario],
    *,
    web_dir: Path,
    config_path: str,
    env: Mapping[str, str],
) -> List[Dict[str, str]]:
    specs = [str(scenario.spec or scenario.test_cmd).strip() for scenario in scenarios]
    specs = [spec for spec in specs if spec]
    if not specs:
        return [_preflight_check("playwright.spec_bundle", "fail", "no executable Playwright specs selected")]

    aggregate = _run_playwright_spec_bundle_check(
        specs,
        check_name="playwright.spec_bundle",
        web_dir=web_dir,
        config_path=config_path,
        env=env,
    )
    if aggregate["status"] == "ok" or len(specs) == 1:
        return [aggregate]

    per_spec_checks: List[Dict[str, str]] = []
    loadable_specs: List[str] = []
    blocked_specs: List[str] = []
    for spec in specs:
        check = _run_playwright_spec_bundle_check(
            [spec],
            check_name=f"playwright.spec_bundle.{spec}",
            web_dir=web_dir,
            config_path=config_path,
            env=env,
        )
        per_spec_checks.append(check)
        if check["status"] == "ok":
            loadable_specs.append(spec)
        else:
            blocked_specs.append(spec)

    aggregate_detail = (
        "Playwright test --list failed for selected set; "
        f"loadable specs: {_format_spec_list(loadable_specs)}; "
        f"blocked specs: {_format_spec_list(blocked_specs)}; "
        f"first error: {aggregate['detail']}"
    )
    return [_preflight_check("playwright.spec_bundle", "fail", aggregate_detail), *per_spec_checks]


def _run_playwright_spec_bundle_check(
    specs: Sequence[str],
    *,
    check_name: str,
    web_dir: Path,
    config_path: str,
    env: Mapping[str, str],
) -> Dict[str, str]:
    command = _playwright_cli_command(web_dir=web_dir) + [
        "test",
        f"--config={config_path}",
        "--list",
        *specs,
    ]
    run_env = os.environ.copy()
    run_env.update({str(key): str(value) for key, value in env.items()})
    try:
        proc = subprocess.run(
            command,
            cwd=str(web_dir.resolve()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            check=False,
            timeout=60,
            env=run_env,
        )
    except FileNotFoundError as exc:
        return _preflight_check(check_name, "fail", f"Playwright test --list is not available: {exc}")
    except subprocess.TimeoutExpired as exc:
        return _preflight_check(check_name, "fail", f"Playwright test --list timed out: {exc}")
    output = "\n".join(part for part in (proc.stdout or "", proc.stderr or "") if part)
    if proc.returncode != 0:
        return _preflight_check(
            check_name,
            "fail",
            f"Playwright test --list failed: {_compact_preflight_detail(output)}",
        )
    label = "spec" if len(specs) == 1 else "specs"
    return _preflight_check(check_name, "ok", f"{len(specs)} selected {label} loaded")


def _format_spec_list(specs: Sequence[str]) -> str:
    return ", ".join(specs) if specs else "<none>"


def _playwright_install_locations(text: str) -> List[str]:
    locations: List[str] = []
    for line in text.splitlines():
        match = re.search(r"Install location:\s*(.+?)\s*$", line)
        if match:
            locations.append(match.group(1).strip())
    return locations


def _compact_preflight_detail(text: str, *, limit: int = 500) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    compact = " ".join(lines)
    return compact[:limit]


def _preflight_exception_check_name(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("adapter.product"):
        return "adapter.product"
    if message.startswith("adapter.playwright."):
        token = message.split(maxsplit=1)[0]
        token = token.split("[", 1)[0]
        return token
    return "adapter.semantic_map"


def _preflight_check(name: str, status: str, detail: str) -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _preflight_result(checks: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    failed = [check for check in checks if check.get("status") != "ok"]
    return {
        "status": "ok" if not failed else "not-ready",
        "failed_count": len(failed),
        "checks": [dict(check) for check in checks],
    }


def build_sample_dry_run(
    ledger: Ledger,
    *,
    queue_path: Path,
    brief_path: Path,
    model_router: Optional[ModelRouter] = None,
) -> SampleDryRun:
    """Build the M4 offline sample with one stable L2 failure."""

    entry = {
        "id": "example.skill.change_mode.intent-to-mode",
        "expected_terminal_state": {
            "assertion": "current_mode == target_mode",
            "observable": "structured terminal mode",
            "assertion_type": "mode_state",
            "machine_check": {"field": "current_mode", "op": "eq", "value_from": "target_mode"},
        },
    }
    scenario = ExecutionScenario(
        scenario_id="guide:change-mode",
        step_id="assert-mode",
        test_cmd="mock:test-mode",
        semantic_map_entry=entry,
    )
    failure = ExecutionResult(
        observed_state={"current_mode": "exploration", "target_mode": "write_functions"},
        raw_failure="mode stayed exploration",
        exit_code=1,
    )
    executor = MockExecutor({scenario.scenario_id: [failure, failure, failure, failure]})
    sentinel = ExecutionScenario(
        scenario_id="sentinel:stable",
        step_id="sentinel",
        test_cmd="mock:sentinel",
        is_sentinel=True,
    )
    return SampleDryRun(
        orchestrator=PipelineV2Orchestrator(
            ledger=ledger,
            executor=executor,
            queue_path=queue_path,
            brief_path=brief_path,
            model_router=model_router,
        ),
        sentinels=[sentinel],
        scenarios=[scenario],
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="pipeline-v2 orchestrator dry-run")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--brief", type=Path)
    parser.add_argument("--batch-id", default="dry-run-001")
    parser.add_argument("--product-version", default="product@offline")
    parser.add_argument("--environment-digest", default="offline")
    parser.add_argument("--executor", choices=("mock", "playwright", "http", "l1-fuzz"), default="mock")
    parser.add_argument("--adapter", type=Path, help="Product adapter config for adapter-driven executors")
    parser.add_argument("--semantic-map", type=Path, help="Semantic map YAML; defaults to adapters/<product>/semantic-map.yaml")
    parser.add_argument("--product-cwd", type=Path, default=Path("."), help="Readonly product checkout for real executor runs")
    parser.add_argument("--dry-run", action="store_true", help="Use executor fixture mode where supported")
    parser.add_argument("--report", type=Path, help="Playwright JSON reporter fixture for --dry-run")
    parser.add_argument("--output-dir", type=Path, help="Playwright output dir; must be outside product_cwd")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--preflight", action="store_true", help="Run read-only adapter preflight checks and exit")
    parser.add_argument("--hosts-file", type=Path, default=Path("/etc/hosts"), help="Hosts file for --preflight")
    parser.add_argument("--suggestions", type=Path, help="L4 建议通道输出（与 bug queue 隔离）")
    parser.add_argument("--target-plan", type=Path, help="change-impact target plan used to filter executable scenarios")
    parser.add_argument(
        "--enable-model-routing",
        action="store_true",
        help="Explicitly enable adapter model routing; default is disabled",
    )
    parser.add_argument(
        "--model-budget-overlay",
        type=Path,
        help="Runtime-only JSON budget required for model routing opt-in",
    )
    args = parser.parse_args(argv)
    if args.executor == "playwright" and not args.adapter:
        parser.error("--executor playwright requires --adapter")
    if args.executor == "http" and not args.adapter:
        parser.error("--executor http requires --adapter")
    if args.executor == "l1-fuzz" and not args.adapter:
        parser.error("--executor l1-fuzz requires --adapter")
    if args.enable_model_routing and not args.adapter:
        parser.error("--enable-model-routing requires --adapter")
    if args.model_budget_overlay and not args.enable_model_routing:
        parser.error("--model-budget-overlay requires --enable-model-routing")
    if args.report and not args.dry_run:
        parser.error("--report requires --dry-run")
    if args.dry_run and args.executor == "playwright" and not args.report:
        parser.error("--executor playwright --dry-run requires --report")
    if args.preflight:
        if args.enable_model_routing:
            parser.error("--preflight cannot enable model routing")
        if args.executor == "playwright":
            result = run_adapter_playwright_preflight(
                adapter_path=args.adapter,
                semantic_map_path=args.semantic_map,
                target_plan_path=args.target_plan,
                product_cwd=args.product_cwd,
                output_dir=args.output_dir,
                hosts_file=args.hosts_file,
            )
        elif args.executor == "http":
            result = run_adapter_http_preflight(
                adapter_path=args.adapter,
                semantic_map_path=args.semantic_map,
                target_plan_path=args.target_plan,
            )
        else:
            parser.error("--preflight requires --executor playwright or http")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] == "ok" else 1
    if not args.ledger or not args.queue or not args.brief:
        parser.error("--ledger, --queue, and --brief are required unless --preflight is set")

    playwright_executor: Optional[ExecutorProtocol] = None
    playwright_scenarios: Sequence[ExecutionScenario] = ()
    http_executor: Optional[ExecutorProtocol] = None
    http_scenarios: Sequence[ExecutionScenario] = ()
    l1_executor: Optional[ExecutorProtocol] = None
    l1_scenarios: Sequence[ExecutionScenario] = ()
    if args.executor == "l1-fuzz":
        try:
            l1_executor, l1_scenarios = build_adapter_l1_fuzz_run(
                adapter_path=args.adapter,
                semantic_map_path=args.semantic_map,
                target_plan_path=args.target_plan,
            )
        except SchemaError as exc:
            print(json.dumps({"status": "adapter-error", "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
    if args.executor == "http":
        try:
            http_executor, http_scenarios = build_adapter_http_run(
                adapter_path=args.adapter,
                semantic_map_path=args.semantic_map,
                target_plan_path=args.target_plan,
            )
        except SchemaError as exc:
            print(
                json.dumps(
                    {"status": "adapter-error", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
    if args.executor == "playwright":
        try:
            playwright_executor, playwright_scenarios = build_adapter_playwright_run(
                adapter_path=args.adapter,
                semantic_map_path=args.semantic_map,
                target_plan_path=args.target_plan,
                product_cwd=args.product_cwd,
                dry_run_report_path=args.report if args.dry_run else None,
                output_dir=args.output_dir,
                timeout_sec=args.timeout_sec,
            )
        except SchemaError as exc:
            print(
                json.dumps(
                    {"status": "adapter-error", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1

    semantic_map_for_suggestions = _load_optional_semantic_map(
        adapter_path=args.adapter,
        semantic_map_path=args.semantic_map,
    )
    ledger = Ledger(args.ledger)
    try:
        ledger.init_schema()
        model_router = None
        if args.enable_model_routing:
            try:
                model_router = build_opt_in_model_router(
                    adapter=_load_json_mapping(args.adapter),
                    ledger=ledger,
                    enable_requested=True,
                    budget_overlay=(
                        _load_json_mapping(args.model_budget_overlay)
                        if args.model_budget_overlay
                        else None
                    ),
                )
            except (SchemaError, ModelRoutingError) as exc:
                print(
                    json.dumps(
                        {"status": "adapter-error", "error": str(exc)},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 1
        if args.executor == "l1-fuzz":
            assert l1_executor is not None
            orchestrator = PipelineV2Orchestrator(
                ledger=ledger,
                executor=l1_executor,
                queue_path=args.queue,
                brief_path=args.brief,
                suggestion_path=args.suggestions,
                semantic_map=semantic_map_for_suggestions,
                model_router=model_router,
            )
            result = orchestrator.run_batch(
                batch_id=args.batch_id,
                product_version=args.product_version,
                environment_digest=args.environment_digest,
                sentinel_scenarios=[],
                scenarios=l1_scenarios,
            )
        elif args.executor == "playwright":
            assert playwright_executor is not None
            orchestrator = PipelineV2Orchestrator(
                ledger=ledger,
                executor=playwright_executor,
                queue_path=args.queue,
                brief_path=args.brief,
                suggestion_path=args.suggestions,
                semantic_map=semantic_map_for_suggestions,
                model_router=model_router,
            )
            result = orchestrator.run_batch(
                batch_id=args.batch_id,
                product_version=args.product_version,
                environment_digest=args.environment_digest,
                sentinel_scenarios=[],
                scenarios=playwright_scenarios,
            )
        elif args.executor == "http":
            assert http_executor is not None
            orchestrator = PipelineV2Orchestrator(
                ledger=ledger,
                executor=http_executor,
                queue_path=args.queue,
                brief_path=args.brief,
                suggestion_path=args.suggestions,
                semantic_map=semantic_map_for_suggestions,
                model_router=model_router,
            )
            result = orchestrator.run_batch(
                batch_id=args.batch_id,
                product_version=args.product_version,
                environment_digest=args.environment_digest,
                sentinel_scenarios=[],
                scenarios=http_scenarios,
            )
        else:
            sample = build_sample_dry_run(
                ledger,
                queue_path=args.queue,
                brief_path=args.brief,
                model_router=model_router,
            )
            result = sample.orchestrator.run_batch(
                batch_id=args.batch_id,
                product_version=args.product_version,
                environment_digest=args.environment_digest,
                sentinel_scenarios=sample.sentinels,
                scenarios=sample.scenarios,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
