"""v2 多方案排名的人类可读文本输出。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

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


def render_ranking(result: Dict[str, Any]) -> str:
    """渲染名次表、每名分配与理由、检查范围、券预览和边界提示。"""
    req = result["request"]
    meta = result["ranking_meta"]
    lines: List[str] = [
        "# 麦麦明白点 · 多方案排名",
        "",
        MODE_LABELS.get(result["mode"], result["mode"]),
        f"数据时间：{result['as_of']}",
        f"结果：{result['status']}",
        f"门店：{req['context']['store_code']}；取餐方式：{result['fulfillment']['label']}；"
        f"{len(req['people'])} 位用餐者；预算 {_yuan(req['budget_cents'])}",
        f"排序规则：{meta['rule']}",
        "",
    ]
    ranking = result.get("ranking", [])
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
        for item in ranking:
            lines.append(f"## 第{item['rank']}名")
            lines.append(
                f"本次报价（未下单）：{_yuan(item['total_cents'])}；"
                f"金额依据：{item['price_status']}／{item.get('pricing_source') or '来源未标记'}"
            )
            for assignment in item["assignments"]:
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
    if preview.get("store_usable"):
        lines.append("")
        lines.append("## 券预览")
        for coupon in preview["store_usable"]:
            lines.append(f"- 本店可用：{coupon['title']}")
    lines.extend(
        [
            "",
            "边界：只读比较，不下单、不领券、不支付；"
            "领券属后续阶段（P1），未领取的券不计入本次报价。",
        ]
    )
    return "\n".join(lines)
