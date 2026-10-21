"""50 人实时（回环模拟）：核价次数上限、调用总数、载荷合并、限流（T-S7、T-S8）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from meal_clear.config import RUN_CALL_CAP  # noqa: E402
from meal_clear.live import MultiLivePlanner, merge_wire_items  # noqa: E402
from meal_clear.mcp import RateLimiter  # noqa: E402
from sim_mcp import SimMcpServer  # noqa: E402
from sim_support import only_read_tools, request, run_cli, sim_client, write_json  # noqa: E402
from test_sim_server import FakeClock, make  # noqa: E402

MEATS = ("chicken", "beef", "pork")


def people(n):
    """n 位用餐者，3 种偏好轮换（3 个同质组），同一硬要求。"""
    return [
        {"id": f"U{i + 1:02d}", "hard": {"non_spicy": True}, "prefer": {"main_meat": MEATS[i % 3]}}
        for i in range(n)
    ]


def calc_args(sim):
    """服务端收到的全部 calculate-price 入参。"""
    return [c["args"] for c in sim.calls if c["tool"] == "calculate-price"]


class ScaleLiveTests(unittest.TestCase):
    """规模 × 实时流程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def live(self, sim, req, *extra):
        path = write_json(self.dir, "req.json", req)
        code, out = run_cli(sim, "live-plan", "--request", path, "--json", *extra)
        return code, json.loads(out)

    def test_S7_quotes_and_call_caps_for_each_size(self):
        """1/3/6/20/50 人：核价 ≤ max_quotes、运行调用 ≤150，且仍只读。"""
        for n in (1, 3, 6, 20, 50):
            with self.subTest(people=n), SimMcpServer() as sim:
                code, data = self.live(sim, request(people(n), budget=10**7))
                self.assertEqual(code, 0)
                self.assertTrue(data["ranking"])
                self.assertEqual(len(data["ranking"][0]["assignments"]), n)
                checked = data["ranking_meta"]["checked"]
                self.assertLessEqual(len(calc_args(sim)), checked["quote_cap"])
                self.assertLessEqual(len(calc_args(sim)), 12)
                self.assertLessEqual(checked["run_calls"], RUN_CALL_CAP)
                self.assertLessEqual(len(sim.calls), RUN_CALL_CAP)
                self.assertTrue(only_read_tools(sim))
                if n > 6:
                    self.assertIn("同质组", checked["statement"])

    def test_S7_max_quotes_one_states_limit(self):
        """max_quotes=1：只核价 1 次，声明『仅检查了前 1 个』且含『不是全局最优』。"""
        with SimMcpServer() as sim:
            req = request(people(50), budget=10**7, limits={"max_quotes": 1})
            code, data = self.live(sim, req)
            self.assertEqual(code, 0)
            self.assertEqual(len(calc_args(sim)), 1)
            statement = data["ranking_meta"]["checked"]["statement"]
            self.assertIn("仅检查了前 1 个", statement)
            self.assertIn("不是全局最优", statement)
            self.assertTrue(only_read_tools(sim))

    def test_S7_rate_limiter_respected_with_fake_clock(self):
        """小窗口限流：任意连续 limit+1 次调用的时间跨度 ≥ 窗口，结果不受影响。"""
        limit, window = 10, 60.0
        with SimMcpServer() as sim:
            clock = FakeClock()
            limiter = RateLimiter(limit, window, clock=clock, sleep=clock.sleep)
            stamps = []
            original = limiter.acquire

            def recorded():
                original()
                stamps.append(clock.now)

            limiter.acquire = recorded
            client = make(sim, rate_limiter=limiter)
            client.initialize()
            data = MultiLivePlanner(client).plan_v2(request(people(50), budget=10**7))
            self.assertTrue(data["ranking"])
            self.assertTrue(clock.slept)
            for i in range(len(stamps) - limit):
                self.assertGreaterEqual(stamps[i + limit] - stamps[i], window)

    def test_S8_payload_merged(self):
        """服务端收到的商品行数=不同商品行数，数量总和=人数；券行数量 1。"""
        for n in (3, 20, 50):
            with self.subTest(people=n), SimMcpServer() as sim:
                code, data = self.live(sim, request(people(n), budget=10**7))
                self.assertEqual(code, 0)
                for args in calc_args(sim):
                    items = args["items"]
                    self.assertEqual(sum(i["quantity"] for i in items), n)
                    plain = [i for i in items if not i.get("couponId")]
                    keys = {json.dumps([i["productCode"], i["roundList"]], sort_keys=True) for i in plain}
                    self.assertEqual(len(keys), len(plain))
                    for line in items:
                        if line.get("couponId"):
                            self.assertEqual(line["quantity"], 1)
                    if n >= 20:
                        self.assertLess(len(items), n)
                self.assertTrue(only_read_tools(sim))

    def test_merge_wire_items_rules(self):
        """合并规则：相同行累加；券行恒为 1 且不与普通行合并；不同轮次选择不合并。"""
        def row(code, choice="MAIN", coupon=False, qty=1):
            item = {"productCode": code, "quantity": qty,
                    "roundList": [{"round": "1", "comboItemList": [{"code": choice, "quantity": 1}]}]}
            if coupon:
                item.update(couponId="id1", couponCode="code1")
            return {"wire_items": [item]}

        merged = merge_wire_items(
            [row("M1"), row("M1"), row("M1", coupon=True), row("M1", choice="ZERO"), row("M2"), row("M1")]
        )
        by = [(m["productCode"], m["roundList"][0]["comboItemList"][0]["code"], bool(m.get("couponId")), m["quantity"])
              for m in merged]
        self.assertEqual(by, [("M1", "MAIN", False, 3), ("M1", "MAIN", True, 1), ("M1", "ZERO", False, 1), ("M2", "MAIN", False, 1)])
        source = [row("M1"), row("M1")]
        merge_wire_items(source)
        self.assertEqual(source[0]["wire_items"][0]["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
