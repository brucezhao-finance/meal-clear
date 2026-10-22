"""团餐 20 人与 50 人性能基准（不以 test_ 开头，不会被 discover 收集）。
用法：python3 tests/bench_group.py
输出 Markdown 表：离线引擎耗时（5 次中位数）、整单候选数、live-plan 在回环模拟服务上的
核价次数、调用总数、墙钟时间与限流等待情况。全部为合成数据，不联网。
"""
import copy
import platform
import statistics
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from meal_clear.live import MultiLivePlanner  # noqa: E402
from meal_clear.mcp import RateLimiter  # noqa: E402
from meal_clear.ranking import compose_v2, format_space  # noqa: E402
from scale_fixtures import group_request, synth_catalog  # noqa: E402
from sim_mcp import SimMcpServer  # noqa: E402
from sim_support import request as live_request  # noqa: E402
from sim_support import sim_client  # noqa: E402
REPEAT = 5
BE_CODE = "GBE-BENCH"
SCENARIOS = (
    ("G20", 20, ((8, "chicken"), (6, "beef"), (6, "pork"))),
    ("G50", 50, ((20, "chicken"), (15, "beef"), (10, "pork"), (5, "fish"))),
)
def group_catalog():
    """60 个合成 offer，每个补两个小食，上下文为团餐。"""
    catalog = synth_catalog(60)
    for offer in catalog["offers"]:
        oid = offer["id"]
        offer["items"] = [offer["items"][0]] + [
            {"code": f"{oid}-s{k}", "name": f"小食{oid}{k}", "quantity": 1, "role": "side"} for k in (1, 2)
        ] + [offer["items"][1]]
    catalog["context"] = {"store_code": "T1", "meal_time": "t", "fulfillment": "group",
                          "be_code": BE_CODE, "reservation_date": None}
    return catalog
def offline_request(shape):
    """离线团餐请求（总预算给足，人均预算档为 50 元以上）。"""
    req = group_request([(n, {}, {"main_meat": meat}) for n, meat in shape], budget=10**7)
    req["fulfillment"] = {"type": "group", "be_code": BE_CODE, "address_index": 1, "gm_service_code": "GM-BENCH"}
    return req
def median_ms(fn):
    """调用 fn 共 REPEAT 次，返回耗时中位数（毫秒）与最后一次的返回值。"""
    times = []
    value = None
    for _ in range(REPEAT):
        start = time.perf_counter()
        value = fn()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), value
def engine_table(catalog):
    """离线引擎表。"""
    rows = [
        "### 团餐离线引擎（默认参数：per_person_top_k=8，max_whole_candidates=200）",
        "",
        "| 场景 | 人数 | 同质组数 | 整单候选 | 搜索空间 | 引擎 compose_v2 (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for name, people, shape in SCENARIOS:
        req = offline_request(shape)
        ms, result = median_ms(lambda: compose_v2(req, catalog))
        assert result["ranking"] and result["group"]["budget"]["people_count"] == people, name
        meta = result["ranking_meta"]["checked"]
        text = meta["statement"]
        groups = len(shape)
        rows.append(f"| {name} | {people} | {groups} | {meta['whole_generated']} | "
                    f"{format_space(meta['search_space'])} | {ms:.1f} |")
        assert text, name
    return rows
def live_table():
    """模拟服务上的团餐 live-plan：核价次数、调用总数、墙钟时间、限流等待。"""
    rows = [
        "### 团餐实时流程（回环模拟服务，默认参数 max_quotes=12）",
        "",
        "| 场景 | 人数 | calculate-price 次数 | 工具调用总数 | 墙钟 (s) | 触发限流等待 | 名次数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, people, shape in SCENARIOS:
        persons = []
        for n, meat in shape:
            for _ in range(n):
                persons.append({"id": f"P{len(persons) + 1:03d}", "hard": {}, "prefer": {"main_meat": meat}})
        req = live_request(
            persons, store="30001", budget=10**7,
            fulfillment={"type": "group", "be_code": "GBE30001", "address_index": 1, "gm_service_code": "GM-STD"},
            ranking={"top_n": 3, "priority": "price"},
        )
        with SimMcpServer() as sim:
            waits = []
            client = sim_client(sim)
            client.rate_limiter = RateLimiter(sleep=lambda seconds: waits.append(seconds))
            start = time.perf_counter()
            result = MultiLivePlanner(client).plan_v2(copy.deepcopy(req))
            wall = time.perf_counter() - start
            quotes = sum(1 for c in sim.calls if c["tool"] == "calculate-price")
            rows.append(
                f"| {name} | {people} | {quotes} | {len(sim.calls)} | {wall:.2f} | "
                f"{'是' if waits else '否'} | {len(result['ranking'])} |"
            )
    return rows
def main():
    """打印全部表格。"""
    print(f"环境：Python {platform.python_version()}，{platform.system()} {platform.machine()}；"
          f"引擎耗时为 {REPEAT} 次中位数；合成目录 60 个 offer（含小食），固定随机种子。")
    print()
    for line in engine_table(group_catalog()):
        print(line)
    print()
    for line in live_table():
        print(line)
if __name__ == "__main__":
    main()
