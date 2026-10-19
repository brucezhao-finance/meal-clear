"""行为验收：金额、逐人约束、报价上下文和有限只读边界。"""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from meal_clear.cli import save_json
from meal_clear.core import (
    InputError,
    amend_request,
    break_even,
    check_person,
    compare_results,
    compose,
    fingerprint,
    money_to_cents,
    validate_request,
)
from meal_clear.live import LivePlanner, check_pricing, detail_offers, facts_for
from meal_clear.mcp import McpClient, McpError, decode_response, unwrap_tool


class BehaviorTests(unittest.TestCase):
    """每项断言面向用户结果或交易/隐私不变量。"""

    def setUp(self):
        """使用标明模拟的完整请求和菜单。"""
        self.catalog = json.loads((ROOT / "examples/demo-catalog.json").read_text())
        self.request = json.loads((ROOT / "examples/request.json").read_text())

    def test_demo_does_not_call_unknown_safe(self):
        """低价但辣度未知的餐不会混入符合要求的方案。"""
        result = compose(self.request, self.catalog)
        self.assertEqual(result["recommendations"][0]["total_cents"], 3800)
        self.assertFalse(
            any(
                a["offer_id"] == "demo-unknown"
                for p in result["plans"]
                for a in p["assignments"]
            )
        )

    def test_malformed_catalog_containers_raise_input_error(self) -> None:
        """损坏套餐、餐品或事实容器应产生受控错误，不能抛AttributeError。"""
        variants = []
        catalog = copy.deepcopy(self.catalog)
        catalog["offers"] = [None]
        variants.append(catalog)
        for malformed_items in ([None], {"main": "not-a-list"}):
            catalog = copy.deepcopy(self.catalog)
            catalog["offers"][0]["items"] = malformed_items
            variants.append(catalog)
        for malformed_facts in ([], None):
            catalog = copy.deepcopy(self.catalog)
            catalog["offers"][0]["facts"] = malformed_facts
            variants.append(catalog)
        for catalog in variants:
            with self.subTest(catalog=catalog), self.assertRaises(InputError):
                compose(self.request, catalog)

    def test_breakfast_combo_reaches_detail_without_member_card(self) -> None:
        """普通早餐组合必须进入详情核验，附带会员条件的组合仍被排除。"""

        class BreakfastClient:
            """模拟当前菜单形状，不调用网络或提供真实价格。"""

            def __init__(self) -> None:
                """记录实际被查询的详情编码。"""
                self.details = []

            def list_tools(self) -> dict:
                """提供适配器所需只读工具。"""
                return {
                    "tools": [
                        {"name": name}
                        for name in (
                            "query-meals",
                            "query-meal-detail",
                            "query-store-coupons",
                            "calculate-price",
                        )
                    ]
                }

            def call(self, name: str, arguments: dict) -> dict:
                """返回结构化模拟菜单与主食、饮品轮次。"""
                if name == "query-meals":
                    return {
                        "data": {
                            "meals": {
                                "COMBO": {
                                    "name": "模拟鸡肉堡组合",
                                    "canWithOrder": False,
                                },
                                "MEMBER": {
                                    "name": "模拟鸡肉堡组合",
                                    "canWithOrder": True,
                                },
                            }
                        }
                    }
                if name == "query-store-coupons":
                    return {"data": []}
                self.details.append(arguments["code"])
                return {
                    "data": {
                        "name": "模拟鸡肉堡组合",
                        "rounds": [
                            {
                                "id": 1,
                                "name": "主食",
                                "minQuantity": 1,
                                "quantity": 1,
                                "choices": [
                                    {
                                        "code": "MAIN",
                                        "name": "模拟鸡肉堡",
                                        "isDefault": 1,
                                    }
                                ],
                            },
                            {
                                "id": 2,
                                "name": "选择早餐组合饮料",
                                "minQuantity": 1,
                                "quantity": 1,
                                "choices": [
                                    {
                                        "code": "DRINK",
                                        "name": "模拟咖啡",
                                        "isDefault": 1,
                                    },
                                    {
                                        "code": "ZERO",
                                        "name": "模拟无糖饮料",
                                        "isDefault": 0,
                                    },
                                ],
                            },
                        ],
                    }
                }

        client = BreakfastClient()
        request = {
            "context": {"store_code": "123", "channel": "pickup", "meal_time": "now"},
            "budget_cents": 10000,
            "people": [{"id": "A", "hard": {"sugar_free_drink": True}}],
        }
        catalog = LivePlanner(client).catalog(request)
        self.assertEqual(client.details, ["COMBO"])
        self.assertEqual(
            compose(request, catalog)["status"], "needs_price_verification"
        )

    def test_change_preserves_other_person_and_budget(self):
        """改B口味不会丢掉A的要求或修改预算。"""
        original = copy.deepcopy(self.request)
        new = amend_request(
            self.request, {"people": {"B": {"hard": {"main_meat": "beef"}}}}
        )
        before = compose(self.request, self.catalog)
        after = compose(new, self.catalog)
        self.assertEqual(new["people"][0], original["people"][0])
        self.assertEqual(new["budget_cents"], 5000)
        self.assertEqual(self.request, original)
        self.assertEqual(compare_results(before, after)["delta_cents"], 500)
        self.assertNotEqual(before["request_id"], after["request_id"])

    def test_new_card_not_disguised_as_cheap(self):
        """低餐费加高卡费不能挤掉无卡更便宜的同内容方案。"""
        result = compose(self.request, self.catalog)
        self.assertFalse(
            any(p["steps"]["new_card_count"] for p in result["recommendations"])
        )
        card_plans = [p for p in result["plans"] if p["new_card_cents"] > 0]
        self.assertTrue(all(p["total_cents"] >= 4400 for p in card_plans))

    def test_alternatives_have_real_tradeoff(self):
        """更贵但偏好更匹配时保留备选，不重复相同购物篮。"""
        self.request["people"][1]["prefer"] = {"main_meat": "beef"}
        choices = compose(self.request, self.catalog)["recommendations"]
        self.assertEqual([p["total_cents"] for p in choices], [3800, 4300])
        self.assertLess(
            choices[0]["preference_matches"], choices[1]["preference_matches"]
        )

    def test_budget_equality_is_allowed(self):
        """预算等于实际全单价属于可行。"""
        self.request["budget_cents"] = 3800
        self.assertTrue(compose(self.request, self.catalog)["recommendations"])

    def test_budget_conflict_is_not_silently_relaxed(self):
        """无解时保留要求，不为了生成推荐自动换掉约束。"""
        self.request["budget_cents"] = 100
        result = compose(self.request, self.catalog)
        self.assertEqual(result["status"], "over_budget_in_checked_set")
        self.assertNotIn("recommendations", result)
        self.assertEqual(result["request"]["budget_cents"], 100)

    def test_context_change_requires_new_menu(self):
        """换店后不使用旧店价格或优惠。"""
        changed = amend_request(self.request, {"context": {"store_code": "OTHER"}})
        self.assertEqual(compose(changed, self.catalog)["status"], "context_mismatch")

    def test_unknown_sugar_not_replaced_with_low_sugar(self):
        """低糖/原味不自动等于无糖/不辣。"""
        facts = facts_for(
            [
                {"role": "main", "name": "原味鸡堡"},
                {"role": "drink", "name": "低糖饮料"},
            ],
            "official",
        )
        self.assertNotIn("non_spicy", facts)
        self.assertNotIn("sugar_free_drink", facts)

    def test_explicit_nonspicy_not_misread_as_spicy(self):
        """非辣的“辣”字不能反向判为辣味。"""
        facts = facts_for(
            [
                {"role": "main", "name": "官方非辣鸡肉堡"},
                {"role": "drink", "name": "不含糖饮料"},
            ],
            "official",
        )
        self.assertIs(facts["non_spicy"]["value"], True)
        self.assertIs(facts["sugar_free_drink"]["value"], True)

    def test_mixed_meat_not_exclusively_beef(self):
        """牛鸡混合名称不被标成单一牛肉属性。"""
        facts = facts_for([{"role": "main", "name": "牛肉鸡肉双拼堡"}], "official")
        self.assertNotIn("main_meat", facts)

    def test_false_boolean_not_integer_zero(self):
        """避免Python将False与0视作相同而误判。"""
        checks = check_person(
            {"hard": {"non_spicy": False}},
            {"facts": {"non_spicy": {"value": 0, "source": "bad"}}},
        )
        self.assertEqual(checks[0]["status"], "violated")

    def test_missing_offline_fee_not_zero(self):
        """缺失本地费用时不声称算出完整实付。"""
        self.catalog.pop("order_fee_cents")
        result = compose(self.request, self.catalog)
        self.assertEqual(result["status"], "needs_price_verification")

    def test_live_never_uses_local_menu_price(self):
        """实时模式即使给了菜单价也必须等官方报价。"""
        self.catalog["mode"] = "live"
        result = compose(self.request, self.catalog)
        self.assertTrue(all(p["total_cents"] is None for p in result["plans"]))
        self.assertEqual(result["status"], "needs_price_verification")

    def test_wrong_context_quote_rejected(self):
        """其他门店的报价即使商品相同也不能采纳。"""
        self.catalog["mode"] = "live"
        result = compose(self.request, self.catalog)
        signature = result["plans"][0]["signature"]
        quotes = {
            signature: {
                "signature": signature,
                "context": {"store_code": "WRONG"},
                "verified": True,
                "total_cents": 1,
            }
        }
        after = compose(self.request, self.catalog, quotes)
        self.assertNotIn("recommendations", after)

    def test_money_units_and_precision(self):
        """金额单位显式处理，不口算/猜单位或舍入。"""
        self.assertEqual(money_to_cents("26.50", "CNY"), 2650)
        self.assertEqual(money_to_cents(2650, "cent"), 2650)
        for amount, unit in [
            (True, "cent"),
            ("NaN", "CNY"),
            (-1, "cent"),
            (1, "unknown"),
            ("1.001", "CNY"),
            (1.5, "cent"),
        ]:
            with self.subTest(amount=amount, unit=unit), self.assertRaises(InputError):
                money_to_cents(amount, unit)

    def test_break_even_boundary(self):
        """回本向上取整，零/负节省不生成有限次数。"""
        self.assertEqual(break_even(1400, 200), 7)
        self.assertEqual(break_even(1401, 200), 8)
        self.assertIsNone(break_even(1400, 0))
        self.assertIsNone(break_even(1400, -5))

    def test_coupon_reuse_does_not_pass(self):
        """首版单券边界阻止一张券重复给两个人用。"""
        self.catalog["offers"] = self.catalog["offers"][:1]
        self.catalog["offers"][0]["coupon_key"] = "same-coupon"
        result = compose(self.request, self.catalog)
        self.assertFalse(result["plans"])

    def test_unknown_people_and_fields_rejected(self):
        """修改不误创建陌生用餐者或忽略拼错字段。"""
        for patch in [
            {"people": {"C": {"hard": {}}}},
            {"budegt_cents": 1},
            {"people": {"A": {"preferences": {}}}},
        ]:
            with self.subTest(patch=patch), self.assertRaises(InputError):
                amend_request(self.request, patch)

    def test_invalid_request_modes_rejected(self):
        """超范围人群、模式和布尔预算不会悄悄运行。"""
        for change in [
            {"budget_cents": True},
            {"people": []},
            {
                "context": {
                    "store_code": "DEMO",
                    "channel": "delivery",
                    "meal_time": "now",
                }
            },
        ]:
            with self.subTest(change=change), self.assertRaises(InputError):
                validate_request({**self.request, **change})

    def test_quote_consistency_and_missing_total(self):
        """费用冲突、商品错配或总价缺失不会作为可确认方案。"""
        items = [{"productCode": "X", "quantity": 1}]
        base = {
            "price": 1000,
            "productPrice": 1000,
            "deliveryPrice": 0,
            "packingPrice": 0,
            "tablewarePrice": 0,
            "productList": [{"productCode": "X", "quantity": 1}],
        }
        self.assertEqual(check_pricing({"data": base}, items), 1000)
        for patch in [
            {"price": None},
            {"price": 999},
            {"productPrice": 1100},
            {"productList": [{"productCode": "Y", "quantity": 1}]},
        ]:
            with self.subTest(patch=patch), self.assertRaises(InputError):
                check_pricing({"data": {**base, **patch}}, items)

    def test_official_total_can_have_unknown_breakdown(self):
        """真实到店回包可缺费用拆分，采用官方总额但不伪填零。"""
        payload = {
            "data": {
                "price": 2650,
                "productPrice": 2650,
                "productList": [{"productCode": "X", "quantity": 1}],
            }
        }
        self.assertEqual(
            check_pricing(payload, [{"productCode": "X", "quantity": 1}]), 2650
        )
        self.assertNotIn("deliveryPrice", payload["data"])

    def test_same_request_fingerprint_stable(self):
        """字段顺序不改变身份，预算或用餐时间改变会更新身份。"""
        self.assertEqual(
            fingerprint(self.request), fingerprint(copy.deepcopy(self.request))
        )
        self.assertNotEqual(
            fingerprint(self.request),
            fingerprint({**self.request, "budget_cents": 5001}),
        )

    def test_private_state_permissions(self):
        """保存会话只给当前用户读写，不因默认umask开放。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_json(str(path), {"request": self.request})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ProtocolTests(unittest.TestCase):
    """协议错误与写操作拒绝在离线完成。"""

    def test_sse_selects_matching_response(self):
        """通知不被误读为工具成功。"""
        raw = b'event: message\ndata: {"jsonrpc":"2.0","method":"notice"}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n'
        self.assertTrue(decode_response(raw, 2)["result"]["ok"])

    def test_business_failure_sanitized(self):
        """业务失败不能因为HTTP成功被接受，消息内容不泄漏。"""
        with self.assertRaises(McpError) as caught:
            unwrap_tool(
                {
                    "content": [
                        {"type": "text", "text": '{"code":600012,"message":"secret"}'}
                    ]
                }
            )
        self.assertNotIn("secret", str(caught.exception))

    def test_readonly_client_rejects_mutations_without_network(self):
        """所有交易工具在任何网络调用前被拒绝。"""
        client = object.__new__(McpClient)
        for tool in (
            "create-order",
            "auto-bind-coupons",
            "cancel-order",
            "draw-lottery",
            "mall-create-order",
        ):
            with self.subTest(tool=tool), self.assertRaises(McpError):
                client.call(tool, {})

    def test_explicit_sugar_free_variant_uses_actual_code(self):
        """无糖替换使用返回的选项编码，不靠备注。"""
        detail = {
            "name": "模拟套餐",
            "rounds": [
                {
                    "id": 1,
                    "name": "主食",
                    "minQuantity": 1,
                    "quantity": 1,
                    "maxQuantity": 1,
                    "choices": [{"code": "MAIN", "name": "模拟鸡肉堡", "isDefault": 1}],
                },
                {
                    "id": 2,
                    "name": "饮料",
                    "minQuantity": 1,
                    "quantity": 1,
                    "maxQuantity": 1,
                    "choices": [
                        {"code": "SUGAR", "name": "普通饮料", "isDefault": 1},
                        {"code": "ZERO", "name": "无糖饮料", "isDefault": 0},
                    ],
                },
            ],
        }
        offers = detail_offers("PARENT", detail, "模拟官方返回")
        changed = next(o for o in offers if o["facts"].get("sugar_free_drink"))
        self.assertEqual(
            changed["wire_items"][0]["roundList"][1]["comboItemList"][0]["code"], "ZERO"
        )
        self.assertNotIn("non_spicy", changed["facts"])


if __name__ == "__main__":
    unittest.main()
