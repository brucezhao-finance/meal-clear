"""本地模拟 MCP 服务器（纯标准库、仅绑定回环、全部合成数据）。

非 test_ 命名，不会被当作用例；用于在不联网、不使用真实凭证的前提下
验证协议、参数规则、故障处理和"写工具永不发出"。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

SIM_TOKEN = "SIM-TOKEN-0123456789-ABCDEFGHIJ"
SESSION_ID = "SIM-SESSION-1"
WRITE_TOOLS = ("create-order", "auto-bind-coupons", "cancel-order", "delivery-create-address")
READ_TOOLS = (
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
)

# 哨兵：这些字符串绝不能出现在任何对外输出里
ADDR_SENTINELS = ("ADDR-SENTINEL-1", "PHONE-SENTINEL-1", "CONTACT-SENTINEL-1")
COUPON_SENTINELS = ("COUPONCODE-SENTINEL-1", "COUPONCODE-SENTINEL-2", "MYCODE-SENTINEL-1")

DINE_STORES = [
    {"storeCode": "10001", "storeName": "模拟一店", "businessStatus": 1},
    {"storeCode": "10002", "storeName": "模拟二店", "businessStatus": 1},
]
DT_STORES = [{"storeCode": "20001", "storeName": "模拟得来速店", "beCode": "BE20001"}]
DELIVERY_STORES = {
    "ADDRID-1": [
        {"storeCode": "30001", "storeName": "模拟外送店A", "beCode": "BE30001"},
        {"storeCode": "30002", "storeName": "模拟外送店B", "beCode": "BE30002"},
    ],
    "ADDRID-2": [{"storeCode": "30002", "storeName": "模拟外送店B", "beCode": "BE30002"}],
}
ADDRESSES = [
    {
        "addressId": "ADDRID-1",
        "fullAddress": ADDR_SENTINELS[0],
        "phone": ADDR_SENTINELS[1],
        "contactName": ADDR_SENTINELS[2],
    },
    {
        "addressId": "ADDRID-2",
        "fullAddress": ADDR_SENTINELS[0] + "-B",
        "phone": ADDR_SENTINELS[1] + "-B",
        "contactName": ADDR_SENTINELS[2] + "-B",
    },
]
# code -> (名称, 价格分, 提示价)
MEALS = {
    "M1": ("模拟不辣鸡肉堡套餐", 2500),
    "M2": ("模拟辣鸡肉堡套餐", 2300),
    "M3": ("模拟牛肉堡套餐", 3200),
    "M4": ("模拟猪肉堡套餐", 2100),
    "M5": ("模拟鸡肉卷套餐", 2400),
    "M6": ("模拟双人套餐", 5600),
    "M7": ("模拟鱼堡套餐", 2800),
    "M8": ("模拟鸡翅组合", 2600),
    "M9": ("模拟牛肉卷套餐", 3000),
    "M10": ("模拟单品薯条", 900),
}
COUPONS = [
    {
        "couponId": "COUPONID-1",
        "couponCode": COUPON_SENTINELS[0],
        "title": "模拟满减券",
        "tradeDateTime": "2099-01-01",
        "products": [{"productCode": "M1", "name": "模拟不辣鸡肉堡套餐"}],
    },
    {
        "couponId": "COUPONID-2",
        "couponCode": COUPON_SENTINELS[1],
        "title": "模拟立减券",
        "tradeDateTime": "2099-01-01",
        "products": [{"productCode": "M3", "name": "模拟牛肉堡套餐"}],
    },
]
MY_COUPONS = [
    {"id": "MYID-%d" % i, "code": COUPON_SENTINELS[2], "title": "模拟已领券%d" % i,
     "subtitle": "合成", "discountDesc": "减5元", "tradeEnd": "2099-01-01", "enable": True,
     "image": "BASE64-SENTINEL"}
    for i in range(1, 4)
]
CLAIMABLE = [
    {"couponName": "模拟可领券%d" % i, "label": "合成", "couponStatus": 1, "couponCode": "X"}
    for i in range(1, 3)
]


def _detail(name: str) -> Dict[str, Any]:
    """合成详情：主食轮 + 含糖/无糖饮料轮。"""
    return {
        "name": name,
        "rounds": [
            {"id": 1, "name": "主食", "quantity": 1, "minQuantity": 1, "maxQuantity": 1,
             "choices": [{"code": "MAIN", "name": name.replace("套餐", ""), "isDefault": 1}]},
            {"id": 2, "name": "饮料", "quantity": 1, "minQuantity": 1, "maxQuantity": 1,
             "choices": [{"code": "DRINK", "name": "模拟含糖可乐", "isDefault": 1},
                         {"code": "ZERO", "name": "模拟无糖可乐", "isDefault": 0}]},
        ],
    }


class _Handler(BaseHTTPRequestHandler):
    """处理 JSON-RPC POST；所有逻辑委托给 SimMcpServer。"""

    def log_message(self, *args: Any) -> None:
        """静默，避免测试输出噪声。"""

    def do_POST(self) -> None:
        """入口：鉴权、解析、分派。"""
        sim: "SimMcpServer" = self.server.sim  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        sim.handle(self, raw)


class SimMcpServer:
    """随机端口的模拟 MCP 服务；用作上下文管理器。"""

    def __init__(self, use_sse: bool = False) -> None:
        """准备状态；use_sse 控制响应格式。"""
        self.use_sse = use_sse
        self.calls: List[Dict[str, Any]] = []
        self.write_attempts: List[str] = []
        self.request_log_methods: List[str] = []
        self.faults: Dict[str, List[Any]] = {}
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.url = ""

    # --- 生命周期 ---
    def __enter__(self) -> "SimMcpServer":
        """启动回环服务。"""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.sim = self  # type: ignore[attr-defined]
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        """关闭服务。"""
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def fail(self, tool: str, mode: str) -> None:
        """注入故障：http401 | http429:N | tool_error | business_code | bad_structure | garbage_json。"""
        count = None
        if mode.startswith("http429:"):
            count = int(mode.split(":", 1)[1])
            mode = "http429"
        self.faults[tool] = [mode, count]

    # --- 协议 ---
    def _send(self, h: BaseHTTPRequestHandler, status: int, body: bytes,
              ctype: str = "application/json", extra: Optional[Dict[str, str]] = None) -> None:
        """写出响应。"""
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _rpc(self, h: BaseHTTPRequestHandler, rid: Any, result: Dict[str, Any],
             extra: Optional[Dict[str, str]] = None) -> None:
        """按 JSON 或 SSE 返回 JSON-RPC 结果。"""
        msg = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False)
        if self.use_sse:
            body = ("event: message\ndata: " + msg + "\n\n").encode()
            self._send(h, 200, body, "text/event-stream", extra)
        else:
            self._send(h, 200, msg.encode(), "application/json", extra)

    def handle(self, h: BaseHTTPRequestHandler, raw: bytes) -> None:
        """处理一次 POST。"""
        if h.headers.get("Authorization") != "Bearer " + SIM_TOKEN:
            self._send(h, 401, b"{}")
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self._send(h, 400, b"{}")
            return
        method = msg.get("method")
        rid = msg.get("id")
        with self._lock:
            self.request_log_methods.append(str(method))
        if method == "initialize":
            self._rpc(h, rid, {"protocolVersion": "2025-03-26",
                               "serverInfo": {"name": "sim-mcp"}, "capabilities": {}},
                      {"Mcp-Session-Id": SESSION_ID})
        elif method == "notifications/initialized":
            self._send(h, 202, b"")
        elif method == "tools/list":
            tools = [{"name": n, "inputSchema": {"type": "object", "required": []}}
                     for n in READ_TOOLS + WRITE_TOOLS]
            self._rpc(h, rid, {"tools": tools})
        elif method == "tools/call":
            self._call(h, rid, msg.get("params") or {})
        else:
            self._send(h, 400, b"{}")

    def _call(self, h: BaseHTTPRequestHandler, rid: Any, params: Dict[str, Any]) -> None:
        """工具调用：记录、注入故障、规则校验、返回合成数据。"""
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name in WRITE_TOOLS:
            with self._lock:
                self.write_attempts.append(name)
            self._rpc(h, rid, {"isError": True, "content": []})
            return
        with self._lock:
            self.calls.append({"tool": name, "args": json.loads(json.dumps(args))})
        fault = self.faults.get(name)
        if fault:
            mode, count = fault
            if mode == "http401":
                self._send(h, 401, b"{}")
                return
            if mode == "http429":
                if count is None or count > 0:
                    if count is not None:
                        fault[1] = count - 1
                    self._send(h, 429, b"{}")
                    return
            elif mode == "tool_error":
                self._rpc(h, rid, {"isError": True, "content": []})
                return
            elif mode == "business_code":
                self._rpc(h, rid, {"structuredContent": {"code": 500100, "message": "SIM-SECRET"}})
                return
            elif mode == "bad_structure":
                self._rpc(h, rid, {"content": "not-a-list"})
                return
            elif mode == "garbage_json":
                self._send(h, 200, b"<<<not json>>>")
                return
        error = self._rule_error(name, args)
        payload = {"code": 400100, "message": error} if error else {"code": 0, "data": self._data(name, args)}
        self._rpc(h, rid, {"structuredContent": payload})

    # --- 参数规则（像真实接口一样校验） ---
    @staticmethod
    def _rule_error(name: str, a: Dict[str, Any]) -> Optional[str]:
        """返回规则错误说明；通过则 None。"""
        if name == "delivery-query-stores":
            if not a.get("addressId") or a.get("beType") not in (2, 6):
                return "addressId/beType 缺失或非法"
            return None
        if name in ("query-meals", "query-meal-detail", "query-store-coupons", "calculate-price"):
            be, order = a.get("beType"), a.get("orderType")
            if not a.get("storeCode"):
                return "storeCode 缺失"
            if (be, order) not in {(1, 1), (5, 1), (2, 2), (6, 2)}:
                return "beType 与 orderType 不一致"
            if be in (5, 2, 6) and not a.get("beCode"):
                return "beCode 必传"
            if be == 1 and "beCode" in a:
                return "到店自取不能传 beCode"
            if name == "query-meal-detail" and not a.get("code"):
                return "code 缺失"
            if name == "calculate-price" and not a.get("items"):
                return "items 缺失"
        if name == "query-nearby-stores" and a.get("beType") not in (1, 5):
            return "beType 非法"
        return None

    # --- 合成数据 ---
    @staticmethod
    def _data(name: str, a: Dict[str, Any]) -> Any:
        """各只读工具的合成返回。"""
        if name == "query-nearby-stores":
            return list(DT_STORES if a.get("beType") == 5 else DINE_STORES)
        if name == "delivery-query-addresses":
            return [dict(x) for x in ADDRESSES]
        if name == "delivery-query-stores":
            return list(DELIVERY_STORES.get(a["addressId"], []))
        if name == "query-meals":
            return {"meals": {c: {"name": n, "currentPrice": p // 100} for c, (n, p) in MEALS.items()}}
        if name == "query-meal-detail":
            return _detail(MEALS.get(a["code"], ("未知", 0))[0])
        if name == "query-store-coupons":
            return [dict(x) for x in COUPONS]
        if name == "available-coupons":
            return [dict(x) for x in CLAIMABLE]
        if name == "query-my-coupons":
            return {"list": [dict(x) for x in MY_COUPONS]}
        if name == "now-time-info":
            return {"now": "2099-01-01 12:00:00"}
        if name == "campaign-calendar":
            return []
        if name == "calculate-price":
            product = 0
            plist = []
            for item in a["items"]:
                price = MEALS.get(item.get("productCode"), ("", 0))[1]
                qty = item.get("quantity", 1)
                if item.get("couponId"):
                    price = max(price - 500, 0)
                product += price * qty
                plist.append({"productCode": item.get("productCode"), "quantity": qty})
            delivery = 600 if a.get("orderType") == 2 else 0
            packing = 100 if a.get("orderType") == 2 else 0
            return {
                "price": product + delivery + packing,
                "productPrice": product,
                "deliveryPrice": delivery,
                "packingPrice": packing,
                "tablewarePrice": 0,
                "productList": plist,
            }
        return {}
