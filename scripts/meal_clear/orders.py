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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from . import gate, live, net_guard, realwrite
from .config import (
    CHANNEL_OFFICIAL,
    DIGEST_VERSION,
    DUPLICATE_BASKET_WINDOW_SECONDS,
    EXIT_DUPLICATE,
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_OK,
    EXIT_RECONFIRM,
    EXIT_REFUSED,
    EXIT_UNKNOWN,
    FULFILLMENTS,
    GROUP_REFUSAL_REASON,
    MSG_GROUP_PREVIEW_CLOSED,
    MSG_GROUP_REAL_WRITE_CLOSED,
    PHRASE_TAIL_LEN,
    RESOLVE_CHOICES,
    RESOLVE_CONFIRM_WORD,
    ORDER_STATUS_LABELS,
    SUMMARY_TTL_RANGE,
    SUMMARY_TTL_SECONDS,
    TOOL_AUTO_BIND_COUPONS,
    TOOL_CREATE_ORDER,
)
from .core import InputError
from .group_meal import is_group_record, split_budget
from .grouping import group_people
from .ledger import Ledger, LedgerCorrupt, LedgerError, LedgerStateError
from .mcp import BusinessError, McpError, WriteFailed, WriteRefused, WriteUnknown
from .privacy import assert_public, mask_addresses, sanitize_claimable, sanitize_held  # noqa: F401

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


def quote_basket(
    client: Any,
    wire_ctx: Dict[str, Any],
    wire_items: List[Dict[str, Any]],
    price_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对合并后的商品行重新 calculate-price；price_extra 仅团餐（gmServiceCode），旧账本记录缺省为空。"""
    payload = client.call(
        "calculate-price", {**wire_ctx, **(price_extra or {}), "items": wire_items, "needTableware": False}
    )
    return parse_quote(payload, wire_items)


def _quote(client: Any, wire: Dict[str, Any], items: List[Dict[str, Any]], extra: Any) -> Dict[str, Any]:
    """非团餐沿用三参调用（price_extra 为空时不传第四参，保持既有调用形态）。"""
    return quote_basket(client, wire, items, extra) if extra else quote_basket(client, wire, items)


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
    quote = _quote(client, wire, rebuilt["wire_items"], private.get("price_extra"))
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
    group_budget = None
    if spec.needs_gm_service:  # 团餐专属材料键仅 group 写入，非 group 的 digest 不变
        group_budget = split_budget(req)
        fulfillment.update(
            gm_service_code=req["fulfillment"].get("gm_service_code"),
            reservation_date=req["fulfillment"].get("reservation_date"),
            per_person_budget_cents=group_budget["per_person_cents"],
            people_count=group_budget["people_count"],
        )
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
            **(
                {
                    "gm_service_code": fulfillment["gm_service_code"],
                    "gm_service_name": ((private.get("group") or {}).get("selected") or {}).get("name"),
                    "reservation_date": fulfillment["reservation_date"],
                    "per_person_budget_cents": fulfillment["per_person_budget_cents"],
                }
                if spec.needs_gm_service
                else {}
            ),
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
        **({"group_budget": group_budget} if group_budget is not None else {}),
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
        "price_extra": dict(private.get("price_extra") or {}),
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
    quote = _quote(client, wire, rebuilt["wire_items"], rebuild.get("price_extra"))
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
    if _is_official(client) and is_group_record(record):
        out["note"] += "；" + MSG_GROUP_PREVIEW_CLOSED
        out["group_real_write_closed"] = True
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


LOCK_HINT = "该篮子已被锁定 24 小时：先用 order-status 或到麦当劳 App 核实，再用 order-resolve 标注结果"


def _is_official(client: Any) -> bool:
    """客户端目标是否为固定官方端点（走第四道门）；回环模拟服务返回 False。"""
    return net_guard.is_official_url(getattr(client, "endpoint", None))


def _refuse_official_group(client: Any, record: Dict[str, Any]) -> None:
    """第二层防线：官方端点 + 团餐记录一律拒绝（退出码 4），早于第四道门与任何网络动作。"""
    if _is_official(client) and is_group_record(record):
        raise OrderError(
            MSG_GROUP_REAL_WRITE_CLOSED,
            EXIT_REFUSED,
            {"status": "group_real_write_closed", "reason": GROUP_REFUSAL_REASON},
        )


def _confirm_lines(summary: Dict[str, Any], tool: str) -> List[str]:
    """真实写前给人看的要点（只含摘要里的公开字段，不含券码/地址/Token）。"""
    ful = summary["fulfillment"]
    store = summary["store"]
    lines = [
        "== 真实%s：请核对后亲手确认 ==" % ("领券" if tool == TOOL_AUTO_BIND_COUPONS else "下单"),
        "门店：%s（%s）" % (store["store_code"], store["name"] or "名称未核实"),
        "取餐方式：%s" % ful["label"],
        "商品：" + "；".join("%s×%s" % (r["name"], r["quantity"]) for r in summary["lines"]),
        "应付：¥%.2f" % (summary["payable_cents"] / 100),
    ]
    if tool == TOOL_AUTO_BIND_COUPONS:
        lines.append("将领券数：%d 张（一键领全部，无法逐张选择，不可撤销）" % summary["claim"]["total"])
    else:
        lines.append("订单创建后为待支付，本工具不会自动付款，也不会取消。")
    lines.append("摘要有效至：%s" % summary["expires_at"])
    return lines


def claim_execute(client: Any, ledger: Ledger, prefix: str, confirm: Optional[str]) -> Dict[str, Any]:
    """计划 D15/D16/D17：离线校验 → 回环检查 → 工具校验 → 将领券比较 → 写前账本 → 单次写 → 重新核价。"""
    record = precheck_claim(ledger, prefix, confirm)
    _refuse_official_group(client, record)
    digest = record["digest"]
    official = _is_official(client)
    session = realwrite.authorize(ledger, TOOL_AUTO_BIND_COUPONS, record, utcnow()) if official else None
    gate.check_write_target(getattr(client, "endpoint", None), session=session)
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
    approval = None
    if official:  # 真实领券前也必须重新核价（价格/券/商品不一致→退出码 3 + 新摘要）
        latest = ledger.read(digest)
        check = verify_fresh(budgeted, latest)
        if not check["consistent"]:
            new = supersede(ledger, latest, check["fresh"], claimable=current, claim_done=False, now=utcnow())
            return _reconfirm_result(ledger, latest, check, new, "领券前重新核价发现变化，未领取任何券，旧摘要作废")
        approval = session.confirm(_confirm_lines(public_summary(latest), TOOL_AUTO_BIND_COUPONS))
    now = utcnow()
    guard = gate.WriteGate.open(
        budgeted,
        execute=True,
        confirm_digest=confirm,
        record_digest=digest,
        expires_at=parse_time(record["expires_at"]),
        now=now,
        ledger=ledger,
        approval=approval,
    )
    try:
        if official:  # 全局锁内“计数→写前落账”原子完成，防两个进程同时通过上限
            with ledger.global_locked():
                session.check_counts()
                attempt = ledger.begin_step(digest, "claim", ("none",), channel=CHANNEL_OFFICIAL)
        else:
            attempt = ledger.begin_step(digest, "claim", ("none",))
    except LedgerStateError:
        raise OrderError("该摘要已有领券记录，拒绝重复执行", EXIT_DUPLICATE,
                         {"status": "duplicate", "prior": _prior(ledger.read(digest))}) from None
    try:
        guard.call(TOOL_AUTO_BIND_COUPONS, {}, attempt)
    except WriteUnknown as exc:
        _fail_step(ledger, digest, attempt, "unknown", "result_unknown")
        hint = "；请先用 available-coupons 或 App 核对，不要重试"
        raise OrderError(str(exc) + hint + ("。" + LOCK_HINT if official else ""), EXIT_UNKNOWN) from None
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
        done = {"status": "claimed", "exit_code": EXIT_OK, "digest": digest, "requote": "consistent",
                "changes": [], "notes": check["notes"], "executed": True,
                "message": "已一键领取全部可领券；领券后重新核价，价格与摘要一致"}
        if official:
            done["channel"] = CHANNEL_OFFICIAL
        return done
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


# --- 下单（仅回环模拟服务）与订单查询 ---
ORDER_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
CREATE_ORDER_REQUIRED_OK = frozenset({"storeCode", "orderType", "beType"})
UNKNOWN_HINT = "请先用 order-status 或到麦当劳 App 订单列表核对，不要重试；本工具不会自动重试"
MSG_ORDER_UNKNOWN = "下单请求结果未知（订单可能已创建）。" + UNKNOWN_HINT
MSG_FINISH_FAILED = "订单可能已处理，但账本终态未能写入，结果未知。" + UNKNOWN_HINT
_STATUS_CODE = re.compile(r"\A\d{1,3}\Z")
_TEXT_LIMIT = 60


def mask_order_id(order_id: Any) -> str:
    """订单号只显示打码尾号。"""
    text = str(order_id or "")
    return "****" + text[-4:] if len(text) > 4 else "****"


def money_to_cents(value: Any) -> Optional[int]:
    """官方金额字符串（元）→ 分；无法精确换算返回 None（不当作 0）。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    cents = amount * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def _clean_text(value: Any) -> Optional[str]:
    """展示用短文本：非字符串、含链接或手机号形态的值一律丢弃。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "http" in text.lower() or _PHONE.search(text):
        return None
    return text[:_TEXT_LIMIT]


def _status_of(detail: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(官方状态码, 中文标签, 非标准状态的原文) —— 缺失时全为 None。"""
    raw = detail.get("orderStatus")
    if raw in (None, ""):
        raw = detail.get("status")
    if raw in (None, "") or isinstance(raw, bool):
        return None, None, None
    text = str(raw).strip()
    if _STATUS_CODE.match(text) and text in ORDER_STATUS_LABELS:
        return text, ORDER_STATUS_LABELS[text], None
    return None, None, _clean_text(text)


def sanitize_order_detail(payload: Any) -> Dict[str, Any]:
    """计划 D20：只留状态、金额、商品名与数量、时间、门店名；丢弃配送信息、取餐码、取餐柜与支付链接。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise McpError("订单详情结构不匹配")
    code, label, other = _status_of(data)
    amount = money_to_cents(data.get("realTotalAmount"))
    if amount is None:
        amount = money_to_cents(data.get("totalAmount"))
    items = []
    rows = data.get("orderProductList")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = _clean_text(row.get("productName"))
        qty = row.get("quantity")
        if name and isinstance(qty, int) and not isinstance(qty, bool):
            items.append({"name": name, "quantity": qty})
    return {
        "status_code": code,
        "status_label": label,
        "official_status_text": other,
        "amount_cents": amount,
        "items": items,
        "created_at": _clean_text(data.get("createTime") or data.get("orderTime")),
        "expire_pay_time": _clean_text(data.get("expirePayTime")),
        "store_name": _clean_text(data.get("storeName")),
    }


def parse_created_order(payload: Any, payable_cents: int) -> Dict[str, Any]:
    """create-order 成功回包：要求非空 data.orderId；金额与摘要应付对比；支付信息仅供本次输出。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    order_id = data.get("orderId") if isinstance(data, dict) else None
    if not isinstance(order_id, str) or not order_id.strip() or not ORDER_ID_RE.match(order_id.strip()):
        raise McpError("成功码但缺少有效订单号")
    detail = data.get("orderDetail")
    detail = detail if isinstance(detail, dict) else {}
    code, label, other = _status_of(detail)
    amount = money_to_cents(detail.get("realTotalAmount"))
    if amount is None:
        amount = money_to_cents(detail.get("totalAmount"))
    if amount is None:
        check = "missing"
    else:
        check = "match" if amount == payable_cents else "mismatch"
    pay_url = data.get("payH5Url")
    pay_id = data.get("payId")
    return {
        "order_id": order_id.strip(),
        "pay_url": pay_url if isinstance(pay_url, str) and pay_url else None,
        "pay_id": str(pay_id) if isinstance(pay_id, (str, int)) and not isinstance(pay_id, bool) and str(pay_id) else None,
        "status_code": code,
        "status_label": label,
        "official_status_text": other,
        "amount_cents": amount,
        "amount_check": check,
        "expire_pay_time": _clean_text(detail.get("expirePayTime")),
    }


def build_create_order_args(
    record: Dict[str, Any], wire_items: List[Dict[str, Any]], address_id: Optional[str]
) -> Dict[str, Any]:
    """计划 §5.3：严格按 tools-schema 映射；不发送 remark/withOrder；仅团餐追加 gmServiceCode/reservationDate。"""
    material = record["material"]
    ful = material["fulfillment"]
    spec = FULFILLMENTS.get(ful.get("type"))
    if spec is None or not spec.implemented or ful.get("order_type") != spec.order_type or ful.get("be_type") != spec.be_type:
        raise InputError("该取餐方式暂不支持下单")
    args: Dict[str, Any] = {
        "storeCode": material["store_code"],
        "orderType": spec.order_type,
        "beType": spec.be_type,
    }
    if spec.be_code == "required":
        if not ful.get("be_code"):
            raise InputError("摘要缺少 beCode，不能下单")
        args["beCode"] = ful["be_code"]
    if spec.needs_gm_service:
        if not ful.get("gm_service_code"):
            raise InputError("团餐摘要缺少助餐服务编码（gm_service_code），不能下单")
        args["gmServiceCode"] = ful["gm_service_code"]
        if ful.get("reservation_date"):
            args["reservationDate"] = ful["reservation_date"]
    if spec.order_type == 1:
        code = (material.get("take_way") or {}).get("code")
        if not code:
            raise InputError("摘要缺少取餐方式选项，不能下单")
        args["takeWayCode"] = code
    else:
        if not address_id:
            raise InputError("外送需要解析出配送地址，不能下单")
        args["addressId"] = address_id
    if not wire_items:
        raise InputError("商品行为空，不能下单")
    args["items"] = copy.deepcopy(wire_items)
    args["needTableware"] = False
    return args


def _prior_order(record: Dict[str, Any]) -> Dict[str, Any]:
    """先前下单结果摘要（永不含订单号）。"""
    step = record["steps"]["order"]
    return {
        "digest": record["digest"],
        "order_state": step["state"],
        "status_label": step.get("status_label"),
        "finished_at": step.get("finished_at"),
        "claim_state": record["steps"]["claim"]["state"],
    }


def _basket_conflicts(ledger: Ledger, record: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """24 小时内同篮子且 pending/unknown/created 的其他摘要记录。"""
    hits = ledger.find_by_basket(record["basket_key"], DUPLICATE_BASKET_WINDOW_SECONDS, now)
    return [
        h for h in hits
        if h["digest"] != record["digest"] and h["steps"]["order"]["state"] in ("pending", "unknown", "created")
    ]


def _claim_blocker(record: Dict[str, Any]) -> Optional[OrderError]:
    """领券前置：claim∈{succeeded,not_required} 且 requote≠changed。"""
    steps = record["steps"]
    if steps["claim"]["state"] not in ("succeeded", "not_required"):
        return OrderError(
            "请先完成领券（order-claim），或重新生成不含领券的摘要（order-summary --skip-claim）后再下单",
            EXIT_REFUSED,
            {"status": "claim_required", "claim_state": steps["claim"]["state"]},
        )
    if steps["requote"]["state"] == "changed":
        return OrderError("领券后重新核价已发现变化，旧确认失效；请改用新摘要重新确认",
                          EXIT_REFUSED, {"status": "requote_changed"})
    return None


def _basket_error(conflicts: List[Dict[str, Any]]) -> OrderError:
    return OrderError(
        "24 小时内已有同一篮子的订单记录（执行中、结果未知或已创建），为防重复下单已拒绝；"
        "确认确需再下一单请加 --allow-duplicate-basket",
        EXIT_DUPLICATE,
        {
            "status": "duplicate_basket",
            "conflicts": [
                {"digest": h["digest"][:12], "order_state": h["steps"]["order"]["state"], "created_at": h["created_at"]}
                for h in conflicts
            ],
        },
    )


def precheck_place(
    ledger: Ledger,
    prefix: str,
    confirm: Optional[str],
    *,
    allow_duplicate_basket: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """离线校验：digest 完整匹配 / 重复 / 未过期未被取代 / claim 前置 / 同篮子守卫；不联网。"""
    record = _load(ledger, prefix)
    if not isinstance(confirm, str) or confirm != record["digest"] or not re.match(r"\A[0-9a-f]{64}\Z", confirm):
        raise gate.WriteDenied(gate.MSG_DIGEST)
    if record["steps"]["order"]["state"] != "none":
        raise OrderError("该摘要已有下单记录，拒绝重复下单；先前结果见下（不再显示订单号）", EXIT_DUPLICATE,
                         {"status": "duplicate", "prior": _prior_order(record)})
    moment = now or utcnow()
    blocked = _reconfirm_reason(record, moment)
    if blocked is not None:
        raise blocked
    claim_blocked = _claim_blocker(record)
    if claim_blocked is not None:
        raise claim_blocked
    if not allow_duplicate_basket:
        conflicts = _basket_conflicts(ledger, record, moment)
        if conflicts:
            raise _basket_error(conflicts)
    return record


def _check_create_tool(client: Any) -> None:
    tools = {t.get("name"): t for t in client.list_tools()["tools"]}
    tool = tools.get(TOOL_CREATE_ORDER)
    schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else None
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required) \
            or not set(required) <= CREATE_ORDER_REQUIRED_OK:
        raise OrderError("服务端下单工具缺失或必填参数与预期不符，已拒绝执行", EXIT_REFUSED)


def _resolve_address(client: Any, record: Dict[str, Any]) -> Optional[str]:
    """外送：执行期按 address_index 重新解析 addressId（仅内存），并校验 address_ref 与摘要一致。"""
    ful = record["material"]["fulfillment"]
    if ful.get("order_type") != 2:
        return None
    addresses = mask_addresses(client.call("delivery-query-addresses", {}))
    index = ful.get("address_index")
    if type(index) is not int or not 1 <= index <= len(addresses) or addresses[index - 1].ref != ful.get("address_ref"):
        raise OrderError("配送地址列表顺序可能已变化（地址引用与摘要不一致），未下单；请重新生成摘要并重新确认",
                         EXIT_RECONFIRM, {"status": "address_changed"})
    return addresses[index - 1].address_id


def _finish_order(ledger: Ledger, digest: str, attempt: Any, state: str, detail: Dict[str, Any],
                  order_id: Optional[str] = None) -> None:
    """落终态；账本写失败时按结果未知处理（不吞掉订单可能已创建的事实）。"""
    try:
        ledger.finish_step(digest, "order", attempt, state, detail, order_id=order_id)
    except Exception:
        raise OrderError(MSG_FINISH_FAILED, EXIT_UNKNOWN, {"status": "unknown"}) from None


def place_preview(client: Any, ledger: Ledger, prefix: str, *, allow_duplicate_basket: bool = False,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """只读预演：离线校验 + 只读新鲜度校验，展示将提交的内容；明确尚未调用下单工具。"""
    record = _load(ledger, prefix)
    moment = now or utcnow()
    summary = public_summary(record)
    out: Dict[str, Any] = {
        "digest": record["digest"],
        "summary": summary,
        "executed": False,
        "order_placed": False,
        "note": f"本次未调用 {TOOL_CREATE_ORDER}，没有创建任何订单",
        "will_submit": {
            "store_code": summary["store"]["store_code"],
            "store_name": summary["store"]["name"],
            "fulfillment": summary["fulfillment"]["label"],
            "lines": [{"name": l["name"], "quantity": l["quantity"]} for l in summary["lines"]],
            "payable_cents": summary["payable_cents"],
            "pay_by_you": True,
        },
    }
    if _is_official(client) and is_group_record(record):
        out["note"] += "；" + MSG_GROUP_PREVIEW_CLOSED
        out["group_real_write_closed"] = True
    if record["steps"]["order"]["state"] != "none":
        out.update(status="already_done", exit_code=EXIT_OK, message="该摘要已有下单记录，不能再次下单",
                   prior=_prior_order(record))
        return out
    blocked = _reconfirm_reason(record, moment)
    if blocked is not None:
        out.update(status=blocked.payload["status"], exit_code=EXIT_RECONFIRM, message=str(blocked))
        return out
    blockers: List[str] = []
    claim_blocked = _claim_blocker(record)
    if claim_blocked is not None:
        blockers.append(str(claim_blocked))
    if not allow_duplicate_basket:
        conflicts = _basket_conflicts(ledger, record, moment)
        if conflicts:
            blockers.append(str(_basket_error(conflicts)))
    check = verify_fresh(client, record)
    address_changed = None
    if check["consistent"]:
        try:
            _resolve_address(client, record)
        except OrderError as exc:
            address_changed = exc
    if not check["consistent"] or address_changed is not None:
        changes = check["changes"] if not check["consistent"] else [
            {"kind": "address", "field": "address_ref", "before": "摘要中的地址", "after": "地址列表顺序已变化"}]
        out.update(status="reconfirm", exit_code=EXIT_RECONFIRM, changes=changes, notes=check["notes"],
                   message="价格或内容已变化，旧确认失效；请重新运行 order-summary 生成新摘要并重新确认")
        return out
    out.update(status="preview", exit_code=EXIT_OK, requote="consistent", notes=check["notes"],
               ready=not blockers, blockers=blockers,
               message="预演完成：尚未下单" if not blockers else "预演完成：尚未下单；当前还不满足下单前置条件")
    return out


def place_execute(client: Any, ledger: Ledger, prefix: str, confirm: Optional[str], *,
                  allow_duplicate_basket: bool = False) -> Dict[str, Any]:
    """计划 D16/D18/D19：离线校验 → 回环 → 工具校验 → 新鲜度 → 写前账本 → 单次写 → 分类落账。"""
    record = precheck_place(ledger, prefix, confirm, allow_duplicate_basket=allow_duplicate_basket)
    _refuse_official_group(client, record)
    digest = record["digest"]
    official = _is_official(client)
    session = realwrite.authorize(ledger, TOOL_CREATE_ORDER, record, utcnow()) if official else None
    gate.check_write_target(getattr(client, "endpoint", None), session=session)
    budgeted = client if hasattr(client, "spend") else live.BudgetedClient(client)
    _check_create_tool(budgeted)
    try:
        check = verify_fresh(budgeted, record)
        address_id = _resolve_address(budgeted, record) if check["consistent"] else None
    except OrderError:
        raise
    except (McpError, InputError):
        raise OrderError("下单前重新核价失败，未下单；请稍后重新生成摘要", EXIT_ERROR, {"status": "requote_error"}) from None
    latest = ledger.read(digest)
    if not check["consistent"]:
        new = supersede(ledger, latest, check["fresh"], claimable=[],
                        claim_done=latest["steps"]["claim"]["state"] == "succeeded", now=utcnow())
        result = _reconfirm_result(ledger, latest, check, new, "下单前重新核价发现变化，未下单，旧摘要作废")
        result.update(executed=False, order_placed=False)
        return result
    ledger.mutate(digest, lambda rec: rec["steps"]["requote"].update(
        state="consistent", checked_at=iso(utcnow()), changes=[]))
    args = build_create_order_args(latest, check["fresh"]["rebuilt"]["wire_items"], address_id)
    approval = None
    if official:  # 金额上限用刚核价的应付额；人工确认在新鲜度检查之后、写前落账之前
        session.check_amount(check["fresh"]["total_cents"])
        approval = session.confirm(_confirm_lines(public_summary(latest), TOOL_CREATE_ORDER))
    now = utcnow()
    if not is_fresh(latest, now):
        raise OrderError("摘要已过期，必须重新生成摘要并重新核价", EXIT_RECONFIRM, {"status": "expired"})
    guard = gate.WriteGate.open(
        budgeted, execute=True, confirm_digest=confirm, record_digest=digest,
        expires_at=parse_time(latest["expires_at"]), now=now, ledger=ledger, approval=approval,
    )
    try:
        preconditions = {"claim": ("succeeded", "not_required")}
        if official:  # 全局锁内“计数→写前落账”原子完成
            with ledger.global_locked():
                session.check_counts()
                attempt = ledger.begin_step(digest, "order", ("none",), preconditions=preconditions,
                                            channel=CHANNEL_OFFICIAL)
        else:
            attempt = ledger.begin_step(digest, "order", ("none",), preconditions=preconditions)
    except LedgerStateError:
        again = ledger.read(digest)
        if again["steps"]["order"]["state"] != "none":
            raise OrderError("该摘要已有下单记录，拒绝重复下单", EXIT_DUPLICATE,
                             {"status": "duplicate", "prior": _prior_order(again)}) from None
        raise OrderError("前置步骤尚未满足，拒绝下单", EXIT_REFUSED, {"status": "claim_required"}) from None
    try:
        payload = guard.call(TOOL_CREATE_ORDER, args, attempt)
    except WriteUnknown:
        _finish_order(ledger, digest, attempt, "unknown", {"reason": "result_unknown"})
        raise OrderError(MSG_ORDER_UNKNOWN + ("。" + LOCK_HINT if official else ""), EXIT_UNKNOWN,
                         {"status": "unknown"}) from None
    except WriteFailed:
        _finish_order(ledger, digest, attempt, "failed", {"reason": "rejected"})
        raise OrderError("官方明确拒绝或未执行，订单未创建（以官方返回为准）；同一摘要不能重试，"
                         "如要继续请重新生成摘要并重新确认", EXIT_ERROR, {"status": "failed"}) from None
    except (gate.WriteDenied, WriteRefused, McpError) as exc:
        _finish_order(ledger, digest, attempt, "failed", {"reason": "refused_before_send"})
        if isinstance(exc, gate.WriteDenied):
            raise
        raise OrderError(str(exc), EXIT_ERROR, {"status": "failed"}) from None
    except Exception:
        _finish_order(ledger, digest, attempt, "unknown", {"reason": "unexpected"})
        raise OrderError(MSG_ORDER_UNKNOWN, EXIT_UNKNOWN, {"status": "unknown"}) from None
    try:
        info = parse_created_order(payload, latest["material"]["total_cents"])
    except Exception:
        _finish_order(ledger, digest, attempt, "unknown", {"reason": "bad_structure"})
        raise OrderError(MSG_ORDER_UNKNOWN, EXIT_UNKNOWN, {"status": "unknown"}) from None
    _finish_order(ledger, digest, attempt, "created",
                  {"official_status": info["status_code"] or "none", "amount_check": info["amount_check"]},
                  order_id=info["order_id"])
    try:
        ledger.mutate(digest, lambda rec: rec["steps"]["order"].update(status_label=info["status_label"]))
    except Exception:  # 标签只是展示；订单号与状态已落账
        pass
    lookup = None
    try:  # 尽力核对一次，失败不影响状态
        lookup = sanitize_order_detail(budgeted.call("query-order", {"orderId": info["order_id"]}))
    except Exception:
        lookup = None
    created_order = {"orderId": info["order_id"]}
    if info["pay_url"]:
        created_order["payH5Url"] = info["pay_url"]
    if info["pay_id"]:
        created_order["payId"] = info["pay_id"]
    created_result = {
        "status": "created",
        "exit_code": EXIT_OK,
        "digest": digest,
        "executed": True,
        "order_placed": True,
        "message": "订单已创建，尚未付款",
        "order": {
            "state": "created",
            "status_code": info["status_code"],
            "status_label": info["status_label"],
            "official_status_text": info["official_status_text"],
            "payable_cents": latest["material"]["total_cents"],
            "official_amount_cents": info["amount_cents"],
            "amount_check": info["amount_check"],
            "expire_pay_time": info["expire_pay_time"],
            "paid": False,
            "auto_pay": False,
            "auto_cancel": False,
        },
        "lookup": lookup,
        "created_order": created_order,
    }
    if official:
        created_result["channel"] = CHANNEL_OFFICIAL
    return created_result


def resolve_unknown_cmd(ledger: Ledger, prefix: str, step: str, outcome: str, human_confirm: Any) -> Dict[str, Any]:
    """人工标注真实通道中结果未知的步骤。先通过 TTY 短语确认，再写账本（事件留痕，不写订单号）。"""
    if (step, outcome) not in RESOLVE_CHOICES:
        raise InputError("--as 与 --step 不匹配：领券用 claimed/not-claimed，下单用 created/not-created")
    digest = ledger.resolve(prefix)
    record = ledger.read(digest)
    entry = record["steps"][step]
    if ledger.step_channel(entry) != CHANNEL_OFFICIAL or entry["state"] != "unknown":
        raise OrderError("只能标注真实通道中『结果未知』的步骤；回环模拟记录和其他状态不可标注",
                         EXIT_REFUSED, {"status": "not_resolvable", "state": entry["state"]})
    human_confirm(
        ["== 人工标注：你声明已核实真实结果，工具不会替你去官方核实 ==",
         "摘要：%s…；步骤：%s；声明：%s" % (digest[:12], step, outcome)],
        "%s %s" % (digest[-PHRASE_TAIL_LEN:], RESOLVE_CONFIRM_WORD),
    )
    try:
        updated = ledger.resolve_unknown(digest, step, outcome)
    except LedgerStateError as exc:
        raise OrderError(str(exc), EXIT_REFUSED, {"status": "not_resolvable"}) from None
    return {"status": "resolved", "exit_code": EXIT_OK, "digest": digest, "step": step, "as": outcome,
            "state": updated["steps"][step]["state"],
            "message": "已按你的声明标注；账本留有人工标注事件，工具未向官方核实"}


def order_status(client_factory: Any, ledger: Ledger, *, order_id: Optional[str] = None,
                 digest: Optional[str] = None) -> Dict[str, Any]:
    """计划 D20：--order-id 直接查；--digest 先读账本（无网络），有订单号再查。只读，不改账本。"""
    if (order_id is None) == (digest is None):
        raise InputError("需要且只能提供 --order-id 或 --digest 之一")
    local = None
    if digest is not None:
        record = _load(ledger, digest)
        step = record["steps"]["order"]
        local = {"digest": record["digest"], "order_state": step["state"],
                 "status_label": step.get("status_label"), "finished_at": step.get("finished_at")}
        order_id = step.get("order_id")
        if not order_id:
            hints = {
                "none": "该摘要尚未下单",
                "failed": "该摘要的下单被明确拒绝，订单未创建",
                "pending": "该摘要的下单正在执行或进程曾中断，结果未知。" + UNKNOWN_HINT,
                "unknown": "该摘要的下单结果未知。" + UNKNOWN_HINT,
            }
            return {"status": "local_only", "exit_code": EXIT_OK, "local": local,
                    "message": hints.get(step["state"], "账本中没有订单号")}
    elif not isinstance(order_id, str) or not ORDER_ID_RE.match(order_id):
        raise InputError("订单号格式无效（字母、数字、下划线、连字符，最长 64 位）")
    client = client_factory()
    try:
        payload = client.call("query-order", {"orderId": order_id})
    except BusinessError:
        return {"status": "not_found", "exit_code": EXIT_ERROR, "order_id_masked": mask_order_id(order_id),
                "local": local, "message": "官方未找到该订单（或不属于当前账号）。请到麦当劳 App 订单列表核对"}
    return {"status": "ok", "exit_code": EXIT_OK, "order_id_masked": mask_order_id(order_id),
            "order": sanitize_order_detail(payload), "local": local}
