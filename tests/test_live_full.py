"""Full adapter contracts: variants, defaults, schemas, coupons, pricing and limits."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.core import InputError
from meal_clear.live import (
    LivePlanner,
    check_pricing,
    classify_role,
    default_modification,
    detail_offers,
    facts_for,
    timestamp,
)
from meal_clear.mcp import McpError
from full_fixtures import SyntheticClient, detail, request


class LiveFullTests(unittest.TestCase):
    """Validate wire-format choices and service error behavior with fictional data."""

    def test_L01_role_and_facts_cover_all_food_and_drink_states(self) -> None:
        """Classification must never turn missing sugar/spice/meat evidence into safety."""
        self.assertEqual(classify_role("未知", "选择饮品"), "drink")
        self.assertEqual(classify_role("牛肉堡"), "main")
        self.assertEqual(classify_role("咖啡"), "drink")
        self.assertEqual(classify_role("薯条"), "side")
        cases = [
            (
                [
                    {"role": "main", "name": "非辣牛肉堡"},
                    {"role": "drink", "name": "无糖可乐"},
                ],
                {"main_meat": "beef", "non_spicy": True, "sugar_free_drink": True},
            ),
            (
                [
                    {"role": "main", "name": "辣鸡肉堡"},
                    {"role": "drink", "name": "含糖可乐"},
                ],
                {"main_meat": "chicken", "non_spicy": False, "sugar_free_drink": False},
            ),
            (
                [
                    {"role": "main", "name": "原味鸡肉堡"},
                    {"role": "side", "name": "辣薯条"},
                    {"role": "drink", "name": "低糖饮料"},
                ],
                {"main_meat": "chicken", "non_spicy": False},
            ),
            ([{"role": "main", "name": "牛鸡混合堡"}], {}),
            ([], {}),
        ]
        for items, expected in cases:
            with self.subTest(items=items):
                self.assertEqual(
                    {k: v["value"] for k, v in facts_for(items, "模拟来源").items()},
                    expected,
                )
        self.assertIn("+00:00", timestamp())

    def test_L02_modification_preserves_only_complete_declared_defaults(self) -> None:
        """Required modification defaults retain server codes, not invented notes."""
        valid = {
            "modification": {
                "items": [
                    {
                        "minValues": 1,
                        "values": [
                            {
                                "code": "ICE",
                                "selectedKey": "REGULAR",
                                "selectedQuantity": 1,
                            },
                            {
                                "code": "NONE",
                                "selectedKey": "NONE",
                                "selectedQuantity": 0,
                            },
                        ],
                    }
                ]
            }
        }
        self.assertEqual(
            default_modification(valid),
            {"values": [{"code": "ICE", "key": "REGULAR", "quantity": 1}]},
        )
        self.assertIsNone(default_modification({}))
        for changes in [
            {"minValues": 2},
            {"values": [{"code": "ICE", "selectedQuantity": 1}]},
        ]:
            bad = copy.deepcopy(valid)
            bad["modification"]["items"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InputError):
                default_modification(bad)

    def test_L03_complete_default_and_sugar_free_wire_variants(self) -> None:
        """The two variants use exact explicit option codes and intact modifications."""
        data = detail()
        data["rounds"][0]["choices"][0]["modification"] = {
            "items": [
                {
                    "minValues": 1,
                    "values": [
                        {
                            "code": "SAUCE",
                            "selectedKey": "DEFAULT",
                            "selectedQuantity": 1,
                        }
                    ],
                }
            ]
        }
        original = copy.deepcopy(data)
        offers = detail_offers("P", data, "模拟")
        self.assertEqual(len(offers), 2)
        self.assertEqual(
            offers[1]["wire_items"][0]["roundList"][1]["comboItemList"][0]["code"],
            "ZERO",
        )
        self.assertEqual(
            offers[0]["wire_items"][0]["roundList"][0]["comboItemList"][0][
                "modification"
            ]["values"][0]["code"],
            "SAUCE",
        )
        self.assertFalse(offers[0]["facts"]["sugar_free_drink"]["value"])
        self.assertTrue(offers[1]["facts"]["sugar_free_drink"]["value"])
        self.assertEqual(data, original)

    def test_L04_reject_incomplete_unsupported_and_ambiguous_meals(self) -> None:
        """Incomplete rounds, multi-select defaults, zero mains and bad codes are skipped."""
        variants = [
            None,
            {"rounds": {"bad": True}},
            {},
            {"rounds": []},
            {"rounds": [None]},
            {"rounds": None},
        ]
        for mutation in [
            "two_required",
            "max_zero",
            "no_default",
            "two_default",
            "no_code",
            "no_drink",
            "no_main",
            "optional_round",
            "bad_round_id",
        ]:
            data = detail()
            if mutation == "two_required":
                data["rounds"][0]["quantity"] = 2
            elif mutation == "max_zero":
                data["rounds"][0]["maxQuantity"] = 0
            elif mutation == "no_default":
                data["rounds"][0]["choices"][0]["isDefault"] = 0
            elif mutation == "two_default":
                data["rounds"][0]["choices"].append(
                    {"code": "OTHER", "name": "鸡肉堡", "isDefault": 1}
                )
            elif mutation == "no_code":
                data["rounds"][0]["choices"][0]["code"] = ""
            elif mutation == "no_drink":
                data["rounds"] = data["rounds"][:1]
            elif mutation == "no_main":
                data["rounds"] = data["rounds"][1:]
            elif mutation == "optional_round":
                data["rounds"][0]["minQuantity"] = 0
                data["rounds"][0]["quantity"] = 0
            else:
                data["rounds"][0].pop("id")
            variants.append(data)
        for data in variants:
            with self.subTest(detail=data):
                self.assertEqual(detail_offers("P", data, "模拟"), [])

    def test_L05_identical_variants_and_coupon_parameters_are_bounded(self) -> None:
        """Repeated default variants deduplicate; valid coupon codes stay only in wire input."""
        data = detail()
        data["rounds"][1]["choices"] = data["rounds"][1]["choices"][:1]
        self.assertEqual(len(detail_offers("P", data, "模拟")), 1)
        self.assertEqual(
            detail_offers("P", detail(), "模拟", {"couponId": "SYNTHETIC"}), []
        )
        offers = detail_offers(
            "P",
            detail(),
            "模拟",
            {"couponId": "SYNTHETIC", "couponCode": "SYNTHETIC-COUPON"},
        )
        self.assertTrue(all(o["id"].endswith("-coupon") for o in offers))
        self.assertEqual(offers[0]["wire_items"][0]["couponCode"], "SYNTHETIC-COUPON")
        self.assertNotEqual(offers[0]["coupon_key"], "SYNTHETIC")

    def test_L06_pricing_requires_complete_matching_product_identity(self) -> None:
        """Total, nonnegative integer units, fee sums, quantities and SKUs must agree."""
        items = [
            {"productCode": "P", "quantity": 1},
            {"productCode": "P", "quantity": 1},
        ]
        base = {
            "price": 2000,
            "productPrice": 2000,
            "deliveryPrice": 0,
            "packingPrice": 0,
            "tablewarePrice": 0,
            "productList": [{"productCode": "P", "quantity": 2}],
        }
        self.assertEqual(check_pricing({"data": base}, items), 2000)
        for data in [
            None,
            {},
            [],
            {**base, "price": True},
            {**base, "price": 2000.0},
            {**base, "productPrice": -1},
            {**base, "deliveryPrice": 1},
            {**base, "price": 2001},
            {**base, "productList": []},
            {**base, "productList": [{"productCode": "P", "quantity": 1}]},
            {**base, "productList": [{"productCode": "P", "quantity": 0}]},
            {**base, "productList": None},
            {**base, "productList": [None]},
        ]:
            with self.subTest(data=data), self.assertRaises(InputError):
                check_pricing({"data": data}, items)

    def test_L07_planner_schema_context_and_container_failures(self) -> None:
        """Unsupported time/store/schema and malformed service containers stop safely."""
        for changes in [{"meal_time": "tomorrow"}, {"store_code": "not-a-code"}]:
            req = request()
            req["context"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InputError):
                LivePlanner(SyntheticClient()).catalog(req)
        for mutation in [
            "no_tools",
            "schema_change",
            "menu_list",
            "menu_data_null",
            "coupons_dict",
        ]:
            client = SyntheticClient()
            if mutation == "no_tools":
                client.schemas = {"tools": []}
            elif mutation == "schema_change":
                client.schemas["tools"][0]["inputSchema"]["required"] = ["NEW_FIELD"]
            elif mutation == "menu_list":
                client.menu = {"data": {"meals": []}}
            elif mutation == "menu_data_null":
                client.menu = {"data": None}
            else:
                client.coupons = {"data": {}}
            with self.subTest(mutation=mutation), self.assertRaises(McpError):
                LivePlanner(client).catalog(request())

    def test_L08_catalog_filter_rank_and_finite_detail_limit(self) -> None:
        """Skip members/groups/sides; sort malformed hints safely; fetch at most six regulars."""
        client = SyntheticClient(12)
        client.menu["data"]["meals"].update(
            {
                "MEMBER": {"name": "模拟鸡肉堡套餐", "canWithOrder": True},
                "MULTI": {"name": "模拟双人堡套餐"},
                "SIDE": {"name": "模拟薯条套餐"},
                "BAD": None,
            }
        )
        hints = ["bad", "NaN", -1, None, "Infinity", {}, "1.50"]
        for index, value in enumerate(hints):
            client.menu["data"]["meals"][f"P{index}"]["currentPrice"] = value
        req = request()
        req["people"][0]["prefer"] = {"main_meat": "chicken"}
        result = LivePlanner(client).catalog(req)
        self.assertEqual(result["coverage"]["detail_queries"], 6)
        self.assertLessEqual(len(result["offers"]), 12)

    def test_L09_coupon_detail_failures_are_visible_and_bounded(self) -> None:
        """At most two coupon products; skipped failed details are counted."""
        client = SyntheticClient(2)
        client.coupons = {
            "data": [
                {
                    "couponId": "SYNTHETIC",
                    "couponCode": "SYNTHETIC-COUPON",
                    "products": [{"productCode": "C0"}, {"productCode": "IGNORED"}],
                },
                {"products": [{}]},
                {"products": [{"productCode": "IGNORED2"}]},
            ]
        }
        client.details["C0"] = detail()
        client.fail_details = {"P0"}
        result = LivePlanner(client).catalog(request())
        self.assertEqual(result["coverage"]["detail_queries"], 3)
        self.assertEqual(result["coverage"]["skipped_errors"], 1)
        self.assertTrue(any(o["id"].endswith("-coupon") for o in result["offers"]))

    def test_L10_real_planner_path_ready_no_price_overbudget_and_partial_failure(
        self,
    ) -> None:
        """Exercise the entire real planner against deterministic official-schema fixtures."""
        for mutation in [
            "ready",
            "all_fail",
            "partial_fail",
            "bad_price",
            "over_budget",
            "no_evidence",
            "no_offers",
        ]:
            client = SyntheticClient()
            req = request()
            expected = "ready"
            if mutation == "all_fail":
                client.fail_quotes = 99
                expected = "needs_price_verification"
            elif mutation == "partial_fail":
                client.fail_quotes = 1
            elif mutation == "bad_price":
                client.bad_price = True
                expected = "needs_price_verification"
            elif mutation == "over_budget":
                client = SyntheticClient(1)
                req["budget_cents"] = 1
                expected = "over_budget_in_checked_set"
            elif mutation == "no_evidence":
                req["people"][0]["hard"]["unsupported"] = True
                expected = "no_verified_plan"
            elif mutation == "no_offers":
                client = SyntheticClient(0)
                expected = "no_verified_plan"
            result = LivePlanner(client).plan(req)
            with self.subTest(mutation=mutation):
                self.assertEqual(result["status"], expected)
                self.assertLessEqual(result["quote_attempts"], 3)
                self.assertLessEqual(len(result["plans"]), 6)
                if mutation in ("all_fail", "bad_price"):
                    self.assertEqual(len(result["quote_errors"]), 3)
                if expected == "ready":
                    self.assertTrue(result["recommendations"][0]["quoted_at"])

    def test_L11_duplicate_baskets_do_not_spend_quote_budget_twice(self) -> None:
        """Person ordering duplicates share one request opportunity, capped at three."""
        client = SyntheticClient(1)
        out = LivePlanner(client).plan(request(people=2, hard=False))
        self.assertEqual(out["quote_attempts"], 3)
        self.assertEqual(out["verified_quotes"], 3)

    def test_L12_corrupt_detail_options_and_modifications_fail_closed(self) -> None:
        """Broken upstream default/choice/modification containers never produce a fabricated meal."""
        mutations = [("choices", [None]), ("choices", None), ("name", None)]
        for target, value in mutations:
            data = detail()
            if target == "name":
                data["rounds"][1]["choices"][1]["name"] = value
            else:
                data["rounds"][1][target] = value
            with self.subTest(target=target, value=value):
                self.assertEqual(detail_offers("P", data, "模拟"), [])
        for modification in [
            [],
            {"items": [None]},
            {"items": None},
            {"items": [{"values": [None]}]},
        ]:
            data = detail()
            data["rounds"][0]["choices"][0]["modification"] = modification
            with self.subTest(modification=modification):
                self.assertEqual(detail_offers("P", data, "模拟"), [])

    def test_L13_corrupt_schemas_and_coupons_fail_with_safe_protocol_errors(
        self,
    ) -> None:
        """Malformed remote schemas/coupon lists stop before using guessed wire parameters."""
        for mutation in (
            "schema_null",
            "schema_required_dict",
            "coupon_null",
            "coupon_products_null",
        ):
            fake = SyntheticClient()
            if mutation == "schema_null":
                fake.schemas["tools"][0]["inputSchema"] = None
            elif mutation == "schema_required_dict":
                fake.schemas["tools"][0]["inputSchema"]["required"] = {"extra": True}
            elif mutation == "coupon_null":
                fake.coupons = {"data": [None]}
            else:
                fake.coupons = {"data": [{"products": None}]}
            with self.subTest(mutation=mutation), self.assertRaises(McpError):
                LivePlanner(fake).catalog(request())

    def test_L14_unpriced_candidates_keep_pending_status_despite_overbudget_quotes(
        self,
    ) -> None:
        """Three over-budget quotes do not resolve remaining unquoted candidates."""
        req = request()
        req["budget_cents"] = 1
        result = LivePlanner(SyntheticClient(5)).plan(req)
        self.assertEqual(result["status"], "needs_price_verification")
        self.assertEqual(result["verified_quotes"], 3)
        self.assertFalse(result.get("recommendations"))

    def test_L15_inapplicable_coupon_cannot_become_verified_recommendation(
        self,
    ) -> None:
        """A coupon offered by lookup is still unusable if official pricing rejects it."""
        fake = SyntheticClient(0)
        fake.coupons = {
            "data": [
                {
                    "couponId": "SYNTHETIC-EXPIRED",
                    "couponCode": "SYNTHETIC-COUPON",
                    "products": [{"productCode": "C0"}],
                }
            ]
        }
        fake.details["C0"] = detail()
        fake.fail_quotes = 99
        result = LivePlanner(fake).plan(request())
        self.assertEqual(result["coverage"]["coupon_count"], 1)
        self.assertGreater(result["quote_attempts"], 0)
        self.assertEqual(result["verified_quotes"], 0)
        self.assertFalse(result.get("recommendations"))
        self.assertEqual(result["status"], "needs_price_verification")
