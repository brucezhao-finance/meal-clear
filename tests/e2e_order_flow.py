"""确认式下单端到端演示（非 test_*，不会被 discover 收集）。

启动回环模拟服务（全部合成数据、不联网、不用真实 Token），按场景驱动真实 CLI，
打印每一步的真实终端输出和服务端调用统计；退出码 0 表示全部断言成立。
用法：python3 tests/e2e_order_flow.py
"""
import json
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
    "query-order", "query-promotions", "query-meal-assistance",
}
CONTROLLED = ("auto-bind-coupons", "create-order")
GROUP_PEOPLE = [{"id": "P%02d" % i, "hard": {}, "prefer": {}} for i in range(1, 21)]
GROUP_FUL = {"type": "group", "be_code": "GBE30001", "address_index": 1, "gm_service_code": "GM-STD"}
GROUP_REQ = {"store": "30001", "fulfillment": GROUP_FUL, "people": GROUP_PEOPLE,
             "budget": 200040, "per_person_budget_cents": 10002}
SCENARIOS = {
    "到店单人": {},
    "外送": delivery_request(),
    "3 人到店": {"people": [{"id": i, "hard": {"non_spicy": True}, "prefer": {}} for i in "ABC"]},
}
GROUP_SCENARIOS = {"团餐20人": GROUP_REQ}  # 与 SCENARIOS 分开，保持既有场景清单不变


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


def run_group_walkthrough():
    """团餐脚本驱动全流程（回环 sim）：地址→团餐门店→group-info→live-plan→摘要→预演→领券/下单，并手算核对。"""
    from sim_mcp import MEALS
    print("=" * 78)
    print("场景：团餐 20 人脚本驱动全流程（回环模拟服务，全部合成数据）")
    print("=" * 78)
    with OrderEnv() as env:
        total_only = dict(GROUP_REQ, budget=200050)
        del total_only["per_person_budget_cents"]
        total_path = env.request_path(**total_only)
        path = env.request_path(**GROUP_REQ)
        flow = Flow(env, path)
        code, out = flow.run("列出配送地址序号（不含地址内容）", "addresses")
        ensure(code == 0 and "ADDR-SENTINEL" not in out, "团餐：addresses 退出 %s" % code)
        code, out = flow.run("团餐可配送门店（beType=6）", "delivery-stores", "--address-index", "1", "--be-type", "6")
        ensure(code == 0 and "GBE30001" in out, "团餐：delivery-stores --be-type 6 退出 %s" % code)
        stores = [c["args"] for c in env.sim.calls if c["tool"] == "delivery-query-stores"]
        ensure([a["beType"] for a in stores] == [6], "团餐：门店查询应为 beType=6")
        code, out = flow.run("group-info：只给总预算 ¥2000.50（手算：200050÷20=10002 余 10）",
                             "group-info", "--request", total_path)
        ensure(code == 0 and "¥100.02" in out and "余 10 分" in out and "以官方核价为准" in out,
               "团餐：group-info 预算/文案不符：%s" % out)
        ensure(not any(w in out for w in ("ADDR-SENTINEL", "PHONE-SENTINEL", "CONTACT-SENTINEL", "ADDRID")), "团餐：group-info 泄露地址")
        code, out = flow.run("live-plan：20 人、人均预算 ¥100.02", "live-plan", "--request", path, "--top", "2")
        ensure(code == 0 and "官方核价" in out, "团餐：live-plan 退出 %s" % code)
        quotes = [c["args"] for c in env.sim.calls if c["tool"] == "calculate-price"]
        ensure(quotes and all(q["gmServiceCode"] == "GM-STD" and q["beType"] == 6 for q in quotes), "团餐：核价入参不符")
        code, out = flow.run("order-summary（生成摘要）", "order-summary", "--request", path)
        ensure(code == 0, "团餐：order-summary 退出 %s" % code)
        digest = DIGEST_LINE.search(out).group(1)
        short = digest[:12]
        code, jout = env.run("order-summary", "--request", path, "--json")
        summary = json.loads(jout)
        ensure(summary["digest"] == digest, "团餐：同输入摘要应一致")
        # 手算：商品价（MEALS）× 数量（有券行每件减 500）+ 配送费 600 + 包装费 100
        price_by_name = {n: p for n, p in MEALS.values()}
        product = 0
        parts = []
        for row in summary["lines"]:
            base = max((n for n in price_by_name if row["name"].startswith(n)), key=len)  # 行名可能带选项后缀
            unit = price_by_name[base] - (500 if row.get("coupon") else 0)
            product += unit * row["quantity"]
            parts.append("%d×%d" % (row["quantity"], unit))
        hand = product + 600 + 100
        print("\n手算核对：商品 " + "＋".join(parts) + " = %d 分 + 配送费 600 + 包装费 100 = %d 分（¥%.2f）；摘要官方核价 %d 分；人均 %d 分（向下取整）余 %d 分；预算上限 %d 分"
              % (product, hand, hand / 100, summary["payable_cents"], hand // 20, hand % 20, GROUP_REQ["budget"]))
        ensure(hand == summary["payable_cents"], "团餐：手算总价与摘要不一致")
        ensure(summary["group_budget"]["per_person_cents"] == 10002 and summary["group_budget"]["total_cents"] == 200040,
               "团餐：摘要人均预算不符")
        ensure(hand <= GROUP_REQ["budget"], "团餐：总价应在预算内")
        ensure(flow.controlled() == 0, "团餐：预演前不应有写调用")
        code, out = flow.run("领券预演（默认不领）", "order-claim", "--digest", short)
        ensure(code == 0 and flow.controlled() == 0, "团餐：领券预演应不写")
        code, out = flow.run("下单预演（默认不下）", "order-place", "--digest", short)
        ensure(code == 0 and flow.controlled() == 0, "团餐：下单预演应不写")
        code, out = flow.run("确认后领券", "order-claim", "--digest", short, "--execute", "--confirm-digest", digest)
        ensure(code == 0 and flow.controlled("auto-bind-coupons") == 1, "团餐：领券应恰 1 次")
        code, out = flow.run("确认后下单", "order-place", "--digest", short, "--execute", "--confirm-digest", digest)
        ensure(code == 0 and flow.controlled("create-order") == 1 and len(env.sim.orders) == 1, "团餐：下单应恰 1 次")
        args = env.create_calls()[0]["args"]
        ensure({k: v for k, v in args.items() if k != "items"} == {
            "storeCode": "30001", "orderType": 2, "beType": 6, "beCode": "GBE30001", "addressId": "ADDRID-1",
            "gmServiceCode": "GM-STD", "needTableware": False}, "团餐：create-order 入参不符")
        ensure("takeWayCode" not in args, "团餐：不应带 takeWayCode")
        code, out = flow.run("重复下单被拒", "order-place", "--digest", short, "--execute", "--confirm-digest", digest)
        ensure(code == 5 and flow.controlled("create-order") == 1, "团餐：重复下单应退出 5")
        _common_checks(flow)
        for number, step in enumerate(flow.steps, 1):
            print(f"\n--- 步骤 {number}：{step['title']} ---")
            print("$ meal-clear " + " ".join(step["argv"]) + f"    [退出码 {step['code']}]")
            print(step["out"].rstrip())
        stats = flow.stats()
        print("\n--- 服务端调用统计（模拟服务）---")
        print(f"只读调用 {stats['read_calls']} 次（{stats['read_by_tool']}）；auto-bind-coupons {stats['auto-bind-coupons']} 次；"
              f"create-order {stats['create-order']} 次；已创建订单 {stats['orders']} 个；重复订单 {stats['duplicate_orders']} 个")
def run_group_official_refusal():
    """官方端点团餐真实写被拒（打桩，零请求零 initialize），开关开/关均如此。"""
    from official_support import OfficialEnv
    print("=" * 78)
    print("场景：官方端点团餐真实写被拒（打桩传输，零联网）")
    print("=" * 78)
    for label, switch in (("开关开启", True), ("开关关闭", False)):
        with OfficialEnv(switch=switch) as env:
            code, out, skip = env.summary("--skip-claim", **GROUP_REQ)
            ensure(code == 0, "官方团餐(%s)：摘要退出 %s" % (label, code))
            code, out, claim = env.summary(**dict(GROUP_REQ, fulfillment=dict(GROUP_FUL, gm_service_code="GM-ALT")))
            ensure(code == 0, "官方团餐(%s)：摘要退出 %s" % (label, code))
            env.mark()
            code, out = env.official_claim(claim["digest"])
            ensure(code == 4 and env.since() == ([], 0) and env.official_writes() == [], "官方团餐(%s)：claim 应退出 4 且零请求，实际 %s" % (label, code))
            print("$ order-claim --execute（官方端点，团餐，%s）    [退出码 %s]\n%s" % (label, code, out.strip()))
            env.mark()
            code, out = env.official_place(skip["digest"])
            ensure(code == 4 and env.since() == ([], 0) and env.official_writes() == [], "官方团餐(%s)：place 应退出 4 且零请求，实际 %s" % (label, code))
            print("$ order-place --execute（官方端点，团餐，%s）    [退出码 %s]\n%s" % (label, code, out.strip()))
            ensure(env.sim.controlled_calls == [], "官方团餐：服务端不应有受控写调用")
        print("%s：团餐 claim/place 均退出 4，桩请求数与 initialize 次数均为 0。" % label)
def main():
    for name, req in list(SCENARIOS.items()) + list(GROUP_SCENARIOS.items()):
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
    run_group_walkthrough()
    run_official_scenario()
    run_group_official_refusal()
    print("\n全部场景断言成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
