"""团餐实时规划：CLI → MultiLivePlanner → 回环模拟服务（助餐服务、促销仅提示、搭配启发、理由、隐私）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear import config, group_meal  # noqa: E402
from meal_clear.live import MultiLivePlanner  # noqa: E402
from meal_clear.request_v2 import validate_request_v2  # noqa: E402
from sim_mcp import GROUP_READ_TOOLS, SimMcpServer  # noqa: E402
from sim_support import (  # noqa: E402
    SENTINELS, only_group_read_tools, request, run_cli, sim_client, write_json,
)
GM = "GM-STD"
def group_ful(**extra):
    """团餐取餐方式（合成：地址1、团餐店A、助餐服务 GM-STD）。"""
    ful = {"type": "group", "be_code": "GBE30001", "address_index": 1, "gm_service_code": GM}
    ful.update(extra)
    return ful
def people(n):
    """n 位无约束用餐者。"""
    return [{"id": f"P{i:02d}", "hard": {}, "prefer": {}} for i in range(n)]
def greq(n=20, ful=None, **extra):
    """团餐 v2 请求，默认 20 人、总预算 200050 分。"""
    return request(people=people(n), fulfillment=ful or group_ful(), store="30001",
                   budget=extra.pop("budget", 200050), **extra)
def stable(result):
    """去掉时间戳后的排名投影，用于确定性比较。"""
    return [
        (i["rank"], i["signature"], i["total_cents"],
         [r for r in i["reasons"] if "官方核价（" not in r])
        for i in result["ranking"]
    ]
class GroupLiveTests(unittest.TestCase):
    """端到端行为。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def plan(self, sim, req, *extra):
        path = write_json(self.dir, "req.json", req)
        return run_cli(sim, "live-plan", "--request", path, "--json", *extra)
    def test_call_sequence_and_params(self):
        """先助餐服务后菜单；核价带 gmServiceCode；其他工具不带；无 takeWayCode。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, greq(ful=group_ful(reservation_date="2099-01-02 09:30")))
            data = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertTrue(data["ranking"])
            self.assertEqual(data["ranking"][0]["price_status"], "official_verified")
            tools = [c["tool"] for c in sim.calls]
            self.assertLess(tools.index("query-meal-assistance"), tools.index("query-meals"))
            self.assertLess(tools.index("query-promotions"), tools.index("query-meals"))
            self.assertEqual(tools.count("query-meal-assistance"), 1)
            self.assertEqual(tools.count("query-promotions"), 1)
            self.assertIn("calculate-price", tools)
            for call in sim.calls:
                args = call["args"]
                if call["tool"] == "calculate-price":
                    self.assertEqual(args["gmServiceCode"], GM)
                else:
                    self.assertNotIn("gmServiceCode", args)
                self.assertNotIn("takeWayCode", args)
                if call["tool"] in ("query-meals", "query-meal-detail", "query-store-coupons",
                                    "calculate-price", "query-promotions", "query-meal-assistance"):
                    self.assertEqual((args["beType"], args["orderType"]), (6, 2))
                    self.assertEqual(args["beCode"], "GBE30001")
                    self.assertEqual(args["reservationDate"], "2099-01-02 09:30")
            stores = [c["args"] for c in sim.calls if c["tool"] == "delivery-query-stores"]
            self.assertEqual([a["beType"] for a in stores], [6])
            self.assertTrue(only_group_read_tools(sim))
            self.assertEqual(sim.write_attempts, [])
            self.assertEqual(data["group"]["budget"]["per_person_cents"], 10002)
            self.assertEqual(data["group"]["budget"]["remainder_cents"], 10)
    def test_no_reservation_param_when_absent(self):
        with SimMcpServer() as sim:
            code, _ = self.plan(sim, greq())
            self.assertEqual(code, 0)
            for call in sim.calls:
                self.assertNotIn("reservationDate", call["args"])
    def test_promotions_are_hints_only(self):
        """有/无/不同促销规则：总价与排名完全一致；提示只出现在理由里。"""
        runs = []
        other = [dict(r) for r in group_meal_rules()]
        other[0] = dict(other[0], ruleDetail={"orderReduce": {"reduceInfo": [
            {"startDiscountPoint": "1", "reduceAmount": "9999"}]}})
        for rules in (None, [], other):
            with SimMcpServer() as sim:
                if rules is not None:
                    sim.set_group_promotions(rules)
                code, out = self.plan(sim, greq())
                self.assertEqual(code, 0, out)
                runs.append(json.loads(out))
        base = [(i["signature"], i["total_cents"], i["rank"]) for i in runs[0]["ranking"]]
        for other_run in runs[1:]:
            self.assertEqual(base, [(i["signature"], i["total_cents"], i["rank"]) for i in other_run["ranking"]])
        self.assertTrue(any("促销提示" in r for r in runs[0]["ranking"][0]["reasons"]))
        self.assertFalse(any("促销提示" in r for r in runs[1]["ranking"][0]["reasons"]))
        self.assertTrue(any("以官方核价" in r for r in runs[0]["ranking"][0]["reasons"]))
        self.assertEqual(runs[1]["group"]["promotions"], [])
    def test_price_only_follows_calculate_price(self):
        """sim 折扣开关只改 calculate-price：总价随之等额下降，排名不变。"""
        results = []
        for discount in (0, 700):
            with SimMcpServer() as sim:
                sim.set_group_discount(discount)
                code, out = self.plan(sim, greq())
                self.assertEqual(code, 0)
                results.append(json.loads(out))
        for a, b in zip(results[0]["ranking"], results[1]["ranking"]):
            self.assertEqual(a["signature"], b["signature"])
            self.assertEqual(a["total_cents"] - 700, b["total_cents"])
    def test_gm_missing_invalid_disabled_stop_early(self):
        """gm 缺失/无效/不可用：exit 1，零核价零菜单，不采用官方 selected 默认项。"""
        cases = {
            "missing": {k: v for k, v in group_ful().items() if k != "gm_service_code"},
            "invalid": group_ful(gm_service_code="NOPE"),
            "disabled": group_ful(gm_service_code="GM-PRO"),
        }
        for label, ful in cases.items():
            with SimMcpServer() as sim:
                code, out = self.plan(sim, greq(ful=ful))
                body = json.loads(out)
                self.assertEqual(code, 1, label)
                self.assertEqual(body["status"], "need_gm_service_code")
                self.assertRegex(body["message"], "[\u4e00-\u9fff]")
                tools = {c["tool"] for c in sim.calls}
                self.assertFalse(tools & {"calculate-price", "query-meals", "query-meal-detail",
                                          "query-store-coupons"}, label)
                self.assertIn("query-meal-assistance", tools)
                default = [o for o in body["options"] if o["official_default"]]
                self.assertEqual([o["gm_service_code"] for o in default], ["GM-STD"])
                if label == "missing":
                    self.assertIn("不会自动采用", body["message"])
    def test_assistance_failures_stop(self):
        """助餐服务查询失败或结构异常：停止，零核价。"""
        for mode in ("http401", "http429:99", "tool_error", "bad_structure", "business_code", "garbage_json"):
            with SimMcpServer() as sim:
                sim.fail("query-meal-assistance", mode)
                code, out = self.plan(sim, greq())
                self.assertEqual(code, 1, mode)
                self.assertFalse({c["tool"] for c in sim.calls} & {"calculate-price", "query-meals"}, mode)
                self.assertNotIn("SIM-SECRET", out)
        with SimMcpServer() as sim:
            sim.set_group_assistance([])
            code, out = self.plan(sim, greq())
            self.assertEqual(code, 1)
            self.assertEqual(sim.group_calls()[0]["tool"], "query-meal-assistance")
            self.assertFalse({c["tool"] for c in sim.calls} & {"calculate-price"})
    def test_promotion_failure_is_non_fatal(self):
        for mode in ("http401", "tool_error", "bad_structure", "business_code", "garbage_json", "http429:99"):
            with SimMcpServer() as sim:
                sim.fail("query-promotions", mode)
                code, out = self.plan(sim, greq())
                data = json.loads(out)
                self.assertEqual(code, 0, mode)
                self.assertTrue(data["ranking"])
                self.assertTrue(any("促销规则查询失败" in n for n in data["group"]["notes"]), mode)
    def test_wrong_be_code_rejected(self):
        """团餐 be_code 不在 beType=6 的可配送门店内（用外送 beCode）→ 报错，零菜单。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, greq(ful=group_ful(be_code="BE30001")))
            self.assertEqual(code, 1)
            self.assertNotIn("query-meals", {c["tool"] for c in sim.calls})
    def test_deterministic(self):
        outs = []
        for _ in range(2):
            with SimMcpServer() as sim:
                outs.append(stable(json.loads(self.plan(sim, greq())[1])))
        self.assertEqual(outs[0], outs[1])
    def test_reasons_and_text_report(self):
        with SimMcpServer() as sim:
            code, out = self.plan(sim, greq())
            data = json.loads(out)
            reasons = " ".join(data["ranking"][0]["reasons"])
            for needle in ("团餐人均预算", "向下取整", "余 10 分", "官方搭配建议", "助餐服务：模拟标准送达",
                           "促销提示", "以官方核价"):
                self.assertIn(needle, reasons)
            self.assertIn("group_fit", data["ranking"][0])
            path = write_json(self.dir, "t.json", greq())
            code, text = run_cli(sim, "live-plan", "--request", path)
            self.assertEqual(code, 0)
            for needle in ("## 团餐", "人均预算 ¥100.02（向下取整），余 10 分", "搭配建议", "助餐服务：模拟标准送达",
                           "以官方核价为准"):
                self.assertIn(needle, text)
    def test_privacy(self):
        with SimMcpServer() as sim:
            path = write_json(self.dir, "t.json", greq())
            _, out = self.plan(sim, greq())
            _, text = run_cli(sim, "live-plan", "--request", path)
            _, fail = self.plan(sim, greq(ful=group_ful(gm_service_code="NOPE")))
            for blob in (out, text, fail):
                for sentinel in SENTINELS:
                    self.assertNotIn(sentinel, blob)
    def test_call_budget_20_and_50(self):
        for n, budget in ((20, 200050), (50, 500000)):
            with SimMcpServer() as sim:
                planner = MultiLivePlanner(sim_client(sim))
                result = planner.plan_v2(greq(n=n, budget=budget))
                self.assertTrue(result["ranking"], n)
                self.assertLessEqual(len(sim.calls), 150, n)
                self.assertLessEqual(planner.client.calls, config.RUN_CALL_CAP, n)
    def test_group_info_in_private(self):
        with SimMcpServer() as sim:
            planner = MultiLivePlanner(sim_client(sim))
            _, private = planner._plan_v2_core(greq())
            self.assertEqual(private["price_extra"], {"gmServiceCode": GM})
            self.assertEqual(private["group"]["selected"]["gm_service_code"], GM)
    def test_small_group_uses_legacy_beam_path(self):
        """≤6 人走逐人 beam：同样带 group_fit、助餐服务与核价参数。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, greq(n=3, budget=60000))
            data = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertIn("group_fit", data["ranking"][0])
            self.assertEqual(data["group"]["budget"]["per_person_cents"], 20000)
            self.assertTrue(all(c["args"]["gmServiceCode"] == GM
                                for c in sim.calls if c["tool"] == "calculate-price"))
    def test_non_group_unchanged(self):
        """非团餐不调用团餐工具，结果无 group 段。"""
        with SimMcpServer() as sim:
            code, out = self.plan(sim, request())
            self.assertEqual(code, 0)
            self.assertNotIn("group", json.loads(out))
            self.assertFalse(sim.group_calls())
class GroupMealUnitTests(unittest.TestCase):
    """纯函数：搭配档、促销提示、理由、开关。"""
    def test_band_boundaries(self):
        self.assertEqual(group_meal.band_for(1999)["label"], "20元以下")
        self.assertEqual(group_meal.band_for(2000)["label"], "20-30元")
        self.assertEqual(group_meal.band_for(2999)["label"], "20-30元")
        self.assertEqual(group_meal.band_for(3000)["label"], "30-40元")
        self.assertEqual(group_meal.band_for(4000)["label"], "40-50元")
        self.assertEqual(group_meal.band_for(5000)["label"], "50元以上")
        self.assertEqual(group_meal.band_for(10002)["label"], "50元以上")
    def test_template_fit(self):
        band = group_meal.band_for(3500)
        full = {"items": [{"role": "main", "name": "a"}, {"role": "side", "name": "b"},
                          {"role": "drink", "name": "c"}]}
        part = {"items": [{"role": "main", "name": "a"}, {"role": "drink", "name": "c"}]}
        self.assertEqual(group_meal.template_fit(full, band), 0)
        self.assertEqual(group_meal.template_fit(part, band), -1)
        top = group_meal.band_for(6000)
        dup = {"items": [{"role": "main", "name": "a"}, {"role": "side", "name": "b"},
                         {"role": "side", "name": "b"}, {"role": "drink", "name": "c"}]}
        self.assertEqual(group_meal.template_fit(dup, top), -1)
    def test_group_fit_leads_quote_selection(self):
        """group_fit 前置于菜单提示价：契合度高的候选先送核价；无该键时行为不变。"""
        from meal_clear.ranking import select_for_quote
        def cand(sig, hint, fit=None):
            c = {"signature": sig, "hint_cents": hint, "preference_matches": 0,
                 "new_card_count": 0, "coupon_count": 0, "ids": (sig,)}
            if fit is not None:
                c["group_fit"] = fit
            return c
        plain = [cand("a", 100), cand("b", 200)]
        self.assertEqual(select_for_quote(plain, 1, "price")[0]["signature"], "a")
        fitted = [cand("a", 100, -2), cand("b", 200, 0)]
        self.assertEqual(select_for_quote(fitted, 1, "price")[0]["signature"], "b")
    def test_band_switch_off(self):
        req = validate_request_v2(greq())
        cands = [{"offers": []}]
        with mock.patch.object(config, "GROUP_BAND_ENABLED", False):
            self.assertNotIn("group_fit", group_meal.annotate_group_fit(cands, req)[0])
        self.assertIn("group_fit", group_meal.annotate_group_fit(cands, req)[0])
        pickup = validate_request_v2(request())
        self.assertNotIn("group_fit", group_meal.annotate_group_fit([{"offers": []}], pickup)[0])
    def test_no_public_amount_function(self):
        """模块不导出返回金额的函数；promo_hints 只返回字符串。"""
        names = [n for n in dir(group_meal) if not n.startswith("_") and callable(getattr(group_meal, n))]
        for name in names:
            self.assertFalse(any(w in name for w in ("cents", "amount", "price", "total")) and name != "split_budget", name)
        rules = group_meal.sanitize_promotions({"data": group_meal_rules()})
        hints = group_meal.promo_hints(rules, 50000, GM)
        self.assertTrue(hints and all(isinstance(h, str) for h in hints))
        self.assertTrue(any("官方原始 discount=30" in h for h in hints))
        self.assertTrue(any("适用于其他助餐服务" in h for h in hints))
        self.assertTrue(any("已达到该门槛" in h for h in hints))
        self.assertTrue(any("未达到该门槛" in h for h in hints))
    def test_sanitize_rejects_bad_structure(self):
        from meal_clear.core import InputError
        with self.assertRaises(InputError):
            group_meal.sanitize_assistance({"data": {"mealAssistanceItems": "x"}})
        with self.assertRaises(InputError):
            group_meal.sanitize_promotions({"data": {}})
        self.assertEqual(group_meal.sanitize_assistance(
            {"data": {"mealAssistanceItems": [{"gmServiceCode": ""}, 5, {"gmServiceCode": "A"},
                                              {"gmServiceCode": "A"}]}}
        )[0]["gm_service_code"], "A")
    def test_promo_cap(self):
        rows = [{"promotionId": str(i)} for i in range(50)]
        self.assertEqual(len(group_meal.sanitize_promotions({"data": rows})), config.GROUP_PROMO_RULE_CAP)
def group_meal_rules():
    """sim 默认促销规则的副本（避免依赖实例）。"""
    import sim_mcp
    import copy
    return copy.deepcopy(sim_mcp.GROUP_PROMOTIONS)
if __name__ == "__main__":
    unittest.main()
