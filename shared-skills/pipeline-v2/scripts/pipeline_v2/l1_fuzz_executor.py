"""L1 崩溃 fuzz 执行器：对 HTTP 边界喂畸形输入，抓崩溃类硬失败。

设计依据：discuss/06 §2（L1 崩溃 fuzzing 在任意输入下都安全，是离开指南
路径的兜底信号）。本执行器只发畸形 HTTP 请求并采集硬指标
（http_status / 超时 / 连接异常），交给 oracle_l1 判定。

铁律：产品中立——端点、畸形 payload、方法全部由 adapter 注入；core 不内置
任何产品 URL。连接拒绝/超时归"环境不可用或挂死"，由 oracle_l1 区分。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol

from .orchestrator import ExecutionResult, ExecutionScenario


class HttpProbe(Protocol):
    """单次 HTTP 探测边界。返回 (status_code, body_text)；超时抛 TimeoutError；
    连接失败抛 ConnectionError。"""

    def request(self, *, method: str, path: str, headers: Mapping[str, str], body: bytes) -> "ProbeResponse": ...


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    body: str = ""


@dataclass(frozen=True)
class FuzzCase:
    """一条 fuzz 探测（由 adapter 注入）。"""

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    raw_body: str = ""              # 原样字节体（可为畸形 JSON / 超长串）


class L1FuzzExecutor:
    """把一条 fuzz 探测的 HTTP 结果转成 ExecutionResult（供 oracle_l1 判定）。"""

    def __init__(self, probe: HttpProbe, cases_by_id: Mapping[str, FuzzCase]):
        self._probe = probe
        self._cases = dict(cases_by_id)

    def run(self, *, test_cmd: str, scenario: ExecutionScenario) -> ExecutionResult:
        case = self._cases.get(test_cmd)
        if case is None:
            return ExecutionResult(
                observed_state={"environment_unavailable": True, "environment_reason": f"no fuzz case: {test_cmd}"},
                raw_failure=f"no fuzz case: {test_cmd}",
                environment_unavailable=True,
            )
        try:
            resp = self._probe.request(
                method=case.method,
                path=case.path,
                headers=dict(case.headers),
                body=case.raw_body.encode("utf-8"),
            )
        except TimeoutError as exc:
            # 挂死 = L1 硬失败（不是环境不可用）。
            return ExecutionResult(
                observed_state={"timed_out": True, "path": case.path},
                raw_failure=f"timeout: {exc}",
                exit_code=0,
            )
        except (ConnectionError, OSError) as exc:
            # 连不上 = 服务没起，环境不可用，不当产品 bug。
            return ExecutionResult(
                observed_state={"environment_unavailable": True, "environment_reason": f"connect failed: {exc}"},
                raw_failure=f"connect failed: {exc}",
                environment_unavailable=True,
            )
        observed: Dict[str, Any] = {
            "http_status": int(resp.status),
            "path": case.path,
            # 5xx 时把响应体作为崩溃证据片段（oracle_l1 也会扫 panic 模式）。
            "response_excerpt": resp.body[:500],
        }
        return ExecutionResult(
            observed_state=observed,
            stdout=resp.body if resp.status < 500 else "",
            stderr=resp.body if resp.status >= 500 else "",
            exit_code=0,
        )


class UrllibHttpProbe:
    """基于 urllib 的真实 HTTP 探测（仅真跑用；单测注入假 probe）。"""

    def __init__(self, *, base_url: str, timeout_sec: float = 15.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec

    def request(self, *, method: str, path: str, headers: Mapping[str, str], body: bytes) -> ProbeResponse:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self._base + path, data=body or None, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return ProbeResponse(status=int(resp.status), body=(resp.read() or b"").decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 都走这里：保留状态码交给 oracle_l1 区分（5xx=L1，4xx=正常）。
            body = ""
            try:
                body = (exc.read() or b"").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return ProbeResponse(status=int(exc.code), body=body)
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            raise ConnectionError(str(exc)) from exc
