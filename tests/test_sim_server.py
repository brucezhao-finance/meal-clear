"""模拟 MCP 服务器、回环端点、限流与写工具拒绝（全部本机合成）。"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from meal_clear import mcp  # noqa: E402
from meal_clear.mcp import (  # noqa: E402
    ENDPOINT,
    READ_ONLY,
    McpClient,
    McpError,
    RateLimitedError,
    RateLimiter,
)
from sim_mcp import ADDR_SENTINELS, SIM_TOKEN, WRITE_TOOLS, SimMcpServer  # noqa: E402


def make(sim: SimMcpServer, **kw) -> McpClient:
    """指向模拟服务的客户端（令牌仅来自合成环境变量）。"""
    with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
        return McpClient(endpoint=sim.url, retry_delay=0, **kw)


class FakeClock:
    """可控时钟。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class SimServerTests(unittest.TestCase):
    """T-A9、T-G1~G6 与端点校验。"""

    def test_full_flow_json_and_sse(self) -> None:
        """initialize→tools/list→tools/call 在 JSON 与 SSE 下都走通，且无写尝试。"""
        for sse in (False, True):
            with SimMcpServer(use_sse=sse) as sim:
                c = make(sim)
                self.assertEqual(c.initialize(), {"name": "sim-mcp"})
                names = {t["name"] for t in c.list_tools()["tools"]}
                self.assertTrue(set(WRITE_TOOLS) <= names)
                data = c.call("query-nearby-stores", {"beType": 1, "searchType": 2})
                self.assertEqual(data["data"][0]["storeCode"], "10001")
                self.assertEqual(sim.write_attempts, [])
                self.assertTrue({x["tool"] for x in sim.calls} <= READ_ONLY)
                self.assertIn("notifications/initialized", sim.request_log_methods)

    def test_bad_token_401_no_retry(self) -> None:
        """错误令牌 401：不重试，消息不含令牌。"""
        with SimMcpServer() as sim:
            with patch.dict(os.environ, {"MCD_MCP_TOKEN": "WRONG-TOKEN-0123456789-ABCDEFG"}):
                c = McpClient(endpoint=sim.url, retry_delay=0)
            with self.assertRaises(McpError) as caught:
                c.initialize()
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("WRONG-TOKEN", str(caught.exception))
            self.assertEqual(sim.request_log_methods, [])

    def test_G1_tool_401_not_retried(self) -> None:
        """工具级 401 只请求一次。"""
        with SimMcpServer() as sim:
            c = make(sim)
            sim.fail("now-time-info", "http401")
            with self.assertRaises(McpError):
                c.call("now-time-info", {})
            self.assertEqual(len(sim.calls), 1)

    def test_G2_429_retry_then_success(self) -> None:
        """429 先退避后成功。"""
        with SimMcpServer() as sim:
            c = make(sim)
            sim.fail("now-time-info", "http429:2")
            self.assertIn("now", c.call("now-time-info", {})["data"])
            self.assertEqual(len(sim.calls), 3)

    def test_G3_429_exhausted(self) -> None:
        """429 耗尽抛 RateLimitedError，文案不变，仍是 McpError。"""
        with SimMcpServer() as sim:
            c = make(sim)
            sim.fail("now-time-info", "http429:9")
            with self.assertRaises(RateLimitedError) as caught:
                c.call("now-time-info", {})
            self.assertEqual(str(caught.exception), "HTTP 429：只读请求失败")
            self.assertIsInstance(caught.exception, McpError)
            self.assertEqual(len(sim.calls), 3)

    def test_G4_error_modes_are_sanitized(self) -> None:
        """isError、业务码、坏结构、非 JSON 均抛 McpError 且不泄露原始回包。"""
        for mode in ("tool_error", "business_code", "bad_structure", "garbage_json"):
            with SimMcpServer() as sim:
                c = make(sim)
                sim.fail("now-time-info", mode)
                with self.subTest(mode=mode), self.assertRaises(McpError) as caught:
                    c.call("now-time-info", {})
                text = str(caught.exception)
                for leaked in ("SIM-SECRET", "not-a-list", "not json", SIM_TOKEN):
                    self.assertNotIn(leaked, text)
                self.assertEqual(c.audit[-1], {"tool": "now-time-info", "success": False})

    def test_G5_server_enforces_parameter_rules(self) -> None:
        """服务端像真实接口一样拒绝非法参数组合。"""
        with SimMcpServer() as sim:
            c = make(sim)
            bad = [
                ("query-meals", {"storeCode": "1", "orderType": 1, "beType": 2}),
                ("query-meals", {"storeCode": "1", "orderType": 1, "beType": 5}),
                ("query-meals", {"storeCode": "1", "orderType": 2, "beType": 2}),
                ("query-meals", {"storeCode": "1", "orderType": 1, "beType": 1, "beCode": "X"}),
                ("delivery-query-stores", {"beType": 2}),
                ("calculate-price", {"storeCode": "1", "orderType": 1, "beType": 1}),
            ]
            for tool, args in bad:
                with self.subTest(tool=tool, args=args), self.assertRaises(McpError):
                    c.call(tool, args)
            ok = c.call("query-meals", {"storeCode": "1", "orderType": 2, "beType": 2, "beCode": "B"})
            self.assertIn("meals", ok["data"])
            stores = c.call("delivery-query-stores", {"beType": 2, "addressId": "ADDRID-1"})
            self.assertEqual(stores["data"][0]["beCode"], "BE30001")
            addresses = c.call("delivery-query-addresses", {})
            self.assertIn(ADDR_SENTINELS[0], str(addresses))  # 服务端确有哨兵，供隐私测试使用

    def test_calculate_price_rules(self) -> None:
        """核价按合成规则：外送加运费，带券减免，单位为分。"""
        with SimMcpServer() as sim:
            c = make(sim)
            item = {"productCode": "M1", "quantity": 1}
            dine = c.call("calculate-price", {"storeCode": "1", "orderType": 1, "beType": 1,
                                              "items": [item], "needTableware": False})["data"]
            self.assertEqual(dine["price"], 2500)
            delivery = c.call("calculate-price", {"storeCode": "1", "orderType": 2, "beType": 2,
                                                  "beCode": "B", "items": [item]})["data"]
            self.assertEqual(delivery["price"], 2500 + 600 + 100)
            coupon = c.call("calculate-price", {"storeCode": "1", "orderType": 1, "beType": 1,
                                                "items": [dict(item, couponId="C", couponCode="K")]})["data"]
            self.assertEqual(coupon["price"], 2000)

    def test_G6_write_tools_rejected_before_network(self) -> None:
        """四个写工具被客户端白名单拒绝，服务端 calls 不增长、无写尝试。"""
        with SimMcpServer() as sim:
            c = make(sim)
            c.initialize()
            before = len(sim.calls)
            methods = len(sim.request_log_methods)
            for tool in WRITE_TOOLS:
                self.assertNotIn(tool, READ_ONLY)
                with self.assertRaises(McpError):
                    c.call(tool, {})
            self.assertEqual(len(sim.calls), before)
            self.assertEqual(len(sim.request_log_methods), methods)
            self.assertEqual(sim.write_attempts, [])

    def test_endpoint_validation(self) -> None:
        """只接受回环端点；默认仍为官方 ENDPOINT。"""
        with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
            self.assertEqual(McpClient().endpoint, ENDPOINT)
            self.assertEqual(ENDPOINT, "https://mcp.mcd.cn")
            McpClient(endpoint="http://127.0.0.1:8080")
            McpClient(endpoint="http://localhost:9")
            for bad in (
                "https://127.0.0.1:8080",
                "http://example.com:80",
                "http://127.0.0.1",
                "http://127.0.0.1:8080/path",
                "http://user@127.0.0.1:8080",
                "http://127.0.0.1.evil.com:80",
                "http://127.0.0.1:99999",
                "",
            ):
                with self.subTest(bad=bad), self.assertRaises(McpError):
                    McpClient(endpoint=bad)

    def test_A9_rate_limiter_with_fake_clock(self) -> None:
        """第 501 次调用前触发等待；窗口过后恢复。"""
        clock = FakeClock()
        limiter = RateLimiter(500, 60.0, clock=clock, sleep=clock.sleep)
        for _ in range(500):
            limiter.acquire()
        self.assertEqual(clock.slept, [])
        limiter.acquire()
        self.assertEqual(len(clock.slept), 1)
        self.assertGreaterEqual(clock.now, 60.0)
        limiter.acquire()
        self.assertEqual(len(clock.slept), 1)

    def test_rate_limiter_wired_into_client(self) -> None:
        """McpClient 每次发送前都占用限流名额。"""
        with SimMcpServer() as sim:
            clock = FakeClock()
            c = make(sim, rate_limiter=RateLimiter(2, 60.0, clock=clock, sleep=clock.sleep))
            c.call("now-time-info", {})
            c.call("now-time-info", {})
            self.assertEqual(clock.slept, [])
            c.call("now-time-info", {})
            self.assertEqual(len(clock.slept), 1)

    def test_readonly_whitelist_extended(self) -> None:
        """白名单为 11 个只读工具，含新增 4 个。"""
        self.assertEqual(len(READ_ONLY), 11)
        for tool in ("available-coupons", "query-my-coupons",
                     "delivery-query-addresses", "delivery-query-stores"):
            self.assertIn(tool, READ_ONLY)
        self.assertIs(mcp.READ_ONLY, READ_ONLY)


if __name__ == "__main__":
    unittest.main()
