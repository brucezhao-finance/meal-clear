"""规模测试共用夹具：合成目录与按组结构生成的 N 人请求（全部为虚构数据，固定随机种子）。"""
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

CTX = {"store_code": "T1", "meal_time": "t", "fulfillment": "pickup", "be_code": None}
MEATS = ("chicken", "beef", "pork", "fish")


def make_offer(
    oid: str,
    price: int,
    meat: str = "chicken",
    non_spicy: Optional[bool] = True,
    sugar_free: Optional[bool] = True,
    card: int = 0,
    coupon: Optional[str] = None,
) -> Dict[str, Any]:
    """构造一个合成 offer（含主食与饮品，facts 均带来源）。"""
    facts: Dict[str, Any] = {"main_meat": {"value": meat, "source": "合成"}}
    if non_spicy is not None:
        facts["non_spicy"] = {"value": non_spicy, "source": "合成"}
    if sugar_free is not None:
        facts["sugar_free_drink"] = {"value": sugar_free, "source": "合成"}
    offer: Dict[str, Any] = {
        "id": oid,
        "name": "餐" + oid,
        "price_cents": price,
        "card_fee_cents": card,
        "facts": facts,
        "items": [
            {"code": oid + "-m", "name": "主" + oid, "quantity": 1, "role": "main"},
            {"code": oid + "-d", "name": "饮" + oid, "quantity": 1, "role": "drink"},
        ],
    }
    if coupon:
        offer["coupon_key"] = coupon
        offer["coupon_title"] = "券" + coupon
    return offer


def make_catalog(offers: List[Dict[str, Any]], mode: str = "simulation") -> Dict[str, Any]:
    """合成菜单（费用 0，便于对账）。"""
    return {
        "mode": mode,
        "as_of": "合成时间",
        "context": dict(CTX),
        "order_fee_cents": 0,
        "price_source": "合成价目",
        "offers": offers,
    }


def synth_offers(n: int = 40, seed: int = 11, coupons: int = 3, cards: int = 2) -> List[Dict[str, Any]]:
    """n 个固定种子的合成 offer：多肉类、多价位，含带券款、新购卡款和属性未知款。"""
    rng = random.Random(seed)
    offers = []
    for i in range(n):
        offers.append(
            make_offer(
                f"s{i:03d}",
                rng.randrange(800, 3200, 50),
                rng.choice(MEATS),
                non_spicy=rng.choice((True, True, True, False, None)),
                sugar_free=rng.choice((True, True, False)),
                card=900 if i < cards else 0,
                coupon=f"c{i % 2}" if cards <= i < cards + coupons else None,
            )
        )
    return offers


def synth_catalog(n: int = 40, seed: int = 11, coupons: int = 3, mode: str = "simulation") -> Dict[str, Any]:
    """≥40 个 offer 的合成菜单。"""
    return make_catalog(synth_offers(n, seed, coupons), mode)


Group = Tuple[int, Dict[str, Any], Dict[str, Any]]  # (人数, hard, prefer)


def group_request(
    groups: Sequence[Group],
    budget: int = 10**7,
    priority: str = "price",
    shuffle_seed: Optional[int] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """按组结构生成 v2 请求；shuffle_seed 非空时打乱人员顺序（id 不变）。"""
    people = []
    for size, hard, prefer in groups:
        for _ in range(size):
            people.append({"id": f"P{len(people) + 1:03d}", "hard": dict(hard), "prefer": dict(prefer)})
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(people)
    req = {
        "schema_version": 2,
        "context": {"store_code": CTX["store_code"], "meal_time": CTX["meal_time"]},
        "budget_cents": budget,
        "people": people,
        "ranking": {"top_n": 3, "priority": priority},
    }
    req.update(extra)
    return req


def distinct_people_request(n: int, budget: int = 10**7, priority: str = "price", **extra: Any) -> Dict[str, Any]:
    """n 个互不相同的人：偏好键互不相同且不匹配任何 offer（n 个同质组、每组 1 人）。"""
    people = [
        {"id": f"P{i + 1:03d}", "hard": {"non_spicy": True}, "prefer": {f"x{i}": "v"}}
        for i in range(n)
    ]
    req = {
        "schema_version": 2,
        "context": {"store_code": CTX["store_code"], "meal_time": CTX["meal_time"]},
        "budget_cents": budget,
        "people": people,
        "ranking": {"top_n": 3, "priority": priority},
    }
    req.update(extra)
    return req


# 50 人 4 组混合（14/13/12/11）
FOUR_GROUPS: List[Group] = [
    (14, {"non_spicy": True, "sugar_free_drink": True}, {"main_meat": "chicken"}),
    (13, {"non_spicy": True}, {"main_meat": "beef"}),
    (12, {"non_spicy": True, "sugar_free_drink": True}, {"main_meat": "pork"}),
    (11, {"non_spicy": True}, {"main_meat": "fish"}),
]
