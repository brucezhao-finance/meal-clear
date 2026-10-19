"""命令行入口：离线演示、结构化选餐、增量修改与只读真实联调。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .core import InputError, amend_request, break_even, compare_results, compose
from .live import LivePlanner, timestamp
from .mcp import McpClient, McpError

ROOT = Path(__file__).resolve().parents[2]


def read_json(path: str) -> Dict[str, Any]:
    """读取本地结构化输入，错误不回显文件内容。"""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InputError("JSON顶层必须为对象")
    return value


def save_json(path: str, value: Dict[str, Any]) -> None:
    """以仅当前用户可读写的原子文件保存状态；不保存原始账户回包。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def render(result: Dict[str, Any]) -> str:
    """渲染可解释的方案，不把模拟、历史与真实报价混为一谈。"""
    labels = {
        "simulation": "模拟演示，不是真实菜单/价格",
        "live": "只读实时查询",
        "historical": "历史资料，非当前报价",
        "user_supplied": "用户提供资料，未在线核价",
    }
    lines = [
        "# 麦麦明白点",
        "",
        labels[result["mode"]],
        f"数据时间：{result['as_of']}",
        f"结果：{result['status']}",
        f"门店：{result['request']['context']['store_code']}；到店自取",
        "",
    ]
    names = {
        "non_spicy": "不辣",
        "sugar_free_drink": "饮品无糖",
        "main_meat": "主食肉类",
    }
    states = {"verified": "已核实", "violated": "冲突", "unknown": "未知"}
    for index, plan in enumerate(result.get("recommendations", []), 1):
        lines.extend(
            [
                f"## 方案{index}",
                f"本次报价（未下单）：¥{plan['total_cents'] / 100:.2f}；新卡费用已计入：¥{plan['new_card_cents'] / 100:.2f}",
                f"金额依据：{plan['price_status']}／{plan.get('pricing_source') or '来源未标记'}",
            ]
        )
        for assignment in plan["assignments"]:
            items = "＋".join(
                f"{i['name']}×{i.get('quantity', 1)}" for i in assignment["items"]
            )
            lines.append(f"- {assignment['person_id']}：{items}")
            for check in assignment["checks"]:
                lines.append(
                    f"  - {names.get(check['attribute'], check['attribute'])}={check['expected']}：{states[check['status']]}"
                )
        lines.append(
            f"- 预算：满足；新购卡数量：{plan['steps']['new_card_count']}；订单数：1"
        )
        if plan.get("quoted_at"):
            lines.append(f"- 官方核价时间：{plan['quoted_at']}；购买前价格可能变化")
            unknown_fees = [
                k for k, v in plan.get("fee_breakdown", {}).items() if v is None
            ]
            if unknown_fees:
                lines.append(
                    "- 官方已返回整单应付总额；部分费用未拆分返回，保留未知，不当作0元"
                )
        lines.append("")
    if not result.get("recommendations"):
        lines.append("没有可确认的预算内方案，未自动放宽要求。")
    if result.get("diagnostics"):
        lines.append("## 需要说明")
        for entry in result["diagnostics"]:
            lines.append(
                "- "
                + (
                    entry
                    if isinstance(entry, str)
                    else json.dumps(entry, ensure_ascii=False)
                )
            )
    if result.get("quote_errors"):
        lines.extend(["## 核价异常", *["- " + e for e in result["quote_errors"]]])
    lines.extend(["", "范围：仅比较已检查的候选；未领券、买卡或下单。"])
    return "\n".join(lines)


def client_from_args(args: argparse.Namespace) -> McpClient:
    """初始化安全只读客户端。"""
    client = McpClient(token_file=args.token_file)
    client.initialize()
    return client


def main(argv: Optional[list] = None) -> int:
    """分发命令并对预期错误返回非零退出状态。"""
    parser = argparse.ArgumentParser(description="麦麦明白点：只读选餐与增量调整")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="模拟数据演示修改前后")
    demo.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan", help="本地标注菜单选餐")
    plan.add_argument("--request", required=True)
    plan.add_argument("--catalog", required=True)
    plan.add_argument("--save")
    plan.add_argument("--json", action="store_true")
    update = sub.add_parser("update", help="修改已保存请求并重新计算")
    update.add_argument("--state", required=True)
    update.add_argument("--patch", required=True)
    update.add_argument("--catalog", help="离线模式菜单；上下文须匹配")
    update.add_argument("--token-file")
    update.add_argument("--save")
    update.add_argument("--json", action="store_true")
    stores = sub.add_parser("stores", help="只读查询门店，不自动替用户选店")
    stores.add_argument("--city", required=True)
    stores.add_argument("--keyword", required=True)
    stores.add_argument("--token-file")
    live = sub.add_parser("live-plan", help="只读真实菜单与官方核价")
    live.add_argument("--request", required=True)
    live.add_argument("--token-file")
    live.add_argument("--save")
    live.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect-tools", help="检查当前只读工具可用性")
    inspect.add_argument("--token-file")
    card = sub.add_parser("break-even", help="固定单次节省额的回本计算（整数分）")
    card.add_argument("--fee", type=int, required=True)
    card.add_argument("--saving", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "break-even":
            print(
                json.dumps(
                    {"visits": break_even(args.fee, args.saving)}, ensure_ascii=False
                )
            )
            return 0
        if args.command == "demo":
            catalog = read_json(str(ROOT / "examples/demo-catalog.json"))
            request = read_json(str(ROOT / "examples/request.json"))
            before = compose(request, catalog)
            changed = amend_request(
                request, read_json(str(ROOT / "examples/patch-beef.json"))
            )
            after = compose(changed, catalog)
            diff = compare_results(before, after)
            if args.json:
                print(
                    json.dumps(
                        {"before": before, "after": after, "difference": diff},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(
                    render(before)
                    + "\n\n--- 改一句：B想吃牛肉，其他要求不变 ---\n\n"
                    + render(after)
                )
                print("\n变化说明：" + json.dumps(diff, ensure_ascii=False))
            return 0
        if args.command == "inspect-tools":
            current = client_from_args(args).list_tools()["tools"]
            from .mcp import READ_ONLY

            print(
                json.dumps(
                    {
                        "as_of": timestamp(),
                        "tool_count": len(current),
                        "available_read_only": [
                            t["name"] for t in current if t["name"] in READ_ONLY
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "stores":
            value = client_from_args(args).call(
                "query-nearby-stores",
                {
                    "beType": 1,
                    "searchType": 2,
                    "city": args.city,
                    "keyword": args.keyword,
                },
            )
            public = [
                {
                    k: r.get(k)
                    for k in ("storeCode", "storeName", "address", "businessStatus")
                }
                for r in value.get("data", [])
            ]
            print(
                json.dumps(
                    {
                        "as_of": timestamp(),
                        "stores": public,
                        "note": "请用户明确选定门店，未自动选店或下单",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        difference = None
        if args.command == "update":
            state = read_json(args.state)
            request = amend_request(state["request"], read_json(args.patch))
            if state["mode"] == "live":
                result = LivePlanner(client_from_args(args)).plan(request)
            else:
                if not args.catalog:
                    raise InputError("离线修改需要显式提供--catalog，不静默复用旧价格")
                result = compose(request, read_json(args.catalog))
            difference = compare_results(state, result)
            result["difference"] = difference
        elif args.command == "live-plan":
            result = LivePlanner(client_from_args(args)).plan(read_json(args.request))
        else:
            result = compose(read_json(args.request), read_json(args.catalog))
        if args.save:
            save_json(args.save, result)
        print(
            json.dumps(result, ensure_ascii=False, indent=2)
            if args.json
            else render(result)
        )
        if difference and not args.json:
            print("\n变化说明：" + json.dumps(difference, ensure_ascii=False))
        return 0 if result.get("recommendations") else 2
    except (
        InputError,
        McpError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        # 不输出网络异常、文件内容或原始响应以避免账户信息泄漏。
        safe = (
            str(exc)
            if isinstance(exc, (InputError, McpError))
            else "输入文件缺失、格式错误或结构不匹配"
        )
        print(json.dumps({"status": "error", "message": safe}, ensure_ascii=False))
        return 1
