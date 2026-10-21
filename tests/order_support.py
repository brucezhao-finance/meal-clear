"""订单测试夹具：临时账本 + 回环模拟服务 + 可推进的固定时钟 + 禁止非回环连接（全部合成数据）。"""
import contextlib
import io
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import netblock
import sim_support
from meal_clear import cli, orders
from meal_clear.ledger import Ledger
from sim_mcp import SimMcpServer

REAL_CLIENT_FROM_ARGS = cli.client_from_args  # 在任何 patch 之前捕获，用于“官方端点拒绝”测试
T0 = datetime(2026, 10, 20, 3, 0, 0, tzinfo=timezone.utc)
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class OrderEnv:
    """上下文管理器：with OrderEnv() as env: code, out = env.run(...)。"""

    def __init__(self, take_way_count=1):
        self.take_way_count = take_way_count
        self._now = T0

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "local-state" / "orders"
        self.sim = SimMcpServer()
        self.sim.__enter__()
        self.sim.set_take_way_count(self.take_way_count)
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(netblock.block_network())
        self._stack.enter_context(
            patch.object(cli, "client_from_args", side_effect=lambda args, write=False: sim_support.sim_client(self.sim))
        )
        self._stack.enter_context(patch.object(cli, "ledger_root", return_value=self.root))
        self._stack.enter_context(patch.object(orders, "utcnow", side_effect=lambda: self._now))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        self.sim.__exit__(*exc)
        self._tmp.cleanup()

    # --- 时钟 ---
    def advance(self, seconds):
        """推进固定时钟。"""
        self._now = self._now + timedelta(seconds=seconds)

    @property
    def now(self):
        return self._now

    # --- 运行 ---
    def run(self, *argv):
        """运行 CLI，返回 (退出码, 标准输出)。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def request_path(self, **kwargs):
        """写入 v2 实时请求并返回路径。"""
        return sim_support.write_json(self.dir, "req-%d.json" % len(list(self.dir.glob("req-*.json"))),
                                      sim_support.request(**kwargs))

    def summary(self, *extra, **req):
        """生成摘要（--json），返回 (退出码, 输出文本, 摘要对象或 None)。"""
        path = self.request_path(**req)
        code, out = self.run("order-summary", "--request", path, "--json", *extra)
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        return code, out, data

    @property
    def ledger(self):
        return Ledger(self.root)

    def record(self, digest):
        return self.ledger.read(digest)

    def claim(self, digest, execute=False, confirm=None, *extra):
        """order-claim；execute 时默认用完整 digest 确认。"""
        argv = ["order-claim", "--digest", digest[:12], "--json"]
        if execute:
            argv += ["--execute", "--confirm-digest", confirm or digest]
        return self.run(*argv, *extra)

    def place(self, digest, execute=False, confirm=None, *extra, json_out=True):
        """order-place；execute 时默认用完整 digest 确认。"""
        argv = ["order-place", "--digest", digest[:12]] + (["--json"] if json_out else [])
        if execute:
            argv += ["--execute", "--confirm-digest", confirm or digest]
        return self.run(*argv, *extra)

    def status(self, *argv):
        """order-status（--json）。"""
        return self.run("order-status", *argv, "--json")

    def ready(self, **req):
        """生成摘要并（如需要）领券，返回可下单的完整 digest。"""
        code, out, data = self.summary(**req)
        assert code == 0, out
        digest = data["digest"]
        if data["claim"]["required"]:
            code, out = self.claim(digest, execute=True)
            assert code == 0, out
        return digest

    def create_calls(self):
        """服务端收到的 create-order 调用（参数副本）。"""
        return [c for c in self.sim.controlled_calls if c["tool"] == "create-order"]

    def all_text(self):
        """账本目录下全部文件内容（用于隐私扫描）。"""
        return "\n".join(p.read_text(encoding="utf-8") for p in sorted(self.root.glob("*.json")))


def delivery_request():
    """外送请求参数（地址1，门店 30001）。"""
    return {"store": "30001", "fulfillment": {"type": "delivery", "be_code": "BE30001", "address_index": 1}}


def drive_thru_request():
    """得来速请求参数（店 20001）。"""
    return {"store": "20001", "fulfillment": {"type": "drive_thru", "be_code": "BE20001"}}
