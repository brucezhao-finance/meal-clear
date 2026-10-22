"""本地模拟 MCP 服务器（纯标准库、仅绑定回环、全部合成数据）。

非 test_ 命名，不会被当作用例；用于在不联网、不使用真实凭证的前提下
验证协议、参数规则、故障处理和"写工具永不发出"。
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

SIM_TOKEN = "SIM-TOKEN-0123456789-ABCDEFGHIJ"
SESSION_ID = "SIM-SESSION-1"
CONTROLLED_WRITE = ("auto-bind-coupons", "create-order")
FORBIDDEN_WRITE = (
    "cancel-order",
    "delivery-create-address",
    "draw-lottery",
    "mall-create-order",
    "party-order-create",
)
# 旧语义：任何写工具名都计入 write_attempts；原有四项顺序不变
WRITE_TOOLS = ("create-order", "auto-bind-coupons", "cancel-order", "delivery-create-address") + (
    "draw-lottery",
    "mall-create-order",
    "party-order-create",
)
ORDER_ID_PREFIX = "SIM-ORDER-"
PAYURL_SENTINEL = "PAYURL-SENTINEL"
PICKUP_SENTINEL = "PICKUP-SENTINEL"
LOCKER_SENTINEL = "LOCKER-SENTINEL"
RIDER_SENTINEL = "RIDER-SENTINEL"
ORDER_SENTINELS = (PAYURL_SENTINEL, PICKUP_SENTINEL, LOCKER_SENTINEL, RIDER_SENTINEL)
NEW_COUPON_CODE = "COUPONCODE-SENTINEL-3"
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
    "query-order",
)
# 团餐专用只读工具（READ_TOOLS 不变）
GROUP_READ_TOOLS = ("query-promotions", "query-meal-assistance")
GROUP_REQUIRED = {
    "query-promotions": ["storeCode", "orderType", "beType"],
    "query-meal-assistance": ["storeCode"],
}
RESERVATION_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

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
# 团餐（beType=6）可配送门店，与外送（beType=2）分开
GROUP_STORES = {
    "ADDRID-1": [
        {"storeCode": "30001", "storeName": "模拟团餐店A", "beCode": "GBE30001"},
        {"storeCode": "30002", "storeName": "模拟团餐店B", "beCode": "GBE30002"},
    ],
    "ADDRID-2": [{"storeCode": "30002", "storeName": "模拟团餐店B", "beCode": "GBE30002"}],
}
# 助餐服务（合成）：一个默认选中、一个可选、一个不可用
GROUP_ASSISTANCE = [
    {"gmServiceCode": "GM-STD", "gmServiceName": "模拟标准送达", "enable": True, "selected": True,
     "serviceItems": [{"name": "模拟送达服务"}], "unusableReason": ""},
    {"gmServiceCode": "GM-ALT", "gmServiceName": "模拟分装送达", "enable": True, "selected": False,
     "serviceItems": [{"name": "模拟分装服务"}], "unusableReason": ""},
    {"gmServiceCode": "GM-PRO", "gmServiceName": "模拟专人服务", "enable": False, "selected": False,
     "serviceItems": [{"name": "模拟专人服务"}], "unusableReason": "2: 专人服务需提前4h预约"},
]
# 促销规则（合成；金额单位「元」字符串，仅供提示，绝不参与报价）
GROUP_PROMOTIONS = [
    {"promotionId": "PROMO-1", "promotionType": 31, "ruleCategory": 40, "beTypes": [6],
     "startTime": "2099-01-01 00:00:00", "endTime": "2099-12-31 23:59:59", "gmServiceCode": "",
     "products": [{"productCode": "", "type": 3}],
     "ruleDetail": {"orderReduce": {"reduceInfo": [
         {"startDiscountPoint": "500", "reduceAmount": "50"},
         {"startDiscountPoint": "1000", "reduceAmount": "120"}]}}},
    {"promotionId": "PROMO-2", "promotionType": 33, "ruleCategory": 30, "beTypes": [6],
     "startTime": "2099-01-01 00:00:00", "endTime": "2099-12-31 23:59:59", "gmServiceCode": "GM-STD",
     "products": [{"productCode": "", "type": 3}],
     "ruleDetail": {"orderDiscount": {"discount": "30", "startDiscountPoint": "2000"}}},
    {"promotionId": "PROMO-3", "promotionType": 31, "ruleCategory": 40, "beTypes": [6],
     "startTime": "2099-01-01 00:00:00", "endTime": "2099-12-31 23:59:59", "gmServiceCode": "GM-ALT",
     "products": [{"productCode": "", "type": 3}],
     "ruleDetail": {"orderReduce": {"reduceInfo": [
         {"startDiscountPoint": "300", "reduceAmount": "20"}]}}},
]
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
        self.controlled_calls: List[Dict[str, Any]] = []
        self.forbidden_attempts: List[str] = []
        self.orders: List[Dict[str, Any]] = []
        self.claims = 0
        self.duplicate_orders = 0
        self.on_write: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.timeout_delay = 0.6
        self._claim_effect = {"price_delta": 0, "add_store_coupon": False, "clear_claimable": True}
        self._claimed = False
        self._price_drift = 0
        self._take_way_count = 2
        self._last_take_way: List[str] = []
        self._order_keys: set = set()
        self.request_log_methods: List[str] = []
        self.faults: Dict[str, List[Any]] = {}
        self._group_discount = 0
        self.group_promotions: List[Dict[str, Any]] = copy.deepcopy(GROUP_PROMOTIONS)
        self.group_assistance: List[Dict[str, Any]] = copy.deepcopy(GROUP_ASSISTANCE)
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
        """注入故障。

        通用：http401 | http429:N | tool_error | business_code | bad_structure | garbage_json | redirect。
        create-order 另有：business_fail | timeout | lost_response | disconnect | bad_structure。
        """
        count = None
        if mode.startswith("http429:"):
            count = int(mode.split(":", 1)[1])
            mode = "http429"
        self.faults[tool] = [mode, count]

    def set_claim_effect(self, price_delta: int = 0, add_store_coupon: bool = False,
                         clear_claimable: bool = True) -> None:
        """领券成功后：核价偏移（分）、新增门店券、清空可领券。"""
        self._claim_effect = {"price_delta": price_delta, "add_store_coupon": add_store_coupon,
                              "clear_claimable": clear_claimable}

    def set_price_drift(self, delta: int) -> None:
        """从下一次 calculate-price 起，总价偏移 delta 分。"""
        self._price_drift = delta

    def set_group_discount(self, cents: int) -> None:
        """团餐 calculate-price 的官方折扣（分）；默认 0。价格只随它变化，与促销规则表无关。"""
        self._group_discount = cents

    def set_group_promotions(self, rules: List[Dict[str, Any]]) -> None:
        """替换/清空 query-promotions 返回的规则表（不影响 calculate-price）。"""
        self.group_promotions = copy.deepcopy(rules)

    def set_group_assistance(self, items: List[Dict[str, Any]]) -> None:
        """替换 query-meal-assistance 返回的助餐服务。"""
        self.group_assistance = copy.deepcopy(items)

    def group_calls(self) -> List[Dict[str, Any]]:
        """服务端收到的团餐专用只读工具调用。"""
        return [c for c in self.calls if c["tool"] in GROUP_READ_TOOLS]

    def set_take_way_count(self, n: int) -> None:
        """到店核价返回的取餐方式数量（默认 2）。"""
        self._take_way_count = n

    # --- 协议 ---
    def _send(self, h: BaseHTTPRequestHandler, status: int, body: bytes,
              ctype: str = "application/json", extra: Optional[Dict[str, str]] = None) -> None:
        """写出响应（客户端已超时断开时静默）。"""
        try:
            h.send_response(status)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            tools += [{"name": n, "inputSchema": {"type": "object", "required": list(GROUP_REQUIRED[n])}}
                      for n in GROUP_READ_TOOLS]
            self._rpc(h, rid, {"tools": tools})
        elif method == "tools/call":
            self._call(h, rid, msg.get("params") or {})
        else:
            self._send(h, 400, b"{}")

    def _call(self, h: BaseHTTPRequestHandler, rid: Any, params: Dict[str, Any]) -> None:
        """工具调用：记录、注入故障、规则校验、返回合成数据。"""
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name in FORBIDDEN_WRITE:
            with self._lock:
                self.write_attempts.append(name)
                self.forbidden_attempts.append(name)
            self._rpc(h, rid, {"isError": True, "content": []})
            return
        if name in CONTROLLED_WRITE:
            with self._lock:
                self.write_attempts.append(name)
                self.controlled_calls.append({"tool": name, "args": copy.deepcopy(args)})
            if self.on_write is not None:
                self.on_write(name, copy.deepcopy(args))
            self._controlled(h, rid, name, args)
            return
        with self._lock:
            self.calls.append({"tool": name, "args": json.loads(json.dumps(args))})
        fault = self.faults.get(name)
        if fault:
            mode, count = fault
            if mode == "http401":
                self._send(h, 401, b"{}")
                return
            if mode == "redirect":
                self._send(h, 302, b"", extra={"Location": "http://sim.invalid/redirected"})
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
        if name == "query-order":
            order = next((o for o in self.orders if o["orderId"] == args.get("orderId")), None)
            if order is None:
                payload = {"code": 400100, "message": "订单不存在"}
            else:
                payload = {"code": 0, "data": self._order_detail(order)}
            self._rpc(h, rid, {"structuredContent": payload})
            return
        error = self._rule_error(name, args) or self._group_rule_error(name, args)
        if error:
            payload = {"code": 400100, "message": error}
        elif name == "calculate-price":
            payload = {"code": 0, "data": self._calc(args)}
        else:
            payload = {"code": 0, "data": self._stateful_data(name, args)}
        self._rpc(h, rid, {"structuredContent": payload})

    # --- 受控写 ---
    def _controlled(self, h: BaseHTTPRequestHandler, rid: Any, name: str, args: Dict[str, Any]) -> None:
        """auto-bind-coupons 与 create-order：故障注入、规则校验、状态变化。"""
        fault = self.faults.get(name)
        mode = None
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
                mode = None
            elif mode == "redirect":
                self._send(h, 302, b"", extra={"Location": "http://sim.invalid/redirected"})
                return
            elif mode == "disconnect":
                h.close_connection = True
                return
            elif mode == "tool_error":
                self._rpc(h, rid, {"isError": True, "content": []})
                return
            elif mode in ("business_fail", "business_code"):
                self._rpc(h, rid, {"structuredContent": {"code": 500100, "message": "SIM-SECRET"}})
                return
            elif mode == "garbage_json":
                self._send(h, 200, b"<<<not json>>>")
                return
        if name == "auto-bind-coupons":
            if mode == "bad_structure":
                self._rpc(h, rid, {"content": "not-a-list"})
                return
            with self._lock:
                self.claims += 1
                self._claimed = True
            self._rpc(h, rid, {"structuredContent": {"code": 200, "message": "ok",
                                                      "data": {"boundCount": len(CLAIMABLE)}}})
            return
        error = self._order_rule_error(args) or self._group_rule_error("create-order", args)
        if error:
            self._rpc(h, rid, {"structuredContent": {"code": 400100, "message": error}})
            return
        if mode == "timeout":
            time.sleep(self.timeout_delay)
        order = self._create_order(args)
        if mode == "lost_response":
            h.close_connection = True
            return
        if mode == "bad_structure":
            self._rpc(h, rid, {"structuredContent": {"code": 0, "data": {}}})
            return
        detail = self._order_detail(order)
        data = {
            "orderId": order["orderId"],
            "payId": order["payId"],
            "payH5Url": "http://sim.invalid/pay/%s-%s" % (PAYURL_SENTINEL, order["orderId"]),
            "orderDetail": detail,
        }
        self._rpc(h, rid, {"structuredContent": {"code": 0, "data": data}})

    def _order_rule_error(self, a: Dict[str, Any]) -> Optional[str]:
        """create-order 按真实规则校验；通过则 None。"""
        for key in ("storeCode", "orderType", "beType"):
            if a.get(key) in (None, ""):
                return "%s 缺失" % key
        be, order = a.get("beType"), a.get("orderType")
        if (be, order) not in {(1, 1), (5, 1), (2, 2), (6, 2)}:
            return "beType 与 orderType 不一致"
        if be in (5, 2, 6) and not a.get("beCode"):
            return "beCode 必传"
        if be == 1 and "beCode" in a:
            return "到店自取不能传 beCode"
        if order == 1:
            if not a.get("takeWayCode") or a.get("takeWayCode") not in self._last_take_way:
                return "takeWayCode 缺失或不在最近一次核价的取餐方式内"
        else:
            if a.get("addressId") not in {x["addressId"] for x in ADDRESSES}:
                return "addressId 缺失或未知"
        if not isinstance(a.get("items"), list) or not a["items"]:
            return "items 缺失"
        return None

    def _create_order(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """建单（运行时生成订单号）；相同参数重复下单计入 duplicate_orders。"""
        amount = self._calc(a)["price"]
        key = json.dumps(a, sort_keys=True, ensure_ascii=False)
        with self._lock:
            if key in self._order_keys:
                self.duplicate_orders += 1
            self._order_keys.add(key)
            seq = len(self.orders) + 1
            order = {
                "orderId": "%s%04d" % (ORDER_ID_PREFIX, seq),
                "payId": "SIM-PAY-%04d" % seq,
                "args": copy.deepcopy(a),
                "status": "1",
                "amount_cents": amount,
            }
            self.orders.append(order)
        return order

    def _order_detail(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """create-order 与 query-order 共用的订单详情（含应被丢弃的敏感哨兵）。"""
        a = order["args"]
        calc = self._calc(a)
        yuan = lambda cents: "%.2f" % (cents / 100)  # noqa: E731
        detail: Dict[str, Any] = {
            "orderId": order["orderId"],
            "payId": order["payId"],
            "orderStatus": order["status"],
            "storeName": "模拟一店",
            "takeWay": "模拟取餐方式",
            "totalAmount": yuan(order["amount_cents"]),
            "realTotalAmount": yuan(order["amount_cents"]),
            "productPrice": yuan(calc["productPrice"]),
            "realDeliveryPrice": yuan(calc["deliveryPrice"]),
            "realPackingFeeTotalPrice": yuan(calc["packingPrice"]),
            "tablewarePrice": yuan(0),
            "totalDiscountAmount": yuan(0),
            "orderTime": "2099-01-01 12:00:00",
            "expirePayTime": "2099-01-01 12:10:00",
            "pickupCode": PICKUP_SENTINEL,
            "lockerCode": LOCKER_SENTINEL,
            "lockerQrCode": "http://sim.invalid/" + LOCKER_SENTINEL,
            "payH5Url": "http://sim.invalid/pay/%s-%s" % (PAYURL_SENTINEL, order["orderId"]),
            "orderProductList": [
                {"productName": MEALS.get(i.get("productCode"), ("未知", 0))[0],
                 "quantity": i.get("quantity", 1),
                 "price": yuan(MEALS.get(i.get("productCode"), ("", 0))[1])}
                for i in a.get("items", [])
            ],
        }
        if a.get("orderType") == 2:
            detail["deliveryInfo"] = {
                "address": ADDR_SENTINELS[0],
                "phone": ADDR_SENTINELS[1],
                "contactName": ADDR_SENTINELS[2],
                "riderPhone": RIDER_SENTINEL,
            }
        return detail

    def _calc(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """calculate-price：基础价 + 领券/漂移偏移；到店返回 takeWayList。"""
        data = self._data("calculate-price", a)
        delta = self._price_drift
        if a.get("beType") == 6:
            delta -= self._group_discount
        if self._claimed:
            delta += self._claim_effect["price_delta"]
        data["productPrice"] = max(data["productPrice"] + delta, 0)
        data["price"] = data["productPrice"] + data["deliveryPrice"] + data["packingPrice"]
        if a.get("orderType") == 1:
            lst = [{"code": "SIM-TW-%d" % i, "title": "模拟取餐方式%d" % i, "subtitle": "合成"}
                   for i in range(1, self._take_way_count + 1)]
            data["takeWayList"] = lst
            with self._lock:
                self._last_take_way = [x["code"] for x in lst]
        return data

    def _group_rule_error(self, name: str, a: Dict[str, Any]) -> Optional[str]:
        """团餐专用规则：新工具入参、gmServiceCode/reservationDate 校验；非团餐不接受 gmServiceCode。"""
        if name == "query-promotions":
            if not a.get("storeCode") or a.get("orderType") != 2 or a.get("beType") != 6:
                return "storeCode/orderType=2/beType=6 必传"
            if not a.get("beCode"):
                return "团餐场景 beCode 必传"
        elif name == "query-meal-assistance":
            if not a.get("storeCode"):
                return "storeCode 缺失"
            if a.get("beType") not in (None, 6):
                return "此接口仅团餐场景使用"
            if a.get("beType") == 6 and (a.get("orderType") != 2 or not a.get("beCode")):
                return "团餐 orderType=2 且 beCode 必传"
        if name in GROUP_READ_TOOLS or name in ("calculate-price", "create-order"):
            if "reservationDate" in a and not (
                isinstance(a["reservationDate"], str) and RESERVATION_RE.match(a["reservationDate"])
            ):
                return "reservationDate 格式必须为 yyyy-MM-dd HH:mm"
        if name in ("calculate-price", "create-order"):
            if a.get("beType") != 6:
                if "gmServiceCode" in a:
                    return "gmServiceCode 仅团餐使用"
                return None
            gm = a.get("gmServiceCode")
            if not gm:
                return "团餐 gmServiceCode 必传"
            match = [x for x in self.group_assistance if x["gmServiceCode"] == gm]
            if not match or not match[0]["enable"]:
                return "gmServiceCode 无效或不可用"
            if name == "create-order" and "takeWayCode" in a:
                return "外送/团餐不传 takeWayCode"
        return None

    def _stateful_data(self, name: str, a: Dict[str, Any]) -> Any:
        """受领券状态影响的只读工具。"""
        if name == "delivery-query-stores" and a.get("beType") == 6:
            return [dict(x) for x in GROUP_STORES.get(a["addressId"], [])]
        if name == "query-promotions":
            return copy.deepcopy(self.group_promotions)
        if name == "query-meal-assistance":
            return {"mealAssistanceItems": copy.deepcopy(self.group_assistance)}
        if name == "available-coupons" and self._claimed and self._claim_effect["clear_claimable"]:
            return []
        if name == "query-store-coupons" and self._claimed and self._claim_effect["add_store_coupon"]:
            extra = {"couponId": "COUPONID-3", "couponCode": NEW_COUPON_CODE, "title": "模拟新领券",
                     "tradeDateTime": "2099-01-01",
                     "products": [{"productCode": "M2", "name": "模拟辣鸡肉堡套餐"}]}
            return [dict(x) for x in COUPONS] + [extra]
        return self._data(name, a)

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
