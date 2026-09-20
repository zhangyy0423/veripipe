"""Dependency-free MCP stdio server exposing the pipeline_v2 tools.

Implements the Model Context Protocol stdio transport: newline-delimited
JSON-RPC 2.0 on stdin/stdout, one message per line with no embedded newlines,
and human-readable logs on stderr. Supported methods: ``initialize``,
``notifications/initialized``, ``ping``, ``tools/list``, ``tools/call``.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Dict, Optional

from . import __version__, tools as tools_module

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {
    "name": "veripipe-pipeline-v2",
    "title": "veripipe pipeline-v2 verification tools",
    "version": __version__,
}
INSTRUCTIONS = (
    "Tools wrap the product-neutral pipeline_v2 verification core. run_batch and "
    "run_loop default to offline dry-runs; the core never confirms a bug via an "
    "LLM and writes publication intent to a queue instead of pushing."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _result(message_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, structured: Optional[Dict[str, Any]], is_error: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}
    if isinstance(structured, dict):
        result["structuredContent"] = structured
    return result


def handle_message(message: Any, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a JSON-RPC response dict, or None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        message_id = message.get("id") if isinstance(message, dict) else None
        return _error(message_id, INVALID_REQUEST, "invalid JSON-RPC 2.0 message")

    method = message.get("method")
    message_id = message.get("id")
    is_notification = "id" not in message

    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        state["initialized"] = False
        return _result(
            message_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
        )

    if method == "notifications/initialized":
        state["initialized"] = True
        return None

    if method == "ping":
        return _result(message_id, {})

    if method == "tools/list":
        return _result(message_id, {"tools": tools_module.list_tools()})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(name, str):
            return _error(message_id, INVALID_PARAMS, "tools/call requires a string 'name'")
        if not isinstance(arguments, dict):
            return _error(message_id, INVALID_PARAMS, "tools/call 'arguments' must be an object")
        try:
            payload = tools_module.call_tool(name, arguments)
        except tools_module.ToolError as exc:
            return _result(message_id, _tool_result(str(exc), None, True))
        except Exception as exc:  # keep the transport alive on handler failure
            return _result(message_id, _tool_result("tool execution failed: %s" % exc, None, True))
        return _result(
            message_id,
            _tool_result(payload["text"], payload.get("structured"), payload["isError"]),
        )

    if is_notification:
        return None
    return _error(message_id, METHOD_NOT_FOUND, "method not found: %s" % method)


def _write(stream: IO[str], obj: Dict[str, Any]) -> None:
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def serve(stdin: IO[str], stdout: IO[str]) -> None:
    state: Dict[str, Any] = {"initialized": False}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, _error(None, PARSE_ERROR, "parse error"))
            continue
        if isinstance(message, list):
            for item in message:
                response = handle_message(item, state)
                if response is not None:
                    _write(stdout, response)
            continue
        response = handle_message(message, state)
        if response is not None:
            _write(stdout, response)


def main(argv: Optional[list] = None) -> int:
    try:
        serve(sys.stdin, sys.stdout)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0
