"""v2 实时多方案端到端：CLI → MultiLivePlanner → 回环模拟 MCP 服务器。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear.live import MultiLivePlanner  # noqa: E402
from meal_clear.mcp import McpError  # noqa: E402
from sim_mcp import SimMcpServer  # noqa: E402
from sim_support import (  # noqa: E402
    SENTINELS, only_read_tools, request, run_cli, sim_client, write_json,
)

THREE = [
    {"id": "A", "hard": {"non_spicy": True, "sugar_free_drink": True}, "prefer": {"main_meat": "chicken"}},
    {"id": "B", "hard": {"non_spicy": True}, "prefer": {"main_meat": "beef"}},
    {"id": "C", "hard": {}, "prefer": {}},
]


def tool_args(sim, tool):
    """某工具在服务端收到的全部入参。"""
    return [c["args"] for c in sim.calls if c["tool"] == tool]


class MultiE2ETests(unittest.TestCase):
    """四类取餐场景与边界。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, sim, req, *extra):
        path = write_json(self.dir, "req.json", req)
        return run_cli(sim, "live-plan", "--request", path, "--json", *extra)

    def test_pickup_single(self):
        """到店单人：非空排名，参数为 beType1/orderType1 且不带 beCode。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, request())
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertTrue(data["ranking"])
            self.assertEqual(data["mode"], "live")
            self.assertEqual(data["ranking"][0]["price_status"], "official_verified")
            for tool in ("query-meals", "query-meal-detail", "query-store-coupons", "calculate-price"):
                for args in tool_args(sim, tool):
                    self.assertEqual((args["beType"], args["orderType"]), (1, 1))
                    self.assertNotIn("beCode", args)
                    self.assertEqual(args["storeCode"], "10001")
            self.assertTrue(only_read_tools(sim))

    def test_pickup_three_people(self):
        """到店三人：每人都有分配，官方核价数不超过默认上限 12。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, request(THREE))
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(len(data["ranking"]), 3)
            for item in data["ranking"]:
                self.assertEqual([a["person_id"] for a in item["assignments"]], ["A", "B", "C"])
            self.assertLessEqual(len(tool_args(sim, "calculate-price")), 12)
            self.assertEqual(data["ranking_meta"]["checked"]["quotes_attempted"],
                             len(tool_args(sim, "calculate-price")))
            self.assertEqual(data["ranking_meta"]["checked"]["run_calls"], len(sim.calls) + 1)
            self.assertTrue(only_read_tools(sim))

    def test_drive_thru(self):
        """得来速：beType5/orderType1 且全部请求带 beCode。"""
        with SimMcpServer() as sim:
            req = request(store="20001", fulfillment={"type": "drive_thru", "be_code": "BE20001"})
            code, out = self.plan(sim, req)
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(data["fulfillment"]["be_type"], 5)
            self.assertTrue(tool_args(sim, "calculate-price"))
            for tool in ("query-meals", "query-meal-detail", "query-store-coupons", "calculate-price"):
                for args in tool_args(sim, tool):
                    self.assertEqual((args["beType"], args["orderType"], args["beCode"]), (5, 1, "BE20001"))
            self.assertTrue(only_read_tools(sim))

    def test_delivery_with_address_index(self):
        """外送：beType2/orderType2/beCode；运费入账；输出与状态文件无地址敏感信息。"""
        with SimMcpServer() as sim:
            req = request(store="30001", fulfillment={"type": "delivery", "be_code": "BE30001", "address_index": 1})
            state = str(self.dir / "state.json")
            code, out = self.plan(sim, req, "--save", state)
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(data["fulfillment"]["address_label"], "地址1")
            self.assertEqual(len(data["fulfillment"]["address_ref"]), 8)
            self.assertEqual(data["ranking"][0]["fee_breakdown"]["deliveryPrice"], 600)
            # 最低价为带券方案：2500 - 500 券减免 + 600 配送费 + 100 包装费
            self.assertEqual(data["ranking"][0]["total_cents"], 2700)
            for tool in ("query-meals", "query-meal-detail", "query-store-coupons", "calculate-price"):
                for args in tool_args(sim, tool):
                    self.assertEqual((args["beType"], args["orderType"], args["beCode"]), (2, 2, "BE30001"))
            self.assertEqual(tool_args(sim, "delivery-query-stores"), [{"beType": 2, "addressId": "ADDRID-1"}])
            saved = Path(state).read_text(encoding="utf-8")
            for text in (out, saved):
                self.assertIn("地址1", text)
                for word in SENTINELS:
                    self.assertNotIn(word, text)
            self.assertTrue(only_read_tools(sim))

    def test_delivery_second_address_and_text_output(self):
        """用序号2选第二个地址；文本输出含标签、不含哨兵。"""
        with SimMcpServer() as sim:
            req = request(store="30002", fulfillment={"type": "delivery", "be_code": "BE30002", "address_index": 2})
            path = write_json(self.dir, "r.json", req)
            code, out = run_cli(sim, "live-plan", "--request", path)
            self.assertEqual(code, 0)
            self.assertIn("地址2", out)
            for word in SENTINELS:
                self.assertNotIn(word, out)
            self.assertEqual(tool_args(sim, "delivery-query-stores")[0]["addressId"], "ADDRID-2")

    def test_delivery_store_not_deliverable_or_bad_index(self):
        """门店不在可配送列表 / 序号越界：明确报错，不继续核价，错误信息无敏感内容。"""
        with SimMcpServer() as sim:
            req = request(store="30001", fulfillment={"type": "delivery", "be_code": "BE30001", "address_index": 2})
            code, out = self.plan(sim, req)
            self.assertEqual(code, 1)
            self.assertIn("可配送列表", out)
            self.assertEqual(tool_args(sim, "calculate-price"), [])
            req = request(store="30001", fulfillment={"type": "delivery", "be_code": "BE30001", "address_index": 3})
            code, out = self.plan(sim, req)
            self.assertEqual(code, 1)
            self.assertIn("超出地址列表范围", out)
            for word in SENTINELS:
                self.assertNotIn(word, out)
            self.assertTrue(only_read_tools(sim))

    def test_quote_cap_ta8(self):
        """T-A8：max_quotes=2 时核价恰好 2 次，并声明仅检查了前 2 个。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, request(limits={"max_quotes": 2}))
            data = json.loads(out)
            self.assertEqual(len(tool_args(sim, "calculate-price")), 2)
            self.assertIn("仅检查了前 2 个", data["ranking_meta"]["checked"]["statement"])
            self.assertIn("不是全局最优", data["ranking_meta"]["checked"]["statement"])
            self.assertNotIn("全网最低", out)

    def test_rate_limit_429_stops_quotes(self):
        """429 重试耗尽后立即停止核价并说明；无推荐归 needs_price_verification。"""
        with SimMcpServer() as sim:
            sim.fail("calculate-price", "http429:1000")
            code, out = self.plan(sim, request())
            data = json.loads(out)
            self.assertEqual(code, 2)
            self.assertEqual(len(tool_args(sim, "calculate-price")), 3)  # 仅一个候选的 3 次重试
            self.assertIn("限流", data["ranking_meta"]["checked"]["statement"])
            self.assertEqual(data["no_result"]["category"], "needs_price_verification")
            self.assertTrue(only_read_tools(sim))

    def test_no_result_live_branches(self):
        """无推荐的三种实时分支：属性未知、超预算、待核价（见 429 用例）。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, request([{"id": "A", "hard": {"main_meat": "tofu"}, "prefer": {}}]))
            data = json.loads(out)
            self.assertEqual((code, data["no_result"]["category"]), (2, "attribute_unknown"))
            code, out = self.plan(sim, request(budget=100))
            data = json.loads(out)
            self.assertEqual((code, data["no_result"]["category"]), (2, "over_budget_in_checked_set"))
            self.assertEqual(data["status"], "over_budget_in_checked_set")
            self.assertTrue(only_read_tools(sim))

    def test_update_live_v2_requeries(self):
        """update 复用 v2 实时路径：重新取数与核价，地址引用不变。"""
        with SimMcpServer() as sim:
            req = request(store="30001", fulfillment={"type": "delivery", "be_code": "BE30001", "address_index": 1})
            state = str(self.dir / "s.json")
            self.plan(sim, req, "--save", state)
            before = len(tool_args(sim, "calculate-price"))
            patch = write_json(self.dir, "p.json", {"budget_cents": 9000})
            code, out = run_cli(sim, "update", "--state", state, "--patch", patch, "--json")
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertGreater(len(tool_args(sim, "calculate-price")), before)
            self.assertEqual(data["request"]["budget_cents"], 9000)
            self.assertEqual(data["fulfillment"]["address_ref"],
                             json.loads(Path(state).read_text("utf-8"))["fulfillment"]["address_ref"])
            self.assertNotIn("地址列表顺序可能已变化", out)

    def test_new_commands_addresses_and_stores(self):
        """addresses / delivery-stores / stores --be-type：仅序号标签与门店信息。"""
        with SimMcpServer() as sim:
            code, out = run_cli(sim, "addresses")
            self.assertEqual(code, 0)
            self.assertIn("地址1", out)
            self.assertIn("地址2", out)
            for word in SENTINELS:
                self.assertNotIn(word, out)
            code, out = run_cli(sim, "delivery-stores", "--address-index", "2")
            stores = json.loads(out)["stores"]
            self.assertEqual([s["storeCode"] for s in stores], ["30002"])
            self.assertEqual(stores[0]["beCode"], "BE30002")
            code, out = run_cli(sim, "delivery-stores", "--address-index", "9")
            self.assertEqual(code, 1)
            code, out = run_cli(sim, "stores", "--city", "模拟", "--keyword", "模拟", "--be-type", "5")
            self.assertEqual(json.loads(out)["stores"][0]["beCode"], "BE20001")
            code, out = run_cli(sim, "stores", "--city", "模拟", "--keyword", "模拟")
            self.assertNotIn("beCode", json.loads(out)["stores"][0])
            self.assertTrue(only_read_tools(sim))

    def test_group_not_implemented_no_network(self):
        """团餐未实现：校验阶段报错，服务端零调用。"""
        with SimMcpServer() as sim:
            req = request(fulfillment={"type": "group", "be_code": "X", "address_index": 1})
            code, out = self.plan(sim, req)
            self.assertEqual(code, 1)
            self.assertIn("尚未实现", out)
            self.assertEqual(sim.calls, [])

    def test_run_call_cap(self):
        """BudgetedClient 超限：在发请求前抛出明确错误。"""
        from meal_clear.request_v2 import validate_request_v2
        with SimMcpServer() as sim:
            planner = MultiLivePlanner(sim_client(sim), max_calls=3)
            with self.assertRaises(McpError) as ctx:
                planner.plan_v2(validate_request_v2(request()))
            self.assertIn("调用上限", str(ctx.exception))
            self.assertLessEqual(len(sim.calls), 3)

    def test_privacy_all_channels(self):
        """T-C：异常、audit、repr 拼接后无哨兵。"""
        with SimMcpServer() as sim:
            client = sim_client(sim)
            planner = MultiLivePlanner(client)
            addresses = planner.list_addresses()
            text = repr(addresses) + str(client.audit) + json.dumps([a.public() for a in addresses])
            for word in ("ADDR-SENTINEL", "PHONE-SENTINEL", "CONTACT-SENTINEL", "ADDRID", "SIM-TOKEN"):
                self.assertNotIn(word, text)
            self.assertIn("地址2", text)
            self.assertFalse(hasattr(planner, "payload"))


if __name__ == "__main__":
    unittest.main()
