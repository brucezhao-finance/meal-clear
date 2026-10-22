"""团餐纯函数：人均预算拆分与预约时间解析（无网络调用）。"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict
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
