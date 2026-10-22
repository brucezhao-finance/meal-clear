"""取餐方式校验与官方参数映射。"""

from __future__ import annotations

from typing import Any, Dict

from .config import DEFAULT_FULFILLMENT, FULFILLMENTS
from .core import InputError
from .group_meal import parse_reservation


def validate_fulfillment(raw: Any) -> Dict[str, Any]:
    """校验 fulfillment 对象；省略即到店自取。"""
    if raw is None:
        raw = {"type": DEFAULT_FULFILLMENT}
    if not isinstance(raw, dict):
        raise InputError("fulfillment 必须为对象")
    kind = raw.get("type", DEFAULT_FULFILLMENT)
    if not isinstance(kind, str) or kind not in FULFILLMENTS:
        raise InputError("fulfillment.type 必须是 " + "/".join(FULFILLMENTS))
    spec = FULFILLMENTS[kind]
    extra_keys = set()
    if spec.needs_gm_service:
        extra_keys.add("gm_service_code")
    if spec.supports_reservation:
        extra_keys.add("reservation_date")
    if set(raw) - {"type", "be_code", "address_index"} - extra_keys:
        raise InputError("fulfillment 仅支持 type、be_code、address_index"
                         + ("、gm_service_code、reservation_date" if extra_keys else ""))
    result: Dict[str, Any] = {"type": kind}
    be_code = raw.get("be_code")
    if spec.be_code == "none":
        if be_code is not None:
            raise InputError(f"{spec.label}不需要 be_code，请勿提供")
    else:
        if not isinstance(be_code, str) or not be_code.strip():
            raise InputError(f"{spec.label}必须提供非空 be_code")
        result["be_code"] = be_code
    index = raw.get("address_index")
    if spec.needs_address:
        if type(index) is not int or index < 1:
            raise InputError(f"{spec.label}必须提供从1开始的 address_index")
        result["address_index"] = index
    elif index is not None:
        raise InputError(f"{spec.label}不需要 address_index，请勿提供")
    if spec.needs_gm_service and raw.get("gm_service_code") is not None:
        gm = raw["gm_service_code"]
        if not isinstance(gm, str) or not gm.strip():
            raise InputError("gm_service_code 必须是非空字符串")
        result["gm_service_code"] = gm
    if spec.supports_reservation and raw.get("reservation_date") is not None:
        parse_reservation(raw["reservation_date"])
        result["reservation_date"] = raw["reservation_date"]
    return result


def wire_context(req: Dict[str, Any]) -> Dict[str, Any]:
    """生成官方工具的公共入参 storeCode/orderType/beType[/beCode]。"""
    ful = validate_fulfillment(req.get("fulfillment"))
    spec = FULFILLMENTS[ful["type"]]
    wire: Dict[str, Any] = {
        "storeCode": req["context"]["store_code"],
        "orderType": spec.order_type,
        "beType": spec.be_type,
    }
    if "be_code" in ful:
        wire["beCode"] = ful["be_code"]
    if spec.supports_reservation and "reservation_date" in ful:
        wire["reservationDate"] = ful["reservation_date"]
    return wire


def price_extra(req: Dict[str, Any]) -> Dict[str, Any]:
    """仅 calculate-price/create-order 使用的额外入参（团餐助餐服务编码）。"""
    ful = validate_fulfillment(req.get("fulfillment"))
    if FULFILLMENTS[ful["type"]].needs_gm_service and "gm_service_code" in ful:
        return {"gmServiceCode": ful["gm_service_code"]}
    return {}


def context_key(req: Dict[str, Any]) -> Dict[str, Any]:
    """离线菜单匹配键：门店、用餐时间、取餐方式与 be_code。"""
    ful = validate_fulfillment(req.get("fulfillment"))
    key = {
        "store_code": req["context"]["store_code"],
        "meal_time": req["context"]["meal_time"],
        "fulfillment": ful["type"],
        "be_code": ful.get("be_code"),
    }
    if ful["type"] == "group":
        key["reservation_date"] = ful.get("reservation_date")
    return key
