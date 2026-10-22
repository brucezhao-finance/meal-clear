"""团餐订单摘要、回环领券/下单端到端，以及官方端点团餐真实写的四层拒绝（零传输调用）。"""
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from meal_clear import cli, config, orders, realwrite  # noqa: E402
from meal_clear.core import InputError  # noqa: E402
from meal_clear.mcp import McpClient, McpError, WriteRefused  # noqa: E402
from official_support import CLAIM, ORDER, OfficialEnv  # noqa: E402
from order_support import OrderEnv, delivery_request  # noqa: E402
from sim_mcp import FORBIDDEN_WRITE  # noqa: E402
from sim_support import SENTINELS  # noqa: E402
def people(n=3):
    return [{"id": f"P{i}", "hard": {}, "prefer": {}} for i in range(n)]
def group_request(gm="GM-STD", reservation=None, budget=100000, be_code="GBE30001", store="30001", n=3):
    """团餐摘要请求参数（地址1，团餐店A）。"""
    ful = {"type": "group", "be_code": be_code, "address_index": 1}
    if gm:
        ful["gm_service_code"] = gm
    if reservation:
        ful["reservation_date"] = reservation
    return {"store": store, "fulfillment": ful, "people": people(n), "budget": budget}
class DigestTests(unittest.TestCase):
    """摘要 digest 覆盖团餐字段；非团餐材料键不变。"""
    def test_digest_changes_per_field(self):
        with OrderEnv() as env:
            digests = {}
            for label, kwargs in {
                "base": {},
                "gm": {"gm": "GM-ALT"},
                "reservation": {"reservation": "2099-01-02 09:30"},
                "reservation2": {"reservation": "2099-01-02 10:00"},
                "budget": {"budget": 150000},
                "store_becode": {"store": "30002", "be_code": "GBE30002"},
            }.items():
                code, out, data = env.summary("--skip-claim", **group_request(**kwargs))
                self.assertEqual(code, 0, f"{label}: {out}")
                digests[label] = data["digest"]
            self.assertEqual(len(set(digests.values())), len(digests), digests)
            code, _, again = env.summary("--skip-claim", **group_request())
            self.assertEqual(again["digest"], digests["base"])  # 同输入同时钟幂等
    def test_material_fields_each_change_digest(self):
        with OrderEnv() as env:
            code, out, data = env.summary("--skip-claim", **group_request(reservation="2099-01-02 09:30"))
            material = env.record(data["digest"])["material"]
            ful = material["fulfillment"]
            self.assertEqual(ful["gm_service_code"], "GM-STD")
            self.assertEqual(ful["reservation_date"], "2099-01-02 09:30")
            self.assertEqual(ful["per_person_budget_cents"], 33333)
            self.assertEqual(ful["people_count"], 3)
            base = orders.compute_digest(material)
            self.assertEqual(base, data["digest"])
            for path, value in (("gm_service_code", "X"), ("reservation_date", "2099-01-02 09:31"),
                                ("per_person_budget_cents", 1), ("be_code", "OTHER"), ("people_count", 4)):
                changed = deepcopy(material)
                changed["fulfillment"][path] = value
                self.assertNotEqual(orders.compute_digest(changed), base, path)
    def test_summary_fields_and_non_group_material_unchanged(self):
        with OrderEnv() as env:
            code, out, data = env.summary("--skip-claim", **group_request())
            self.assertEqual(data["group_budget"], {"people_count": 3, "total_cents": 100000,
                                                    "per_person_cents": 33333, "remainder_cents": 1})
            self.assertEqual(data["fulfillment"]["gm_service_code"], "GM-STD")
            self.assertEqual(data["fulfillment"]["gm_service_name"], "模拟标准送达")
            rec = env.record(data["digest"])
            self.assertEqual(rec["rebuild"]["price_extra"], {"gmServiceCode": "GM-STD"})
            code, out, plain = env.summary("--skip-claim", **delivery_request())
            self.assertEqual(code, 0, out)
            self.assertNotIn("group_budget", plain)
            rec = env.record(plain["digest"])
            self.assertEqual(set(rec["material"]["fulfillment"]),
                             {"type", "be_type", "order_type", "be_code", "address_index", "address_ref"})
            self.assertEqual(rec["rebuild"]["price_extra"], {})
    def test_old_ledger_record_without_price_extra(self):
        """旧账本记录没有 rebuild.price_extra：重新核价照常工作。"""
        import sim_support
        with OrderEnv() as env:
            code, out, data = env.summary("--skip-claim", **delivery_request())
            record = env.record(data["digest"])
            del record["rebuild"]["price_extra"]
            check = orders.verify_fresh(sim_support.sim_client(env.sim), record)
            self.assertTrue(check["consistent"])
    def test_requote_group_carries_gm(self):
        import sim_support
        with OrderEnv() as env:
            code, out, data = env.summary("--skip-claim", **group_request())
            before = len([c for c in env.sim.calls if c["tool"] == "calculate-price"])
            check = orders.verify_fresh(sim_support.sim_client(env.sim), env.record(data["digest"]))
            self.assertTrue(check["consistent"])
            calcs = [c["args"] for c in env.sim.calls if c["tool"] == "calculate-price"][before:]
            self.assertTrue(calcs and all(a["gmServiceCode"] == "GM-STD" for a in calcs))
class BuildArgsTests(unittest.TestCase):
    """create-order 参数构造。"""
    def material(self, **ful):
        base = {"type": "group", "be_type": 6, "order_type": 2, "be_code": "GBE30001"}
        base.update(ful)
        return {"material": {"store_code": "30001", "take_way": None, "fulfillment": base}}
    ITEMS = [{"productCode": "M1", "quantity": 1}]
    def test_args(self):
        built = orders.build_create_order_args(self.material(gm_service_code="GM-STD"), self.ITEMS, "ADDRID-1")
        self.assertEqual(set(built), {"storeCode", "orderType", "beType", "beCode", "gmServiceCode",
                                      "addressId", "items", "needTableware"})
        built = orders.build_create_order_args(
            self.material(gm_service_code="GM-STD", reservation_date="2099-01-02 09:30"), self.ITEMS, "ADDRID-1")
        self.assertEqual(built["reservationDate"], "2099-01-02 09:30")
        self.assertNotIn("takeWayCode", built)
        self.assertEqual((built["beType"], built["orderType"]), (6, 2))
    def test_missing_gm_raises(self):
        for ful in ({}, {"gm_service_code": None}, {"gm_service_code": ""}):
            with self.assertRaises(InputError):
                orders.build_create_order_args(self.material(**ful), self.ITEMS, "ADDRID-1")
class LoopbackEndToEndTests(unittest.TestCase):
    """回环 sim：团餐领券、下单端到端。"""
    def test_claim_and_place(self):
        with OrderEnv() as env:
            code, out, data = env.summary(**group_request(reservation="2099-01-02 09:30"))
            self.assertEqual(code, 0, out)
            self.assertTrue(data["claim"]["required"])
            digest = data["digest"]
            code, out = env.claim(digest)
            self.assertEqual(code, 0, out)
            code, out = env.place(digest)
            self.assertEqual(code, 0, out)
            self.assertEqual(env.create_calls(), [])
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(env.sim.claims, 1)
            code, out = env.place(digest, execute=True)
            body = json.loads(out)
            self.assertEqual((code, body["status"]), (0, "created"), out)
            calls = env.create_calls()
            self.assertEqual(len(calls), 1)
            args = calls[0]["args"]
            self.assertEqual({k: v for k, v in args.items() if k != "items"}, {
                "storeCode": "30001", "orderType": 2, "beType": 6, "beCode": "GBE30001",
                "addressId": "ADDRID-1", "gmServiceCode": "GM-STD", "reservationDate": "2099-01-02 09:30",
                "needTableware": False})
            self.assertNotIn("takeWayCode", args)
            self.assertEqual(sum(i["quantity"] for i in args["items"]), 3)
            self.assertEqual(len(env.sim.orders), 1)
            self.assertEqual(env.sim.duplicate_orders, 0)
            code, out = env.place(digest, execute=True)  # 重复被拒
            self.assertEqual(code, config.EXIT_DUPLICATE, out)
            self.assertEqual(len(env.sim.orders), 1)
            self.assertEqual(env.sim.forbidden_attempts, [])
    def test_no_reservation_no_param(self):
        with OrderEnv() as env:
            digest = env.ready(**group_request())
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 0, out)
            self.assertNotIn("reservationDate", env.create_calls()[0]["args"])
    def test_privacy(self):
        with OrderEnv() as env:
            code, out, data = env.summary(**group_request())
            digest = data["digest"]
            outs = [out, env.claim(digest, execute=True)[1], env.place(digest)[1], env.place(digest, execute=True)[1]]
            outs.append(env.all_text())
            for blob in outs:
                for sentinel in SENTINELS + ("ADDR-SENTINEL-1", "PHONE-SENTINEL-1", "CONTACT-SENTINEL-1"):
                    self.assertNotIn(sentinel, blob)
class OfficialGroupRefusalBase(unittest.TestCase):
    def assert_closed(self, env, code, out):
        self.assertEqual(code, config.EXIT_REFUSED, out)
        body = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(body.get("reason") == config.GROUP_REFUSAL_REASON
                        or body.get("status") == "group_real_write_closed", out)
        self.assertIn("团餐真实下单尚未开放", body["message"])
        requests, inits = env.since()
        self.assertEqual(requests, [], out)
        self.assertEqual(inits, 0)
        env.urlopen_mock.assert_not_called()
        self.assertEqual(env.official_writes(), [])
        self.assertEqual(env.sim.controlled_calls, [])
        self.assertEqual(env.sim.write_attempts, [])
class OfficialCliRefusalTests(OfficialGroupRefusalBase):
    """CLI 层：开关开/关、紧急锁、非 TTY 的 claim 与 place 均 exit 4 且零请求零 initialize。"""
    def scenarios(self):
        yield "switch_on", dict(switch=True), None
        yield "switch_off", dict(switch=False), None
        yield "locked", dict(switch=True), "lock"
        yield "not_tty", dict(switch=True), "notty"
    def test_claim_and_place_refused(self):
        for label, kwargs, mode in self.scenarios():
            with self.subTest(label), OfficialEnv(**kwargs) as env:
                code, out, data = env.summary("--skip-claim", **group_request())
                self.assertEqual(code, 0, out)
                skip = data["digest"]
                code, out, data = env.summary(**group_request(gm="GM-ALT"))
                self.assertEqual(code, 0, out)
                with_claim = data["digest"]
                if mode == "lock":
                    self.assertEqual(env.run("write-lock", "--on", "--json")[0], 0)
                tty = mode != "notty"
                env.mark()
                code, out = env.official_claim(with_claim, tty=tty)
                self.assert_closed(env, code, out)
                env.mark()
                code, out = env.official_place(skip, tty=tty)
                self.assert_closed(env, code, out)
                self.assertEqual(env.record(skip)["steps"]["order"]["state"], "none")
                self.assertEqual(env.record(with_claim)["steps"]["claim"]["state"], "none")
                self.assertEqual(env.stdin.tell(), 0)
    def test_wrong_phrase_still_group_closed(self):
        """即使人工短语错误，也先是团餐拒绝（不泄露其他拒绝原因、不读终端）。"""
        with OfficialEnv() as env:
            code, out, data = env.summary(**group_request())
            env.mark()
            code, out = env.official_claim(data["digest"], phrase="随便")
            self.assert_closed(env, code, out)
    def test_preview_notes_closed_on_official(self):
        with OfficialEnv() as env:
            code, out, data = env.summary("--skip-claim", **group_request())
            digest = data["digest"]
            code, out = env.claim(digest)
            code, out = env.place(digest)
            body = json.loads(out)
            self.assertTrue(body["group_real_write_closed"])
            self.assertIn("团餐真实下单未开放", body["note"])
            self.assertEqual(env.official_writes(), [])
    def test_non_group_official_flow_unchanged(self):
        with OfficialEnv() as env:
            digest = env.official_ready(**delivery_request())
            code, out = env.official_place(digest)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.official_writes(ORDER)), 1)
class OfficialLibraryLayerTests(OfficialGroupRefusalBase):
    """orders / realwrite / 传输层：直接调用各层，同样零传输。"""
    def test_orders_layer_with_switch_open(self):
        with OfficialEnv(switch=True) as env:
            code, out, data = env.summary("--skip-claim", **group_request())
            skip = data["digest"]
            code, out, data = env.summary(**group_request(gm="GM-ALT"))
            claim = data["digest"]
            client = McpClient()
            env.mark()
            with self.assertRaises(orders.OrderError) as caught:
                orders.place_execute(client, env.ledger, skip, skip)
            self.assertEqual((caught.exception.exit_code, caught.exception.payload),
                             (4, {"status": "group_real_write_closed", "reason": "group_closed"}))
            with self.assertRaises(orders.OrderError) as caught:
                orders.claim_execute(client, env.ledger, claim, claim)
            self.assertEqual(caught.exception.exit_code, 4)
            requests, inits = env.since()
            self.assertEqual((requests, inits), ([], 0))
            self.assertEqual(env.official_writes(), [])
    def test_authorize_first_line(self):
        with OfficialEnv(switch=True) as env:
            code, out, data = env.summary("--skip-claim", **group_request())
            record = env.record(data["digest"])
            for tool in (CLAIM, ORDER):
                with self.assertRaises(realwrite.RealWriteDenied) as caught:
                    realwrite.authorize(env.ledger, tool, record, env.now)
                self.assertEqual(caught.exception.reason, "group_closed")
                self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn("group_closed", config.REAL_REFUSAL_REASONS)
    def test_transport_layer_zero_requests(self):
        with OfficialEnv(switch=True) as env:
            client = McpClient()
            env.mark()
            args = {"storeCode": "30001", "orderType": 2, "beType": 6, "beCode": "GBE30001",
                    "addressId": "x", "gmServiceCode": "GM-STD", "items": [{"productCode": "M1", "quantity": 1}]}
            with self.assertRaises(WriteRefused) as caught:
                client.call_write(ORDER, args, grant=object())
            self.assertIn("团餐真实下单尚未开放", str(caught.exception))
            client._write_ctx = ORDER
            client._official_ctx = ORDER  # 即使上下文齐全，传输层仍拒绝团餐 create-order
            with self.assertRaises(WriteRefused) as caught:
                client._post("tools/call", {"name": ORDER, "arguments": args})
            self.assertIn("团餐真实下单尚未开放", str(caught.exception))
            requests, inits = env.since()
            self.assertEqual((requests, inits), ([], 0))
    def test_other_write_tools_still_refused(self):
        with OfficialEnv(switch=True) as env:
            client = McpClient()
            env.mark()
            for tool in FORBIDDEN_WRITE:
                with self.assertRaises(WriteRefused):
                    client.call_write(tool, {}, grant=object())
                with self.assertRaises(McpError):
                    client.call(tool, {})
            for tool in (ORDER, CLAIM):
                with self.assertRaises(McpError):
                    client.call(tool, {})
            requests, inits = env.since()
            self.assertEqual((requests, inits), ([], 0))
        self.assertEqual(config.CONTROLLED_WRITE_TOOLS, frozenset({CLAIM, ORDER}))
if __name__ == "__main__":
    unittest.main()
