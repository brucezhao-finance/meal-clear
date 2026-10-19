"""取餐方式校验与官方参数映射。"""

from __future__ import annotations

from typing import Any, Dict

from .config import DEFAULT_FULFILLMENT, FULFILLMENTS
from .core import InputError


def validate_fulfillment(raw: Any) -> Dict[str, Any]:
    """校验 fulfillment 对象；省略即到店自取。"""
    if raw is None:
        raw = {"type": DEFAULT_FULFILLMENT}
    if not isinstance(raw, dict):
        raise InputError("fulfillment 必须为对象")
    if set(raw) - {"type", "be_code", "address_index"}:
        raise InputError("fulfillment 仅支持 type、be_code、address_index")
    kind = raw.get("type", DEFAULT_FULFILLMENT)
    if not isinstance(kind, str) or kind not in FULFILLMENTS:
        raise InputError("fulfillment.type 必须是 " + "/".join(FULFILLMENTS))
    spec = FULFILLMENTS[kind]
    if not spec.implemented:
        raise InputError(f"{spec.label}(beType={spec.be_type})尚未实现，后续阶段提供")
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
    return wire


def context_key(req: Dict[str, Any]) -> Dict[str, Any]:
    """离线菜单匹配键：门店、用餐时间、取餐方式与 be_code。"""
    ful = validate_fulfillment(req.get("fulfillment"))
    return {
        "store_code": req["context"]["store_code"],
        "meal_time": req["context"]["meal_time"],
        "fulfillment": ful["type"],
        "be_code": ful.get("be_code"),
    }
