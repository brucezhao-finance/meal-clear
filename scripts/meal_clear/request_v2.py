"""v2 请求（schema_version=2）校验、修改与指纹。"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict

from .config import (
    DEFAULT_PRIORITY,
    DEFAULT_TOP_N,
    LIMITS,
    MAX_PEOPLE,
    MAX_TOP_N,
    PRIORITIES,
    default_limits,
)
from .core import InputError, integer, validate_people
from .fulfillment import validate_fulfillment

TOP_FIELDS = {
    "schema_version",
    "context",
    "fulfillment",
    "budget_cents",
    "people",
    "ranking",
    "limits",
}


def validate_request_v2(request: Dict[str, Any]) -> Dict[str, Any]:
    """校验 v2 请求并补全默认 fulfillment/ranking/limits。"""
    if not isinstance(request, dict):
        raise InputError("请求必须为对象")
    req = copy.deepcopy(request)
    if "channel" in req or "channel" in (req.get("context") or {}):
        raise InputError("v2 请求不支持 channel，请使用 fulfillment")
    if req.get("schema_version") != 2 or type(req.get("schema_version")) is not int:
        raise InputError("v2 请求需要 schema_version=2")
    if set(req) - TOP_FIELDS:
        raise InputError("请求包含不支持的字段")
    context = req.get("context")
    if not isinstance(context, dict):
        raise InputError("需要门店与用餐上下文")
    if set(context) != {"store_code", "meal_time"} or any(
        not isinstance(v, str) or not v.strip() for v in context.values()
    ):
        raise InputError("上下文必须包含非空 store_code、meal_time")
    req["fulfillment"] = validate_fulfillment(req.get("fulfillment"))
    integer(req.get("budget_cents"), "预算", 1)
    validate_people(
        req.get("people"), MAX_PEOPLE, f"支持1至{MAX_PEOPLE}位用餐者"
    )
    ranking = req.get("ranking", {})
    if not isinstance(ranking, dict) or set(ranking) - {"top_n", "priority"}:
        raise InputError("ranking 仅支持 top_n、priority")
    top_n = ranking.get("top_n", DEFAULT_TOP_N)
    if type(top_n) is not int or not 1 <= top_n <= MAX_TOP_N:
        raise InputError(f"ranking.top_n 必须是 1 至 {MAX_TOP_N} 的整数")
    priority = ranking.get("priority", DEFAULT_PRIORITY)
    if priority not in PRIORITIES:
        raise InputError("ranking.priority 必须是 " + "/".join(PRIORITIES))
    req["ranking"] = {"top_n": top_n, "priority": priority}
    limits = req.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - set(LIMITS):
        raise InputError("limits 包含不支持的字段")
    merged = default_limits()
    for key, value in limits.items():
        ceiling = LIMITS[key][1]
        if type(value) is not int or not 1 <= value <= ceiling:
            raise InputError(f"limits.{key} 必须是 1 至 {ceiling} 的整数")
        merged[key] = value
    req["limits"] = merged
    return req


def fingerprint_v2(request: Dict[str, Any]) -> str:
    """将完整 v2 请求绑定到一次方案。"""
    raw = json.dumps(validate_request_v2(request), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def amend_request_v2(request: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """仅改变显式指定字段；people 内 null 删除一个约束。"""
    result = validate_request_v2(request)
    allowed = {"context", "fulfillment", "budget_cents", "people", "ranking", "limits"}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise InputError("修改只支持 " + "、".join(sorted(allowed)))
    for key in ("context", "ranking", "limits"):
        if key in patch:
            if not isinstance(patch[key], dict):
                raise InputError(f"{key}修改必须为对象")
            result.setdefault(key, {}).update(patch[key])
    if "fulfillment" in patch:
        if not isinstance(patch["fulfillment"], dict):
            raise InputError("fulfillment修改必须为对象")
        # 整体替换，避免遗留旧取餐方式的 be_code/address_index
        result["fulfillment"] = patch["fulfillment"]
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
    return validate_request_v2(result)
