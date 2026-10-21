"""多人多方案排名：候选生成、帕累托、beam、排序与理由（纯函数，无 I/O）。"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    FULFILLMENTS,
    GROUP_ALLOCATIONS_CAP,
    GROUP_MAX_DISTINCT_OFFERS,
    LEGACY_BEAM_MAX_PEOPLE,
    PRIORITIES,
    SPACE_SCI_THRESHOLD,
)
from .core import InputError, check_person, validate_catalog
from .fulfillment import context_key
from .request_v2 import fingerprint_v2, validate_request_v2

UNKNOWN_COST = 10**12
RULE_TEXT = (
    "门槛(硬要求全部已核实且预算内) → 总价升序 → 偏好数降序 → "
    "新购卡升序 → 券依赖升序 → offer id 字典序"
)
PREFERENCE_RULE_TEXT = (
    "门槛(硬要求全部已核实且预算内) → 偏好数降序 → 总价升序 → "
    "新购卡升序 → 券依赖升序 → offer id 字典序"
)
COUPON_NOTE = "未领取的券不计入本次报价（待验证）"
PREVIEW_NOTE = (
    "领券属后续阶段（P1），且 auto-bind-coupons 只能一键领取全部；"
    "未领取的券不计入本次报价"
)
MODE_NOTES = {
    "simulation": "模拟数据，非当前报价",
    "historical": "历史资料，非当前报价",
    "user_supplied": "用户提供资料，未在线核价，非当前报价",
}
# 排序键名称（按优先级顺序），用于“第一个不同的排序键”说明
KEY_ORDER = {
    "price": ("total", "preference", "new_card", "coupon"),
    "preference": ("preference", "total", "new_card", "coupon"),
}


def basket_signature(offers: List[Dict[str, Any]]) -> str:
    """购物篮多重集签名：与用餐者顺序无关。"""
    raw = "\n".join(sorted(o["id"] for o in offers))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cost(entry: Dict[str, Any]) -> int:
    """未知价格按极大值排序，不当作 0。"""
    return UNKNOWN_COST if entry["cost"] is None else entry["cost"]


def entry_key(entry: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """单人候选排序键，以 offer id 收尾。"""
    pref = -entry["matches"]
    if priority == "price":
        return (_cost(entry), pref, entry["card"], entry["coupon"], entry["id"])
    return (pref, _cost(entry), entry["card"], entry["coupon"], entry["id"])


def _dominates(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """a 在价格、偏好、新购卡上不劣于 b 且至少一项严格更优；未知价格不能支配他人。"""
    if a["cost"] is None:
        return False
    cost_ok = b["cost"] is None or a["cost"] <= b["cost"]
    if not (cost_ok and a["matches"] >= b["matches"] and a["card"] <= b["card"]):
        return False
    return (
        b["cost"] is None
        or a["cost"] < b["cost"]
        or a["matches"] > b["matches"]
        or a["card"] < b["card"]
    )


def pareto_topk(
    entries: List[Dict[str, Any]], k: int, priority: str
) -> Tuple[List[Dict[str, Any]], bool]:
    """帕累托前沿取前 k；不足则按同一排序补满。带券候选不参与被支配判定。
    返回 (候选, 前沿是否被截断)。"""
    plain = [e for e in entries if not e["coupon"]]
    frontier = [
        e
        for e in entries
        if e["coupon"] or not any(_dominates(o, e) for o in plain if o is not e)
    ]
    ordered = sorted(frontier, key=lambda e: entry_key(e, priority))
    if len(ordered) >= k:
        return ordered[:k], len(ordered) > k
    in_frontier = {id(e) for e in frontier}
    rest = [e for e in entries if id(e) not in in_frontier]
    rest.sort(key=lambda e: entry_key(e, priority))
    return (ordered + rest)[:k], False


def _partial_key(part: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """部分解排序键。"""
    pref = -part["matches"]
    if priority == "price":
        return (part["cost"], pref, part["card"], part["coupons"], part["ids"])
    return (pref, part["cost"], part["card"], part["coupons"], part["ids"])


def beam_combine(
    per_person: List[List[Dict[str, Any]]],
    width: int,
    priority: str,
    budget_cents: Optional[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按人员顺序逐人扩展并保留 ≤width 个部分解；每单至多一张券。"""
    space = 1
    for options in per_person:
        space *= len(options)
    all_known = all(e["cost"] is not None for options in per_person for e in options)
    prune = budget_cents is not None and all_known
    tail_min = [0] * (len(per_person) + 1)
    for i in range(len(per_person) - 1, -1, -1):
        low = min((_cost(e) for e in per_person[i]), default=0)
        tail_min[i] = tail_min[i + 1] + low
    parts = [{"entries": (), "cost": 0, "matches": 0, "card": 0, "coupons": 0, "ids": ()}]
    stats = {"search_space": space, "generated": 0, "truncated": False, "pruned": 0}
    for index, options in enumerate(per_person):
        merged: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for part in parts:
            for entry in options:
                if entry["coupon"] and part["coupons"] >= 1:
                    continue
                cost = part["cost"] + _cost(entry)
                if prune and cost + tail_min[index + 1] > budget_cents:
                    stats["pruned"] += 1
                    continue
                stats["generated"] += 1
                new = {
                    "entries": part["entries"] + (entry,),
                    "cost": cost,
                    "matches": part["matches"] + entry["matches"],
                    "card": part["card"] + entry["card"],
                    "coupons": part["coupons"] + entry["coupon"],
                    "ids": part["ids"] + (entry["id"],),
                }
                basket = tuple(sorted(new["ids"]))
                old = merged.get(basket)
                if old is None or _partial_key(new, priority) < _partial_key(old, priority):
                    merged[basket] = new
        parts = sorted(merged.values(), key=lambda p: _partial_key(p, priority))
        if len(parts) > width:
            stats["truncated"] = True
            parts = parts[:width]
        if not parts:
            break
    return parts, stats


def _make_entry(person: Dict[str, Any], offer: Dict[str, Any], checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造单人候选条目。"""
    matches = sum(
        offer.get("facts", {}).get(k, {}).get("value") == v
        for k, v in person["prefer"].items()
    )
    price = offer.get("price_cents")
    card = offer.get("card_fee_cents", 0)
    return {
        "offer": offer,
        "id": offer["id"],
        "checks": checks,
        "matches": matches,
        "cost": None if price is None else price + card,
        "card": card,
        "coupon": 1 if offer.get("coupon_key") else 0,
    }


def _candidate(req: Dict[str, Any], part: Dict[str, Any]) -> Dict[str, Any]:
    """部分解 → 整单候选。"""
    entries = part["entries"]
    offers = [e["offer"] for e in entries]
    assignments = []
    for person, entry in zip(req["people"], entries):
        offer = entry["offer"]
        assignments.append(
            {
                "person_id": person["id"],
                "offer_id": offer["id"],
                "name": offer["name"],
                "items": [
                    {"name": i["name"], "quantity": i.get("quantity", 1)}
                    for i in offer["items"]
                ],
                "checks": [dict(c) for c in entry["checks"]],
                "preferences": {
                    "matched": entry["matches"],
                    "total": len(person["prefer"]),
                },
            }
        )
    costs = [e["cost"] for e in entries]
    return {
        "signature": basket_signature(offers),
        "offers": offers,
        "assignments": assignments,
        "preference_matches": part["matches"],
        "preference_total": sum(len(p["prefer"]) for p in req["people"]),
        "new_card_cents": part["card"],
        "new_card_count": sum(1 for e in entries if e["card"]),
        "coupon_count": part["coupons"],
        "ids": part["ids"],
        "hint_cents": None if any(c is None for c in costs) else sum(costs),
    }


def generate_candidates(
    req: Dict[str, Any], catalog: Dict[str, Any], use_legacy_beam: bool = False
) -> Dict[str, Any]:
    """逐人过滤硬要求 → 帕累托 → beam，输出整单候选与统计。

    人数超过 LEGACY_BEAM_MAX_PEOPLE 时走同质组多重集合搜索（grouping.py）；
    use_legacy_beam=True 仅供基准与差分测试强制使用旧的逐人 beam。
    """
    if len(req["people"]) > LEGACY_BEAM_MAX_PEOPLE and not use_legacy_beam:
        from .grouping import generate_candidates_grouped  # 延迟导入，避免循环依赖

        return generate_candidates_grouped(req, catalog)
    limits = req["limits"]
    priority = req["ranking"]["priority"]
    per_person: List[List[Dict[str, Any]]] = []
    diagnostics: List[Any] = []
    missing = 0
    rejected = 0
    frontier_truncated = False
    for person in req["people"]:
        eligible = []
        failures = []
        for offer in catalog["offers"]:
            checks = check_person(person, offer)
            bad = [c for c in checks if c["status"] != "verified"]
            if bad:
                failures.append({"offer_id": offer["id"], "checks": bad})
                continue
            eligible.append(_make_entry(person, offer, checks))
        rejected += len(failures)
        if not eligible:
            missing += 1
            diagnostics.append(
                {
                    "person_id": person["id"],
                    "reason": "没有证据完整且满足硬要求的组合",
                    "examples": failures[:5],
                }
            )
        top, cut = pareto_topk(eligible, limits["per_person_top_k"], priority)
        frontier_truncated = frontier_truncated or cut
        per_person.append(top)
    stats = {
        "search_space": 0,
        "generated": 0,
        "truncated": False,
        "pruned": 0,
        "offers_rejected": rejected,
        "persons_without_option": missing,
        "per_person_truncated": frontier_truncated,
    }
    whole: List[Dict[str, Any]] = []
    if not missing:
        budget = None if catalog["mode"] == "live" else req["budget_cents"]
        parts, beam_stats = beam_combine(
            per_person, limits["max_whole_candidates"], priority, budget
        )
        stats.update(beam_stats)
        whole = [_candidate(req, p) for p in parts]
    return {"per_person": per_person, "whole": whole, "diagnostics": diagnostics, "stats": stats}


def _hint_key(cand: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """核价选样用的菜单提示价排序键（提示价不可信，仅用于选样）。"""
    hint = UNKNOWN_COST if cand["hint_cents"] is None else cand["hint_cents"]
    pref = -cand["preference_matches"]
    if priority == "price":
        head: Tuple[Any, ...] = (hint, pref)
    else:
        head = (pref, hint)
    return head + (cand["new_card_count"], cand["coupon_count"], cand["ids"])


def select_for_quote(
    whole: List[Dict[str, Any]], max_quotes: int, priority: str
) -> List[Dict[str, Any]]:
    """双键交错选样：前半按用户优先键，其余按另一个键补，去重。"""
    other = [p for p in PRIORITIES if p != priority][0]
    first = sorted(whole, key=lambda c: _hint_key(c, priority))
    second = sorted(whole, key=lambda c: _hint_key(c, other))
    chosen: List[Dict[str, Any]] = []
    seen = set()

    def take(source: List[Dict[str, Any]], limit: int) -> None:
        for cand in source:
            if len(chosen) >= limit:
                return
            if cand["signature"] not in seen:
                seen.add(cand["signature"])
                chosen.append(cand)

    take(first, math.ceil(max_quotes / 2))
    take(second, max_quotes)
    take(first, max_quotes)
    return chosen


def _rank_key(item: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """整单排序键（D6），以 offer id 元组收尾。"""
    pref = -item["preference_matches"]
    tail = (item["steps"]["new_card_count"], item["coupon_count"], item["_ids"])
    if priority == "price":
        return (item["total_cents"], pref) + tail
    return (pref, item["total_cents"]) + tail


def _coupon_titles(cand: Dict[str, Any]) -> List[Dict[str, Any]]:
    """依赖的本店可用券（只含券名，不含券标识）。"""
    seen = set()
    result = []
    for offer in cand["offers"]:
        if offer.get("coupon_key") and offer["coupon_key"] not in seen:
            seen.add(offer["coupon_key"])
            result.append(
                {
                    "title": offer.get("coupon_title") or offer["name"],
                    "kind": "store_usable",
                    "needs_claim": False,
                }
            )
    return result


def _quoted(quote: Any, sig: str, ctx: Dict[str, Any]) -> bool:
    """官方报价必须绑定签名、上下文，且已核验。"""
    return (
        isinstance(quote, dict)
        and quote.get("signature") == sig
        and quote.get("context") == ctx
        and quote.get("verified") is True
        and type(quote.get("total_cents")) is int
        and quote["total_cents"] >= 0
    )


def rank_candidates(
    req: Dict[str, Any],
    catalog: Dict[str, Any],
    cands: List[Dict[str, Any]],
    quotes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """门槛过滤 + 排序；返回 ranked（全部预算内条目）与 excluded 计数。"""
    priority = req["ranking"]["priority"]
    ctx = context_key(req)
    mode = catalog["mode"]
    excluded = {"over_budget": 0, "hard_not_verified": 0, "price_unknown": 0}
    items: List[Dict[str, Any]] = []
    for cand in cands:
        checks = [c for a in cand["assignments"] for c in a["checks"]]
        if any(c["status"] != "verified" for c in checks):
            excluded["hard_not_verified"] += 1
            continue
        total = None
        extra: Dict[str, Any] = {"quoted_at": None, "pricing_source": None, "fee_breakdown": {}}
        status = "unknown"
        if mode == "live":
            quote = (quotes or {}).get(cand["signature"])
            if _quoted(quote, cand["signature"], ctx):
                total = quote["total_cents"]
                status = "official_verified"
                extra = {
                    "quoted_at": quote.get("quoted_at"),
                    "pricing_source": quote.get("source"),
                    "fee_breakdown": quote.get("fee_breakdown", {}),
                }
        elif cand["hint_cents"] is not None:
            fees = catalog.get("order_fee_cents")
            if type(fees) is int and fees >= 0:
                total = cand["hint_cents"] + fees
                status = mode
                extra["pricing_source"] = catalog.get("price_source")
        if total is None:
            excluded["price_unknown"] += 1
            continue
        if total > req["budget_cents"]:
            excluded["over_budget"] += 1
            continue
        items.append(
            {
                "signature": cand["signature"],
                "total_cents": total,
                "price_status": status,
                "assignments": cand["assignments"],
                "hard_all_verified": True,
                "preference_matches": cand["preference_matches"],
                "preference_total": cand["preference_total"],
                "coupons": _coupon_titles(cand),
                "coupon_note": COUPON_NOTE,
                "data_mode": mode,
                "quoted_at": extra["quoted_at"],
                "pricing_source": extra["pricing_source"],
                "fee_breakdown": extra["fee_breakdown"],
                "new_card_cents": cand["new_card_cents"],
                "steps": {"order_count": 1, "new_card_count": cand["new_card_count"]},
                "within_budget": True,
                "coupon_count": cand["coupon_count"],
                "_ids": cand["ids"],
            }
        )
    items.sort(key=lambda i: _rank_key(i, priority))
    for index, item in enumerate(items, 1):
        item["rank"] = index
        item["gap_vs_first_cents"] = item["total_cents"] - items[0]["total_cents"]
    return {"ranked": items, "excluded": excluded}


def _yuan(cents: int) -> str:
    """分 → ¥元。"""
    return f"¥{cents / 100:.2f}"


def _first_difference(item: Dict[str, Any], prev: Dict[str, Any], priority: str) -> str:
    """与上一名比较，取第一个不同的排序键作说明。"""
    for key in KEY_ORDER[priority]:
        if key == "total" and item["total_cents"] != prev["total_cents"]:
            diff = item["total_cents"] - prev["total_cents"]
            return f"总价 {_yuan(item['total_cents'])}，比第{prev['rank']}名贵 {_yuan(diff)}"
        if key == "preference" and item["preference_matches"] != prev["preference_matches"]:
            diff = prev["preference_matches"] - item["preference_matches"]
            return (
                f"偏好满足 {item['preference_matches']}/{item['preference_total']}，"
                f"比第{prev['rank']}名少 {diff} 项"
            )
        if key == "new_card" and item["steps"]["new_card_count"] != prev["steps"]["new_card_count"]:
            diff = item["steps"]["new_card_count"] - prev["steps"]["new_card_count"]
            return f"新购卡比第{prev['rank']}名多 {diff} 张"
        if key == "coupon" and item["coupon_count"] != prev["coupon_count"]:
            return f"券依赖比第{prev['rank']}名更多"
    return f"与第{prev['rank']}名在总价、偏好、新购卡、券依赖上完全相同，按餐品 id 稳定排序"


def explain(
    item: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    first: Dict[str, Any],
    priority: str,
    count: int = 1,
) -> List[str]:
    """按固定模板生成理由，无 LLM、无综合打分。"""
    people = len(item["assignments"])
    reasons = [f"{people} 位用餐者硬要求全部已核实"]
    if previous is None:
        if count == 1:
            reasons.append(f"总价 {_yuan(item['total_cents'])}，是已检查方案中唯一满足预算的方案")
        elif priority == "price":
            reasons.append(
                f"总价 {_yuan(item['total_cents'])}，是已检查的 {count} 个预算内方案中最低"
            )
        else:
            reasons.append(
                f"偏好满足 {item['preference_matches']}/{item['preference_total']}，"
                "是已检查方案中最多（并列取总价较低者）"
            )
    else:
        reasons.append(_first_difference(item, previous, priority))
    if item["coupons"]:
        titles = "、".join(f"「{c['title']}」" for c in item["coupons"])
        reasons.append(f"依赖本店当前可用券{titles}；未领取的券不计入报价")
    else:
        reasons.append("不依赖任何券")
    if item["price_status"] == "official_verified":
        when = item.get("quoted_at") or "时间未标记"
        reasons.append(f"官方核价（{when}），购买前价格可能变化")
    else:
        reasons.append(MODE_NOTES.get(item["data_mode"], "非当前报价"))
    return reasons


def classify_no_result(
    context_ok: bool,
    persons_without_option: int,
    excluded: Dict[str, int],
    pruned: int,
    whole_count: int,
) -> Dict[str, Any]:
    """无推荐时归入四类之一；返回 {status, no_result}。"""
    counts = dict(excluded)
    counts["persons_without_option"] = persons_without_option
    counts["budget_pruned_partial"] = pruned
    if not context_ok:
        category, status = "context_mismatch", "context_mismatch"
        detail = "门店、取餐方式或时间不同；需要对应菜单和新报价"
    elif persons_without_option or (
        excluded["hard_not_verified"] and not excluded["price_unknown"] and not excluded["over_budget"]
    ) or (not whole_count and not pruned):
        category, status = "attribute_unknown", "no_verified_plan"
        detail = "没有证据完整且满足硬要求的组合；未知属性不当作满足，也不自动放宽要求"
    elif excluded["price_unknown"]:
        category, status = "needs_price_verification", "needs_price_verification"
        detail = "候选缺少已核验的官方价格，需要先核价"
    else:
        category, status = "over_budget_in_checked_set", "over_budget_in_checked_set"
        detail = "已检查组合均超预算；未证明全门店无解，不自动放宽要求"
    return {"status": status, "no_result": {"category": category, "detail": detail, "counts": counts}}


def format_space(space: int) -> str:
    """搜索空间文案：小于阈值逐字显示整数，否则用科学计数法（约 1.4×10^45）。"""
    if space < SPACE_SCI_THRESHOLD:
        return str(space)
    exponent = len(str(space)) - 1
    mantissa = round(int(str(space)[:3]) / 100, 1)
    if mantissa >= 10:
        mantissa, exponent = 1.0, exponent + 1
    return f"约 {mantissa:.1f}×10^{exponent}"


def _group_note(stats: Dict[str, Any], people: int) -> str:
    """同质组搜索的额外限制说明（仅 >6 人走分组路径时出现）。"""
    return (
        f"{people} 位用餐者归并为 {stats['groups']} 个同质组"
        f"（组内最多 {GROUP_MAX_DISTINCT_OFFERS} 种不同套餐，"
        f"每组保留前 {GROUP_ALLOCATIONS_CAP} 种分配）。"
    )


def _statement(
    live: bool, stats: Dict[str, Any], whole: int, selected: int, people: int = 0
) -> str:
    """检查范围声明：永远说明是已检查方案中的排名。"""
    if live and whole > selected:
        text = (
            f"仅检查了前 {selected} 个候选的官方核价（候选共 {whole} 个），"
            "结果是已检查方案中的排名，不是全局最优。"
        )
    else:
        text = (
            f"共检查 {whole} 个整单候选（搜索空间 {format_space(stats['search_space'])} 种），"
            "结果是已检查方案中的排名，不是全局最优。"
        )
        if stats["truncated"]:
            text += "整单候选达到上限，已截断。"
    if stats.get("groups"):
        text += _group_note(stats, people)
    return text


def _coupon_preview(whole_offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """离线预览：仅汇总菜单内带券 offer 的券名；其余两类由实时流程填充。"""
    titles: List[Dict[str, Any]] = []
    seen = set()
    for offer in whole_offers:
        key = offer.get("coupon_key")
        if key and key not in seen:
            seen.add(key)
            titles.append({"title": offer.get("coupon_title") or offer["name"]})
    return {"claimable": [], "held": [], "store_usable": titles, "notes": [PREVIEW_NOTE]}


def compose_v2(
    request: Dict[str, Any],
    catalog: Dict[str, Any],
    quotes: Optional[Dict[str, Any]] = None,
    run_calls: int = 0,
) -> Dict[str, Any]:
    """v2 编排：候选 → 排名 → 理由；保留 v1 旧字段，不输出券码与 wire_items。"""
    req = validate_request_v2(request)
    validate_catalog(catalog)
    spec = FULFILLMENTS[req["fulfillment"]["type"]]
    limits = req["limits"]
    priority = req["ranking"]["priority"]
    top_n = req["ranking"]["top_n"]
    live = catalog["mode"] == "live"
    result: Dict[str, Any] = {
        "schema_version": 2,
        "request": req,
        "request_id": fingerprint_v2(req),
        "mode": catalog["mode"],
        "as_of": catalog["as_of"],
        "status": "no_verified_plan",
        "fulfillment": {
            "type": spec.key,
            "label": spec.label,
            "be_type": spec.be_type,
            "order_type": spec.order_type,
        },
        "plans": [],
        "diagnostics": [],
        "search": {
            "catalog_offers": len(catalog["offers"]),
            "max_offers_per_person": limits["per_person_top_k"],
            "max_combinations": limits["max_whole_candidates"],
        },
        "ranking": [],
        "ranking_meta": {},
        "no_result": None,
        "recommendations": [],
        "coupon_preview": _coupon_preview(catalog["offers"]),
    }
    meta = {
        "priority": priority,
        "top_n": top_n,
        "rule": RULE_TEXT if priority == "price" else PREFERENCE_RULE_TEXT,
        "checked": {
            "search_space": 0,
            "whole_generated": 0,
            "whole_truncated": False,
            "quotes_attempted": 0,
            "quotes_verified": 0,
            "quote_cap": limits["max_quotes"],
            "run_calls": run_calls,
            "statement": "",
        },
        "excluded": {"over_budget": 0, "hard_not_verified": 0, "price_unknown": 0},
    }
    result["ranking_meta"] = meta
    if catalog.get("context") != context_key(req):
        result["status"] = "context_mismatch"
        result["diagnostics"].append("门店、取餐方式或时间不同；需要对应菜单和新报价")
        result["no_result"] = classify_no_result(False, 0, meta["excluded"], 0, 0)["no_result"]
        meta["checked"]["statement"] = "上下文不匹配，未检查任何候选。"
        return result
    generated = generate_candidates(req, catalog)
    stats = generated["stats"]
    whole = generated["whole"]
    result["diagnostics"].extend(generated["diagnostics"])
    selected = select_for_quote(whole, limits["max_quotes"], priority) if live else whole
    ranked = rank_candidates(req, catalog, selected, quotes)
    items = ranked["ranked"]
    checked = meta["checked"]
    checked.update(
        search_space=stats["search_space"],
        whole_generated=len(whole),
        whole_truncated=stats["truncated"],
        quotes_attempted=len(selected) if live else 0,
        quotes_verified=sum(1 for i in items if i["price_status"] == "official_verified"),
        statement=_statement(live, stats, len(whole), len(selected), len(req["people"])),
    )
    meta["excluded"] = ranked["excluded"]
    result["search"]["checked_combinations"] = len(whole)
    top = items[:top_n]
    for index, item in enumerate(top):
        previous = top[index - 1] if index else None
        item["reasons"] = explain(item, previous, items[0], priority, len(items))
    for item in items:
        item.pop("_ids", None)
    result["ranking"] = top
    result["recommendations"] = top
    result["plans"] = top
    if top:
        result["status"] = "ready" if live else catalog["mode"]
    else:
        verdict = classify_no_result(
            True,
            stats["persons_without_option"],
            ranked["excluded"],
            stats["pruned"],
            len(whole),
        )
        result["status"] = verdict["status"]
        result["no_result"] = verdict["no_result"]
    return result
