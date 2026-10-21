"""订单摘要与受控领券核心（P1）。

本模块不直接联网：只经传入的客户端、账本与时钟工作。摘要与账本只保存 coupon_key 哈希；
券 ID/券码只在执行期按 coupon_key 从 query-store-coupons 重建并驻留内存。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import gate, live, net_guard
from .config import (
    DIGEST_VERSION,
    EXIT_DUPLICATE,
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_OK,
    EXIT_RECONFIRM,
    EXIT_REFUSED,
    EXIT_UNKNOWN,
    FULFILLMENTS,
    SUMMARY_TTL_RANGE,
    SUMMARY_TTL_SECONDS,
    TOOL_AUTO_BIND_COUPONS,
)
from .core import InputError
from .grouping import group_people
from .ledger import Ledger, LedgerCorrupt, LedgerError, LedgerStateError
from .mcp import McpError, WriteFailed, WriteRefused, WriteUnknown
from .privacy import assert_public, sanitize_claimable, sanitize_held  # noqa: F401

FEE_KEYS = ("productPrice", "deliveryPrice", "packingPrice", "tablewarePrice")
CLOCK_SKEW = timedelta(seconds=60)
DATA_SOURCE = "官方 calculate-price（本摘要生成时重新核价）"
REAL_ENV_MESSAGE = "真实领券/下单暂未开放；当前只能在本机回环模拟服务上执行"
_SIGNATURE = re.compile(r"\A[0-9a-f]{8,16}\Z")
_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_MATERIAL_KEYS = frozenset(
    {
        "v",
        "store_code",
        "fulfillment",
        "need_tableware",
        "lines",
        "take_way",
        "total_cents",
        "fee_breakdown",
        "claim_coupons",
        "quote_window",
    }
)


def utcnow() -> datetime:
    """当前时间（模块级，供测试 patch）。"""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """秒精度 ISO 时间。"""
    return moment.isoformat(timespec="seconds")


def parse_time(text: Any) -> datetime:
    """解析账本里的 ISO 时间；无时区按 UTC。"""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        raise LedgerCorrupt("账本损坏：时间无效，已拒绝执行") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class OrderError(McpError):
    """带退出码与可选公开载荷的订单流程错误（文案可安全展示）。"""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload or {}


class MenuChanged(McpError):
    """重建篮子时找不到原商品变体或所依赖的券：菜单/券已变化。"""


# --- 规范化与 digest ---
def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序、无空白、保留中文。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_digest(material: Dict[str, Any]) -> str:
    """规范化 JSON 的 sha256（64 位十六进制）。"""
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def _walk_strings(value: Any) -> List[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _walk_strings(v)]
    return [value] if isinstance(value, str) else []


def assert_material_clean(material: Dict[str, Any]) -> None:
    """材料只允许约定键；不含敏感键、链接或手机号形态的值。"""
    if set(material) != _MATERIAL_KEYS:
        raise InputError("摘要材料字段不符，已拦截")
    assert_public(material, allow=("code",))  # code 仅为公开菜单商品编码键
    for text in _walk_strings(material):
        if "http" in text.lower() or _PHONE.search(text):
            raise InputError("摘要材料含疑似链接或手机号，已拦截")


def coupon_key_of(coupon_id: Any) -> str:
    """couponId 的 sha256 前 12 位（与 live.detail_offers 一致）。"""
    return hashlib.sha256(str(coupon_id).encode()).hexdigest()[:12]


def _norm_rounds(round_list: Any) -> List[Dict[str, Any]]:
    rounds = []
    for rnd in round_list or []:
        items = [
            {"code": c.get("code"), "quantity": c.get("quantity"), "modification": c.get("modification")}
            for c in (rnd.get("comboItemList") or [])
        ]
        rounds.append({"round": str(rnd.get("round")), "items": items})
    return rounds


def material_lines(wire_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并后的商品行 → 材料行（不含券 ID/券码，仅 coupon_key）。"""
    lines = [
        {
            "product_code": row["productCode"],
            "quantity": row["quantity"],
            "rounds": _norm_rounds(row.get("roundList")),
            "coupon_key": coupon_key_of(row["couponId"]) if row.get("couponId") else None,
        }
        for row in wire_items
    ]
    return sorted(lines, key=lambda l: (l["product_code"], canonical_json(l["rounds"]), l["coupon_key"] or ""))


def basket_key(material: Dict[str, Any]) -> str:
    """同一篮子：门店 + 取餐方式 + 地址引用 + 商品行。"""
    raw = canonical_json(
        {"store_code": material["store_code"], "fulfillment": material["fulfillment"], "lines": material["lines"]}
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lines_from_candidate(cand: Dict[str, Any]) -> List[Dict[str, Any]]:
    """候选 → 可重建的行（offer_id、productCode、数量、coupon_key）。"""
    counts: Counter = Counter()
    meta: Dict[str, Dict[str, Any]] = {}
    for offer in cand["offers"]:
        counts[offer["id"]] += 1
        meta[offer["id"]] = {
            "product_code": offer["wire_items"][0]["productCode"],
            "coupon_key": offer.get("coupon_key"),
        }
    return [
        {"offer_id": oid, "product_code": meta[oid]["product_code"], "quantity": counts[oid],
         "coupon_key": meta[oid]["coupon_key"]}
        for oid in sorted(counts)
    ]


# --- 重建与核价 ---
def rebuild_basket(client: Any, wire_ctx: Dict[str, Any], lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 productCode 重新取详情、按 coupon_key 匹配券，重建同一篮子（券 ID/券码仅内存）。"""
    coupons = client.call("query-store-coupons", wire_ctx).get("data", [])
    if not isinstance(coupons, list):
        raise McpError("门店券结构不匹配")
    by_key = {
        coupon_key_of(c["couponId"]): c
        for c in coupons
        if isinstance(c, dict) and c.get("couponId") and c.get("couponCode")
    }
    details: Dict[str, Any] = {}
    for code in sorted({line["product_code"] for line in lines}):
        details[code] = client.call("query-meal-detail", {**wire_ctx, "code": code}).get("data", {})
    offers: List[Dict[str, Any]] = []
    distinct: List[Dict[str, Any]] = []
    for line in lines:
        coupon = None
        if line["coupon_key"]:
            coupon = by_key.get(line["coupon_key"])
            if coupon is None:
                raise MenuChanged("所依赖的券已不在本店可用券中")
            if line["quantity"] != 1:
                raise MenuChanged("带券商品行数量异常")
        variants = live.detail_offers(line["product_code"], details[line["product_code"]], "官方query-meal-detail", coupon)
        match = next((o for o in variants if o["id"] == line["offer_id"]), None)
        if match is None:
            raise MenuChanged("菜单已变化，找不到原商品选项")
        distinct.append(match)
        offers.extend([match] * line["quantity"])
    return {
        "wire_items": live.merge_wire_items(offers),
        "offers": distinct,
        "coupon_keys": sorted(by_key),
    }


def parse_quote(payload: Dict[str, Any], wire_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """校验官方核价并解析费用拆分与 takeWayList。"""
    total = live.check_pricing(payload, wire_items)
    data = payload["data"]
    options: List[Dict[str, Any]] = []
    raw = data.get("takeWayList")
    if raw is not None:
        if not isinstance(raw, list):
            raise InputError("取餐方式选项结构异常")
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not row["code"]:
                raise InputError("取餐方式选项结构异常")
            options.append(
                {"code": row["code"], "title": str(row.get("title") or ""), "subtitle": str(row.get("subtitle") or "")}
            )
    return {
        "total_cents": total,
        "fee_breakdown": {k: data.get(k) for k in FEE_KEYS},
        "take_way_options": options,
    }


def quote_basket(client: Any, wire_ctx: Dict[str, Any], wire_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对合并后的商品行重新 calculate-price。"""
    payload = client.call("calculate-price", {**wire_ctx, "items": wire_items, "needTableware": False})
    return parse_quote(payload, wire_items)


def claim_set(client: Any) -> List[Dict[str, Any]]:
    """available-coupons → 将领券集合（仅名称/标签/数量，排序稳定）。"""
    rows = sanitize_claimable(client.call("available-coupons", {}))
    counts: Counter = Counter(
        (str(r.get("couponName", "")), str(r.get("label", ""))) for r in rows
    )
    return [{"name": n, "label": l, "count": c} for (n, l), c in sorted(counts.items())]


# --- 摘要组装 ---
def _material(
    store_code: str,
    fulfillment: Dict[str, Any],
    lines: List[Dict[str, Any]],
    take_way_code: Optional[str],
    quote: Dict[str, Any],
    claim_coupons: List[Dict[str, Any]],
    quoted_at: datetime,
    expires_at: datetime,
) -> Dict[str, Any]:
    """digest 材料（计划 §5.2）。人员与餐品的对应关系不进 digest。"""
    return {
        "v": DIGEST_VERSION,
        "store_code": store_code,
        "fulfillment": fulfillment,
        "need_tableware": False,
        "lines": lines,
        "take_way": {"code": take_way_code} if fulfillment["order_type"] == 1 else None,
        "total_cents": quote["total_cents"],
        "fee_breakdown": quote["fee_breakdown"],
        "claim_coupons": sorted(claim_coupons, key=lambda c: (c["name"], c["label"])),
        "quote_window": {"quoted_at": iso(quoted_at), "expires_at": iso(expires_at)},
    }


def _public_lines(offers: List[Dict[str, Any]], lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {o["id"]: o for o in offers}
    out = []
    for line in lines:
        offer = by_id[line["offer_id"]]
        out.append(
            {
                "offer_id": offer["id"],
                "name": offer["name"],
                "product_code": line["product_code"],
                "quantity": line["quantity"],
                "coupon": {"title": offer["coupon_title"]} if offer.get("coupon_title") else (
                    {"title": "（券）"} if offer.get("coupon_key") else None
                ),
            }
        )
    return out


def _claim_block(coupons: List[Dict[str, Any]], skipped: bool) -> Dict[str, Any]:
    return {
        "required": bool(coupons) and not skipped,
        "coupons": coupons,
        "total": sum(c["count"] for c in coupons),
        "one_click_all": True,
        "selectable": False,
        "irreversible": True,
        "skipped": skipped,
    }


def _target_loopback(client: Any) -> bool:
    try:
        net_guard.assert_write_target(getattr(client, "endpoint", None))
        return True
    except net_guard.NetGuardError:
        return False


def public_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """账本记录 → 对外摘要（附账本当前状态快照；不含任何敏感值）。"""
    summary = copy.deepcopy(record["summary"])
    steps = record["steps"]
    summary["state"] = {
        "claim": steps["claim"]["state"],
        "requote": steps["requote"]["state"],
        "order": steps["order"]["state"],
    }
    summary["supersedes"] = record.get("supersedes")
    summary["superseded_by"] = record.get("superseded_by")
    return summary


def _exists_or_create(ledger: Ledger, digest: str, **kwargs: Any) -> Dict[str, Any]:
    """同 digest 已存在则复用（同输入同时钟的幂等），否则创建。"""
    try:
        return ledger.read(digest)
    except LedgerCorrupt:
        raise
    except LedgerError:
        pass
    return ledger.create(digest, **kwargs)


def check_ttl(ttl_seconds: Any) -> int:
    """TTL 必须在集中定义的范围内（60–1800 秒）。"""
    low, high = SUMMARY_TTL_RANGE
    if type(ttl_seconds) is not int or not low <= ttl_seconds <= high:
        raise InputError(f"--ttl-seconds 必须在 {low}–{high} 秒之间")
    return ttl_seconds


def build_summary(
    planner: Any,
    request: Dict[str, Any],
    ledger: Ledger,
    *,
    signature: Optional[str] = None,
    rank: Optional[int] = None,
    take_way: Optional[int] = None,
    skip_claim: bool = False,
    ttl_seconds: int = SUMMARY_TTL_SECONDS,
    city: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """计划 D13：选方案 → 重建 → 重新核价 → 读将领券 → 组装摘要与 digest → 写账本。

    返回 {"status", "exit_code", "message", ...}；超预算、需选取餐方式、零选项时不落账（退出码 3）。
    """
    check_ttl(ttl_seconds)
    if signature is not None and rank is not None:
        raise InputError("--signature 与 --rank 只能二选一")
    result, private = planner._plan_v2_core(request)
    ranking = result["ranking"]
    if not ranking:
        return {"status": "no_plan", "exit_code": EXIT_NO_PLAN,
                "message": "没有可确认的预算内方案，未生成摘要"}
    if signature is not None:
        if not _SIGNATURE.match(signature):
            raise InputError("--signature 需为 8–16 位小写十六进制方案编号")
        hits = [r for r in ranking if r["signature"].startswith(signature)]
        if not hits:
            raise InputError("未找到该方案编号（只能选择本次排名中的方案）")
        if len(hits) > 1:
            raise InputError("方案编号前缀不唯一，请提供更长的编号")
        chosen = hits[0]
    else:
        position = 1 if rank is None else rank
        if type(position) is not int or not 1 <= position <= len(ranking):
            raise InputError(f"--rank 需在 1–{len(ranking)} 之间")
        chosen = ranking[position - 1]
    req = private["req"]
    resolved = private["resolved"]
    wire = resolved["wire"]
    spec = FULFILLMENTS[req["fulfillment"]["type"]]
    client = planner.client
    cand = private["candidates"][chosen["signature"]]
    lines = lines_from_candidate(cand)
    try:
        rebuilt = rebuild_basket(client, wire, lines)
    except MenuChanged as exc:
        return {"status": "menu_changed", "exit_code": EXIT_RECONFIRM, "message": str(exc) + "，请重新比价后再生成摘要"}
    quote = quote_basket(client, wire, rebuilt["wire_items"])
    if quote["total_cents"] > req["budget_cents"]:
        return {
            "status": "over_budget",
            "exit_code": EXIT_RECONFIRM,
            "message": "官方重新核价后超出预算，未生成可确认摘要",
            "total_cents": quote["total_cents"],
            "budget_cents": req["budget_cents"],
        }
    take_way_code: Optional[str] = None
    options = quote["take_way_options"]
    selected_index: Optional[int] = None
    if spec.order_type == 1:
        indexed = [{"index": i, "title": o["title"], "subtitle": o["subtitle"]} for i, o in enumerate(options, 1)]
        if not options:
            return {"status": "no_take_way", "exit_code": EXIT_RECONFIRM,
                    "message": "官方核价未返回可用的取餐方式，不能生成可下单摘要"}
        if take_way is None:
            if len(options) > 1:
                return {"status": "need_take_way", "exit_code": EXIT_RECONFIRM,
                        "message": "有多个取餐方式，请用 --take-way N 选择后重新生成摘要",
                        "options": indexed}
            selected_index = 1
        else:
            if type(take_way) is not int or not 1 <= take_way <= len(options):
                raise InputError(f"--take-way 需在 1–{len(options)} 之间")
            selected_index = take_way
        take_way_code = options[selected_index - 1]["code"]
    else:
        indexed = []
        if take_way is not None:
            raise InputError("外送不需要 --take-way")
    claimable = [] if skip_claim else claim_set(client)
    store_name, name_source = resolved.get("store_name"), "delivery-query-stores"
    if store_name is None:
        name_source = "未核实"
        if spec.order_type == 1 and city and keyword:
            try:
                rows = client.call(
                    "query-nearby-stores",
                    {"beType": wire["beType"], "searchType": 2, "city": city, "keyword": keyword},
                ).get("data", [])
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and str(row.get("storeCode")) == wire["storeCode"] and row.get("storeName"):
                        store_name, name_source = str(row["storeName"]), "query-nearby-stores"
                        break
            except McpError:
                pass
    address = resolved.get("address")
    fulfillment = {
        "type": spec.key,
        "be_type": spec.be_type,
        "order_type": spec.order_type,
        "be_code": wire.get("beCode"),
        "address_index": req["fulfillment"].get("address_index"),
        "address_ref": address.ref if address is not None else None,
    }
    now = utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    mat_lines = material_lines(rebuilt["wire_items"])
    material = _material(wire["storeCode"], fulfillment, mat_lines, take_way_code, quote, claimable, now, expires)
    assert_material_clean(material)
    digest = compute_digest(material)
    people = req["people"]
    by_person = {a["person_id"]: a for a in chosen["assignments"]}
    per_person = [
        {"person_id": p["id"], "offer_id": by_person[p["id"]]["offer_id"], "name": by_person[p["id"]]["name"],
         "items": [dict(i) for i in by_person[p["id"]]["items"]]}
        for p in people
    ]
    groups = []
    for group in group_people(people):
        ids = [p["id"] for p in group["people"]]
        meals = Counter(by_person[i]["offer_id"] for i in ids)
        groups.append(
            {
                "label": group["label"],
                "size": len(ids),
                "person_ids": ids,
                "meals": [
                    {"offer_id": oid, "name": next(x["name"] for x in per_person if x["offer_id"] == oid),
                     "items": next(x["items"] for x in per_person if x["offer_id"] == oid), "count": n}
                    for oid, n in sorted(meals.items())
                ],
            }
        )
    summary = {
        "schema": "meal-clear/order-summary/1",
        "digest": digest,
        "digest_short": digest[:12],
        "mode": "live",
        "data_source": DATA_SOURCE,
        "created_at": iso(now),
        "quoted_at": iso(now),
        "expires_at": iso(expires),
        "ttl_seconds": ttl_seconds,
        "store": {"store_code": wire["storeCode"], "name": store_name, "name_source": name_source},
        "fulfillment": {
            "type": spec.key,
            "label": spec.label,
            "be_type": spec.be_type,
            "order_type": spec.order_type,
            **({"be_code": wire["beCode"]} if "beCode" in wire else {}),
            **({"address_label": address.label, "address_index": fulfillment["address_index"],
                "address_ref": address.ref} if address is not None else {}),
        },
        "people_count": len(people),
        "groups": groups,
        "per_person": per_person,
        "lines": _public_lines(rebuilt["offers"], lines),
        "payable_cents": quote["total_cents"],
        "fee_breakdown": quote["fee_breakdown"],
        "budget_cents": req["budget_cents"],
        "within_budget": True,
        "take_way": {
            "required": spec.order_type == 1,
            "options": indexed,
            "selected_index": selected_index,
            "selected_code": take_way_code,
        },
        "claim": _claim_block(claimable, skip_claim),
        "plan_ref": {"request_id": result["request_id"], "signature": chosen["signature"], "rank": chosen["rank"]},
        "execution": {
            "target_loopback": _target_loopback(client),
            "real_orders_open": False,
            "message": REAL_ENV_MESSAGE,
        },
    }
    assert_public(summary)
    rebuild = {
        "wire_context": dict(wire),
        "address_index": fulfillment["address_index"],
        "address_ref": fulfillment["address_ref"],
        "take_way_code": take_way_code,
        "lines": lines,
        "store_coupon_keys": rebuilt["coupon_keys"],
    }
    record = _exists_or_create(
        ledger,
        digest,
        basket_key=basket_key(material),
        created_at=iso(now),
        expires_at=iso(expires),
        options={"skip_claim": skip_claim, "ttl_seconds": ttl_seconds},
        summary=summary,
        material=material,
        rebuild=rebuild,
        claim_required=summary["claim"]["required"],
    )
    # 同 digest 复用时，“谁吃哪份/方案来源”不属于 digest，但展示应与本次请求一致
    soft = ("per_person", "groups", "plan_ref", "people_count")
    if any(record["summary"].get(k) != summary[k] for k in soft):
        record = ledger.mutate(digest, lambda rec: rec["summary"].update({k: summary[k] for k in soft}))
    return {"status": "ok", "exit_code": EXIT_OK, "digest": digest, "record": record,
            "summary": public_summary(record)}


# --- 新鲜度比较、取代 ---
def _brief(lines: List[Dict[str, Any]]) -> List[str]:
    return [f"{l['product_code']}×{l['quantity']}" + ("（券）" if l.get("coupon_key") else "") for l in lines]


def compare_fresh(material: Dict[str, Any], fresh: Dict[str, Any]) -> List[Dict[str, Any]]:
    """语义比较（排除 quote_window）：总额、费用拆分、商品行、取餐方式、地址引用。"""
    changes: List[Dict[str, Any]] = []
    if fresh["total_cents"] != material["total_cents"]:
        changes.append({"kind": "price", "field": "total_cents", "before": material["total_cents"],
                        "after": fresh["total_cents"],
                        "delta_cents": fresh["total_cents"] - material["total_cents"]})
    for key in FEE_KEYS:
        if fresh["fee_breakdown"].get(key) != material["fee_breakdown"].get(key):
            changes.append({"kind": "fee", "field": key, "before": material["fee_breakdown"].get(key),
                            "after": fresh["fee_breakdown"].get(key)})
    if canonical_json(fresh["lines"]) != canonical_json(material["lines"]):
        changes.append({"kind": "items", "field": "lines", "before": _brief(material["lines"]),
                        "after": _brief(fresh["lines"])})
    wanted = (material.get("take_way") or {}).get("code")
    if wanted is not None and wanted not in [o["code"] for o in fresh["take_way_options"]]:
        changes.append({"kind": "take_way", "field": "take_way_code", "before": "已选取餐方式",
                        "after": "不在最新选项内"})
    ref = material["fulfillment"].get("address_ref")
    if ref is not None and fresh.get("address_ref") not in (None, ref):
        changes.append({"kind": "address", "field": "address_ref", "before": ref, "after": fresh["address_ref"]})
    return changes


def verify_fresh(client: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    """重建篮子 → query-store-coupons → calculate-price → 语义比较（领券后与下单前共用）。"""
    rebuild = record["rebuild"]
    wire = rebuild["wire_context"]
    try:
        rebuilt = rebuild_basket(client, wire, rebuild["lines"])
    except MenuChanged as exc:
        return {"consistent": False, "fresh": None, "notes": [],
                "changes": [{"kind": "menu", "field": "menu_or_coupon", "before": "原商品/券", "after": str(exc)}]}
    quote = quote_basket(client, wire, rebuilt["wire_items"])
    fresh = dict(quote, lines=material_lines(rebuilt["wire_items"]), rebuilt=rebuilt)
    changes = compare_fresh(record["material"], fresh)
    notes = []
    if rebuilt["coupon_keys"] != rebuild.get("store_coupon_keys"):
        notes.append("本店可用券集合与生成摘要时不同；价格若未变不阻塞，建议重新 order-summary 以便用新券比价")
    return {"consistent": not changes, "fresh": fresh, "changes": changes, "notes": notes}


def supersede(
    ledger: Ledger,
    old: Dict[str, Any],
    fresh: Optional[Dict[str, Any]],
    *,
    claimable: List[Dict[str, Any]],
    claim_done: bool,
    now: datetime,
) -> Optional[Dict[str, Any]]:
    """用最新核价生成新摘要（新 digest、新 TTL）；旧记录标记 superseded_by。

    无法安全生成（菜单/券已变、取餐方式失效、超预算）时返回 None。
    """
    if fresh is None:
        return None
    old_mat = old["material"]
    wanted = (old_mat.get("take_way") or {}).get("code")
    if wanted is not None and wanted not in [o["code"] for o in fresh["take_way_options"]]:
        return None
    if fresh["total_cents"] > old["summary"]["budget_cents"]:
        return None
    ttl = int(old["options"].get("ttl_seconds", SUMMARY_TTL_SECONDS))
    coupons = [] if (claim_done or old["options"].get("skip_claim")) else claimable
    expires = now + timedelta(seconds=ttl)
    material = copy.deepcopy(old_mat)
    material.update(
        lines=fresh["lines"],
        total_cents=fresh["total_cents"],
        fee_breakdown=fresh["fee_breakdown"],
        claim_coupons=sorted(coupons, key=lambda c: (c["name"], c["label"])),
        quote_window={"quoted_at": iso(now), "expires_at": iso(expires)},
    )
    assert_material_clean(material)
    digest = compute_digest(material)
    if digest == old["digest"]:
        return None
    summary = copy.deepcopy(old["summary"])
    rebuilt_lines = old["rebuild"]["lines"]
    summary.update(
        digest=digest,
        digest_short=digest[:12],
        created_at=iso(now),
        quoted_at=iso(now),
        expires_at=iso(expires),
        lines=_public_lines(fresh["rebuilt"]["offers"], rebuilt_lines),
        payable_cents=fresh["total_cents"],
        fee_breakdown=fresh["fee_breakdown"],
        claim=_claim_block(coupons, bool(old["options"].get("skip_claim"))),
    )
    summary["take_way"]["options"] = [
        {"index": i, "title": o["title"], "subtitle": o["subtitle"]}
        for i, o in enumerate(fresh["take_way_options"], 1)
    ] if wanted is not None else []
    if wanted is not None:
        codes = [o["code"] for o in fresh["take_way_options"]]
        summary["take_way"]["selected_index"] = codes.index(wanted) + 1
    assert_public(summary)
    rebuild = dict(old["rebuild"], store_coupon_keys=fresh["rebuilt"]["coupon_keys"])
    new = _exists_or_create(
        ledger,
        digest,
        basket_key=basket_key(material),
        created_at=iso(now),
        expires_at=iso(expires),
        options=dict(old["options"]),
        summary=summary,
        material=material,
        rebuild=rebuild,
        supersedes=old["digest"],
        claim_required=summary["claim"]["required"],
    )
    if claim_done:
        def inherit(rec: Dict[str, Any]) -> None:
            if rec["steps"]["claim"]["state"] in ("none", "not_required"):
                rec["steps"]["claim"].update(state="succeeded", finished_at=iso(now),
                                             detail={"inherited": True})
                rec["events"].append({"at": iso(now), "event": "claim_inherited"})

        new = ledger.mutate(digest, inherit)

    def mark(rec: Dict[str, Any]) -> None:
        rec["superseded_by"] = digest
        rec["events"].append({"at": iso(now), "event": "superseded"})

    ledger.mutate(old["digest"], mark)
    return new


def is_fresh(record: Dict[str, Any], now: datetime) -> bool:
    """now < expires_at 且 now ≥ created_at − 60s（拒绝时钟回拨造成的异常）。"""
    return parse_time(record["created_at"]) - CLOCK_SKEW <= now < parse_time(record["expires_at"])


# --- 领券 ---
def _load(ledger: Ledger, prefix: str) -> Dict[str, Any]:
    return ledger.read(ledger.resolve(prefix))


def _prior(record: Dict[str, Any]) -> Dict[str, Any]:
    steps = record["steps"]
    return {
        "digest": record["digest"],
        "claim_state": steps["claim"]["state"],
        "claim_finished_at": steps["claim"].get("finished_at"),
        "requote_state": steps["requote"]["state"],
        "superseded_by": record.get("superseded_by"),
    }


def _reconfirm_reason(record: Dict[str, Any], now: datetime) -> Optional[OrderError]:
    if record.get("superseded_by"):
        return OrderError("该摘要已被新摘要取代，旧确认失效；请改用新摘要重新确认", EXIT_RECONFIRM,
                          {"status": "superseded", "superseded_by": record["superseded_by"]})
    if not is_fresh(record, now):
        return OrderError("摘要已过期，必须重新生成摘要并重新核价", EXIT_RECONFIRM, {"status": "expired"})
    return None


def claim_preview(client: Any, ledger: Ledger, prefix: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """只读预演：读 available-coupons 与摘要比较；不写账本、不碰写通道。"""
    record = _load(ledger, prefix)
    moment = now or utcnow()
    summary = public_summary(record)
    out: Dict[str, Any] = {
        "digest": record["digest"],
        "summary": summary,
        "executed": False,
        "note": "本次未调用 auto-bind-coupons",
        "one_click_all": True,
        "selectable": False,
        "irreversible": True,
    }
    blocked = _reconfirm_reason(record, moment)
    if blocked is not None:
        out.update(status=blocked.payload["status"], exit_code=EXIT_RECONFIRM, message=str(blocked))
        return out
    state = record["steps"]["claim"]["state"]
    if state == "not_required":
        out.update(status="not_required", exit_code=EXIT_OK, message="该摘要无需领券")
        return out
    if state != "none":
        out.update(status="already_done", exit_code=EXIT_OK, message="该摘要已有领券记录，不能再次领券",
                   prior=_prior(record))
        return out
    current = claim_set(client)
    matches = current == record["material"]["claim_coupons"]
    out.update(claimable_now=current, matches=matches)
    if matches:
        out.update(status="preview", exit_code=EXIT_OK, message="预演完成：未领取任何券")
    else:
        out.update(status="claimable_changed", exit_code=EXIT_RECONFIRM,
                   message="当前可领券与摘要不同，需重新生成摘要后再确认")
    return out


def precheck_claim(ledger: Ledger, prefix: str, confirm: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """离线校验（digest 完整匹配/未过期/未被取代/claim.required/状态 none）；不联网。"""
    record = _load(ledger, prefix)
    if not isinstance(confirm, str) or confirm != record["digest"] or not re.match(r"\A[0-9a-f]{64}\Z", confirm):
        raise gate.WriteDenied(gate.MSG_DIGEST)
    blocked = _reconfirm_reason(record, now or utcnow())
    if blocked is not None:
        raise blocked
    state = record["steps"]["claim"]["state"]
    if state == "not_required":
        raise OrderError("该摘要无需领券（没有可领券或已选择跳过）", EXIT_REFUSED, {"status": "not_required"})
    if state != "none":
        raise OrderError("该摘要已有领券记录，拒绝重复执行；先前结果见下", EXIT_DUPLICATE,
                         {"status": "duplicate", "prior": _prior(record)})
    return record


def _check_claim_tool(client: Any) -> None:
    tools = {t.get("name"): t for t in client.list_tools()["tools"]}
    tool = tools.get(TOOL_AUTO_BIND_COUPONS)
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else None
    if not isinstance(required, list) or required:
        raise OrderError("服务端领券工具缺失或出现必填参数，与预期不符，已拒绝执行", EXIT_REFUSED)


def _fail_step(ledger: Ledger, digest: str, attempt: Any, state: str, reason: str) -> None:
    ledger.finish_step(digest, "claim", attempt, state, {"reason": reason})


def claim_execute(client: Any, ledger: Ledger, prefix: str, confirm: Optional[str]) -> Dict[str, Any]:
    """计划 D15/D16/D17：离线校验 → 回环检查 → 工具校验 → 将领券比较 → 写前账本 → 单次写 → 重新核价。"""
    record = precheck_claim(ledger, prefix, confirm)
    digest = record["digest"]
    gate.check_write_target(getattr(client, "endpoint", None))
    budgeted = client if hasattr(client, "spend") else live.BudgetedClient(client)
    _check_claim_tool(budgeted)
    current = claim_set(budgeted)
    if current != record["material"]["claim_coupons"]:
        latest = ledger.read(digest)
        if latest["steps"]["claim"]["state"] != "none":
            raise OrderError("该摘要已有领券记录，拒绝重复执行", EXIT_DUPLICATE,
                             {"status": "duplicate", "prior": _prior(latest)})
        check = verify_fresh(budgeted, latest)
        new = supersede(ledger, latest, check["fresh"], claimable=current, claim_done=False, now=utcnow())
        return _reconfirm_result(ledger, latest, check, new, "将领券集合已变化，旧确认失效，未领取任何券")
    now = utcnow()
    guard = gate.WriteGate.open(
        budgeted,
        execute=True,
        confirm_digest=confirm,
        record_digest=digest,
        expires_at=parse_time(record["expires_at"]),
        now=now,
        ledger=ledger,
    )
    try:
        attempt = ledger.begin_step(digest, "claim", ("none",))
    except LedgerStateError:
        raise OrderError("该摘要已有领券记录，拒绝重复执行", EXIT_DUPLICATE,
                         {"status": "duplicate", "prior": _prior(ledger.read(digest))}) from None
    try:
        guard.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
    except WriteUnknown as exc:
        _fail_step(ledger, digest, attempt, "unknown", "result_unknown")
        raise OrderError(str(exc) + "；请先用 available-coupons 或 App 核对，不要重试", EXIT_UNKNOWN) from None
    except WriteFailed as exc:
        _fail_step(ledger, digest, attempt, "failed", "rejected")
        raise OrderError(str(exc), EXIT_ERROR) from None
    except (gate.WriteDenied, WriteRefused, McpError) as exc:
        _fail_step(ledger, digest, attempt, "failed", "refused_before_send")
        if isinstance(exc, gate.WriteDenied):
            raise
        raise OrderError(str(exc), EXIT_ERROR) from None
    except Exception:
        _fail_step(ledger, digest, attempt, "unknown", "unexpected")
        raise OrderError("领券出现未预期错误，结果未知；请先核对，不要重试", EXIT_UNKNOWN) from None
    ledger.finish_step(digest, "claim", attempt, "succeeded", {"bound": True})
    latest = ledger.read(digest)
    try:
        check = verify_fresh(budgeted, latest)
    except (McpError, InputError):
        ledger.mutate(digest, lambda rec: rec["steps"]["requote"].update(state="error", checked_at=iso(utcnow())))
        raise OrderError("领券已完成，但重新核价失败；请重新生成摘要后再确认", EXIT_ERROR,
                         {"status": "claimed_requote_error"}) from None
    if check["consistent"]:
        ledger.mutate(digest, lambda rec: rec["steps"]["requote"].update(
            state="consistent", checked_at=iso(utcnow()), changes=[]))
        return {"status": "claimed", "exit_code": EXIT_OK, "digest": digest, "requote": "consistent",
                "changes": [], "notes": check["notes"], "executed": True,
                "message": "已一键领取全部可领券；领券后重新核价，价格与摘要一致"}
    new = supersede(ledger, latest, check["fresh"], claimable=[], claim_done=True, now=utcnow())
    return _reconfirm_result(ledger, latest, check, new, "已一键领取全部可领券；领券后重新核价发现变化，旧摘要作废")


def _reconfirm_result(ledger: Ledger, old: Dict[str, Any], check: Dict[str, Any],
                      new: Optional[Dict[str, Any]], message: str) -> Dict[str, Any]:
    """记录 requote=changed，并返回需重新确认的结果（含新摘要）。"""
    changes = check["changes"]
    ledger.mutate(old["digest"], lambda rec: rec["steps"]["requote"].update(
        state="changed", checked_at=iso(utcnow()), changes=changes))
    result: Dict[str, Any] = {
        "status": "reconfirm",
        "exit_code": EXIT_RECONFIRM,
        "digest": old["digest"],
        "requote": "changed",
        "changes": changes,
        "notes": check["notes"],
        "executed": old["steps"]["claim"]["state"] == "succeeded" or False,
        "message": message,
    }
    if new is not None:
        result["new_digest"] = new["digest"]
        result["summary"] = public_summary(new)
    else:
        result["message"] += "；无法自动生成新摘要，请重新运行 order-summary"
    return result
