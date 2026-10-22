"""受控下单（T-D1~D6）：参数映射、成功路径、三态落账、幂等、写前账本与崩溃恢复、新鲜度校验。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from order_support import (  # noqa: E402
    REAL_CLIENT_FROM_ARGS, OrderEnv, delivery_request, drive_thru_request, sim_support,
)
from meal_clear import cli, gate, orders  # noqa: E402
from meal_clear.config import TOOL_CREATE_ORDER  # noqa: E402
from meal_clear.core import InputError  # noqa: E402
from meal_clear.ledger import Ledger  # noqa: E402
from meal_clear.mcp import McpClient  # noqa: E402
import sim_mcp  # noqa: E402
from sim_mcp import ADDR_SENTINELS, COUPON_SENTINELS, ORDER_SENTINELS  # noqa: E402

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "mcp-write-tools-schema.json").read_text(encoding="utf-8"))
ITEM_KEYS = {"productCode", "quantity", "roundList", "couponId", "couponCode", "modification"}
NOT_SENT = ("remark", "reservationDate", "withOrder", "gmServiceCode")
SECRETS = sim_support.SENTINELS + COUPON_SENTINELS + ADDR_SENTINELS + ORDER_SENTINELS


class Crash(BaseException):
    """模拟进程在发请求前崩溃（不被 except Exception 捕获）。"""


def short_timeout_client(env, timeout=0.15):
    """指向模拟服务、读超时很短的客户端（用于 timeout 故障）。"""
    client = sim_support.sim_client(env.sim)
    client.timeout = timeout
    return client


class ParamMappingTests(unittest.TestCase):
    """T-D1：三种取餐方式逐字段断言，并与 schema 夹具一致、无多余键。"""

    def _args_for(self, **req):
        with OrderEnv() as env:
            digest = env.ready(**req)
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 0, out)
            calls = env.create_calls()
            self.assertEqual(len(calls), 1)
            return calls[0]["args"]

    def _check_schema(self, args):
        spec = SCHEMA["create-order"]
        self.assertLessEqual(set(args), set(spec["properties"]))
        self.assertLessEqual(set(spec["required"]), set(args))
        for key in NOT_SENT:
            self.assertNotIn(key, args)
        self.assertIs(args["needTableware"], False)
        self.assertTrue(args["items"])
        for row in args["items"]:
            self.assertLessEqual(set(row), ITEM_KEYS)
            self.assertTrue(row["productCode"])
            self.assertGreaterEqual(row["quantity"], 1)

    def test_pickup(self):
        args = self._args_for()
        self._check_schema(args)
        self.assertEqual(
            {k: v for k, v in args.items() if k != "items"},
            {"storeCode": "10001", "orderType": 1, "beType": 1, "takeWayCode": "SIM-TW-1", "needTableware": False},
        )
        self.assertNotIn("beCode", args)
        self.assertNotIn("addressId", args)

    def test_drive_thru(self):
        args = self._args_for(**drive_thru_request())
        self._check_schema(args)
        self.assertEqual(
            {k: v for k, v in args.items() if k != "items"},
            {"storeCode": "20001", "orderType": 1, "beType": 5, "beCode": "BE20001",
             "takeWayCode": "SIM-TW-1", "needTableware": False},
        )
        self.assertNotIn("addressId", args)

    def test_delivery(self):
        args = self._args_for(**delivery_request())
        self._check_schema(args)
        self.assertEqual(
            {k: v for k, v in args.items() if k != "items"},
            {"storeCode": "30001", "orderType": 2, "beType": 2, "beCode": "BE30001",
             "addressId": "ADDRID-1", "needTableware": False},
        )
        self.assertNotIn("takeWayCode", args)

    def test_merged_lines_for_multiple_people(self):
        """3 人同款合并成 商品行 × 数量（数量总和 = 3）。"""
        people = [{"id": i, "hard": {"non_spicy": True}, "prefer": {}} for i in "ABC"]
        args = self._args_for(people=people)
        self.assertEqual(sum(r["quantity"] for r in args["items"]), 3)
        self.assertLess(len(args["items"]), 3)  # 已合并，不是逐人重复

    def test_builder_rejects_incomplete_input(self):
        """纯函数：缺地址/缺取餐选项/未实现的取餐方式直接拒绝。"""
        base = {"material": {
            "store_code": "1", "need_tableware": False, "take_way": {"code": "T"},
            "fulfillment": {"type": "pickup", "be_type": 1, "order_type": 1, "be_code": None},
        }}
        items = [{"productCode": "M1", "quantity": 1}]
        built = orders.build_create_order_args(base, items, None)
        self.assertEqual(built["takeWayCode"], "T")
        self.assertIsNot(built["items"], items)  # 深拷贝
        delivery = {"material": {
            "store_code": "1", "take_way": None,
            "fulfillment": {"type": "delivery", "be_type": 2, "order_type": 2, "be_code": "B"},
        }}
        with self.assertRaises(InputError):
            orders.build_create_order_args(delivery, items, None)
        self.assertEqual(orders.build_create_order_args(delivery, items, "A1")["addressId"], "A1")
        group = {"material": {
            "store_code": "1", "take_way": None,
            "fulfillment": {"type": "group", "be_type": 6, "order_type": 2, "be_code": "B"},
        }}
        with self.assertRaises(InputError):
            orders.build_create_order_args(group, items, "A1")
        with self.assertRaises(InputError):
            orders.build_create_order_args(base, [], None)


class PlaceSuccessTests(unittest.TestCase):
    """T-D2 与预演、三重门。"""

    def test_preview_makes_no_create_order(self):
        with OrderEnv() as env:
            digest = env.ready()
            before = len(env.sim.controlled_calls)
            code, out = env.place(digest, json_out=False)
            self.assertEqual(code, 0)
            for needle in ("下单预演", "尚未下单", "本次未调用 create-order", "需你自己付款",
                           "--confirm-digest " + digest):
                self.assertIn(needle, out)
            self.assertEqual(len(env.sim.controlled_calls), before)
            self.assertEqual(env.create_calls(), [])
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "none")
            self.assertNotIn("SIM-ORDER", out)

    def test_execute_once_and_wording(self):
        with OrderEnv() as env:
            digest = env.ready()
            code, out = env.place(digest, execute=True, json_out=False)
            self.assertEqual(code, 0, out)
            for needle in ("订单已创建", "待支付", "未付款", "不会自动付款", "不会自动取消"):
                self.assertIn(needle, out)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(len(env.sim.orders), 1)
            order_id = env.sim.orders[0]["orderId"]
            self.assertIn(order_id, out)
            record = env.record(digest)
            self.assertEqual(record["steps"]["order"]["state"], "created")
            self.assertEqual(record["steps"]["order"]["order_id"], order_id)
            self.assertEqual(record["steps"]["order"]["status_label"], "待支付")
            # orderId 在账本里只出现在 steps.order
            self.assertEqual(env.all_text().count(order_id), 1)
            for part in ("summary", "material", "events", "rebuild"):
                self.assertNotIn(order_id, json.dumps(record[part], ensure_ascii=False))
            # 支付信息不写账本
            self.assertNotIn("sim.invalid", env.all_text())
            self.assertNotIn("PAYURL", env.all_text())
            # 创建后尽力核对一次，且是只读
            self.assertEqual([c["tool"] for c in env.sim.calls].count("query-order"), 1)
            self.assertEqual(env.sim.duplicate_orders, 0)
            self.assertEqual(env.sim.forbidden_attempts, [])

    def test_json_result_contains_created_order_once(self):
        with OrderEnv() as env:
            digest = env.ready()
            code, out = env.place(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(data["status"], "created")
            self.assertEqual(data["created_order"]["orderId"], env.sim.orders[0]["orderId"])
            self.assertFalse(data["order"]["paid"])
            self.assertFalse(data["order"]["auto_pay"])
            self.assertFalse(data["order"]["auto_cancel"])
            self.assertEqual(data["order"]["status_code"], "1")
            self.assertEqual(data["order"]["amount_check"], "match")
            # created_order 以外的部分不含订单号/支付信息
            rest = json.dumps({k: v for k, v in data.items() if k != "created_order"}, ensure_ascii=False)
            self.assertNotIn(env.sim.orders[0]["orderId"], rest)
            self.assertNotIn("sim.invalid", rest)

    def test_non_pending_official_status_is_reported_honestly(self):
        """官方返回状态不是 1 时如实写，不擅自断言待支付。"""
        with OrderEnv() as env:
            digest = env.ready()
            orig = env.sim._order_detail

            def odd(order):
                detail = orig(order)
                detail["orderStatus"] = "2"
                return detail

            with patch.object(env.sim, "_order_detail", side_effect=odd):
                code, out = env.place(digest, execute=True, json_out=False)
            self.assertEqual(code, 0, out)
            self.assertIn("官方返回状态：2", out)
            self.assertNotIn("状态：待支付", out)
            with OrderEnv() as env2:
                digest2 = env2.ready()
                orig2 = env2.sim._order_detail

                def none(order):
                    detail = orig2(order)
                    detail.pop("orderStatus", None)
                    return detail

                with patch.object(env2.sim, "_order_detail", side_effect=none):
                    code, out = env2.place(digest2, execute=True, json_out=False)
                self.assertEqual(code, 0, out)
                self.assertIn("官方返回状态：未返回", out)

    def test_gates_each_required(self):
        """缺确认、错确认、短确认、大写确认一律退出 4，零 create-order。"""
        with OrderEnv() as env:
            digest = env.ready()
            for confirm in (None, "0" * 64, digest[:12], digest.upper()):
                argv = ["order-place", "--digest", digest[:12], "--json", "--execute"]
                if confirm is not None:
                    argv += ["--confirm-digest", confirm]
                self.assertEqual(env.run(*argv)[0], 4, confirm)
            self.assertEqual(env.create_calls(), [])
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "none")

    def test_official_endpoint_refused_before_network(self):
        with OrderEnv() as env:
            digest = env.ready()
            calls_before = len(env.sim.calls)
            boom = AssertionError("不应构造客户端或联网")
            with patch.object(cli, "client_from_args", REAL_CLIENT_FROM_ARGS), \
                    patch.object(McpClient, "__init__", side_effect=boom), \
                    patch.object(McpClient, "initialize", side_effect=boom), \
                    patch("urllib.request.OpenerDirector.open", side_effect=boom):
                code, out = env.run("order-place", "--digest", digest[:12], "--execute",
                                    "--confirm-digest", digest, "--json")
            self.assertEqual(code, 4)
            self.assertIn("真实领券/下单默认关闭且未经真实验证；如需启用，请阅读 docs/真实写操作启用指南.md", out)
            self.assertEqual(len(env.sim.calls), calls_before)
            self.assertEqual(env.create_calls(), [])
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "none")

    def test_claim_prerequisite(self):
        """需要领券却没领 → 退出 4，零下单；预演列出阻塞项。"""
        with OrderEnv() as env:
            code, out, data = env.summary()
            digest = data["digest"]
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 4, out)
            self.assertIn("order-claim", out)
            self.assertEqual(env.create_calls(), [])
            code, out = env.place(digest)
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out)["ready"])
            self.assertTrue(json.loads(out)["blockers"])

    def test_expired_and_superseded_digest_refused(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.advance(601)
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["status"], "expired")
            self.assertEqual(env.place(digest)[0], 3)
            self.assertEqual(env.create_calls(), [])

    def test_skip_claim_summary_can_place_directly(self):
        with OrderEnv() as env:
            code, out, data = env.summary("--skip-claim")
            self.assertEqual(code, 0)
            code, out = env.place(data["digest"], execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(env.sim.claims, 0)


class PlaceFailureTests(unittest.TestCase):
    """T-D3：业务失败 → failed；其余全部 unknown 且永不自动重试。"""

    def test_business_failure_is_failed_not_retried(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.fail("create-order", "business_fail")
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 1, out)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "failed")
            self.assertEqual(env.sim.orders, [])
            self.assertNotIn("SIM-SECRET", out)
            env.sim.faults.clear()
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 5)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(env.sim.orders, [])

    def _unknown(self, mode, expect_orders):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.fail("create-order", mode)
            if mode == "timeout":
                env.sim.timeout_delay = 0.5
                client = short_timeout_client(env)
                with patch.object(cli, "client_from_args", side_effect=lambda a, write=False: client):
                    code, out = env.place(digest, execute=True, json_out=False)
            else:
                code, out = env.place(digest, execute=True, json_out=False)
            self.assertEqual(code, 6, (mode, out))
            self.assertIn("不要重试", out)
            self.assertIn("order-status", out)
            self.assertEqual(len(env.create_calls()), 1, mode)  # 客户端没有自动重试
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "unknown", mode)
            self.assertIsNone(env.record(digest)["steps"]["order"]["order_id"])
            if expect_orders is not None:
                self.assertEqual(len(env.sim.orders), expect_orders, mode)
            env.sim.faults.clear()
            before = len(env.sim.controlled_calls)
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 5, mode)
            self.assertEqual(json.loads(out)["prior"]["order_state"], "unknown")
            self.assertEqual(len(env.sim.controlled_calls), before)
            self.assertEqual(env.sim.duplicate_orders, 0)

    def test_disconnect_is_unknown(self):
        self._unknown("disconnect", 0)

    def test_lost_response_is_unknown_even_though_server_created(self):
        self._unknown("lost_response", 1)

    def test_bad_structure_is_unknown(self):
        self._unknown("bad_structure", 1)

    def test_timeout_is_unknown(self):
        self._unknown("timeout", None)  # 服务端仍在处理，建单时间不确定

    def test_redirect_is_not_followed_and_unknown(self):
        self._unknown("redirect", 0)

    def test_http401_is_failed(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.fail("create-order", "http401")
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 1, out)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "failed")
            self.assertEqual(len(env.create_calls()), 1)

    def test_unknown_then_same_basket_guard_and_allow(self):
        """T-D4：unknown 后新摘要（同篮子）默认被拒；--allow-duplicate-basket 放行。"""
        with OrderEnv() as env:
            first = env.ready()
            env.sim.fail("create-order", "disconnect")
            self.assertEqual(env.place(first, execute=True)[0], 6)
            env.sim.faults.clear()
            env.advance(5)
            code, out, data = env.summary()
            second = data["digest"]
            self.assertNotEqual(first, second)
            self.assertEqual(data["claim"]["required"], False)
            code, out = env.place(second, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 5, out)
            self.assertEqual(data["status"], "duplicate_basket")
            self.assertEqual(data["conflicts"][0]["order_state"], "unknown")
            self.assertEqual(len(env.create_calls()), 1)
            # 预演也提示阻塞
            code, out = env.place(second)
            self.assertFalse(json.loads(out)["ready"])
            code, out = env.place(second, True, None, "--allow-duplicate-basket")
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.create_calls()), 2)
            self.assertEqual(env.record(second)["steps"]["order"]["state"], "created")

    def test_created_then_same_basket_refused(self):
        with OrderEnv() as env:
            first = env.ready()
            self.assertEqual(env.place(first, execute=True)[0], 0)
            env.advance(5)
            code, out, data = env.summary()
            second = data["digest"]
            code, out = env.place(second, execute=True)
            self.assertEqual(code, 5)
            self.assertEqual(json.loads(out)["conflicts"][0]["order_state"], "created")
            self.assertNotIn(env.sim.orders[0]["orderId"], out)
            self.assertEqual(len(env.create_calls()), 1)

    def test_failed_does_not_trigger_basket_guard(self):
        """明确失败（未创建）不阻止同篮子重新确认后再下单。"""
        with OrderEnv() as env:
            first = env.ready()
            env.sim.fail("create-order", "business_fail")
            self.assertEqual(env.place(first, execute=True)[0], 1)
            env.sim.faults.clear()
            env.advance(5)
            code, out, data = env.summary()
            code, out = env.place(data["digest"], execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.create_calls()), 2)


class PlaceDuplicateTests(unittest.TestCase):
    """T-D4：同 digest 只有一次下单机会。"""

    def test_duplicate_refused_without_network_or_order_id(self):
        with OrderEnv() as env:
            digest = env.ready()
            self.assertEqual(env.place(digest, execute=True)[0], 0)
            order_id = env.sim.orders[0]["orderId"]
            calls, controlled = len(env.sim.calls), len(env.sim.controlled_calls)
            code, out = env.place(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 5)
            self.assertEqual(data["status"], "duplicate")
            self.assertEqual(data["prior"]["order_state"], "created")
            self.assertNotIn(order_id, out)
            self.assertNotIn("payH5Url", out)
            self.assertEqual(len(env.sim.calls), calls)  # 离线拒绝
            self.assertEqual(len(env.sim.controlled_calls), controlled)
            self.assertEqual(len(env.sim.orders), 1)
            # 预演也告知已下过单
            code, out = env.place(digest)
            self.assertEqual(json.loads(out)["status"], "already_done")
            self.assertNotIn(order_id, out)

    def test_concurrent_place_only_one_order(self):
        import threading

        with OrderEnv() as env:
            digest = env.ready()
            results, lock = [], threading.Lock()
            barrier = threading.Barrier(5)

            def worker():
                client = sim_support.sim_client(env.sim)
                ledger = Ledger(env.root)
                barrier.wait()
                try:
                    code = orders.place_execute(client, ledger, digest[:12], digest)["exit_code"]
                except orders.OrderError as exc:
                    code = exc.exit_code
                with lock:
                    results.append(code)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(len(results), 5)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(len(env.sim.orders), 1)
            self.assertEqual(sorted(results), [0, 5, 5, 5, 5])


class WriteAheadTests(unittest.TestCase):
    """T-D5：写前账本与崩溃恢复。"""

    def test_pending_when_server_receives_request(self):
        with OrderEnv() as env:
            digest = env.ready()
            seen = []

            def spy(name, args):
                if name == TOOL_CREATE_ORDER:
                    rec = env.record(digest)["steps"]["order"]
                    seen.append((rec["state"], rec["attempt_id"] is not None, rec["order_id"]))

            env.sim.on_write = spy
            self.assertEqual(env.place(digest, execute=True)[0], 0)
            self.assertEqual(seen, [("pending", True, None)])

    def _release(self, created):
        for ledger in created:
            ledger.release_all()

    def test_crash_before_send_is_unknown_and_not_replayed(self):
        """①发请求前崩溃：重跑不下单，状态按 unknown 处理。"""
        with OrderEnv() as env:
            digest = env.ready()
            made = []
            real = Ledger

            def factory(root):
                made.append(real(root))
                return made[-1]

            with patch.object(cli, "Ledger", side_effect=factory), \
                    patch.object(gate.WriteGate, "call", side_effect=Crash()):
                with self.assertRaises(Crash):
                    env.place(digest, execute=True)
            self.assertEqual(env.create_calls(), [])
            self._release(made)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "unknown")
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 5, out)
            self.assertEqual(json.loads(out)["prior"]["order_state"], "unknown")
            self.assertEqual(env.create_calls(), [])
            self.assertEqual(env.sim.orders, [])
            code, out = env.status("--digest", digest[:12])
            self.assertIn("核对", json.loads(out)["message"])

    def test_server_created_but_terminal_state_not_written(self):
        """②服务端已建单但终态未写：重跑不重复下单，状态按 unknown 处理。"""
        with OrderEnv() as env:
            digest = env.ready()
            made = []
            real = Ledger
            original = Ledger.finish_step

            def factory(root):
                made.append(real(root))
                return made[-1]

            def broken(self, d, step, attempt, state, detail=None, order_id=None):
                if step == "order":
                    raise RuntimeError("磁盘写入失败")
                return original(self, d, step, attempt, state, detail, order_id)

            with patch.object(cli, "Ledger", side_effect=factory), \
                    patch.object(Ledger, "finish_step", broken):
                code, out = env.place(digest, execute=True, json_out=False)
            self.assertEqual(code, 6, out)
            self.assertIn("不要重试", out)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(len(env.sim.orders), 1)
            self._release(made)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "unknown")
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 5)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(len(env.sim.orders), 1)
            self.assertEqual(env.sim.duplicate_orders, 0)


class FreshnessTests(unittest.TestCase):
    """T-D6：下单前新鲜度校验与金额核对。"""

    def test_price_drift_requires_reconfirm_and_no_order(self):
        with OrderEnv() as env:
            old = env.ready()
            env.sim.set_price_drift(300)
            code, out = env.place(old, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 3, out)
            self.assertEqual(data["status"], "reconfirm")
            self.assertFalse(data["order_placed"])
            self.assertEqual(env.create_calls(), [])
            price = [c for c in data["changes"] if c["kind"] == "price"][0]
            self.assertEqual((price["before"], price["after"], price["delta_cents"]), (2000, 2300, 300))
            new = data["new_digest"]
            self.assertNotEqual(new, old)
            self.assertEqual(env.record(old)["superseded_by"], new)
            self.assertEqual(env.record(old)["steps"]["order"]["state"], "none")
            # 旧确认失效
            code, out = env.place(old, execute=True)
            self.assertEqual((code, json.loads(out)["status"]), (3, "superseded"))
            self.assertEqual(env.create_calls(), [])
            # 对新摘要重新确认后可下单，且金额为新价
            code, out = env.place(new, execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.create_calls()), 1)
            self.assertEqual(env.sim.orders[0]["amount_cents"], 2300)

    def test_preview_reports_change_without_writing(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.set_price_drift(-100)
            before = len(list(env.root.glob("*.json")))
            code, out = env.place(digest, json_out=False)
            self.assertEqual(code, 3)
            self.assertIn("¥20.00 → ¥19.00", out)
            self.assertEqual(len(list(env.root.glob("*.json"))), before)
            self.assertIsNone(env.record(digest)["superseded_by"])
            self.assertEqual(env.create_calls(), [])

    def test_amount_mismatch_warns_and_is_recorded(self):
        with OrderEnv() as env:
            digest = env.ready()
            env.sim.on_write = lambda name, args: env.sim.set_price_drift(200) if name == TOOL_CREATE_ORDER else None
            code, out = env.place(digest, execute=True, json_out=False)
            self.assertEqual(code, 0, out)
            self.assertIn("警告", out)
            self.assertIn("支付前请核对金额", out)
            self.assertIn("¥22.00", out)
            step = env.record(digest)["steps"]["order"]
            self.assertEqual(step["state"], "created")
            self.assertEqual(step["detail"]["amount_check"], "mismatch")

    def test_menu_change_means_reconfirm(self):
        """所依赖的商品从菜单消失：不下单，退出 3。"""
        with OrderEnv() as env:
            digest = env.ready()
            lines = env.record(digest)["rebuild"]["lines"]
            self.assertTrue(lines)
            original = dict(sim_mcp.MEALS)
            sim_mcp.MEALS.clear()
            try:
                code, out = env.place(digest, execute=True)
            finally:
                sim_mcp.MEALS.update(original)
            self.assertIn(code, (1, 3), out)
            self.assertEqual(env.create_calls(), [])

    def test_delivery_address_order_changed(self):
        """地址列表顺序变化：address_ref 不一致 → 退出 3，零下单。"""
        with OrderEnv() as env:
            digest = env.ready(**delivery_request())
            with patch.object(sim_mcp, "ADDRESSES", list(reversed(sim_mcp.ADDRESSES))):
                code, out = env.place(digest, execute=True)
                self.assertEqual(code, 3, out)
                self.assertEqual(json.loads(out)["status"], "address_changed")
                self.assertEqual(env.create_calls(), [])
                self.assertEqual(env.place(digest)[0], 3)
            # 恢复顺序后同一摘要仍可下单（摘要本身未被取代）
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(env.create_calls()[0]["args"]["addressId"], "ADDRID-1")

    def test_no_sensitive_values_in_failure_outputs(self):
        with OrderEnv() as env:
            digest = env.ready(**delivery_request())
            env.sim.fail("create-order", "business_fail")
            texts = [env.place(digest, execute=True)[1], env.place(digest, execute=True)[1], env.all_text()]
            for text in texts:
                for word in SECRETS + ("couponCode", "couponId", "addressId"):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
