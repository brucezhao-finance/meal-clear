"""Run read-only multi-store smoke cases; log sanitized results without credentials."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.cli import save_json  # noqa: E402
from meal_clear.core import amend_request, compare_results  # noqa: E402
from meal_clear.live import LivePlanner  # noqa: E402
from meal_clear.mcp import READ_ONLY, McpClient, McpError  # noqa: E402


def main() -> int:
    """Exercise every allowed tool plus planning/update invariants in two cities."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file")
    parser.add_argument("--output-dir", default="local-state/full-live-replay")
    args = parser.parse_args()
    output = ROOT / args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    client = McpClient(token_file=args.token_file, timeout=12)
    client.initialize()
    schemas = client.list_tools()["tools"]
    rows = []
    called = set()
    start_all = time.perf_counter()

    def record(
        cid: str, task: str, payload: Dict[str, Any], started: float, passed: bool
    ) -> None:
        """Persist only normalized public data or already sanitized planner output."""
        item = {
            "id": cid,
            "task": task,
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "passed": passed,
            "output": payload,
        }
        (output / (cid + ".json")).write_text(
            json.dumps(item, ensure_ascii=False, indent=2) + "\n"
        )
        rows.append(
            {k: item[k] for k in ("id", "task", "finished_at", "elapsed_ms", "passed")}
        )
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    t = time.perf_counter()
    record(
        "R00",
        "当前工具发现",
        {
            "count": len(schemas),
            "available_read_only": [
                s["name"] for s in schemas if s["name"] in READ_ONLY
            ],
        },
        t,
        READ_ONLY.issubset({s["name"] for s in schemas}),
    )
    for cid, name in [("R01", "now-time-info"), ("R02", "campaign-calendar")]:
        t = time.perf_counter()
        try:
            result = client.call(name, {})
            called.add(name)
            summary = {
                "tool": name,
                "business_code": result.get("code"),
                "payload_keys": sorted(result.keys()),
                "data_type": type(result.get("data")).__name__,
            }
            record(cid, "白名单工具实际查询：" + name, summary, t, True)
        except McpError as error:
            record(cid, name, {"error": str(error)}, t, False)
    for region, city, keyword in [("SH", "上海", "静安寺"), ("BJ", "北京", "王府井")]:
        t = time.perf_counter()
        try:
            found = client.call(
                "query-nearby-stores",
                {"beType": 1, "searchType": 2, "city": city, "keyword": keyword},
            ).get("data", [])
            called.add("query-nearby-stores")
            stores = [
                {
                    "storeCode": str(s.get("storeCode", "")),
                    "storeName": s.get("storeName"),
                }
                for s in found
                if isinstance(s, dict) and str(s.get("storeCode", "")).isdigit()
            ]
            chosen = next(
                (s for s in stores if s["storeCode"] == "1450002"),
                stores[0] if stores else None,
            )
            record(
                region + "-S",
                "搜索工程测试店，非用户默认位置",
                {"stores": stores, "engineering_selection": chosen},
                t,
                bool(chosen),
            )
            if chosen is None:
                continue
        except McpError as error:
            record(region + "-S", "工程店查询", {"error": str(error)}, t, False)
            continue
        request = {
            "context": {
                "store_code": chosen["storeCode"],
                "channel": "pickup",
                "meal_time": "now",
            },
            "budget_cents": 15000,
            "people": [{"id": "A", "hard": {}, "prefer": {}}],
        }
        previous = None
        for number, task in [
            (1, "单人默认组合真实核价"),
            (2, "增加无糖要求后重新取数核价"),
            (3, "两人逐人无糖核价"),
            (4, "加入不辣要求检查未知不得通过"),
            (5, "极低预算不可静默放宽"),
        ]:
            t = time.perf_counter()
            audit_start = len(client.audit)
            req = json.loads(json.dumps(request))
            if number == 2:
                req = amend_request(
                    request, {"people": {"A": {"hard": {"sugar_free_drink": True}}}}
                )
            if number == 3:
                req["people"][0]["hard"] = {"sugar_free_drink": True}
                req["people"].append(
                    {
                        "id": "B",
                        "hard": {"sugar_free_drink": True},
                        "prefer": {"main_meat": "chicken"},
                    }
                )
            if number == 4:
                req["people"][0]["hard"] = {"sugar_free_drink": True, "non_spicy": True}
            if number == 5:
                req["budget_cents"] = 1
            try:
                result = LivePlanner(client).plan(req)
                if number == 2 and previous is not None:
                    result["difference"] = compare_results(previous, result)
                recs = result.get("recommendations", [])
                passed = all(
                    p["total_cents"] <= req["budget_cents"]
                    and p["price_status"] == "official_verified"
                    and all(
                        c["status"] == "verified"
                        for a in p["assignments"]
                        for c in a["checks"]
                    )
                    for p in recs
                )
                if number in (1, 2, 3):
                    passed = passed and bool(recs)
                if number == 3 and recs:
                    passed = passed and len(recs[0]["assignments"]) == 2
                if (
                    number == 2
                    and previous
                    and previous.get("recommendations")
                    and recs
                ):
                    passed = (
                        passed
                        and result["difference"]["delta_cents"]
                        == recs[0]["total_cents"]
                        - previous["recommendations"][0]["total_cents"]
                    )
                    passed = (
                        passed
                        and recs[0]["quoted_at"]
                        > previous["recommendations"][0]["quoted_at"]
                    )
                    passed = (
                        passed
                        and result["request"]["context"]
                        == previous["request"]["context"]
                    )
                if number == 1:
                    previous = result
                audit = client.audit[audit_start:]
                called.update(a["tool"] for a in audit)
                passed = (
                    passed
                    and all(a["tool"] in READ_ONLY for a in audit)
                    and result["quote_attempts"] <= 3
                )
                record(
                    region + "-" + str(number),
                    task,
                    {"result": result, "case_audit": audit},
                    t,
                    passed,
                )
                save_json(
                    str(
                        ROOT
                        / "local-state/full-live"
                        / region
                        / (str(number) + ".json")
                    ),
                    result,
                )
            except McpError as error:
                record(
                    region + "-" + str(number), task, {"error": str(error)}, t, False
                )
    summary = {
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "elapsed_ms": round((time.perf_counter() - start_all) * 1000),
        "passed": sum(r["passed"] for r in rows),
        "failed": sum(not r["passed"] for r in rows),
        "cases": rows,
        "called_read_only_tools": sorted(called),
        "all_seven_tools_called": READ_ONLY.issubset(called),
        "transactions_executed": 0,
        "note": "单账户、当次两城工程门店；不代表多账户或未来餐段；无原始账户回包",
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in ("passed", "failed", "all_seven_tools_called", "elapsed_ms")
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return int(summary["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
