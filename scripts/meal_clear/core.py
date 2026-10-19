"""逐人约束、有限组合、完整金额与请求修改。"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


class InputError(ValueError):
    """输入不完整或超出支持范围。"""


def money_to_cents(value: Any, unit: str) -> int:
    """显式识别元/分；拒绝舍入、负数、布尔值和非有限数字。"""
    if isinstance(value, bool) or unit not in {"CNY", "cent"}:
        raise InputError("金额单位必须明确为 CNY 或 cent，金额不能是布尔值")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InputError("金额必须为数字") from None
    if not amount.is_finite() or amount < 0:
        raise InputError("金额必须有限且非负")
    scaled = amount * (100 if unit == "CNY" else 1)
    if scaled != scaled.to_integral_value():
        raise InputError("金额精度超出分，不自动四舍五入")
    return int(scaled)


def integer(value: Any, label: str, minimum: int = 0) -> int:
    """拒绝布尔、字符串和小数形式的整数参数。"""
    if type(value) is not int or value < minimum:
        raise InputError(f"{label}必须是至少{minimum}的整数")
    return value


def validate_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """校验单/双人、明确上下文和可解释的原子属性约束。"""
    req = copy.deepcopy(request)
    if set(req) - {"context", "budget_cents", "people"}:
        raise InputError("请求包含不支持的字段")
    if not isinstance(req.get("context"), dict):
        raise InputError("需要门店与用餐上下文")
    context = req["context"]
    required = {"store_code", "channel", "meal_time"}
    if set(context) != required or any(
        not isinstance(context[k], str) or not context[k].strip() for k in required
    ):
        raise InputError("上下文必须包含非空 store_code、channel、meal_time")
    if context["channel"] != "pickup":
        raise InputError("首版只支持到店自取 pickup")
    integer(req.get("budget_cents"), "预算", 1)
    people = req.get("people")
    if not isinstance(people, list) or not 1 <= len(people) <= 2:
        raise InputError("首版支持1或2位用餐者")
    seen = set()
    for person in people:
        if not isinstance(person, dict) or set(person) - {"id", "hard", "prefer"}:
            raise InputError("用餐者仅支持 id、hard、prefer")
        pid = person.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise InputError("每位用餐者需要不同的非空id")
        if pid in seen:
            raise InputError("用餐者 id 重复")
        seen.add(pid)
        for key in ("hard", "prefer"):
            person.setdefault(key, {})
            if not isinstance(person[key], dict) or len(person[key]) > 12:
                raise InputError("约束/偏好必须为最多12项的对象")
            for attribute, value in person[key].items():
                if not isinstance(attribute, str) or type(value) not in (
                    str,
                    bool,
                    int,
                ):
                    raise InputError("属性必须是字符串键及字符串/布尔/整数值")
    return req


def fingerprint(request: Dict[str, Any]) -> str:
    """将完整请求绑定到一次方案，任何修改都会使旧报价身份失效。"""
    raw = json.dumps(validate_request(request), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def amend_request(request: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """仅改变显式指定字段；null删除一个约束，未知用户/字段报错。"""
    result = validate_request(request)
    if not isinstance(patch, dict) or set(patch) - {
        "context",
        "budget_cents",
        "people",
    }:
        raise InputError("修改只支持 context、budget_cents、people")
    if "context" in patch:
        if not isinstance(patch["context"], dict):
            raise InputError("context修改必须为对象")
        result["context"].update(patch["context"])
    if "budget_cents" in patch:
        result["budget_cents"] = patch["budget_cents"]
    updates = patch.get("people", {})
    if not isinstance(updates, dict):
        raise InputError("people修改为用餐者id到修改对象的映射")
    by_id = {p["id"]: p for p in result["people"]}
    for pid, edit in updates.items():
        if (
            pid not in by_id
            or not isinstance(edit, dict)
            or set(edit) - {"hard", "prefer"}
        ):
            raise InputError("修改包含未知用餐者或字段")
        for key, changes in edit.items():
            if not isinstance(changes, dict):
                raise InputError("约束修改必须为对象")
            for attr, value in changes.items():
                if value is None:
                    by_id[pid][key].pop(attr, None)
                else:
                    by_id[pid][key][attr] = value
    return validate_request(result)


def validate_catalog(catalog: Dict[str, Any]) -> None:
    """校验受信结构；约束属性均必须附有可追溯来源。"""
    if catalog.get("mode") not in {"simulation", "live", "historical", "user_supplied"}:
        raise InputError("菜单必须标注数据模式")
    if not catalog.get("as_of") or not isinstance(catalog.get("offers"), list):
        raise InputError("菜单需要获取时间和offers列表")
    seen = set()
    for offer in catalog["offers"]:
        if not isinstance(offer, dict):
            raise InputError("每个套餐/组合必须为对象")
        oid = offer.get("id")
        if not isinstance(oid, str) or not oid or oid in seen:
            raise InputError("套餐/组合id必须唯一")
        seen.add(oid)
        if not offer.get("name") or not offer.get("items"):
            raise InputError("组合需要名称与餐品清单")
        items = offer["items"]
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise InputError("餐品清单必须为对象列表")
        facts = offer.get("facts", {})
        if not isinstance(facts, dict):
            raise InputError("属性证据集合必须为对象")
        for role in ("main", "drink"):
            if not any(i.get("role") == role for i in items):
                raise InputError("每人组合必须包含主食与饮品，不静默减少餐品")
        for fact in facts.values():
            if (
                not isinstance(fact, dict)
                or not fact.get("source")
                or "value" not in fact
            ):
                raise InputError("属性证据需要value与source")
        price = offer.get("price_cents")
        if price is not None:
            integer(price, "组合价")
        integer(offer.get("card_fee_cents", 0), "新卡费")
        for item in offer["items"]:
            if not item.get("code") or not item.get("name"):
                raise InputError("餐品编码与名称不能为空")
            integer(item.get("quantity", 1), "餐品数量", 1)


def check_person(person: Dict[str, Any], offer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按属性事实核对个人约束；未提供证据即unknown。"""
    checks = []
    for key, expected in person["hard"].items():
        fact = offer.get("facts", {}).get(key)
        status = "unknown"
        if fact is not None and fact.get("value") is not None:
            status = (
                "verified"
                if type(fact["value"]) is type(expected) and fact["value"] == expected
                else "violated"
            )
        checks.append(
            {
                "attribute": key,
                "expected": expected,
                "status": status,
                "source": fact.get("source") if fact else None,
            }
        )
    return checks


def quote_signature(context: Dict[str, Any], offers: List[Dict[str, Any]]) -> str:
    """绑定报价到上下文与具体商品参数，而非仅绑定名字。"""
    wire = [
        {
            "id": o["id"],
            "wire_items": o.get("wire_items"),
            "items": o["items"],
            "card_fee_cents": o.get("card_fee_cents", 0),
        }
        for o in offers
    ]
    raw = json.dumps({"context": context, "offers": wire}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def compose(
    request: Dict[str, Any],
    catalog: Dict[str, Any],
    quotes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在明确有界集合中枚举组合；没有报价不声称实时到手价。"""
    req = validate_request(request)
    validate_catalog(catalog)
    base = {
        "request": req,
        "request_id": fingerprint(req),
        "mode": catalog["mode"],
        "as_of": catalog["as_of"],
        "status": "no_verified_plan",
        "plans": [],
        "diagnostics": [],
        "search": {
            "catalog_offers": len(catalog["offers"]),
            "max_offers_per_person": 30,
            "max_combinations": 900,
        },
    }
    if req["context"] != catalog.get("context"):
        base["status"] = "context_mismatch"
        base["diagnostics"].append("门店、方式或时间不同；需要对应菜单和新报价")
        return base
    candidates = []
    for person in req["people"]:
        eligible = []
        failures = []
        for offer in catalog["offers"]:
            checks = check_person(person, offer)
            bad = [c for c in checks if c["status"] != "verified"]
            if bad:
                failures.append({"offer_id": offer["id"], "checks": bad})
                continue
            matches = sum(
                offer.get("facts", {}).get(k, {}).get("value") == v
                for k, v in person["prefer"].items()
            )
            eligible.append((offer, checks, matches))
        eligible.sort(
            key=lambda x: (
                -x[2],
                (
                    x[0].get("price_cents")
                    if x[0].get("price_cents") is not None
                    else 10**12
                ),
                x[0]["id"],
            )
        )
        candidates.append(eligible[:30])
        if not eligible:
            base["diagnostics"].append(
                {
                    "person_id": person["id"],
                    "reason": "没有证据完整且满足硬要求的组合",
                    "examples": failures[:5],
                }
            )
    if any(not c for c in candidates):
        return base
    plans = []
    for entries in itertools.product(*candidates):
        offers = [e[0] for e in entries]
        coupon_ids = [o["coupon_key"] for o in offers if o.get("coupon_key")]
        # 首版仅支持每单至多一张已确认的券，避免假设可叠加/重复使用。
        if len(coupon_ids) > 1:
            continue
        sig = quote_signature(req["context"], offers)
        assignments = [
            {
                "person_id": p["id"],
                "offer_id": o["id"],
                "name": o["name"],
                "items": o["items"],
                "checks": checks,
            }
            for p, (o, checks, _) in zip(req["people"], entries)
        ]
        plan = {
            "signature": sig,
            "assignments": assignments,
            "preference_matches": sum(e[2] for e in entries),
            "total_cents": None,
            "price_status": "unknown",
            "pricing_source": None,
            "new_card_cents": sum(o.get("card_fee_cents", 0) for o in offers),
            "steps": {
                "order_count": 1,
                "new_card_count": sum(bool(o.get("card_fee_cents")) for o in offers),
            },
            "within_budget": None,
        }
        q = (quotes or {}).get(sig)
        if catalog["mode"] == "live":
            if q and q.get("context") == req["context"] and q.get("signature") == sig:
                total = q.get("total_cents")
                if type(total) is int and total >= 0 and q.get("verified") is True:
                    plan.update(
                        total_cents=total,
                        price_status="official_verified",
                        pricing_source=q.get("source"),
                        quoted_at=q.get("quoted_at"),
                        fee_breakdown=q.get("fee_breakdown", {}),
                    )
        elif all(o.get("price_cents") is not None for o in offers):
            fees = catalog.get("order_fee_cents")
            if type(fees) is int and fees >= 0:
                total = (
                    sum(o["price_cents"] + o.get("card_fee_cents", 0) for o in offers)
                    + fees
                )
                plan.update(
                    total_cents=total,
                    price_status=catalog["mode"],
                    pricing_source=catalog.get("price_source"),
                )
        if plan["total_cents"] is not None:
            plan["within_budget"] = plan["total_cents"] <= req["budget_cents"]
        plans.append(plan)
    plans.sort(
        key=lambda p: (
            p["within_budget"] is not True,
            -p["preference_matches"],
            p["total_cents"] if p["total_cents"] is not None else 10**12,
            p["steps"]["new_card_count"],
            p["signature"],
        )
    )
    base["search"]["checked_combinations"] = len(plans)
    base["plans"] = plans
    feasible = [p for p in plans if p["within_budget"] is True]
    if feasible:
        base["status"] = "ready" if catalog["mode"] == "live" else catalog["mode"]
        # 不推荐价格更高、偏好不更好、还需多购卡的被支配方案。
        frontier = [
            p
            for p in feasible
            if not any(
                q["total_cents"] <= p["total_cents"]
                and q["preference_matches"] >= p["preference_matches"]
                and q["steps"]["new_card_count"] <= p["steps"]["new_card_count"]
                and (
                    q["total_cents"] < p["total_cents"]
                    or q["preference_matches"] > p["preference_matches"]
                    or q["steps"]["new_card_count"] < p["steps"]["new_card_count"]
                )
                for q in feasible
            )
        ]
        frontier.sort(
            key=lambda p: (
                p["total_cents"],
                -p["preference_matches"],
                p["steps"]["new_card_count"],
                p["signature"],
            )
        )
        choices = []
        baskets = set()
        for plan in frontier:
            basket = tuple(sorted(a["offer_id"] for a in plan["assignments"]))
            if basket not in baskets:
                choices.append(plan)
                baskets.add(basket)
        base["recommendations"] = choices[:2]
    elif plans and any(p["total_cents"] is None for p in plans):
        base["status"] = "needs_price_verification"
    elif plans:
        base["status"] = "over_budget_in_checked_set"
        base["diagnostics"].append("已检查组合超预算；未证明全门店无解，不自动放宽要求")
    return base


def compare_results(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """比较两个重新生成的结果，只有两边有价时才计算价差。"""
    old = (before.get("recommendations") or [])[:1]
    new = (after.get("recommendations") or [])[:1]
    differences = []
    if old and new:
        old_assign = {a["person_id"]: a for a in old[0]["assignments"]}
        for assignment in new[0]["assignments"]:
            prev = old_assign.get(assignment["person_id"])
            if prev and prev["offer_id"] != assignment["offer_id"]:
                differences.append(
                    {
                        "person_id": assignment["person_id"],
                        "before": prev["name"],
                        "after": assignment["name"],
                    }
                )
    delta = None
    if (
        old
        and new
        and old[0]["total_cents"] is not None
        and new[0]["total_cents"] is not None
    ):
        delta = new[0]["total_cents"] - old[0]["total_cents"]
    return {
        "before_request_id": before["request_id"],
        "after_request_id": after["request_id"],
        "changes": differences,
        "delta_cents": delta,
        "new_status": after["status"],
        "old_quote_reused": False,
    }


def break_even(card_fee_cents: int, saving_per_visit_cents: int) -> Optional[int]:
    """固定每次节省额下的回本次数；非正节省无有限回本。"""
    integer(card_fee_cents, "卡费")
    if type(saving_per_visit_cents) is not int:
        raise InputError("单次节省必须为整数分")
    if card_fee_cents == 0:
        return 0
    if saving_per_visit_cents <= 0:
        return None
    return (card_fee_cents + saving_per_visit_cents - 1) // saving_per_visit_cents
