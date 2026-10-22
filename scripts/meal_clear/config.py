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
    needs_gm_service: bool = False  # 团餐核价/下单必须带助餐服务编码
    supports_reservation: bool = False  # 可带预约时间 reservationDate


FULFILLMENTS: Dict[str, Fulfillment] = {
    "pickup": Fulfillment("pickup", "到店自取", 1, 1, "none", False, True),
    "drive_thru": Fulfillment("drive_thru", "得来速", 5, 1, "required", False, True),
    "delivery": Fulfillment("delivery", "外送", 2, 2, "required", True, True),
    "group": Fulfillment("group", "团餐", 6, 2, "required", True, True, True, True),
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

# --- P1 受控写通道、账本与退出码（唯一定义处） ---
TOOL_CREATE_ORDER = "create-order"
TOOL_AUTO_BIND_COUPONS = "auto-bind-coupons"
CONTROLLED_WRITE_TOOLS = frozenset({TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER})
SUMMARY_TTL_SECONDS = 600
SUMMARY_TTL_RANGE: Tuple[int, int] = (60, 1800)
DUPLICATE_BASKET_WINDOW_SECONDS = 24 * 3600
PENDING_STALE_SECONDS = 600  # 仅影响提示措辞
LEDGER_DIRNAME = "orders"  # <repo>/local-state/orders
LEDGER_MAX_BYTES = 1_000_000
LEDGER_SCHEMA = "meal-clear/ledger/1"
DIGEST_VERSION = 1
# 账本步骤状态与写步骤（唯一定义处；ledger.py 从这里导入）
LEDGER_STEP_STATES: Dict[str, Tuple[str, ...]] = {
    "claim": ("none", "not_required", "pending", "succeeded", "failed", "unknown"),
    "requote": ("none", "consistent", "changed", "error"),
    "order": ("none", "pending", "created", "failed", "unknown"),
}
LEDGER_WRITE_STEPS: Tuple[str, ...] = ("claim", "order")
ORDER_STATUS_LABELS: Dict[str, str] = {
    "1": "待支付",
    "2": "配餐中-已支付",
    "4": "配送中",
    "6": "已完成",
    "7": "已取消",
    "8": "已评价",
    "10": "配餐中-餐厅确认配餐中",
}
(
    EXIT_OK,
    EXIT_ERROR,
    EXIT_NO_PLAN,
    EXIT_RECONFIRM,
    EXIT_REFUSED,
    EXIT_DUPLICATE,
    EXIT_UNKNOWN,
    EXIT_LIMIT,
    EXIT_LOCKED,
) = range(9)


def default_limits() -> Dict[str, int]:
    """返回各上限参数的默认值副本。"""
    return {key: value[0] for key, value in LIMITS.items()}


def get_fulfillment(key: Optional[str]) -> Fulfillment:
    """按键取取餐方式，未知键返回 KeyError。"""
    return FULFILLMENTS[key or DEFAULT_FULFILLMENT]


# --- P2 真实写第四道门（唯一定义处） ---
OFFICIAL_ENDPOINT = "https://mcp.mcd.cn"
REAL_WRITE_ENV = "MEAL_CLEAR_ENABLE_REAL_WRITE"  # 仅当值恰为 "1" 时开启
REAL_WRITE_CONFIG_NAME = "real-write.json"
WRITE_LOCK_NAME = "WRITE_LOCK"
WRITE_TOKEN_REL: Tuple[str, ...] = ("credentials", "mcd_mcp_token")
REAL_CONFIG_MAX_BYTES = 64 * 1024
# 配置键 -> (默认值, 硬上限)；越界拒绝而非截断
REAL_LIMITS: Dict[str, Tuple[int, int]] = {
    "max_order_cents": (10000, 50000),
    "max_orders_24h": (3, 10),
    "max_claims_24h": (1, 3),
}
REAL_CONFIG_KEYS: Tuple[str, ...] = ("enabled",) + tuple(REAL_LIMITS)
REAL_WINDOW_SECONDS = 24 * 3600
UNKNOWN_LOCK_SECONDS = 24 * 3600
CONFIRM_WORDS: Dict[str, str] = {"claim": "确认领券", "order": "确认下单"}
RESOLVE_CONFIRM_WORD = "确认标注"  # order-resolve：<digest 末 6 位> 确认标注
UNLOCK_PHRASE = "确认解除写锁"  # write-lock --off
PHRASE_TAIL_LEN = 6
CHANNEL_LOOPBACK = "loopback"
CHANNEL_OFFICIAL = "official"
CHANNELS: Tuple[str, ...] = (CHANNEL_LOOPBACK, CHANNEL_OFFICIAL)
# 受控写工具 -> 账本步骤；以及各步骤计入上限的状态（failed=明确未执行，不计）
TOOL_STEP: Dict[str, str] = {TOOL_AUTO_BIND_COUPONS: "claim", TOOL_CREATE_ORDER: "order"}
REAL_COUNTED_STATES: Dict[str, Tuple[str, ...]] = {
    "claim": ("pending", "succeeded", "unknown"),
    "order": ("pending", "created", "unknown"),
}
REAL_COUNT_LIMIT_KEY: Dict[str, str] = {"claim": "max_claims_24h", "order": "max_orders_24h"}
# 人工标注：(步骤, 声明) -> 账本终态
RESOLVE_CHOICES: Dict[Tuple[str, str], str] = {
    ("claim", "claimed"): "succeeded",
    ("claim", "not-claimed"): "failed",
    ("order", "created"): "created",
    ("order", "not-created"): "failed",
}
GROUP_REFUSAL_REASON = "group_closed"  # 团餐真实写拒绝原因（退出码 4）
REAL_REFUSAL_REASONS: Tuple[str, ...] = (
    "switch_off",
    "config_invalid",
    "locked",
    "not_tty",
    "phrase_mismatch",
    "amount_cap",
    "orders_cap",
    "claims_cap",
    "unknown_lock",
    "bad_endpoint",
    "token_source",
    "bad_tool",
    GROUP_REFUSAL_REASON,
)

# --- P3 团餐（唯一定义处） ---
TOOL_QUERY_PROMOTIONS = "query-promotions"
TOOL_QUERY_MEAL_ASSISTANCE = "query-meal-assistance"
GROUP_READ_ONLY_TOOLS: Tuple[str, ...] = (TOOL_QUERY_PROMOTIONS, TOOL_QUERY_MEAL_ASSISTANCE)
RESERVATION_FORMAT = "%Y-%m-%d %H:%M"  # 官方 reservationDate 格式，不含秒
GROUP_PROMO_RULE_CAP = 20  # 最多展示的促销规则条数
GROUP_BAND_ENABLED = True  # 搭配启发开关（仅影响送核价的候选顺序与理由）
# 官方 query-meals 描述中的团餐搭配建议；区间 [下限分, 上限分)（按人均预算划分属推测）
# (下限分, 上限分|None, 标签, 建议角色组合)
GROUP_BAND_TABLE: Tuple[Tuple[int, Optional[int], str, Tuple[Tuple[str, ...], ...]], ...] = (
    (0, 2000, "20元以下", (("side",),)),
    (2000, 3000, "20-30元", (("main", "side"), ("main", "drink"))),
    (3000, 4000, "30-40元", (("main", "side", "drink"),)),
    (4000, 5000, "40-50元", (("main", "side", "side", "drink"),)),
    (5000, None, "50元以上", (("main", "side", "side", "drink"),)),
)
MSG_GROUP_REAL_WRITE_CLOSED = (
    "团餐真实下单尚未开放，待你在真实环境验证只读流程后再决定。本次未发出任何请求。"
)
