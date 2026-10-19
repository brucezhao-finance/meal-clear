"""端到端测试共用夹具：指向回环模拟服务的客户端与 CLI 运行器（全部合成数据）。"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear.cli import main  # noqa: E402
from meal_clear.mcp import READ_ONLY, McpClient  # noqa: E402
from sim_mcp import ADDR_SENTINELS, COUPON_SENTINELS, SIM_TOKEN, SimMcpServer  # noqa: E402

SENTINELS = ADDR_SENTINELS + COUPON_SENTINELS + (
    "ADDR-SENTINEL-1-B", "ADDRID-1", "ADDRID-2", "COUPONID-1", "COUPONID-2",
    "MYID-1", "BASE64-SENTINEL", "SIM-TOKEN",
)


def sim_client(sim: SimMcpServer) -> McpClient:
    """已初始化、指向模拟服务的只读客户端（令牌仅来自合成环境变量）。"""
    with patch.dict(os.environ, {"MCD_MCP_TOKEN": SIM_TOKEN}):
        client = McpClient(endpoint=sim.url, retry_delay=0)
    client.initialize()
    return client


def run_cli(sim: SimMcpServer, *argv: str):
    """注入回环客户端运行 CLI，返回 (退出码, 标准输出)。"""
    client = sim_client(sim)
    out = io.StringIO()
    with patch("meal_clear.cli.client_from_args", return_value=client):
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
    return code, out.getvalue()


def request(people=None, fulfillment=None, store="10001", budget=20000, **extra):
    """构造 v2 实时请求。"""
    req = {
        "schema_version": 2,
        "context": {"store_code": store, "meal_time": "now"},
        "budget_cents": budget,
        "people": people or [{"id": "A", "hard": {"non_spicy": True}, "prefer": {}}],
    }
    if fulfillment:
        req["fulfillment"] = fulfillment
    req.update(extra)
    return req


def write_json(directory: Path, name: str, value) -> str:
    """写入临时 JSON 并返回路径。"""
    path = directory / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


def only_read_tools(sim: SimMcpServer) -> bool:
    """服务端记录的调用全部属于只读白名单，且没有写尝试。"""
    return not sim.write_attempts and {c["tool"] for c in sim.calls} <= READ_ONLY
