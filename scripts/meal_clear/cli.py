"""命令行入口：离线演示、结构化选餐、增量修改与只读真实联调。"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .core import (
    InputError,
    amend_request,
    break_even,
    compare_results,
    compose,
    validate_request,
)
from . import orders, report_orders
from .config import EXIT_OK, EXIT_REFUSED, LEDGER_DIRNAME, PRIORITIES, SUMMARY_TTL_SECONDS
from .gate import WriteDenied, check_write_target
from .ledger import Ledger
from .live import LivePlanner, MultiLivePlanner, timestamp
from .mcp import ENDPOINT, McpClient, McpError
from .privacy import assert_public
from .ranking import compose_v2
from .report import render_coupon_preview, render_ranking
from .request_v2 import amend_request_v2, validate_request_v2

ROOT = Path(__file__).resolve().parents[2]


def ledger_root() -> Path:
    """订单账本目录（测试可 patch；不提供命令行/环境变量入口）。"""
    return ROOT / "local-state" / LEDGER_DIRNAME


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


def apply_rank_options(request: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """把 --top/--priority 写入 v2 请求的 ranking（命令行优先）。"""
    top = getattr(args, "top", None)
    priority = getattr(args, "priority", None)
    if top is None and priority is None:
        return request
    result = copy.deepcopy(request)
    ranking = result.setdefault("ranking", {})
    if not isinstance(ranking, dict):
        raise InputError("ranking 必须为对象")
    if top is not None:
        ranking["top_n"] = top
    if priority is not None:
        ranking["priority"] = priority
    return result


def reject_rank_options(args: argparse.Namespace) -> None:
    """--top/--priority 仅适用于 schema_version=2。"""
    if getattr(args, "top", None) is not None or getattr(args, "priority", None):
        raise InputError("--top/--priority 仅适用于 schema_version=2 请求")


def emit(result: Dict[str, Any], args: argparse.Namespace, v2: bool) -> None:
    """保存并输出结果；v2 在保存/输出前自检敏感字段。"""
    if v2:
        assert_public(result)
    if getattr(args, "save", None):
        save_json(args.save, result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_ranking(result, getattr(args, "detail", False)) if v2 else render(result))


def client_from_args(args: argparse.Namespace, write: bool = False) -> McpClient:
    """初始化安全客户端；write=True 时先校验写目标，失败则零网络请求。"""
    if write:
        # 在读取 Token、构造客户端、initialize 之前对固定官方端点做写目标检查
        check_write_target(ENDPOINT)
    client = McpClient(token_file=args.token_file)
    client.initialize()
    return client


def _print_result(result: Dict[str, Any], as_json: bool, text: str) -> None:
    """输出前自检敏感字段；JSON 或文本二选一。"""
    assert_public(result)
    print(json.dumps(result, ensure_ascii=False, indent=2) if as_json else text)


def run_order_summary(args: argparse.Namespace) -> int:
    """order-summary：联网只读，写本地账本（摘要记录）。"""
    orders.check_ttl(args.ttl_seconds)
    raw = read_json(args.request)
    if raw.get("schema_version") != 2:
        raise InputError("order-summary 需要 schema_version=2 的实时请求")
    ledger = Ledger(ledger_root())
    planner = MultiLivePlanner(client_from_args(args))
    outcome = orders.build_summary(
        planner,
        raw,
        ledger,
        signature=args.signature,
        rank=args.rank,
        take_way=args.take_way,
        skip_claim=args.skip_claim,
        ttl_seconds=args.ttl_seconds,
        city=args.city,
        keyword=args.keyword,
    )
    if outcome["status"] != "ok":
        public = {k: v for k, v in outcome.items() if k != "record"}
        _print_result(public, args.json, report_orders.render_notice(public))
        return outcome["exit_code"]
    summary = outcome["summary"]
    assert_public(summary)
    if args.save:
        save_json(args.save, summary)
    _print_result(summary, args.json, report_orders.render_summary(summary, args.detail))
    return EXIT_OK


def run_order_claim(args: argparse.Namespace) -> int:
    """order-claim：默认预演（只读）；--execute 需三重门。"""
    ledger = Ledger(ledger_root())
    if not args.execute:
        ledger.resolve(args.digest)
        preview = orders.claim_preview(client_from_args(args), ledger, args.digest)
        _print_result(preview, args.json, report_orders.render_claim_preview(preview))
        return preview["exit_code"]
    orders.precheck_claim(ledger, args.digest, args.confirm_digest)
    client = client_from_args(args, write=True)
    result = orders.claim_execute(client, ledger, args.digest, args.confirm_digest)
    _print_result(result, args.json, report_orders.render_result(result))
    return result["exit_code"]


def add_rank_options(parser: argparse.ArgumentParser) -> None:
    """v2 排名选项。"""
    parser.add_argument("--top", type=int, help="展示名次数（仅 v2）")
    parser.add_argument("--priority", choices=PRIORITIES, help="排序优先（仅 v2）")
    parser.add_argument(
        "--detail", action="store_true", help="≥13 人时文本报告展开逐人（默认按同质组汇总；仅影响文本）"
    )


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
    add_rank_options(plan)
    update = sub.add_parser("update", help="修改已保存请求并重新计算")
    update.add_argument("--state", required=True)
    update.add_argument("--patch", required=True)
    update.add_argument("--catalog", help="离线模式菜单；上下文须匹配")
    update.add_argument("--token-file")
    update.add_argument("--save")
    update.add_argument("--json", action="store_true")
    add_rank_options(update)
    stores = sub.add_parser("stores", help="只读查询门店，不自动替用户选店")
    stores.add_argument("--city", required=True)
    stores.add_argument("--keyword", required=True)
    stores.add_argument("--be-type", type=int, choices=(1, 5), default=1)
    stores.add_argument("--token-file")
    addresses = sub.add_parser("addresses", help="只读列出配送地址序号（不含地址内容）")
    addresses.add_argument("--token-file")
    dstores = sub.add_parser("delivery-stores", help="只读查询某地址可配送门店")
    dstores.add_argument("--address-index", type=int, required=True)
    dstores.add_argument("--token-file")
    coupons = sub.add_parser("coupons", help="只读券预览（可领/已领/本店可用）")
    coupons.add_argument("--request", required=True)
    coupons.add_argument("--token-file")
    coupons.add_argument("--json", action="store_true")
    live = sub.add_parser("live-plan", help="只读真实菜单与官方核价")
    live.add_argument("--request", required=True)
    live.add_argument("--token-file")
    live.add_argument("--save")
    live.add_argument("--json", action="store_true")
    add_rank_options(live)
    multi = sub.add_parser("demo-multi", help="离线3人多方案排名演示")
    multi.add_argument("--json", action="store_true")
    exit_codes = "退出码：" + "；".join(
        f"{code} {report_orders.exit_hint(code)}" for code in range(7)
    )
    osum = sub.add_parser(
        "order-summary",
        help="生成确认式订单摘要（只读联网，写本地账本；不领券不下单）",
        epilog=exit_codes,
    )
    osum.add_argument("--request", required=True, help="schema_version=2 的实时请求")
    pick = osum.add_mutually_exclusive_group()
    pick.add_argument("--signature", help="live-plan 排名里的方案编号（8–16 位前缀）")
    pick.add_argument("--rank", type=int, help="按本次排名名次选择（默认 1；排名可能与上次不同，请核对）")
    osum.add_argument("--take-way", type=int, help="多个取餐选项时选择第 N 项")
    osum.add_argument("--skip-claim", action="store_true", help="不领券，摘要不含将领券集合")
    osum.add_argument("--ttl-seconds", type=int, default=SUMMARY_TTL_SECONDS, help="摘要有效期（60–1800 秒）")
    osum.add_argument("--city", help="到店/得来速：用于核实门店名")
    osum.add_argument("--keyword", help="到店/得来速：用于核实门店名")
    osum.add_argument("--token-file")
    osum.add_argument("--detail", action="store_true", help="≥13 人时展开逐人")
    osum.add_argument("--save")
    osum.add_argument("--json", action="store_true")
    oclaim = sub.add_parser(
        "order-claim",
        help="领券：默认只读预演；--execute 需完整摘要确认（一键领全部，不可撤销）",
        epilog=exit_codes,
    )
    oclaim.add_argument("--digest", required=True, help="摘要编号（唯一前缀，至少 12 位）")
    oclaim.add_argument("--execute", action="store_true", help="真正领券（仍需 --confirm-digest 与回环目标）")
    oclaim.add_argument("--confirm-digest", help="完整 64 位摘要编号，表示你已确认该摘要")
    oclaim.add_argument("--token-file")
    oclaim.add_argument("--json", action="store_true")
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
        if args.command == "order-summary":
            return run_order_summary(args)
        if args.command == "order-claim":
            return run_order_claim(args)
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
        if args.command == "demo-multi":
            catalog = read_json(str(ROOT / "examples/demo-catalog-multi.json"))
            request = read_json(str(ROOT / "examples/request-multi3.json"))
            result = compose_v2(request, catalog)
            emit(result, args, True)
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
                    "beType": args.be_type,
                    "searchType": 2,
                    "city": args.city,
                    "keyword": args.keyword,
                },
            )
            public = [
                {
                    **{
                        k: r.get(k)
                        for k in ("storeCode", "storeName", "address", "businessStatus")
                    },
                    **({"beCode": r["beCode"]} if "beCode" in r else {}),
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
        if args.command == "addresses":
            planner = MultiLivePlanner(client_from_args(args))
            labels = [a.public() for a in planner.list_addresses()]
            output = {
                "as_of": timestamp(),
                "addresses": labels,
                "note": "仅显示序号与标签；请对照 App 地址列表顺序，用 address_index 选择",
            }
            assert_public(output)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "delivery-stores":
            planner = MultiLivePlanner(client_from_args(args))
            output = {
                "as_of": timestamp(),
                "stores": planner.list_delivery_stores(args.address_index),
                "note": "请用户明确选定门店与 beCode，未自动选店或下单",
            }
            assert_public(output)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "coupons":
            req = validate_request_v2(read_json(args.request))
            planner = MultiLivePlanner(client_from_args(args))
            preview = planner.coupon_preview(planner.resolve_context(req)["wire"])
            assert_public(preview)
            if args.json:
                print(json.dumps(preview, ensure_ascii=False, indent=2))
            else:
                print(render_coupon_preview(preview))
            return 0
        difference = None
        v2 = False
        if args.command == "update":
            state = read_json(args.state)
            v2 = state.get("schema_version") == 2
            if v2:
                patch = read_json(args.patch)
                request = amend_request_v2(
                    apply_rank_options(state["request"], args), patch
                )
                unchanged = request == validate_request_v2(state["request"])
            else:
                reject_rank_options(args)
                request = amend_request(state["request"], read_json(args.patch))
                unchanged = request == validate_request(state["request"])
            if unchanged:
                # 修改包未改变任何值：不重新计算、不保存、不联网。
                print(
                    json.dumps(
                        {
                            "status": "no_change",
                            "message": "无变化：修改包未改变任何请求内容",
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            if state["mode"] == "live":
                if v2:
                    result = MultiLivePlanner(client_from_args(args)).plan_v2(request)
                    old_ref = (state.get("fulfillment") or {}).get("address_ref")
                    new_ref = result["fulfillment"].get("address_ref")
                    if old_ref and new_ref and old_ref != new_ref:
                        result["diagnostics"].append(
                            "地址列表顺序可能已变化（address_ref 不同），请核对 address_index"
                        )
                else:
                    result = LivePlanner(client_from_args(args)).plan(request)
            else:
                if not args.catalog:
                    raise InputError("离线修改需要显式提供--catalog，不静默复用旧价格")
                catalog = read_json(args.catalog)
                result = compose_v2(request, catalog) if v2 else compose(request, catalog)
            difference = compare_results(state, result)
            result["difference"] = difference
        elif args.command == "live-plan":
            raw = read_json(args.request)
            v2 = raw.get("schema_version") == 2
            if v2:
                result = MultiLivePlanner(client_from_args(args)).plan_v2(
                    apply_rank_options(raw, args)
                )
            else:
                reject_rank_options(args)
                result = LivePlanner(client_from_args(args)).plan(raw)
        else:
            raw = read_json(args.request)
            catalog = read_json(args.catalog)
            v2 = raw.get("schema_version") == 2
            if v2:
                result = compose_v2(apply_rank_options(raw, args), catalog)
            else:
                reject_rank_options(args)
                result = compose(raw, catalog)
        emit(result, args, v2)
        if difference and not args.json:
            print("\n变化说明：" + json.dumps(difference, ensure_ascii=False))
        return 0 if result.get("recommendations") else 2
    except WriteDenied as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return EXIT_REFUSED
    except orders.OrderError as exc:
        print(json.dumps({"status": "error", "message": str(exc), **exc.payload}, ensure_ascii=False))
        return exc.exit_code
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
