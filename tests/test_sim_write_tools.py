"""T-G1：模拟服务新增工具、故障模式、规则校验与记录语义（原始 JSON-RPC，不经受控写通道）。"""
import contextlib
import json
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.mcp import NoRedirect  # noqa: E402
from netblock import block_network  # noqa: E402
from sim_mcp import (  # noqa: E402
    ADDR_SENTINELS, CONTROLLED_WRITE, FORBIDDEN_WRITE, LOCKER_SENTINEL, NEW_COUPON_CODE,
    ORDER_SENTINELS, PAYURL_SENTINEL, PICKUP_SENTINEL, READ_TOOLS, RIDER_SENTINEL, SIM_TOKEN,
    WRITE_TOOLS, SimMcpServer,
)
from sim_support import only_read_tools  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/mcp-write-tools-schema.json").read_text(encoding="utf-8"))
ITEM = {"productCode": "M1", "quantity": 2}
PICKUP = {"storeCode": "10001", "orderType": 1, "beType": 1}
DELIVERY = {"storeCode": "30001", "orderType": 2, "beType": 2, "beCode": "BE30001"}


class Raw:
    """直接发 JSON-RPC，不跟随重定向。"""

    def __init__(self, sim: SimMcpServer, timeout: float = 5) -> None:
        self.sim, self.timeout = sim, timeout
        self.opener = urllib.request.build_opener(NoRedirect())
        self.n = 0

    def tool(self, name: str, args: dict) -> dict:
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}).encode()
        req = urllib.request.Request(self.sim.url, data=body, method="POST", headers={
            "Authorization": "Bearer " + SIM_TOKEN, "Content-Type": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["result"]

    def data(self, name: str, args: dict) -> dict:
        result = self.tool(name, args)
        return result["structuredContent"]

    def price(self, base: dict) -> dict:
        return self.data("calculate-price", dict(base, items=[ITEM], needTableware=False))

    def order_args(self, **kw) -> dict:
        args = dict(PICKUP, items=[ITEM], needTableware=False, takeWayCode="SIM-TW-1")
        args.update(kw)
        return args


class SimWriteToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(block_network())
        self.sim = stack.enter_context(SimMcpServer())
        self.raw = Raw(self.sim)

    def place(self, **kw) -> dict:
        self.raw.price(PICKUP)
        return self.raw.data("create-order", self.raw.order_args(**kw))

    # --- 常量与旧语义 ---
    def test_constants_and_tools_list(self) -> None:
        self.assertEqual(CONTROLLED_WRITE, ("auto-bind-coupons", "create-order"))
        self.assertEqual(len(FORBIDDEN_WRITE), 5)
        self.assertEqual(set(WRITE_TOOLS), set(CONTROLLED_WRITE) | set(FORBIDDEN_WRITE))
        self.assertEqual(len(WRITE_TOOLS), 7)
        self.assertIn("query-order", READ_TOOLS)

    def test_read_only_semantics_preserved(self) -> None:
        for name in READ_TOOLS:
            if name != "query-order":
                self.raw.tool(name, {"storeCode": "1", "orderType": 1, "beType": 1, "code": "M1",
                                     "items": [ITEM], "addressId": "ADDRID-1"})
        self.raw.data("query-order", {"orderId": "NOPE"})
        self.assertTrue(only_read_tools(self.sim))
        self.assertEqual(self.sim.controlled_calls, [])

    def test_forbidden_tools_recorded(self) -> None:
        for tool in FORBIDDEN_WRITE:
            result = self.raw.tool(tool, {"x": 1})
            self.assertTrue(result["isError"])
        self.assertEqual(self.sim.forbidden_attempts, list(FORBIDDEN_WRITE))
        self.assertEqual(self.sim.write_attempts, list(FORBIDDEN_WRITE))
        self.assertEqual(self.sim.controlled_calls, [])
        self.assertEqual(self.sim.calls, [])
        self.assertFalse(only_read_tools(self.sim))

    # --- auto-bind-coupons ---
    def test_auto_bind_changes_later_state(self) -> None:
        self.sim.set_claim_effect(price_delta=-300, add_store_coupon=True, clear_claimable=True)
        base = {"storeCode": "10001", "orderType": 1, "beType": 1}
        before = self.raw.price(base)["data"]
        self.assertEqual(before["price"], 5000)
        self.assertEqual(len(self.raw.data("available-coupons", {})["data"]), 2)
        self.assertEqual(len(self.raw.data("query-store-coupons", base)["data"]), 2)
        result = self.raw.data("auto-bind-coupons", {})
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.sim.claims, 1)
        self.assertEqual(self.raw.price(base)["data"]["price"], 4700)
        self.assertEqual(self.raw.data("available-coupons", {})["data"], [])
        coupons = self.raw.data("query-store-coupons", base)["data"]
        self.assertEqual(len(coupons), 3)
        self.assertIn(NEW_COUPON_CODE, json.dumps(coupons))
        self.assertEqual(self.sim.controlled_calls, [{"tool": "auto-bind-coupons", "args": {}}])

    def test_claim_effect_defaults_and_keep_claimable(self) -> None:
        self.sim.set_claim_effect(clear_claimable=False)
        self.raw.data("auto-bind-coupons", {})
        self.assertEqual(len(self.raw.data("available-coupons", {})["data"]), 2)
        base = {"storeCode": "10001", "orderType": 1, "beType": 1}
        self.assertEqual(self.raw.price(base)["data"]["price"], 5000)

    def test_price_drift_and_take_way(self) -> None:
        base = {"storeCode": "10001", "orderType": 1, "beType": 1}
        data = self.raw.price(base)["data"]
        self.assertEqual([t["code"] for t in data["takeWayList"]], ["SIM-TW-1", "SIM-TW-2"])
        self.sim.set_price_drift(100)
        self.assertEqual(self.raw.price(base)["data"]["price"], 5100)
        self.sim.set_take_way_count(0)
        self.assertEqual(self.raw.price(base)["data"]["takeWayList"], [])
        self.sim.set_take_way_count(1)
        self.assertEqual(len(self.raw.price(base)["data"]["takeWayList"]), 1)
        delivery = self.raw.price(dict(DELIVERY))["data"]
        self.assertNotIn("takeWayList", delivery)
        self.assertEqual(delivery["price"], 5000 + 600 + 100 + 100)

    # --- create-order 规则 ---
    def test_rule_validation(self) -> None:
        self.raw.price(PICKUP)
        good = self.raw.order_args()
        cases = {
            "缺 storeCode": {k: v for k, v in good.items() if k != "storeCode"},
            "缺 orderType": {k: v for k, v in good.items() if k != "orderType"},
            "缺 beType": {k: v for k, v in good.items() if k != "beType"},
            "不一致": dict(good, beType=2),
            "到店传 beCode": dict(good, beCode="X"),
            "得来速缺 beCode": dict(good, beType=5),
            "缺 takeWayCode": {k: v for k, v in good.items() if k != "takeWayCode"},
            "takeWayCode 不在列表": dict(good, takeWayCode="NOPE"),
            "items 为空": dict(good, items=[]),
            "缺 items": {k: v for k, v in good.items() if k != "items"},
            "外送缺 addressId": dict(DELIVERY, items=[ITEM]),
            "外送地址未知": dict(DELIVERY, items=[ITEM], addressId="NOPE"),
            "外送缺 beCode": {"storeCode": "30001", "orderType": 2, "beType": 2,
                           "items": [ITEM], "addressId": "ADDRID-1"},
        }
        for label, args in cases.items():
            with self.subTest(label):
                result = self.raw.data("create-order", args)
                self.assertEqual(result["code"], 400100, label)
        self.assertEqual(self.sim.orders, [])
        self.assertEqual(len(self.sim.controlled_calls), len(cases))

    def test_pickup_success_and_detail(self) -> None:
        payload = self.place()
        self.assertEqual(payload["code"], 0)
        data = payload["data"]
        self.assertEqual(data["orderId"], "SIM-ORDER-0001")
        self.assertIn(PAYURL_SENTINEL, data["payH5Url"])
        self.assertTrue(data["payH5Url"].startswith("http://sim.invalid/"))
        detail = data["orderDetail"]
        self.assertEqual(detail["orderStatus"], "1")
        self.assertEqual(detail["realTotalAmount"], "50.00")
        self.assertIsInstance(detail["realTotalAmount"], str)
        self.assertEqual(detail["pickupCode"], PICKUP_SENTINEL)
        self.assertEqual(detail["lockerCode"], LOCKER_SENTINEL)
        self.assertNotIn("deliveryInfo", detail)
        self.assertEqual(len(self.sim.orders), 1)
        self.assertEqual(self.sim.duplicate_orders, 0)

    def test_delivery_success_has_address_sentinels(self) -> None:
        self.raw.price(DELIVERY)
        payload = self.raw.data("create-order", dict(DELIVERY, items=[ITEM], addressId="ADDRID-1"))
        info = payload["data"]["orderDetail"]["deliveryInfo"]
        self.assertEqual(info["address"], ADDR_SENTINELS[0])
        self.assertEqual(info["riderPhone"], RIDER_SENTINEL)
        self.assertEqual(payload["data"]["orderDetail"]["realTotalAmount"], "57.00")

    def test_duplicates_counted_and_ids_unique(self) -> None:
        first = self.place()["data"]["orderId"]
        second = self.place()["data"]["orderId"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.sim.orders), 2)
        self.assertEqual(self.sim.duplicate_orders, 1)
        self.place(takeWayCode="SIM-TW-2")
        self.assertEqual(self.sim.duplicate_orders, 1)

    def test_query_order(self) -> None:
        order_id = self.place()["data"]["orderId"]
        data = self.raw.data("query-order", {"orderId": order_id})["data"]
        self.assertEqual(data["orderId"], order_id)
        self.assertEqual(data["orderStatus"], "1")
        self.assertEqual(data["realTotalAmount"], "50.00")
        self.assertEqual(data["pickupCode"], PICKUP_SENTINEL)
        self.assertEqual(self.raw.data("query-order", {"orderId": "NOPE"})["code"], 400100)
        self.assertIn("query-order", [c["tool"] for c in self.sim.calls])
        self.assertEqual(len(self.sim.orders), 1)

    def test_on_write_runs_before_response(self) -> None:
        seen = []
        self.sim.on_write = lambda tool, args: seen.append((tool, len(self.sim.orders), args.get("takeWayCode")))
        self.place()
        self.assertEqual(seen, [("create-order", 0, "SIM-TW-1")])
        self.raw.data("auto-bind-coupons", {})
        self.assertEqual(seen[-1][0], "auto-bind-coupons")

    # --- 故障模式 ---
    def test_business_fail(self) -> None:
        self.sim.fail("create-order", "business_fail")
        self.assertEqual(self.place()["code"], 500100)
        self.assertEqual(self.sim.orders, [])
        self.assertEqual(len(self.sim.controlled_calls), 1)

    def test_timeout_creates_order_after_client_gave_up(self) -> None:
        self.sim.timeout_delay = 0.5
        self.sim.fail("create-order", "timeout")
        self.raw.price(PICKUP)
        short = Raw(self.sim, timeout=0.15)
        with self.assertRaises((TimeoutError, socket.timeout, urllib.error.URLError)):
            short.tool("create-order", short.order_args())
        deadline = time.time() + 5
        while not self.sim.orders and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.sim.orders), 1)

    def test_lost_response_creates_order_without_reply(self) -> None:
        self.sim.fail("create-order", "lost_response")
        self.raw.price(PICKUP)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError, Exception)):
            self.raw.tool("create-order", self.raw.order_args())
        self.assertEqual(len(self.sim.orders), 1)
        self.assertEqual(len(self.sim.controlled_calls), 1)

    def test_disconnect_before_creation(self) -> None:
        self.sim.fail("create-order", "disconnect")
        self.raw.price(PICKUP)
        with self.assertRaises(Exception):
            self.raw.tool("create-order", self.raw.order_args())
        self.assertEqual(self.sim.orders, [])
        self.assertEqual(len(self.sim.controlled_calls), 1)

    def test_bad_structure_missing_order_id(self) -> None:
        self.sim.fail("create-order", "bad_structure")
        payload = self.place()
        self.assertEqual(payload["code"], 0)
        self.assertNotIn("orderId", payload["data"])
        self.assertEqual(len(self.sim.orders), 1)

    def test_redirect_not_followed(self) -> None:
        self.sim.fail("create-order", "redirect")
        self.raw.price(PICKUP)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.raw.tool("create-order", self.raw.order_args())
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(len(self.sim.controlled_calls), 1)
        self.assertEqual(self.sim.orders, [])

    def test_http_errors(self) -> None:
        self.raw.price(PICKUP)
        self.sim.fail("create-order", "http401")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.raw.tool("create-order", self.raw.order_args())
        self.assertEqual(caught.exception.code, 401)
        self.sim.fail("create-order", "http429:2")
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.raw.tool("create-order", self.raw.order_args())
            self.assertEqual(caught.exception.code, 429)
        self.assertEqual(self.sim.orders, [])
        self.assertEqual(self.raw.data("create-order", self.raw.order_args())["code"], 0)
        self.assertEqual(len(self.sim.orders), 1)
        self.assertEqual(len(self.sim.controlled_calls), 4)

    def test_auto_bind_faults(self) -> None:
        self.sim.fail("auto-bind-coupons", "business_fail")
        self.assertEqual(self.raw.data("auto-bind-coupons", {})["code"], 500100)
        self.assertEqual(self.sim.claims, 0)
        self.sim.fail("auto-bind-coupons", "http401")
        with self.assertRaises(urllib.error.HTTPError):
            self.raw.tool("auto-bind-coupons", {})
        self.assertEqual(self.sim.claims, 0)

    def test_sentinels_are_distinct_runtime_values(self) -> None:
        self.assertEqual(len(set(ORDER_SENTINELS)), 4)

    # --- 与官方 Schema 夹具一致 ---
    def test_schema_fixture(self) -> None:
        self.assertEqual(set(FIXTURE), {"create-order", "calculate-price", "auto-bind-coupons", "query-order"})
        self.assertEqual(FIXTURE["query-order"]["required"], ["orderId"])
        self.assertEqual(FIXTURE["auto-bind-coupons"], {"required": [], "properties": []})
        for tool in ("create-order", "calculate-price"):
            self.assertEqual(FIXTURE[tool]["required"], ["beType", "orderType", "storeCode"])
        self.assertTrue(set(self.raw.order_args()) <= set(FIXTURE["create-order"]["properties"]))
        self.assertLessEqual({"takeWayCode", "addressId", "beCode", "items", "needTableware"},
                             set(FIXTURE["create-order"]["properties"]))
        self.assertNotIn("takeWayCode", FIXTURE["calculate-price"]["properties"])
        raw = json.dumps(FIXTURE, ensure_ascii=False)
        self.assertNotIn("/Users/", raw)


if __name__ == "__main__":
    unittest.main()
