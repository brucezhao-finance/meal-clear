"""订单摘要、领券预演、变化清单的人类可读文本（金额按元显示；不含券码/地址文本）。"""

from __future__ import annotations

from typing import Any, Dict, List

from .config import (
    COMPACT_MIN_PEOPLE,
    EXIT_DUPLICATE,
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_OK,
    EXIT_RECONFIRM,
    EXIT_REFUSED,
    EXIT_UNKNOWN,
)

FEE_LABELS = {
    "productPrice": "商品",
    "deliveryPrice": "配送费",
    "packingPrice": "包装费",
    "tablewarePrice": "餐具费",
}
STATE_LABELS = {
    "none": "未执行",
    "not_required": "无需领券",
    "pending": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "unknown": "结果未知",
    "consistent": "与摘要一致",
    "changed": "已变化",
    "error": "核价失败",
    "created": "已创建",
}
EXIT_HINTS = {
    EXIT_OK: "成功或预演完成",
    EXIT_ERROR: "输入、认证或协议错误（含官方明确的业务失败）",
    EXIT_NO_PLAN: "没有可确认的方案",
    EXIT_RECONFIRM: "需要重新确认：摘要过期、内容或价格变化、超预算、需选择取餐方式，或已生成新摘要",
    EXIT_REFUSED: "安全门或前置条件拒绝，本次未发出写请求",
    EXIT_DUPLICATE: "重复执行被拒：账本已有该步骤记录",
    EXIT_UNKNOWN: "写调用结果未知：先核实，禁止重试",
}


def yuan(cents: Any) -> str:
    """分 → ¥元；缺失显示为未返回，不当作 0。"""
    return "未返回" if cents is None else f"¥{cents / 100:.2f}"


def _items_text(items: List[Dict[str, Any]]) -> str:
    return "＋".join(f"{i['name']}×{i.get('quantity', 1)}" for i in items)


def _members(ids: List[str]) -> str:
    shown = "、".join(ids[:3])
    return shown if len(ids) <= 3 else f"{shown}…共{len(ids)}人"


def render_summary(summary: Dict[str, Any], detail: bool = False) -> str:
    """订单摘要：N≤12 逐人，N≥13 按同质组；--detail 展开逐人。"""
    ful = summary["fulfillment"]
    store = summary["store"]
    name = store["name"] or "名称未核实"
    lines: List[str] = [
        "# 订单摘要（待你确认）",
        "",
        f"取餐方式：{ful['label']}" + (f"（{ful['address_label']}）" if ful.get("address_label") else ""),
        f"门店：{store['store_code']}（{name}）",
        f"用餐人数：{summary['people_count']}",
        "",
        "## 餐品",
    ]
    compact = summary["people_count"] >= COMPACT_MIN_PEOPLE and not detail
    if compact:
        for group in summary["groups"]:
            meals = "；".join(f"{m['name']}（{_items_text(m['items'])}）×{m['count']}" for m in group["meals"])
            lines.append(f"- {group['label']}（{group['size']}人：{_members(group['person_ids'])}）：{meals}")
    else:
        for person in summary["per_person"]:
            lines.append(f"- {person['person_id']}：{person['name']}（{_items_text(person['items'])}）")
    lines.append("")
    lines.append("## 商品行（将提交核价的数量）")
    for row in summary["lines"]:
        tag = f"；使用券「{row['coupon']['title']}」" if row.get("coupon") else ""
        lines.append(f"- {row['name']} × {row['quantity']}{tag}")
    lines.append("")
    lines.append(f"官方核价应付总额：{yuan(summary['payable_cents'])}（预算 {yuan(summary['budget_cents'])}）")
    parts = [f"{label} {yuan(summary['fee_breakdown'].get(key))}" for key, label in FEE_LABELS.items()]
    lines.append("费用拆分：" + "；".join(parts))
    take = summary["take_way"]
    if take.get("required"):
        chosen = take.get("selected_index")
        title = next((o["title"] for o in take["options"] if o["index"] == chosen), "已选")
        lines.append(f"取餐选项：第 {chosen} 项（{title}）")
    claim = summary["claim"]
    lines.append("")
    if claim["required"]:
        total = claim["total"]
        names = "；".join(f"{c['name']}×{c['count']}" for c in claim["coupons"])
        lines.append(f"将领取的券（共 {total} 张，一键领全部，无法逐张选择，不可撤销）：{names}")
    elif claim.get("skipped"):
        lines.append("领券：已选择跳过")
    else:
        lines.append("领券：当前没有可领取的券")
    lines.append("")
    lines.append(f"报价时间：{summary['quoted_at']}")
    lines.append(f"数据来源模式：{summary['mode']}／{summary['data_source']}")
    lines.append(f"过期时间：{summary['expires_at']}（{summary['ttl_seconds']} 秒内有效，过期必须重新核价）")
    lines.append(f"摘要编号（digest）：{summary['digest']}")
    state = summary.get("state")
    if state:
        lines.append(
            "当前状态："
            f"领券{STATE_LABELS.get(state['claim'], state['claim'])}；"
            f"领券后核价{STATE_LABELS.get(state['requote'], state['requote'])}；"
            f"下单{STATE_LABELS.get(state['order'], state['order'])}"
        )
    lines.append("")
    lines.append("## 下一步（需你明确同意后才可执行，不要替你确认）")
    short = summary["digest"][:12]
    claim_state = (state or {}).get("claim", "none")
    claim_pending = claim["required"] and claim_state not in ("succeeded", "not_required")
    if claim_pending:
        lines.append(f"- 预演领券（只读）：order-claim --digest {short}")
        lines.append(f"- 确认后领券：order-claim --digest {short} --execute --confirm-digest {summary['digest']}")
    else:
        lines.append(f"- 预演下单（只读）：order-place --digest {short}")
        lines.append(f"- 确认后下单：order-place --digest {short} --execute --confirm-digest {summary['digest']}")
    lines.append(f"说明：{summary['execution']['message']}。")
    return "\n".join(lines)


def render_claim_preview(preview: Dict[str, Any]) -> str:
    """领券预演：一键领全部、无法逐张选择、不可撤销、本次未调用 auto-bind-coupons。"""
    lines = [
        "# 领券预演（只读）",
        "",
        f"摘要：{preview['digest'][:12]}…",
        f"结果：{preview['message']}",
    ]
    coupons = preview.get("claimable_now")
    if coupons is not None:
        lines.append("")
        lines.append("当前可领取的券：" + ("；".join(f"{c['name']}×{c['count']}" for c in coupons) or "无"))
    lines.extend(
        [
            "",
            "领券规则：只能一键领全部，无法逐张选择；领取后不可撤销。",
            "本次未调用 auto-bind-coupons，没有领取任何券。",
        ]
    )
    status = preview["status"]
    if status == "preview":
        lines.append(
            f"需你明确同意后再执行：order-claim --digest {preview['digest'][:12]} "
            f"--execute --confirm-digest {preview['digest']}"
        )
    elif status in ("expired", "superseded", "claimable_changed"):
        lines.append("请先重新运行 order-summary 生成新摘要并重新确认。")
    return "\n".join(lines)


def render_changes(changes: List[Dict[str, Any]]) -> str:
    """价格/商品/券依赖变化清单；金额用元显示。"""
    lines = ["## 变化清单"]
    for change in changes:
        kind = change["kind"]
        if kind == "price":
            delta = change["delta_cents"]
            sign = "+" if delta > 0 else "-"
            lines.append(
                f"- 应付总额：{yuan(change['before'])} → {yuan(change['after'])}（{sign}{yuan(abs(delta))}）"
            )
        elif kind == "fee":
            lines.append(
                f"- {FEE_LABELS.get(change['field'], change['field'])}：{yuan(change['before'])} → {yuan(change['after'])}"
            )
        elif kind == "items":
            lines.append(f"- 商品行变化：{'、'.join(change['before'])} → {'、'.join(change['after'])}")
        elif kind == "menu":
            lines.append(f"- 菜单或券变化：{change['after']}")
        elif kind == "take_way":
            lines.append("- 已选取餐方式不在最新选项内")
        else:
            lines.append(f"- {kind}：{change.get('field')} 已变化")
    return "\n".join(lines)


def render_result(result: Dict[str, Any]) -> str:
    """领券执行/重新确认结果文本。"""
    lines = [f"# {result['message']}"]
    if result.get("changes"):
        lines.extend(["", render_changes(result["changes"])])
    for note in result.get("notes", []):
        lines.append(f"- 提示：{note}")
    if result.get("new_digest"):
        lines.extend(["", f"已生成新摘要，旧确认作废。新摘要编号：{result['new_digest']}"])
        if result.get("summary"):
            lines.extend(["", render_summary(result["summary"])])
    return "\n".join(lines)


def render_place_preview(preview: Dict[str, Any]) -> str:
    """下单预演：展示将提交的内容；明确尚未调用、不自动付款。"""
    lines = ["# 下单预演（只读）", "", f"摘要：{preview['digest'][:12]}…", f"结果：{preview['message']}"]
    sub = preview.get("will_submit")
    if sub and preview["status"] in ("preview", "reconfirm"):
        name = sub["store_name"] or "名称未核实"
        lines.extend(["", f"门店：{sub['store_code']}（{name}）", f"取餐方式：{sub['fulfillment']}", "将提交的商品："])
        lines.extend(f"- {row['name']} × {row['quantity']}" for row in sub["lines"])
        lines.append(f"应付总额：{yuan(sub['payable_cents'])}（订单创建后为待支付，需你自己付款）")
    if preview.get("changes"):
        lines.extend(["", render_changes(preview["changes"])])
    for note in preview.get("notes", []):
        lines.append(f"- 提示：{note}")
    for blocker in preview.get("blockers", []):
        lines.append(f"- 当前不能下单：{blocker}")
    lines.extend(["", preview["note"] + "。"])
    if preview["status"] == "preview" and preview.get("ready"):
        lines.append(
            f"需你明确同意后再执行：order-place --digest {preview['digest'][:12]} "
            f"--execute --confirm-digest {preview['digest']}"
        )
    elif preview["status"] in ("expired", "superseded", "reconfirm"):
        lines.append("请先重新运行 order-summary 生成新摘要并重新确认。")
    return "\n".join(lines)


def render_created(result: Dict[str, Any]) -> str:
    """创建订单那一次的输出：唯一会打印订单号与支付信息的地方。"""
    order = result["order"]
    created = result["created_order"]
    if order["status_code"] == "1":
        state = "状态：待支付（官方 orderStatus=1）"
    elif order["status_code"] is not None:
        state = f"官方返回状态：{order['status_code']}（{order['status_label']}）"
    else:
        state = f"官方返回状态：{order['official_status_text'] or '未返回'}"
    lines = [
        "# 订单已创建，未付款",
        "",
        state,
        "本工具不会自动付款，也不会自动取消订单；请在 App 订单列表或下方支付信息中自行完成支付。",
        f"摘要应付：{yuan(order['payable_cents'])}；官方返回金额：{yuan(order['official_amount_cents'])}",
    ]
    if order["amount_check"] != "match":
        lines.append("警告：官方返回金额与摘要应付不一致或未返回，支付前请核对金额。")
    if order.get("expire_pay_time"):
        lines.append(f"支付截止：{order['expire_pay_time']}")
    lines.extend(["", f"订单号：{created['orderId']}（仅本次显示，之后只显示打码尾号）"])
    if created.get("payH5Url"):
        lines.append(f"支付链接：{created['payH5Url']}（仅本次显示，不写入账本）")
    if created.get("payId"):
        lines.append(f"支付编号：{created['payId']}")
    lines.extend(["", f"之后可用 order-status --digest {result['digest'][:12]} 查询状态。"])
    return "\n".join(lines)


def render_order_status(result: Dict[str, Any]) -> str:
    """订单查询：只含状态、金额、商品、时间、门店；订单号打码。"""
    local = result.get("local")
    lines = ["# 订单状态"]
    if result["status"] in ("local_only", "not_found"):
        if result.get("order_id_masked"):
            lines.append(f"订单号：{result['order_id_masked']}")
        lines.append(result["message"])
        if local:
            lines.append(f"本地账本：下单{STATE_LABELS.get(local['order_state'], local['order_state'])}")
        return "\n".join(lines)
    order = result["order"]
    lines.append(f"订单号：{result['order_id_masked']}")
    if order["status_code"] is not None:
        lines.append(f"状态：{order['status_label']}（官方 orderStatus={order['status_code']}）")
    else:
        lines.append(f"官方返回状态：{order['official_status_text'] or '未返回'}")
    lines.append(f"实付/应付金额：{yuan(order['amount_cents'])}")
    if order["items"]:
        lines.append("商品：" + "；".join(f"{i['name']}×{i['quantity']}" for i in order["items"]))
    for label, key in (("门店", "store_name"), ("下单时间", "created_at"), ("支付截止", "expire_pay_time")):
        if order.get(key):
            lines.append(f"{label}：{order[key]}")
    lines.append("本工具不会付款或取消订单。")
    return "\n".join(lines)


def render_notice(result: Dict[str, Any]) -> str:
    """不落账的拒绝提示（超预算、需选取餐方式等）。"""
    lines = [f"# {result['message']}"]
    if result.get("total_cents") is not None:
        lines.append(f"官方核价：{yuan(result['total_cents'])}；预算：{yuan(result.get('budget_cents'))}")
    for option in result.get("options", []):
        lines.append(f"- 选项 {option['index']}：{option['title']} {option['subtitle']}".rstrip())
    return "\n".join(lines)


def exit_hint(code: int) -> str:
    """退出码说明（中文）。"""
    return EXIT_HINTS.get(code, "未知退出码")
