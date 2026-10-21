"""T-F1 / T-F2：回环严格校验、重定向不跟随、无端点覆盖入口。"""
import contextlib
import io
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear import net_guard  # noqa: E402
from meal_clear.cli import main  # noqa: E402
from meal_clear.mcp import ENDPOINT, McpClient, McpError  # noqa: E402
from netblock import NetBlocked, block_network  # noqa: E402
from sim_mcp import SIM_TOKEN, SimMcpServer  # noqa: E402

BAD = (
    "http://127.0.0.1.evil.com:80", "http://127.1:80", "http://2130706433:80",
    "http://0x7f000001:80", "http://0177.0.0.1:80", "http://0.0.0.0:80",
    "http://[::ffff:8.8.8.8]:80", "http://user@127.0.0.1:80", "http://127.0.0.1:80/path",
    "http://127.0.0.1:80/", "http://127.0.0.1:80?x=1", "http://127.0.0.1:80#f",
    "https://127.0.0.1:80", "http://127.0.0.1", "http://127.0.0.1:0",
    "http://127.0.0.1:65536", "http://127.0.0.1.:80", "http://localhost.:80",
    "http://127.0.0.1 :80", "http://127.0.0.1%2e:80", "http://127.0.0.%31:80",
    "http://example.com:80", "http://8.8.8.8:80", "http://[::2]:80", "", None, 5,
    "http://127.0.0.1:80\n", "HTTP://127.0.0.1:80",
)


class NetGuardTests(unittest.TestCase):
    def test_accepts_loopback_literals(self) -> None:
        for url in ("http://127.0.0.1:8080", "http://[::1]:8080", "http://127.0.0.1:1",
                    "http://127.0.0.1:65535"):
            with self.subTest(url=url):
                net_guard.validate_loopback_url(url)
                net_guard.assert_write_target(url)

    def test_rejects_tricky_forms(self) -> None:
        for url in BAD:
            with self.subTest(url=url):
                with self.assertRaises(net_guard.NetGuardError):
                    net_guard.validate_loopback_url(url)
                with self.assertRaises(net_guard.NetGuardError):
                    net_guard.assert_write_target(url)

    def test_hostnames_never_resolved(self) -> None:
        """非 localhost 主机名直接拒绝，不做 DNS 查询。"""
        with patch("socket.getaddrinfo", side_effect=AssertionError("不应做 DNS 查询")):
            for url in ("http://127.0.0.1.evil.com:80", "http://evil.com:80", "http://localhost.:80"):
                with self.subTest(url=url), self.assertRaises(net_guard.NetGuardError):
                    net_guard.validate_loopback_url(url)

    def test_localhost_read_only_and_all_loopback(self) -> None:
        net_guard.validate_loopback_url("http://LocalHost:9")
        with self.assertRaises(net_guard.NetGuardError):
            net_guard.assert_write_target("http://localhost:9")
        bad = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 9))]
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9))] + bad
        for infos in (bad, mixed, []):
            with self.subTest(infos=infos), patch("socket.getaddrinfo", return_value=infos):
                with self.assertRaises(net_guard.NetGuardError):
                    net_guard.validate_loopback_url("http://localhost:9")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaises(net_guard.NetGuardError):
                net_guard.validate_loopback_url("http://localhost:9")

    def test_write_only_127_and_ipv6_literal(self) -> None:
        net_guard.validate_loopback_url("http://127.0.0.2:80")  # 读路径：整个 127/8 回环
        with self.assertRaises(net_guard.NetGuardError):
            net_guard.assert_write_target("http://127.0.0.2:80")
        for url in ("http://[::ffff:127.0.0.1]:80", "http://[0:0:0:0:0:0:0:1]:80"):
            with self.subTest(url=url), self.assertRaises(net_guard.NetGuardError):
                net_guard.assert_write_target(url)

    def test_client_validation_uses_guard(self) -> None:
        with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
            self.assertEqual(McpClient(endpoint="http://[::1]:9").endpoint, "http://[::1]:9")
            for url in BAD:
                if not isinstance(url, str):
                    continue
                with self.subTest(url=url), self.assertRaises(McpError):
                    McpClient(endpoint=url)

    # --- T-F2 ---
    def test_redirect_not_followed(self) -> None:
        with block_network(), SimMcpServer() as sim:
            with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
                client = McpClient(endpoint=sim.url, retry_delay=0)
            client.initialize()
            sim.fail("now-time-info", "redirect")
            with self.assertRaises(McpError):
                client.call("now-time-info", {})
            self.assertEqual(len(sim.calls), 1)

    def test_no_endpoint_override(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scripts/meal_clear/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("--endpoint", source)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["stores", "--city", "a", "--keyword", "b", "--endpoint", "http://127.0.0.1:1"])
        env = {"MCD_MCP_TOKEN": SIM_TOKEN, "MCD_MCP_ENDPOINT": "http://127.0.0.1:1",
               "MCD_ENDPOINT": "http://127.0.0.1:1", "ENDPOINT": "http://127.0.0.1:1",
               "MCP_ENDPOINT": "http://127.0.0.1:1"}
        with patch.dict(os.environ, env):
            self.assertEqual(McpClient().endpoint, ENDPOINT)
        self.assertEqual(ENDPOINT, "https://mcp.mcd.cn")

    def test_netblock_blocks_non_loopback(self) -> None:
        with block_network():
            with self.assertRaises(NetBlocked):
                socket.create_connection(("8.8.8.8", 80), timeout=1)
            s = socket.socket()
            try:
                with self.assertRaises(NetBlocked):
                    s.connect(("8.8.8.8", 80))
                with self.assertRaises(NetBlocked):
                    s.connect_ex(("1.1.1.1", 80))
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
