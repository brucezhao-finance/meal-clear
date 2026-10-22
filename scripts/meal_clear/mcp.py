"""固定官方端点、只读白名单的 Streamable HTTP MCP 客户端。"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

from . import net_guard
from .config import (
    CONTROLLED_WRITE_TOOLS,
    GROUP_READ_ONLY_TOOLS,
    OFFICIAL_ENDPOINT,
    RATE_LIMIT,
    TOOL_CREATE_ORDER,
)

ENDPOINT = OFFICIAL_ENDPOINT
READ_ONLY = frozenset(
    {
        "query-nearby-stores",
        "query-meals",
        "query-meal-detail",
        "query-store-coupons",
        "calculate-price",
        "campaign-calendar",
        "now-time-info",
        "available-coupons",
        "query-my-coupons",
        "delivery-query-addresses",
        "delivery-query-stores",
        "query-order",
    }
)
# 团餐专用只读工具（READ_ONLY 保持 12 个不变；传输层白名单是 ALL_READ_ONLY）
GROUP_READ_ONLY = frozenset(GROUP_READ_ONLY_TOOLS)
ALL_READ_ONLY = READ_ONLY | GROUP_READ_ONLY
# 受控写工具只能经 gate.WriteGate → McpClient.call_write 使用
CONTROLLED_WRITE = CONTROLLED_WRITE_TOOLS


class McpError(RuntimeError):
    """可安全显示的协议/业务错误，不携带原始账户响应。"""


class RateLimitedError(McpError):
    """429 重试耗尽；调用方应停止继续请求并如实说明。"""


class BusinessError(McpError):
    """结构完整的业务失败；.code 只存纯数字。"""

    def __init__(self, message: str, code: str = "unknown") -> None:
        super().__init__(message)
        self.code = code


class ToolError(McpError):
    """工具结果带 isError。"""


class WriteRefused(McpError):
    """写调用在发出网络请求前被拒绝（授权、工具或目标不满足）。"""


class WriteFailed(McpError):
    """写调用被明确拒绝或明确未执行；同一确认不得重试。"""


class WriteUnknown(McpError):
    """写调用结果未知；先核实，不要重试。"""


class RateLimiter:
    """滑动窗口客户端限流：超出则等待，而不是发出请求。"""

    def __init__(
        self,
        max_calls: int = RATE_LIMIT[0],
        window: float = RATE_LIMIT[1],
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        """clock/sleep 可注入假时钟以便测试。"""
        self.max_calls = max_calls
        self.window = window
        self._clock = clock or time.monotonic
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._stamps: Deque[float] = deque()

    def acquire(self) -> None:
        """占用一个名额；窗口已满时等待最早一次调用过期。"""
        while True:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= self.window:
                self._stamps.popleft()
            if len(self._stamps) < self.max_calls:
                self._stamps.append(now)
                return
            self._sleep(max(self.window - (now - self._stamps[0]), 0.001))


def _validate_test_endpoint(url: str) -> str:
    """仅接受严格回环端点（见 net_guard；无用户信息和路径，不做 DNS 猜测）。"""
    try:
        return net_guard.validate_loopback_url(url)
    except net_guard.NetGuardError:
        raise McpError("测试端点只允许 http://127.0.0.1、[::1] 或 localhost 的本机端口") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止认证头随重定向传往其他地址。"""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        """官方端点不进行自动跳转。"""
        return None


def decode_response(raw: bytes, request_id: Optional[int]) -> Dict[str, Any]:
    """解析JSON或SSE；从通知流中选取对应请求结果。"""
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeError:
        raise McpError("服务响应不是有效UTF-8，未记录原始内容") from None
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    values = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        if not data:
            continue
        try:
            value = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id") == request_id:
            values.append(value)
    if not values:
        raise McpError("服务未返回可识别的对应JSON-RPC结果")
    return values[-1]


def unwrap_tool(result: Dict[str, Any]) -> Dict[str, Any]:
    """提取结构化工具结果，业务失败只暴露状态码。"""
    if not isinstance(result, dict):
        raise McpError("工具结果必须为对象")
    if result.get("isError"):
        raise ToolError("MCP工具返回isError，原始账户响应未写入日志")
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        payload = None
        content = result.get("content", [])
        if not isinstance(content, list):
            raise McpError("工具内容结构异常")
        for part in content:
            if not isinstance(part, dict):
                raise McpError("工具内容项必须为对象")
            if part.get("type") == "text":
                try:
                    candidate = json.loads(part.get("text", ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
    if payload is None:
        raise McpError("工具没有可识别的结构化结果")
    code = payload.get("code")
    if code is not None and str(code) not in {"0", "200"}:
        safe_code = str(code) if str(code).isdigit() else "unknown"
        raise BusinessError(
            f"业务请求未成功（code={safe_code}），未保存原始回包",
            safe_code if safe_code.isdigit() else "unknown",
        )
    return payload


class McpClient:
    """凭证只驻留内存；客户端没有任意端点和任意工具入口。"""

    def __init__(
        self,
        token_file: Optional[str] = None,
        timeout: float = 20,
        endpoint: Optional[str] = None,
        retry_delay: float = 0.5,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        """优先使用环境变量，或读取用户明确给定的本地凭证文件。"""
        token = os.environ.get("MCD_MCP_TOKEN", "").strip()
        if not token and token_file:
            raw = Path(token_file).read_text(encoding="utf-8").strip()
            candidates = re.findall(r"[A-Za-z0-9._~+/-]{20,}={0,2}", raw)
            if len(candidates) != 1:
                raise McpError("凭证文件中未找到唯一Token，内容不会输出")
            token = candidates[0]
        if not token or any(c.isspace() for c in token):
            raise McpError("需要有效的MCD_MCP_TOKEN或纯Token文件，凭证不会输出")
        self._token = token
        self._session = None
        self._protocol = "2024-11-05"
        self._counter = 0
        self.timeout = timeout
        self.endpoint = ENDPOINT if endpoint is None else _validate_test_endpoint(endpoint)
        self.retry_delay = retry_delay
        self.rate_limiter = rate_limiter or RateLimiter()
        self.audit = []
        self._write_ctx: Optional[str] = None
        self._official_ctx: Optional[str] = None
        self._opener = urllib.request.build_opener(NoRedirect())

    def _post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        notification: bool = False,
        attempts: int = 3,
    ) -> Dict[str, Any]:
        """发送协议消息；只对安全的只读暂时故障有限重试，写调用恒为单次。"""
        write = False
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str) or name not in ALL_READ_ONLY:
                # 传输层兜底：非只读工具必须处于 call_write 上下文，且目标是严格回环，
                # 或（官方端点）call_write 已校验过人工批准并设置了官方上下文
                if (
                    not isinstance(name, str)
                    or name != self._write_ctx
                    or name not in CONTROLLED_WRITE
                ):
                    raise WriteRefused("传输层拒绝非白名单工具，未发出任何请求")
                if net_guard.is_official_url(self.endpoint):
                    if name != self._official_ctx:
                        raise WriteRefused("官方端点缺少人工批准，未发出任何请求")
                else:
                    try:
                        net_guard.assert_write_target(self.endpoint)
                    except net_guard.NetGuardError:
                        raise WriteRefused(
                            "写操作只允许发往本机回环地址，未发出任何请求"
                        ) from None
                write = True
                attempts = 1
        self._counter += 1
        rid = None if notification else self._counter
        body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            body["id"] = rid
        if params is not None:
            body["params"] = params
        headers = {
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        encoded = json.dumps(body).encode()
        for attempt in range(attempts):
            try:
                self.rate_limiter.acquire()
                req = urllib.request.Request(
                    self.endpoint, data=encoded, headers=headers, method="POST"
                )
                with self._opener.open(req, timeout=self.timeout) as response:
                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        self._session = session
                    raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise (WriteUnknown if write else McpError)("响应超过8MB上限，停止解析")
                if notification:
                    return {}
                err = WriteUnknown if write else McpError
                try:
                    result = decode_response(raw, rid)
                except McpError:
                    raise err("响应无法解码；未记录原始内容") from None
                if "error" in result:
                    raise err("JSON-RPC请求失败；未记录原始账户响应")
                if type(result.get("id")) is not type(rid) or result.get("id") != rid:
                    raise err("JSON-RPC响应id不匹配")
                data = result.get("result", {})
                if not isinstance(data, dict):
                    raise err("JSON-RPC结果结构异常")
                return data
            except (WriteUnknown, WriteFailed, WriteRefused):
                raise
            except urllib.error.HTTPError as exc:
                if write:
                    if exc.code in {400, 401, 403, 404, 422, 429}:
                        raise WriteFailed(
                            f"HTTP {exc.code}：服务端明确拒绝，本次未执行"
                        ) from None
                    raise WriteUnknown(
                        f"HTTP {exc.code}：写请求结果未知，请先核实，不要重试"
                    ) from None
                if exc.code in {401, 403}:
                    raise McpError(
                        f"HTTP {exc.code}：认证或访问受限，停止重试"
                    ) from None
                if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                    if exc.code == 429:
                        raise RateLimitedError(
                            f"HTTP {exc.code}：只读请求失败"
                        ) from None
                    raise McpError(f"HTTP {exc.code}：只读请求失败") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if write:
                    reason = getattr(exc, "reason", exc)
                    if isinstance(reason, ConnectionRefusedError):
                        raise WriteFailed("连接被拒绝，本次未执行") from None
                    raise WriteUnknown(
                        "网络或超时错误；写请求结果未知，请先核实，不要重试"
                    ) from None
                if attempt == attempts - 1:
                    raise McpError("网络或超时错误；只读请求已达到重试上限") from None
            except Exception:
                if write:
                    raise WriteUnknown(
                        "写请求出现未预期错误；结果未知，请先核实，不要重试"
                    ) from None
                raise
            time.sleep(self.retry_delay * (2**attempt))
        raise McpError("未完成请求")

    def initialize(self) -> Dict[str, Any]:
        """初始化并读取当前服务协议版本。"""
        result = self._post(
            "initialize",
            {
                "protocolVersion": self._protocol,
                "capabilities": {},
                "clientInfo": {"name": "meal-clear", "version": "0.1.0"},
            },
        )
        self._protocol = result.get("protocolVersion", self._protocol)
        self._post("notifications/initialized", notification=True)
        return result.get("serverInfo", {})

    def list_tools(self) -> Dict[str, Any]:
        """读取当前Schema并处理分页，不保存凭证。"""
        result = self._post("tools/list", {})

        def validate_page(page: Dict[str, Any]) -> None:
            """在合并工具或使用游标之前检查服务分页结构。"""
            if not isinstance(page.get("tools", []), list) or any(
                not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                for tool in page.get("tools", [])
            ):
                raise McpError("工具清单结构异常")
            if page.get("nextCursor") is not None and not isinstance(
                page["nextCursor"], str
            ):
                raise McpError("工具分页游标结构异常")

        validate_page(result)
        tools = list(result.get("tools", []))
        cursor = result.get("nextCursor")
        seen = set()
        while cursor:
            if cursor in seen or len(seen) >= 10:
                raise McpError("工具分页异常")
            seen.add(cursor)
            page = self._post("tools/list", {"cursor": cursor})
            validate_page(page)
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
        return {"tools": tools}

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """拒绝所有白名单外工具，包括下单和领券。"""
        if name not in ALL_READ_ONLY:
            raise McpError("首版客户端拒绝非白名单工具")
        started = time.monotonic()
        try:
            result = unwrap_tool(
                self._post("tools/call", {"name": name, "arguments": arguments})
            )
        except McpError:
            self.audit.append({"tool": name, "success": False})
            raise
        self.audit.append(
            {
                "tool": name,
                "success": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return result

    def call_write(
        self, name: str, arguments: Dict[str, Any], *, grant: Any
    ) -> Dict[str, Any]:
        """受控写调用：只接受 CONTROLLED_WRITE，需 gate.py 签发的授权，单次尝试。"""
        from . import gate  # 延迟导入避免循环依赖

        if not isinstance(name, str) or name not in CONTROLLED_WRITE:
            raise WriteRefused("拒绝非受控写工具，未发出任何请求")
        if not gate.grant_is_valid(grant, name):
            raise WriteRefused("缺少有效的写授权，未发出任何请求")
        if net_guard.is_official_url(self.endpoint):
            if not gate.grant_is_official(grant, name):
                raise WriteRefused("官方端点缺少人工批准，未发出任何请求")
            self._official_ctx = name
        self._write_ctx = name
        try:
            payload = unwrap_tool(
                self._post(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                    attempts=1,
                )
            )
            data = payload.get("data")
            if name == TOOL_CREATE_ORDER and not (
                isinstance(data, dict)
                and isinstance(data.get("orderId"), str)
                and data["orderId"].strip()
            ):
                raise WriteUnknown("成功码但缺少订单号，结果未知，请先核实，不要重试")
        except WriteRefused:
            raise
        except (WriteFailed, WriteUnknown):
            self.audit.append({"tool": name, "success": False})
            raise
        except (BusinessError, ToolError) as exc:
            self.audit.append({"tool": name, "success": False})
            raise WriteFailed(str(exc)) from None
        except McpError:
            self.audit.append({"tool": name, "success": False})
            raise WriteUnknown("写请求响应结构异常，结果未知，请先核实，不要重试") from None
        except Exception:
            self.audit.append({"tool": name, "success": False})
            raise WriteUnknown("写请求出现未预期错误，结果未知，请先核实，不要重试") from None
        finally:
            self._write_ctx = None
            self._official_ctx = None
        self.audit.append({"tool": name, "success": True})
        return payload
