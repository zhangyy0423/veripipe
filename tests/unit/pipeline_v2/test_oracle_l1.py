# -*- coding: utf-8 -*-
"""oracle_l1 单测：硬失败判定，纯离线。"""
import unittest
from pipeline_v2.oracle_l1 import evaluate_l1, evaluate_l1_from_result
from pipeline_v2.orchestrator import ExecutionResult


class OracleL1Test(unittest.TestCase):
    def test_panic_in_stderr_is_l1(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x",
                        stderr="panic: runtime error: invalid memory address\n\tmain.go:42 +0x1f",
                        exit_code=2)
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.failure_type, "process_crash")
        self.assertEqual(r.failure_signal.oracle_level, "L1")
        self.assertEqual(r.failure_signal.stack_anchor, "main.go:42")

    def test_timeout_is_l1_hang(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x", timed_out=True)
        self.assertEqual(r.failure_type, "hang_timeout")

    def test_nonzero_exit_is_l1(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x", exit_code=1, stderr="boom")
        self.assertEqual(r.failure_type, "nonzero_exit")

    def test_http_5xx_is_l1(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x", http_status=500)
        self.assertEqual(r.failure_type, "server_5xx")

    def test_http_4xx_is_not_l1(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x", http_status=400)
        self.assertEqual(r.status, "pass")
        self.assertIsNone(r.failure_signal)

    def test_clean_exit_is_not_l1(self):
        r = evaluate_l1({}, scenario_id="s", step_id="x", exit_code=0, stdout="ok")
        self.assertEqual(r.status, "pass")

    def test_from_execution_result_5xx(self):
        res = ExecutionResult(observed_state={"http_status": 503}, exit_code=0)
        r = evaluate_l1_from_result(res, scenario_id="s", step_id="x")
        self.assertEqual(r.failure_type, "server_5xx")

    def test_from_execution_result_timed_out(self):
        res = ExecutionResult(observed_state={"timed_out": True}, exit_code=0)
        r = evaluate_l1_from_result(res, scenario_id="s", step_id="x")
        self.assertEqual(r.failure_type, "hang_timeout")


if __name__ == "__main__":
    unittest.main()
