import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import json

from pipeline_v2.orchestrator import ExecutionScenario
from pipeline_v2 import playwright_executor
from pipeline_v2.playwright_executor import PlaywrightExecutionConfig, PlaywrightExecutor


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "pipeline_v2" / "playwright"


class PlaywrightExecutorTest(unittest.TestCase):
    def test_environment_patterns_do_not_hardcode_adapter_env_names(self):
        patterns = [pattern.pattern for pattern in playwright_executor.ENVIRONMENT_PATTERNS]

        self.assertFalse(any("SAMPLE_COOKIE" in pattern for pattern in patterns))

    def test_dry_run_fixture_converts_failed_json_report_to_execution_result(self):
        executor = PlaywrightExecutor(
            PlaywrightExecutionConfig(
                product_cwd=Path("/example/product"),
                web_subdir="web",
                config_path="playwright.go-integration.config.ts",
                dry_run_report_path=FIXTURES / "failure-report.json",
            )
        )

        result = executor.run(
            test_cmd="manage-context.spec.ts",
            scenario=ExecutionScenario(
                scenario_id="spec:manage-context",
                step_id="playwright",
                test_cmd="manage-context.spec.ts",
                spec="manage-context.spec.ts",
            ),
        )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertEqual(result.observed_state["spec"], "manage-context.spec.ts")
        self.assertEqual(result.observed_state["passed"], False)
        self.assertEqual(result.observed_state["duration"], 1284)
        self.assertEqual(len(result.observed_state["failed_assertions"]), 1)
        self.assertIn("manage_context", result.raw_failure)
        self.assertIn("manage-context.spec.ts", result.stdout)

    def test_top_level_playwright_errors_are_environment_blockers(self):
        report = {
            "errors": [
                {
                    "message": (
                        "SyntaxError: The requested module '../sample-web/src/utils/formatTokens' "
                        "does not provide an export named 'formatTokens'"
                    ),
                    "stack": (
                        "SyntaxError: The requested module '../sample-web/src/utils/formatTokens' "
                        "does not provide an export named 'formatTokens'"
                    ),
                },
                {
                    "message": (
                        "Error: No tests found.\n"
                        "Make sure that arguments are regular expressions matching test files."
                    )
                },
            ],
            "stats": {"duration": 15, "expected": 0, "skipped": 0, "unexpected": 0, "flaky": 0},
            "suites": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            executor = PlaywrightExecutor(
                PlaywrightExecutionConfig(
                    product_cwd=Path("/example/product"),
                    web_subdir="web",
                    config_path="playwright.go-integration.config.ts",
                    dry_run_report_path=report_path,
                )
            )

            result = executor.run(
                test_cmd="version-consistency.spec.ts",
                scenario=ExecutionScenario(
                    scenario_id="spec:version",
                    step_id="playwright",
                    test_cmd="version-consistency.spec.ts",
                    spec="version-consistency.spec.ts",
                ),
            )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertIn("formatTokens", result.raw_failure)
        self.assertNotIn("No tests found", result.raw_failure)
        self.assertEqual(result.observed_state["environment_unavailable"], True)
        self.assertIn("formatTokens", result.observed_state["environment_reason"])

    def test_environment_top_level_error_does_not_mask_real_suite_failure(self):
        report = {
            "errors": [
                {
                    "message": (
                        "Error: No tests found.\n"
                        "Make sure that arguments are regular expressions matching test files."
                    )
                }
            ],
            "stats": {"duration": 1284, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "title": "tests/integration/manage-context.spec.ts",
                    "file": "tests/integration/manage-context.spec.ts",
                    "specs": [
                        {
                            "title": "agent browses project files and cleans context",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "agent browses project files and cleans context",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 1284,
                                            "error": {
                                                "message": (
                                                    "Error: agent must call manage_context at least once "
                                                    "to clean up context"
                                                ),
                                                "stack": (
                                                    "Error: agent must call manage_context at least once\n"
                                                    "    at tests/integration/manage-context.spec.ts:96:11"
                                                ),
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            executor = PlaywrightExecutor(
                PlaywrightExecutionConfig(
                    product_cwd=Path("/example/product"),
                    web_subdir="web",
                    config_path="playwright.go-integration.config.ts",
                    dry_run_report_path=report_path,
                )
            )

            result = executor.run(
                test_cmd="manage-context.spec.ts",
                scenario=ExecutionScenario(
                    scenario_id="spec:manage-context",
                    step_id="playwright",
                    test_cmd="manage-context.spec.ts",
                    spec="manage-context.spec.ts",
                ),
            )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertIn("manage_context", result.raw_failure)
        self.assertNotIn("No tests found", result.raw_failure)

    def test_dry_run_fixture_converts_passed_json_report_to_execution_result(self):
        executor = PlaywrightExecutor(
            PlaywrightExecutionConfig(
                product_cwd=Path("/example/product"),
                web_subdir="web",
                config_path="playwright.go-integration.config.ts",
                dry_run_report_path=FIXTURES / "pass-report.json",
            )
        )

        result = executor.run(
            test_cmd="environment-skill.spec.ts",
            scenario=ExecutionScenario(
                scenario_id="spec:environment-skill",
                step_id="playwright",
                test_cmd="environment-skill.spec.ts",
                spec="environment-skill.spec.ts",
            ),
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.observed_state["passed"], True)
        self.assertEqual(result.observed_state["failed_assertions"], [])
        self.assertEqual(result.raw_failure, "")

    def test_real_run_uses_injected_paths_without_shell_or_product_name_assumptions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "custom-web"
            web_dir.mkdir(parents=True)
            stdout = (FIXTURES / "failure-report.json").read_text(encoding="utf-8")
            completed = mock.Mock(returncode=1, stdout=stdout, stderr="raw stderr")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", return_value=completed) as run:
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="custom-web",
                        config_path="custom.config.ts",
                        output_dir=Path(tmpdir) / "playwright-output",
                    )
                )
                result = executor.run(
                    test_cmd="manage-context.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:manage-context",
                        step_id="playwright",
                        test_cmd="manage-context.spec.ts",
                        spec="manage-context.spec.ts",
                    ),
                )

            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual(
                args[0],
                [
                    "npx",
                    "playwright",
                    "test",
                    "--config=custom.config.ts",
                    "--reporter=json",
                    "--timeout=120000",
                    f"--output={(Path(tmpdir) / 'playwright-output').resolve()}",
                    "manage-context.spec.ts",
                ],
            )
            self.assertEqual(kwargs["cwd"], str(web_dir.resolve()))
            self.assertFalse(kwargs["shell"])
            self.assertEqual(result.stderr, "raw stderr")
            self.assertEqual(result.exit_code, 1)

    def test_output_dir_inside_product_repo_is_rejected_to_preserve_readonly_product(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            web_dir.mkdir(parents=True)
            with mock.patch("pipeline_v2.playwright_executor.subprocess.run") as run:
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=product_cwd / "test-results",
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        run.assert_not_called()
        self.assertTrue(result.environment_unavailable)
        self.assertIn("outside product_cwd", result.raw_failure)

    def test_relative_output_dir_is_resolved_before_running_from_product_web(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "automation"
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            workspace.mkdir()
            web_dir.mkdir(parents=True)
            completed = mock.Mock(
                returncode=0,
                stdout=(FIXTURES / "pass-report.json").read_text(encoding="utf-8"),
                stderr="",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(workspace)
                with mock.patch("pipeline_v2.playwright_executor.subprocess.run", return_value=completed) as run:
                    executor = PlaywrightExecutor(
                        PlaywrightExecutionConfig(
                            product_cwd=product_cwd,
                            web_subdir="web",
                            config_path="playwright.go-integration.config.ts",
                            output_dir=Path(".tmp/pv2/out"),
                        )
                    )
                    executor.run(
                        test_cmd="environment-skill.spec.ts",
                        scenario=ExecutionScenario(
                            scenario_id="spec:environment-skill",
                            step_id="playwright",
                            test_cmd="environment-skill.spec.ts",
                            spec="environment-skill.spec.ts",
                        ),
                    )
            finally:
                os.chdir(previous_cwd)

        run.assert_called_once()
        args, kwargs = run.call_args
        expected_output_dir = (workspace / ".tmp/pv2/out").resolve()
        self.assertIn(f"--output={expected_output_dir}", args[0])
        self.assertEqual(kwargs["cwd"], str(web_dir.resolve()))

    def test_web_subdir_must_stay_inside_injected_product_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            outside_web = Path(tmpdir) / "outside-web"
            outside_web.mkdir(parents=True)
            with mock.patch("pipeline_v2.playwright_executor.subprocess.run") as run:
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="../outside-web",
                        config_path="playwright.go-integration.config.ts",
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        run.assert_not_called()
        self.assertTrue(result.environment_unavailable)
        self.assertIn("web_subdir", result.raw_failure)

    def test_config_path_must_be_relative_to_web_subdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            web_dir.mkdir(parents=True)
            with mock.patch("pipeline_v2.playwright_executor.subprocess.run") as run:
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="../playwright.config.ts",
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        run.assert_not_called()
        self.assertTrue(result.environment_unavailable)
        self.assertIn("config_path", result.raw_failure)

    def test_non_json_environment_failure_is_classified_as_environment_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            web_dir.mkdir(parents=True)
            completed = mock.Mock(
                returncode=1,
                stdout="",
                stderr="Error: Timed out waiting 30000ms from config.webServer.",
            )

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", return_value=completed):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertEqual(result.observed_state["environment_unavailable"], True)
        self.assertIn("webServer", result.raw_failure)

    def test_node_version_mismatch_is_environment_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            web_dir.mkdir(parents=True)
            completed = mock.Mock(
                returncode=1,
                stdout="",
                stderr=(
                    "You are running Node.js 16.20.2. "
                    "Playwright requires Node.js 18 or higher. Please update your version of Node.js."
                ),
            )

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", return_value=completed):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertIn("Node.js 18", result.raw_failure)

    def test_missing_required_env_is_environment_unavailable_without_running_subprocess(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            web_dir.mkdir(parents=True)
            with mock.patch("pipeline_v2.playwright_executor.subprocess.run") as run:
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        required_env=["SAMPLE_COOKIE"],
                        env={"SAMPLE_COOKIE": ""},
                    )
                )
                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        run.assert_not_called()
        self.assertTrue(result.environment_unavailable)
        self.assertIn("SAMPLE_COOKIE", result.raw_failure)

    def test_all_skipped_json_report_is_environment_unavailable_not_pass(self):
        executor = PlaywrightExecutor(
            PlaywrightExecutionConfig(
                product_cwd=Path("/example/product"),
                web_subdir="web",
                config_path="playwright.go-integration.config.ts",
                dry_run_report_path=FIXTURES / "skipped-report.json",
            )
        )

        result = executor.run(
            test_cmd="clarify-question.spec.ts",
            scenario=ExecutionScenario(
                scenario_id="spec:clarify-question",
                step_id="playwright",
                test_cmd="clarify-question.spec.ts",
                spec="clarify-question.spec.ts",
            ),
        )

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertIn("skipped", result.raw_failure)

    def test_idaas_redirect_in_json_report_is_environment_unavailable(self):
        report = {
            "stats": {"duration": 30000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "environment-skill.spec.ts",
                    "specs": [
                        {
                            "title": "/api/skills exposes every environment tool",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "/api/skills exposes every environment tool",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 12,
                                            "error": {
                                                "message": "Error: expect(received).toBeTruthy() Received: false",
                                                "location": {
                                                    "file": "tests/integration/environment-skill.spec.ts",
                                                    "line": 21,
                                                    "column": 26,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "title": "Skills panel renders a single active Environment row",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "Skills panel renders a single active Environment row",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "timedOut",
                                            "duration": 30000,
                                            "error": {
                                                "message": (
                                                    "locator.click: Test timeout of 30000ms exceeded.\n"
                                                    "Call log:\n"
                                                    "  - waiting for https://idaas.example.test/login?"
                                                    "redirect=http%3A%2F%2Fsample.example.test%3A8000%2F "
                                                    "navigation to finish"
                                                ),
                                                "location": {
                                                    "file": "tests/integration/environment-skill.spec.ts",
                                                    "line": 33,
                                                    "column": 53,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "idaas-report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            executor = PlaywrightExecutor(
                PlaywrightExecutionConfig(
                    product_cwd=Path("/example/product"),
                    web_subdir="web",
                    config_path="playwright.go-integration.config.ts",
                    dry_run_report_path=report_path,
                )
            )

            result = executor.run(
                test_cmd="environment-skill.spec.ts",
                scenario=ExecutionScenario(
                    scenario_id="spec:environment-skill",
                    step_id="playwright",
                    test_cmd="environment-skill.spec.ts",
                    spec="environment-skill.spec.ts",
                ),
            )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["failed_assertions"], [])
        self.assertIn("idaas", result.raw_failure.lower())

    def test_provider_rate_limit_in_json_report_is_environment_unavailable(self):
        report = {
            "stats": {"duration": 120000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "agent-final-conclusion.spec.ts",
                    "specs": [
                        {
                            "title": "agent produces a final conclusion",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "agent produces a final conclusion",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 120000,
                                            "error": {
                                                "message": (
                                                    "Error: [models] upstream OpenAI ListModels failed: "
                                                    "http 429 from OpenAI models API: "
                                                    "{\"message\":\"Over rate limit.\",\"code\":\"OverRateLimit\"}"
                                                ),
                                                "location": {
                                                    "file": "tests/integration/agent-final-conclusion.spec.ts",
                                                    "line": 48,
                                                    "column": 15,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "rate-limit-report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            executor = PlaywrightExecutor(
                PlaywrightExecutionConfig(
                    product_cwd=Path("/example/product"),
                    web_subdir="web",
                    config_path="playwright.go-integration.config.ts",
                    dry_run_report_path=report_path,
                )
            )

            result = executor.run(
                test_cmd="agent-final-conclusion.spec.ts",
                scenario=ExecutionScenario(
                    scenario_id="spec:agent-final-conclusion",
                    step_id="playwright",
                    test_cmd="agent-final-conclusion.spec.ts",
                    spec="agent-final-conclusion.spec.ts",
                ),
            )

        self.assertTrue(result.environment_unavailable)
        self.assertIn("OverRateLimit", result.raw_failure)

    def test_idaas_redirect_in_error_context_is_environment_unavailable(self):
        report = {
            "stats": {"duration": 30000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "environment-skill.spec.ts",
                    "specs": [
                        {
                            "title": "/api/skills exposes every environment tool",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "/api/skills exposes every environment tool",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 12,
                                            "error": {
                                                "message": "Error: expect(received).toBeTruthy() Received: false",
                                                "location": {
                                                    "file": "tests/integration/environment-skill.spec.ts",
                                                    "line": 21,
                                                    "column": 26,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "environment-skill-auth-redirect"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "waiting for https://idaas.example.test/login?redirect=... navigation to finish",
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="environment-skill.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:environment-skill",
                        step_id="playwright",
                        test_cmd="environment-skill.spec.ts",
                        spec="environment-skill.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["failed_assertions"], [])
        self.assertIn("idaas", result.raw_failure.lower())

    def test_domain_mismatch_login_page_in_error_context_is_environment_unavailable(self):
        report = {
            "stats": {"duration": 30000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "skills-panel.spec.ts",
                    "specs": [
                        {
                            "title": "returns full registry plus default-enabled meta tools without a session",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "returns full registry plus default-enabled meta tools without a session",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 30000,
                                            "error": {
                                                "message": "Error: apiRequestContext.get timed out",
                                                "location": {
                                                    "file": "tests/integration/skills-panel.spec.ts",
                                                    "line": 38,
                                                    "column": 5,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "skills-panel-domain-mismatch"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Page snapshot",
                            "  - heading \"域名不匹配，无法登录\"",
                            "  - text: 当前访问域名 localhost 与 SSO 配置的应用域名不一致。",
                            "  - text: 通过 localhost 访问会触发登录 401 重新登录的死循环。",
                            "# Test source",
                            "> 38  |         await loginWithRealSSO(page, baseURL!);",
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="skills-panel.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:skills-panel",
                        step_id="playwright",
                        test_cmd="skills-panel.spec.ts",
                        spec="skills-panel.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.environment_unavailable)
        self.assertEqual(result.observed_state["failed_assertions"], [])
        self.assertIn("域名不匹配", result.raw_failure)

    def test_login_form_error_context_redacts_entered_credentials(self):
        report = {
            "stats": {"duration": 30000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "skills-panel.spec.ts",
                    "specs": [
                        {
                            "title": "fresh session inherits DefaultDisabledSkills",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "fresh session inherits DefaultDisabledSkills",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 30000,
                                            "error": {
                                                "message": "Error: page.waitForURL: Test timeout exceeded",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "skills-panel-login"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Page snapshot",
                            '  - textbox "请输入用户名" [ref=e23]: admin',
                            '  - textbox "请输入密码" [ref=e30]: example-password',
                            '  - button "登 录" [active]',
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="skills-panel.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:skills-panel",
                        step_id="playwright",
                        test_cmd="skills-panel.spec.ts",
                        spec="skills-panel.spec.ts",
                    ),
                )

        self.assertTrue(result.environment_unavailable)
        self.assertIn("请输入用户名", result.raw_failure)
        self.assertIn("[REDACTED]", result.raw_failure)
        self.assertNotIn("admin", result.raw_failure)
        self.assertNotIn("example-password", result.raw_failure)

    def test_login_page_context_is_not_dropped_after_large_product_failures(self):
        report = {
            "stats": {"duration": 120000, "expected": 0, "skipped": 0, "unexpected": 3, "flaky": 0},
            "suites": [
                {
                    "file": "manage-notepad-context-chip.spec.ts",
                    "specs": [
                        {
                            "title": "manage_notepad Context chip + markdown preview",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "Scenario: create_notepad chip opens markdown preview modal",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 5000,
                                            "error": {
                                                "message": "Error: Notepad preview dialog did not open",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "title": "Scenario: load_notepad modal reads content from result metadata",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 5000,
                                            "error": {
                                                "message": "Error: h1 from disk was not visible",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "title": "Scenario: update_notepad chip resolves name via NameResolver",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 60000,
                                            "error": {
                                                "message": "TimeoutError: page.waitForURL: Timeout 60000ms exceeded",
                                            },
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                for name in ("a-product-failure", "b-product-failure"):
                    context_dir = output_dir / name
                    context_dir.mkdir(parents=True)
                    (context_dir / "error-context.md").write_text(
                        "\n".join(
                            [
                                "# Error details",
                                "Error: Notepad preview dialog did not open",
                                "# Page snapshot",
                                "  - text: product failure context",
                                "x" * 14000,
                            ]
                        ),
                        encoding="utf-8",
                    )
                context_dir = output_dir / "c-login-page"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Error details",
                            "TimeoutError: page.waitForURL: Timeout 60000ms exceeded.",
                            "# Page snapshot",
                            '  - textbox "请输入用户名" [ref=e23]: admin',
                            '  - textbox "请输入密码" [ref=e30]: example-password',
                            '  - button "登 录" [active]',
                            "# Test source",
                            "  80 | await page.waitForURL((url) => url.host === appHost, {timeout: 60_000});",
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="manage-notepad-context-chip.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:manage-notepad-context-chip",
                        step_id="playwright",
                        test_cmd="manage-notepad-context-chip.spec.ts",
                        spec="manage-notepad-context-chip.spec.ts",
                    ),
                )

        self.assertTrue(result.environment_unavailable)
        self.assertIn("请输入用户名", result.raw_failure)
        self.assertIn("[REDACTED]", result.raw_failure)
        self.assertNotIn("admin", result.raw_failure)
        self.assertNotIn("example-password", result.raw_failure)

    def test_idaas_source_comment_in_error_context_does_not_mask_product_failure(self):
        report = {
            "stats": {"duration": 12, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "skills-panel.spec.ts",
                    "specs": [
                        {
                            "title": "returns full registry plus default-enabled meta tools without a session",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "returns full registry plus default-enabled meta tools without a session",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 12,
                                            "error": {
                                                "message": "Error: skills missing local_files",
                                                "location": {
                                                    "file": "tests/integration/skills-panel.spec.ts",
                                                    "line": 49,
                                                    "column": 57,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "skills-panel-missing-local-files"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Test source",
                            "  15  |  * Runs against real IDaaS SSO via the go-integration config — every test",
                            "> 49  |             expect(byName.has(name), `skills missing ${name}`).toBeTruthy();",
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="skills-panel.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:skills-panel",
                        step_id="playwright",
                        test_cmd="skills-panel.spec.ts",
                        spec="skills-panel.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertIn("skills missing local_files", result.raw_failure)

    def test_idaas_source_helper_code_in_error_context_does_not_mask_product_failure(self):
        report = {
            "stats": {"duration": 10000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "manage-context.spec.ts",
                    "specs": [
                        {
                            "title": "agent cleans context after browsing a workspace",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "agent cleans context after browsing a workspace",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 10000,
                                            "error": {
                                                "message": (
                                                    "Error: expected a workspace named exactly \"pipeline\"\n\n"
                                                    "expect(locator).toBeVisible() failed"
                                                ),
                                                "location": {
                                                    "file": "tests/integration/manage-context.spec.ts",
                                                    "line": 77,
                                                    "column": 5,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "manage-context-missing-pipeline-workspace"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Error details",
                            'Error: expected a workspace named exactly "pipeline"',
                            "# Test source",
                            "  15  |         if (meResp.ok()) return;",
                            (
                                "  16  |         throw new Error(`expected redirect to IDaaS or "
                                "existing auth, got ${page.url()}`);"
                            ),
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )

                result = executor.run(
                    test_cmd="manage-context.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:manage-context",
                        step_id="playwright",
                        test_cmd="manage-context.spec.ts",
                        spec="manage-context.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertIn("pipeline", result.raw_failure)

    def test_dry_run_cli_returns_zero_even_when_fixture_represents_failed_spec(self):
        with mock.patch("pipeline_v2.playwright_executor.print"):
            rc = playwright_executor.main(
                [
                    "--product-cwd",
                    "/example/product",
                    "--web-subdir",
                    "web",
                    "--config",
                    "playwright.go-integration.config.ts",
                    "--spec",
                    "manage-context.spec.ts",
                    "--dry-run",
                    "--report",
                    str(FIXTURES / "failure-report.json"),
                ]
            )

        self.assertEqual(rc, 0)

    def test_json_assertion_failure_is_not_environment_due_to_unrelated_report_text(self):
        report = {
            "config": {
                "metadata": "SAMPLE_COOKIE missing in a setup note unrelated to this assertion",
            },
            "stats": {"duration": 12, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "environment-skill.spec.ts",
                    "specs": [
                        {
                            "title": "/api/skills exposes every environment tool",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "/api/skills exposes every environment tool",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 12,
                                            "error": {
                                                "message": "Error: expect(received).toBeTruthy() Received: false",
                                                "location": {
                                                    "file": "tests/integration/environment-skill.spec.ts",
                                                    "line": 21,
                                                    "column": 26,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            executor = PlaywrightExecutor(
                PlaywrightExecutionConfig(
                    product_cwd=Path("/example/product"),
                    web_subdir="web",
                    config_path="playwright.go-integration.config.ts",
                    dry_run_report_path=report_path,
                )
            )

            result = executor.run(
                test_cmd="environment-skill.spec.ts",
                scenario=ExecutionScenario(
                    scenario_id="spec:environment-skill",
                    step_id="playwright",
                    test_cmd="environment-skill.spec.ts",
                    spec="environment-skill.spec.ts",
                ),
            )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertEqual(len(result.observed_state["failed_assertions"]), 1)

    def test_generic_logged_out_label_in_page_snapshot_does_not_mask_ui_assertion_failure(self):
        report = {
            "stats": {"duration": 5000, "expected": 0, "skipped": 0, "unexpected": 1, "flaky": 0},
            "suites": [
                {
                    "file": "change-mode-skill-card.spec.ts",
                    "specs": [
                        {
                            "title": "change mode card appears",
                            "ok": False,
                            "tests": [
                                {
                                    "title": "change mode card appears",
                                    "status": "unexpected",
                                    "results": [
                                        {
                                            "status": "failed",
                                            "duration": 5000,
                                            "error": {
                                                "message": (
                                                    "Error: expect(locator).toBeVisible() failed\n"
                                                    "Locator: getByTestId('change-mode-tool-card')"
                                                ),
                                                "location": {
                                                    "file": "tests/integration/change-mode-skill-card.spec.ts",
                                                    "line": 43,
                                                    "column": 5,
                                                },
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            product_cwd = Path(tmpdir) / "product"
            web_dir = product_cwd / "web"
            output_dir = Path(tmpdir) / "playwright-output"
            web_dir.mkdir(parents=True)

            def run_with_error_context(command, **kwargs):
                context_dir = output_dir / "change-mode-card"
                context_dir.mkdir(parents=True)
                (context_dir / "error-context.md").write_text(
                    "\n".join(
                        [
                            "# Page snapshot",
                            '  - generic "未登录"',
                            '  - textbox "Ask a question or make a request"',
                            "# Test source",
                            "> 43 | await expect(page.getByTestId('change-mode-tool-card')).toBeVisible();",
                        ]
                    ),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=1, stdout=json.dumps(report), stderr="")

            with mock.patch("pipeline_v2.playwright_executor.subprocess.run", side_effect=run_with_error_context):
                executor = PlaywrightExecutor(
                    PlaywrightExecutionConfig(
                        product_cwd=product_cwd,
                        web_subdir="web",
                        config_path="playwright.go-integration.config.ts",
                        output_dir=output_dir,
                    )
                )
                result = executor.run(
                    test_cmd="change-mode-skill-card.spec.ts",
                    scenario=ExecutionScenario(
                        scenario_id="spec:change-mode-skill-card",
                        step_id="playwright",
                        test_cmd="change-mode-skill-card.spec.ts",
                        spec="change-mode-skill-card.spec.ts",
                    ),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.environment_unavailable)
        self.assertEqual(result.observed_state["passed"], False)
        self.assertEqual(len(result.observed_state["failed_assertions"]), 1)
        self.assertIn("change-mode-tool-card", result.raw_failure)


if __name__ == "__main__":
    unittest.main()
