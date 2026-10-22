"""T-F3 / T-F4 / T-F5：受控写通道的权限分层、三重门与静态约束。"""
import argparse
import ast
import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear import cli, gate  # noqa: E402
from meal_clear.config import TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER  # noqa: E402
from meal_clear.gate import WriteDenied, WriteGate, WriteGrant  # noqa: E402
from meal_clear.ledger import Ledger  # noqa: E402
from meal_clear.live import BudgetedClient, CallCapError  # noqa: E402
from meal_clear.mcp import ENDPOINT, McpClient, McpError, WriteRefused  # noqa: E402
from netblock import block_network  # noqa: E402
from sim_mcp import FORBIDDEN_WRITE, SIM_TOKEN, SimMcpServer  # noqa: E402
from sim_support import sim_client  # noqa: E402

DIGEST = hashlib.sha256(b"gate").hexdigest()
FORBIDDEN = FORBIDDEN_WRITE + ("totally-unknown-tool", "")
NOW, LATER = 1000.0, 1600.0


def forged(tool: str) -> WriteGrant:
    """模拟拿到私有哨兵的攻击者（正常代码拿不到）。"""
    return WriteGrant(tool, gate._SENTINEL)


class GateBase(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(block_network())
        self.sim = stack.enter_context(SimMcpServer())
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        self.ledger = Ledger(Path(tmp) / "local-state" / "orders")
        self.addCleanup(self.ledger.release_all)
        self.ledger.create(DIGEST, basket_key="bk", created_at="2099-01-01T00:00:00+00:00",
                           expires_at="2099-01-01T00:10:00+00:00")
        self.client = sim_client(self.sim)

    def open_gate(self, **kw) -> WriteGate:
        args = dict(execute=True, confirm_digest=DIGEST, record_digest=DIGEST,
                    expires_at=LATER, now=NOW, ledger=self.ledger)
        args.update(kw)
        return WriteGate.open(self.client, **args)

    def attempt(self, step: str = "order"):
        return self.ledger.begin_step(DIGEST, step, ("none",))


class PermissionLayerTests(GateBase):
    def assert_no_writes(self) -> None:
        self.assertEqual(self.sim.write_attempts, [])
        self.assertEqual(self.sim.controlled_calls, [])

    def test_forbidden_tools_denied_on_all_four_paths(self) -> None:
        gate_ = self.open_gate()
        attempt = self.attempt()
        for tool in FORBIDDEN:
            with self.subTest(tool=tool):
                with self.assertRaises(McpError):
                    self.client.call(tool, {})
                with self.assertRaises(McpError):
                    self.client.call_write(tool, {}, grant=None)
                with self.assertRaises(McpError):
                    self.client.call_write(tool, {}, grant=forged(tool))  # 即使授权齐全
                with self.assertRaises(WriteDenied):
                    gate_.call(tool, {}, attempt)  # 门全开
                with self.assertRaises(McpError):
                    self.client._post("tools/call", {"name": tool, "arguments": {}})
                with self.assertRaises(McpError):
                    self.client._post("tools/call", {"name": tool, "arguments": {}}, attempts=3)
                self.assert_no_writes()
        for weird in (None, 5, ["create-order"], {"a": 1}):
            with self.subTest(weird=weird):
                with self.assertRaises(McpError):
                    self.client._post("tools/call", {"name": weird, "arguments": {}})
                with self.assertRaises(McpError):
                    self.client.call_write(weird, {}, grant=forged(TOOL_CREATE_ORDER))
                with self.assertRaises(WriteDenied):
                    gate_.call(weird, {}, attempt)
        with self.assertRaises(McpError):
            self.client._post("tools/call", None)
        self.assert_no_writes()

    def test_controlled_tools_rejected_by_call_and_post(self) -> None:
        for tool in (TOOL_CREATE_ORDER, TOOL_AUTO_BIND_COUPONS):
            with self.subTest(tool=tool):
                before = len(self.sim.request_log_methods)
                with self.assertRaises(McpError):
                    self.client.call(tool, {})
                with self.assertRaises(WriteRefused):
                    self.client._post("tools/call", {"name": tool, "arguments": {}})
                for grant in (None, "yes", object(), MagicMock(tool=tool)):
                    with self.assertRaises(WriteRefused):
                        self.client.call_write(tool, {}, grant=grant)
                with self.assertRaises(WriteRefused):  # 授权与工具不匹配
                    other = TOOL_AUTO_BIND_COUPONS if tool == TOOL_CREATE_ORDER else TOOL_CREATE_ORDER
                    self.client.call_write(tool, {}, grant=forged(other))
                self.assertEqual(len(self.sim.request_log_methods), before)
                self.assert_no_writes()
        with self.assertRaises(WriteDenied):  # 构造授权需要私有哨兵
            WriteGrant(TOOL_CREATE_ORDER, object())
        self.assertIsNone(self.client._write_ctx)

    def test_forged_grant_blocked_by_transport_loopback_check(self) -> None:
        """官方端点 / 被篡改的端点：即使授权伪造成功也在传输层被拦截，且零网络请求。"""
        with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
            official = McpClient()
        self.assertEqual(official.endpoint, ENDPOINT)
        official._opener = MagicMock()
        official._opener.open.side_effect = AssertionError("不应发出网络请求")
        for tool in (TOOL_CREATE_ORDER, TOOL_AUTO_BIND_COUPONS):
            with self.assertRaises(WriteRefused):
                official.call_write(tool, {}, grant=forged(tool))
        official._opener.open.assert_not_called()
        tampered = sim_client(self.sim)
        tampered._opener = MagicMock()
        for url in ("http://127.0.0.1.evil.com:80", "https://127.0.0.1:80", "http://localhost:80",
                    "http://[::ffff:127.0.0.1]:80"):
            tampered.endpoint = url
            with self.subTest(url=url), self.assertRaises(WriteRefused):
                tampered.call_write(TOOL_AUTO_BIND_COUPONS, {}, grant=forged(TOOL_AUTO_BIND_COUPONS))
        tampered._opener.open.assert_not_called()
        self.assert_no_writes()


class ThreeDoorTests(GateBase):
    def test_missing_execute(self) -> None:
        for flag in (False, None, 1, "yes"):
            with self.subTest(flag=flag), self.assertRaises(WriteDenied):
                self.open_gate(execute=flag)
        self.assertEqual(self.sim.controlled_calls, [])

    def test_missing_or_wrong_digest(self) -> None:
        other = hashlib.sha256(b"other").hexdigest()
        for confirm in ("", None, other, DIGEST[:12], DIGEST[:63], DIGEST.upper(), DIGEST + "0", "é" * 64):
            with self.subTest(confirm=confirm), self.assertRaises(WriteDenied):
                self.open_gate(confirm_digest=confirm)
        with self.assertRaises(WriteDenied):  # 记录侧为空串也不能"空等于空"
            self.open_gate(confirm_digest="", record_digest="")
        self.assertEqual(self.sim.controlled_calls, [])

    def test_expired(self) -> None:
        for expires, now in ((NOW, NOW), (NOW - 1, NOW), (None, NOW), ("x", NOW)):
            with self.subTest(expires=expires), self.assertRaises(WriteDenied):
                self.open_gate(expires_at=expires, now=now)

    def test_expiry_rechecked_on_each_call(self) -> None:
        class Clock:
            value = NOW

            def __lt__(self, other):
                return self.value < other

            def __gt__(self, other):
                return self.value > other

        clock = Clock()
        gate_ = self.open_gate(now=clock)
        attempt = self.attempt("claim")
        clock.value = LATER + 1
        with self.assertRaises(WriteDenied):
            gate_.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
        self.assertEqual(self.sim.controlled_calls, [])

    def test_official_endpoint_refused_before_token_initialize_or_urlopen(self) -> None:
        boom = AssertionError("不应被调用")
        args = argparse.Namespace(token_file="/nonexistent/should-not-be-read")
        with patch.object(cli, "McpClient", side_effect=boom) as ctor, patch(
            "urllib.request.urlopen", side_effect=boom
        ) as urlopen, patch("urllib.request.OpenerDirector.open", side_effect=boom) as opener, patch.object(
            McpClient, "initialize", side_effect=boom
        ) as init, patch.object(Path, "read_text", side_effect=boom) as reader:
            with self.assertRaises(WriteDenied) as caught:
                cli.client_from_args(args, write=True)
        for mocked in (ctor, urlopen, opener, init, reader):
            mocked.assert_not_called()
        self.assertIn("真实领券/下单默认关闭且未经真实验证；如需启用，请阅读 docs/真实写操作启用指南.md。本次未发出任何请求。", str(caught.exception))

    def test_cli_maps_write_denied_to_exit_4(self) -> None:
        with patch.object(cli, "client_from_args", side_effect=WriteDenied("拒绝")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["stores", "--city", "a", "--keyword", "b"])
        self.assertEqual(code, 4)
        self.assertIn("拒绝", out.getvalue())

    def test_read_mode_client_from_args_unchanged(self) -> None:
        with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}), patch.object(
            McpClient, "initialize", return_value={}
        ):
            client = cli.client_from_args(argparse.Namespace(token_file=None))
        self.assertEqual(client.endpoint, ENDPOINT)

    def test_success_path_exactly_one_write(self) -> None:
        gate_ = self.open_gate()
        attempt = self.attempt("claim")
        result = gate_.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.sim.claims, 1)
        self.assertEqual([c["tool"] for c in self.sim.controlled_calls], [TOOL_AUTO_BIND_COUPONS])
        self.assertEqual(self.sim.forbidden_attempts, [])
        self.assertIsNone(self.client._write_ctx)
        # 句柄用完即失效（先落账、一次机会）
        self.ledger.finish_step(DIGEST, "claim", attempt, "succeeded", {"code": 200})
        with self.assertRaises(WriteDenied):
            gate_.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
        self.assertEqual(self.sim.claims, 1)

    def test_attempt_handle_required(self) -> None:
        gate_ = self.open_gate()
        other_digest = hashlib.sha256(b"other").hexdigest()
        self.ledger.create(other_digest, basket_key="b2", created_at="2099-01-01T00:00:00+00:00",
                           expires_at="2099-01-01T00:10:00+00:00")
        foreign = self.ledger.begin_step(other_digest, "claim", ("none",))
        for attempt in (None, "", "abc123", object(), foreign):
            with self.subTest(attempt=attempt), self.assertRaises(WriteDenied):
                gate_.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
        self.assertEqual(self.sim.controlled_calls, [])

    def test_budget_counts_writes(self) -> None:
        budget = BudgetedClient(self.client, max_calls=1)
        gate_ = WriteGate.open(budget, execute=True, confirm_digest=DIGEST, record_digest=DIGEST,
                               expires_at=LATER, now=NOW, ledger=self.ledger)
        claim = self.attempt("claim")
        gate_.call(TOOL_AUTO_BIND_COUPONS, {}, claim)
        self.ledger.finish_step(DIGEST, "claim", claim, "succeeded", {"code": 200})
        self.assertEqual(budget.calls, 1)
        attempt2 = self.attempt("order")
        with self.assertRaises(CallCapError):
            gate_.call(TOOL_CREATE_ORDER, {}, attempt2)
        self.assertEqual(len(self.sim.controlled_calls), 1)


class StaticTests(unittest.TestCase):
    """T-F5：AST 静态约束。"""

    def modules(self):
        for path in sorted((ROOT / "scripts" / "meal_clear").glob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"))

    def test_call_write_only_in_gate_and_mcp(self) -> None:
        for path, tree in self.modules():
            if path.name in ("gate.py", "mcp.py"):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotEqual(node.func.attr, "call_write", path.name)

    def test_write_tool_names_only_in_config(self) -> None:
        names = {"create-order", "auto-bind-coupons"}
        for path, tree in self.modules():
            if path.name == "config.py":
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value, names, path.name)

    def test_config_exports_constants(self) -> None:
        self.assertEqual(TOOL_CREATE_ORDER, "create-order")
        self.assertEqual(TOOL_AUTO_BIND_COUPONS, "auto-bind-coupons")


if __name__ == "__main__":
    unittest.main()
