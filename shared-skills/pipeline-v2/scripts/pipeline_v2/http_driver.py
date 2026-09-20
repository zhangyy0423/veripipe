"""Product-neutral HTTP+WebSocket agent-driving executor.

设计依据：discuss/10 的 B 型（HTTP API 驱动），Phase 0 spike 已验证可行。
本模块实现 orchestrator.ExecutorProtocol，把"建会话 -> 连 WS -> 驱动一步 ->
收事件直到终态 -> 抽结构化 observed_state"封装成一个产品中立的执行器。

铁律（portability）：本文件禁止出现任何产品名 / 写死端点。所有产品差异
（base_url、ws 路径模板、驱动消息、终态事件名）必须由调用方从 adapter 注入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from .orchestrator import ExecutionResult, ExecutionScenario


class Transport(Protocol):
    """HTTP + WebSocket 传输边界。

    真实实现走 urllib + websocket-client；单测注入假传输，
    因此 oracle / 执行器逻辑可完全离线验证（仿 playwright_executor 的 dry-run）。
    """

    def post_json(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST 一个 JSON，返回解析后的 JSON dict。失败抛 TransportError。"""

    def ws_session(self, path: str) -> "WsSession":
        """打开一个 WebSocket 会话。"""


class WsSession(Protocol):
    """单个 WebSocket 会话：发 JSON、收 JSON、关闭。"""

    def send_json(self, message: Mapping[str, Any]) -> None: ...

    def recv_json(self) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class TransportError(RuntimeError):
    """传输层失败（连接拒绝 / 非 2xx / 超时）。归环境事件，不归产品 bug。"""


@dataclass(frozen=True)
class HttpDriverConfig:
    """从 adapter 注入的产品专属驱动配置（全部由调用方提供）。"""

    create_session_path: str                       # 建会话的 POST 路径
    ws_path_template: str                          # WS 路径模板，含 {session_id}
    session_id_field: str = "session_id"           # 建会话响应里 session id 的字段名
    create_session_body: Mapping[str, Any] = field(default_factory=dict)
    drain_initial_events: int = 5                  # 建连后先读几条服务端主动推送
    terminal_event_types: Sequence[str] = ("tool_call_result", "command_result")
    error_event_types: Sequence[str] = ("error",)
    max_events: int = 50                           # 收事件上限，防卡死


class HttpAgentExecutor:
    """通过 HTTP+WS 驱动被测 agent 的一步，产出结构化 observed_state。"""

    def __init__(self, transport: Transport, config: HttpDriverConfig):
        self._transport = transport
        self._config = config

    def run(self, *, test_cmd: str, scenario: ExecutionScenario) -> ExecutionResult:
        cfg = self._config
        try:
            created = self._transport.post_json(cfg.create_session_path, dict(cfg.create_session_body))
        except TransportError as exc:
            return self._environment_result(str(exc))

        session_id = str(created.get(cfg.session_id_field) or "").strip()
        if not session_id:
            return self._environment_result("create_session returned no session id")

        ws_path = cfg.ws_path_template.format(session_id=session_id)
        # 单会话内可顺序驱动多条消息（有状态蜕变关系，如 A->B->A / hide->unhide）。
        # 默认从 test_cmd 取一条；若 scenario.l3_steps 多于一步且本执行器被复用于
        # "整段序列产一个终态"，调用方应改用 run_session_sequence。这里保持单步语义。
        drive_message = self._drive_message(scenario.test_cmd)
        try:
            ws = self._transport.ws_session(ws_path)
        except TransportError as exc:
            return self._environment_result(str(exc))

        seen: List[str] = []
        terminal: Optional[Mapping[str, Any]] = None
        try:
            terminal = self._drain_initial(ws, seen)
            if terminal is None:
                terminal = self._send_and_wait(ws, drive_message, seen)
        finally:
            ws.close()

        if terminal is None:
            return self._environment_result(f"no terminal event before limit; events={seen}")
        return self._build_result(scenario, terminal, seen)

    def run_session_sequence(self, drive_messages: Sequence[Mapping[str, Any]]) -> ExecutionResult:
        """在**同一个会话**内顺序驱动多条消息，返回最后一步的终态。

        用于有状态蜕变关系（A->B->A、hide->unhide）：前面步骤改变 agent 状态，
        最后一步终态才是被断言的对象。任一步失败/无终态归环境事件。"""

        cfg = self._config
        try:
            created = self._transport.post_json(cfg.create_session_path, dict(cfg.create_session_body))
        except TransportError as exc:
            return self._environment_result(str(exc))
        session_id = str(created.get(cfg.session_id_field) or "").strip()
        if not session_id:
            return self._environment_result("create_session returned no session id")
        if not drive_messages:
            return self._environment_result("empty drive sequence")

        ws_path = cfg.ws_path_template.format(session_id=session_id)
        try:
            ws = self._transport.ws_session(ws_path)
        except TransportError as exc:
            return self._environment_result(str(exc))

        seen: List[str] = []
        last_terminal: Optional[Mapping[str, Any]] = None
        try:
            # 序列模式不单独 drain：_send_and_wait 会跳过服务端主动推送的非终态
            # 事件（skill_state 等），直到收到本步终态，避免误吞下一步的终态。
            for message in drive_messages:
                term = self._send_and_wait(ws, dict(message), seen)
                if term is None:
                    return self._environment_result(f"no terminal event for step; events={seen}")
                last_terminal = term
        finally:
            ws.close()
        if last_terminal is None:
            return self._environment_result(f"no terminal event in sequence; events={seen}")
        return self._build_result_seq(last_terminal, seen)

    def _drain_initial(self, ws: "WsSession", seen: List[str]) -> Optional[Mapping[str, Any]]:
        cfg = self._config
        for _ in range(max(0, cfg.drain_initial_events)):
            pre = self._safe_recv(ws)
            if pre is None:
                break
            ptype = str(pre.get("type") or "")
            seen.append(ptype)
            if ptype in cfg.terminal_event_types or ptype in cfg.error_event_types:
                return pre
        return None

    def _send_and_wait(self, ws: "WsSession", message: Mapping[str, Any], seen: List[str]) -> Optional[Mapping[str, Any]]:
        cfg = self._config
        ws.send_json(message)
        for _ in range(max(1, cfg.max_events)):
            msg = self._safe_recv(ws)
            if msg is None:
                break
            mtype = str(msg.get("type") or "")
            seen.append(mtype)
            if mtype in cfg.terminal_event_types or mtype in cfg.error_event_types:
                return msg
        return None

    def _drive_message(self, raw: str) -> Mapping[str, Any]:
        """把一条 test_cmd（JSON 编码的 WS 驱动消息）解析为 dict。

        core 不知道任何产品协议细节；非法 JSON 退化为 {"type":"raw"}。"""

        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            message = {"type": "raw", "payload": raw}
        if not isinstance(message, Mapping):
            message = {"type": "raw", "payload": raw}
        return message

    def _build_result_seq(self, terminal: Mapping[str, Any], seen: Sequence[str]) -> ExecutionResult:
        return self._build_result(None, terminal, seen)

    def _build_result(
        self,
        scenario: Optional[ExecutionScenario],
        terminal: Mapping[str, Any],
        seen: Sequence[str],
    ) -> ExecutionResult:
        data = terminal.get("data") if isinstance(terminal.get("data"), Mapping) else terminal
        observed: Dict[str, Any] = {
            "terminal_event_type": terminal.get("type"),
            "terminal_state": data,
            "event_sequence": list(seen),
        }
        is_error = str(terminal.get("type") or "") in self._config.error_event_types
        observed["passed"] = not is_error
        return ExecutionResult(
            observed_state=observed,
            raw_failure="" if not is_error else json.dumps(data, ensure_ascii=False),
            exit_code=0 if not is_error else 1,
        )

    def _safe_recv(self, ws: WsSession) -> Optional[Mapping[str, Any]]:
        try:
            msg = ws.recv_json()
        except Exception:
            return None
        return msg if isinstance(msg, Mapping) else None

    def _environment_result(self, reason: str) -> ExecutionResult:
        return ExecutionResult(
            observed_state={"environment_unavailable": True, "environment_reason": reason},
            raw_failure=reason,
            environment_unavailable=True,
        )


# --- 真实传输实现（urllib + websocket-client）。仅在真跑时使用；单测走假传输。 ---


class UrllibWsTransport:
    """基于 urllib（HTTP）+ websocket-client（WS）的真实传输。

    base_url / ws_base_url 由调用方注入（产品中立）。任何网络/超时错误统一
    转成 TransportError，由执行器归为环境事件，绝不当产品 bug。
    """

    def __init__(self, *, base_url: str, ws_base_url: str, recv_timeout_sec: float = 20.0):
        self._base = base_url.rstrip("/")
        self._ws_base = ws_base_url.rstrip("/")
        self._recv_timeout = recv_timeout_sec

    def post_json(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(dict(body)).encode("utf-8")
        req = urllib.request.Request(
            self._base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._recv_timeout) as resp:
                payload = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(f"post {path} failed: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise TransportError(f"post {path} returned non-object")
        return payload

    def ws_session(self, path: str) -> "WsSession":
        try:
            import websocket  # websocket-client
        except ImportError as exc:
            raise TransportError("websocket-client not installed") from exc
        try:
            conn = websocket.create_connection(self._ws_base + path, timeout=self._recv_timeout)
        except Exception as exc:  # noqa: BLE001 - 任何连接失败都归环境事件
            raise TransportError(f"ws connect {path} failed: {exc}") from exc
        return _UrllibWs(conn, self._recv_timeout)


class _UrllibWs:
    """websocket-client 连接的薄封装，统一收发 JSON。"""

    def __init__(self, conn: Any, recv_timeout_sec: float):
        self._conn = conn
        self._conn.settimeout(recv_timeout_sec)

    def send_json(self, message: Mapping[str, Any]) -> None:
        self._conn.send(json.dumps(dict(message)))

    def recv_json(self) -> Mapping[str, Any]:
        raw = self._conn.recv()
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            raise ValueError("ws frame is not a JSON object")
        return data

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
