"""团餐纯函数：人均预算拆分与预约时间解析（无网络调用）。"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from . import config
from .config import RESERVATION_FORMAT
from .core import InputError


def parse_reservation(text: Any) -> datetime:
    """严格解析 yyyy-MM-dd HH:mm；不接受秒、时区或其他格式。"""
    if not isinstance(text, str) or len(text) != 16:
        raise InputError("reservation_date 格式必须为 yyyy-MM-dd HH:mm")
    try:
        return datetime.strptime(text, RESERVATION_FORMAT)
    except ValueError:
        raise InputError("reservation_date 格式必须为 yyyy-MM-dd HH:mm") from None


def split_budget(req: Dict[str, Any]) -> Dict[str, int]:
    """人均预算拆分（整数分）。

    给人均 p：总预算 = p×人数，余数 0；给总预算 T：人均 = T//人数（向下取整），
    余数 T%人数 留在总预算内。硬约束始终是总预算。
    """
    people = len(req["people"])
    per_given = req.get("per_person_budget_cents")
    if per_given is not None:
        return {
            "people_count": people,
            "total_cents": per_given * people,
            "per_person_cents": per_given,
            "remainder_cents": 0,
        }
    total = req["budget_cents"]
    per, remainder = divmod(total, people)
    if per < 1:
        raise InputError("总预算不足以让每人至少 1 分")
    return {
        "people_count": people,
        "total_cents": total,
        "per_person_cents": per,
        "remainder_cents": remainder,
    }


# ---------------------------------------------------------------------------
# 提交 3：助餐服务、促销提示、搭配启发、理由（纯函数，无网络；促销金额只用于展示与大小比较，
# 本模块不导出任何返回金额的函数，价格一律来自 calculate-price）
# ---------------------------------------------------------------------------
ROLE_TEXT = {"main": "主食", "side": "小食", "drink": "饮料"}
PROMO_NOTE = "促销规则仅作提示（来自 query-promotions 原始规则），价格一律以官方 calculate-price 核价为准"


class NeedGmServiceCode(InputError):
    """团餐必须先由用户选定助餐服务；携带可选项，工具不替用户选择、不采用官方默认项。"""

    def __init__(self, message: str, options: List[Dict[str, Any]]) -> None:
        """options 为 sanitize_assistance 的结果。"""
        super().__init__(message)
        self.options = options


def _yuan(cents: int) -> str:
    """分 → ¥元文本。"""
    return f"¥{cents / 100:.2f}"


def _rows(payload: Any) -> Any:
    """取 data 字段；允许直接传列表。"""
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def _text(value: Any) -> Optional[str]:
    """标量 → 字符串；其他类型丢弃。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    return None


def sanitize_assistance(payload: Any) -> List[Dict[str, Any]]:
    """query-meal-assistance 响应 → 助餐服务选项（白名单字段；结构异常即报错，不猜测）。"""
    data = _rows(payload)
    items = data.get("mealAssistanceItems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InputError("助餐服务结构不匹配，已停止，未猜测服务编码")
    options: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        gm = item.get("gmServiceCode")
        if not isinstance(gm, str) or not gm.strip() or gm in seen:
            continue
        seen.add(gm)
        names: List[str] = []
        raw_items = item.get("serviceItems")
        for sub in raw_items[:10] if isinstance(raw_items, list) else []:
            name = _text(sub.get("name") if isinstance(sub, dict) else sub)
            if name:
                names.append(name)
        options.append(
            {
                "index": len(options) + 1,
                "gm_service_code": gm,
                "name": _text(item.get("gmServiceName")) or "未命名助餐服务",
                "enable": item.get("enable") in (True, 1),
                "official_default": item.get("selected") in (True, 1),
                "unusable_reason": _text(item.get("unusableReason")) or "",
                "service_items": names,
            }
        )
    return options


def format_gm_options(options: List[Dict[str, Any]]) -> str:
    """可选项的中文清单（用于追问）。"""
    if not options:
        return "当前门店没有返回任何助餐服务"
    lines = []
    for opt in options:
        state = "可选" if opt["enable"] else "不可用" + (
            f"（{opt['unusable_reason']}）" if opt["unusable_reason"] else ""
        )
        default = "；官方默认项，仅供参考，不会自动采用" if opt["official_default"] else ""
        lines.append(f"{opt['index']}) {opt['name']} gm_service_code={opt['gm_service_code']}：{state}{default}")
    return "；".join(lines)


def select_gm_service(options: List[Dict[str, Any]], requested: Optional[str]) -> Dict[str, Any]:
    """校验用户选定的助餐服务；缺失、不在列表、不可用都抛 NeedGmServiceCode，绝不采用 selected 默认项。"""
    if not requested:
        raise NeedGmServiceCode(
            "团餐需要先选择助餐服务（fulfillment.gm_service_code）。工具不会替你选默认项。可选："
            + format_gm_options(options),
            options,
        )
    for opt in options:
        if opt["gm_service_code"] == requested:
            if not opt["enable"]:
                raise NeedGmServiceCode(
                    "所选助餐服务当前不可用，请改选其他服务。可选：" + format_gm_options(options),
                    options,
                )
            return opt
    raise NeedGmServiceCode(
        "所选 gm_service_code 不在当前门店的助餐服务列表内，未猜测替代项。可选："
        + format_gm_options(options),
        options,
    )


def _to_cents(text: Any) -> Optional[int]:
    """元字符串 → 分；仅供提示时与核价总额做大小比较，解析失败返回 None（不比较）。"""
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value * 100)


def sanitize_promotions(payload: Any, cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """query-promotions 响应 → 促销规则（白名单字段，金额与 discount 一律保留官方原始字符串）。"""
    limit = config.GROUP_PROMO_RULE_CAP if cap is None else cap
    data = _rows(payload)
    if not isinstance(data, list):
        raise InputError("促销规则结构不匹配")
    rules: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        detail = row.get("ruleDetail") if isinstance(row.get("ruleDetail"), dict) else {}
        reduce = detail.get("orderReduce") if isinstance(detail.get("orderReduce"), dict) else None
        discount = detail.get("orderDiscount") if isinstance(detail.get("orderDiscount"), dict) else None
        rule: Dict[str, Any] = {
            "promotion_id": _text(row.get("promotionId")) or "",
            "kind": "满减" if reduce else ("满折" if discount else "其他"),
            "gm_service_code": _text(row.get("gmServiceCode")) or "",
            "start_time": _text(row.get("startTime")) or "",
            "end_time": _text(row.get("endTime")) or "",
        }
        products = row.get("products")
        if isinstance(products, list) and products:
            all_products = any(isinstance(p, dict) and p.get("type") == 3 for p in products)
            rule["scope"] = "全部商品" if all_products else "部分商品（以官方为准）"
        else:
            rule["scope"] = "未标注"
        if reduce:
            tiers = []
            infos = reduce.get("reduceInfo")
            for info in infos if isinstance(infos, list) else []:
                if isinstance(info, dict):
                    threshold = _text(info.get("startDiscountPoint"))
                    amount = _text(info.get("reduceAmount"))
                    if threshold and amount:
                        tiers.append({"threshold_yuan": threshold, "reduce_yuan": amount})
            rule["tiers"] = tiers
        if discount:
            rule["discount"] = _text(discount.get("discount")) or ""
            rule["discount_threshold_yuan"] = _text(discount.get("startDiscountPoint")) or ""
        rules.append(rule)
        if len(rules) >= limit:
            break
    return rules


def promo_hints(rules: List[Dict[str, Any]], total_cents: int, gm_service_code: Optional[str]) -> List[str]:
    """促销提示文案（只返回字符串）：规则原样展示，与官方核价总额只做门槛大小对比。"""
    hints: List[str] = []
    other = 0
    for rule in rules:
        bound = rule.get("gm_service_code") or ""
        if bound and bound != (gm_service_code or ""):
            other += 1
            continue
        if rule.get("tiers"):
            for tier in rule["tiers"]:
                cents = _to_cents(tier["threshold_yuan"])
                reach = (
                    "" if cents is None
                    else ("，核价总额已达到该门槛" if total_cents >= cents else "，核价总额未达到该门槛")
                )
                hints.append(
                    f"促销提示：满 ¥{tier['threshold_yuan']} 减 ¥{tier['reduce_yuan']}{reach}；"
                    f"该规则是否已体现在核价中待验证，以官方核价（{_yuan(total_cents)}）为准，本工具未据此改动价格"
                )
        elif rule.get("kind") == "满折":
            hints.append(
                f"促销提示：满 ¥{rule.get('discount_threshold_yuan') or '?'} 折扣，官方原始 discount={rule.get('discount') or '?'}"
                f"（含义待验证，不做换算）；以官方核价（{_yuan(total_cents)}）为准，本工具未据此改动价格"
            )
    if other:
        hints.append(f"另有 {other} 条促销规则适用于其他助餐服务，未展示")
    return hints


def band_for(per_person_cents: int) -> Dict[str, Any]:
    """按人均预算取官方搭配建议档，区间 [下限, 上限)（按人均划分属推测）。"""
    for lower, upper, label, combos in config.GROUP_BAND_TABLE:
        if per_person_cents >= lower and (upper is None or per_person_cents < upper):
            return {
                "label": label,
                "lower_cents": lower,
                "upper_cents": upper,
                "combos": [list(c) for c in combos],
                "text": " 或 ".join("＋".join(ROLE_TEXT[r] for r in c) for c in combos),
            }
    raise InputError("人均预算无法归入搭配档")


def template_fit(offer: Dict[str, Any], band: Dict[str, Any]) -> int:
    """单人餐品与建议组合的契合度：0 为完全一致，越负越偏离；最高档额外扣重复小食。"""
    roles: Dict[str, int] = {}
    for item in offer.get("items", []):
        roles[item["role"]] = roles.get(item["role"], 0) + item.get("quantity", 1)
    best = None
    for combo in band["combos"]:
        want: Dict[str, int] = {}
        for role in combo:
            want[role] = want.get(role, 0) + 1
        gap = sum(abs(roles.get(r, 0) - want.get(r, 0)) for r in set(roles) | set(want))
        best = gap if best is None else min(best, gap)
    penalty = 0
    if band["upper_cents"] is None:
        sides = [i["name"] for i in offer.get("items", []) if i["role"] == "side"]
        penalty = len(sides) - len(set(sides))
    return -((best or 0) + penalty)


def annotate_group_fit(cands: List[Dict[str, Any]], req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """给整单候选写 group_fit，仅影响送核价的候选顺序与理由；关闭开关或非团餐则原样返回。"""
    if not config.GROUP_BAND_ENABLED or req["fulfillment"]["type"] != "group":
        return cands
    band = band_for(split_budget(req)["per_person_cents"])
    for cand in cands:
        cand["group_fit"] = sum(template_fit(o, band) for o in cand["offers"])
    return cands


def build_group_section(req: Dict[str, Any]) -> Dict[str, Any]:
    """result['group'] 的基础部分（纯请求推导）：预算拆分、搭配档、预约、说明。"""
    parts = split_budget(req)
    band = band_for(parts["per_person_cents"])
    ful = req["fulfillment"]
    notes = [PROMO_NOTE, "搭配档按人均预算划分，属推测；当前版本只生成完整套餐候选"]
    if band["lower_cents"] == 0:
        notes.append("官方建议该档为纯小食，当前版本不生成纯小食候选")
    return {
        "budget": parts,
        "band": {"label": band["label"], "suggestion": band["text"]},
        "reservation_date": ful.get("reservation_date"),
        "notes": notes,
    }


def group_reasons(item: Dict[str, Any], req: Dict[str, Any]) -> List[str]:
    """团餐理由：人均预算与余数、方案人均、搭配契合度（固定模板）。"""
    parts = split_budget(req)
    band = band_for(parts["per_person_cents"])
    people = parts["people_count"]
    per_plan, left = divmod(item["total_cents"], people)
    reasons = [
        f"团餐人均预算 {_yuan(parts['per_person_cents'])}（向下取整），余 {parts['remainder_cents']} 分留在总预算内；总预算是硬约束",
        f"方案核价 {_yuan(item['total_cents'])}，折合人均 {_yuan(per_plan)}（向下取整），余 {left} 分",
    ]
    if "group_fit" in item:
        verdict = "每人餐品结构与建议一致" if item["group_fit"] == 0 else "每人餐品结构与建议有出入，仅作参考"
        reasons.append(f"对照官方搭配建议「{band['label']}」（{band['text']}，按人均划分属推测）：{verdict}")
    return reasons
