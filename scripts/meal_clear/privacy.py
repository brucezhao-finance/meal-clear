"""敏感信息边界：地址脱敏、券字段白名单、输出自检。"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional

from .core import InputError

FORBIDDEN_KEYS = frozenset(
    {
        "couponcode",
        "couponid",
        "code",
        "phone",
        "mobile",
        "address",
        "fulladdress",
        "contactname",
        "contact",
        "addressid",
        "token",
        # P1 订单相关（计划 D22）
        "orderid",
        "payh5url",
        "payid",
        "pickupcode",
        "lockercode",
        "lockerqrcode",
        "mobilephone",
        "ridermobilephone",
        "addressdetail",
        "deliveryaddress",
        "customernickname",
    }
)


class ResolvedAddress:
    """地址仅保留序号、标签和内存中的 addressId；repr 不含地址内容。"""

    def __init__(self, index: int, address_id: str) -> None:
        """address_id 只驻留内存，不进入任何输出。"""
        self.index = index
        self.label = f"地址{index}"
        self._address_id = address_id

    @property
    def address_id(self) -> str:
        """供 delivery-query-stores 使用的内存值。"""
        return self._address_id

    @property
    def ref(self) -> str:
        """addressId 的短哈希，用于发现地址顺序变化。"""
        return hashlib.sha256(self._address_id.encode()).hexdigest()[:8]

    def public(self) -> Dict[str, Any]:
        """可对外输出的字段。"""
        return {"index": self.index, "label": self.label, "address_ref": self.ref}

    def __repr__(self) -> str:
        """脱敏表示。"""
        return f"ResolvedAddress({self.label})"


def _rows(payload: Any) -> List[Any]:
    """从常见响应结构（data / data.list 等）中取出列表。"""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("list", "addresses", "coupons", "records", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def mask_addresses(payload: Any) -> List[ResolvedAddress]:
    """把地址列表立即转换为序号+内存 addressId，丢弃其余全部字段。"""
    result = []
    for row in _rows(payload):
        if isinstance(row, dict) and row.get("addressId") not in (None, ""):
            result.append(ResolvedAddress(len(result) + 1, str(row["addressId"])))
    return result


def _pick(row: Any, keys: List[str]) -> Dict[str, Any]:
    """按白名单取标量字段。"""
    if not isinstance(row, dict):
        return {}
    return {
        k: row[k]
        for k in keys
        if k in row and isinstance(row[k], (str, int, bool, float))
    }


def sanitize_claimable(payload: Any) -> List[Dict[str, Any]]:
    """available-coupons：只留名称/标签/状态。"""
    keys = ["couponName", "label", "couponStatus"]
    return [r for r in (_pick(x, keys) for x in _rows(payload)) if r]


def sanitize_held(payload: Any) -> List[Dict[str, Any]]:
    """query-my-coupons：丢弃 code、id、图片。"""
    keys = ["title", "subtitle", "discountDesc", "datetimeText", "tradeEnd", "enable"]
    return [r for r in (_pick(x, keys) for x in _rows(payload)) if r]


def sanitize_store_coupons(payload: Any) -> List[Dict[str, Any]]:
    """query-store-coupons：留标题、时间与适用商品名；券标识由调用方另存内存。"""
    out = []
    for row in _rows(payload):
        item = _pick(row, ["title", "tradeDateTime"])
        products = row.get("products") if isinstance(row, dict) else None
        if isinstance(products, list):
            names = [
                p["name"]
                for p in products
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            ]
            if names:
                item["product_names"] = names
        if item:
            out.append(item)
    return out


def assert_public(value: Any, path: str = "$", allow: Optional[Iterable[str]] = None) -> None:
    """递归检查输出中不含敏感键名，命中即抛 InputError。

    allow：小写键名白名单，仅用于明确的受控场景（例如摘要材料里的公开菜单商品编码键 code）。
    """
    exempt = frozenset(k.lower() for k in (allow or ()))
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS and key.lower() not in exempt:
                raise InputError(f"输出包含敏感字段 {key}，已拦截")
            assert_public(child, path + "." + str(key), allow)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_public(child, path + "[]", allow)
