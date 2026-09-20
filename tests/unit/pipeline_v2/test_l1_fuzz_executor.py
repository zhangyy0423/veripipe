# -*- coding: utf-8 -*-
"""l1_fuzz_executor 单测：假 probe 离线验证 HTTP 崩溃 fuzz。"""
import unittest
from pipeline_v2.l1_fuzz_executor import FuzzCase, L1FuzzExecutor, ProbeResponse
from pipeline_v2.orchestrator import ExecutionScenario
from pipeline_v2.oracle_l1 import evaluate_l1_from_result


class FakeProbe:
    def __init__(self, *, status=None, raise_exc=None, body=""):
        self._status = status
        self._raise = raise_exc
        self._body = body
    def request(self, *, method, path, headers, body):
        if self._raise:
            raise self._raise
        return ProbeResponse(status=self._status, body=self._body)


CASES = {"c1": FuzzCase(method="POST", path="/api/x", raw_body="{bad json")}


def sc():
    return ExecutionScenario(scenario_id="l1:c1", step_id="http-fuzz", test_cmd="c1", oracle_level="L1")


class L1FuzzTest(unittest.TestCase):
    def test_5xx_becomes_l1_signal(self):
        ex = L1FuzzExecutor(FakeProbe(status=500, body="panic: boom"), CASES)
        res = ex.run(test_cmd="c1", scenario=sc())
        self.assertEqual(res.observed_state["http_status"], 500)
        l1 = evaluate_l1_from_result(res, scenario_id="s", step_id="x")
        self.assertEqual(l1.status, "fail")

    def test_4xx_not_l1(self):
        ex = L1FuzzExecutor(FakeProbe(status=400, body="bad request"), CASES)
        res = ex.run(test_cmd="c1", scenario=sc())
        l1 = evaluate_l1_from_result(res, scenario_id="s", step_id="x")
        self.assertEqual(l1.status, "pass")

    def test_timeout_is_l1_hang_not_environment(self):
        ex = L1FuzzExecutor(FakeProbe(raise_exc=TimeoutError("slow")), CASES)
        res = ex.run(test_cmd="c1", scenario=sc())
        self.assertFalse(res.environment_unavailable)
        self.assertTrue(res.observed_state["timed_out"])
        l1 = evaluate_l1_from_result(res, scenario_id="s", step_id="x")
        self.assertEqual(l1.failure_type, "hang_timeout")

    def test_connect_failure_is_environment(self):
        ex = L1FuzzExecutor(FakeProbe(raise_exc=ConnectionError("refused")), CASES)
        res = ex.run(test_cmd="c1", scenario=sc())
        self.assertTrue(res.environment_unavailable)

    def test_unknown_case_is_environment(self):
        ex = L1FuzzExecutor(FakeProbe(status=200), CASES)
        res = ex.run(test_cmd="nope", scenario=sc())
        self.assertTrue(res.environment_unavailable)


if __name__ == "__main__":
    unittest.main()
