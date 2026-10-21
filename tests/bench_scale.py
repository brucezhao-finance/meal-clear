"""规模性能基准（不以 test_ 开头，不会被 discover 收集）。

用法：python3 tests/bench_scale.py
输出 Markdown 表到 stdout：引擎耗时（5 次取中位数）、整单候选数、搜索空间、新路径与旧 beam 对照，
以及用回环模拟服务跑 live-plan 的核价次数、调用总数、墙钟时间与限流等待情况。全部为合成数据，不联网。
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
from meal_clear.ranking import compose_v2, format_space, generate_candidates  # noqa: E402
from meal_clear.request_v2 import validate_request_v2  # noqa: E402
from scale_fixtures import distinct_people_request, group_request, synth_catalog  # noqa: E402
from sim_mcp import SimMcpServer  # noqa: E402
from sim_support import request as live_request  # noqa: E402
from sim_support import sim_client  # noqa: E402

REPEAT = 5
HARDS = ({}, {"non_spicy": True}, {"non_spicy": True, "sugar_free_drink": True}, {"sugar_free_drink": True})
MEATS = ("chicken", "beef", "pork", "fish")


def vocabulary_groups(total):
    """词汇表内所有 hard×prefer 组合（4×4=16）循环分配 total 个人。"""
    combos = [(h, {"main_meat": m}) for h in HARDS for m in MEATS]
    sizes = [total // len(combos) + (1 if i < total % len(combos) else 0) for i in range(len(combos))]
    return [(s, h, p) for s, (h, p) in zip(sizes, combos) if s]


def same_hard(sizes, prefer_meats=MEATS):
    """每组同一硬要求、偏好各异的组结构。"""
    return [
        (s, {"non_spicy": True}, {"main_meat": prefer_meats[i % len(prefer_meats)]})
        for i, s in enumerate(sizes)
    ]


SCENARIOS = (
    ("S1", 1, distinct_people_request(1)),
    ("S3", 3, distinct_people_request(3)),
    ("S6a", 6, distinct_people_request(6)),
    ("S6b", 6, group_request(same_hard([3, 3]))),
    ("S20a", 20, group_request(same_hard([8, 6, 4, 2]))),
    ("S20b", 20, distinct_people_request(20)),
    ("S50a", 50, group_request(same_hard([30, 10, 5, 3, 2]))),
    ("S50b", 50, group_request(vocabulary_groups(50))),
    ("S50c", 50, distinct_people_request(50)),
)


def median_ms(fn):
    """调用 fn 共 REPEAT 次，返回耗时中位数（毫秒）与最后一次的返回值。"""
    times = []
    value = None
    for _ in range(REPEAT):
        start = time.perf_counter()
        value = fn()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), value


def with_limits(req, limits):
    """复制请求并覆盖 limits。"""
    result = copy.deepcopy(req)
    result["limits"] = dict(limits)
    return validate_request_v2(result)


def engine_table(catalog, limits, label):
    """离线引擎表。"""
    rows = [
        f"### {label}",
        "",
        "| 场景 | 人数 | 同质组数 | 整单候选 | 搜索空间 | 引擎 compose_v2 (ms) | 新路径候选生成 (ms) | 旧 beam 候选生成 (ms) | 旧 beam 候选 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, people, base in SCENARIOS:
        req = with_limits(base, limits)
        engine_ms, result = median_ms(lambda: compose_v2(req, catalog))
        if people > 6:
            new_ms, new = median_ms(lambda: generate_candidates(req, catalog))
        else:
            from meal_clear.grouping import generate_candidates_grouped

            new_ms, new = median_ms(lambda: generate_candidates_grouped(req, catalog))
        old_ms, old = median_ms(lambda: generate_candidates(req, catalog, use_legacy_beam=True))
        stats = new["stats"]
        rows.append(
            f"| {name} | {people} | {stats['groups']} | {len(new['whole'])} | "
            f"{format_space(stats['search_space'])} | {engine_ms:.1f} | {new_ms:.1f} | "
            f"{old_ms:.1f} | {len(old['whole'])} |"
        )
        assert result["ranking"], name
    return rows


def live_table():
    """模拟服务上的 live-plan：核价次数、调用总数、墙钟时间、限流等待。"""
    rows = [
        "### 实时流程（回环模拟服务，默认参数 max_quotes=12）",
        "",
        "| 场景 | 人数 | calculate-price 次数 | 工具调用总数 | 墙钟 (s) | 触发限流等待 | 名次数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, people, base in SCENARIOS:
        req = copy.deepcopy(base)
        req["context"] = {"store_code": "10001", "meal_time": "now"}
        req["budget_cents"] = 10**7
        for person in req["people"]:
            person["hard"] = {"non_spicy": True}
        req = live_request(req["people"], budget=10**7, ranking=req["ranking"])
        with SimMcpServer() as sim:
            waits = []
            client = sim_client(sim)
            client.rate_limiter = RateLimiter(sleep=lambda seconds: waits.append(seconds))
            start = time.perf_counter()
            result = MultiLivePlanner(client).plan_v2(req)
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
          f"引擎耗时为 {REPEAT} 次中位数；合成目录 60 个 offer（含带券款与新购卡款），固定随机种子。")
    print()
    catalog = synth_catalog(60)
    for line in engine_table(catalog, {}, "离线引擎（默认参数：per_person_top_k=8，max_whole_candidates=200）"):
        print(line)
    print()
    upper = {"per_person_top_k": 12, "max_whole_candidates": 1000}
    for line in engine_table(catalog, upper, "离线引擎（上限参数：per_person_top_k=12，max_whole_candidates=1000）"):
        print(line)
    print()
    for line in live_table():
        print(line)


if __name__ == "__main__":
    main()
