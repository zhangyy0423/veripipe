"""Transport-independent tool registry for the pipeline_v2 MCP server.

``list_tools()`` returns MCP tool descriptors; ``call_tool(name, arguments)``
executes a tool and returns ``{"isError": bool, "text": str, "structured":
dict|None}``. The server module maps these onto JSON-RPC over stdio.

Every tool that mutates or inspects a product runs the matching pipeline_v2
CLI in a fresh subprocess, reusing the module's own fail-closed argument
validation. ``query_ledger`` opens an existing SQLite ledger read-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent  # .../pipeline-v2/scripts
DEFAULT_TIMEOUT_SEC = 1800
AUDIT_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 3600


class ToolError(Exception):
    """Invalid tool arguments; surfaced to the caller as a tool error result."""


class UnknownToolError(ToolError):
    """Requested tool name is not registered."""


def _child_env() -> Dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    scripts = str(SCRIPTS_ROOT)
    env["PYTHONPATH"] = scripts + (os.pathsep + existing if existing else "")
    return env


def _run_module(module: str, argv: List[str], timeout: int) -> Dict[str, Any]:
    command = [sys.executable, "-m", "pipeline_v2." + module, *argv]
    try:
        proc = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=_child_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "isError": True,
            "text": "pipeline_v2.%s timed out after %ss" % (module, timeout),
            "structured": {"module": module, "status": "timeout", "timeout_sec": timeout},
        }
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed: Optional[Any]
    try:
        parsed = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    text = stdout.strip() or stderr.strip() or (
        "pipeline_v2.%s exited with code %d" % (module, proc.returncode)
    )
    structured: Dict[str, Any] = {"module": module, "returncode": proc.returncode}
    if parsed is not None:
        structured["result"] = parsed
    if stderr.strip():
        structured["stderr"] = stderr.strip()
    return {"isError": proc.returncode != 0, "text": text, "structured": structured}


def _require_str(args: Dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("'%s' is required and must be a non-empty string" % field)
    return value


def _opt_str(args: Dict[str, Any], field: str) -> Optional[str]:
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("'%s' must be a string" % field)
    return value


def _opt(argv: List[str], flag: str, value: Optional[str]) -> None:
    if value is not None:
        argv.extend([flag, value])


def _opt_int(argv: List[str], flag: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError("'%s' expects an integer" % flag)
    argv.extend([flag, str(value)])


def _opt_num(argv: List[str], flag: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("'%s' expects a number" % flag)
    argv.extend([flag, str(value)])


def _bool(args: Dict[str, Any], field: str, default: bool) -> bool:
    value = args.get(field, default)
    if not isinstance(value, bool):
        raise ToolError("'%s' must be a boolean" % field)
    return value


def _call_timeout(args: Dict[str, Any], default: int) -> int:
    value = args.get("call_timeout_sec")
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > MAX_TIMEOUT_SEC:
        raise ToolError("'call_timeout_sec' must be a positive integer <= %d" % MAX_TIMEOUT_SEC)
    return value


def _tool_preflight(args: Dict[str, Any]) -> Dict[str, Any]:
    executor = args.get("executor", "playwright")
    if executor not in ("playwright", "http"):
        raise ToolError("'executor' must be 'playwright' or 'http'")
    argv = ["--preflight", "--executor", executor, "--adapter", _require_str(args, "adapter")]
    _opt(argv, "--product-cwd", _opt_str(args, "product_cwd"))
    _opt(argv, "--semantic-map", _opt_str(args, "semantic_map"))
    _opt(argv, "--target-plan", _opt_str(args, "target_plan"))
    _opt(argv, "--output-dir", _opt_str(args, "output_dir"))
    _opt(argv, "--hosts-file", _opt_str(args, "hosts_file"))
    return _run_module("orchestrator", argv, _call_timeout(args, AUDIT_TIMEOUT_SEC))


def _tool_run_batch(args: Dict[str, Any]) -> Dict[str, Any]:
    executor = args.get("executor", "mock")
    if executor not in ("mock", "playwright", "http", "l1-fuzz"):
        raise ToolError("'executor' must be one of mock, playwright, http, l1-fuzz")
    argv = ["--executor", executor]
    if _bool(args, "dry_run", True):
        argv.append("--dry-run")
    _opt(argv, "--adapter", _opt_str(args, "adapter"))
    _opt(argv, "--product-cwd", _opt_str(args, "product_cwd"))
    _opt(argv, "--semantic-map", _opt_str(args, "semantic_map"))
    _opt(argv, "--target-plan", _opt_str(args, "target_plan"))
    _opt(argv, "--report", _opt_str(args, "report"))
    _opt(argv, "--ledger", _opt_str(args, "ledger"))
    _opt(argv, "--queue", _opt_str(args, "queue"))
    _opt(argv, "--brief", _opt_str(args, "brief"))
    _opt(argv, "--output-dir", _opt_str(args, "output_dir"))
    _opt(argv, "--batch-id", _opt_str(args, "batch_id"))
    _opt(argv, "--product-version", _opt_str(args, "product_version"))
    _opt(argv, "--environment-digest", _opt_str(args, "environment_digest"))
    _opt_int(argv, "--timeout-sec", args.get("timeout_sec"))
    return _run_module("orchestrator", argv, _call_timeout(args, DEFAULT_TIMEOUT_SEC))


def _tool_run_loop(args: Dict[str, Any]) -> Dict[str, Any]:
    argv = [
        "--adapter", _require_str(args, "adapter"),
        "--product-cwd", _require_str(args, "product_cwd"),
    ]
    _opt(argv, "--executor", _opt_str(args, "executor"))
    if _bool(args, "dry_run", True):
        argv.append("--dry-run")
    _opt(argv, "--report", _opt_str(args, "report"))
    _opt(argv, "--semantic-map", _opt_str(args, "semantic_map"))
    _opt(argv, "--code-map", _opt_str(args, "code_map"))
    _opt(argv, "--target-plan", _opt_str(args, "target_plan"))
    _opt(argv, "--output-root", _opt_str(args, "output_root"))
    _opt(argv, "--run-id", _opt_str(args, "run_id"))
    _opt_int(argv, "--max-rounds", args.get("max_rounds"))
    _opt_num(argv, "--duration-sec", args.get("duration_sec"))
    _opt_num(argv, "--sleep-sec", args.get("sleep_sec"))
    _opt_int(argv, "--timeout-sec", args.get("timeout_sec"))
    if _bool(args, "no_preflight", False):
        argv.append("--no-preflight")
    return _run_module("loop_runner", argv, _call_timeout(args, DEFAULT_TIMEOUT_SEC))


def _tool_knowledge_audit(args: Dict[str, Any]) -> Dict[str, Any]:
    argv = [
        "--semantic-map", _require_str(args, "semantic_map"),
        "--code-map", _require_str(args, "code_map"),
        "--adapter", _require_str(args, "adapter"),
    ]
    _opt(argv, "--cases", _opt_str(args, "cases"))
    _opt(argv, "--excluded-leads", _opt_str(args, "excluded_leads"))
    _opt(argv, "--target-plan", _opt_str(args, "target_plan"))
    _opt(argv, "--understanding-profile", _opt_str(args, "understanding_profile"))
    _opt(argv, "--product-head", _opt_str(args, "product_head"))
    _opt(argv, "--product-repo", _opt_str(args, "product_repo"))
    fmt = _opt_str(args, "format")
    if fmt is not None:
        if fmt not in ("json", "markdown"):
            raise ToolError("'format' must be 'json' or 'markdown'")
        argv.extend(["--format", fmt])
    return _run_module("knowledge_audit", argv, _call_timeout(args, AUDIT_TIMEOUT_SEC))


def _tool_code_map_validate(args: Dict[str, Any]) -> Dict[str, Any]:
    argv = ["validate", "--code-map", _require_str(args, "code_map")]
    _opt(argv, "--semantic-map", _opt_str(args, "semantic_map"))
    return _run_module("code_map", argv, _call_timeout(args, AUDIT_TIMEOUT_SEC))


def _tool_change_impact(args: Dict[str, Any]) -> Dict[str, Any]:
    argv = [
        "--code-map", _require_str(args, "code_map"),
        "--adapter", _require_str(args, "adapter"),
    ]
    _opt(argv, "--product-repo", _opt_str(args, "product_repo"))
    _opt(argv, "--anchor", _opt_str(args, "anchor"))
    _opt(argv, "--head", _opt_str(args, "head"))
    changed = args.get("changed_file", []) or []
    if not isinstance(changed, list):
        raise ToolError("'changed_file' must be an array of strings")
    for item in changed:
        if not isinstance(item, str):
            raise ToolError("'changed_file' entries must be strings")
        argv.extend(["--changed-file", item])
    fmt = _opt_str(args, "format")
    if fmt is not None:
        if fmt not in ("json", "markdown"):
            raise ToolError("'format' must be 'json' or 'markdown'")
        argv.extend(["--format", fmt])
    return _run_module("change_impact", argv, _call_timeout(args, AUDIT_TIMEOUT_SEC))


def _tool_knowledge_export(args: Dict[str, Any]) -> Dict[str, Any]:
    argv = [
        "--semantic-map", _require_str(args, "semantic_map"),
        "--code-map", _require_str(args, "code_map"),
        "--output-dir", _require_str(args, "output_dir"),
    ]
    _opt(argv, "--cases", _opt_str(args, "cases"))
    _opt(argv, "--excluded-leads", _opt_str(args, "excluded_leads"))
    return _run_module("knowledge_export", argv, _call_timeout(args, AUDIT_TIMEOUT_SEC))


def _tool_query_ledger(args: Dict[str, Any]) -> Dict[str, Any]:
    op = _require_str(args, "op")
    ledger_path = Path(_require_str(args, "ledger"))
    if not ledger_path.is_file():
        return {
            "isError": True,
            "text": "ledger not found: %s" % ledger_path,
            "structured": {"op": op, "status": "not-found"},
        }
    from pipeline_v2.ledger import Ledger  # lazy: SCRIPTS_ROOT is importable

    ledger = Ledger(ledger_path)
    try:
        if op == "recent_batches":
            limit = args.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ToolError("'limit' must be a positive integer")
            data: Any = ledger.recent_batches(limit)
        elif op == "all_batches":
            data = ledger.all_batches()
        elif op == "list_cases":
            data = ledger.list_cases(status=_opt_str(args, "status"))
        elif op == "get_batch":
            data = ledger.get_batch(_require_str(args, "batch_id"))
        elif op == "get_case":
            data = ledger.get_case(_require_str(args, "fingerprint"))
        elif op == "list_reported_cases_for_batch":
            data = ledger.list_reported_cases_for_batch(_require_str(args, "batch_id"))
        elif op == "list_hits_for_batch":
            data = ledger.list_hits_for_batch(_require_str(args, "batch_id"))
        elif op == "count_observing_cases":
            data = {"count": ledger.count_observing_cases()}
        else:
            raise ToolError("unknown ledger op: %s" % op)
    finally:
        ledger.close()
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return {"isError": False, "text": text, "structured": {"op": op, "result": data}}


_LEDGER_OPS = (
    "recent_batches",
    "all_batches",
    "list_cases",
    "get_batch",
    "get_case",
    "list_reported_cases_for_batch",
    "list_hits_for_batch",
    "count_observing_cases",
)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "preflight",
        "title": "Adapter preflight",
        "description": (
            "Run read-only adapter preflight checks (Playwright browser bundle or "
            "HTTP driver reachability) without creating ledger/queue/brief files."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "adapter": {"type": "string", "description": "Path to products/<product>/adapter.config.json"},
                "executor": {"type": "string", "enum": ["playwright", "http"], "default": "playwright"},
                "product_cwd": {"type": "string", "description": "Read-only product checkout"},
                "semantic_map": {"type": "string"},
                "target_plan": {"type": "string"},
                "output_dir": {"type": "string"},
                "hosts_file": {"type": "string"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["adapter"],
        },
        "handler": _tool_preflight,
    },
    {
        "name": "run_batch",
        "title": "Run verification batch",
        "description": (
            "Run a single orchestrator batch through the process bus. Defaults to a "
            "safe offline dry-run with the mock executor; set executor and dry_run for "
            "adapter-driven runs. Publication intent is written to the queue, never pushed."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "executor": {"type": "string", "enum": ["mock", "playwright", "http", "l1-fuzz"], "default": "mock"},
                "dry_run": {"type": "boolean", "default": True},
                "adapter": {"type": "string"},
                "product_cwd": {"type": "string"},
                "semantic_map": {"type": "string"},
                "target_plan": {"type": "string"},
                "report": {"type": "string", "description": "Playwright JSON reporter fixture for dry-run"},
                "ledger": {"type": "string"},
                "queue": {"type": "string"},
                "brief": {"type": "string"},
                "output_dir": {"type": "string"},
                "batch_id": {"type": "string"},
                "product_version": {"type": "string"},
                "environment_digest": {"type": "string"},
                "timeout_sec": {"type": "integer"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
        },
        "handler": _tool_run_batch,
    },
    {
        "name": "run_loop",
        "title": "Run verification loop",
        "description": (
            "Run the iterative loop runner across multiple rounds. Requires an adapter "
            "and product checkout; defaults to a dry-run that replays a Playwright JSON "
            "report fixture. Bound the loop with max_rounds or duration_sec."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "adapter": {"type": "string"},
                "product_cwd": {"type": "string"},
                "executor": {"type": "string", "default": "playwright"},
                "dry_run": {"type": "boolean", "default": True},
                "report": {"type": "string"},
                "semantic_map": {"type": "string"},
                "code_map": {"type": "string"},
                "target_plan": {"type": "string"},
                "output_root": {"type": "string"},
                "run_id": {"type": "string"},
                "max_rounds": {"type": "integer"},
                "duration_sec": {"type": "number"},
                "sleep_sec": {"type": "number"},
                "timeout_sec": {"type": "integer"},
                "no_preflight": {"type": "boolean", "default": False},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["adapter", "product_cwd"],
        },
        "handler": _tool_run_loop,
    },
    {
        "name": "knowledge_audit",
        "title": "Audit knowledge inputs",
        "description": (
            "Audit whether the semantic map, code map, adapter, and optional target "
            "plan are sufficient to drive mining. Returns a structured report."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "semantic_map": {"type": "string"},
                "code_map": {"type": "string"},
                "adapter": {"type": "string"},
                "cases": {"type": "string"},
                "excluded_leads": {"type": "string"},
                "target_plan": {"type": "string"},
                "understanding_profile": {"type": "string"},
                "product_head": {"type": "string"},
                "product_repo": {"type": "string"},
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["semantic_map", "code_map", "adapter"],
        },
        "handler": _tool_knowledge_audit,
    },
    {
        "name": "code_map_validate",
        "title": "Validate code map",
        "description": "Validate a product-neutral code-map.yaml, optionally cross-checking semantic-map entry ids.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code_map": {"type": "string"},
                "semantic_map": {"type": "string"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["code_map"],
        },
        "handler": _tool_code_map_validate,
    },
    {
        "name": "change_impact",
        "title": "Compute change impact",
        "description": (
            "Map changed product files to affected code-map anchors and executable "
            "scenarios. Supply changed_file entries for offline use, or product_repo "
            "plus anchor/head to derive them from git."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code_map": {"type": "string"},
                "adapter": {"type": "string"},
                "product_repo": {"type": "string"},
                "anchor": {"type": "string"},
                "head": {"type": "string"},
                "changed_file": {"type": "array", "items": {"type": "string"}},
                "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["code_map", "adapter"],
        },
        "handler": _tool_change_impact,
    },
    {
        "name": "knowledge_export",
        "title": "Export knowledge bundle",
        "description": "Export a knowledge/ bundle from the semantic map, code map, and optional ledger cases.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "semantic_map": {"type": "string"},
                "code_map": {"type": "string"},
                "output_dir": {"type": "string"},
                "cases": {"type": "string"},
                "excluded_leads": {"type": "string"},
                "call_timeout_sec": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
            },
            "required": ["semantic_map", "code_map", "output_dir"],
        },
        "handler": _tool_knowledge_export,
    },
    {
        "name": "query_ledger",
        "title": "Query verification ledger",
        "description": "Read an existing SQLite ledger (read-only). Choose an op to list batches, cases, or hits.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ledger": {"type": "string", "description": "Path to an existing ledger .sqlite file"},
                "op": {"type": "string", "enum": list(_LEDGER_OPS)},
                "limit": {"type": "integer", "minimum": 1, "description": "recent_batches limit"},
                "status": {"type": "string", "description": "list_cases status filter"},
                "batch_id": {"type": "string"},
                "fingerprint": {"type": "string"},
            },
            "required": ["ledger", "op"],
        },
        "handler": _tool_query_ledger,
    },
]

_TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}


def list_tools() -> List[Dict[str, Any]]:
    """Return MCP tool descriptors (without internal handler references)."""
    return [{key: value for key, value in tool.items() if key != "handler"} for tool in TOOLS]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a registered tool and return its result payload."""
    tool = _TOOL_INDEX.get(name)
    if tool is None:
        raise UnknownToolError("unknown tool: %s" % name)
    if not isinstance(arguments, dict):
        raise ToolError("tool arguments must be an object")
    return tool["handler"](arguments)
