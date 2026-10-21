"""v2 枚举与数值上限的唯一定义处（页面/逻辑不得散落硬编码）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class Fulfillment:
    """一种取餐方式及其官方参数映射。"""

    key: str
    label: str
    be_type: int
    order_type: int
    be_code: str  # "none" 不传 / "required" 必传
    needs_address: bool
    implemented: bool


FULFILLMENTS: Dict[str, Fulfillment] = {
    "pickup": Fulfillment("pickup", "到店自取", 1, 1, "none", False, True),
    "drive_thru": Fulfillment("drive_thru", "得来速", 5, 1, "required", False, True),
    "delivery": Fulfillment("delivery", "外送", 2, 2, "required", True, True),
    "group": Fulfillment("group", "团餐", 6, 2, "required", True, False),
}
DEFAULT_FULFILLMENT = "pickup"
PRIORITIES: Tuple[str, ...] = ("price", "preference")
DEFAULT_PRIORITY = "price"
DATA_MODES: Tuple[str, ...] = ("live", "simulation", "historical", "user_supplied")
MAX_PEOPLE = 50  # 单次用餐者人数上限的唯一定义处（校验文案由它生成）
LEGACY_BEAM_MAX_PEOPLE = 6  # ≤6 人沿用 P0 逐人 beam，输出由哈希基线锁定
COMPACT_MIN_PEOPLE = 13  # ≥13 人文本报告默认按同质组汇总
GROUP_MAX_DISTINCT_OFFERS = 2  # 同质组内最多几种不同套餐
GROUP_ALLOCATIONS_CAP = 48  # 每个同质组保留的分配方案数
SPACE_SCI_THRESHOLD = 10**12  # 搜索空间 ≥ 此值用科学计数法显示
MAX_TOP_N = 10
DEFAULT_TOP_N = 3

# (默认值, 上限)
LIMITS: Dict[str, Tuple[int, int]] = {
    "per_person_top_k": (8, 12),
    "max_whole_candidates": (200, 1000),
    "max_quotes": (12, 30),
    "max_detail_queries": (12, 20),
}
COUPON_DETAIL_CAP = 4
RUN_CALL_CAP = 150
RATE_LIMIT: Tuple[int, float] = (500, 60.0)


def default_limits() -> Dict[str, int]:
    """返回各上限参数的默认值副本。"""
    return {key: value[0] for key, value in LIMITS.items()}


def get_fulfillment(key: Optional[str]) -> Fulfillment:
    """按键取取餐方式，未知键返回 KeyError。"""
    return FULFILLMENTS[key or DEFAULT_FULFILLMENT]
