"""Round-based loop runner for pipeline-v2.

This module is intentionally product-neutral. Product coordinates are injected
through adapter/config/runtime arguments; the loop only repeats the existing
orchestrator CLI and writes round-scoped artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]


@dataclass(frozen=True)
class LoopConfig:
    adapter: Path
    product_cwd: Path
    output_root: Path = Path(".tmp/pv2/loops")
    run_id: Optional[str] = None
    executor: str = "playwright"
    semantic_map: Optional[Path] = None
    code_map: Optional[Path] = None
    knowledge_cases: Optional[Path] = None
    knowledge_excluded_leads: Optional[Path] = None
    understanding_profile: Optional[Path] = None
    target_plan: Optional[Path] = None
    product_head: str = ""
    hosts_file: Path = Path("/etc/hosts")
    duration_sec: float = 8 * 60 * 60
    max_rounds: Optional[int] = None
    sleep_sec: float = 60
    timeout_sec: int = 1800
    preflight: bool = True
    stop_on_not_ready: bool = True
    stop_after_consecutive_blockers: Optional[int] = None
    environment_retry_count: int = 1
    pre_batch_hook: Optional[Path] = None
    pre_batch_hook_timeout_sec: int = 300
    pre_batch_hook_env: Optional[Mapping[str, str]] = None
    dry_run: bool = False
    report: Optional[Path] = None
    python_executable: str = sys.executable
    enforce_knowledge_gate: bool = True


@dataclass(frozen=True)
class LoopResult:
    run_id: str
    run_dir: Path
    rounds: Sequence[Mapping[str, Any]]
    summary_path: Path


def run_loop(
    config: LoopConfig,
    *,
    run_command: RunCommand = subprocess.run,
    sleep: SleepFn = time.sleep,
    now: NowFn = time.monotonic,
) -> LoopResult:
    """Run preflight + pipeline batches until duration or max_rounds is reached."""

    run_id = config.run_id or _timestamp_run_id()
    run_dir = (config.output_root / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    config = _with_adapter_knowledge_defaults(config)
    config = _with_adapter_runtime_defaults(config)

    started_at = _utc_timestamp()
    adapter_metadata = _adapter_summary_metadata(config.adapter)
    knowledge_audit = _knowledge_audit_metadata(config)
    if knowledge_audit is not None:
        adapter_metadata["knowledge_audit"] = knowledge_audit
    deadline = now() + max(0.0, config.duration_sec)
    rounds: List[Dict[str, Any]] = []
    round_index = 0
    stop_reason: Optional[str] = None

    if isinstance(knowledge_audit, Mapping) and knowledge_audit.get("status") == "fail":
        blockers = knowledge_audit.get("blockers", [])
        blocker_kinds = [
            str(item.get("kind") or "")
            for item in blockers
            if isinstance(item, Mapping) and str(item.get("kind") or "")
        ]
        stop_reason = "knowledge-audit-blocked"
        if blocker_kinds:
            stop_reason = f"{stop_reason}: {', '.join(blocker_kinds)}"
        summary_path = _write_summary(
            run_dir=run_dir,
            run_id=run_id,
            started_at=started_at,
            adapter_metadata=adapter_metadata,
            rounds=rounds,
            stop_reason=stop_reason,
            completed_at=_utc_timestamp(),
        )
        return LoopResult(run_id=run_id, run_dir=run_dir, rounds=tuple(rounds), summary_path=summary_path)

    while _should_start_next_round(
        round_index=round_index,
        max_rounds=config.max_rounds,
        now=now,
        deadline=deadline,
    ):
        round_index += 1
        round_dir = run_dir / f"round-{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        round_record: Dict[str, Any] = {
            "round": round_index,
            "round_dir": str(round_dir),
            "started_at": _utc_timestamp(),
        }

        if config.preflight:
            preflight_record = _run_preflight(config, round_dir=round_dir, run_command=run_command)
            round_record["preflight"] = preflight_record
            round_record["preflight_status"] = preflight_record.get("json", {}).get("status", "unknown")
            if preflight_record["returncode"] != 0:
                round_record["status"] = round_record["preflight_status"]
                round_record["completed_at"] = _utc_timestamp()
                rounds.append(round_record)
                stop_reason = _repeated_blocker_stop_reason(rounds, config.stop_after_consecutive_blockers)
                _write_summary(
                    run_dir=run_dir,
                    run_id=run_id,
                    started_at=started_at,
                    adapter_metadata=adapter_metadata,
                    rounds=rounds,
                    stop_reason=stop_reason,
                )
                if stop_reason:
                    break
                if config.stop_on_not_ready:
                    stop_reason = "not-ready preflight"
                    _write_summary(
                        run_dir=run_dir,
                        run_id=run_id,
                        started_at=started_at,
                        adapter_metadata=adapter_metadata,
                        rounds=rounds,
                        stop_reason=stop_reason,
                    )
                    break
                _sleep_before_next_round(
                    round_index=round_index,
                    max_rounds=config.max_rounds,
                    now=now,
                    deadline=deadline,
                    sleep_sec=config.sleep_sec,
                    sleep=sleep,
                )
                continue

        hook_record = _run_pre_batch_hook(config, round_dir=round_dir, run_command=run_command)
        if hook_record is not None:
            round_record["pre_batch_hook"] = hook_record
            if hook_record["returncode"] != 0:
                round_record["status"] = "environment-event"
                round_record["reported_count"] = 0
                round_record["observed_count"] = 0
                round_record["signal_count"] = 0
                round_record["completed_at"] = _utc_timestamp()
                rounds.append(round_record)
                stop_reason = _repeated_blocker_stop_reason(rounds, config.stop_after_consecutive_blockers)
                _write_summary(
                    run_dir=run_dir,
                    run_id=run_id,
                    started_at=started_at,
                    adapter_metadata=adapter_metadata,
                    rounds=rounds,
                    stop_reason=stop_reason,
                )
                if stop_reason:
                    break
                _sleep_before_next_round(
                    round_index=round_index,
                    max_rounds=config.max_rounds,
                    now=now,
                    deadline=deadline,
                    sleep_sec=config.sleep_sec,
                    sleep=sleep,
                )
                continue

        batch_record, batch_attempts, retry_hook_records = _run_batch_with_environment_retries(
            config,
            round_dir=round_dir,
            run_command=run_command,
        )
        if retry_hook_records:
            hook_attempts = [hook_record] if hook_record is not None else []
            hook_attempts.extend(retry_hook_records)
            round_record["pre_batch_hook_attempts"] = hook_attempts
        round_record["batch"] = batch_record
        if len(batch_attempts) > 1:
            round_record["batch_attempts"] = batch_attempts
        round_record["status"] = batch_record.get("json", {}).get("status", "unknown")
        round_record["reported_count"] = int(batch_record.get("json", {}).get("reported_count") or 0)
        round_record["observed_count"] = int(batch_record.get("json", {}).get("observed_count") or 0)
        round_record["signal_count"] = int(batch_record.get("json", {}).get("signal_count") or 0)
        round_record["completed_at"] = _utc_timestamp()
        rounds.append(round_record)
        stop_reason = _repeated_blocker_stop_reason(rounds, config.stop_after_consecutive_blockers)
        _write_summary(
            run_dir=run_dir,
            run_id=run_id,
            started_at=started_at,
            adapter_metadata=adapter_metadata,
            rounds=rounds,
            stop_reason=stop_reason,
        )
        if stop_reason:
            break
        _sleep_before_next_round(
            round_index=round_index,
            max_rounds=config.max_rounds,
            now=now,
            deadline=deadline,
            sleep_sec=config.sleep_sec,
            sleep=sleep,
        )

    summary_path = _write_summary(
        run_dir=run_dir,
        run_id=run_id,
        started_at=started_at,
        adapter_metadata=adapter_metadata,
        rounds=rounds,
        stop_reason=stop_reason,
        completed_at=_utc_timestamp(),
    )
    return LoopResult(run_id=run_id, run_dir=run_dir, rounds=tuple(rounds), summary_path=summary_path)


def _run_preflight(config: LoopConfig, *, round_dir: Path, run_command: RunCommand) -> Dict[str, Any]:
    command = _orchestrator_base_command(config)
    command.extend(
        [
            "--preflight",
            f"--hosts-file={config.hosts_file}",
            f"--output-dir={round_dir / 'playwright-output'}",
        ]
    )
    return _run_and_record(
        command,
        path_prefix=round_dir / "preflight",
        run_command=run_command,
        timeout_sec=config.timeout_sec,
        timeout_json={
            "status": "not-ready",
            "failed_count": 1,
            "checks": [
                {
                    "name": "loop_runner.orchestrator_timeout",
                    "status": "fail",
                    "detail": f"orchestrator timed out after {config.timeout_sec}s",
                }
            ],
        },
    )


def _run_batch_with_environment_retries(
    config: LoopConfig,
    *,
    round_dir: Path,
    run_command: RunCommand,
) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    max_attempts = max(1, int(config.environment_retry_count) + 1)
    attempts: List[Dict[str, Any]] = []
    retry_hook_records: List[Dict[str, Any]] = []
    final_record: Optional[Dict[str, Any]] = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and config.pre_batch_hook is not None:
            hook_record = _run_pre_batch_hook(
                config,
                round_dir=round_dir,
                run_command=run_command,
                artifact_stem=f"pre-batch-hook-attempt-{attempt}",
            )
            if hook_record is not None:
                retry_hook_records.append(hook_record)
                if hook_record["returncode"] != 0:
                    final_record = hook_record
                    break
        record = _run_batch(
            config,
            round_dir=round_dir,
            run_command=run_command,
            artifact_stem=f"batch-attempt-{attempt}",
        )
        attempts.append(record)
        final_record = record
        if record.get("json", {}).get("status") != "environment-event":
            break
    assert final_record is not None
    return _publish_final_batch_record(final_record, round_dir=round_dir), attempts, retry_hook_records


def _publish_final_batch_record(record: Dict[str, Any], *, round_dir: Path) -> Dict[str, Any]:
    final_record = dict(record)
    for suffix in ("stdout", "stderr", "json"):
        src_value = str(record.get(f"{suffix}_path") or "")
        src = Path(src_value) if src_value else None
        dest = round_dir / f"batch.{suffix}"
        if src and src.exists():
            shutil.copyfile(src, dest)
        elif suffix in ("stdout", "stderr"):
            dest.write_text("", encoding="utf-8")
        final_record[f"{suffix}_path"] = str(dest)
    return final_record


def _run_pre_batch_hook(
    config: LoopConfig,
    *,
    round_dir: Path,
    run_command: RunCommand,
    artifact_stem: str = "pre-batch-hook",
) -> Optional[Dict[str, Any]]:
    if config.pre_batch_hook is None:
        return None
    timeout_sec = max(1, int(config.pre_batch_hook_timeout_sec))
    path_prefix = round_dir / artifact_stem
    record = _run_and_record(
        [str(Path(config.pre_batch_hook))],
        path_prefix=path_prefix,
        run_command=run_command,
        timeout_sec=timeout_sec,
        env=dict(config.pre_batch_hook_env or {}) or None,
        timeout_json={
            "status": "environment-event",
            "environment_unavailable": True,
            "environment_reasons": [f"pre-batch hook timed out after {timeout_sec}s"],
            "reported_count": 0,
            "observed_count": 0,
            "signal_count": 0,
        },
    )
    if record["returncode"] != 0 and int(record["returncode"]) != 124:
        _write_hook_environment_json(record, path_prefix=path_prefix)
    return record


def _write_hook_environment_json(record: Dict[str, Any], *, path_prefix: Path) -> None:
    stderr_path = Path(str(record.get("stderr_path") or ""))
    detail = ""
    if stderr_path.exists():
        detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    reason = "pre-batch hook failed"
    if detail:
        reason = f"{reason}: {detail.splitlines()[-1]}"
    parsed = {
        "status": "environment-event",
        "environment_unavailable": True,
        "environment_reasons": [reason],
        "reported_count": 0,
        "observed_count": 0,
        "signal_count": 0,
    }
    json_path = path_prefix.with_suffix(".json")
    json_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["json_path"] = str(json_path)
    record["json"] = parsed


def _run_batch(
    config: LoopConfig,
    *,
    round_dir: Path,
    run_command: RunCommand,
    artifact_stem: str = "batch",
) -> Dict[str, Any]:
    command = _orchestrator_base_command(config)
    runner_timeout_sec = _batch_runner_timeout_sec(config)
    command.extend(
        [
            f"--ledger={round_dir / 'ledger.sqlite'}",
            f"--queue={round_dir / 'queue.jsonl'}",
            f"--brief={round_dir / 'brief.md'}",
            f"--output-dir={round_dir / 'playwright-output'}",
            f"--timeout-sec={config.timeout_sec}",
            f"--suggestions={round_dir / 'suggestions.jsonl'}",
        ]
    )
    if config.dry_run:
        command.append("--dry-run")
        if config.report:
            command.append(f"--report={config.report}")
    return _run_and_record(
        command,
        path_prefix=round_dir / artifact_stem,
        run_command=run_command,
        timeout_sec=runner_timeout_sec,
        timeout_json={
            "status": "environment-event",
            "environment_unavailable": True,
            "environment_reasons": [f"orchestrator timed out after {runner_timeout_sec}s"],
            "reported_count": 0,
            "observed_count": 0,
            "signal_count": 0,
        },
    )


def _orchestrator_base_command(config: LoopConfig) -> List[str]:
    command = [
        config.python_executable,
        "-m",
        "pipeline_v2.orchestrator",
        f"--executor={config.executor}",
        f"--adapter={config.adapter}",
        f"--product-cwd={config.product_cwd}",
    ]
    if config.semantic_map:
        command.append(f"--semantic-map={config.semantic_map}")
    if config.target_plan:
        command.append(f"--target-plan={config.target_plan}")
    return command


def _batch_runner_timeout_sec(config: LoopConfig) -> int:
    scenario_count = _playwright_spec_count(config) if config.executor == "playwright" else 1
    # A failing scenario can be executed once to generate the signal and once
    # more during reproduction verification. The child orchestrator still gets
    # config.timeout_sec as the per-spec Playwright timeout.
    return max(1, int(config.timeout_sec)) * max(1, scenario_count) * 2


def _playwright_spec_count(config: LoopConfig) -> int:
    target_count = _target_plan_item_count(config.target_plan, "must_run_l2_checks")
    if target_count:
        return target_count
    adapter = config.adapter
    try:
        data = json.loads(Path(adapter).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 1
    if not isinstance(data, Mapping):
        return 1
    playwright = data.get("playwright")
    if not isinstance(playwright, Mapping):
        return 1
    whitelist = playwright.get("spec_whitelist")
    if not isinstance(whitelist, list):
        return 1
    return len(whitelist) or 1


def _target_plan_item_count(target_plan: Optional[Path], key: str) -> int:
    if target_plan is None:
        return 0
    try:
        data = json.loads(Path(target_plan).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(data, Mapping):
        return 0
    items = data.get(key)
    return len(items) if isinstance(items, list) else 0


def _adapter_summary_metadata(adapter: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(adapter).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {"profile": "unknown", "spec_count": 0, "semantic_entry_count": 0, "semantic_entry_ids": []}
    if not isinstance(data, Mapping):
        return {"profile": "unknown", "spec_count": 0, "semantic_entry_count": 0, "semantic_entry_ids": []}

    profile = str(data.get("profile") or "stable")
    profile_description = str(data.get("profile_description") or "").strip()
    profile_scope = str(data.get("profile_scope") or "").strip()
    profile_limitations = _string_list(data.get("profile_limitations"))
    metadata: Dict[str, Any] = {
        "profile": profile,
        "spec_count": 0,
        "semantic_entry_count": 0,
        "semantic_entry_ids": [],
    }
    if profile_description:
        metadata["profile_description"] = profile_description
    if profile_scope:
        metadata["profile_scope"] = profile_scope
    if profile_limitations:
        metadata["profile_limitations"] = profile_limitations

    playwright = data.get("playwright")
    if not isinstance(playwright, Mapping):
        return metadata
    whitelist = playwright.get("spec_whitelist")
    if not isinstance(whitelist, list):
        return metadata

    semantic_ids = {
        str(item.get("semantic_map_entry_id"))
        for item in whitelist
        if isinstance(item, Mapping) and str(item.get("semantic_map_entry_id") or "").strip()
    }
    metadata.update(
        {
            "spec_count": len(whitelist),
            "semantic_entry_count": len(semantic_ids),
            "semantic_entry_ids": sorted(semantic_ids),
        }
    )
    return metadata


def _with_adapter_knowledge_defaults(config: LoopConfig) -> LoopConfig:
    try:
        data = json.loads(Path(config.adapter).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return config
    if not isinstance(data, Mapping):
        return config
    audit = data.get("knowledge_audit")
    if not isinstance(audit, Mapping):
        return config

    return replace(
        config,
        semantic_map=config.semantic_map or _adapter_relative_path(config.adapter, audit.get("semantic_map")),
        code_map=config.code_map or _adapter_relative_path(config.adapter, audit.get("code_map")),
        knowledge_cases=config.knowledge_cases or _adapter_relative_path(config.adapter, audit.get("cases")),
        knowledge_excluded_leads=(
            config.knowledge_excluded_leads
            or _adapter_relative_path(config.adapter, audit.get("excluded_leads"))
        ),
        understanding_profile=(
            config.understanding_profile
            or _adapter_relative_path(config.adapter, audit.get("understanding_profile"))
        ),
        target_plan=config.target_plan or _adapter_relative_path(config.adapter, audit.get("target_plan")),
    )


def _with_adapter_runtime_defaults(config: LoopConfig) -> LoopConfig:
    try:
        data = json.loads(Path(config.adapter).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return config
    if not isinstance(data, Mapping):
        return config
    runtime = data.get("runtime")
    if not isinstance(runtime, Mapping):
        return config

    hook = config.pre_batch_hook
    if hook is None:
        hook = _adapter_relative_path(config.adapter, runtime.get("pre_batch_hook"))
    hook_env = config.pre_batch_hook_env
    if hook_env is None and hook is not None:
        hook_env = _adapter_playwright_env(config.adapter)
    timeout_sec = config.pre_batch_hook_timeout_sec
    if "pre_batch_hook_timeout_sec" in runtime:
        value = runtime.get("pre_batch_hook_timeout_sec")
        if isinstance(value, int) and value > 0:
            timeout_sec = value

    return replace(
        config,
        pre_batch_hook=hook,
        pre_batch_hook_timeout_sec=timeout_sec,
        pre_batch_hook_env=hook_env,
    )


def _adapter_playwright_env(adapter: Path) -> Optional[Mapping[str, str]]:
    try:
        data = json.loads(Path(adapter).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, Mapping):
        return None
    playwright = data.get("playwright")
    if not isinstance(playwright, Mapping):
        return None
    env = playwright.get("env")
    if not isinstance(env, Mapping):
        return None
    result: Dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        result[key] = value
    return result or None


def _adapter_relative_path(adapter: Path, value: Any) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    candidates = [
        Path.cwd() / path,
        Path(adapter).parent / path,
    ]
    try:
        candidates.append(Path(adapter).parents[2] / path)
    except IndexError:
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def _knowledge_audit_metadata(config: LoopConfig) -> Optional[Dict[str, Any]]:
    missing_inputs = []
    if config.semantic_map is None:
        missing_inputs.append("semantic_map")
    if config.code_map is None:
        missing_inputs.append("code_map")
    if missing_inputs:
        if not config.enforce_knowledge_gate:
            return None
        return _knowledge_audit_failure(
            "knowledge_gate_missing_inputs",
            "missing required knowledge gate inputs: " + ", ".join(missing_inputs),
            next_step_category="add-code-link",
        )
    if config.semantic_map is None or config.code_map is None:
        return None
    try:
        from .knowledge_audit import audit_knowledge, load_mapping, load_sequence

        target_plan = load_mapping(config.target_plan) if config.target_plan is not None else None
        product_head = config.product_head or _git_product_head(config.product_cwd)
        if not product_head:
            return _knowledge_audit_failure(
                "product_head_unavailable",
                f"cannot read git HEAD from product_cwd: {config.product_cwd}",
                next_step_category="runtime-preflight",
            )
        report = audit_knowledge(
            semantic_map=load_mapping(config.semantic_map),
            code_map=load_mapping(config.code_map),
            adapter=load_mapping(config.adapter),
            cases=load_sequence(config.knowledge_cases),
            excluded_leads=load_sequence(config.knowledge_excluded_leads),
            target_plan=target_plan,
            understanding_profile=(
                load_mapping(config.understanding_profile)
                if config.understanding_profile is not None
                else None
            ),
            product_head=product_head,
            product_repo=config.product_cwd,
            automation_root=Path.cwd(),
        )
        data = report.to_dict()
        if (
            config.enforce_knowledge_gate
            and config.target_plan is None
            and data.get("status") != "fail"
        ):
            return _knowledge_audit_failure(
                "knowledge_gate_missing_inputs",
                "missing required knowledge gate inputs: target_plan",
                next_step_category="add-code-link",
            )
        return data
    except Exception as exc:
        return _knowledge_audit_failure(
            "knowledge_audit_error",
            str(exc),
            next_step_category="add-code-link",
        )


def _knowledge_audit_failure(kind: str, detail: str, *, next_step_category: str) -> Dict[str, Any]:
    return {
        "status": "fail",
        "selection_policy": ["L2", "L3", "L1", "L4-suggestion"],
        "mutation_testing_default": "disabled",
        "counts": {},
        "gaps": {},
        "blockers": [{"kind": kind, "detail": detail}],
        "next_step_category": next_step_category,
    }


def _git_product_head(product_cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(product_cwd), "rev-parse", "--short", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return proc.stdout.strip()


def _run_and_record(
    command: Sequence[str],
    *,
    path_prefix: Path,
    run_command: RunCommand,
    timeout_sec: int,
    env: Optional[Mapping[str, str]] = None,
    timeout_json: Mapping[str, Any],
) -> Dict[str, Any]:
    started_monotonic = time.monotonic()
    try:
        run_kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "shell": False,
            "check": False,
            "timeout": timeout_sec,
        }
        if env is not None:
            subprocess_env = os.environ.copy()
            subprocess_env.update(env)
            run_kwargs["env"] = subprocess_env
        proc = run_command(
            list(command),
            **run_kwargs,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = int(proc.returncode)
        parsed = _parse_json(stdout)
        if returncode != 0 and parsed is None:
            parsed = _non_json_failure_payload(timeout_json, returncode=returncode, path_prefix=path_prefix)
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_process_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        stderr = _coerce_process_text(getattr(exc, "stderr", None))
        timeout_message = f"orchestrator timed out after {timeout_sec}s"
        stderr = f"{stderr.rstrip()}\n{timeout_message}\n" if stderr else f"{timeout_message}\n"
        returncode = 124
        parsed = dict(timeout_json)
    duration_sec = round(max(0.0, time.monotonic() - started_monotonic), 6)
    path_prefix.with_suffix(".stdout").write_text(stdout, encoding="utf-8")
    path_prefix.with_suffix(".stderr").write_text(stderr, encoding="utf-8")
    if parsed is not None:
        path_prefix.with_suffix(".json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "command": list(command),
        "returncode": returncode,
        "stdout_path": str(path_prefix.with_suffix(".stdout")),
        "stderr_path": str(path_prefix.with_suffix(".stderr")),
        "json_path": str(path_prefix.with_suffix(".json")) if parsed is not None else "",
        "json": parsed or {},
        "duration_sec": duration_sec,
    }


def _non_json_failure_payload(
    fallback: Mapping[str, Any],
    *,
    returncode: int,
    path_prefix: Path,
) -> Dict[str, Any]:
    parsed: Dict[str, Any] = dict(fallback)
    artifact = path_prefix.name
    status = str(parsed.get("status") or "")
    label = artifact.replace("-", " ")
    if artifact.startswith("pre-batch-hook"):
        label = "pre-batch hook"
    detail = (
        f"{label} failed with exit code {returncode} without JSON output; "
        "inspect saved stdout/stderr artifacts"
    )
    if status == "not-ready":
        checks = [dict(item) for item in parsed.get("checks", []) if isinstance(item, Mapping)]
        checks.append(
            {
                "name": f"loop_runner.{artifact.replace('-', '_')}_non_json",
                "status": "fail",
                "detail": detail,
            }
        )
        parsed["checks"] = checks
        parsed["failed_count"] = max(1, int(parsed.get("failed_count") or 0))
        return parsed
    parsed["status"] = "environment-event"
    parsed["environment_unavailable"] = True
    reasons = [detail]
    reasons.extend(str(item) for item in parsed.get("environment_reasons", []) if str(item))
    parsed["environment_reasons"] = reasons
    parsed.setdefault("reported_count", 0)
    parsed.setdefault("observed_count", 0)
    parsed.setdefault("signal_count", 0)
    return parsed


def _coerce_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_json(stdout: str) -> Optional[Mapping[str, Any]]:
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


def _write_summary(
    *,
    run_dir: Path,
    run_id: str,
    started_at: str,
    adapter_metadata: Mapping[str, Any],
    rounds: Sequence[Mapping[str, Any]],
    stop_reason: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> Path:
    round_stats = [_round_stat(item) for item in rounds]
    status_counts = _summary_status_counts(round_stats)
    major_blocker = _major_blocker(rounds)
    summary = {
        "run_id": run_id,
        "profile": str(adapter_metadata.get("profile") or "unknown"),
        "profile_scope": str(adapter_metadata.get("profile_scope") or "unspecified"),
        "profile_limitations": _string_list(adapter_metadata.get("profile_limitations")),
        "spec_count": int(adapter_metadata.get("spec_count") or 0),
        "profile_spec_count": int(adapter_metadata.get("spec_count") or 0),
        "semantic_entry_count": int(adapter_metadata.get("semantic_entry_count") or 0),
        "hit_semantic_entries": _string_list(adapter_metadata.get("semantic_entry_ids")),
        "planned_semantic_entries": [],
        "planned_oracle_levels": [],
        "oracle_ran": _oracle_levels_ran(rounds),
        "started_at": started_at,
        "updated_at": _utc_timestamp(),
        "round_count": len(rounds),
        "not_ready_count": sum(1 for item in rounds if item.get("status") == "not-ready"),
        "environment_event_count": status_counts["environment-event"],
        "processed_count": sum(1 for item in rounds if item.get("status") == "processed"),
        "executed_count": sum(int(item.get("executed") or 0) for item in round_stats),
        "skipped_count": sum(int(item.get("skipped") or 0) for item in round_stats),
        "reported_total": sum(int(item.get("reported_count") or 0) for item in rounds),
        "observed_total": sum(int(item.get("observed_count") or 0) for item in rounds),
        "signal_total": sum(int(item.get("signal_count") or 0) for item in rounds),
        "status_counts": status_counts,
        "round_stats": round_stats,
        "major_blocker": major_blocker,
        "next_step_category": _next_step_category(rounds, round_stats, major_blocker),
        "rounds": list(rounds),
    }
    profile_description = str(adapter_metadata.get("profile_description") or "").strip()
    if profile_description:
        summary["profile_description"] = profile_description
    knowledge_audit = adapter_metadata.get("knowledge_audit")
    if isinstance(knowledge_audit, Mapping):
        summary["knowledge_audit"] = dict(knowledge_audit)
        counts = knowledge_audit.get("counts")
        if isinstance(counts, Mapping):
            summary["semantic_entry_count"] = int(counts.get("semantic_entry_count") or 0)
            for field in (
                "target_plan_l2_count",
                "target_plan_l3_count",
                "target_plan_l1_count",
                "target_plan_gap_count",
            ):
                summary[field] = int(counts.get(field) or 0)
            if int(counts.get("target_plan_l2_count") or 0):
                summary["spec_count"] = int(counts.get("target_plan_l2_count") or 0)
        target_summary = knowledge_audit.get("target_summary")
        if isinstance(target_summary, Mapping):
            target_entries = _string_list(target_summary.get("semantic_entry_ids"))
            if target_entries:
                summary["hit_semantic_entries"] = target_entries
            planned_oracles = _string_list(target_summary.get("oracle_levels"))
            summary["planned_semantic_entries"] = target_entries
            summary["planned_oracle_levels"] = planned_oracles
            summary["coverage_gap_count"] = int(target_summary.get("coverage_gap_count") or 0)
            summary["target_scope_semantic_entry_count"] = len(target_entries)
            summary["target_scope_oracle_levels"] = planned_oracles
            summary["target_scope_gap_count"] = int(target_summary.get("coverage_gap_count") or 0)
        else:
            summary["coverage_gap_count"] = 0
            summary["target_scope_semantic_entry_count"] = 0
            summary["target_scope_oracle_levels"] = []
            summary["target_scope_gap_count"] = 0
        blockers = knowledge_audit.get("blockers")
        if isinstance(blockers, list):
            summary["blockers"] = [dict(item) for item in blockers if isinstance(item, Mapping)]
        summary["evidence_chain"] = _summary_evidence_chain(knowledge_audit)
        product_understanding = knowledge_audit.get("product_understanding")
        if isinstance(product_understanding, list):
            gaps = [
                str(item.get("id") or "")
                for item in product_understanding
                if isinstance(item, Mapping)
                and item.get("status") != "covered"
                and str(item.get("id") or "")
            ]
            summary["product_understanding_gap_count"] = len(gaps)
            summary["product_understanding_gaps"] = gaps
            summary["profile_understanding_gap_count"] = len(gaps)
            summary["profile_understanding_gaps"] = gaps
            summary["understanding_gap_scope"] = "profile-adjusted"
        if knowledge_audit.get("status") == "fail":
            summary["next_step_category"] = str(
                knowledge_audit.get("next_step_category") or "add-code-link"
            )
    if stop_reason:
        summary["stop_reason"] = stop_reason
    if completed_at:
        summary["completed_at"] = completed_at
    path = run_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _summary_evidence_chain(knowledge_audit: Mapping[str, Any]) -> List[Dict[str, Any]]:
    product_understanding = knowledge_audit.get("product_understanding")
    if not isinstance(product_understanding, list):
        return []
    chains: List[Dict[str, Any]] = []
    for item in product_understanding:
        if not isinstance(item, Mapping):
            continue
        evidence_anchor_counts = item.get("evidence_anchor_counts")
        if not isinstance(evidence_anchor_counts, Mapping):
            evidence_anchor_counts = {}
        chain = {
            "domain": str(item.get("id") or ""),
            "status": str(item.get("status") or ""),
            "evidence_anchor_counts": dict(evidence_anchor_counts),
            "oracle_levels": _string_list(item.get("oracle_levels")),
            "owner_gated": bool(item.get("owner_gated")),
            "runtime_gated": bool(item.get("runtime_gated")),
        }
        notes = str(item.get("notes") or "").strip()
        if notes:
            chain["notes"] = notes
        chains.append(chain)
    return chains


def _oracle_levels_ran(rounds: Sequence[Mapping[str, Any]]) -> List[str]:
    levels: set[str] = set()
    for round_record in rounds:
        batch = round_record.get("batch")
        if not isinstance(batch, Mapping):
            continue
        payload = batch.get("json")
        if not isinstance(payload, Mapping):
            continue
        for level in _string_list(payload.get("oracle_levels")):
            if level != "L4-suggestion":
                levels.add(level)
    return [level for level in ["L1", "L2", "L3"] if level in levels]


def _round_stat(round_record: Mapping[str, Any]) -> Dict[str, Any]:
    status = str(round_record.get("status") or "unknown")
    executed = 1 if "batch" in round_record else 0
    skipped = 0 if executed else 1
    reported_count = int(round_record.get("reported_count") or 0)
    observed_count = int(round_record.get("observed_count") or 0)
    signal_count = int(round_record.get("signal_count") or 0)
    has_environment_event = _round_environment_event_detail(round_record) is not None
    return {
        "round": int(round_record.get("round") or 0),
        "status": status,
        "executed": executed,
        "skipped": skipped,
        "not_ready": 1 if status == "not-ready" else 0,
        "environment_event": 1 if has_environment_event else 0,
        "reported": reported_count,
        "observed": observed_count,
        "signals": signal_count,
    }


def _summary_status_counts(round_stats: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {
        "environment-event": sum(int(item.get("environment_event") or 0) for item in round_stats),
        "not-ready": sum(int(item.get("not_ready") or 0) for item in round_stats),
        "observed": sum(1 for item in round_stats if int(item.get("observed") or 0) > 0),
        "processed": sum(1 for item in round_stats if item.get("status") == "processed"),
        "reported": sum(1 for item in round_stats if int(item.get("reported") or 0) > 0),
        "skipped": sum(int(item.get("skipped") or 0) for item in round_stats),
    }
    adapter_errors = sum(1 for item in round_stats if item.get("status") == "adapter-error")
    if adapter_errors:
        counts["adapter-error"] = adapter_errors
    return counts


def _major_blocker(rounds: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    counts: Dict[tuple[str, str], int] = {}
    for round_record in rounds:
        fingerprint = _blocker_fingerprint(round_record)
        if fingerprint is None:
            continue
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
    if not counts:
        return None
    (status, detail), count = max(counts.items(), key=lambda item: item[1])
    return {"status": status, "detail": detail, "count": count}


def _next_step_category(
    rounds: Sequence[Mapping[str, Any]],
    round_stats: Sequence[Mapping[str, Any]],
    major_blocker: Optional[Mapping[str, Any]],
) -> str:
    if not rounds:
        return "run-targeted-mining"
    if major_blocker is not None:
        status = str(major_blocker.get("status") or "")
        if status in {"not-ready", "environment-event"}:
            return "runtime-preflight"
        if status == "adapter-error":
            detail = str(major_blocker.get("detail") or "")
            if "target plan" in detail or "non-executable" in detail:
                return "add-l3-oracle"
            return "add-code-link"
    if any(int(item.get("reported") or 0) > 0 for item in round_stats):
        return "run-targeted-mining"
    if any(int(item.get("observed") or 0) > 0 or int(item.get("signals") or 0) > 0 for item in round_stats):
        return "run-targeted-mining"
    return "run-targeted-mining"


def _repeated_blocker_stop_reason(rounds: Sequence[Mapping[str, Any]], threshold: Optional[int]) -> Optional[str]:
    if threshold is None or threshold <= 0 or len(rounds) < threshold:
        return None
    window = list(rounds[-threshold:])
    fingerprints = [_blocker_fingerprint(item) for item in window]
    first = fingerprints[0]
    if first is None or any(item != first for item in fingerprints):
        return None
    status, detail = first
    return f"{threshold} consecutive {status} blockers: {detail}"


def _blocker_fingerprint(round_record: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    status = str(round_record.get("status") or "")
    if status == "not-ready":
        preflight_json = _record_json(round_record.get("preflight"))
        failed_checks = [
            f"{item.get('name', '')}:{item.get('detail', '')}"
            for item in preflight_json.get("checks", [])
            if item.get("status") == "fail"
        ]
        detail = "|".join(sorted(failed_checks)) or status
        return status, detail
    environment_detail = _round_environment_event_detail(round_record)
    if environment_detail is not None:
        return "environment-event", environment_detail
    if status == "adapter-error":
        batch_json = _record_json(round_record.get("batch"))
        detail = str(batch_json.get("error") or "").strip() or "adapter-error"
        return status, detail
    return None


def _round_environment_event_detail(round_record: Mapping[str, Any]) -> Optional[str]:
    if str(round_record.get("status") or "") != "environment-event":
        batch_json = _record_json(round_record.get("batch"))
        if not bool(batch_json.get("environment_event")):
            return None

    reasons: List[str] = []
    for record_name in ("batch", "pre_batch_hook"):
        record_json = _record_json(round_record.get(record_name))
        reasons.extend(str(item) for item in record_json.get("environment_reasons", []) if str(item))
        if reasons:
            break
    if reasons:
        return "|".join(sorted(reasons))

    batch_json = _record_json(round_record.get("batch"))
    if batch_json:
        observed = int(batch_json.get("observed_count") or round_record.get("observed_count") or 0)
        signals = int(batch_json.get("signal_count") or round_record.get("signal_count") or 0)
        reported = int(batch_json.get("reported_count") or round_record.get("reported_count") or 0)
        return f"batch marked environment_event without reasons: signals={signals} observed={observed} reported={reported}"
    return "environment-event"


def _record_json(record: Any) -> Mapping[str, Any]:
    if not isinstance(record, Mapping):
        return {}
    data = record.get("json")
    return data if isinstance(data, Mapping) else {}


def _should_start_next_round(
    *,
    round_index: int,
    max_rounds: Optional[int],
    now: NowFn,
    deadline: float,
) -> bool:
    if max_rounds is not None and round_index >= max_rounds:
        return False
    return now() < deadline or max_rounds is not None


def _sleep_before_next_round(
    *,
    round_index: int,
    max_rounds: Optional[int],
    now: NowFn,
    deadline: float,
    sleep_sec: float,
    sleep: SleepFn,
) -> None:
    if max_rounds is not None and round_index >= max_rounds:
        return
    remaining = deadline - now()
    if max_rounds is None and remaining <= 0:
        return
    if sleep_sec <= 0:
        return
    sleep(min(sleep_sec, remaining) if max_rounds is None else sleep_sec)


def _timestamp_run_id() -> str:
    return datetime.now(timezone.utc).strftime("pv2-loop-%Y%m%dT%H%M%SZ")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="pipeline-v2 loop runner")
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--product-cwd", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path(".tmp/pv2/loops"))
    parser.add_argument("--run-id")
    parser.add_argument("--executor", default="playwright")
    parser.add_argument("--semantic-map", type=Path)
    parser.add_argument("--code-map", type=Path)
    parser.add_argument("--knowledge-cases", type=Path)
    parser.add_argument("--knowledge-excluded-leads", type=Path)
    parser.add_argument("--understanding-profile", type=Path)
    parser.add_argument("--target-plan", type=Path)
    parser.add_argument("--product-head", default="")
    parser.add_argument("--hosts-file", type=Path, default=Path("/etc/hosts"))
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--duration-sec", type=float)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--sleep-sec", type=float, default=60.0)
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--stop-on-not-ready", action="store_true", default=True)
    parser.add_argument("--continue-on-not-ready", dest="stop_on_not_ready", action="store_false")
    parser.add_argument("--stop-after-consecutive-blockers", type=int)
    parser.add_argument("--environment-retry-count", type=int, default=1)
    parser.add_argument("--pre-batch-hook", type=Path)
    parser.add_argument("--pre-batch-hook-timeout-sec", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    if args.dry_run and not args.report:
        parser.error("--dry-run requires --report")
    duration_sec = args.duration_sec if args.duration_sec is not None else args.hours * 60 * 60
    result = run_loop(
        LoopConfig(
            adapter=args.adapter,
            product_cwd=args.product_cwd,
            output_root=args.output_root,
            run_id=args.run_id,
            executor=args.executor,
            semantic_map=args.semantic_map,
            code_map=args.code_map,
            knowledge_cases=args.knowledge_cases,
            knowledge_excluded_leads=args.knowledge_excluded_leads,
            understanding_profile=args.understanding_profile,
            target_plan=args.target_plan,
            product_head=args.product_head,
            hosts_file=args.hosts_file,
            duration_sec=duration_sec,
            max_rounds=args.max_rounds,
            sleep_sec=args.sleep_sec,
            timeout_sec=args.timeout_sec,
            preflight=not args.no_preflight,
            stop_on_not_ready=args.stop_on_not_ready,
            stop_after_consecutive_blockers=args.stop_after_consecutive_blockers,
            environment_retry_count=args.environment_retry_count,
            pre_batch_hook=args.pre_batch_hook,
            pre_batch_hook_timeout_sec=args.pre_batch_hook_timeout_sec,
            dry_run=args.dry_run,
            report=args.report,
        )
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "run_dir": str(result.run_dir),
                "summary_path": str(result.summary_path),
                "round_count": len(result.rounds),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
