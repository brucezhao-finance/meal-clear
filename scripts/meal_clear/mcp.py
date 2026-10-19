"""固定官方端点、只读白名单的 Streamable HTTP MCP 客户端。"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ENDPOINT = "https://mcp.mcd.cn"
READ_ONLY = frozenset(
    {
        "query-nearby-stores",
        "query-meals",
        "query-meal-detail",
        "query-store-coupons",
        "calculate-price",
        "campaign-calendar",
        "now-time-info",
    }
)


class McpError(RuntimeError):
    """可安全显示的协议/业务错误，不携带原始账户响应。"""


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
        raise McpError("MCP工具返回isError，原始账户响应未写入日志")
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
        raise McpError(f"业务请求未成功（code={safe_code}），未保存原始回包")
    return payload


class McpClient:
    """凭证只驻留内存；客户端没有任意端点和任意工具入口。"""

    def __init__(self, token_file: Optional[str] = None, timeout: float = 20) -> None:
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
        self.audit = []
        self._opener = urllib.request.build_opener(NoRedirect())

    def _post(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        notification: bool = False,
    ) -> Dict[str, Any]:
        """发送协议消息；只对安全的只读暂时故障有限重试。"""
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
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    ENDPOINT, data=encoded, headers=headers, method="POST"
                )
                with self._opener.open(req, timeout=self.timeout) as response:
                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        self._session = session
                    raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise McpError("响应超过8MB上限，停止解析")
                if notification:
                    return {}
                result = decode_response(raw, rid)
                if "error" in result:
                    raise McpError("JSON-RPC请求失败；未记录原始账户响应")
                if type(result.get("id")) is not type(rid) or result.get("id") != rid:
                    raise McpError("JSON-RPC响应id不匹配")
                data = result.get("result", {})
                if not isinstance(data, dict):
                    raise McpError("JSON-RPC结果结构异常")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise McpError(
                        f"HTTP {exc.code}：认证或访问受限，停止重试"
                    ) from None
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise McpError(f"HTTP {exc.code}：只读请求失败") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise McpError("网络或超时错误；只读请求已达到重试上限") from None
            time.sleep(0.5 * (2**attempt))
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
        if name not in READ_ONLY:
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
