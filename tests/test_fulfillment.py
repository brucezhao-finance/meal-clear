"""取餐方式映射与 v2 请求校验（离线、合成数据）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from meal_clear.core import InputError, validate_request  # noqa: E402
from meal_clear.fulfillment import context_key, validate_fulfillment, wire_context  # noqa: E402
from meal_clear.request_v2 import (  # noqa: E402
    amend_request_v2,
    fingerprint_v2,
    validate_request_v2,
)


def req(people: int = 1, **extra):
    """构造 v2 请求。"""
    base = {
        "schema_version": 2,
        "context": {"store_code": "10001", "meal_time": "now"},
        "budget_cents": 12000,
        "people": [{"id": chr(65 + i), "hard": {}, "prefer": {}} for i in range(people)],
    }
    base.update(extra)
    return base


class FulfillmentTests(unittest.TestCase):
    """T-B1~B8 与 T-A5/A6 的校验层。"""

    def test_B1_pickup_no_becode(self) -> None:
        """到店自取：beType1/orderType1，不传 beCode。"""
        self.assertEqual(
            wire_context(validate_request_v2(req())),
            {"storeCode": "10001", "orderType": 1, "beType": 1},
        )
        with self.assertRaises(InputError):
            validate_fulfillment({"type": "pickup", "be_code": "X"})

    def test_B2_drive_thru(self) -> None:
        """得来速：beType5/orderType1 并带 beCode。"""
        r = validate_request_v2(req(fulfillment={"type": "drive_thru", "be_code": "BE20001"}))
        self.assertEqual(
            wire_context(r),
            {"storeCode": "10001", "orderType": 1, "beType": 5, "beCode": "BE20001"},
        )

    def test_B3_delivery(self) -> None:
        """外送：beType2/orderType2 并带 beCode。"""
        r = validate_request_v2(
            req(fulfillment={"type": "delivery", "be_code": "BE30001", "address_index": 1})
        )
        self.assertEqual(
            wire_context(r),
            {"storeCode": "10001", "orderType": 2, "beType": 2, "beCode": "BE30001"},
        )
        self.assertEqual(context_key(r)["fulfillment"], "delivery")

    def test_B5_drive_thru_missing_becode(self) -> None:
        """得来速缺 be_code 报错。"""
        for bad in ({"type": "drive_thru"}, {"type": "drive_thru", "be_code": " "}):
            with self.assertRaises(InputError):
                validate_fulfillment(bad)

    def test_B6_delivery_missing_fields(self) -> None:
        """外送缺 address_index 或 be_code 报错。"""
        for bad in (
            {"type": "delivery", "be_code": "B"},
            {"type": "delivery", "address_index": 1},
            {"type": "delivery", "be_code": "B", "address_index": 0},
            {"type": "delivery", "be_code": "B", "address_index": True},
        ):
            with self.assertRaises(InputError):
                validate_fulfillment(bad)

    def test_B7_group_not_implemented(self) -> None:
        """团餐明确未实现。"""
        with self.assertRaises(InputError) as caught:
            validate_fulfillment({"type": "group", "be_code": "B", "address_index": 1})
        self.assertIn("尚未实现", str(caught.exception))
        self.assertIn("beType=6", str(caught.exception))

    def test_B8_default_is_pickup(self) -> None:
        """省略 fulfillment 等同 pickup。"""
        self.assertEqual(validate_request_v2(req())["fulfillment"], {"type": "pickup"})
        with self.assertRaises(InputError):
            validate_fulfillment({"type": "teleport"})
        with self.assertRaises(InputError):
            validate_fulfillment({"type": "pickup", "extra": 1})

    def test_A5_people_range(self) -> None:
        """v2 支持 1..6；0/7 报错，v1 仍为 1或2位。"""
        for n in (1, 3, 6):
            self.assertEqual(len(validate_request_v2(req(n))["people"]), n)
        for n in (0, 7):
            with self.assertRaises(InputError) as caught:
                validate_request_v2(req(n))
            self.assertIn("1至6位", str(caught.exception))
        v1 = {
            "context": {"store_code": "1", "channel": "pickup", "meal_time": "now"},
            "budget_cents": 100,
            "people": [{"id": c} for c in "ABC"],
        }
        with self.assertRaises(InputError) as caught:
            validate_request(v1)
        self.assertIn("1或2位", str(caught.exception))
        v1["context"]["channel"] = "delivery"
        v1["people"] = [{"id": "A"}]
        with self.assertRaises(InputError):
            validate_request(v1)

    def test_A6_duplicate_id(self) -> None:
        """重复 id 报错。"""
        r = req(2)
        r["people"][1]["id"] = "A"
        with self.assertRaises(InputError) as caught:
            validate_request_v2(r)
        self.assertIn("id 重复", str(caught.exception))

    def test_defaults_limits_and_errors(self) -> None:
        """默认 ranking/limits、越界与未知字段、channel 报错。"""
        r = validate_request_v2(req())
        self.assertEqual(r["ranking"], {"top_n": 3, "priority": "price"})
        self.assertEqual(r["limits"]["max_quotes"], 12)
        validate_request_v2(req(limits={"max_quotes": 30}))
        bad = [
            req(limits={"max_quotes": 31}),
            req(limits={"unknown": 1}),
            req(ranking={"top_n": 11}),
            req(ranking={"priority": "x"}),
            req(channel="pickup"),
            req(bogus=1),
            {**req(), "schema_version": 1},
        ]
        for item in bad:
            with self.assertRaises(InputError):
                validate_request_v2(item)

    def test_amend_and_fingerprint(self) -> None:
        """amend 保持校验；fingerprint 随修改变化。"""
        base = req(2)
        new = amend_request_v2(
            base,
            {"budget_cents": 5000, "ranking": {"priority": "preference"},
             "people": {"A": {"hard": {"non_spicy": True}}}},
        )
        self.assertEqual(new["ranking"]["priority"], "preference")
        self.assertEqual(new["people"][0]["hard"], {"non_spicy": True})
        back = amend_request_v2(new, {"people": {"A": {"hard": {"non_spicy": None}}}})
        self.assertEqual(back["people"][0]["hard"], {})
        self.assertNotEqual(fingerprint_v2(base), fingerprint_v2(new))
        with self.assertRaises(InputError):
            amend_request_v2(base, {"people": {"Z": {"hard": {}}}})


if __name__ == "__main__":
    unittest.main()
