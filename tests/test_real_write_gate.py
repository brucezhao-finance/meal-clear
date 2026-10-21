"""P2 第四道门（官方路径，打桩传输）：每道门一个用例，拒绝时桩零传输调用、initialize/urlopen 未被调用。

全部走真实 CLI 入口（cli.main → orders → gate → mcp），官方 URL 客户端 + StubOpener，零联网、合成 Token。
"""
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from meal_clear import cli, gate, net_guard, realwrite  # noqa: E402
from meal_clear.config import CHANNEL_OFFICIAL, EXIT_LIMIT, EXIT_LOCKED  # noqa: E402
from meal_clear.mcp import McpClient, McpError, WriteRefused  # noqa: E402
from official_support import CLAIM, ORDER, OfficialEnv  # noqa: E402
from order_support import OrderEnv  # noqa: E402
from real_write_support import digest_of  # noqa: E402
from sim_mcp import FORBIDDEN_WRITE  # noqa: E402


def seed_official(env, n, step="order", end=None, basket="seed-basket"):
    """直接在账本里放一条 official 通道的写记录（started_at 取环境固定时钟）。"""
    ledger = env.ledger
    digest = digest_of(n)
    ledger.create(digest, basket_key=basket, created_at=env.now.isoformat(),
                  expires_at=(env.now + timedelta(minutes=10)).isoformat())
    attempt = ledger.begin_step(digest, step, ("none",), channel=CHANNEL_OFFICIAL)
    if end:
        ledger.finish_step(digest, step, attempt, end)
    ledger.release_all()
    return digest


class Base(unittest.TestCase):
    def assert_no_traffic(self, env):
        """自 env.mark() 以来：桩零请求、initialize 未调用、urlopen 未调用、账本无新的 official 步骤。"""
        requests, inits = env.since()
        self.assertEqual(requests, [])
        self.assertEqual(inits, 0)
        env.urlopen_mock.assert_not_called()

    def official_steps(self, env, step):
        return [r["digest"] for r in env.ledger._all_records()
                if env.ledger.step_channel(r["steps"][step]) == CHANNEL_OFFICIAL and r["steps"][step]["state"] != "none"]

    def no_write_requests(self, env):
        self.assertEqual(env.official_writes(), [])
        self.assertEqual(env.sim.controlled_calls, [])


class SwitchTests(Base):
    def test_gate_switch_default_off_claim_and_place(self):
        with OfficialEnv(switch=False) as env:
            code, out, data = env.summary()
            self.assertEqual(code, 0, out)
            digest = data["digest"]
            env.mark()
            env.tty(env.phrase(digest, CLAIM))
            code, out = env.claim(digest, execute=True)
            body = json.loads(out)
            self.assertEqual((code, body["reason"]), (4, "switch_off"))
            self.assertEqual(body["message"], gate.MSG_NOT_OPEN)  # 与既有文案逐字一致
            self.assert_no_traffic(env)
            code, out, skip = env.summary("--skip-claim")  # 无需领券的摘要，才会走到第四道门
            self.assertEqual(code, 0, out)
            env.mark()
            env.tty(env.phrase(skip["digest"], ORDER))
            code, out = env.place(skip["digest"], execute=True)
            self.assertEqual((code, json.loads(out)["message"]), (4, gate.MSG_NOT_OPEN))
            self.assert_no_traffic(env)
            self.assertEqual(self.official_steps(env, "claim") + self.official_steps(env, "order"), [])

    def test_env_value_must_be_exactly_one(self):
        for value in ("true", "0", "", " 1", "yes"):
            with self.subTest(value=value), OfficialEnv(switch=False) as env:
                code, out, data = env.summary()
                os.environ[realwrite.REAL_WRITE_ENV] = value
                env.mark()
                env.tty(env.phrase(data["digest"], CLAIM))
                code, out = env.claim(data["digest"], execute=True)
                self.assertEqual(code, 4, out)
                self.assert_no_traffic(env)

    def test_config_file_enables_without_env(self):
        with OfficialEnv(switch=False) as env:
            env.write_config(enabled=True)
            code, out, data = env.summary()
            self.assertEqual(env.official_claim(data["digest"])[0], 0)

    def test_invalid_config_refused_even_with_env_on(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            env.write_config(enabled=True, max_order_cents=10**9)  # 超硬上限：拒绝而非截断
            env.mark()
            code, out = env.official_claim(data["digest"])
            self.assertEqual((code, json.loads(out)["reason"]), (4, "config_invalid"))
            self.assert_no_traffic(env)


class LockAndTtyTests(Base):
    def test_gate_lock_file_all_shapes(self):
        for shape in ("file", "symlink", "dir"):
            with self.subTest(shape=shape), OfficialEnv() as env:
                code, out, data = env.summary()
                lock = env.state / "WRITE_LOCK"
                if shape == "file":
                    lock.write_text("")
                elif shape == "symlink":
                    lock.symlink_to(env.dir / "missing-target")
                else:
                    lock.mkdir()
                env.mark()
                code, out = env.official_claim(data["digest"])
                self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LOCKED, "locked"), out)
                self.assert_no_traffic(env)

    def test_gate_tty_stdin_stdout_pipe_refused(self):
        for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin_tty, stdout=stdout_tty), OfficialEnv() as env:
                code, out, data = env.summary()
                digest = data["digest"]
                env.mark()
                env.tty(env.phrase(digest, CLAIM))  # 即使内容正确
                env.stdin._tty, env.stdout._tty = stdin_tty, stdout_tty
                code, out = env.claim(digest, execute=True)
                self.assertEqual((code, json.loads(out)["reason"]), (4, "not_tty"), out)
                self.assert_no_traffic(env)

    def test_wrong_phrase_no_retry(self):
        wrong = ["", "000000 确认领券", " {tail} 确认领券", "{tail}  确认领券", "{tail} 确认下单", "{tail}确认领券", "确认领券"]
        for template in wrong:
            with self.subTest(phrase=template), OfficialEnv() as env:
                code, out, data = env.summary()
                digest = data["digest"]
                phrase = template.format(tail=digest[-6:])
                env.mark()
                code, out = env.official_claim(digest, phrase=phrase)
                self.assertEqual((code, json.loads(out)["reason"]), (4, "phrase_mismatch"), out)
                # 摘要核价读已发生，但没有任何写请求，账本无 official 步骤
                self.assertEqual(env.official_writes(), [])
                self.assertEqual(self.official_steps(env, "claim"), [])

    def test_token_file_arg_rejected_for_official_write(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            bad = env.dir / "any-token-file"
            bad.write_text("X" * 40)
            env.mark()
            env.tty(env.phrase(data["digest"], CLAIM))
            code, out = env.claim(data["digest"], True, None, "--token-file", str(bad))
            self.assertEqual((code, json.loads(out)["reason"]), (4, "token_source"), out)
            self.assert_no_traffic(env)


class DigestTtlTests(Base):
    def test_gate_digest_wrong_and_partial_prefix(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            digest = data["digest"]
            for confirm in ("0" * 64, digest[:12], digest[:63], digest.upper()):
                with self.subTest(confirm=confirm[:12]):
                    env.mark()
                    env.tty(env.phrase(digest, CLAIM))
                    code, out = env.claim(digest, True, confirm)
                    self.assertEqual((code, json.loads(out)["message"]), (4, gate.MSG_DIGEST), out)
                    self.assert_no_traffic(env)
                    env.tty(env.phrase(digest, ORDER))
                    code, out = env.place(digest, True, confirm)
                    self.assertEqual(code, 4)
                    self.assert_no_traffic(env)

    def test_gate_ttl_expired_exit3(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            env.advance(601)
            env.mark()
            code, out = env.official_claim(data["digest"])
            self.assertEqual(code, 3, out)
            self.assert_no_traffic(env)
            code, out = env.official_place(data["digest"])
            self.assertEqual(code, 3, out)
            self.assert_no_traffic(env)

    def test_duplicate_is_exit5_before_fourth_gate(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            self.assertEqual(env.official_place(digest)[0], 0)
            env.mark()
            code, out = env.official_place(digest)
            self.assertEqual(code, 5, out)
            self.assert_no_traffic(env)
            code, out = env.official_claim(digest)
            self.assertEqual(code, 5, out)
            self.assert_no_traffic(env)


class CapTests(Base):
    def test_gate_amount_cap(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            env.write_config(max_order_cents=1000)  # 应付 ¥20.00 > ¥10.00
            code, out = env.official_place(digest)
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LIMIT, "amount_cap"), out)
            self.assertEqual(env.official_writes(ORDER), [])
            self.assertEqual(env.sim.orders, [])
            self.assertEqual(self.official_steps(env, "order"), [])
            self.assertEqual(env.stdin.tell(), 0)  # 超限先于确认：短语未被读取

    def test_gate_orders_cap_24h_and_window_rolls(self):
        with OfficialEnv() as env:
            env.write_config(max_orders_24h=1)
            seed_official(env, 1, "order", "created", basket="other")
            digest = env.official_ready()
            env.mark()
            code, out = env.official_place(digest)
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LIMIT, "orders_cap"), out)
            self.assert_no_traffic(env)
            env.advance(24 * 3600 + 60)  # 窗口滚动后旧记录不再计数（摘要需重新生成）
            digest = env.official_ready()
            code, out = env.official_place(digest)
            self.assertEqual(code, 0, out)

    def test_gate_claims_cap_24h(self):
        with OfficialEnv() as env:
            seed_official(env, 2, "claim", "succeeded", basket="other")
            code, out, data = env.summary()
            env.mark()
            code, out = env.official_claim(data["digest"])
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LIMIT, "claims_cap"), out)
            self.assert_no_traffic(env)

    def test_failed_does_not_count(self):
        with OfficialEnv() as env:
            seed_official(env, 3, "claim", "failed", basket="other")
            code, out, data = env.summary()
            self.assertEqual(env.official_claim(data["digest"])[0], 0)

    def test_gate_unknown_basket_lock(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            basket = env.record(data["digest"])["basket_key"]
            seed_official(env, 4, "order", "unknown", basket=basket)
            env.mark()
            code, out = env.official_claim(data["digest"])
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LOCKED, "unknown_lock"), out)
            self.assert_no_traffic(env)
            env.write_config(max_orders_24h=5)
            code, out, skip = env.summary("--skip-claim")
            env.mark()
            code, out = env.official_place(skip["digest"], None, "--allow-duplicate-basket")
            self.assertEqual((code, json.loads(out)["reason"]), (EXIT_LOCKED, "unknown_lock"), out)
            self.assert_no_traffic(env)


class TransportTests(Base):
    def test_official_endpoint_fixed_and_not_overridable(self):
        with OfficialEnv() as env:
            for bad in ("https://evil", "https://mcp.mcd.cn", "http://mcp.mcd.cn:80"):
                with self.assertRaises(McpError):
                    McpClient(endpoint=bad)  # 官方端点不能显式传入，其余只接受严格回环
            for url in ("https://mcp.mcd.cn.evil.com", "https://mcp.mcd.cn@evil.com", "http://mcp.mcd.cn",
                        "https://mcp.mcd.cn:443", "https://mcp.mcd.cn/", "HTTPS://MCP.MCD.CN",
                        "https://MCP.mcd.cn", " https://mcp.mcd.cn", None, b"https://mcp.mcd.cn"):
                self.assertFalse(net_guard.is_official_url(url), url)
                with self.assertRaises(net_guard.NetGuardError):
                    net_guard.assert_official_target(url)
            with self.assertRaises(SystemExit):  # CLI 没有 --endpoint
                with patch("sys.stderr"):
                    cli.main(["order-claim", "--digest", "0" * 12, "--endpoint", "http://x"])

    def test_redirect_not_followed_on_official_write(self):
        for tool, run in ((ORDER, "place"), (CLAIM, "claim")):
            with self.subTest(tool=tool), OfficialEnv() as env:
                if tool == ORDER:
                    digest = env.official_ready()
                    env.stub.fault(ORDER, "redirect")
                    code, out = env.official_place(digest)
                    step = "order"
                else:
                    code, out, data = env.summary()
                    digest = data["digest"]
                    env.stub.fault(CLAIM, "redirect")
                    code, out = env.official_claim(digest)
                    step = "claim"
                self.assertEqual(code, 6, out)
                self.assertEqual(len(env.official_writes(tool)), 1)  # 只发 1 次，不重试
                self.assertEqual({r["url"] for r in env.stub.requests}, {"https://mcp.mcd.cn"})  # 不访问 Location
                self.assertEqual(env.record(digest)["steps"][step]["state"], "unknown")

    def test_forged_grant_without_approval_blocked_on_official(self):
        with OfficialEnv() as env:
            client = McpClient()
            client.initialize()
            env.mark()
            for tool in (CLAIM, ORDER):
                with self.assertRaises(WriteRefused):
                    client.call_write(tool, {}, grant=gate.WriteGrant(tool, gate._SENTINEL))
                # 即便拿到授权哨兵并伪称官方通道，批准未被签发/消费也过不了
                fake = realwrite.RealWriteApproval(tool, digest_of(1), realwrite._SENTINEL)
                with self.assertRaises(WriteRefused):
                    client.call_write(tool, {}, grant=gate.WriteGrant(tool, gate._SENTINEL, fake, True))
                with self.assertRaises(WriteRefused):  # 传输层兜底：没有官方上下文
                    client._write_ctx = tool
                    try:
                        client._post("tools/call", {"name": tool, "arguments": {}})
                    finally:
                        client._write_ctx = None
            self.assertEqual(env.since()[0], [])

    def test_forged_approval_blocked(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            digest = data["digest"]
            client = McpClient()
            client.initialize()
            env.mark()
            with self.assertRaises(realwrite.RealWriteDenied):
                realwrite.RealWriteApproval(CLAIM, digest, object())
            ledger = env.ledger
            for approval in (None, "ok", object(), realwrite.RealWriteApproval(CLAIM, digest_of(9), realwrite._SENTINEL)):
                with self.assertRaises(gate.WriteDenied):  # 构造即被拒：批准缺失/类型错/绑定的摘要不符
                    gate.WriteGate.open(client, execute=True, confirm_digest=digest, record_digest=digest,
                                        expires_at=env.now + timedelta(minutes=5), now=env.now,
                                        ledger=ledger, approval=approval)
            self.assertEqual(env.since()[0], [])

    def test_approval_not_reusable_through_gate(self):
        with OfficialEnv() as env:
            digest = env.official_ready()
            client = McpClient()
            client.initialize()
            approval = realwrite.RealWriteApproval(ORDER, digest, realwrite._SENTINEL)
            guard = gate.WriteGate.open(client, execute=True, confirm_digest=digest, record_digest=digest,
                                        expires_at=env.now + timedelta(minutes=5), now=env.now,
                                        ledger=env.ledger, approval=approval)
            self.assertTrue(realwrite.consume_approval(approval, ORDER, digest))
            env.mark()
            with self.assertRaises(gate.WriteDenied):  # 已被消费：第二次调用被拒
                guard.call(ORDER, {}, None)
            self.assertEqual(env.since()[0], [])

    def test_other_write_tools_always_denied_on_official(self):
        with OfficialEnv() as env:
            code, out, data = env.summary()
            digest = data["digest"]
            client = McpClient()
            client.initialize()
            approval = realwrite.RealWriteApproval(CLAIM, digest, realwrite._SENTINEL)  # 即便批准齐全
            guard = gate.WriteGate.open(client, execute=True, confirm_digest=digest, record_digest=digest,
                                        expires_at=env.now + timedelta(minutes=5), now=env.now,
                                        ledger=env.ledger, approval=approval)
            env.mark()
            for tool in FORBIDDEN_WRITE + ("totally-unknown-tool", "", None, 7):
                with self.subTest(tool=tool):
                    with self.assertRaises(gate.WriteDenied):
                        guard.call(tool, {}, None)  # 第一层：WriteGate
                    with self.assertRaises(WriteRefused):
                        client.call_write(tool, {}, grant=gate.WriteGrant(CLAIM, gate._SENTINEL, approval, True))  # 第二层
                    client._write_ctx = tool
                    try:
                        with self.assertRaises(WriteRefused):
                            client._post("tools/call", {"name": tool, "arguments": {}})  # 第三层：传输层
                    finally:
                        client._write_ctx = None
                    if isinstance(tool, str):
                        with self.assertRaises(McpError):
                            client.call(tool, {})
            self.assertEqual(env.since()[0], [])


class LoopbackUnchangedTests(Base):
    def test_loopback_path_unchanged_with_switch_on(self):
        with patch.dict(os.environ, {realwrite.REAL_WRITE_ENV: "1"}), OrderEnv() as env:
            digest = env.ready()  # 非 TTY、无确认短语：回环模拟仍走 P1 行为
            code, out = env.place(digest, execute=True)
            self.assertEqual(code, 0, out)
            ledger = env.ledger
            self.assertEqual(ledger.step_channel(env.record(digest)["steps"]["order"]), "loopback")
            self.assertEqual(ledger.count_channel_writes("official", "order", ("pending", "created", "unknown"), 86400, env.now), 0)

    def test_official_request_args_equal_loopback_args(self):
        with OrderEnv() as lb:
            digest = lb.ready()
            self.assertEqual(lb.place(digest, execute=True)[0], 0)
            loopback_args = lb.create_calls()[0]["args"]
        with OfficialEnv() as env:
            digest = env.official_ready()
            self.assertEqual(env.official_place(digest)[0], 0)
            official_args = env.official_writes(ORDER)[0]["args"]
        self.assertEqual(official_args, loopback_args)


if __name__ == "__main__":
    unittest.main()
