"""P2 官方路径流程（打桩传输）：新鲜度、价格变化、unknown 锁、order-resolve、write-lock、跨进程上限。"""
import contextlib
import io
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import sim_mcp  # noqa: E402
from meal_clear import cli, realwrite, report_orders  # noqa: E402
from meal_clear.config import CHANNEL_OFFICIAL, EXIT_LOCKED, REAL_COUNTED_STATES  # noqa: E402
from meal_clear.ledger import Ledger, LedgerError  # noqa: E402
from official_support import CLAIM, ORDER, OfficialEnv  # noqa: E402
from order_support import T0  # noqa: E402
from real_write_support import digest_of  # noqa: E402
from test_real_write_gate import Base, seed_official  # noqa: E402

ALLOWED_TOOLS = {
    "query-nearby-stores", "query-meals", "query-meal-detail", "query-store-coupons", "calculate-price",
    "available-coupons", "query-my-coupons", "delivery-query-addresses", "delivery-query-stores",
    "query-order", CLAIM, ORDER,
}


class FreshnessTests(Base):
    def test_freshness_runs_before_create_order_and_write_ahead(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            seen = {}

            def on_write(name, args):  # 服务端收到写请求的瞬间：账本必须已写前落账为 pending/official
                step = env.record(digest)["steps"]["order"]
                seen.update(state=step["state"], channel=step.get("channel"))

            env.sim.on_write = on_write
            start = len(env.stub.requests)
            self.assertEqual(env.official_place(digest)[0], 0)
            methods = [r["tool"] for r in env.stub.requests[start:] if r["tool"]]
            at = methods.index(ORDER)
            self.assertIn("query-store-coupons", methods[:at])
            self.assertIn("calculate-price", methods[:at])
            self.assertEqual(seen, {"state": "pending", "channel": "official"})
            self.assertEqual(env.record(digest)["steps"]["order"]["channel"], "official")

    def test_price_drift_before_order_refused_and_new_summary(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.sim.set_price_drift(300)
            code, out = env.official_place(digest)
            body = json.loads(out)
            self.assertEqual((code, body["status"]), (3, "reconfirm"), out)
            self.assertEqual(env.official_writes(ORDER), [])
            self.assertTrue(env.record(digest)["superseded_by"])
            self.assertNotEqual(body["new_digest"], digest)
            self.assertEqual(env.stdin.tell(), 0)  # 价格变化先于人工确认：没有让人确认过期内容

    def test_price_drift_before_claim_refused_no_auto_bind(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            env.sim.set_price_drift(300)
            code, out = env.official_claim(data["digest"])
            self.assertEqual(code, 3, out)
            self.assertEqual(env.official_writes(CLAIM), [])
            self.assertEqual(env.sim.claims, 0)

    def test_menu_or_coupon_changed_refused(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            original = dict(sim_mcp.MEALS)
            sim_mcp.MEALS.clear()
            try:
                code, out = env.official_place(digest)
            finally:
                sim_mcp.MEALS.update(original)
            self.assertIn(code, (1, 3), out)
            self.assertEqual(env.official_writes(ORDER), [])

    def test_claim_requote_change_requires_reconfirm_official(self):
        with OfficialEnv() as env:
            env.sim.set_claim_effect(price_delta=300)
            code, out, data = env.summary()
            digest = data["digest"]
            code, out = env.official_claim(digest)
            self.assertEqual(code, 3, out)
            self.assertEqual(len(env.official_writes(CLAIM)), 1)
            new = json.loads(out)["new_digest"]
            self.assertNotEqual(new, digest)
            code, out = env.official_place(new)  # 对新摘要重新确认后才可下单
            self.assertEqual(code, 0, out)
            self.assertEqual(len(env.official_writes(CLAIM)), 1)  # 不再领券


class UnknownAndResolveTests(Base):
    def make_unknown(self, env, tool=ORDER):
        if tool == ORDER:
            digest = env.official_ready()
            env.stub.fault(ORDER, "timeout")
            code, out = env.official_place(digest)
        else:
            code, out, data = env.summary()
            digest = data["digest"]
            env.stub.fault(CLAIM, "timeout")
            code, out = env.official_claim(digest)
        self.assertEqual(code, 6, out)
        env.stub.faults.clear()
        return digest, out

    def test_unknown_locks_basket_24h_and_not_auto_retried(self):
        with OfficialEnv() as env:
            digest, out = self.make_unknown(env)
            self.assertIn("锁定", out)
            self.assertEqual(len(env.official_writes(ORDER)), 1)  # 不自动重试
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "unknown")
            env.advance(1)
            code, out, data = env.summary()
            new = data["digest"]
            self.assertNotEqual(new, digest)
            code, out = env.official_place(new)  # 同篮子守卫先拒绝（既有退出码 5）
            self.assertEqual(code, 5, out)
            env.mark()
            code, out = env.official_place(new, None, "--allow-duplicate-basket")
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LOCKED, "unknown_lock"), out)
            self.assert_no_traffic(env)
            env.advance(24 * 3600 + 10)  # 窗口外锁自动失效（摘要需重新生成）
            code, out, data = env.summary()
            self.assertEqual(env.official_place(data["digest"], None, "--allow-duplicate-basket")[0], 0)

    def test_order_resolve_requires_tty_and_phrase(self):
        with OfficialEnv() as env:
            digest, _ = self.make_unknown(env)
            short = digest[:12]
            argv = ("order-resolve", "--digest", short, "--step", "order", "--as", "not-created", "--json")
            env.tty("%s 确认标注" % digest[-6:], tty=False)
            code, out = env.run(*argv)
            self.assertEqual((code, json.loads(out)["reason"]), (4, "not_tty"))
            for wrong in ("", "确认标注", "%s 确认下单" % digest[-6:], "%s 确认标注 " % digest[-6:]):
                env.tty(wrong)
                code, out = env.run(*argv)
                self.assertEqual((code, json.loads(out)["reason"]), (4, "phrase_mismatch"), out)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "unknown")
            env.tty("%s 确认标注" % digest[-6:])
            code, out = env.run(*argv)
            self.assertEqual(code, 0, out)
            self.assertEqual(env.record(digest)["steps"]["order"]["state"], "failed")
            events = [e for e in env.record(digest)["events"] if e["event"] == "manual_resolved"]
            self.assertEqual(len(events), 1)

    def test_order_resolve_not_created_unlocks(self):
        with OfficialEnv() as env:
            digest, _ = self.make_unknown(env)
            env.tty("%s 确认标注" % digest[-6:])
            code, out = env.run("order-resolve", "--digest", digest[:12], "--step", "order", "--as", "not-created")
            self.assertEqual(code, 0, out)
            self.assertIn("已人工标注", out)
            env.advance(1)
            code, out, data = env.summary()
            self.assertEqual(env.official_place(data["digest"])[0], 0)

    def test_order_resolve_created_keeps_basket_guard(self):
        with OfficialEnv() as env:
            digest, _ = self.make_unknown(env)
            env.tty("%s 确认标注" % digest[-6:])
            self.assertEqual(env.run("order-resolve", "--digest", digest[:12], "--step", "order", "--as", "created")[0], 0)
            step = env.record(digest)["steps"]["order"]
            self.assertEqual(step["state"], "created")
            self.assertIsNone(step.get("order_id"))  # 人工标注不写订单号
            env.advance(1)
            code, out, data = env.summary()
            code, out = env.official_place(data["digest"], None)
            self.assertEqual(code, 5, out)  # 同篮子已有已创建订单：重复守卫仍生效

    def test_order_resolve_rejects_non_unknown_or_loopback(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.tty("%s 确认标注" % digest[-6:])
            code, out = env.run("order-resolve", "--digest", digest[:12], "--step", "order", "--as", "created", "--json")
            self.assertEqual(code, 4, out)  # 尚未下单（none）
            seeded = seed_official(env, 7, "order", "created", basket="x")
            env.tty("%s 确认标注" % seeded[-6:])
            self.assertEqual(env.run("order-resolve", "--digest", seeded[:12], "--step", "order", "--as", "not-created")[0], 4)
            env.tty("whatever")
            code, out = env.run("order-resolve", "--digest", digest[:12], "--step", "order", "--as", "claimed")
            self.assertEqual(code, 1, out)  # step 与 --as 不匹配
        with OfficialEnv() as env:  # 回环记录
            ledger = env.ledger
            loop = digest_of(8)
            ledger.create(loop, basket_key="y", created_at=T0.isoformat(), expires_at=T0.isoformat())
            attempt = ledger.begin_step(loop, "order", ("none",))
            ledger.finish_step(loop, "order", attempt, "unknown")
            ledger.release_all()
            env.tty("%s 确认标注" % loop[-6:])
            self.assertEqual(env.run("order-resolve", "--digest", loop[:12], "--step", "order", "--as", "not-created")[0], 4)
            self.assertEqual(env.record(loop)["steps"]["order"]["state"], "unknown")

    def test_claim_unknown_resolve_claimed(self):
        with OfficialEnv() as env:
            digest, out = self.make_unknown(env, CLAIM)
            env.tty("%s 确认标注" % digest[-6:])
            code, out = env.run("order-resolve", "--digest", digest[:12], "--step", "claim", "--as", "claimed")
            self.assertEqual(code, 0, out)
            self.assertEqual(env.record(digest)["steps"]["claim"]["state"], "succeeded")


class WriteLockCommandTests(Base):
    def test_write_lock_on_blocks_all_official_writes(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.tty(tty=False)
            code, out = env.run("write-lock", "--on", "--json")  # 无需 TTY
            self.assertEqual((code, json.loads(out)["locked"]), (0, True))
            env.mark()
            code, out = env.official_place(digest)
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LOCKED, "locked"))
            self.assert_no_traffic(env)

    def test_write_lock_off_requires_tty_and_phrase(self):
        with OfficialEnv() as env:
            env.run("write-lock", "--on")
            env.tty("确认解除写锁", tty=False)
            code, out = env.run("write-lock", "--off", "--json")
            self.assertEqual((code, json.loads(out)["reason"]), (4, "not_tty"))
            env.tty("不对")
            self.assertEqual(env.run("write-lock", "--off", "--json")[0], 4)
            self.assertTrue(realwrite.is_locked(env.state))
            env.tty("确认解除写锁")
            code, out = env.run("write-lock", "--off", "--json")
            self.assertEqual((code, json.loads(out)["locked"]), (0, False))

    def test_write_lock_status_and_on_need_no_tty(self):
        with OfficialEnv() as env:
            env.tty(tty=False)
            code, out = env.run("write-lock", "--status", "--json")
            self.assertEqual((code, json.loads(out)["locked"]), (0, False))
            self.assertIn("未锁定", env.run("write-lock", "--status")[1])
            self.assertEqual(env.run("write-lock", "--on")[0], 0)
            self.assertIn("已锁定", env.run("write-lock", "--status")[1])

    def test_help_shows_chinese_and_exit_codes_0_to_8(self):
        for command in ("write-lock", "order-resolve", "order-place"):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit) as cm:
                cli.main([command, "--help"])
            self.assertEqual(cm.exception.code, 0)
            text = " ".join(buffer.getvalue().split())  # argparse 会折行
            for code in range(9):
                self.assertIn("%d %s" % (code, report_orders.exit_hint(code)), text)
            self.assertIn("本机真实写上限", text)
            self.assertIn("真实写被锁定", text)


class CapAcrossProcessTests(Base):
    def test_order_count_cap_across_processes(self):
        with OfficialEnv() as env:
            env.write_config(max_orders_24h=1)
            digest = env.official_ready()
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from datetime import datetime, timezone\n"
                "from meal_clear import ledger as L\n"
                "T0 = datetime(2026, 10, 20, 3, 0, 0, tzinfo=timezone.utc)\n"
                "L._now = lambda: T0\n"
                "led = L.Ledger(%r)\n"
                "d = %r\n"
                "led.create(d, basket_key='proc', created_at=T0.isoformat(), expires_at=T0.isoformat())\n"
                "led.begin_step(d, 'order', ('none',), channel='official')\n"
            ) % (str(ROOT / "scripts"), str(env.root), digest_of(11))
            subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
            env.mark()
            code, out = env.official_place(digest)
            self.assertEqual((code, json.loads(out)["reason"]), (7, "orders_cap"), out)
            self.assert_no_traffic(env)

    def test_concurrent_begin_respects_cap(self):
        with OfficialEnv() as env:
            env.write_config(max_orders_24h=1)
            cfg = realwrite.load_config(env.state)
            digests = [digest_of(21), digest_of(22)]
            for d in digests:
                env.ledger.create(d, basket_key="c", created_at=T0.isoformat(), expires_at=T0.isoformat())
            results = []
            barrier = threading.Barrier(2)

            def worker(d):
                ledger = Ledger(env.root)
                barrier.wait()
                try:
                    with ledger.global_locked():
                        realwrite.check_counts(ledger, cfg, ORDER, env.now)
                        ledger.begin_step(d, "order", ("none",), channel=CHANNEL_OFFICIAL)
                    results.append("ok")
                except realwrite.RealWriteDenied:
                    results.append("denied")
                except LedgerError:
                    results.append("ledger")

            threads = [threading.Thread(target=worker, args=(d,)) for d in digests]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(sorted(results), ["denied", "ok"])
            self.assertEqual(env.ledger.count_channel_writes(
                CHANNEL_OFFICIAL, "order", REAL_COUNTED_STATES["order"], 86400, env.now), 1)


class OutputTests(Base):
    def test_created_order_message_says_unpaid(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.tty(env.phrase(digest, ORDER))
            code, out = env.place(digest, True, None, json_out=False)
            self.assertEqual(code, 0, out)
            for word in ("订单已创建，未付款", "官方待支付订单", "不会付款也不会取消"):
                self.assertIn(word, out)
            code, out = env.official_place(env.official_ready(), None)  # 同篮子重复被拒，不影响上面的输出断言
            self.assertEqual(code, 5)

    def test_json_result_has_channel_and_message(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            code, out = env.official_place(digest)
            body = json.loads(out)
            self.assertEqual((body["channel"], body["message"]), ("official", "订单已创建，尚未付款"))
            self.assertFalse(body["order"]["paid"] or body["order"]["auto_pay"] or body["order"]["auto_cancel"])

    def test_no_auto_pay_or_cancel_calls(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            self.assertEqual(env.official_place(digest)[0], 0)
            self.assertLessEqual({r["tool"] for r in env.stub.requests if r["tool"]}, ALLOWED_TOOLS)
            self.assertEqual(env.sim.forbidden_attempts, [])
            self.assertEqual(len(env.official_writes(ORDER)), 1)
            self.assertEqual(len(env.official_writes(CLAIM)), 1)


if __name__ == "__main__":
    unittest.main()
