"""同质组多重集合搜索：用餐者 >6 人时的候选生成（纯函数，无 I/O）。

思路：硬要求与偏好完全相同的人归为一个同质组，组内可选餐品与偏好得分一致，
因此只需对每组做一次硬要求核对，再把“整组吃什么”表示为多重集合（餐品×数量）。
组内最多 GROUP_MAX_DISTINCT_OFFERS 种不同套餐，每组只保留前 GROUP_ALLOCATIONS_CAP 种分配，
组间沿用 beam 合并。这些都是额外的搜索限制，会写入 checked.statement。
"""
from __future__ import annotations

import json
from bisect import insort
from itertools import combinations
from math import comb
from typing import Any, Dict, List, Optional, Tuple

from .config import GROUP_ALLOCATIONS_CAP, GROUP_MAX_DISTINCT_OFFERS
from .core import check_person
from .ranking import _candidate, _cost, _make_entry, pareto_topk


def group_key(person: Dict[str, Any]) -> str:
    """同质组键：硬要求与偏好完全相同。"""
    return json.dumps(
        {"hard": person["hard"], "prefer": person["prefer"]},
        sort_keys=True,
        ensure_ascii=False,
    )


def group_people(people: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按首次出现顺序编号 G1…，组内成员保持请求顺序。"""
    groups: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for position, person in enumerate(people):
        key = group_key(person)
        group = index.get(key)
        if group is None:
            group = {
                "label": f"G{len(groups) + 1}",
                "key": key,
                "hard": person["hard"],
                "prefer": person["prefer"],
                "people": [],
                "positions": [],
            }
            index[key] = group
            groups.append(group)
        group["people"].append(person)
        group["positions"].append(position)
    return groups


def _alloc_key(alloc: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """分配方案排序键（与部分解排序键同构），以排序后的 offer id 收尾。"""
    pref = -alloc["matches"]
    if priority == "price":
        return (alloc["cost"], pref, alloc["card"], alloc["coupons"], alloc["ids"])
    return (pref, alloc["cost"], alloc["card"], alloc["coupons"], alloc["ids"])


def _make_alloc(parts: Tuple[Tuple[Dict[str, Any], int], ...]) -> Dict[str, Any]:
    """(条目, 数量) 序列 → 分配方案；序列已按 offer id 升序。"""
    ids: List[str] = []
    for entry, count in parts:
        ids.extend([entry["id"]] * count)
    return {
        "parts": parts,
        "cost": sum(_cost(e) * c for e, c in parts),
        "matches": sum(e["matches"] * c for e, c in parts),
        "card": sum(e["card"] * c for e, c in parts),
        "coupons": sum(e["coupon"] * c for e, c in parts),
        "ids": tuple(ids),
    }


def enumerate_allocations(
    top: List[Dict[str, Any]], size: int
) -> List[Tuple[Tuple[Dict[str, Any], int], ...]]:
    """闭式枚举一个组的全部合法分配（未截断）。

    ①整组同一款；②两款按 n1+n2=size 拆分；每单至多一张券，因此带券款最多 1 份。
    """
    entries = sorted(top, key=lambda e: e["id"])
    result: List[Tuple[Tuple[Dict[str, Any], int], ...]] = []
    for entry in entries:
        if size == 1 or not entry["coupon"]:
            result.append(((entry, size),))
    if GROUP_MAX_DISTINCT_OFFERS >= 2 and size >= 2:
        for a, b in combinations(entries, 2):
            if a["coupon"] and b["coupon"]:
                continue
            for n1 in range(1, size):
                n2 = size - n1
                if (a["coupon"] and n1 > 1) or (b["coupon"] and n2 > 1):
                    continue
                result.append(((a, n1), (b, n2)))
    return result


def group_allocations(
    top: List[Dict[str, Any]],
    size: int,
    priority: str,
    max_cost: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """返回 (保留的分配方案, 本组最低成本下界, 因预算被过滤的数量)。

    max_cost 不为 None 时（离线且价格全已知），先滤掉超出“预算−其他组最低成本”的分配，
    再按优先级键截断，避免预算边界附近的拆分被截掉。
    """
    allocs = [_make_alloc(p) for p in enumerate_allocations(top, size)]
    floor = min((a["cost"] for a in allocs), default=0)
    filtered = 0
    if max_cost is not None:
        kept = [a for a in allocs if a["cost"] <= max_cost]
        filtered = len(allocs) - len(kept)
        allocs = kept
    allocs.sort(key=lambda a: _alloc_key(a, priority))
    return allocs[:GROUP_ALLOCATIONS_CAP], floor, filtered


def _part_key(part: Dict[str, Any], priority: str) -> Tuple[Any, ...]:
    """组间部分解排序键。"""
    pref = -part["matches"]
    if priority == "price":
        return (part["cost"], pref, part["card"], part["coupons"], part["basket"])
    return (pref, part["cost"], part["card"], part["coupons"], part["basket"])


def beam_combine_groups(
    group_options: List[List[Dict[str, Any]]],
    floors: List[int],
    width: int,
    priority: str,
    budget_cents: Optional[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按组顺序扩展并保留 ≤width 个部分解；每单至多一张券，按 offer 多重集合并。"""
    space = 1
    for options in group_options:
        space *= len(options)
    tail = [0] * (len(group_options) + 1)
    for i in range(len(group_options) - 1, -1, -1):
        tail[i] = tail[i + 1] + floors[i]
    parts = [
        {"allocs": (), "cost": 0, "matches": 0, "card": 0, "coupons": 0, "basket": ()}
    ]
    stats = {"generated": 0, "truncated": False, "pruned": 0}
    for index, options in enumerate(group_options):
        merged: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for part in parts:
            for alloc in options:
                if alloc["coupons"] and part["coupons"] >= 1:
                    continue
                cost = part["cost"] + alloc["cost"]
                if budget_cents is not None and cost + tail[index + 1] > budget_cents:
                    stats["pruned"] += 1
                    continue
                stats["generated"] += 1
                basket_list = list(part["basket"])
                for oid in alloc["ids"]:
                    insort(basket_list, oid)
                basket = tuple(basket_list)
                new = {
                    "allocs": part["allocs"] + (alloc,),
                    "cost": cost,
                    "matches": part["matches"] + alloc["matches"],
                    "card": part["card"] + alloc["card"],
                    "coupons": part["coupons"] + alloc["coupons"],
                    "basket": basket,
                }
                old = merged.get(basket)
                if old is None or _part_key(new, priority) < _part_key(old, priority):
                    merged[basket] = new
        parts = sorted(merged.values(), key=lambda p: _part_key(p, priority))
        if len(parts) > width:
            stats["truncated"] = True
            parts = parts[:width]
        if not parts:
            break
    stats["search_space"] = space
    return parts, stats


def _expand(
    groups: List[Dict[str, Any]], allocs: Tuple[Dict[str, Any], ...], count: int
) -> Tuple[Tuple[Dict[str, Any], ...], Tuple[str, ...]]:
    """展开回逐人条目：组内按 offer id 升序依次分给成员（请求顺序）。"""
    entries: List[Optional[Dict[str, Any]]] = [None] * count
    for group, alloc in zip(groups, allocs):
        members = iter(group["positions"])
        for entry, number in alloc["parts"]:
            for _ in range(number):
                entries[next(members)] = entry
    final = tuple(e for e in entries if e is not None)
    return final, tuple(e["id"] for e in final)


def generate_candidates_grouped(
    req: Dict[str, Any], catalog: Dict[str, Any]
) -> Dict[str, Any]:
    """同质组版 generate_candidates：返回同结构 {per_person, whole, diagnostics, stats}。"""
    limits = req["limits"]
    priority = req["ranking"]["priority"]
    people = req["people"]
    groups = group_people(people)
    tops: List[List[Dict[str, Any]]] = []
    diagnostics: List[Any] = []
    missing = 0
    rejected = 0
    frontier_truncated = False
    for group in groups:
        person = group["people"][0]
        size = len(group["people"])
        eligible = []
        failures = []
        for offer in catalog["offers"]:
            checks = check_person(person, offer)
            bad = [c for c in checks if c["status"] != "verified"]
            if bad:
                failures.append({"offer_id": offer["id"], "checks": bad})
                continue
            eligible.append(_make_entry(person, offer, checks))
        rejected += len(failures) * size
        if not eligible:
            missing += size
            diagnostics.append(
                {
                    "person_id": person["id"],
                    "person_ids": [p["id"] for p in group["people"]],
                    "reason": "没有证据完整且满足硬要求的组合",
                    "examples": failures[:5],
                }
            )
        top, cut = pareto_topk(eligible, limits["per_person_top_k"], priority)
        frontier_truncated = frontier_truncated or cut
        tops.append(top)
    sizes = [len(g["people"]) for g in groups]
    space = 1
    for top, size in zip(tops, sizes):
        space *= comb(len(top) + size - 1, size) if top else 0
    stats: Dict[str, Any] = {
        "search_space": space,
        "generated": 0,
        "truncated": False,
        "pruned": 0,
        "offers_rejected": rejected,
        "persons_without_option": missing,
        "per_person_truncated": frontier_truncated,
        "groups": len(groups),
        "group_sizes": sizes,
    }
    per_person: List[List[Dict[str, Any]]] = [[] for _ in people]
    for group, top in zip(groups, tops):
        for position in group["positions"]:
            per_person[position] = top
    whole: List[Dict[str, Any]] = []
    if not missing:
        budget = None if catalog["mode"] == "live" else req["budget_cents"]
        all_known = all(e["cost"] is not None for top in tops for e in top)
        prune = budget is not None and all_known
        # 组成本下界：size × 组内最低单价（价格未知时不用于剪枝）
        floors = [min(_cost(e) for e in top) * size for top, size in zip(tops, sizes)]
        total_floor = sum(floors)
        options: List[List[Dict[str, Any]]] = []
        filtered_total = 0
        for top, size, floor in zip(tops, sizes, floors):
            max_cost = budget - (total_floor - floor) if prune else None
            allocs, _, filtered = group_allocations(top, size, priority, max_cost)
            filtered_total += filtered
            options.append(allocs)
        stats["pruned"] += filtered_total
        if all(options):
            parts, beam = beam_combine_groups(
                options, floors, limits["max_whole_candidates"], priority,
                budget if prune else None,
            )
            stats["generated"] = beam["generated"]
            stats["truncated"] = beam["truncated"]
            stats["pruned"] += beam["pruned"]
            for part in parts:
                entries, ids = _expand(groups, part["allocs"], len(people))
                whole.append(
                    _candidate(
                        req,
                        {
                            "entries": entries,
                            "matches": part["matches"],
                            "card": part["card"],
                            "coupons": part["coupons"],
                            "ids": ids,
                        },
                    )
                )
    return {"per_person": per_person, "whole": whole, "diagnostics": diagnostics, "stats": stats}
