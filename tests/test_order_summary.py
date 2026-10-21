"""订单摘要（T-B1/B2/B3）：字段、digest 稳定性与敏感字段排除、TTL、拒绝路径。"""
import contextlib
import copy
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from order_support import DIGEST_RE, OrderEnv, delivery_request  # noqa: E402
from meal_clear import orders  # noqa: E402
from meal_clear.core import InputError  # noqa: E402
from sim_mcp import ADDR_SENTINELS, COUPON_SENTINELS, NEW_COUPON_CODE  # noqa: E402
from sim_support import SENTINELS  # noqa: E402

BEEF = {"id": "A", "hard": {}, "prefer": {"main_meat": "beef"}}
CHICKEN = {"id": "B", "hard": {}, "prefer": {"main_meat": "chicken"}}
PREF = {"priority": "preference"}
SECRET_WORDS = SENTINELS + COUPON_SENTINELS + ADDR_SENTINELS + (
    NEW_COUPON_CODE, "couponCode", "couponId", "SIM-TOKEN", "phone", "contactName", "fullAddress", "addressId",
)
TOP_FIELDS = (
    "schema digest digest_short mode data_source created_at quoted_at expires_at ttl_seconds store fulfillment "
    "people_count groups per_person lines payable_cents fee_breakdown budget_cents within_budget take_way claim "
    "plan_ref execution state"
).split()


class SummaryFieldTests(unittest.TestCase):
    """T-B1：字段齐全、digest 稳定、变化敏感、敏感值排除。"""

    def test_pickup_fields_complete(self):
        """到店摘要含计划 §5.2 全部字段，digest 为 64 位十六进制。"""
        with OrderEnv() as env:
            code, out, data = env.summary()
            self.assertEqual(code, 0)
            for field in TOP_FIELDS:
                self.assertIn(field, data)
            self.assertRegex(data["digest"], r"\A[0-9a-f]{64}\Z")
            self.assertEqual(data["digest_short"], data["digest"][:12])
            self.assertEqual(data["fulfillment"]["type"], "pickup")
            self.assertEqual(data["store"]["store_code"], "10001")
            self.assertEqual(data["store"]["name_source"], "未核实")
            self.assertEqual(data["payable_cents"], 2000)
            self.assertEqual(data["take_way"]["selected_code"], "SIM-TW-1")
            self.assertEqual(data["claim"]["total"], 2)
            self.assertEqual({c["name"] for c in data["claim"]["coupons"]}, {"模拟可领券1", "模拟可领券2"})
            self.assertEqual(data["expires_at"], "2026-10-20T03:10:00+00:00")
            self.assertEqual(data["ttl_seconds"], 600)
            self.assertFalse(data["execution"]["real_orders_open"])
            self.assertEqual(data["state"], {"claim": "none", "requote": "none", "order": "none"})

    def test_drive_thru_and_delivery(self):
        """得来速与外送都能产出摘要；外送无取餐方式且带地址序号/标签/引用。"""
        with OrderEnv() as env:
            code, _, dt = env.summary(store="20001", fulfillment={"type": "drive_thru", "be_code": "BE20001"})
            self.assertEqual(code, 0)
            self.assertEqual(dt["fulfillment"]["type"], "drive_thru")
            self.assertEqual(dt["fulfillment"]["be_type"], 5)
            self.assertEqual(dt["fulfillment"]["be_code"], "BE20001")
            self.assertTrue(dt["take_way"]["required"])
            code, _, dl = env.summary(**delivery_request())
            self.assertEqual(code, 0)
            self.assertEqual(dl["fulfillment"]["order_type"], 2)
            self.assertEqual(dl["fulfillment"]["address_label"], "地址1")
            self.assertEqual(dl["fulfillment"]["address_index"], 1)
            self.assertRegex(dl["fulfillment"]["address_ref"], r"\A[0-9a-f]{8}\Z")
            self.assertEqual(dl["store"]["name"], "模拟外送店A")
            self.assertFalse(dl["take_way"]["required"])
            self.assertIsNone(dl["take_way"]["selected_code"])
            self.assertEqual(dl["fee_breakdown"]["deliveryPrice"], 600)
            self.assertEqual(len({dt["digest"], dl["digest"]}), 2)

    def test_store_name_lookup_optional(self):
        """到店可用 --city/--keyword 核实门店名；匹配不到则保持未核实。"""
        with OrderEnv() as env:
            path = env.request_path()
            _, out = env.run("order-summary", "--request", path, "--json", "--city", "X", "--keyword", "Y")
            data = json.loads(out)
            self.assertEqual(data["store"]["name"], "模拟一店")
            self.assertEqual(data["store"]["name_source"], "query-nearby-stores")
            path = env.request_path(store="10002")
            _, out = env.run("order-summary", "--request", path, "--json", "--city", "X", "--keyword", "Y")
            self.assertEqual(json.loads(out)["store"]["name"], "模拟二店")

    def test_text_render_contents(self):
        """文本含取餐方式、门店、数量、总额、将领券、报价时间、数据来源、过期时间、完整 digest、下一步。"""
        with OrderEnv() as env:
            path = env.request_path(people=[BEEF, CHICKEN], ranking=PREF)
            code, out = env.run("order-summary", "--request", path)
            self.assertEqual(code, 0)
            digest = DIGEST_RE.search(out).group(0)
            for needle in ("取餐方式：到店自取", "门店：10001（名称未核实）", "× 1", "官方核价应付总额：¥",
                           "模拟可领券1×1", "一键领全部，无法逐张选择，不可撤销", "报价时间：2026-10-20T03:00:00",
                           "数据来源模式：live", "过期时间：2026-10-20T03:10:00", "摘要编号（digest）：" + digest,
                           "--execute --confirm-digest " + digest, "需你明确同意"):
                self.assertIn(needle, out)
            for pid in ("A：", "B："):
                self.assertIn(pid, out)

    def test_same_input_same_clock_same_digest(self):
        """固定时钟下同输入同 digest，且不重复落账。"""
        with OrderEnv() as env:
            first = env.summary()[2]["digest"]
            second = env.summary()[2]["digest"]
            self.assertEqual(first, second)
            self.assertEqual(len(list(env.root.glob("*.json"))), 1)

    def test_clock_change_changes_digest(self):
        """报价窗口变化 → digest 变化（并产生新记录）。"""
        with OrderEnv() as env:
            first = env.summary()[2]["digest"]
            env.advance(1)
            second = env.summary()[2]["digest"]
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(env.root.glob("*.json"))), 2)

    def test_content_changes_change_digest(self):
        """总额/数量/取餐方式/将领券集合/门店/取餐类型任一变化 → digest 变化。"""
        with OrderEnv(take_way_count=2) as env:
            base = env.summary("--take-way", "1")[2]["digest"]
            variants = {}
            variants["取餐选项"] = env.summary("--take-way", "2")[2]["digest"]
            variants["不领券"] = env.summary("--take-way", "1", "--skip-claim")[2]["digest"]
            variants["人数"] = env.summary("--take-way", "1", people=[
                {"id": "A", "hard": {}, "prefer": {}}, {"id": "B", "hard": {}, "prefer": {}}])[2]["digest"]
            variants["门店"] = env.summary("--take-way", "1", store="10002")[2]["digest"]
            variants["外送"] = env.summary(**delivery_request())[2]["digest"]
            variants["得来速"] = env.summary("--take-way", "1", store="20001",
                                          fulfillment={"type": "drive_thru", "be_code": "BE20001"})[2]["digest"]
            env.sim.set_price_drift(100)
            variants["总额"] = env.summary("--take-way", "1")[2]["digest"]
            env.sim.set_price_drift(0)
            with patch("sim_mcp.CLAIMABLE", [{"couponName": "另一张券", "label": "合成", "couponStatus": 1}]):
                variants["将领券集合"] = env.summary("--take-way", "1")[2]["digest"]
            values = [base] + list(variants.values())
            self.assertEqual(len(values), len(set(values)), variants)

    def test_swapping_who_eats_what_keeps_digest(self):
        """仅调换谁吃哪份：digest 不变，但摘要里的逐人对应随本次请求更新。"""
        with OrderEnv() as env:
            one = env.summary(people=[BEEF, CHICKEN], ranking=PREF)[2]
            swapped = [dict(BEEF, prefer={"main_meat": "chicken"}), dict(CHICKEN, prefer={"main_meat": "beef"})]
            two = env.summary(people=swapped, ranking=PREF)[2]
            self.assertEqual(one["digest"], two["digest"])
            by_one = {p["person_id"]: p["offer_id"] for p in one["per_person"]}
            by_two = {p["person_id"]: p["offer_id"] for p in two["per_person"]}
            self.assertNotEqual(by_one, by_two)
            self.assertEqual(sorted(by_one.values()), sorted(by_two.values()))

    def test_material_field_mutations(self):
        """材料层：每个影响价格或内容的字段变化都会改变 digest。"""
        with OrderEnv() as env:
            record = env.record(env.summary(**delivery_request())[2]["digest"])
        material = record["material"]
        base = orders.compute_digest(material)
        self.assertEqual(base, record["digest"])

        def changed(mutate):
            copy_ = copy.deepcopy(material)
            mutate(copy_)
            return orders.compute_digest(copy_)

        mutations = {
            "store_code": lambda m: m.update(store_code="30002"),
            "fulfillment.type": lambda m: m["fulfillment"].update(type="pickup"),
            "fulfillment.be_code": lambda m: m["fulfillment"].update(be_code="BE30002"),
            "fulfillment.address_index": lambda m: m["fulfillment"].update(address_index=2),
            "fulfillment.address_ref": lambda m: m["fulfillment"].update(address_ref="deadbeef"),
            "lines.product_code": lambda m: m["lines"][0].update(product_code="M9"),
            "lines.quantity": lambda m: m["lines"][0].update(quantity=m["lines"][0]["quantity"] + 1),
            "lines.rounds": lambda m: m["lines"][0]["rounds"][0]["items"][0].update(code="OTHER"),
            "lines.coupon_key": lambda m: m["lines"][0].update(coupon_key="0" * 12),
            "total_cents": lambda m: m.update(total_cents=m["total_cents"] + 1),
            "fee_breakdown": lambda m: m["fee_breakdown"].update(deliveryPrice=1),
            "claim_coupons": lambda m: m["claim_coupons"].append({"name": "x", "label": "y", "count": 1}),
            "quoted_at": lambda m: m["quote_window"].update(quoted_at="2026-10-20T03:00:01+00:00"),
            "expires_at": lambda m: m["quote_window"].update(expires_at="2026-10-20T03:10:01+00:00"),
            "need_tableware": lambda m: m.update(need_tableware=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assertNotEqual(changed(mutate), base)

    def test_no_sensitive_values_anywhere(self):
        """输出、--save 文件、账本（summary/material/events）零出现券码、券 ID、地址、手机、联系人、Token。"""
        with OrderEnv() as env:
            texts = []
            saved = str(env.dir / "saved.json")
            for req in ({}, delivery_request()):
                path = env.request_path(**req)
                texts.append(env.run("order-summary", "--request", path)[1])
                texts.append(env.run("order-summary", "--request", path, "--json", "--save", saved)[1])
                texts.append(Path(saved).read_text(encoding="utf-8"))
            texts.append(env.all_text())
            for text in texts:
                for word in SECRET_WORDS:
                    self.assertNotIn(word, text)
            for path in env.root.glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                blob = json.dumps([record["summary"], record["material"], record["events"], record["rebuild"]],
                                  ensure_ascii=False)
                for word in SECRET_WORDS:
                    self.assertNotIn(word, blob)
            self.assertEqual(oct(Path(saved).stat().st_mode & 0o777), "0o600")

    def test_delivery_summary_has_no_address_text(self):
        """外送摘要只含地址序号/脱敏标签，不含地址文本、手机号、联系人。"""
        with OrderEnv() as env:
            code, out, data = env.summary(**delivery_request())
            self.assertEqual(code, 0)
            for word in ADDR_SENTINELS + ("ADDR-SENTINEL-1-B", "ADDRID-1"):
                self.assertNotIn(word, out)
                self.assertNotIn(word, env.all_text())
            allowed = {"type", "label", "be_type", "order_type", "be_code", "address_label", "address_index", "address_ref"}
            self.assertEqual(set(data["fulfillment"]), allowed)

    def test_material_clean_guard(self):
        """材料自检拒绝敏感键、链接和手机号形态的值。"""
        with OrderEnv() as env:
            material = env.record(env.summary()[2]["digest"])["material"]
        orders.assert_material_clean(material)
        for bad in (
            lambda m: m.update(couponCode="x"),
            lambda m: m["fulfillment"].update(be_code="http://x"),
            lambda m: m["fulfillment"].update(be_code="13800138000"),
            lambda m: m["lines"][0].update(orderId="x"),
        ):
            broken = copy.deepcopy(material)
            bad(broken)
            with self.assertRaises(InputError):
                orders.assert_material_clean(broken)

    def test_large_group_summary_is_compact(self):
        """≥13 人按同质组展示，--detail 展开逐人。"""
        people = [{"id": "P%02d" % i, "hard": {}, "prefer": {}} for i in range(20)]
        with OrderEnv() as env:
            path = env.request_path(people=people, budget=2000000)
            code, out = env.run("order-summary", "--request", path)
            self.assertEqual(code, 0)
            self.assertIn("G1（20人", out)
            self.assertNotIn("P05：", out)
            code, out = env.run("order-summary", "--request", path, "--detail")
            self.assertIn("P05：", out)
            data = json.loads(env.run("order-summary", "--request", path, "--json")[1])
            self.assertEqual(data["people_count"], 20)
            self.assertEqual(sum(l["quantity"] for l in data["lines"]), 20)


class TtlTests(unittest.TestCase):
    """T-B2：TTL 与时钟。"""

    def test_expired_execute_is_refused_exit_3(self):
        """过期后 --execute 一律退出 3，零写调用；预演提示过期。"""
        with OrderEnv() as env:
            digest = env.summary()[2]["digest"]
            env.advance(601)
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 3)
            self.assertIn("过期", out)
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.sim.write_attempts, [])
            code, out = env.claim(digest)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["status"], "expired")
            code, text = env.run("order-claim", "--digest", digest[:12])
            self.assertEqual(code, 3)
            self.assertIn("过期", text)

    def test_boundary_just_before_expiry_allowed(self):
        """到期前一刻仍有效，到期时刻即失效。"""
        with OrderEnv() as env:
            digest = env.summary()[2]["digest"]
            env.advance(599)
            self.assertEqual(env.claim(digest)[0], 0)
            env.advance(1)
            self.assertEqual(env.claim(digest)[0], 3)

    def test_clock_going_backwards_is_not_fresh(self):
        """当前时间早于创建时间 60 秒以上视为异常，不可执行。"""
        with OrderEnv() as env:
            digest = env.summary()[2]["digest"]
            env.advance(-61)
            code, _ = env.claim(digest, execute=True)
            self.assertEqual(code, 3)
            self.assertEqual(env.sim.controlled_calls, [])
            env.advance(2)
            self.assertEqual(env.claim(digest)[0], 0)

    def test_ttl_range(self):
        """--ttl-seconds 越界被拒（退出 1，且不联网不落账）；边界值可用并体现在过期时间。"""
        with OrderEnv() as env:
            path = env.request_path()
            for bad in ("59", "1801", "0", "-5"):
                code, out = env.run("order-summary", "--request", path, "--json", "--ttl-seconds", bad)
                self.assertEqual(code, 1, bad)
                self.assertIn("60–1800", out)
            self.assertEqual(env.sim.calls, [])
            self.assertFalse(list(env.root.glob("*.json")) if env.root.exists() else [])
            code, out = env.run("order-summary", "--request", path, "--json", "--ttl-seconds", "60")
            self.assertEqual(json.loads(out)["expires_at"], "2026-10-20T03:01:00+00:00")
            code, out = env.run("order-summary", "--request", path, "--json", "--ttl-seconds", "1800")
            self.assertEqual(json.loads(out)["expires_at"], "2026-10-20T03:30:00+00:00")

    def test_default_ttl_is_centralized(self):
        """默认 TTL 来自 config.SUMMARY_TTL_SECONDS（600）。"""
        from meal_clear import config
        self.assertEqual(config.SUMMARY_TTL_SECONDS, 600)
        self.assertEqual(config.SUMMARY_TTL_RANGE, (60, 1800))


class RefusalTests(unittest.TestCase):
    """T-B3：不生成可确认摘要的情形。"""

    def test_over_budget_not_recorded(self):
        """重新核价超预算：退出 3，不落账。"""
        real = orders.quote_basket

        def inflated(client, wire, items):
            quote = real(client, wire, items)
            quote["total_cents"] += 10**6
            quote["fee_breakdown"] = dict(quote["fee_breakdown"], productPrice=quote["fee_breakdown"]["productPrice"] + 10**6)
            return quote

        with OrderEnv() as env:
            with patch.object(orders, "quote_basket", side_effect=inflated):
                code, out, data = env.summary()
            self.assertEqual(code, 3)
            self.assertEqual(data["status"], "over_budget")
            self.assertFalse(list(env.root.glob("*.json")))

    def test_take_way_selection_required(self):
        """多个取餐选项必须用 --take-way 选择；越界被拒；外送不接受 --take-way。"""
        with OrderEnv(take_way_count=2) as env:
            code, out, data = env.summary()
            self.assertEqual(code, 3)
            self.assertEqual(data["status"], "need_take_way")
            self.assertEqual(len(data["options"]), 2)
            self.assertFalse(list(env.root.glob("*.json")))
            code, out = env.run("order-summary", "--request", env.request_path(), "--take-way", "2")
            self.assertEqual(code, 0)
            self.assertEqual(len(list(env.root.glob("*.json"))), 1)
            self.assertEqual(env.summary("--take-way", "2")[2]["take_way"]["selected_code"], "SIM-TW-2")
            self.assertEqual(env.summary("--take-way", "3")[0], 1)
            self.assertEqual(env.summary("--take-way", "1", **delivery_request())[0], 1)

    def test_zero_take_way_options_refused(self):
        """官方核价没有任何取餐选项：拒绝生成可下单摘要。"""
        with OrderEnv(take_way_count=0) as env:
            code, out, data = env.summary()
            self.assertEqual(code, 3)
            self.assertEqual(data["status"], "no_take_way")
            self.assertFalse(list(env.root.glob("*.json")))

    def test_signature_selection(self):
        """--signature 按方案编号（前缀）选择；找不到明确报错；与 --rank 互斥。"""
        with OrderEnv() as env:
            top = env.summary(people=[BEEF, CHICKEN], ranking={"priority": "preference", "top_n": 3})[2]
            signature = top["plan_ref"]["signature"]
            path = env.request_path(people=[BEEF, CHICKEN], ranking={"priority": "preference", "top_n": 3})
            code, out = env.run("order-summary", "--request", path, "--json", "--signature", signature[:8])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["plan_ref"]["signature"], signature)
            code, out = env.run("order-summary", "--request", path, "--json", "--rank", "2")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["plan_ref"]["rank"], 2)
            code, out = env.run("order-summary", "--request", path, "--json", "--signature", "ffffffff")
            self.assertEqual(code, 1)
            self.assertIn("未找到该方案编号", out)
            code, out = env.run("order-summary", "--request", path, "--json", "--signature", "XYZ")
            self.assertEqual(code, 1)
            code, out = env.run("order-summary", "--request", path, "--json", "--rank", "99")
            self.assertEqual(code, 1)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                env.run("order-summary", "--request", path, "--signature", signature, "--rank", "1")

    def test_no_plan_exit_2(self):
        """没有预算内方案：退出 2，不落账。"""
        with OrderEnv() as env:
            code, out, data = env.summary(budget=100)
            self.assertEqual(code, 2)
            self.assertEqual(data["status"], "no_plan")
            self.assertFalse(list(env.root.glob("*.json")))

    def test_requires_v2_request(self):
        """v1 请求不能生成订单摘要。"""
        with OrderEnv() as env:
            path = env.dir / "v1.json"
            path.write_text(json.dumps({"context": {"store_code": "10001"}}), encoding="utf-8")
            code, out = env.run("order-summary", "--request", str(path))
            self.assertEqual(code, 1)
            self.assertIn("schema_version=2", out)

    def test_summary_is_read_only(self):
        """生成摘要不调用任何受控写工具。"""
        with OrderEnv() as env:
            env.summary()
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.sim.write_attempts, [])
            self.assertTrue({c["tool"] for c in env.sim.calls} <= set(__import__("meal_clear.mcp", fromlist=["READ_ONLY"]).READ_ONLY))


if __name__ == "__main__":
    unittest.main()
