"""P2 官方路径隐私：Token/订单号/券码/地址不出现在输出、确认提示、账本与异常文案中（打桩传输，全部合成数据）。"""
import json
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import build_skill  # noqa: E402
from official_support import ORDER, OfficialEnv  # noqa: E402
from order_support import delivery_request  # noqa: E402
from sim_mcp import (  # noqa: E402
    ADDR_SENTINELS, COUPON_SENTINELS, NEW_COUPON_CODE, ORDER_ID_PREFIX, ORDER_SENTINELS, SIM_TOKEN,
)

ADDR_ONLY = ADDR_SENTINELS + ("ADDR-SENTINEL-1-B", "ADDRID-1", "ADDRID-2")
COUPON_ONLY = COUPON_SENTINELS + ("COUPONID-1", "COUPONID-2", "COUPONID-3", NEW_COUPON_CODE, "MYID-1", "BASE64-SENTINEL")
ORDER_ONLY = ORDER_SENTINELS + (ORDER_ID_PREFIX, "SIM-PAY-", "sim.invalid")
TOKEN_WORDS = (SIM_TOKEN, "SIM-TOKEN", "Bearer")


class PrivacyTests(unittest.TestCase):
    def scan(self, label, text, words):
        for word in words:
            self.assertNotIn(word, text, "%s 泄露了 %s" % (label, word))

    def full_flow(self, env, **req):
        """摘要→官方领券→官方下单；返回 (非创建输出们, 创建那次输出, 确认提示)。"""
        outputs = []
        code, out, data = env.summary(**req)
        self.assertEqual(code, 0, out)
        outputs.append(out)
        digest = data["digest"]
        if data["claim"]["required"]:
            code, out = env.official_claim(digest)
            self.assertEqual(code, 0, out)
            outputs.append(out)
        outputs.append(env.claim(digest)[1])  # 已领券：预演只读
        outputs.append(env.place(digest)[1])
        code, created = env.official_place(digest)
        self.assertEqual(code, 0, created)
        prompts = env.stdout.getvalue()
        for _ in range(2):  # 重复与状态查询
            outputs.append(env.official_place(digest)[1])
        outputs.append(env.status("--digest", digest[:12])[1])
        return outputs, created, prompts

    def test_no_token_in_output_ledger_logs_errors(self):
        for req in ({}, delivery_request()):
            with self.subTest(delivery=bool(req)), OfficialEnv() as env:
                outputs, created, prompts = self.full_flow(env, **req)
                labelled = [("输出%d" % i, text) for i, text in enumerate(outputs)]
                labelled += [("创建输出", created), ("确认提示", prompts), ("账本", env.ledger_text())]
                for label, text in labelled:
                    self.scan(label, text, TOKEN_WORDS)
                # 桩只记录 (url, 是否带头, 方法, 工具, 参数)：参数里也不能有 Token
                self.scan("桩请求记录", json.dumps(env.stub.requests, ensure_ascii=False), TOKEN_WORDS)

    def test_no_token_in_error_paths(self):
        for fault in ("timeout", "redirect", "lost"):
            with self.subTest(fault=fault), OfficialEnv() as env:
                digest = env.official_ready()
                env.stub.fault(ORDER, fault)
                code, out = env.official_place(digest)
                self.assertEqual(code, 6, out)
                self.scan("异常文案", out, TOKEN_WORDS + ("mcp.mcd.cn", "Traceback", str(ROOT)))
                self.scan("账本", env.ledger_text(), TOKEN_WORDS)

    def test_no_order_id_coupon_code_address_in_ledger_and_non_creation_output(self):
        for req in ({}, delivery_request()):
            with self.subTest(delivery=bool(req)), OfficialEnv() as env:
                outputs, created, prompts = self.full_flow(env, **req)
                order_id = env.sim.orders[0]["orderId"]
                self.assertIn(order_id, created)  # 订单号只在创建当次出现
                for text in outputs + [prompts]:
                    self.assertNotIn(order_id, text)
                    self.scan("非创建输出", text, ADDR_ONLY + COUPON_ONLY + ORDER_ONLY)
                # 账本：订单步骤的 order_id 字段是 P1 既有的唯一落点（order-status --digest 需要），其余位置不得出现
                ledger_text = env.ledger_text()
                self.assertEqual(ledger_text.count(order_id), 1)
                self.scan("账本", ledger_text.replace(order_id, ""), ADDR_ONLY + COUPON_ONLY + ORDER_ONLY)

    def test_ledger_never_stores_token_or_args(self):
        with OfficialEnv() as env:
            self.full_flow(env)
            text = env.ledger_text()
            self.scan("账本", text, TOKEN_WORDS + ("couponCode", "couponId", "addressId", "roundList",
                                                 "comboItemList", "payH5Url", "payId", "COUPONCODE", "ADDRID"))

    def test_confirm_prompt_has_summary_and_unpaid_notice_without_secrets(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.official_place(digest)
            prompt = env.stdout.getvalue()
            for word in ("门店", "取餐方式", "商品", "应付", "待支付", "不会自动付款", "摘要有效至", digest[-6:] + " 确认下单"):
                self.assertIn(word, prompt)
            self.scan("提示", prompt, TOKEN_WORDS + ADDR_ONLY + COUPON_ONLY + ORDER_ONLY)
            self.assertNotIn(digest, prompt)  # 完整 digest 不进提示，只给末 6 位

    def test_dist_package_contains_no_token_shaped_strings(self):
        package = ROOT / "dist" / "meal-clear.skill"
        self.assertTrue(package.exists())
        with zipfile.ZipFile(package) as archive:
            for name in archive.namelist():
                try:
                    text = archive.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for pattern in build_skill.SECRET_PATTERNS:
                    self.assertIsNone(pattern.search(text), "%s 命中 %s" % (name, pattern.pattern))


if __name__ == "__main__":
    unittest.main()
