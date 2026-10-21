"""隐私脱敏单元测试（合成哨兵数据）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from meal_clear.core import InputError  # noqa: E402
from meal_clear.privacy import (  # noqa: E402
    assert_public,
    mask_addresses,
    sanitize_claimable,
    sanitize_held,
    sanitize_store_coupons,
)
from sim_mcp import ADDRESSES, ADDR_SENTINELS, CLAIMABLE, COUPON_SENTINELS, COUPONS, MY_COUPONS  # noqa: E402


class PrivacyTests(unittest.TestCase):
    """地址与券字段边界。"""

    def test_mask_addresses(self) -> None:
        """只保留序号/标签，repr 与 public 不含哨兵。"""
        masked = mask_addresses({"code": 0, "data": [dict(a) for a in ADDRESSES]})
        self.assertEqual([m.label for m in masked], ["地址1", "地址2"])
        self.assertEqual(masked[0].address_id, "ADDRID-1")
        text = repr(masked) + str([m.public() for m in masked])
        for sentinel in ADDR_SENTINELS:
            self.assertNotIn(sentinel, text)
        self.assertNotIn("ADDRID-1", text)
        self.assertEqual(len(masked[0].ref), 8)
        self.assertEqual(mask_addresses({"data": []}), [])
        self.assertEqual(mask_addresses("garbage"), [])

    def test_sanitize_coupons(self) -> None:
        """三类券白名单：不含 code/id/图片。"""
        out = (
            sanitize_claimable({"data": [dict(c) for c in CLAIMABLE]}),
            sanitize_held({"data": {"list": [dict(c) for c in MY_COUPONS]}}),
            sanitize_store_coupons({"data": [dict(c) for c in COUPONS]}),
        )
        text = str(out)
        for sentinel in COUPON_SENTINELS + ("BASE64-SENTINEL", "COUPONID", "MYID"):
            self.assertNotIn(sentinel, text)
        self.assertEqual(out[0][0]["couponName"], "模拟可领券1")
        self.assertEqual(out[1][0]["title"], "模拟已领券1")
        self.assertEqual(out[2][0]["product_names"], ["模拟不辣鸡肉堡套餐"])
        assert_public(out)

    def test_assert_public(self) -> None:
        """递归发现敏感键即抛错。"""
        for bad in ({"a": {"couponCode": "x"}}, [{"Phone": "1"}], {"x": [{"code": 1}]}):
            with self.assertRaises(InputError):
                assert_public(bad)
        assert_public({"rank": 1, "items": [{"name": "x"}]})

    def test_assert_public_order_keys(self) -> None:
        """订单相关敏感键（D22）被拦截；allow 只放行指定键。"""
        for key in ("orderId", "payH5Url", "payId", "pickupCode", "lockerCode", "lockerQrCode",
                    "mobilePhone", "riderMobilePhone", "addressDetail", "deliveryAddress",
                    "customerNickname"):
            with self.subTest(key=key):
                with self.assertRaises(InputError):
                    assert_public({"x": [{key: "v"}]})
        assert_public({"created_order": {"orderId": "v", "payH5Url": "u"}}, allow=("orderid", "payh5url"))
        assert_public({"lines": [{"code": "MAIN"}]}, allow=("code",))
        with self.assertRaises(InputError):
            assert_public({"orderId": "v", "couponCode": "c"}, allow=("orderid",))


if __name__ == "__main__":
    unittest.main()
