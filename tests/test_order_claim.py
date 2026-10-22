"""受控领券（T-C1~C5、T-E2 后半）：预演零写、执行恰好一次、领券后重新核价、变化即新摘要、重复与并发。"""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from order_support import REAL_CLIENT_FROM_ARGS, OrderEnv, delivery_request, sim_support  # noqa: E402
from meal_clear import cli, orders  # noqa: E402
from meal_clear.ledger import Ledger  # noqa: E402
from meal_clear.mcp import McpClient  # noqa: E402
from sim_mcp import ADDR_SENTINELS, COUPON_SENTINELS, NEW_COUPON_CODE, ORDER_SENTINELS  # noqa: E402
import sim_mcp  # noqa: E402

SECRETS = sim_support.SENTINELS + COUPON_SENTINELS + ADDR_SENTINELS + (NEW_COUPON_CODE, "couponCode", "couponId")


def make(env, **kw):
    """生成摘要并返回完整 digest。"""
    code, out, data = env.summary(**kw)
    assert code == 0, out
    return data["digest"]


class ClaimPreviewTests(unittest.TestCase):
    """T-C1：默认预演，服务端零写。"""

    def test_preview_makes_no_write(self):
        with OrderEnv() as env:
            digest = make(env)
            code, out = env.claim(digest)
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(data["status"], "preview")
            self.assertFalse(data["executed"])
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.sim.write_attempts, [])
            self.assertEqual(env.sim.claims, 0)
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "none")
            self.assertEqual(env.record(digest)["steps"]["requote"]["state"], "none")
            self.assertNotIn("auto-bind-coupons", {c["tool"] for c in env.sim.calls})

    def test_preview_text_wording(self):
        """文本预演含一键领全部、无法逐张选择、不可撤销、本次未调用 auto-bind-coupons。"""
        with OrderEnv() as env:
            digest = make(env)
            code, out = env.run("order-claim", "--digest", digest[:12])
            self.assertEqual(code, 0)
            for needle in ("一键领全部", "无法逐张选择", "不可撤销", "本次未调用 auto-bind-coupons",
                           "模拟可领券1", "--confirm-digest " + digest):
                self.assertIn(needle, out)
            self.assertEqual(env.sim.controlled_calls, [])

    def test_preview_unknown_digest(self):
        """未知/格式错误的摘要编号退出 1 且不联网。"""
        with OrderEnv() as env:
            make(env)
            before = len(env.sim.calls)
            for bad in ("0" * 12, "../../x", "abc"):
                code, out = env.run("order-claim", "--digest", bad, "--json")
                self.assertEqual(code, 1, bad)
            self.assertEqual(len(env.sim.calls), before)

    def test_preview_detects_changed_claimable(self):
        """预演发现可领券与摘要不同：退出 3，仍零写。"""
        with OrderEnv() as env:
            digest = make(env)
            with patch.object(sim_mcp, "CLAIMABLE", [{"couponName": "新券", "label": "合成", "couponStatus": 1}]):
                code, out = env.claim(digest)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["status"], "claimable_changed")
            self.assertEqual(env.sim.controlled_calls, [])

    def test_not_required_when_skipped(self):
        """跳过领券的摘要：预演退出 0 提示无需领券；--execute 被拒（退出 4）。"""
        with OrderEnv() as env:
            digest = make(env)
            _, out, data = env.summary("--skip-claim")
            skipped = data["digest"]
            self.assertFalse(data["claim"]["required"])
            self.assertEqual(env.claim(skipped)[0], 0)
            code, out = env.claim(skipped, execute=True)
            self.assertEqual(code, 4)
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.record(skipped)["steps"]["claim"]["state"], "not_required")
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "none")


class ClaimExecuteTests(unittest.TestCase):
    """T-C2：执行恰好一次，之后重新查券与核价。"""

    def test_execute_once_then_requote_in_order(self):
        with OrderEnv() as env:
            digest = make(env)
            marks = []
            env.sim.on_write = lambda name, args: marks.append(len(env.sim.calls))
            before_record = env.record(digest)
            code, out = env.claim(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 0, out)
            self.assertEqual(data["status"], "claimed")
            self.assertEqual(data["requote"], "consistent")
            self.assertEqual([c["tool"] for c in env.sim.controlled_calls], ["auto-bind-coupons"])
            self.assertEqual(env.sim.controlled_calls[0]["args"], {})
            self.assertEqual(env.sim.claims, 1)
            after = [c["tool"] for c in env.sim.calls[marks[0]:]]
            self.assertIn("query-store-coupons", after)
            self.assertIn("calculate-price", after)
            self.assertLess(after.index("query-store-coupons"), after.index("calculate-price"))
            record = env.record(digest)
            self.assertEqual(record["steps"]["claim"]["state"], "succeeded")
            self.assertEqual(record["steps"]["requote"]["state"], "consistent")
            self.assertIsNone(record["superseded_by"])
            self.assertEqual(record["material"], before_record["material"])
            self.assertEqual(env.sim.duplicate_orders, 0)
            self.assertEqual(env.sim.orders, [])

    def test_pending_written_before_request(self):
        """写前账本：服务端收到 auto-bind-coupons 的那一刻，账本已是 pending。"""
        with OrderEnv() as env:
            digest = make(env)
            seen = []
            env.sim.on_write = lambda name, args: seen.append(env.record(digest)["steps"]["claim"]["state"])
            self.assertEqual(env.claim(digest, execute=True)[0], 0)
            self.assertEqual(seen, ["pending"])

    def test_gates_each_required(self):
        """缺确认、错确认、短确认一律退出 4，零写。"""
        with OrderEnv() as env:
            digest = make(env)
            other = "0" * 64
            for argv in (
                ["--execute"],
                ["--execute", "--confirm-digest", other],
                ["--execute", "--confirm-digest", digest[:12]],
                ["--execute", "--confirm-digest", digest.upper()],
                ["--confirm-digest", digest],
            ):
                code, out = env.run("order-claim", "--digest", digest[:12], "--json", *argv)
                if "--execute" in argv:
                    self.assertEqual(code, 4, argv)
                else:
                    self.assertEqual(code, 0, argv)  # 没有 --execute 就只是预演
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "none")

    def test_official_endpoint_refused_before_network(self):
        """官方端点下 --execute 在联网前被拒（退出 4）。"""
        with OrderEnv() as env:
            digest = make(env)
            calls_before = len(env.sim.calls)
            boom = AssertionError("不应构造客户端或联网")
            with patch.object(cli, "client_from_args", REAL_CLIENT_FROM_ARGS), \
                    patch.object(McpClient, "__init__", side_effect=boom), \
                    patch.object(McpClient, "initialize", side_effect=boom), \
                    patch("urllib.request.OpenerDirector.open", side_effect=boom):
                code, out = env.run("order-claim", "--digest", digest[:12], "--execute",
                                    "--confirm-digest", digest, "--json")
            self.assertEqual(code, 4)
            self.assertIn("默认关闭且未经真实验证", out)
            self.assertEqual(len(env.sim.calls), calls_before)
            self.assertEqual(env.sim.controlled_calls, [])
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "none")

    def test_write_failure_recorded_and_not_retried(self):
        """官方明确拒绝 → failed（退出 1）；同 digest 不再重试（退出 5）。"""
        with OrderEnv() as env:
            digest = make(env)
            env.sim.fail("auto-bind-coupons", "business_fail")
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 1)
            self.assertEqual(len(env.sim.controlled_calls), 1)
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "failed")
            env.sim.faults.clear()
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 5)
            self.assertEqual(len(env.sim.controlled_calls), 1)
            self.assertEqual(env.sim.claims, 0)

    def test_unknown_result_exit_6(self):
        """连接中断 → unknown（退出 6），提示先核实不要重试；之后重复被拒。"""
        with OrderEnv() as env:
            digest = make(env)
            env.sim.fail("auto-bind-coupons", "disconnect")
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 6)
            self.assertIn("不要重试", out)
            self.assertEqual(len(env.sim.controlled_calls), 1)
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "unknown")
            env.sim.faults.clear()
            self.assertEqual(env.claim(digest, execute=True)[0], 5)
            self.assertEqual(len(env.sim.controlled_calls), 1)

    def test_delivery_claim(self):
        """外送摘要同样可以领券并重新核价。"""
        with OrderEnv() as env:
            digest = make(env, **delivery_request())
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 0, out)
            self.assertEqual(env.record(digest)["steps"]["requote"]["state"], "consistent")

    def test_no_sensitive_values_in_outputs_or_ledger(self):
        """领券全流程的输出与账本不含券码、券 ID、地址、手机号、Token、订单相关哨兵。"""
        with OrderEnv() as env:
            env.sim.set_claim_effect(price_delta=-300, add_store_coupon=True)
            digest = make(env)
            texts = [env.claim(digest)[1], env.claim(digest, execute=True)[1], env.claim(digest, execute=True)[1]]
            texts.append(env.all_text())
            for text in texts:
                for word in SECRETS + ORDER_SENTINELS:
                    self.assertNotIn(word, text)


class ClaimChangedTests(unittest.TestCase):
    """T-C3/C4：变化即新摘要，旧确认失效。"""

    def _changed(self, env, delta, **kw):
        env.sim.set_claim_effect(price_delta=delta)
        digest = make(env, **kw)
        code, out = env.claim(digest, execute=True)
        return digest, code, json.loads(out)

    def test_price_drop_after_claim(self):
        """领券后便宜 ¥3.00：退出 3，新摘要（新 digest + supersedes 链接），旧摘要作废。"""
        with OrderEnv() as env:
            old, code, data = self._changed(env, -300)
            self.assertEqual(code, 3)
            self.assertEqual(data["status"], "reconfirm")
            new = data["new_digest"]
            self.assertNotEqual(new, old)
            price = [c for c in data["changes"] if c["kind"] == "price"][0]
            self.assertEqual((price["before"], price["after"], price["delta_cents"]), (2000, 1700, -300))
            self.assertEqual(data["summary"]["payable_cents"], 1700)
            self.assertEqual(data["summary"]["supersedes"], old)
            old_rec, new_rec = env.record(old), env.record(new)
            self.assertEqual(old_rec["superseded_by"], new)
            self.assertEqual(new_rec["supersedes"], old)
            self.assertEqual(old_rec["steps"]["claim"]["state"], "succeeded")
            self.assertEqual(old_rec["steps"]["requote"]["state"], "changed")
            self.assertEqual(new_rec["steps"]["claim"]["state"], "succeeded")
            self.assertFalse(new_rec["summary"]["claim"]["required"])
            self.assertEqual(new_rec["material"]["claim_coupons"], [])
            self.assertEqual(new_rec["material"]["total_cents"], 1700)
            self.assertGreater(new_rec["created_at"], "2026-10-20T00:00:00")
            self.assertEqual(len(env.sim.controlled_calls), 1)
            # 旧 digest 之后不能再用于领券/下单
            self.assertIsNotNone(orders._reconfirm_reason(old_rec, env.now))
            code, out = env.claim(old, execute=True)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["status"], "superseded")
            self.assertEqual(len(env.sim.controlled_calls), 1)
            # 新摘要已继承“已领券”，不能再领
            self.assertEqual(env.claim(new, execute=True)[0], 5)

    def test_price_rise_text_shows_yuan(self):
        """领券后变贵 ¥5.00：文本变化清单用元显示。"""
        with OrderEnv() as env:
            env.sim.set_claim_effect(price_delta=500)
            digest = make(env)
            code, out = env.run("order-claim", "--digest", digest[:12], "--execute", "--confirm-digest", digest)
            self.assertEqual(code, 3)
            self.assertIn("¥20.00 → ¥25.00（+¥5.00）", out)
            self.assertIn("旧摘要作废", out)

    def test_over_budget_after_claim_has_no_new_summary(self):
        """领券后涨价超预算：退出 3，说明无法生成新摘要，不产生新记录。"""
        with OrderEnv() as env:
            env.sim.set_claim_effect(price_delta=500)
            digest = make(env, budget=2200)
            code, out = env.claim(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 3)
            self.assertNotIn("new_digest", data)
            self.assertIn("重新运行 order-summary", data["message"])
            self.assertEqual(len(list(env.root.glob("*.json"))), 1)
            self.assertEqual(env.record(digest)["steps"]["requote"]["state"], "changed")

    def test_store_coupon_change_without_price_change_is_note_only(self):
        """领券后本店券集合变化但价格不变：只提示，退出 0。"""
        with OrderEnv() as env:
            env.sim.set_claim_effect(price_delta=0, add_store_coupon=True)
            digest = make(env)
            code, out = env.claim(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(data["requote"], "consistent")
            self.assertTrue(any("重新 order-summary" in n for n in data["notes"]))

    def test_claimable_set_changes_before_execute(self):
        """T-C4：预演与执行之间可领券集合变化 → 拒绝并出新摘要，零写调用。"""
        with OrderEnv() as env:
            digest = make(env)
            self.assertEqual(env.claim(digest)[0], 0)
            changed = [{"couponName": "新增可领券", "label": "合成", "couponStatus": 1}]
            with patch.object(sim_mcp, "CLAIMABLE", changed):
                code, out = env.claim(digest, execute=True)
                data = json.loads(out)
                self.assertEqual(code, 3)
                self.assertEqual(env.sim.controlled_calls, [])
                self.assertEqual(env.sim.write_attempts, [])
                self.assertEqual(env.sim.claims, 0)
                new = data["new_digest"]
                self.assertNotEqual(new, digest)
                self.assertEqual(data["summary"]["claim"]["coupons"],
                                 [{"name": "新增可领券", "label": "合成", "count": 1}])
                self.assertEqual(env.record(digest)["superseded_by"], new)
                self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "none")
                self.assertEqual(env.record(new)["steps"]["claim"]["state"], "none")
                # 旧确认失效；新摘要需重新确认后才能领券
                self.assertEqual(env.claim(digest, execute=True)[0], 3)
                self.assertEqual(env.sim.controlled_calls, [])
                code, out = env.claim(new, execute=True)
                self.assertEqual(code, 0, out)
            self.assertEqual(env.sim.claims, 1)


class ClaimDuplicateTests(unittest.TestCase):
    """T-C5 与 T-E2 后半。"""

    def test_second_claim_refused_exit_5(self):
        with OrderEnv() as env:
            digest = make(env)
            self.assertEqual(env.claim(digest, execute=True)[0], 0)
            calls = len(env.sim.calls)
            controlled = len(env.sim.controlled_calls)
            code, out = env.claim(digest, execute=True)
            data = json.loads(out)
            self.assertEqual(code, 5)
            self.assertEqual(data["status"], "duplicate")
            self.assertEqual(data["prior"]["claim_state"], "succeeded")
            self.assertEqual(data["prior"]["requote_state"], "consistent")
            self.assertEqual(len(env.sim.controlled_calls), controlled)
            self.assertEqual(len(env.sim.calls), calls)  # 离线拒绝：服务端无任何新增调用
            self.assertEqual(env.sim.claims, 1)
            code, out = env.claim(digest)
            self.assertEqual(json.loads(out)["status"], "already_done")

    def test_concurrent_claims_only_one_effective(self):
        """多线程竞争 claim_execute：服务端 claims == 1。"""
        with OrderEnv() as env:
            digest = make(env)
            results = []
            lock = threading.Lock()
            barrier = threading.Barrier(6)

            def worker():
                client = sim_support.sim_client(env.sim)
                ledger = Ledger(env.root)
                barrier.wait()
                try:
                    out = orders.claim_execute(client, ledger, digest[:12], digest)
                    code = out["exit_code"]
                except orders.OrderError as exc:
                    code = exc.exit_code
                with lock:
                    results.append(code)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(len(results), 6)
            self.assertEqual(env.sim.claims, 1)
            self.assertEqual(len(env.sim.controlled_calls), 1)
            self.assertEqual(sorted(results), [0, 5, 5, 5, 5, 5])
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "succeeded")

    def test_crash_after_pending_is_unknown_and_not_replayed(self):
        """写前账本：进程在发请求后崩溃（pending 留下）→ 视为 unknown，拒绝重放。"""
        with OrderEnv() as env:
            digest = make(env)
            ledger = Ledger(env.root)
            ledger.begin_step(digest, "claim", ("none",))
            ledger.release_all()
            code, out = env.claim(digest, execute=True)
            self.assertEqual(code, 5)
            self.assertEqual(json.loads(out)["prior"]["claim_state"], "unknown")
            self.assertEqual(env.sim.controlled_calls, [])


if __name__ == "__main__":
    unittest.main()
