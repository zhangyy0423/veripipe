"""Playwright JSON reporter executor for pipeline-v2.

The executor is product-neutral: repository location, web subdirectory,
Playwright config, and spec identifier are injected by the caller or adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .orchestrator import ExecutionResult, ExecutionScenario


ENVIRONMENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bECONNREFUSED\b",
        r"\bERR_CONNECTION_REFUSED\b",
        r"\bERR_NAME_NOT_RESOLVED\b",
        r"config\.webServer",
        r"\bwebServer\b.*(failed|timeout|timed out)",
        r"Timed out waiting .*server",
        r"browserType\.launch.*Executable doesn't exist",
        r"Please run.*playwright install",
        r"Host system is missing dependencies",
        r"running Node\.js .*Playwright requires Node\.js",
        r"Playwright requires Node\.js .*or higher",
        r"Cannot find module ['\"]@playwright/test",
        r"No tests found",
        r"https?://[^\s\"')]+idaas[^\s\"')]*",
        r"\bIDaaS\b.*\b(login|redirect|navigation|timeout|timed out|auth|authenticate|authentication)\b",
        r"\b(login|redirect|navigation|timeout|timed out|auth|authenticate|authentication)\b.*\bIDaaS\b",
        r"\bSSO\b.*\b(login|redirect|timeout|timed out|auth|authenticate|authentication)\b",
        r"\b(login|redirect|timeout|timed out|auth|authenticate|authentication)\b.*\bSSO\b",
        r"not authenticated",
        r"authentication required",
        r"login required",
        r"\bhttp\s+429\b",
        r"\bOverRateLimit\b",
        r"Over rate limit",
        r"域名不匹配",
        r"无法登录",
        r"应用域名.*不一致",
        r"localhost.*(SSO|IDaaS|登录|401)",
        r"登录.*401.*重新登录",
        r"(401|unauthorized|login_url|SSO|IDaaS|重新登录).*未登录",
        r"未登录.*(401|unauthorized|login_url|SSO|IDaaS|重新登录)",
        r"请输入用户名",
        r"请输入密码",
        r"required environment variable.*(missing|empty|required)",
    )
)


@dataclass(frozen=True)
class PlaywrightExecutionConfig:
    """Injected runtime coordinates for a Playwright spec run."""

    product_cwd: Path
    web_subdir: str
    config_path: str
    dry_run_report_path: Optional[Path] = None
    output_dir: Optional[Path] = None
    timeout_sec: int = 1800
    test_timeout_sec: int = 120
    env: Optional[Mapping[str, str]] = None
    required_env: Sequence[str] = ()


class PlaywrightExecutor:
    """Run one Playwright spec and return a structured ExecutionResult."""

    def __init__(self, config: PlaywrightExecutionConfig):
        self.config = config

    def run(self, *, test_cmd: str, scenario: ExecutionScenario) -> ExecutionResult:
        spec = _spec_id(scenario.spec or test_cmd)
        web_subdir_error = _relative_path_error(self.config.web_subdir, "web_subdir", allow_current=True)
        if web_subdir_error:
            return _environment_result(spec=spec, stdout="", stderr=web_subdir_error, exit_code=1)
        config_path_error = _relative_path_error(self.config.config_path, "config_path", allow_current=False)
        if config_path_error:
            return _environment_result(spec=spec, stdout="", stderr=config_path_error, exit_code=1)
        if self.config.dry_run_report_path:
            try:
                stdout = self.config.dry_run_report_path.read_text(encoding="utf-8")
            except OSError as exc:
                return _environment_result(
                    spec=spec,
                    stdout="",
                    stderr=f"Playwright dry-run report is not readable: {self.config.dry_run_report_path}: {exc}",
                    exit_code=1,
                )
            return _execution_result_from_report(spec=spec, stdout=stdout, stderr="", exit_code=None)

        product_root = self.config.product_cwd.resolve()
        web_dir = (product_root / self.config.web_subdir).resolve()
        if web_dir != product_root and product_root not in web_dir.parents:
            return _environment_result(
                spec=spec,
                stdout="",
                stderr=f"Playwright web_subdir must stay inside product_cwd: {self.config.web_subdir}",
                exit_code=1,
            )
        if not web_dir.is_dir():
            return _environment_result(
                spec=spec,
                stdout="",
                stderr=f"Playwright web directory not found: {web_dir}",
                exit_code=1,
            )
        if self.config.output_dir:
            output_dir = self.config.output_dir.resolve()
            if output_dir == product_root or product_root in output_dir.parents:
                return _environment_result(
                    spec=spec,
                    stdout="",
                    stderr=f"Playwright output_dir must be outside product_cwd: {output_dir}",
                    exit_code=1,
                )

        env = os.environ.copy()
        if self.config.env:
            env.update({str(key): str(value) for key, value in self.config.env.items()})
        missing_env = [name for name in self.config.required_env if not str(env.get(str(name), "")).strip()]
        if missing_env:
            return _environment_result(
                spec=spec,
                stdout="",
                stderr=f"required environment variable is missing or empty: {', '.join(missing_env)}",
                exit_code=1,
            )

        with _output_dir(self.config.output_dir) as output_dir:
            command = [
                "npx",
                "playwright",
                "test",
                f"--config={self.config.config_path}",
                "--reporter=json",
                f"--timeout={max(1, int(self.config.test_timeout_sec)) * 1000}",
                f"--output={output_dir}",
                spec,
            ]
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(web_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                    check=False,
                    timeout=self.config.timeout_sec,
                    env=env,
                )
            except FileNotFoundError as exc:
                return _environment_result(spec=spec, stdout="", stderr=str(exc), exit_code=127)
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else str(exc)
                return _environment_result(spec=spec, stdout=stdout, stderr=stderr, exit_code=124)
            artifact_text = _read_playwright_error_contexts(Path(output_dir))

        return _execution_result_from_report(
            spec=spec,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=proc.returncode,
            artifact_text=artifact_text,
        )


def _spec_id(value: str) -> str:
    spec = str(value or "").strip()
    if not spec:
        raise ValueError("playwright spec is required")
    path = Path(spec)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"playwright spec must be a relative identifier without '..': {spec}")
    return spec


def _relative_path_error(value: str, field_name: str, *, allow_current: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return f"Playwright {field_name} is required"
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        return f"Playwright {field_name} must be relative and stay inside product_cwd: {text}"
    if not allow_current and text == ".":
        return f"Playwright {field_name} must point to a file below web_subdir"
    return ""


class _output_dir:
    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self._tmp: Optional[tempfile.TemporaryDirectory[str]] = None

    def __enter__(self) -> str:
        if self.path:
            output_dir = self.path.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            return str(output_dir)
        self._tmp = tempfile.TemporaryDirectory(prefix="pipeline-v2-playwright-")
        return self._tmp.name

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._tmp:
            self._tmp.cleanup()


def _execution_result_from_report(
    *,
    spec: str,
    stdout: str,
    stderr: str,
    exit_code: Optional[int],
    artifact_text: str = "",
) -> ExecutionResult:
    report = _load_json_report(stdout)
    if report is None:
        if (exit_code or 0) != 0 and _is_environment_unavailable(stdout, stderr):
            return _environment_result(spec=spec, stdout=stdout, stderr=stderr, exit_code=exit_code or 1)
        raw = _compact_raw_failure(stderr or stdout or "playwright JSON reporter output was not available")
        return ExecutionResult(
            observed_state={
                "spec": spec,
                "passed": False,
                "failed_assertions": [{"title": spec, "message": raw, "location": ""}],
                "duration": 0,
            },
            raw_failure=raw,
            exit_code=exit_code if exit_code is not None else 1,
            stderr=stderr,
            stdout=stdout,
        )

    if _all_tests_skipped(report):
        return _environment_result(
            spec=spec,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code if exit_code is not None else 0,
            reason="playwright skipped all tests; required runtime environment is unavailable",
        )

    failed_assertions = _failed_assertions(report)
    duration = _duration(report)
    passed = not failed_assertions and (exit_code in (None, 0))
    normalized_exit_code = 0 if passed else (exit_code if exit_code is not None else 1)
    product_failures = _product_failures(failed_assertions)
    has_product_failure = bool(product_failures)
    raw_failure = _failure_summary(product_failures or failed_assertions)
    environment_failure = _environment_failure_summary(
        failed_assertions,
        include_top_level=not has_product_failure,
    )
    environment_artifact = _environment_text_summary(artifact_text)
    top_level_blocker = _top_level_failure_summary(failed_assertions)
    failure_text_is_environment = (
        not has_product_failure
        and _is_environment_unavailable(stderr, raw_failure, environment_failure)
    )
    if normalized_exit_code != 0 and top_level_blocker and not has_product_failure:
        return _environment_result(
            spec=spec,
            stdout=stdout,
            stderr=stderr,
            exit_code=normalized_exit_code,
            reason=top_level_blocker,
            duration=duration,
        )
    if normalized_exit_code != 0 and (environment_failure or environment_artifact or failure_text_is_environment):
        return _environment_result(
            spec=spec,
            stdout=stdout,
            stderr=stderr,
            exit_code=normalized_exit_code,
            reason=environment_failure or environment_artifact or raw_failure or None,
            duration=duration,
        )
    return ExecutionResult(
        observed_state={
            "spec": spec,
            "passed": passed,
            "failed_assertions": failed_assertions,
            "duration": duration,
        },
        raw_failure=raw_failure,
        exit_code=normalized_exit_code,
        stderr=stderr,
        stdout=stdout,
    )


def _load_json_report(stdout: str) -> Optional[Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, Mapping) else None


def _failed_assertions(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = _top_level_failures(report)
    for suite in _walk_suites(report.get("suites", [])):
        suite_file = str(suite.get("file") or suite.get("title") or "")
        for spec in suite.get("specs", []) if isinstance(suite.get("specs"), list) else []:
            if not isinstance(spec, Mapping):
                continue
            spec_title = str(spec.get("title") or "")
            spec_failed_without_result = spec.get("ok") is False
            tests = spec.get("tests", [])
            if not isinstance(tests, list):
                tests = []
            saw_failed_result = False
            for test in tests:
                if not isinstance(test, Mapping):
                    continue
                test_title = str(test.get("title") or spec_title or suite_file)
                test_status = str(test.get("status") or "")
                for result in test.get("results", []) if isinstance(test.get("results"), list) else []:
                    if not isinstance(result, Mapping):
                        continue
                    error = _result_error(result)
                    result_status = str(result.get("status") or "")
                    if result_status in {"passed", "skipped"} and not error and test_status in {"expected", "skipped"}:
                        continue
                    saw_failed_result = True
                    failures.append(
                        {
                            "title": test_title,
                            "message": _message_from_error(error) or result_status or test_status or "failed",
                            "location": _location_from_error(error),
                            "status": result_status or test_status,
                            "file": suite_file,
                        }
                    )
            if spec_failed_without_result and not saw_failed_result:
                failures.append(
                    {
                        "title": spec_title or suite_file,
                        "message": "playwright spec failed",
                        "location": "",
                        "status": "failed",
                        "file": suite_file,
                    }
                )
    return failures


def _top_level_failures(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    errors = report.get("errors")
    if not isinstance(errors, list) or not errors:
        return []

    messages = [_message_from_error(error) for error in errors]
    has_non_environment_error = any(message and not _is_environment_unavailable(message) for message in messages)
    failures: List[Dict[str, Any]] = []
    for error, message in zip(errors, messages):
        if not message:
            continue
        if has_non_environment_error and _is_environment_unavailable(message):
            continue
        failures.append(
            {
                "title": "playwright global error",
                "message": message,
                "location": _location_from_error(error),
                "status": "failed",
                "file": "",
            }
        )
    return failures


def _walk_suites(value: Any) -> Sequence[Mapping[str, Any]]:
    suites: List[Mapping[str, Any]] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        suites.append(item)
        suites.extend(_walk_suites(item.get("suites", [])))
    return suites


def _result_error(result: Mapping[str, Any]) -> Any:
    error = result.get("error")
    if error:
        return error
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return errors[0]
    return None


def _message_from_error(error: Any) -> str:
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("value") or "").strip()
    return str(error or "").strip()


def _location_from_error(error: Any) -> str:
    if not isinstance(error, Mapping):
        return ""
    location = error.get("location")
    if isinstance(location, Mapping):
        file_name = str(location.get("file") or "").strip()
        line = location.get("line")
        column = location.get("column")
        if file_name and line and column:
            return f"{file_name}:{line}:{column}"
        if file_name and line:
            return f"{file_name}:{line}"
        return file_name
    stack = str(error.get("stack") or "")
    match = re.search(r"(\S+\.spec\.[tj]s:\d+:\d+)", stack)
    return match.group(1) if match else ""


def _duration(report: Mapping[str, Any]) -> int:
    stats = report.get("stats")
    if isinstance(stats, Mapping) and isinstance(stats.get("duration"), (int, float)):
        return int(stats["duration"])
    total = 0
    for suite in _walk_suites(report.get("suites", [])):
        for spec in suite.get("specs", []) if isinstance(suite.get("specs"), list) else []:
            if not isinstance(spec, Mapping):
                continue
            for test in spec.get("tests", []) if isinstance(spec.get("tests"), list) else []:
                if not isinstance(test, Mapping):
                    continue
                for result in test.get("results", []) if isinstance(test.get("results"), list) else []:
                    if isinstance(result, Mapping) and isinstance(result.get("duration"), (int, float)):
                        total += int(result["duration"])
    return total


def _all_tests_skipped(report: Mapping[str, Any]) -> bool:
    stats = report.get("stats")
    if isinstance(stats, Mapping):
        skipped = int(stats.get("skipped") or 0)
        expected = int(stats.get("expected") or 0)
        unexpected = int(stats.get("unexpected") or 0)
        flaky = int(stats.get("flaky") or 0)
        return skipped > 0 and expected == 0 and unexpected == 0 and flaky == 0
    statuses: List[str] = []
    for suite in _walk_suites(report.get("suites", [])):
        for spec in suite.get("specs", []) if isinstance(suite.get("specs"), list) else []:
            if not isinstance(spec, Mapping):
                continue
            for test in spec.get("tests", []) if isinstance(spec.get("tests"), list) else []:
                if isinstance(test, Mapping):
                    statuses.append(str(test.get("status") or ""))
    return bool(statuses) and all(status == "skipped" for status in statuses)


def _failure_summary(failed_assertions: Sequence[Mapping[str, Any]]) -> str:
    if not failed_assertions:
        return ""
    first = failed_assertions[0]
    title = str(first.get("title") or "playwright assertion")
    message = _compact_raw_failure(str(first.get("message") or "failed"))
    location = str(first.get("location") or "").strip()
    suffix = f" ({location})" if location else ""
    return f"{title}: {message}{suffix}"


def _product_failures(failed_assertions: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    product_failures: List[Mapping[str, Any]] = []
    for failure in failed_assertions:
        if _is_top_level_failure(failure):
            continue
        parts = [
            str(failure.get("title") or ""),
            str(failure.get("message") or ""),
            str(failure.get("location") or ""),
            str(failure.get("status") or ""),
            str(failure.get("file") or ""),
        ]
        if not _is_environment_unavailable(*parts):
            product_failures.append(failure)
    return product_failures


def _environment_failure_summary(
    failed_assertions: Sequence[Mapping[str, Any]],
    *,
    include_top_level: bool = True,
) -> str:
    for failure in failed_assertions:
        if not include_top_level and _is_top_level_failure(failure):
            continue
        parts = [
            str(failure.get("title") or ""),
            str(failure.get("message") or ""),
            str(failure.get("location") or ""),
            str(failure.get("status") or ""),
            str(failure.get("file") or ""),
        ]
        if not _is_environment_unavailable(*parts):
            continue
        title = str(failure.get("title") or "playwright environment")
        message = _compact_raw_failure(str(failure.get("message") or "environment unavailable"))
        location = str(failure.get("location") or "").strip()
        suffix = f" ({location})" if location else ""
        return f"{title}: {message}{suffix}"
    return ""


def _top_level_failure_summary(failed_assertions: Sequence[Mapping[str, Any]]) -> str:
    top_level = [failure for failure in failed_assertions if _is_top_level_failure(failure)]
    if not top_level or len(top_level) != len(failed_assertions):
        return ""
    return _failure_summary(top_level)


def _is_top_level_failure(failure: Mapping[str, Any]) -> bool:
    return (
        str(failure.get("title") or "") == "playwright global error"
        and not str(failure.get("file") or "").strip()
    )


def _environment_text_summary(text: str) -> str:
    lines = [line.strip() for line in _runtime_error_context_lines(text) if line.strip()]
    for index, line in enumerate(lines):
        if not _is_environment_unavailable(line):
            continue
        start = max(0, index - 1)
        end = min(len(lines), index + 2)
        return _compact_raw_failure(" ".join(lines[start:end]))
    return ""


def _runtime_error_context_lines(text: str) -> Sequence[str]:
    """Return Playwright error-context lines that describe runtime state."""

    lines: List[str] = []
    in_test_source = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.lower() == "# test source":
            in_test_source = True
            continue
        if in_test_source:
            if stripped.startswith("# ") and stripped.lower() != "# test source":
                in_test_source = False
            else:
                continue
        lines.append(line)
    return lines


def _read_playwright_error_contexts(output_dir: Path, *, max_files: int = 5, max_chars: int = 20000) -> str:
    try:
        files = sorted(output_dir.glob("**/error-context.md"))
    except OSError:
        return ""
    environment_snippets: List[str] = []
    chunks: List[str] = []
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text:
            continue
        environment_summary = _environment_text_summary(text)
        if environment_summary:
            environment_snippets.append(environment_summary)
    for path in files[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        snippet = _redact_runtime_text(text[:remaining])
        chunks.append(snippet)
        total += len(snippet)
    return "\n".join(environment_snippets + chunks)


def _compact_raw_failure(text: str, *, limit: int = 500) -> str:
    lines = [line.strip() for line in _redact_runtime_text(str(text or "")).splitlines() if line.strip()]
    compact = " ".join(lines)
    return compact[:limit]


def _redact_runtime_text(text: str) -> str:
    redacted = str(text or "")
    redacted = re.sub(
        r'(textbox\s+"[^"]*(?:用户名|账号|密码|username|password)[^"]*"[^:\n]*:\s*)[^\n]+',
        r"\1[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"(\b[A-Za-z0-9_]*(?:KEY|TOKEN|COOKIE|SECRET|PASSWORD)\b\s*[:=]\s*)[^\s,\]}]+",
        r"\1[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def _is_environment_unavailable(*texts: str) -> bool:
    combined = "\n".join(text for text in texts if text)
    return any(pattern.search(combined) for pattern in ENVIRONMENT_PATTERNS)


def _environment_result(
    *,
    spec: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    reason: Optional[str] = None,
    duration: int = 0,
) -> ExecutionResult:
    raw = _compact_raw_failure(reason or stderr or stdout or "playwright environment unavailable")
    return ExecutionResult(
        observed_state={
            "spec": spec,
            "passed": False,
            "failed_assertions": [],
            "duration": duration,
            "environment_unavailable": True,
            "environment_reason": raw,
        },
        raw_failure=raw,
        exit_code=exit_code,
        stderr=stderr,
        stdout=stdout,
        environment_unavailable=True,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Playwright spec through pipeline-v2 executor")
    parser.add_argument("--product-cwd", required=True, type=Path)
    parser.add_argument("--web-subdir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, help="Playwright JSON reporter fixture for --dry-run")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.dry_run and not args.report:
        parser.error("--dry-run requires --report")
    executor = PlaywrightExecutor(
        PlaywrightExecutionConfig(
            product_cwd=args.product_cwd,
            web_subdir=args.web_subdir,
            config_path=args.config,
            dry_run_report_path=args.report if args.dry_run else None,
            output_dir=args.output_dir,
        )
    )
    result = executor.run(
        test_cmd=args.spec,
        scenario=ExecutionScenario(
            scenario_id=f"spec:{args.spec}",
            step_id="playwright",
            test_cmd=args.spec,
            spec=args.spec,
        ),
    )
    print(
        json.dumps(
            {
                "observed_state": result.observed_state,
                "raw_failure": result.raw_failure,
                "exit_code": result.exit_code,
                "environment_unavailable": result.environment_unavailable,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if args.dry_run:
        return 0
    return 0 if result.exit_code == 0 or result.environment_unavailable else result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
