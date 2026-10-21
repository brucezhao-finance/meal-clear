"""确认式下单端到端演示（非 test_*，不会被 discover 收集）。

启动回环模拟服务（全部合成数据、不联网、不用真实 Token），按场景驱动真实 CLI，
打印每一步的真实终端输出和服务端调用统计；退出码 0 表示全部断言成立。
用法：python3 tests/e2e_order_flow.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from order_support import OrderEnv, delivery_request  # noqa: E402

DIGEST_LINE = re.compile(r"摘要编号（digest）：([0-9a-f]{64})")
NEW_DIGEST_LINE = re.compile(r"新摘要编号：([0-9a-f]{64})")
EXPECTED_READ_TOOLS = {
    "query-nearby-stores", "query-meals", "query-meal-detail", "query-store-coupons", "calculate-price",
    "available-coupons", "query-my-coupons", "delivery-query-addresses", "delivery-query-stores",
    "query-order",
}
CONTROLLED = ("auto-bind-coupons", "create-order")
SCENARIOS = {
    "到店单人": {},
    "外送": delivery_request(),
    "3 人到店": {"people": [{"id": i, "hard": {"non_spicy": True}, "prefer": {}} for i in "ABC"]},
}


def ensure(condition, message):
    """断言（不依赖 assert，-O 下也生效）。"""
    if not condition:
        raise AssertionError(message)


class Flow:
    """记录每一步的命令、退出码与真实输出。"""

    def __init__(self, env, request_path):
        self.env = env
        self.request_path = request_path
        self.steps = []

    def run(self, title, *argv):
        code, out = self.env.run(*argv)
        shown = ["<request.json>" if a == self.request_path else a for a in argv]
        self.steps.append({"title": title, "argv": shown, "code": code, "out": out})
        return code, out

    def controlled(self, tool=None):
        calls = self.env.sim.controlled_calls
        return len([c for c in calls if tool is None or c["tool"] == tool])

    def stats(self):
        sim = self.env.sim
        read = {}
        for call in sim.calls:
            read[call["tool"]] = read.get(call["tool"], 0) + 1
        return {
            "read_calls": sum(read.values()),
            "read_by_tool": read,
            "auto-bind-coupons": self.controlled("auto-bind-coupons"),
            "create-order": self.controlled("create-order"),
            "orders": len(sim.orders),
            "duplicate_orders": sim.duplicate_orders,
            "forbidden_attempts": list(sim.forbidden_attempts),
        }


def _common_checks(flow):
    sim = flow.env.sim
    ensure(set(c["tool"] for c in sim.calls) <= EXPECTED_READ_TOOLS, "服务端出现预期之外的只读工具")
    ensure(set(c["tool"] for c in sim.controlled_calls) <= set(CONTROLLED), "服务端出现预期之外的受控写工具")
    ensure(sim.forbidden_attempts == [], "出现被永久禁止的工具调用")
    ensure(sim.duplicate_orders == 0, "服务端出现重复订单")


def run_scenario(name, req, path="unchanged", take_way_count=1):
    """跑一个场景。path='unchanged' 价格不变；'changed' 领券后价格变化需重新确认。返回 (Flow, stats)。"""
    with OrderEnv(take_way_count=take_way_count) as env:
        request_path = env.request_path(**req)
        flow = Flow(env, request_path)
        code, out = flow.run("生成订单摘要（只读联网，写本地账本）", "order-summary", "--request", request_path)
        ensure(code == 0, f"{name}: order-summary 退出 {code}")
        digest = DIGEST_LINE.search(out).group(1)
        short = digest[:12]
        ensure(flow.controlled() == 0, "生成摘要不应有任何写调用")

        code, out = flow.run("领券预演（默认不领）", "order-claim", "--digest", short)
        ensure(code == 0 and "无法逐张选择" in out and "不可撤销" in out, f"{name}: 领券预演文案/退出码不符")
        code, out = flow.run("下单预演（默认不下）", "order-place", "--digest", short)
        ensure(code in (0,), f"{name}: 下单预演退出 {code}")
        ensure(flow.controlled() == 0 and env.sim.write_attempts == [], f"{name}: 预演阶段服务端出现写调用")

        code, out = flow.run("确认后领券（一键领全部）", "order-claim", "--digest", short,
                             "--execute", "--confirm-digest", digest)
        ensure(code == 0, f"{name}: 领券退出 {code}")
        ensure(env.sim.claims == 1 and flow.controlled("auto-bind-coupons") == 1, f"{name}: 领券应恰好 1 次")

        if path == "changed":
            env.sim.set_price_drift(300)
            code, out = flow.run("下单前重新核价：价格变化，要求重新确认", "order-place", "--digest", short,
                                 "--execute", "--confirm-digest", digest)
            ensure(code == 3, f"{name}: 价格变化应退出 3，实际 {code}")
            ensure(flow.controlled("create-order") == 0 and env.sim.orders == [], f"{name}: 价格变化时不应下单")
            new_digest = NEW_DIGEST_LINE.search(out).group(1)
            ensure(new_digest != digest, "价格变化应生成新 digest")
            code, out = flow.run("旧摘要不能再用", "order-place", "--digest", short,
                                 "--execute", "--confirm-digest", digest)
            ensure(code == 3 and flow.controlled("create-order") == 0, f"{name}: 旧摘要应失效")
            digest, short = new_digest, new_digest[:12]
            code, out = flow.run("对新摘要重新确认后下单", "order-place", "--digest", short,
                                 "--execute", "--confirm-digest", digest)
        else:
            code, out = flow.run("确认后下单（创建待支付订单）", "order-place", "--digest", short,
                                 "--execute", "--confirm-digest", digest)
        ensure(code == 0, f"{name}: 下单退出 {code}：{out}")
        ensure(flow.controlled("create-order") == 1 and len(env.sim.orders) == 1, f"{name}: 下单应恰好 1 次")
        ensure(all(w in out for w in ("订单已创建", "待支付", "未付款", "不会自动付款")), f"{name}: 创建文案不全")
        order_id = env.sim.orders[0]["orderId"]
        ensure(order_id in out, f"{name}: 创建那次应显示订单号")

        before = (len(env.sim.calls), len(env.sim.controlled_calls))
        code, out = flow.run("重复下单被拒（不再请求服务端）", "order-place", "--digest", short,
                             "--execute", "--confirm-digest", digest)
        ensure(code == 5, f"{name}: 重复下单应退出 5，实际 {code}")
        ensure(order_id not in out, "重复拒绝不应再印订单号")
        ensure((len(env.sim.calls), len(env.sim.controlled_calls)) == before, f"{name}: 重复下单不应产生服务端调用")
        ensure(flow.controlled("create-order") == 1 and len(env.sim.orders) == 1, f"{name}: 重复下单后订单数应不变")

        code, out = flow.run("查询订单状态（订单号打码）", "order-status", "--digest", short)
        ensure(code == 0 and order_id not in out and "****" in out, f"{name}: 订单查询应打码")
        _common_checks(flow)
        return flow, flow.stats()


def run_official_scenario():
    """场景 7：官方通道（打桩传输，零联网）。开关+假 TTY+正确短语 → claim→place 成功；无开关 → 退出 4 且零请求。"""
    from official_support import CLAIM, ORDER, OfficialEnv

    print("=" * 78)
    print("场景：官方通道打桩（开关 + 假 TTY + 正确短语）")
    print("=" * 78)
    with OfficialEnv(switch=True) as env:
        code, out, data = env.summary()
        ensure(code == 0, "官方打桩：order-summary 退出 %s" % code)
        digest = data["digest"]
        code, out = env.official_claim(digest)
        ensure(code == 0 and len(env.official_writes(CLAIM)) == 1, "官方打桩：领券应退出 0 且恰好 1 次，实际 %s" % code)
        code, out = env.official_place(digest)
        ensure(code == 0 and len(env.official_writes(ORDER)) == 1 and len(env.sim.orders) == 1,
               "官方打桩：下单应退出 0 且恰好 1 次，实际 %s" % code)
        ensure("待支付" in out or "未付款" in out, "官方打桩：创建文案应说明未付款")
        ensure({r["url"] for r in env.stub.requests} == {"https://mcp.mcd.cn"}, "官方打桩：所有请求应发往官方 URL（经桩转发）")
        ensure(env.sim.forbidden_attempts == [], "官方打桩：出现被禁止的工具调用")
        print("开关开启：claim 退出 0，place 退出 0；create-order 1 次，auto-bind-coupons 1 次。")
    with OfficialEnv(switch=False) as env:
        code, out, data = env.summary()
        ensure(code == 0, "官方打桩(无开关)：order-summary 退出 %s" % code)
        env.mark()
        code, out = env.official_claim(data["digest"])
        requests, inits = env.since()
        ensure(code == 4, "无开关：领券应退出 4，实际 %s" % code)
        ensure(requests == [] and inits == 0 and env.stub.writes() == [], "无开关：桩应零请求")
        code, out, skip = env.summary("--skip-claim")
        env.mark()
        code, out = env.official_place(skip["digest"])
        ensure(code == 4 and env.since() == ([], 0), "无开关：下单应退出 4 且零请求，实际 %s" % code)
        print("开关关闭：claim/place 均退出 4，桩零请求。")


def main():
    for name, req in SCENARIOS.items():
        for path in ("unchanged", "changed"):
            label = f"{name}／" + ("价格不变" if path == "unchanged" else "领券后价格变化")
            print("=" * 78)
            print(f"场景：{label}")
            print("=" * 78)
            flow, stats = run_scenario(name, req, path)
            for number, step in enumerate(flow.steps, 1):
                print(f"\n--- 步骤 {number}：{step['title']} ---")
                print("$ meal-clear " + " ".join(step["argv"]) + f"    [退出码 {step['code']}]")
                print(step["out"].rstrip())
            print("\n--- 服务端调用统计（模拟服务）---")
            print(f"只读调用 {stats['read_calls']} 次；auto-bind-coupons {stats['auto-bind-coupons']} 次；"
                  f"create-order {stats['create-order']} 次；已创建订单 {stats['orders']} 个；"
                  f"重复订单 {stats['duplicate_orders']} 个；被禁止工具尝试 {len(stats['forbidden_attempts'])} 次")
    run_official_scenario()
    print("\n全部场景断言成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
