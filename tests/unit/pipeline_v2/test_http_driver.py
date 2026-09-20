# -*- coding: utf-8 -*-
"""http_driver 单测：用假传输离线验证 HTTP+WS 执行器，不起真实服务。"""
import json
import unittest
from typing import Any, List, Mapping

from pipeline_v2.orchestrator import ExecutionScenario
from pipeline_v2.http_driver import (
    HttpAgentExecutor,
    HttpDriverConfig,
    Transport,
    TransportError,
)


class FakeWs:
    def __init__(self, inbound: List[Mapping[str, Any]]):
        self._inbound = list(inbound)
        self.sent: List[Mapping[str, Any]] = []
        self.closed = False

    def send_json(self, message):
        self.sent.append(message)

    def recv_json(self):
        if not self._inbound:
            raise TimeoutError("no more")
        return self._inbound.pop(0)

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, *, create=None, inbound=None, fail_post=False, fail_ws=False):
        self._create = create or {"session_id": "s-1"}
        self._inbound = inbound or []
        self._fail_post = fail_post
        self._fail_ws = fail_ws
        self.ws = None

    def post_json(self, path, body):
        if self._fail_post:
            raise TransportError("connection refused")
        return self._create

    def ws_session(self, path):
        if self._fail_ws:
            raise TransportError("ws connect failed")
        self.ws = FakeWs(self._inbound)
        return self.ws


CFG = HttpDriverConfig(
    create_session_path="/api/sessions",
    ws_path_template="/api/chat/ws/{session_id}",
)


def scenario(drive: Mapping[str, Any]) -> ExecutionScenario:
    return ExecutionScenario(
        scenario_id="drive:test",
        step_id="ws",
        test_cmd=json.dumps(drive),
        oracle_level="L3",
    )


class HttpDriverTest(unittest.TestCase):
    def test_drives_command_and_extracts_terminal_state(self):
        inbound = [
            {"type": "skill_state", "data": {}},
            {"type": "command_result", "data": {"name": "set_mode", "success": True, "mode": "exploration"}},
        ]
        ex = HttpAgentExecutor(FakeTransport(inbound=inbound), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command", "name": "set_mode", "mode": "exploration"}))
        self.assertFalse(res.environment_unavailable)
        self.assertTrue(res.observed_state["passed"])
        self.assertEqual(res.observed_state["terminal_event_type"], "command_result")
        self.assertEqual(res.observed_state["terminal_state"]["mode"], "exploration")
        self.assertIn("skill_state", res.observed_state["event_sequence"])

    def test_error_event_marks_failure_not_environment(self):
        inbound = [{"type": "error", "data": {"message": "set_model requires non-empty model"}}]
        ex = HttpAgentExecutor(FakeTransport(inbound=inbound), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command", "name": "set_model"}))
        self.assertFalse(res.environment_unavailable)
        self.assertFalse(res.observed_state["passed"])
        self.assertEqual(res.exit_code, 1)

    def test_post_failure_is_environment_event(self):
        ex = HttpAgentExecutor(FakeTransport(fail_post=True), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command"}))
        self.assertTrue(res.environment_unavailable)

    def test_ws_failure_is_environment_event(self):
        ex = HttpAgentExecutor(FakeTransport(fail_ws=True), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command"}))
        self.assertTrue(res.environment_unavailable)

    def test_no_terminal_event_is_environment_event(self):
        inbound = [{"type": "skill_state", "data": {}}, {"type": "text_delta", "data": {}}]
        ex = HttpAgentExecutor(FakeTransport(inbound=inbound), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command"}))
        self.assertTrue(res.environment_unavailable)

    def test_missing_session_id_is_environment_event(self):
        ex = HttpAgentExecutor(FakeTransport(create={"nope": 1}), CFG)
        res = ex.run(test_cmd="", scenario=scenario({"type": "command"}))
        self.assertTrue(res.environment_unavailable)


if __name__ == "__main__":
    unittest.main()


class HttpDriverSequenceTest(unittest.TestCase):
    def test_run_session_sequence_returns_last_terminal(self):
        inbound = [
            {"type": "skill_state", "data": {}},
            {"type": "command_result", "data": {"name": "set_mode", "mode": "exploration"}},
            {"type": "command_result", "data": {"name": "set_mode", "mode": "modify_ontology"}},
            {"type": "command_result", "data": {"name": "set_mode", "mode": "exploration"}},
        ]
        ex = HttpAgentExecutor(FakeTransport(inbound=inbound), CFG)
        res = ex.run_session_sequence([
            {"type": "command", "name": "set_mode", "mode": "exploration"},
            {"type": "command", "name": "set_mode", "mode": "modify_ontology"},
            {"type": "command", "name": "set_mode", "mode": "exploration"},
        ])
        self.assertFalse(res.environment_unavailable)
        self.assertEqual(res.observed_state["terminal_state"]["mode"], "exploration")

    def test_sequence_missing_terminal_is_environment(self):
        inbound = [{"type": "skill_state", "data": {}}, {"type": "command_result", "data": {"mode": "x"}}]
        ex = HttpAgentExecutor(FakeTransport(inbound=inbound), CFG)
        # 第二步没有终态可收 -> 环境事件
        res = ex.run_session_sequence([
            {"type": "command", "name": "set_mode", "mode": "x"},
            {"type": "command", "name": "set_mode", "mode": "y"},
        ])
        self.assertTrue(res.environment_unavailable)
