import io
import json
import tempfile
import unittest
from pathlib import Path

from pipeline_v2_mcp import server, tools


EXPECTED_TOOLS = {
    "preflight",
    "run_batch",
    "run_loop",
    "knowledge_audit",
    "code_map_validate",
    "change_impact",
    "knowledge_export",
    "query_ledger",
}


def _fresh_state():
    return {"initialized": False}


class HandshakeTest(unittest.TestCase):
    def test_initialize_echoes_supported_protocol_version(self):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
        response = server.handle_message(message, _fresh_state())
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "veripipe-pipeline-v2")

    def test_initialize_falls_back_for_unknown_version(self):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
        response = server.handle_message(message, _fresh_state())
        self.assertEqual(response["result"]["protocolVersion"], server.DEFAULT_PROTOCOL_VERSION)

    def test_initialized_notification_has_no_response(self):
        state = _fresh_state()
        response = server.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, state
        )
        self.assertIsNone(response)
        self.assertTrue(state["initialized"])

    def test_invalid_jsonrpc_envelope_is_rejected(self):
        response = server.handle_message({"method": "tools/list"}, _fresh_state())
        self.assertEqual(response["error"]["code"], server.INVALID_REQUEST)

    def test_unknown_method_returns_method_not_found(self):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}, _fresh_state()
        )
        self.assertEqual(response["error"]["code"], server.METHOD_NOT_FOUND)


class ToolsListTest(unittest.TestCase):
    def test_tools_list_exposes_all_tools_with_schemas(self):
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, _fresh_state()
        )
        listed = response["result"]["tools"]
        self.assertEqual({tool["name"] for tool in listed}, EXPECTED_TOOLS)
        for tool in listed:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertNotIn("handler", tool)


class ToolCallTest(unittest.TestCase):
    def _call(self, name, arguments):
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            _fresh_state(),
        )
        return response["result"]

    def test_unknown_tool_is_error_result(self):
        result = self._call("does-not-exist", {})
        self.assertTrue(result["isError"])
        self.assertIn("unknown tool", result["content"][0]["text"])

    def test_missing_required_argument_is_error_result(self):
        result = self._call("run_loop", {})
        self.assertTrue(result["isError"])
        self.assertIn("adapter", result["content"][0]["text"])

    def test_query_ledger_missing_file_is_error(self):
        result = self._call("query_ledger", {"ledger": "/nonexistent/ledger.sqlite", "op": "all_batches"})
        self.assertTrue(result["isError"])
        self.assertIn("not found", result["content"][0]["text"])

    def test_code_map_validate_missing_file_fails_closed(self):
        result = self._call("code_map_validate", {"code_map": "/nonexistent/code-map.yaml"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["module"], "code_map")
        self.assertNotEqual(result["structuredContent"]["returncode"], 0)

    def test_run_batch_mock_dry_run_then_query_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ledger = base / "ledger.sqlite"
            batch = self._call(
                "run_batch",
                {
                    "executor": "mock",
                    "dry_run": True,
                    "ledger": str(ledger),
                    "queue": str(base / "queue.jsonl"),
                    "brief": str(base / "brief.md"),
                    "batch_id": "unit-batch-001",
                },
            )
            self.assertFalse(batch["isError"], batch["content"][0]["text"])
            self.assertEqual(batch["structuredContent"]["returncode"], 0)
            self.assertTrue(ledger.is_file())

            query = self._call(
                "query_ledger",
                {"ledger": str(ledger), "op": "recent_batches", "limit": 5},
            )
            self.assertFalse(query["isError"], query["content"][0]["text"])
            batch_ids = [row["batch_id"] for row in query["structuredContent"]["result"]]
            self.assertIn("unit-batch-001", batch_ids)


class TransportTest(unittest.TestCase):
    def test_serve_reports_parse_error_and_continues(self):
        stdin = io.StringIO(
            "not-json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            + "\n"
        )
        stdout = io.StringIO()
        server.serve(stdin, stdout)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], server.PARSE_ERROR)
        self.assertEqual(responses[1]["result"], {})

    def test_call_tool_rejects_non_object_arguments(self):
        with self.assertRaises(tools.ToolError):
            tools.call_tool("preflight", ["not", "an", "object"])


if __name__ == "__main__":
    unittest.main()
