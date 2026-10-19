"""将官方菜单选项归一化，并对有界候选进行只读核价。"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .core import InputError, compose, integer, validate_request
from .mcp import McpClient, McpError


def timestamp() -> str:
    """返回带时区的取数时间。"""
    return datetime.now(timezone.utc).isoformat()


def classify_role(name: str, round_name: str = "") -> str:
    """仅用于区分餐品角色，不据名称推断成分安全。"""
    if "饮料" in round_name or "饮品" in round_name:
        return "drink"
    if any(
        word in name
        for word in (
            "堡",
            "鸡肉卷",
            "麦满分",
            "帕尼尼",
            "巨无霸",
            "麦香鱼",
            "麦香鸡",
            "吉士",
            "板烧",
            "不素之霸",
        )
    ):
        return "main"
    if any(
        word in name
        for word in ("可乐", "咖啡", "无糖", "雪碧", "牛奶", "红茶", "果汁")
    ):
        return "drink"
    return "side"


def default_modification(choice: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """只传服务声明的默认选项；未推导去冰或去配料。"""
    values = []
    for group in (choice.get("modification") or {}).get("items", []):
        selected = [
            x for x in group.get("values", []) if x.get("selectedQuantity", 0) > 0
        ]
        if len(selected) < group.get("minValues", 0):
            raise InputError("特制必选项资料不完整")
        for value in selected:
            if not value.get("selectedKey") or not value.get("code"):
                raise InputError("特制默认编码不完整")
            values.append(
                {
                    "code": value["code"],
                    "key": value["selectedKey"],
                    "quantity": value["selectedQuantity"],
                }
            )
    return {"values": values} if values else None


def facts_for(items: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    """显式标签可提供属性证据；没有辣字绝不自动推为不辣。"""
    facts: Dict[str, Any] = {}
    drinks = [x for x in items if x["role"] == "drink"]
    food = [x for x in items if x["role"] != "drink"]
    if drinks and all(
        any(word in x["name"] for word in ("无糖", "不含糖")) for x in drinks
    ):
        facts["sugar_free_drink"] = {
            "value": True,
            "source": source + "／饮品官方名称明确标注无糖",
        }
    elif any("含糖" in x["name"] for x in drinks):
        facts["sugar_free_drink"] = {
            "value": False,
            "source": source + "／饮品明确标注含糖",
        }
    if any(
        "辣" in x["name"] and not any(word in x["name"] for word in ("不辣", "非辣"))
        for x in food
    ):
        facts["non_spicy"] = {"value": False, "source": source + "／餐品明确标注辣味"}
    elif food and all("不辣" in x["name"] or "非辣" in x["name"] for x in food):
        facts["non_spicy"] = {
            "value": True,
            "source": source + "／全部食物明确标注非辣",
        }
    mains = [x for x in items if x["role"] == "main"]
    if mains and all(
        "牛" in x["name"] and not any(w in x["name"] for w in ("鸡", "猪", "鱼"))
        for x in mains
    ):
        facts["main_meat"] = {"value": "beef", "source": source + "／主食名称明确含牛"}
    elif mains and all(
        "鸡" in x["name"] and not any(w in x["name"] for w in ("牛", "猪", "鱼"))
        for x in mains
    ):
        facts["main_meat"] = {
            "value": "chicken",
            "source": source + "／主食名称明确含鸡",
        }
    return facts


def detail_offers(
    code: str,
    detail: Dict[str, Any],
    source: str,
    coupon: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """构造默认套餐及菜单明确支持的无糖替换，拒绝不完整轮次。"""
    rounds = detail.get("rounds") or []
    if not rounds:
        return []  # 首版不把单个薯条/饮料冒充完整一餐。
    variants = []
    for swap_sugar_free in (False, True):
        items = []
        wire_rounds = []
        try:
            for rnd in rounds:
                # 有些轮次quantity为0但minQuantity为1，按必选要求处理。
                count = max(rnd.get("minQuantity", 0), rnd.get("quantity", 0))
                if count == 0:
                    continue
                if count != 1 or rnd.get("maxQuantity", 1) < 1:
                    raise InputError("当前仅支持每轮选1件的完整单人套餐")
                choices = rnd.get("choices", [])
                selected = [x for x in choices if x.get("isDefault") == 1]
                if swap_sugar_free and (
                    "饮料" in rnd.get("name", "") or "饮品" in rnd.get("name", "")
                ):
                    sugar_free = [x for x in choices if "无糖" in x.get("name", "")]
                    if sugar_free:
                        selected = sugar_free[:1]
                if len(selected) != 1:
                    raise InputError("套餐默认选项不唯一或缺失")
                choice = selected[0]
                if not choice.get("code") or not choice.get("name"):
                    raise InputError("套餐子项缺少编码")
                item = {
                    "code": choice["code"],
                    "name": choice["name"],
                    "quantity": 1,
                    "role": classify_role(choice["name"], rnd.get("name", "")),
                }
                items.append(item)
                wire_choice: Dict[str, Any] = {"code": choice["code"], "quantity": 1}
                modification = default_modification(choice)
                if modification:
                    wire_choice["modification"] = modification
                wire_rounds.append(
                    {"round": str(rnd["id"]), "comboItemList": [wire_choice]}
                )
            if not any(x["role"] == "main" for x in items) or not any(
                x["role"] == "drink" for x in items
            ):
                continue
            wire_item: Dict[str, Any] = {
                "productCode": code,
                "quantity": 1,
                "roundList": wire_rounds,
            }
            coupon_key = None
            if coupon:
                if not coupon.get("couponId") or not coupon.get("couponCode"):
                    continue
                wire_item.update(
                    couponId=coupon["couponId"], couponCode=coupon["couponCode"]
                )
                coupon_key = hashlib.sha256(
                    str(coupon["couponId"]).encode()
                ).hexdigest()[:12]
            short = hashlib.sha256(str(items).encode()).hexdigest()[:8]
            oid = f"{code}-{short}" + ("-coupon" if coupon else "")
            if any(o["id"] == oid for o in variants):
                continue
            variants.append(
                {
                    "id": oid,
                    "name": detail.get("name", code)
                    + ("（无糖饮料选项）" if swap_sugar_free else "（默认选项）"),
                    "items": items,
                    "facts": facts_for(items, source),
                    "price_cents": None,
                    "card_fee_cents": 0,
                    "wire_items": [wire_item],
                    "coupon_key": coupon_key,
                }
            )
        except (InputError, KeyError, TypeError):
            continue
    return variants


def check_pricing(payload: Dict[str, Any], wire_items: List[Dict[str, Any]]) -> int:
    """核对官方总价、费用、商品及数量；缺项不能用0替代。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InputError("算价data缺失")
    fields = ["price", "productPrice"]
    optional = ["deliveryPrice", "packingPrice", "tablewarePrice"]
    for key in fields:
        integer(data.get(key), f"官方{key}")
    known = [key for key in optional if data.get(key) is not None]
    for key in known:
        integer(data[key], f"官方{key}")
    known_sum = data["productPrice"] + sum(data[k] for k in known)
    if (len(known) == len(optional) and data["price"] != known_sum) or known_sum > data[
        "price"
    ]:
        raise InputError("官方总价与费用分项不一致，停止推荐")
    expected: Dict[str, int] = {}
    for item in wire_items:
        expected[item["productCode"]] = (
            expected.get(item["productCode"], 0) + item["quantity"]
        )
    actual: Dict[str, int] = {}
    for item in data.get("productList", []):
        quantity = integer(item.get("quantity"), "报价商品数量", 1)
        code = item.get("productCode")
        actual[code] = actual.get(code, 0) + quantity
    if actual != expected:
        raise InputError("官方报价商品或数量与候选不一致")
    return data["price"]


class LivePlanner:
    """每次只读获取有限套餐、券与至多3次成功/失败报价，不创建订单。"""

    def __init__(self, client: McpClient) -> None:
        """注入只读客户端，便于离线回归协议行为。"""
        self.client = client

    def catalog(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """抓取门店当前菜单；不把未经解释的宣传价当作实付。"""
        req = validate_request(request)
        if req["context"]["meal_time"] != "now":
            raise InputError("实时首版只支持 meal_time=now；不伪造预约参数")
        store = req["context"]["store_code"]
        if not store.isdigit():
            raise InputError("实时门店code必须来自门店查询且全为数字")
        schemas = {t["name"]: t for t in self.client.list_tools()["tools"]}
        required = {
            "query-meals",
            "query-meal-detail",
            "query-store-coupons",
            "calculate-price",
        }
        if not required.issubset(schemas):
            raise McpError("当前服务缺少首版所需的只读工具")
        # 拒绝已改变必填字段的Schema，而非带着旧假设继续调用。
        for tool in required:
            allowed = {
                "storeCode",
                "orderType",
                "beType",
                "code",
                "items",
                "needTableware",
            }
            if set(schemas[tool].get("inputSchema", {}).get("required", [])) - allowed:
                raise McpError("当前Schema必填字段已变化，需要更新适配")
        context = {"storeCode": store, "orderType": 1, "beType": 1}
        menu = self.client.call("query-meals", context).get("data", {})
        meals = menu.get("meals", {})
        if not isinstance(meals, dict):
            raise McpError("当前菜单结构不匹配")
        coupons = self.client.call("query-store-coupons", context).get("data", [])
        if not isinstance(coupons, list):
            raise McpError("门店券结构不匹配")
        wanted = {
            v
            for p in req["people"]
            for v in list(p["hard"].values()) + list(p["prefer"].values())
            if isinstance(v, str)
        }

        def rank(entry: Any) -> Any:
            """菜单价格仅用于搜索启发，不转换成实付金额。"""
            code, meal = entry
            name = meal.get("name", "")
            matches = int("beef" in wanted and "牛" in name) + int(
                "chicken" in wanted and "鸡" in name
            )
            try:
                price_hint = Decimal(str(meal.get("currentPrice", "Infinity")))
                if not price_hint.is_finite() or price_hint < 0:
                    price_hint = Decimal("Infinity")
            except InvalidOperation:
                price_hint = Decimal("Infinity")
            return (-matches, price_hint, code)

        entries = [
            (str(k), v)
            for k, v in meals.items()
            if isinstance(v, dict)
            and not v.get("canWithOrder")
            and not v.get("withOrder")
            and not any(
                x in v.get("name", "") for x in ("双人", "多人", "会员", "月卡")
            )
            and classify_role(v.get("name", "")) == "main"
            and any(x in v.get("name", "") for x in ("件套", "套餐", "单人餐", "组合"))
        ]
        entries.sort(key=rank)
        to_fetch = [(code, None) for code, _ in entries[:6]]
        for coupon in coupons[:2]:
            for product in coupon.get("products", [])[:1]:
                if product.get("productCode"):
                    to_fetch.append((str(product["productCode"]), coupon))
        offers = []
        skipped = 0
        for code, coupon in to_fetch:
            try:
                detail = self.client.call(
                    "query-meal-detail", {**context, "code": code}
                ).get("data", {})
                offers.extend(
                    detail_offers(code, detail, "官方query-meal-detail", coupon)
                )
            except McpError:
                skipped += 1
        unique = {o["id"]: o for o in offers}
        return {
            "mode": "live",
            "as_of": timestamp(),
            "context": req["context"],
            "offers": list(unique.values()),
            "coverage": {
                "menu_entries": len(meals),
                "detail_queries": len(to_fetch),
                "skipped_errors": skipped,
                "coupon_count": len(coupons),
                "note": "有界单人套餐搜索，未穷尽全菜单；辣度等缺失仍未知",
            },
        }

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """重新取数并核价，不复用任何上一轮私人报价。"""
        req = validate_request(request)
        catalog = self.catalog(req)
        preliminary = compose(req, catalog)
        by_id = {o["id"]: o for o in catalog["offers"]}
        quotes = {}
        errors = []
        # 不因重复的同一购物篮顺序耗费两次核价机会。
        seen_baskets = set()
        attempts = 0
        for plan in preliminary["plans"]:
            basket = tuple(sorted(a["offer_id"] for a in plan["assignments"]))
            if basket in seen_baskets:
                continue
            if attempts >= 3:
                break
            seen_baskets.add(basket)
            attempts += 1
            selected = [by_id[a["offer_id"]] for a in plan["assignments"]]
            wire = [copy.deepcopy(i) for o in selected for i in o["wire_items"]]
            try:
                payload = self.client.call(
                    "calculate-price",
                    {
                        "storeCode": req["context"]["store_code"],
                        "orderType": 1,
                        "beType": 1,
                        "items": wire,
                        "needTableware": False,
                    },
                )
                total = check_pricing(payload, wire)
                quote = {
                    "signature": plan["signature"],
                    "context": req["context"],
                    "total_cents": total,
                    "verified": True,
                    "source": "官方calculate-price",
                    "quoted_at": timestamp(),
                }
                quote["fee_breakdown"] = {
                    k: payload["data"].get(k)
                    for k in (
                        "productPrice",
                        "deliveryPrice",
                        "packingPrice",
                        "tablewarePrice",
                    )
                }
                quotes[plan["signature"]] = quote
            except (McpError, InputError) as exc:
                errors.append(str(exc))
        result = compose(req, catalog, quotes)
        result["coverage"] = catalog["coverage"]
        result["quote_attempts"] = attempts
        result["verified_quotes"] = len(quotes)
        result["quote_errors"] = errors
        result["read_only_audit"] = list(self.client.audit)
        # 仅公开已验证/推荐或少量候选，完整计划亦不包含券码。
        result["plans"] = result["plans"][:6]
        return result
