import tempfile
import sys
import unittest
from pathlib import Path

from pipeline_v2.ledger import Ledger
from pipeline_v2.model_routing import (
    ModelRouter,
    ModelRoutingError,
    ModelRoutingPolicy,
    SubprocessModelRunner,
    build_opt_in_model_router,
)
from pipeline_v2.schema import FailureSignal


def budget_config(**overrides):
    budget = {
        "max_calls_per_batch": 5,
        "max_input_tokens_per_batch": 1000,
        "max_output_tokens_per_batch": 500,
        "max_estimated_cost_per_batch": 1.0,
        "per_call_timeout_sec": 5,
        "consecutive_error_stop": 2,
        "telemetry_required": True,
    }
    budget.update(overrides)
    return budget


def policy_config(*, enabled=True, budget=None):
    return {
        "enabled": enabled,
        "allowed_triggers": [
            "coverage-gap",
            "machine-verified-failure",
            "machine-evidence-conflict",
            "security-risk",
            "architecture-risk",
        ],
        "roles": {
            "cheap": {"model_id": "cheap-test", "prompt_version": "coverage-v1"},
            "standard": {"model_id": "standard-test", "prompt_version": "analysis-v1"},
            "strong": {"model_id": "strong-test", "prompt_version": "veto-v1"},
        },
        "budget": budget or budget_config(),
    }


def signal():
    return FailureSignal(
        scenario_id="scenario",
        step_id="step",
        failure_type="assertion",
        oracle_level="L2",
        llm_involvement="clue_source",
        expected_state={"ok": True},
        actual_state={"ok": False},
        semantic_map_entry_id="sample.skill.contract",
    )


class ModelRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init_schema()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_disabled_policy_never_calls_runner(self):
        calls = []
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config(enabled=False)),
            runner=lambda route, payload: calls.append((route, payload)),
            ledger=self.ledger,
        )
        router.begin_batch("b-disabled")

        result = router.review_verified_candidate({"fingerprint": "fp"}, signal())

        self.assertFalse(result.vetoed)
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.list_model_calls_for_batch("b-disabled"), [])

    def test_adapter_routing_requires_explicit_opt_in(self):
        adapter = {"model_routing": policy_config(enabled=False)}
        runner = lambda _route, _payload: {"decision": "allow", "usage": {}}

        self.assertIsNone(
            build_opt_in_model_router(
                adapter=adapter,
                ledger=self.ledger,
                enable_requested=False,
                runner=runner,
            )
        )
        enabled = build_opt_in_model_router(
            adapter=adapter,
            ledger=self.ledger,
            enable_requested=True,
            budget_overlay=budget_config(),
            runner=runner,
        )
        self.assertIsNotNone(enabled)
        self.assertTrue(enabled.policy.enabled)

    def test_opt_in_rejects_unconfigured_model_routes(self):
        config = policy_config(enabled=False)
        config["roles"]["strong"]["model_id"] = "unconfigured"

        with self.assertRaisesRegex(ModelRoutingError, "unconfigured"):
            build_opt_in_model_router(
                adapter={"model_routing": config},
                ledger=self.ledger,
                enable_requested=True,
                budget_overlay=budget_config(),
                runner=lambda _route, _payload: {},
            )

    def test_opt_in_requires_runtime_budget_overlay(self):
        config = policy_config(enabled=False)
        config.pop("budget")

        with self.assertRaisesRegex(ModelRoutingError, "budget overlay"):
            build_opt_in_model_router(
                adapter={"model_routing": config},
                ledger=self.ledger,
                enable_requested=True,
                runner=lambda _route, _payload: {},
            )

    def test_escalation_rejects_model_confidence_and_unverified_evidence(self):
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=lambda _route, _payload: {},
            ledger=self.ledger,
        )
        router.begin_batch("b-reject")

        with self.assertRaisesRegex(ModelRoutingError, "machine trigger"):
            router.invoke(
                role="strong",
                trigger="model-confidence-low",
                payload={},
                machine_verification_passed=True,
            )
        with self.assertRaisesRegex(ModelRoutingError, "verification"):
            router.invoke(
                role="strong",
                trigger="machine-verified-failure",
                payload={},
                machine_verification_passed=False,
            )

    def test_model_cannot_confirm_or_promote_bug(self):
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=lambda _route, _payload: {
                "decision": "confirmed",
                "reason": "model says this is definitely a bug",
                "usage": {},
            },
            ledger=self.ledger,
        )
        router.begin_batch("b-confirm")

        with self.assertRaisesRegex(ModelRoutingError, "decision"):
            router.invoke(
                role="strong",
                trigger="machine-verified-failure",
                payload={},
                machine_verification_passed=True,
                fingerprint="fp-confirm",
            )

        result = router.review_verified_candidate({"fingerprint": "fp-confirm"}, signal())
        self.assertTrue(result.vetoed)
        self.assertIn("model-routing-blocked", result.reason)

        self.assertEqual(self.ledger.list_model_calls_for_batch("b-confirm"), [])

    def test_response_authority_fields_fail_closed_before_ledger(self):
        for label, extra in (
            ("confirmed", {"confirmed": True}),
            ("promote", {"promote": True}),
            ("publish", {"metadata": {"publish": True}}),
            ("card_id", {"usage_extra": {"card_id": "BUG-1"}}),
        ):
            with self.subTest(label=label):
                response = {
                    "decision": "allow",
                    "reason": "shadow advice only",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "estimated_cost": 0,
                        "duration_ms": 1,
                    },
                    "validation_result": "schema-valid",
                    "prompt_version": "coverage-v1",
                    **extra,
                }
                router = ModelRouter(
                    policy=ModelRoutingPolicy.from_mapping(policy_config()),
                    runner=lambda _route, _payload, response=response: response,
                    ledger=self.ledger,
                )
                batch_id = f"b-authority-{label}"
                router.begin_batch(batch_id)

                with self.assertRaisesRegex(ModelRoutingError, label):
                    router.invoke(
                        role="cheap",
                        trigger="coverage-gap",
                        payload={},
                        machine_verification_passed=False,
                    )
                self.assertEqual(self.ledger.list_model_calls_for_batch(batch_id), [])

    def test_runner_failure_is_suppressed_and_fails_closed(self):
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=lambda _route, _payload: (_ for _ in ()).throw(RuntimeError("secret-value")),
            ledger=self.ledger,
        )
        router.begin_batch("b-runner-fail")

        result = router.review_verified_candidate({"fingerprint": "fp-runner-fail"}, signal())

        self.assertTrue(result.vetoed)
        self.assertIn("RuntimeError", result.reason)
        self.assertNotIn("secret-value", result.reason)
        self.assertEqual(self.ledger.list_model_calls_for_batch("b-runner-fail"), [])

    def test_verified_strong_veto_records_complete_telemetry(self):
        def runner(route, payload):
            self.assertEqual(route.role, "strong")
            self.assertEqual(payload["trigger"], "machine-verified-failure")
            return {
                "decision": "veto",
                "reason": "known environment drift",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 15,
                    "estimated_cost": 0.0042,
                    "duration_ms": 350,
                },
                "validation_result": "schema-valid",
                "prompt_version": "veto-v1",
            }

        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=runner,
            ledger=self.ledger,
        )
        router.begin_batch("b-veto")

        result = router.review_verified_candidate({"fingerprint": "fp-veto"}, signal())

        self.assertTrue(result.vetoed)
        self.assertIn("known environment drift", result.reason)
        calls = self.ledger.list_model_calls_for_batch("b-veto")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["role"], "strong")
        self.assertEqual(call["model_id"], "strong-test")
        self.assertEqual(call["prompt_version"], "veto-v1")
        self.assertEqual(call["input_tokens"], 120)
        self.assertEqual(call["output_tokens"], 15)
        self.assertEqual(call["estimated_cost"], 0.0042)
        self.assertEqual(call["duration_ms"], 350)
        self.assertEqual(call["validation_result"], "schema-valid")
        self.assertEqual(call["escalated_from"], "machine-verification")

        token_cost, duration = router.batch_metrics("b-veto")
        self.assertEqual(token_cost["model_routing"]["input_tokens"], 120)
        self.assertEqual(token_cost["model_routing"]["estimated_cost"], 0.0042)
        self.assertEqual(duration["model_routing"]["duration_ms"], 350)

    def test_request_more_evidence_blocks_publication(self):
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=lambda _route, _payload: {
                "decision": "request-more-evidence",
                "reason": "need a second independent reproduction",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost": 0,
                    "duration_ms": 20,
                },
                "validation_result": "schema-valid",
                "prompt_version": "veto-v1",
            },
            ledger=self.ledger,
        )
        router.begin_batch("b-more")

        result = router.review_verified_candidate({"fingerprint": "fp-more"}, signal())

        self.assertTrue(result.vetoed)
        self.assertIn("request-more-evidence", result.reason)

    def test_call_budget_stops_before_invoking_runner_again(self):
        calls = []

        def runner(_route, _payload):
            calls.append("called")
            return {
                "decision": "allow",
                "reason": "shadow only",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost": 0.01,
                    "duration_ms": 20,
                },
                "validation_result": "schema-valid",
                "prompt_version": "coverage-v1",
            }

        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(
                policy_config(budget=budget_config(max_calls_per_batch=1))
            ),
            runner=runner,
            ledger=self.ledger,
        )
        router.begin_batch("b-call-budget")
        router.invoke(
            role="cheap",
            trigger="coverage-gap",
            payload={},
            machine_verification_passed=False,
        )

        with self.assertRaisesRegex(ModelRoutingError, "call budget"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )
        self.assertEqual(calls, ["called"])

    def test_usage_over_budget_is_recorded_then_circuit_stops(self):
        calls = []

        def runner(_route, _payload):
            calls.append("called")
            return {
                "decision": "allow",
                "reason": "shadow only",
                "usage": {
                    "input_tokens": 101,
                    "output_tokens": 5,
                    "estimated_cost": 0.01,
                    "duration_ms": 20,
                },
                "validation_result": "schema-valid",
                "prompt_version": "coverage-v1",
            }

        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(
                policy_config(budget=budget_config(max_input_tokens_per_batch=100))
            ),
            runner=runner,
            ledger=self.ledger,
        )
        router.begin_batch("b-token-budget")

        with self.assertRaisesRegex(ModelRoutingError, "input token budget"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )
        self.assertEqual(len(self.ledger.list_model_calls_for_batch("b-token-budget")), 1)
        with self.assertRaisesRegex(ModelRoutingError, "budget circuit"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )
        self.assertEqual(calls, ["called"])

    def test_missing_telemetry_and_prompt_version_fail_closed(self):
        responses = iter(
            [
                {"decision": "allow", "validation_result": "schema-valid"},
                {
                    "decision": "allow",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "estimated_cost": 0,
                        "duration_ms": 1,
                    },
                    "validation_result": "schema-valid",
                },
            ]
        )
        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(policy_config()),
            runner=lambda _route, _payload: next(responses),
            ledger=self.ledger,
        )
        router.begin_batch("b-telemetry")

        with self.assertRaisesRegex(ModelRoutingError, "usage telemetry"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )
        with self.assertRaisesRegex(ModelRoutingError, "prompt_version"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )

    def test_budget_rejects_fractional_integers_and_nonfinite_numbers(self):
        for field, value in (
            ("max_calls_per_batch", 1.5),
            ("max_input_tokens_per_batch", 10.5),
            ("max_estimated_cost_per_batch", float("nan")),
            ("max_estimated_cost_per_batch", float("inf")),
            ("per_call_timeout_sec", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                config = budget_config(**{field: value})
                with self.assertRaisesRegex(ModelRoutingError, field):
                    ModelRoutingPolicy.from_mapping(
                        policy_config(budget=config)
                    )

    def test_usage_rejects_fractional_or_nonfinite_telemetry(self):
        for field, value in (
            ("input_tokens", 1.5),
            ("output_tokens", 2.5),
            ("duration_ms", 3.5),
            ("estimated_cost", float("nan")),
            ("estimated_cost", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                usage = {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "estimated_cost": 0.01,
                    "duration_ms": 1,
                }
                usage[field] = value
                router = ModelRouter(
                    policy=ModelRoutingPolicy.from_mapping(policy_config()),
                    runner=lambda _route, _payload, usage=usage: {
                        "decision": "allow",
                        "usage": usage,
                        "validation_result": "schema-valid",
                        "prompt_version": "coverage-v1",
                    },
                    ledger=self.ledger,
                )
                batch_id = f"b-invalid-{field}-{repr(value)}"
                router.begin_batch(batch_id)

                with self.assertRaisesRegex(ModelRoutingError, field):
                    router.invoke(
                        role="cheap",
                        trigger="coverage-gap",
                        payload={},
                        machine_verification_passed=False,
                    )
                self.assertEqual(self.ledger.list_model_calls_for_batch(batch_id), [])

    def test_consecutive_provider_errors_open_circuit(self):
        calls = []

        def runner(_route, _payload):
            calls.append("called")
            raise RuntimeError("provider unavailable")

        router = ModelRouter(
            policy=ModelRoutingPolicy.from_mapping(
                policy_config(budget=budget_config(consecutive_error_stop=2))
            ),
            runner=runner,
            ledger=self.ledger,
        )
        router.begin_batch("b-errors")

        for _ in range(2):
            with self.assertRaisesRegex(ModelRoutingError, "runner failed"):
                router.invoke(
                    role="cheap",
                    trigger="coverage-gap",
                    payload={},
                    machine_verification_passed=False,
                )
        with self.assertRaisesRegex(ModelRoutingError, "error circuit"):
            router.invoke(
                role="cheap",
                trigger="coverage-gap",
                payload={},
                machine_verification_passed=False,
            )
        self.assertEqual(calls, ["called", "called"])

    def test_subprocess_runner_enforces_runtime_timeout(self):
        runner = SubprocessModelRunner(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            timeout_sec=0.01,
        )
        route = ModelRoutingPolicy.from_mapping(policy_config()).routes["cheap"]

        with self.assertRaisesRegex(ModelRoutingError, "timed out"):
            runner(route, {})

    def test_subprocess_runner_rejects_explicit_shell_wrapper(self):
        for command in (
            ["/bin/zsh", "-c", "provider --json"],
            ["/usr/bin/env", "MODEL_ROUTE=approved", "bash", "-c", "provider --json"],
            ["/usr/bin/env", "-S", "bash -c 'provider --json'"],
            ["busybox", "sh", "-c", "provider --json"],
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ModelRoutingError, "shell"):
                    SubprocessModelRunner(command, timeout_sec=1)


if __name__ == "__main__":
    unittest.main()
