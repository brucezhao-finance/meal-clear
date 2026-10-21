"""订单查询（T-D7）：状态映射、敏感信息丢弃、订单号打码、账本读取、未知订单提示、只读不改账本。"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from order_support import OrderEnv, delivery_request, sim_support  # noqa: E402
from meal_clear import cli, orders  # noqa: E402
from meal_clear.mcp import McpError  # noqa: E402
from sim_mcp import ADDR_SENTINELS, ORDER_SENTINELS  # noqa: E402

LEAKS = ORDER_SENTINELS + ADDR_SENTINELS + ("sim.invalid", "SIM-PAY-", "payH5Url", "deliveryInfo")


def created(env, **req):
    """走完摘要/领券/下单，返回 (digest, orderId)。"""
    digest = env.ready(**req)
    code, out = env.place(digest, execute=True)
    assert code == 0, out
    return digest, env.sim.orders[-1]["orderId"]


def query_count(env):
    return [c["tool"] for c in env.sim.calls].count("query-order")


class OrderStatusTests(unittest.TestCase):

    def test_by_digest_reads_ledger_then_queries_once(self):
        with OrderEnv() as env:
            digest, order_id = created(env)
            before_queries = query_count(env)
            ledger_before = env.all_text()
            code, out = env.status("--digest", digest[:12])
            data = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertEqual(query_count(env) - before_queries, 1)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["order_id_masked"], "****" + order_id[-4:])
            self.assertEqual(data["order"]["status_code"], "1")
            self.assertEqual(data["order"]["status_label"], "待支付")
            self.assertEqual(data["order"]["amount_cents"], 2000)
            self.assertEqual(data["order"]["items"][0]["name"], "模拟不辣鸡肉堡套餐")
            self.assertEqual(data["order"]["store_name"], "模拟一店")
            self.assertEqual(data["order"]["created_at"], "2099-01-01 12:00:00")
            self.assertEqual(data["order"]["expire_pay_time"], "2099-01-01 12:10:00")
            self.assertNotIn(order_id, out)
            for word in LEAKS:
                self.assertNotIn(word, out)
            self.assertEqual(env.all_text(), ledger_before)  # 只读，不改账本
            self.assertEqual(env.sim.write_attempts.count("create-order"), 1)

    def test_by_order_id_text_output(self):
        with OrderEnv() as env:
            digest, order_id = created(env)
            code, out = env.run("order-status", "--order-id", order_id)
            self.assertEqual(code, 0, out)
            self.assertIn("待支付", out)
            self.assertIn("****" + order_id[-4:], out)
            self.assertIn("¥20.00", out)
            self.assertIn("本工具不会付款或取消订单", out)
            self.assertNotIn(order_id, out)
            for word in LEAKS:
                self.assertNotIn(word, out)

    def test_delivery_order_drops_address_and_phone(self):
        with OrderEnv() as env:
            digest, order_id = created(env, **delivery_request())
            code, out = env.status("--digest", digest[:12])
            self.assertEqual(code, 0, out)
            for word in LEAKS + ("ADDRID-1",):
                self.assertNotIn(word, out)
            code, out = env.run("order-status", "--order-id", order_id)
            for word in LEAKS:
                self.assertNotIn(word, out)

    def test_status_mapping(self):
        mapping = {"1": "待支付", "2": "配餐中-已支付", "4": "配送中", "6": "已完成",
                   "7": "已取消", "8": "已评价", "10": "配餐中-餐厅确认配餐中"}
        with OrderEnv() as env:
            digest, order_id = created(env)
            for code_text, label in mapping.items():
                env.sim.orders[0]["status"] = code_text
                code, out = env.status("--order-id", order_id)
                data = json.loads(out)["order"]
                self.assertEqual((data["status_code"], data["status_label"]), (code_text, label))
            env.sim.orders[0]["status"] = "99"
            data = json.loads(env.status("--order-id", order_id)[1])["order"]
            self.assertIsNone(data["status_code"])
            self.assertEqual(data["official_status_text"], "99")
            code, out = env.run("order-status", "--order-id", order_id)
            self.assertIn("官方返回状态：99", out)
            env.sim.orders[0]["status"] = ""
            code, out = env.run("order-status", "--order-id", order_id)
            self.assertIn("官方返回状态：未返回", out)

    def test_unknown_order_id_points_to_app(self):
        with OrderEnv() as env:
            created(env)
            code, out = env.run("order-status", "--order-id", "SIM-ORDER-9999")
            self.assertEqual(code, 1)
            self.assertIn("App 订单列表", out)
            self.assertIn("****9999", out)
            self.assertNotIn("SIM-ORDER-9999", out)
            self.assertEqual(query_count(env), 2)

    def test_bad_order_id_rejected_before_network(self):
        with OrderEnv() as env:
            created(env)
            before = len(env.sim.calls)
            for bad in ("bad id!", "../x", "a" * 65, ""):
                code, out = env.run("order-status", "--order-id", bad, "--json")
                self.assertEqual(code, 1, bad)
            self.assertEqual(len(env.sim.calls), before)

    def test_digest_without_order_id_is_local_and_offline(self):
        with OrderEnv() as env:
            digest = env.ready()
            calls = len(env.sim.calls)
            with patch.object(cli, "client_from_args", side_effect=AssertionError("不应联网")):
                code, out = env.status("--digest", digest[:12])
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(data["status"], "local_only")
            self.assertIn("尚未下单", data["message"])
            self.assertEqual(len(env.sim.calls), calls)

    def test_unknown_state_digest_says_check_app(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.fail("create-order", "disconnect")
            self.assertEqual(env.place(digest, execute=True)[0], 6)
            calls = len(env.sim.calls)
            code, out = env.run("order-status", "--digest", digest[:12])
            self.assertEqual(code, 0)
            self.assertIn("App 订单列表", out)
            self.assertIn("不要重试", out)
            self.assertEqual(len(env.sim.calls), calls)

    def test_requires_exactly_one_selector(self):
        with OrderEnv() as env:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    env.run("order-status")
                with self.assertRaises(SystemExit):
                    env.run("order-status", "--order-id", "X1", "--digest", "0" * 12)

    def test_unknown_digest_prefix(self):
        with OrderEnv() as env:
            created(env)
            code, out = env.run("order-status", "--digest", "0" * 12, "--json")
            self.assertEqual(code, 1)


class SanitizeTests(unittest.TestCase):
    """sanitize_order_detail / parse_created_order 的纯函数边界。"""

    def test_sanitize_drops_everything_not_whitelisted(self):
        payload = {"code": 0, "data": {
            "orderId": "SIM-ORDER-0007", "orderStatus": "6", "realTotalAmount": "12.30",
            "storeName": "某店", "createTime": "2099-01-01 10:00:00", "expirePayTime": "2099-01-01 10:10:00",
            "pickupCode": "PICKUP-SENTINEL", "lockerCode": "LOCKER-SENTINEL",
            "lockerQrCode": "http://x/LOCKER-SENTINEL", "payH5Url": "http://x/pay",
            "deliveryInfo": {"address": "ADDR-SENTINEL-1", "mobilePhone": "13800000000"},
            "orderProductList": [{"productName": "汉堡", "quantity": 2, "productImage": "http://img"},
                                 {"productName": "http://evil", "quantity": 1}, "junk"],
        }}
        out = orders.sanitize_order_detail(payload)
        text = json.dumps(out, ensure_ascii=False)
        for word in ("SIM-ORDER", "PICKUP", "LOCKER", "http", "ADDR-SENTINEL", "13800000000", "deliveryInfo"):
            self.assertNotIn(word, text)
        self.assertEqual(out["amount_cents"], 1230)
        self.assertEqual(out["items"], [{"name": "汉堡", "quantity": 2}])
        self.assertEqual(out["status_label"], "已完成")

    def test_sanitize_rejects_bad_structure(self):
        for bad in (None, {}, {"data": []}, {"data": "x"}):
            with self.assertRaises(McpError):
                orders.sanitize_order_detail(bad)

    def test_money_conversion_is_exact(self):
        self.assertEqual(orders.money_to_cents("20.00"), 2000)
        self.assertEqual(orders.money_to_cents("0.1"), 10)
        self.assertEqual(orders.money_to_cents(5), 500)
        for bad in ("20.005", "abc", "-1", None, True, "NaN", "Infinity"):
            self.assertIsNone(orders.money_to_cents(bad), bad)

    def test_parse_created_order_requires_order_id(self):
        good = {"data": {"orderId": "A1", "payH5Url": "http://p", "payId": 7,
                         "orderDetail": {"orderStatus": "1", "realTotalAmount": "20.00"}}}
        info = orders.parse_created_order(good, 2000)
        self.assertEqual((info["order_id"], info["status_code"], info["amount_check"]), ("A1", "1", "match"))
        self.assertEqual(info["pay_id"], "7")
        self.assertEqual(orders.parse_created_order(good, 1999)["amount_check"], "mismatch")
        no_amount = {"data": {"orderId": "A1"}}
        self.assertEqual(orders.parse_created_order(no_amount, 2000)["amount_check"], "missing")
        for bad in (None, {}, {"data": {}}, {"data": {"orderId": "  "}}, {"data": {"orderId": 5}},
                    {"data": {"orderId": "bad id"}}):
            with self.assertRaises(McpError):
                orders.parse_created_order(bad, 2000)

    def test_mask(self):
        self.assertEqual(orders.mask_order_id("SIM-ORDER-0001"), "****0001")
        self.assertEqual(orders.mask_order_id("ab"), "****")
        self.assertEqual(orders.mask_order_id(None), "****")


if __name__ == "__main__":
    unittest.main()
