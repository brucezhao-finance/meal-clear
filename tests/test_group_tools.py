"""团餐只读工具：白名单、参数与模拟服务扩展（离线、回环、合成数据）。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear import config, mcp  # noqa: E402
from meal_clear.fulfillment import wire_context  # noqa: E402
from meal_clear.live import check_schemas  # noqa: E402
from meal_clear.mcp import (  # noqa: E402
    ALL_READ_ONLY, CONTROLLED_WRITE, GROUP_READ_ONLY, READ_ONLY, BusinessError, McpError,
    RateLimitedError, WriteRefused,
)
from meal_clear.request_v2 import validate_request_v2  # noqa: E402
from sim_mcp import GROUP_READ_TOOLS, READ_TOOLS, SimMcpServer  # noqa: E402
from sim_support import only_group_read_tools, only_read_tools, sim_client  # noqa: E402
# 摘自 tools-schema.json：入参字段与必填字段
SCHEMA = {
    "query-promotions": ({"beCode", "beType", "orderType", "reservationDate", "storeCode"},
                         {"storeCode", "orderType", "beType"}),
    "query-meal-assistance": ({"beCode", "beType", "orderType", "reservationDate", "storeCode"},
                              {"storeCode"}),
}
GROUP_REQ = {
    "schema_version": 2,
    "context": {"store_code": "30001", "meal_time": "now"},
    "fulfillment": {"type": "group", "be_code": "GBE30001", "address_index": 1,
                    "reservation_date": "2099-01-02 09:30"},
    "budget_cents": 200000,
    "people": [{"id": "A", "hard": {}, "prefer": {}}, {"id": "B", "hard": {}, "prefer": {}}],
}
ITEM = {"productCode": "M1", "quantity": 2}
class WhitelistTests(unittest.TestCase):
    """READ_ONLY 仍 12 个；新工具走 ALL_READ_ONLY；写白名单未扩大。"""
    def test_sets(self):
        self.assertEqual(len(READ_ONLY), 12)
        self.assertEqual(GROUP_READ_ONLY, frozenset({"query-promotions", "query-meal-assistance"}))
        self.assertEqual(ALL_READ_ONLY, READ_ONLY | GROUP_READ_ONLY)
        self.assertEqual(len(ALL_READ_ONLY), 14)
        self.assertFalse(READ_ONLY & GROUP_READ_ONLY)
        self.assertEqual(set(GROUP_READ_TOOLS), set(GROUP_READ_ONLY))
        self.assertEqual(len(READ_TOOLS), 12)
        self.assertEqual(set(config.GROUP_READ_ONLY_TOOLS), set(GROUP_READ_ONLY))
    def test_write_whitelist_unchanged(self):
        self.assertEqual(CONTROLLED_WRITE, frozenset({"auto-bind-coupons", "create-order"}))
        self.assertFalse(CONTROLLED_WRITE & ALL_READ_ONLY)
    def test_client_call_and_transport(self):
        """call() 接受团餐只读工具；仍拒绝写工具；传输层对非白名单零请求。"""
        with SimMcpServer() as sim:
            client = sim_client(sim)
            client.call("query-promotions", {"storeCode": "30001", "orderType": 2, "beType": 6,
                                             "beCode": "GBE30001"})
            for bad in ("create-order", "auto-bind-coupons", "cancel-order", "query-promotions-x"):
                with self.assertRaises(McpError):
                    client.call(bad, {})
            with self.assertRaises(WriteRefused):
                client._post("tools/call", {"name": "cancel-order", "arguments": {}})
            with self.assertRaises(WriteRefused):
                client._post("tools/call", {"name": "create-order", "arguments": {}})
            self.assertEqual([c["tool"] for c in sim.calls], ["query-promotions"])
            self.assertEqual(sim.write_attempts, [])
            self.assertTrue(only_group_read_tools(sim))
            self.assertFalse(only_read_tools(sim))  # 旧辅助仍只认 12 个
class SchemaAndArgsTests(unittest.TestCase):
    """入参恰为 schema 字段子集且含必填；Schema 变化时拒绝。"""
    def test_args_match_schema(self):
        req = validate_request_v2(GROUP_REQ)
        wire = wire_context(req)
        self.assertEqual(wire["beType"], 6)
        self.assertEqual(wire["reservationDate"], "2099-01-02 09:30")
        with SimMcpServer() as sim:
            client = sim_client(sim)
            client.call("query-promotions", wire)
            client.call("query-meal-assistance", wire)
            for call in sim.group_calls():
                props, required = SCHEMA[call["tool"]]
                self.assertTrue(set(call["args"]) <= props)
                self.assertTrue(required <= set(call["args"]))
                self.assertNotIn("gmServiceCode", call["args"])
    def test_tools_list_has_real_required(self):
        with SimMcpServer() as sim:
            tools = {t["name"]: t for t in sim_client(sim).list_tools()["tools"]}
            for name, (_, required) in SCHEMA.items():
                self.assertEqual(set(tools[name]["inputSchema"]["required"]), required)
            check_schemas(tools, GROUP_READ_TOOLS, frozenset())
    def test_schema_change_rejected(self):
        with SimMcpServer() as sim:
            tools = {t["name"]: t for t in sim_client(sim).list_tools()["tools"]}
        tools["query-promotions"] = {"name": "query-promotions",
                                     "inputSchema": {"required": ["storeCode", "newField"]}}
        with self.assertRaises(McpError):
            check_schemas(tools, GROUP_READ_TOOLS, frozenset())
        del tools["query-meal-assistance"]
        with self.assertRaises(McpError):
            check_schemas(tools, GROUP_READ_TOOLS, frozenset())
class SimGroupTests(unittest.TestCase):
    """模拟服务团餐规则与注入。"""
    def setUp(self):
        self.ctx = {"storeCode": "30001", "orderType": 2, "beType": 6, "beCode": "GBE30001"}
    def price(self, client, **extra):
        return client.call("calculate-price", {**self.ctx, "items": [ITEM], **extra})
    def test_group_stores_separate(self):
        with SimMcpServer() as sim:
            client = sim_client(sim)
            group = client.call("delivery-query-stores", {"beType": 6, "addressId": "ADDRID-1"})
            deliv = client.call("delivery-query-stores", {"beType": 2, "addressId": "ADDRID-1"})
            self.assertEqual(group["data"][0]["beCode"], "GBE30001")
            self.assertEqual(deliv["data"][0]["beCode"], "BE30001")
    def test_new_tool_rules(self):
        with SimMcpServer() as sim:
            client = sim_client(sim)
            for bad in ({"storeCode": "30001", "orderType": 2, "beType": 6},
                        {"storeCode": "30001", "orderType": 1, "beType": 6, "beCode": "G"},
                        {"orderType": 2, "beType": 6, "beCode": "G"}):
                with self.assertRaises(BusinessError):
                    client.call("query-promotions", bad)
            with self.assertRaises(BusinessError):
                client.call("query-meal-assistance", {"storeCode": "30001", "beType": 6, "orderType": 2})
            with self.assertRaises(BusinessError):
                client.call("query-meal-assistance", {**self.ctx, "reservationDate": "2099-01-02 09:30:00"})
            data = client.call("query-meal-assistance", self.ctx)["data"]["mealAssistanceItems"]
            self.assertEqual([x["gmServiceCode"] for x in data], ["GM-STD", "GM-ALT", "GM-PRO"])
            self.assertEqual(len(client.call("query-promotions", self.ctx)["data"]), 3)
    def test_gm_validation_on_price(self):
        with SimMcpServer() as sim:
            client = sim_client(sim)
            for extra in ({}, {"gmServiceCode": "NOPE"}, {"gmServiceCode": "GM-PRO"}):
                with self.assertRaises(BusinessError):
                    self.price(client, **extra)
            ok = self.price(client, gmServiceCode="GM-STD", reservationDate="2099-01-02 09:30")
            self.assertEqual(ok["data"]["productPrice"], 5000)
            with self.assertRaises(BusinessError):
                self.price(client, gmServiceCode="GM-STD", reservationDate="2099-01-02")
            with self.assertRaises(BusinessError):  # 非团餐不接受 gmServiceCode
                client.call("calculate-price", {"storeCode": "30001", "orderType": 2, "beType": 2,
                                                "beCode": "BE30001", "items": [ITEM],
                                                "gmServiceCode": "GM-STD"})
    def test_discount_only_affects_calculate_price(self):
        with SimMcpServer() as sim:
            client = sim_client(sim)
            base = self.price(client, gmServiceCode="GM-STD")["data"]["price"]
            sim.set_group_promotions([])
            self.assertEqual(self.price(client, gmServiceCode="GM-STD")["data"]["price"], base)
            self.assertEqual(client.call("query-promotions", self.ctx)["data"], [])
            sim.set_group_discount(700)
            self.assertEqual(self.price(client, gmServiceCode="GM-STD")["data"]["price"], base - 700)
            sim.set_group_discount(0)
            self.assertEqual(self.price(client, gmServiceCode="GM-STD")["data"]["price"], base)
    def test_fault_injection_covers_new_tools(self):
        for tool in GROUP_READ_TOOLS:
            for mode, exc in (("http401", McpError), ("tool_error", McpError),
                              ("business_code", BusinessError), ("bad_structure", McpError),
                              ("garbage_json", McpError)):
                with SimMcpServer() as sim:
                    client = sim_client(sim)
                    sim.fail(tool, mode)
                    with self.assertRaises(exc):
                        client.call(tool, self.ctx)
            with SimMcpServer() as sim:
                client = sim_client(sim)
                client.retry_delay = 0
                sim.fail(tool, "http429:99")
                with self.assertRaises(RateLimitedError):
                    client.call(tool, self.ctx)
    def test_create_order_group_rules(self):
        """sim 的 create-order 团餐校验（直接对回环服务，不经客户端写门）。"""
        from urllib import request as ur
        import json as _json
        from sim_mcp import SIM_TOKEN
        def post(sim, args):
            body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "create-order", "arguments": args}}).encode()
            req = ur.Request(sim.url, data=body, headers={"Authorization": "Bearer " + SIM_TOKEN,
                                                          "Content-Type": "application/json"})
            with ur.urlopen(req) as resp:
                return _json.loads(resp.read())["result"]["structuredContent"]
        base = {**self.ctx, "addressId": "ADDRID-1", "items": [ITEM], "needTableware": False}
        with SimMcpServer() as sim:
            self.assertNotEqual(post(sim, base)["code"], 0)
            self.assertNotEqual(post(sim, {**base, "gmServiceCode": "GM-PRO"})["code"], 0)
            self.assertNotEqual(post(sim, {**base, "gmServiceCode": "GM-STD", "takeWayCode": "T"})["code"], 0)
            self.assertEqual(len(sim.orders), 0)
            self.assertEqual(post(sim, {**base, "gmServiceCode": "GM-STD"})["code"], 0)
            self.assertEqual(len(sim.orders), 1)
if __name__ == "__main__":
    unittest.main()
