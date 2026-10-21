"""官方通道打桩夹具：官方端点 URL 的客户端 + 转发到回环模拟服务的 StubOpener（全部合成数据，零联网）。

走真实 CLI 入口：cli.client_from_args 不被替换，McpClient 的 opener 被换成 StubOpener，
它记录“发往官方 URL 的请求”后转发给回环 SimMcpServer；urllib.request.urlopen 被换成失败桩。
"""
import json
import os
import urllib.error
import urllib.request
from unittest.mock import patch

import netblock
from meal_clear import cli, ledger as ledger_mod, orders, realwrite
from meal_clear.config import CONFIRM_WORDS, TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER
from meal_clear.mcp import ENDPOINT, McpClient
from order_support import OrderEnv
from real_write_support import FakeTTY
from sim_mcp import SIM_TOKEN, SimMcpServer

CLAIM, ORDER = TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER


class StubOpener:
    """记录 (url, 是否带 Authorization, 方法, 工具) 后转发到回环 sim；可注入 302/超时/丢回包。"""

    def __init__(self, sim, real_build):
        self.sim = sim
        self._real = real_build()
        self.requests = []
        self.faults = {}

    def fault(self, tool, kind):
        """kind: redirect（302）| timeout（未转发即超时）| lost（已转发但回包丢失）。"""
        self.faults[tool] = kind

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        params = body.get("params") or {}
        tool = params.get("name") if body.get("method") == "tools/call" else None
        self.requests.append({
            "url": req.full_url,
            "auth": req.get_header("Authorization") is not None,
            "method": body.get("method"),
            "tool": tool,
            "args": params.get("arguments") if tool else None,
        })
        kind = self.faults.get(tool) if tool else None
        if kind == "redirect":
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "http://127.0.0.1:1/x"}, None)
        if kind == "timeout":
            raise TimeoutError("stub timeout")
        forward = urllib.request.Request(self.sim.url, data=req.data, headers=dict(req.header_items()), method="POST")
        response = self._real.open(forward, timeout=timeout)
        if kind == "lost":
            response.read()
            response.close()
            raise TimeoutError("stub lost response")
        return response

    def writes(self):
        return [r for r in self.requests if r["tool"] in (CLAIM, ORDER)]


class OfficialEnv(OrderEnv):
    """with OfficialEnv(switch=True) as env：真实 CLI → 官方 URL 客户端 → StubOpener → 回环 sim。"""

    def __init__(self, take_way_count=1, switch=True):
        super().__init__(take_way_count)
        self.switch = switch
        self.stdin = FakeTTY("")
        self.stdout = FakeTTY("")
        self.init_calls = 0

    def __enter__(self):
        import contextlib
        import tempfile
        from pathlib import Path
        from order_support import T0

        self._now = T0
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "local-state"
        self.state.mkdir(mode=0o700)
        os.chmod(self.state, 0o700)
        self.root = self.state / "orders"
        self.sim = SimMcpServer()
        self.sim.__enter__()
        self.sim.set_take_way_count(self.take_way_count)
        real_build = urllib.request.build_opener
        self.stub = StubOpener(self.sim, real_build)
        original_init = McpClient.initialize

        def counting_init(client):
            self.init_calls += 1
            return original_init(client)

        env = {"MCD_MCP_TOKEN": SIM_TOKEN}
        stack = contextlib.ExitStack()
        self._stack = stack
        stack.enter_context(netblock.block_network())
        stack.enter_context(patch.dict(os.environ, env))
        os.environ.pop(realwrite.REAL_WRITE_ENV, None)
        if self.switch:
            os.environ[realwrite.REAL_WRITE_ENV] = "1"
        stack.enter_context(patch("urllib.request.build_opener", side_effect=lambda *a, **k: self.stub))
        self.urlopen_mock = stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("urlopen 不应被调用")))
        stack.enter_context(patch.object(McpClient, "initialize", counting_init))
        stack.enter_context(patch.object(cli, "ledger_root", return_value=self.root))
        stack.enter_context(patch.object(orders, "utcnow", side_effect=lambda: self._now))
        stack.enter_context(patch.object(ledger_mod, "_now", side_effect=lambda: self._now))
        stack.enter_context(patch.object(realwrite, "_io_streams", side_effect=lambda: (self.stdin, self.stdout)))
        return self

    # --- 打桩与终端 ---
    def mark(self):
        """记录当前请求数与 initialize 次数，之后用 since() 断言拒绝时零传输。"""
        self._mark = (len(self.stub.requests), self.init_calls)
        return self._mark

    def since(self):
        """自 mark() 以来的 (请求列表, initialize 次数)。"""
        return self.stub.requests[self._mark[0]:], self.init_calls - self._mark[1]

    def tty(self, *lines, tty=True):
        """设置下一次运行的终端输入；tty=False 模拟管道/重定向。"""
        text = "".join(line + "\n" for line in lines)
        self.stdin = FakeTTY(text, tty=tty)
        self.stdout = FakeTTY("", tty=tty)

    @staticmethod
    def phrase(digest, tool):
        return "%s %s" % (digest[-6:], CONFIRM_WORDS["claim" if tool == CLAIM else "order"])

    def write_config(self, **values):
        path = self.state / "real-write.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        os.chmod(path, 0o600)

    def official_claim(self, digest, phrase=None, **kw):
        self.tty(self.phrase(digest, CLAIM) if phrase is None else phrase, **kw)
        return self.claim(digest, execute=True)

    def official_place(self, digest, phrase=None, *extra, **kw):
        self.tty(self.phrase(digest, ORDER) if phrase is None else phrase, **kw)
        return self.place(digest, True, None, *extra)

    def official_ready(self, **req):
        """摘要→（如需）官方领券，返回可下单的 digest。"""
        code, out, data = self.summary(**req)
        assert code == 0, out
        digest = data["digest"]
        if data["claim"]["required"]:
            code, out = self.official_claim(digest)
            assert code == 0, out
        return digest

    def official_writes(self, tool=None):
        return [r for r in self.stub.writes() if tool is None or r["tool"] == tool]

    def ledger_text(self):
        return self.all_text()
