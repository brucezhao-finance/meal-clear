"""将官方菜单选项归一化，并对有界候选进行只读核价。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .config import COUPON_DETAIL_CAP, FULFILLMENTS, RUN_CALL_CAP
from .core import InputError, compose, integer, validate_request
from .fulfillment import context_key, wire_context
from .mcp import McpClient, McpError, RateLimitedError
from .privacy import (
    ResolvedAddress,
    mask_addresses,
    sanitize_claimable,
    sanitize_held,
    sanitize_store_coupons,
)
from .ranking import compose_v2, generate_candidates, select_for_quote
from .request_v2 import validate_request_v2


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
    modification = choice.get("modification")
    if modification is None:
        modification = {}
    if not isinstance(modification, dict) or not isinstance(
        modification.get("items", []), list
    ):
        raise InputError("特制结构异常")
    for group in modification.get("items", []):
        if (
            not isinstance(group, dict)
            or not isinstance(group.get("values", []), list)
            or any(not isinstance(value, dict) for value in group.get("values", []))
        ):
            raise InputError("特制选项结构异常")
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
    if not isinstance(detail, dict):
        return []
    rounds = detail.get("rounds") or []
    if not isinstance(rounds, list):
        return []
    if not rounds:
        return []  # 首版不把单个薯条/饮料冒充完整一餐。
    variants = []
    for swap_sugar_free in (False, True):
        items = []
        wire_rounds = []
        try:
            for rnd in rounds:
                if not isinstance(rnd, dict):
                    raise InputError("套餐轮次必须为对象")
                # 有些轮次quantity为0但minQuantity为1，按必选要求处理。
                count = max(rnd.get("minQuantity", 0), rnd.get("quantity", 0))
                if count == 0:
                    continue
                if count != 1 or rnd.get("maxQuantity", 1) < 1:
                    raise InputError("当前仅支持每轮选1件的完整单人套餐")
                choices = rnd.get("choices", [])
                if not isinstance(choices, list) or any(
                    not isinstance(choice, dict)
                    or not isinstance(choice.get("name"), str)
                    for choice in choices
                ):
                    raise InputError("套餐选项结构异常")
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
                    **(
                        {"coupon_title": coupon["title"]}
                        if coupon and isinstance(coupon.get("title"), str)
                        else {}
                    ),
                }
            )
        except (InputError, KeyError, TypeError):
            continue
    return variants


def merge_wire_items(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把多人相同的商品行合并为“商品行 × 数量”（核价载荷）。

    相同 (productCode, roundList, 券) 的行累加 quantity；券行数量恒为 1，且不与普通行合并。
    """
    merged: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for offer in offers:
        for item in offer["wire_items"]:
            row = copy.deepcopy(item)
            if row.get("couponId") or row.get("couponCode"):
                row["quantity"] = 1
                merged.append(row)
                continue
            key = json.dumps(
                {"p": row["productCode"], "r": row.get("roundList")},
                sort_keys=True,
                ensure_ascii=False,
            )
            if key in index:
                index[key]["quantity"] += row["quantity"]
            else:
                index[key] = row
                merged.append(row)
    return merged


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
    products = data.get("productList", [])
    if not isinstance(products, list) or any(
        not isinstance(item, dict) for item in products
    ):
        raise InputError("官方报价商品清单结构不匹配")
    for item in products:
        quantity = integer(item.get("quantity"), "报价商品数量", 1)
        code = item.get("productCode")
        actual[code] = actual.get(code, 0) + quantity
    if actual != expected:
        raise InputError("官方报价商品或数量与候选不一致")
    return data["price"]


MENU_TOOLS = (
    "query-meals",
    "query-meal-detail",
    "query-store-coupons",
    "calculate-price",
)
MENU_ALLOWED = frozenset(
    {"storeCode", "orderType", "beType", "code", "items", "needTableware"}
)
# 个别工具的必填字段允许集不同于菜单类工具
TOOL_ALLOWED = {
    "query-nearby-stores": frozenset({"beType", "searchType"}),
    "delivery-query-stores": frozenset({"beType", "addressId"}),
}


class CallCapError(McpError):
    """达到单次运行调用上限。"""


def check_schemas(
    schemas: Dict[str, Any], tools: Any, default_allowed: Any
) -> None:
    """工具齐全且必填字段未变化；否则拒绝带着旧假设继续调用。"""
    if not set(tools).issubset(schemas):
        raise McpError("当前服务缺少首版所需的只读工具")
    for tool in tools:
        allowed = TOOL_ALLOWED.get(tool, default_allowed)
        schema = schemas[tool].get("inputSchema", {})
        if (
            not isinstance(schema, dict)
            or not isinstance(schema.get("required", []), list)
            or any(not isinstance(field, str) for field in schema.get("required", []))
        ):
            raise McpError("当前Schema结构异常")
        if set(schema.get("required", [])) - set(allowed):
            raise McpError("当前Schema必填字段已变化，需要更新适配")


def fetch_menu_offers(
    client: Any,
    context: Dict[str, Any],
    req: Dict[str, Any],
    detail_cap: int,
    coupon_detail_cap: int,
    stop_on_rate_limit: bool = False,
) -> Dict[str, Any]:
    """读取菜单、门店券与有限详情并归一化为 offers（v1/v2 共用）。"""
    menu = client.call("query-meals", context).get("data", {})
    if not isinstance(menu, dict):
        raise McpError("当前菜单结构不匹配")
    meals = menu.get("meals", {})
    if not isinstance(meals, dict):
        raise McpError("当前菜单结构不匹配")
    coupons = client.call("query-store-coupons", context).get("data", [])
    if not isinstance(coupons, list) or any(
        not isinstance(coupon, dict)
        or not isinstance(coupon.get("products", []), list)
        or any(not isinstance(product, dict) for product in coupon.get("products", []))
        for coupon in coupons
    ):
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
        and not any(x in v.get("name", "") for x in ("双人", "多人", "会员", "月卡"))
        and classify_role(v.get("name", "")) == "main"
        and any(x in v.get("name", "") for x in ("件套", "套餐", "单人餐", "组合"))
    ]
    entries.sort(key=rank)
    to_fetch = [(code, None) for code, _ in entries[:detail_cap]]
    for coupon in coupons[:coupon_detail_cap]:
        for product in coupon.get("products", [])[:1]:
            if product.get("productCode"):
                to_fetch.append((str(product["productCode"]), coupon))
    offers = []
    skipped = 0
    rate_limited = False
    for code, coupon in to_fetch:
        try:
            detail = client.call("query-meal-detail", {**context, "code": code}).get(
                "data", {}
            )
            offers.extend(detail_offers(code, detail, "官方query-meal-detail", coupon))
        except CallCapError:
            raise
        except RateLimitedError:
            skipped += 1
            if stop_on_rate_limit:
                rate_limited = True
                break
        except McpError:
            skipped += 1
    unique = {o["id"]: o for o in offers}
    return {
        "offers": list(unique.values()),
        "coupons": coupons,
        "rate_limited": rate_limited,
        "coverage": {
            "menu_entries": len(meals),
            "detail_queries": len(to_fetch),
            "skipped_errors": skipped,
            "coupon_count": len(coupons),
            "note": "有界单人套餐搜索，未穷尽全菜单；辣度等缺失仍未知",
        },
    }


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
        check_schemas(schemas, MENU_TOOLS, MENU_ALLOWED)
        context = {"storeCode": store, "orderType": 1, "beType": 1}
        fetched = fetch_menu_offers(self.client, context, req, 6, 2)
        return {
            "mode": "live",
            "as_of": timestamp(),
            "context": req["context"],
            "offers": fetched["offers"],
            "coverage": fetched["coverage"],
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


class BudgetedClient:
    """包装只读客户端并限制单次运行的调用总数。"""

    def __init__(self, client: Any, max_calls: int = RUN_CALL_CAP) -> None:
        """max_calls 为本次运行允许的最大工具/协议调用数。"""
        self._client = client
        self.max_calls = max_calls
        self.calls = 0

    def _spend(self) -> None:
        """占用一次调用名额，超限则在发请求前拒绝。"""
        if self.calls >= self.max_calls:
            raise CallCapError("已达到本次运行调用上限")
        self.calls += 1

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """计数后转发只读调用。"""
        self._spend()
        return self._client.call(name, arguments)

    def list_tools(self) -> Dict[str, Any]:
        """计数后转发工具清单读取。"""
        self._spend()
        return self._client.list_tools()

    @property
    def audit(self) -> List[Dict[str, Any]]:
        """底层客户端的只读调用审计（仅工具名与成败）。"""
        return list(getattr(self._client, "audit", []))


class MultiLivePlanner:
    """v2 实时多方案：取餐方式映射、外送选址校验、券预览与有界核价。"""

    def __init__(self, client: Any, max_calls: int = RUN_CALL_CAP) -> None:
        """注入只读客户端；地址原始回包不保留。"""
        self.client = BudgetedClient(client, max_calls)
        self._schemas: Optional[Dict[str, Any]] = None
        self._addresses: Optional[List[ResolvedAddress]] = None

    def _tools(self) -> Dict[str, Any]:
        """读取并缓存当前工具 Schema。"""
        if self._schemas is None:
            self._schemas = {t["name"]: t for t in self.client.list_tools()["tools"]}
        return self._schemas

    def list_addresses(self) -> List[ResolvedAddress]:
        """读取配送地址并立即脱敏，只保留序号、标签和内存 addressId。"""
        if self._addresses is None:
            check_schemas(self._tools(), ("delivery-query-addresses",), MENU_ALLOWED)
            payload = self.client.call("delivery-query-addresses", {})
            self._addresses = mask_addresses(payload)
            del payload
        return self._addresses

    def _address(self, address_index: int) -> ResolvedAddress:
        """按从1开始的序号取地址。"""
        addresses = self.list_addresses()
        if not addresses:
            raise InputError("账户没有可用配送地址")
        if not 1 <= address_index <= len(addresses):
            raise InputError(f"address_index 超出地址列表范围（共{len(addresses)}个）")
        return addresses[address_index - 1]

    def list_delivery_stores(self, address_index: int) -> List[Dict[str, Any]]:
        """某地址可配送的门店（仅门店名/编码）。"""
        address = self._address(address_index)
        check_schemas(self._tools(), ("delivery-query-stores",), MENU_ALLOWED)
        value = self.client.call(
            "delivery-query-stores", {"beType": 2, "addressId": address.address_id}
        )
        rows = value.get("data", [])
        if not isinstance(rows, list):
            raise McpError("可配送门店结构不匹配")
        return [
            {k: r.get(k) for k in ("storeCode", "storeName", "beCode", "businessStatus") if k in r}
            for r in rows
            if isinstance(r, dict)
        ]

    def resolve_context(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """生成公共入参；外送时校验所选门店在该地址可配送列表内。"""
        wire = wire_context(req)
        if not wire["storeCode"].isdigit():
            raise InputError("实时门店code必须来自门店查询且全为数字")
        spec = FULFILLMENTS[req["fulfillment"]["type"]]
        address = None
        if spec.needs_address:
            index = req["fulfillment"]["address_index"]
            stores = self.list_delivery_stores(index)
            address = self._address(index)
            if not any(
                str(s.get("storeCode")) == wire["storeCode"]
                and str(s.get("beCode")) == wire["beCode"]
                for s in stores
            ):
                raise InputError("所选门店与 be_code 不在该地址的可配送列表内，请重新选店")
        return {"wire": wire, "address": address}

    def coupon_preview(
        self, wire: Dict[str, Any], store_payload: Any = None
    ) -> Dict[str, Any]:
        """三类券预览；仅保留白名单字段，任一类查询失败只记说明。"""
        notes = [
            "领券属后续阶段（P1），且 auto-bind-coupons 只能一键领取全部；"
            "未领取的券不计入本次报价"
        ]

        def attempt(label: str, fn: Any) -> List[Dict[str, Any]]:
            """单类查询失败不影响其它两类。"""
            try:
                return fn()
            except McpError:
                notes.append(f"{label}查询失败，未展示")
                return []

        def held() -> List[Dict[str, Any]]:
            """我的券：最多2页，每页200条。"""
            rows: List[Dict[str, Any]] = []
            for page in (1, 2):
                payload = self.client.call(
                    "query-my-coupons", {"page": str(page), "pageSize": "200"}
                )
                batch = sanitize_held(payload)
                rows.extend(batch)
                if len(batch) < 200:
                    break
            return rows

        if store_payload is None:
            store_usable = attempt(
                "本店可用券",
                lambda: sanitize_store_coupons(
                    self.client.call("query-store-coupons", wire)
                ),
            )
        else:
            store_usable = sanitize_store_coupons(store_payload)
        return {
            "claimable": attempt(
                "可领取券",
                lambda: sanitize_claimable(self.client.call("available-coupons", {})),
            ),
            "held": attempt("已领取券", held),
            "store_usable": store_usable,
            "notes": notes,
        }

    def catalog_v2(self, req: Dict[str, Any], wire: Dict[str, Any]) -> Dict[str, Any]:
        """抓取当前取餐方式下的菜单；返回目录与内存中的门店券原始列表。"""
        schemas = self._tools()
        check_schemas(schemas, MENU_TOOLS, MENU_ALLOWED | {"beCode"})
        fetched = fetch_menu_offers(
            self.client,
            wire,
            req,
            req["limits"]["max_detail_queries"],
            COUPON_DETAIL_CAP,
            stop_on_rate_limit=True,
        )
        catalog = {
            "mode": "live",
            "as_of": timestamp(),
            "context": context_key(req),
            "offers": fetched["offers"],
            "coverage": fetched["coverage"],
        }
        return {
            "catalog": catalog,
            "coupons": fetched["coupons"],
            "rate_limited": fetched["rate_limited"],
        }

    def plan_v2(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """重新取数、有界核价并排名；不创建订单、不领券。"""
        req = validate_request_v2(request)
        if req["context"]["meal_time"] != "now":
            raise InputError("实时首版只支持 meal_time=now；不伪造预约参数")
        resolved = self.resolve_context(req)
        wire = resolved["wire"]
        fetched = self.catalog_v2(req, wire)
        catalog = fetched["catalog"]
        limits = req["limits"]
        priority = req["ranking"]["priority"]
        generated = generate_candidates(req, catalog)
        selected = select_for_quote(generated["whole"], limits["max_quotes"], priority)
        ctx = context_key(req)
        quotes: Dict[str, Any] = {}
        attempts = 0
        errors = 0
        stop_reason = ""
        if fetched["rate_limited"]:
            stop_reason = "读取菜单时触发限流（429），已停止后续请求"
        for cand in ([] if stop_reason else selected):
            wire_items = merge_wire_items(cand["offers"])
            try:
                attempts += 1
                payload = self.client.call(
                    "calculate-price",
                    {**wire, "items": wire_items, "needTableware": False},
                )
                total = check_pricing(payload, wire_items)
            except RateLimitedError:
                stop_reason = "核价触发限流（429），已停止核价"
                break
            except CallCapError:
                attempts -= 1
                stop_reason = "已达到本次运行调用上限，已停止核价"
                break
            except (McpError, InputError):
                errors += 1
                continue
            quotes[cand["signature"]] = {
                "signature": cand["signature"],
                "context": ctx,
                "total_cents": total,
                "verified": True,
                "source": "官方calculate-price",
                "quoted_at": timestamp(),
                "fee_breakdown": {
                    k: payload["data"].get(k)
                    for k in (
                        "productPrice",
                        "deliveryPrice",
                        "packingPrice",
                        "tablewarePrice",
                    )
                },
            }
        preview = self.coupon_preview(wire, {"data": fetched["coupons"]})
        result = compose_v2(req, catalog, quotes, self.client.calls)
        checked = result["ranking_meta"]["checked"]
        checked["quotes_attempted"] = attempts
        checked["run_calls"] = self.client.calls
        if stop_reason:
            checked["statement"] += (
                f"{stop_reason}；完成官方核价 {len(quotes)} 次，其余候选未核价、不进入排名。"
            )
        if errors:
            result["diagnostics"].append(f"{errors} 个候选核价失败，已排除，不当作 0 元")
        fulfillment = result["fulfillment"]
        if resolved["address"] is not None:
            fulfillment["address_label"] = resolved["address"].label
            fulfillment["address_ref"] = resolved["address"].ref
        result["coupon_preview"] = preview
        result["coverage"] = catalog["coverage"]
        return result
