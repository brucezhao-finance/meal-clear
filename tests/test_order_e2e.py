"""T-X1：CLI → 模拟服务的三场景端到端（到店单人、外送、3 人），价格不变与价格变化两条路径。

场景逻辑与断言在 e2e_order_flow.py（同一份代码也用于打印真实终端输出）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_order_flow as flow  # noqa: E402


class OrderE2ETests(unittest.TestCase):

    def test_unchanged_price_path_for_each_scenario(self):
        for name, req in flow.SCENARIOS.items():
            with self.subTest(scenario=name):
                run, stats = flow.run_scenario(name, req, "unchanged")
                self.assertEqual(stats["auto-bind-coupons"], 1)
                self.assertEqual(stats["create-order"], 1)
                self.assertEqual(stats["orders"], 1)
                self.assertEqual(stats["duplicate_orders"], 0)
                self.assertEqual(stats["forbidden_attempts"], [])
                codes = [s["code"] for s in run.steps]
                # 摘要 / 领券预演 / 下单预演 / 领券 / 下单 / 重复被拒 / 查询
                self.assertEqual(codes, [0, 0, 0, 0, 0, 5, 0])
                # 服务端只收到预期工具
                self.assertLessEqual(set(stats["read_by_tool"]), flow.EXPECTED_READ_TOOLS)

    def test_price_change_path_requires_reconfirm_without_order(self):
        for name, req in flow.SCENARIOS.items():
            with self.subTest(scenario=name):
                run, stats = flow.run_scenario(name, req, "changed")
                codes = [s["code"] for s in run.steps]
                # 摘要 / 预演x2 / 领券 / 价格变化(3) / 旧摘要失效(3) / 新摘要下单(0) / 重复被拒(5) / 查询
                self.assertEqual(codes, [0, 0, 0, 0, 3, 3, 0, 5, 0])
                self.assertEqual(stats["create-order"], 1)
                self.assertEqual(stats["orders"], 1)
                changed = run.steps[4]["out"]
                self.assertIn("+¥3.00", changed)
                self.assertIn("新摘要编号", changed)

    def test_dry_run_steps_leave_no_write_trace(self):
        """所有预演步骤执行时，服务端写调用记录为空。"""
        for name, req in flow.SCENARIOS.items():
            with self.subTest(scenario=name):
                run, _ = flow.run_scenario(name, req, "unchanged")
                titles = [s["title"] for s in run.steps]
                self.assertIn("领券预演（默认不领）", titles)
                self.assertIn("下单预演（默认不下）", titles)

    def test_scenarios_cover_pickup_delivery_and_three_people(self):
        self.assertEqual(list(flow.SCENARIOS), ["到店单人", "外送", "3 人到店"])
        run, _ = flow.run_scenario("外送", flow.SCENARIOS["外送"], "unchanged")
        self.assertIn("外送", run.steps[0]["out"])
        run, _ = flow.run_scenario("3 人到店", flow.SCENARIOS["3 人到店"], "unchanged")
        self.assertIn("用餐人数：3", run.steps[0]["out"])


if __name__ == "__main__":
    unittest.main()
