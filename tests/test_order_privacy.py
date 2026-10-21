"""T-X2：隐私总扫。

全部命令输出（创建订单那一次除外）、--save 文件、账本 summary/material/events、
dist/meal-clear.skill 解压内容，对 Token、券码、券 ID、地址/手机/联系人哨兵、orderId、
支付链接、取餐码零命中；创建那次输出只允许含订单号与支付信息。
"""
import json
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from order_support import OrderEnv, delivery_request, sim_support  # noqa: E402
from sim_mcp import (  # noqa: E402
    ADDR_SENTINELS, COUPON_SENTINELS, NEW_COUPON_CODE, ORDER_ID_PREFIX, ORDER_SENTINELS, SIM_TOKEN,
)

ROOT = Path(__file__).resolve().parents[1]
ADDR_ONLY = ADDR_SENTINELS + ("ADDR-SENTINEL-1-B", "ADDRID-1", "ADDRID-2")
COUPON_ONLY = COUPON_SENTINELS + ("COUPONID-1", "COUPONID-2", "COUPONID-3", NEW_COUPON_CODE, "MYID-1", "BASE64-SENTINEL")
ORDER_ONLY = ORDER_SENTINELS + (ORDER_ID_PREFIX, "SIM-PAY-", "sim.invalid")
SECRETS = (SIM_TOKEN, "SIM-TOKEN") + ADDR_ONLY + COUPON_ONLY + ORDER_ONLY
# 创建那一次允许出现的只有订单号与支付信息
CREATE_ALLOWED = ORDER_ID_PREFIX + "|PAYURL-SENTINEL|sim.invalid|SIM-PAY-"


def scan(testcase, label, text, words=SECRETS):
    for word in words:
        testcase.assertNotIn(word, text, f"{label} 泄露了 {word}")


class OrderPrivacyTests(unittest.TestCase):

    def _full_run(self, env, outputs, saved, **req):
        """摘要(+save)、预演、领券、下单（返回创建输出）、重复、状态、失败路径；收集除创建外的全部输出。"""
        path = env.request_path(**req)
        save = env.dir / ("summary-%d.json" % len(saved))
        code, out = env.run("order-summary", "--request", path, "--save", str(save), "--json")
        self.assertEqual(code, 0, out)
        saved.append(save)
        outputs.append(out)
        digest = json.loads(out)["digest"]
        d12 = digest[:12]
        outputs.append(env.run("order-summary", "--request", path)[1])
        outputs.append(env.run("order-claim", "--digest", d12)[1])
        outputs.append(env.run("order-claim", "--digest", d12, "--json")[1])
        outputs.append(env.run("order-place", "--digest", d12)[1])
        outputs.append(env.run("order-place", "--digest", d12, "--json")[1])
        outputs.append(env.run("order-place", "--digest", d12, "--execute", "--confirm-digest", "0" * 64)[1])
        outputs.append(env.run("order-claim", "--digest", d12, "--execute", "--confirm-digest", digest)[1])
        created_code, created_out = env.run("order-place", "--digest", d12, "--execute", "--confirm-digest", digest)
        self.assertEqual(created_code, 0, created_out)
        outputs.append(env.run("order-place", "--digest", d12, "--execute", "--confirm-digest", digest)[1])
        outputs.append(env.run("order-place", "--digest", d12, "--json")[1])
        outputs.append(env.run("order-status", "--digest", d12)[1])
        outputs.append(env.run("order-status", "--digest", d12, "--json")[1])
        outputs.append(env.run("order-status", "--order-id", env.sim.orders[-1]["orderId"])[1])
        outputs.append(env.run("order-status", "--order-id", "SIM-ORDER-9999")[1])
        return created_out

    def test_created_output_only_adds_order_and_payment_info(self):
        with OrderEnv() as env:
            outputs, saved = [], []
            created = self._full_run(env, outputs, saved)
            order_id = env.sim.orders[0]["orderId"]
            self.assertIn(order_id, created)
            self.assertIn("PAYURL-SENTINEL", created)
            # 去掉允许的订单号与支付信息后，其余内容不含任何其它敏感值
            stripped = created
            for allowed in ("http://sim.invalid/pay/PAYURL-SENTINEL-" + order_id, order_id, "SIM-PAY-0001"):
                stripped = stripped.replace(allowed, "")
            scan(self, "创建输出", stripped)
            for word in ADDR_ONLY + COUPON_ONLY + (SIM_TOKEN,) + ("PICKUP-SENTINEL", "LOCKER-SENTINEL", "RIDER-SENTINEL"):
                self.assertNotIn(word, created)

    def test_every_other_output_ledger_and_saved_file_is_clean(self):
        for label, req in (("到店", {}), ("外送", delivery_request())):
            with OrderEnv() as env:
                outputs, saved = [], []
                self._full_run(env, outputs, saved, **req)
                order_id = env.sim.orders[0]["orderId"]
                self.assertGreaterEqual(len(outputs), 14)
                for number, text in enumerate(outputs):
                    scan(self, f"{label} 输出#{number}", text)
                    self.assertNotIn(order_id, text)
                for path in saved:
                    scan(self, f"{label} --save 文件", path.read_text(encoding="utf-8"))
                # 账本：summary / material / events / rebuild 不含任何敏感值，订单号只在 steps.order
                for ledger_file in env.root.glob("*.json"):
                    record = json.loads(ledger_file.read_text(encoding="utf-8"))
                    for part in ("summary", "material", "events", "rebuild", "options"):
                        text = json.dumps(record[part], ensure_ascii=False)
                        scan(self, f"{label} 账本.{part}", text)
                        self.assertNotIn(order_id, text)
                    for step in ("claim", "requote"):
                        scan(self, f"{label} 账本.steps.{step}", json.dumps(record["steps"][step], ensure_ascii=False))
                    order_step = dict(record["steps"]["order"])
                    if order_step.pop("order_id"):
                        self.assertEqual(order_step["state"], "created")
                    scan(self, f"{label} 账本.steps.order(除订单号)", json.dumps(order_step, ensure_ascii=False))
                scan(self, f"{label} 账本全部文件(除订单号)", env.all_text().replace(order_id, ""))

    def test_failure_paths_are_clean_too(self):
        with OrderEnv() as env:
            digest = env.ready(**delivery_request())
            texts = []
            env.sim.fail("create-order", "business_fail")
            texts.append(env.place(digest, execute=True)[1])
            texts.append(env.place(digest, execute=True)[1])
            env.sim.faults.clear()
            env.advance(5)
            code, out, data = env.summary(**delivery_request())
            env.sim.fail("create-order", "lost_response")
            texts.append(env.place(data["digest"], execute=True, json_out=False)[1])
            texts.append(env.status("--digest", data["digest"][:12])[1])
            texts.append(env.all_text())
            for text in texts:
                scan(self, "失败路径", text)

    def test_skill_package_has_no_secrets_or_order_data(self):
        package = ROOT / "dist" / "meal-clear.skill"
        self.assertTrue(package.exists())
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            self.assertFalse([n for n in names if "local-state" in n or "/verification/" in n])
            for name in names:
                if name.endswith("/") or "/tests/" in name:
                    continue  # tests/ 下的模拟服务源码本身就定义了合成哨兵，不是泄露
                data = archive.read(name)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                scan(self, "发布包 " + name, text, (SIM_TOKEN,) + ADDR_ONLY + COUPON_ONLY + ORDER_SENTINELS)


if __name__ == "__main__":
    unittest.main()
