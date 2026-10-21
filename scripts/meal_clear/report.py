"""v2 多方案排名的人类可读文本输出。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .config import COMPACT_MIN_PEOPLE
from .grouping import group_people
from .ranking import PREVIEW_NOTE

MODE_LABELS = {
    "simulation": "模拟演示，不是真实菜单/价格",
    "live": "只读实时查询",
    "historical": "历史资料，非当前报价",
    "user_supplied": "用户提供资料，未在线核价",
}
ATTRIBUTE_NAMES = {
    "non_spicy": "不辣",
    "sugar_free_drink": "饮品无糖",
    "main_meat": "主食肉类",
}
STATES = {"verified": "已核实", "violated": "冲突", "unknown": "未知"}
FORBIDDEN_PHRASES = ("全网最低", "一定买得到", "库存保证")


def _yuan(cents: int) -> str:
    """分 → ¥元。"""
    return f"¥{cents / 100:.2f}"


def render_coupon_preview(preview: Dict[str, Any]) -> str:
    """三类券预览文本：仅名称/标签，不含券码。"""
    lines: List[str] = ["## 券预览（只读）"]
    for coupon in preview.get("store_usable", []):
        lines.append(f"- 本店可用：{coupon.get('title', '未命名券')}")
    for coupon in preview.get("claimable", []):
        lines.append(f"- 可领取：{coupon.get('couponName', '未命名券')}")
    for coupon in preview.get("held", []):
        lines.append(f"- 已领取：{coupon.get('title', '未命名券')}")
    if len(lines) == 1:
        lines.append("- 三类券均为空")
    lines.extend(f"- 说明：{note}" for note in preview.get("notes", []))
    return "\n".join(lines)


def _members_text(ids: List[str]) -> str:
    """成员 id 缩略：最多列前 3 个。"""
    shown = "、".join(ids[:3])
    return shown if len(ids) <= 3 else f"{shown}…共{len(ids)}人"


def _prefer_text(prefer: Dict[str, Any]) -> str:
    """偏好摘要。"""
    if not prefer:
        return "无偏好"
    return "；".join(
        f"{ATTRIBUTE_NAMES.get(k, k)}={v}" for k, v in sorted(prefer.items())
    )


def _hard_text(hard: Dict[str, Any]) -> str:
    """硬要求摘要。"""
    if not hard:
        return "无硬要求"
    return "；".join(f"{ATTRIBUTE_NAMES.get(k, k)}={v}" for k, v in sorted(hard.items()))


def _render_group_item(item: Dict[str, Any], groups: List[Dict[str, Any]]) -> List[str]:
    """一个名次按同质组汇总：组标签、人数、餐品×数量、硬要求核实状态。"""
    by_person = {a["person_id"]: a for a in item["assignments"]}
    lines: List[str] = []
    for group in groups:
        ids = [p["id"] for p in group["people"]]
        counts: Dict[str, int] = {}
        for pid in ids:
            a = by_person[pid]
            foods = "＋".join(f"{i['name']}×{i.get('quantity', 1)}" for i in a["items"])
            label = f"{a['name']}（{foods}）"
            counts[label] = counts.get(label, 0) + 1
        meal = "；".join(f"{label}×{n}" for label, n in sorted(counts.items()))
        checks = [c for pid in ids for c in by_person[pid]["checks"]]
        if not checks:
            state = "无硬要求"
        elif all(c["status"] == "verified" for c in checks):
            state = "硬要求全部已核实"
        else:
            state = "硬要求存在未核实项"
        matched = sum(by_person[pid]["preferences"]["matched"] for pid in ids)
        total = sum(by_person[pid]["preferences"]["total"] for pid in ids)
        lines.append(f"- {group['label']}（{len(ids)}人：{_members_text(ids)}）：{meal}")
        lines.append(
            f"  - {_hard_text(group['hard'])}：{state}；"
            f"{_prefer_text(group['prefer'])}，偏好满足 {matched}/{total}"
        )
    return lines


def render_ranking(result: Dict[str, Any], detail: bool = False) -> str:
    """渲染名次表、每名分配与理由、检查范围、券预览和边界提示。

    人数 ≥ COMPACT_MIN_PEOPLE 且未传 detail 时按同质组汇总；JSON 输出不受影响。
    """
    req = result["request"]
    meta = result["ranking_meta"]
    lines: List[str] = [
        "# 麦麦明白点 · 多方案排名",
        "",
        MODE_LABELS.get(result["mode"], result["mode"]),
        f"数据时间：{result['as_of']}",
        f"结果：{result['status']}",
        f"门店：{req['context']['store_code']}；取餐方式：{result['fulfillment']['label']}"
        f"{'（' + result['fulfillment']['address_label'] + '）' if result['fulfillment'].get('address_label') else ''}；"
        f"{len(req['people'])} 位用餐者；预算 {_yuan(req['budget_cents'])}",
        f"排序规则：{meta['rule']}",
        "",
    ]
    ranking = result.get("ranking", [])
    compact = len(req["people"]) >= COMPACT_MIN_PEOPLE and not detail
    if ranking:
        lines.append("## 名次一览")
        lines.append("名次 | 总价 | 比第1名 | 偏好 | 券依赖")
        for item in ranking:
            gap = "—" if item["rank"] == 1 else "+" + _yuan(item["gap_vs_first_cents"])
            lines.append(
                f"{item['rank']} | {_yuan(item['total_cents'])} | {gap} | "
                f"{item['preference_matches']}/{item['preference_total']} | "
                f"{item['coupon_count']}"
            )
        lines.append("")
        groups = group_people(req["people"]) if compact else []
        if compact:
            lines.append(
                f"（共 {len(req['people'])} 位用餐者，已按 {len(groups)} 个同质组汇总；"
                "加 --detail 展开逐人，--json 含完整逐人分配）"
            )
            lines.append("")
        for item in ranking:
            lines.append(f"## 第{item['rank']}名")
            lines.append(
                f"本次报价（未下单）：{_yuan(item['total_cents'])}；"
                f"金额依据：{item['price_status']}／{item.get('pricing_source') or '来源未标记'}"
            )
            if compact:
                lines.extend(_render_group_item(item, groups))
            for assignment in [] if compact else item["assignments"]:
                foods = "＋".join(
                    f"{i['name']}×{i.get('quantity', 1)}" for i in assignment["items"]
                )
                lines.append(f"- {assignment['person_id']}：{foods}")
                for check in assignment["checks"]:
                    name = ATTRIBUTE_NAMES.get(check["attribute"], check["attribute"])
                    lines.append(
                        f"  - {name}={check['expected']}：{STATES[check['status']]}"
                    )
            lines.append("理由：")
            lines.extend(f"- {reason}" for reason in item["reasons"])
            lines.append("")
    else:
        no_result = result.get("no_result") or {}
        lines.append("没有可确认的预算内方案，未自动放宽要求。")
        if no_result:
            lines.append(f"原因类别：{no_result['category']}；{no_result['detail']}")
        lines.append("")
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
        lines.append("")
    lines.append("## 检查范围")
    lines.append(meta["checked"]["statement"])
    preview = result.get("coupon_preview") or {}
    if any(preview.get(k) for k in ("claimable", "held", "store_usable")):
        lines.append("")
        lines.append(render_coupon_preview(preview))
    lines.extend(
        [
            "",
            "边界：只读比较，不下单、不领券、不支付；" + PREVIEW_NOTE + "。",
        ]
    )
    return "\n".join(lines)
