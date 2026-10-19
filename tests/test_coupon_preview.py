"""券预览（T-D1~D4）：三类券、白名单字段、未领券不计价、单类失败不影响其它。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear.privacy import sanitize_claimable, sanitize_held  # noqa: E402
from sim_mcp import CLAIMABLE, COUPON_SENTINELS, COUPONS, MY_COUPONS, SimMcpServer  # noqa: E402
from sim_support import SENTINELS, only_read_tools, request, run_cli, write_json  # noqa: E402

BEEF = [{"id": "A", "hard": {}, "prefer": {"main_meat": "beef"}}]
PREF = {"priority": "preference"}


class CouponPreviewTests(unittest.TestCase):
    """coupons 命令与 live-plan 的券字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_d1_three_categories_and_notice(self):
        """三类券均出现，并带 P1 与一键领全部说明。"""
        with SimMcpServer() as sim:
            path = write_json(self.dir, "r.json", request())
            code, out = run_cli(sim, "coupons", "--request", path)
            self.assertEqual(code, 0)
            for needle in ("本店可用：模拟满减券", "可领取：模拟可领券1", "已领取：模拟已领券1",
                           "领券属后续阶段（P1）", "auto-bind-coupons 只能一键领取全部"):
                self.assertIn(needle, out)
            self.assertTrue(only_read_tools(sim))

    def test_d2_no_coupon_secrets_anywhere(self):
        """文本、JSON、live-plan 输出与保存文件都不含券码/券 id/图片哨兵。"""
        with SimMcpServer() as sim:
            path = write_json(self.dir, "r.json", request(BEEF))
            state = str(self.dir / "s.json")
            texts = []
            texts.append(run_cli(sim, "coupons", "--request", path)[1])
            code, out = run_cli(sim, "coupons", "--request", path, "--json")
            preview = json.loads(out)
            self.assertEqual(len(preview["claimable"]), len(CLAIMABLE))
            self.assertEqual(len(preview["held"]), len(MY_COUPONS))
            self.assertEqual(len(preview["store_usable"]), len(COUPONS))
            texts.append(out)
            texts.append(run_cli(sim, "live-plan", "--request", path, "--json", "--save", state)[1])
            texts.append(run_cli(sim, "live-plan", "--request", path)[1])
            texts.append(Path(state).read_text(encoding="utf-8"))
            for text in texts:
                for word in SENTINELS + COUPON_SENTINELS + ("couponCode", "couponId"):
                    self.assertNotIn(word, text)
            self.assertTrue(only_read_tools(sim))

    def test_d3_sanitizers_drop_identifiers(self):
        """白名单：丢弃 code、id、图片与可领券的券码。"""
        held = sanitize_held({"data": {"list": MY_COUPONS}})
        self.assertEqual(held[0]["title"], "模拟已领券1")
        for row in held:
            self.assertFalse({"code", "id", "image"} & set(row))
        claimable = sanitize_claimable({"data": CLAIMABLE})
        self.assertTrue(all("couponCode" not in r for r in claimable))

    def test_d4_only_store_usable_coupons_priced(self):
        """报价只使用本店可用券；未领取的可领/已领券不进入核价请求；带 coupon_note。"""
        with SimMcpServer() as sim:
            path = write_json(self.dir, "r.json", request(BEEF, ranking=PREF))
            code, out = run_cli(sim, "live-plan", "--request", path, "--json")
            data = json.loads(out)
            self.assertEqual(code, 0)
            top = data["ranking"][0]
            self.assertEqual([c["title"] for c in top["coupons"]], ["模拟立减券"])
            self.assertIn("未领取的券不计入本次报价", top["coupon_note"])
            self.assertEqual(top["total_cents"], 3200 - 500)
            used = {
                item.get("couponId")
                for call in sim.calls if call["tool"] == "calculate-price"
                for item in call["args"]["items"] if item.get("couponId")
            }
            self.assertEqual(used, {"COUPONID-2"})
            self.assertTrue(used <= {c["couponId"] for c in COUPONS})
            self.assertEqual(data["coupon_preview"]["store_usable"][0]["title"], "模拟满减券")
            self.assertTrue(only_read_tools(sim))

    def test_one_category_failure_does_not_block_others(self):
        """可领券查询失败：其余两类仍展示，并说明失败。"""
        with SimMcpServer() as sim:
            sim.fail("available-coupons", "tool_error")
            path = write_json(self.dir, "r.json", request())
            code, out = run_cli(sim, "coupons", "--request", path, "--json")
            preview = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(preview["claimable"], [])
            self.assertTrue(preview["held"] and preview["store_usable"])
            self.assertTrue(any("可领取券查询失败" in n for n in preview["notes"]))


if __name__ == "__main__":
    unittest.main()
