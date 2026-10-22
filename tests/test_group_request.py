"""团餐请求校验：取餐方式映射、人均预算取整、预约时间、人数上限、幂等（离线、合成数据）。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear import config  # noqa: E402
from meal_clear.core import InputError  # noqa: E402
from meal_clear.fulfillment import (  # noqa: E402
    context_key, price_extra, validate_fulfillment, wire_context,
)
from meal_clear.group_meal import parse_reservation, split_budget  # noqa: E402
from meal_clear.request_v2 import (  # noqa: E402
    amend_request_v2, fingerprint_v2, validate_request_v2,
)
GROUP = {"type": "group", "be_code": "BE-1", "address_index": 1}
def greq(people=20, fulfillment=None, **extra):
    """构造团餐 v2 请求；默认总预算 200050 分。"""
    base = {
        "schema_version": 2,
        "context": {"store_code": "10001", "meal_time": "now"},
        "fulfillment": dict(fulfillment or GROUP),
        "people": [{"id": f"P{i}", "hard": {}, "prefer": {}} for i in range(people)],
    }
    if "per_person_budget_cents" not in extra and "budget_cents" not in extra:
        base["budget_cents"] = 200050
    base.update(extra)
    return base
class GroupFulfillmentTests(unittest.TestCase):
    """取餐方式映射与助餐服务、预约时间校验。"""
    def test_mapping(self):
        """group：beType=6、orderType=2、beCode 必传、需要地址。"""
        spec = config.FULFILLMENTS["group"]
        self.assertTrue(spec.implemented and spec.needs_gm_service and spec.supports_reservation)
        wire = wire_context(validate_request_v2(greq()))
        self.assertEqual(wire, {"storeCode": "10001", "orderType": 2, "beType": 6, "beCode": "BE-1"})
        self.assertFalse(config.FULFILLMENTS["delivery"].needs_gm_service)
    def test_missing_fields_chinese_error(self):
        """缺 be_code 或 address_index 报中文错误。"""
        for bad in ({"type": "group", "address_index": 1}, {"type": "group", "be_code": "B"},
                    {"type": "group", "be_code": "B", "address_index": 0}):
            with self.assertRaises(InputError) as caught:
                validate_fulfillment(bad)
            self.assertRegex(str(caught.exception), "[\u4e00-\u9fff]")
    def test_gm_service_code(self):
        """gm_service_code 可选非空串；仅 group 接受；price_extra 仅 group 且有值。"""
        ful = validate_fulfillment({**GROUP, "gm_service_code": "GM1"})
        self.assertEqual(ful["gm_service_code"], "GM1")
        self.assertEqual(price_extra(greq(fulfillment=ful)), {"gmServiceCode": "GM1"})
        self.assertEqual(price_extra(greq()), {})
        for bad in ("", "  ", 5):
            with self.assertRaises(InputError):
                validate_fulfillment({**GROUP, "gm_service_code": bad})
        with self.assertRaises(InputError):
            validate_fulfillment({"type": "delivery", "be_code": "B", "address_index": 1,
                                  "gm_service_code": "GM1"})
        self.assertNotIn("gmServiceCode", wire_context(validate_request_v2(greq(
            fulfillment={**GROUP, "gm_service_code": "GM1"}))))
    def test_reservation_format(self):
        """预约格式严格为 yyyy-MM-dd HH:mm，不接受秒。"""
        ful = validate_fulfillment({**GROUP, "reservation_date": "2026-11-02 09:30"})
        wire = wire_context(validate_request_v2(greq(fulfillment=ful)))
        self.assertEqual(wire["reservationDate"], "2026-11-02 09:30")
        for bad in ("2026-11-02 09:30:00", "2026-11-02T09:30", "2026-13-02 09:30",
                    "2026-11-02 25:30", "2026/11/02 09:30", "2026-11-2 9:30", "", 5):
            with self.assertRaises(InputError):
                parse_reservation(bad)
            with self.assertRaises(InputError):
                validate_fulfillment({**GROUP, "reservation_date": bad})
        with self.assertRaises(InputError):
            validate_fulfillment({"type": "delivery", "be_code": "B", "address_index": 1,
                                  "reservation_date": "2026-11-02 09:30"})
        self.assertNotIn("reservationDate", wire_context(validate_request_v2(greq())))
    def test_context_key_only_group_adds_reservation(self):
        """非 group 的离线菜单匹配键不变；group 追加 reservation_date。"""
        pickup = {"schema_version": 2, "context": {"store_code": "1", "meal_time": "now"},
                  "budget_cents": 100, "people": [{"id": "A", "hard": {}, "prefer": {}}]}
        self.assertEqual(set(context_key(validate_request_v2(pickup))),
                         {"store_code", "meal_time", "fulfillment", "be_code"})
        self.assertEqual(context_key(validate_request_v2(greq()))["reservation_date"], None)
class GroupBudgetTests(unittest.TestCase):
    """人均预算取整、余数、二选一与幂等。"""
    def test_total_budget_sample(self):
        """N=20, T=200050 → per=10002, 余 10。"""
        parts = split_budget(validate_request_v2(greq()))
        self.assertEqual(parts, {"people_count": 20, "total_cents": 200050,
                                 "per_person_cents": 10002, "remainder_cents": 10})
    def test_per_person_budget(self):
        """给人均时 T=p×N，余数 0。"""
        req = validate_request_v2(greq(per_person_budget_cents=10002))
        self.assertEqual(req["budget_cents"], 200040)
        self.assertEqual(split_budget(req)["remainder_cents"], 0)
        self.assertEqual(split_budget(req)["per_person_cents"], 10002)
    def test_exclusive_and_consistency(self):
        """总预算与人均并存仅当 T==p×N；缺一都缺则报错。"""
        validate_request_v2(greq(budget_cents=200040, per_person_budget_cents=10002))
        with self.assertRaises(InputError):
            validate_request_v2(greq(budget_cents=200050, per_person_budget_cents=10002))
        base = greq()
        base.pop("budget_cents")
        with self.assertRaises(InputError):
            validate_request_v2(base)
        for bad in (0, -1, True, 1.5, "100"):
            with self.assertRaises(InputError):
                validate_request_v2(greq(per_person_budget_cents=bad))
    def test_budget_below_one_cent_per_person(self):
        """总预算不足以每人 1 分报错。"""
        with self.assertRaises(InputError):
            validate_request_v2(greq(people=20, budget_cents=19))
        validate_request_v2(greq(people=20, budget_cents=20))
    def test_non_group_rejects_per_person(self):
        """非团餐带 per_person_budget_cents 拒绝。"""
        pickup = {"schema_version": 2, "context": {"store_code": "1", "meal_time": "now"},
                  "per_person_budget_cents": 100, "budget_cents": 100,
                  "people": [{"id": "A", "hard": {}, "prefer": {}}]}
        with self.assertRaises(InputError):
            validate_request_v2(pickup)
    def test_idempotent_validation(self):
        """重复校验结果不变，指纹不变。"""
        for extra in ({}, {"per_person_budget_cents": 10002}):
            once = validate_request_v2(greq(**extra))
            twice = validate_request_v2(once)
            self.assertEqual(once, twice)
            self.assertEqual(fingerprint_v2(once), fingerprint_v2(twice))
    def test_people_range(self):
        """人数 1–50 通过，51 拒绝，0 拒绝。"""
        for n in (1, 20, 50):
            validate_request_v2(greq(people=n, budget_cents=n * 1000))
        for n in (0, 51):
            with self.assertRaises(InputError):
                validate_request_v2(greq(people=n, budget_cents=51000))
    def test_amend(self):
        """修改一个预算字段就清掉另一个；两个同时改拒绝。"""
        base = validate_request_v2(greq(per_person_budget_cents=10002))
        out = amend_request_v2(base, {"budget_cents": 250000})
        self.assertNotIn("per_person_budget_cents", out)
        self.assertEqual(out["budget_cents"], 250000)
        out = amend_request_v2(validate_request_v2(greq()), {"per_person_budget_cents": 9000})
        self.assertEqual(out["budget_cents"], 180000)
        self.assertEqual(out["per_person_budget_cents"], 9000)
        with self.assertRaises(InputError):
            amend_request_v2(base, {"budget_cents": 1, "per_person_budget_cents": 1})
        out = amend_request_v2(base, {"fulfillment": {**GROUP, "gm_service_code": "GM1",
                                                       "reservation_date": "2026-11-02 09:30"}})
        self.assertEqual(out["fulfillment"]["gm_service_code"], "GM1")
if __name__ == "__main__":
    unittest.main()
