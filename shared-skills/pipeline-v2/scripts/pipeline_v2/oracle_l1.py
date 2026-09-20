"""L1 隐式硬 oracle：崩溃 / panic / OOM / 超时 / 非零退出 / 服务端 5xx。

设计依据：discuss/02 §1（L1 机器可证、零判断、误报近零）、discuss/06 §2
（L1 崩溃 fuzzing 在任意输入下都安全，是离开指南路径的兜底信号）。

铁律：
- L1 只认"机器可证的硬失败"，绝不掺语义判断；
- 4xx 等正常拒绝 / 业务校验失败 **不是** L1（那是产品在正确工作）；
- 强通道 N=1（见 verification.classify_channel）。

本模块是纯函数，离线可单测，产品中立（不出现产品名/端点）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .schema import FailureSignal


# stderr/输出里命中即视为崩溃的结构特征（大小写不敏感）。
CRASH_PATTERNS = (
    r"\bpanic\b",
    r"\bfatal error\b",
    r"\bruntime error\b",
    r"\bsegmentation fault\b",
    r"\bout of memory\b",
    r"\bOOM\b",
    r"\bnil pointer dereference\b",
    r"\bstack overflow\b",
    r"\bgoroutine \d+ \[",          # Go panic 堆栈头
    r"\bdata race\b",
)

_CRASH_RE = re.compile("|".join(CRASH_PATTERNS), re.IGNORECASE)
_GO_PANIC_ANCHOR_RE = re.compile(r"^\s*([\w./]+\.go):(\d+)", re.MULTILINE)


@dataclass(frozen=True)
class L1Result:
    """L1 判定结果。failure_signal 仅在确认硬失败时填充。"""

    status: str                              # "pass" | "fail"
    reason: str
    failure_type: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    failure_signal: Optional[FailureSignal] = None


def _stack_anchor(text: str) -> Optional[str]:
    match = _GO_PANIC_ANCHOR_RE.search(text or "")
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None


def evaluate_l1(
    observed: Mapping[str, Any],
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str = "none",
    semantic_map_entry_id: Optional[str] = None,
    exit_code: Optional[int] = None,
    stderr: str = "",
    stdout: str = "",
    timed_out: bool = False,
    http_status: Optional[int] = None,
) -> L1Result:
    """把一次执行的硬指标判定为是否 L1 崩溃。

    判据（任一命中即 L1，按优先级给 failure_type）：
      1. timed_out=True              -> "hang_timeout"
      2. stderr/stdout 命中崩溃模式   -> "process_crash"
      3. exit_code 非 0 / 非 None     -> "nonzero_exit"
      4. http_status 5xx             -> "server_5xx"
    4xx / 正常退出 / 无异常 -> pass（不立案）。
    """

    text = f"{stderr}\n{stdout}"
    detail: Dict[str, Any] = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "http_status": http_status,
    }

    failure_type = ""
    reason = "no-hard-failure"
    if timed_out:
        failure_type, reason = "hang_timeout", "execution timed out"
    elif _CRASH_RE.search(text):
        failure_type, reason = "process_crash", "crash pattern in output"
    elif exit_code is not None and exit_code != 0:
        failure_type, reason = "nonzero_exit", f"nonzero exit code {exit_code}"
    elif http_status is not None and 500 <= int(http_status) <= 599:
        failure_type, reason = "server_5xx", f"server error status {http_status}"

    if not failure_type:
        return L1Result(status="pass", reason=reason, detail=detail)

    signal = FailureSignal(
        scenario_id=scenario_id,
        step_id=step_id,
        failure_type=failure_type,
        oracle_level="L1",
        llm_involvement=llm_involvement,
        expected_state={"hard_oracle": "no-crash"},
        actual_state={"failure_type": failure_type, **detail},
        stack_anchor=_stack_anchor(text),
        exit_code=exit_code,
        semantic_map_entry_id=semantic_map_entry_id,
        evidence={"oracle_l1": {"reason": reason, **detail}},
    )
    return L1Result(status="fail", reason=reason, failure_type=failure_type, detail=detail, failure_signal=signal)


def evaluate_l1_from_result(
    result: Any,
    *,
    scenario_id: str,
    step_id: str,
    llm_involvement: str = "none",
    semantic_map_entry_id: Optional[str] = None,
) -> L1Result:
    """从 ExecutionResult 取硬指标做 L1 判定（执行器适配）。"""

    observed = getattr(result, "observed_state", {}) or {}
    http_status = observed.get("http_status") if isinstance(observed, Mapping) else None
    timed_out = bool(observed.get("timed_out")) if isinstance(observed, Mapping) else False
    return evaluate_l1(
        observed if isinstance(observed, Mapping) else {},
        scenario_id=scenario_id,
        step_id=step_id,
        llm_involvement=llm_involvement,
        semantic_map_entry_id=semantic_map_entry_id,
        exit_code=getattr(result, "exit_code", None),
        stderr=getattr(result, "stderr", "") or getattr(result, "raw_failure", ""),
        stdout=getattr(result, "stdout", ""),
        timed_out=timed_out,
        http_status=http_status,
    )
