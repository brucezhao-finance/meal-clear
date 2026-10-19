"""Synthetic official-interface fixtures, without credentials or real account data."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from meal_clear.mcp import McpError


def request(people: int = 1, hard: bool = True) -> Dict[str, Any]:
    """Return a same-store pickup task with stable synthetic IDs."""
    return {
        "context": {"store_code": "12345", "channel": "pickup", "meal_time": "now"},
        "budget_cents": 10000,
        "people": [
            {
                "id": str(i),
                "hard": {"sugar_free_drink": True} if hard else {},
                "prefer": {},
            }
            for i in range(people)
        ],
    }


def detail(name: str = "模拟鸡肉堡套餐") -> Dict[str, Any]:
    """Return one complete two-round single meal with an explicit sugar-free variant."""
    return {
        "name": name,
        "rounds": [
            {
                "id": 1,
                "name": "主食",
                "quantity": 1,
                "minQuantity": 1,
                "maxQuantity": 1,
                "choices": [{"code": "MAIN", "name": "模拟非辣鸡肉堡", "isDefault": 1}],
            },
            {
                "id": 2,
                "name": "饮料",
                "quantity": 1,
                "minQuantity": 1,
                "maxQuantity": 1,
                "choices": [
                    {"code": "DRINK", "name": "模拟含糖可乐", "isDefault": 1},
                    {"code": "ZERO", "name": "模拟无糖可乐", "isDefault": 0},
                ],
            },
        ],
    }


class SyntheticClient:
    """Emulate structured read-only tools and preserve their actual invocation audit."""

    def __init__(self, count: int = 4) -> None:
        """Start with explicit mock schemas, menu entries and complete meal details."""
        self.schemas = {
            "tools": [
                {"name": name, "inputSchema": {"required": []}}
                for name in (
                    "query-meals",
                    "query-meal-detail",
                    "query-store-coupons",
                    "calculate-price",
                )
            ]
        }
        self.menu = {
            "data": {
                "meals": {
                    f"P{i}": {"name": f"模拟鸡肉堡套餐{i}", "currentPrice": i + 1}
                    for i in range(count)
                }
            }
        }
        self.coupons = {"data": []}
        self.details = {f"P{i}": detail(f"模拟鸡肉堡套餐{i}") for i in range(count)}
        self.audit: List[Dict[str, Any]] = []
        self.fail_details = set()
        self.fail_quotes = 0
        self.quote_count = 0
        self.price = 1000
        self.bad_price = False

    def initialize(self) -> Dict[str, Any]:
        """Expose no network or user account operations."""
        return {"name": "synthetic"}

    def list_tools(self) -> Dict[str, Any]:
        """Return an isolated schema copy so tests cannot contaminate other cases."""
        return copy.deepcopy(self.schemas)

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Respond to the real planner's tool calls, refusing any unrecognized tool."""
        self.audit.append({"tool": name, "success": True, "elapsed_ms": 0})
        if name == "query-nearby-stores":
            return {
                "data": [
                    {
                        "storeCode": "12345",
                        "storeName": "模拟测试店",
                        "address": "PUBLIC-SYNTHETIC",
                        "businessStatus": 1,
                        "private": "DO-NOT-EXPOSE",
                    }
                ]
            }
        if name == "query-meals":
            return copy.deepcopy(self.menu)
        if name == "query-store-coupons":
            return copy.deepcopy(self.coupons)
        if name == "query-meal-detail":
            code = arguments["code"]
            if code in self.fail_details:
                self.audit[-1]["success"] = False
                raise McpError("模拟详情失败")
            return {"data": copy.deepcopy(self.details.get(code, {}))}
        if name == "calculate-price":
            self.quote_count += 1
            if self.quote_count <= self.fail_quotes:
                self.audit[-1]["success"] = False
                raise McpError("模拟核价失败")
            items = arguments["items"]
            price = self.price * sum(x["quantity"] for x in items)
            if self.bad_price:
                return {"data": {"price": price}}
            return {
                "data": {
                    "price": price,
                    "productPrice": price,
                    "productList": [
                        {"productCode": x["productCode"], "quantity": x["quantity"]}
                        for x in items
                    ],
                }
            }
        raise AssertionError("Unexpected tool: " + name)
