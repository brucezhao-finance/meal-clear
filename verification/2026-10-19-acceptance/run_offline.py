"""Run independent CLI acceptance cases; retain inputs and full offline outputs."""

from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INPUTS = HERE / "inputs"
OUTPUTS = HERE / "outputs"
STATE = ROOT / "local-state/acceptance-20261019"


def write_json(path: Path, value: Any) -> str:
    """Persist inspectable synthetic test inputs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return str(path.relative_to(ROOT))


def offer(oid: str, price: int, meat: str = "chicken") -> Dict[str, Any]:
    """Create a fictional complete meal with explicit evidence."""
    return {
        "id": oid,
        "name": "验收模拟" + oid,
        "price_cents": price,
        "card_fee_cents": 0,
        "facts": {
            k: {"value": v, "source": "验收自建模拟证据，非官方数据"}
            for k, v in {
                "non_spicy": True,
                "sugar_free_drink": True,
                "main_meat": meat,
            }.items()
        },
        "items": [
            {
                "code": oid + "-main",
                "name": "验收模拟主食",
                "role": "main",
                "quantity": 1,
            },
            {
                "code": oid + "-drink",
                "name": "验收模拟无糖饮料",
                "role": "drink",
                "quantity": 1,
            },
        ],
    }


def verify(result: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    """Check observable business outcomes, independent of rendered wording."""
    failures = []
    output = result["parsed"]
    if result["returncode"] != expected.get("exit", 0):
        failures.append(
            f"exit expected {expected.get('exit', 0)}, got {result['returncode']}"
        )
    if not isinstance(output, dict):
        failures.append("输出不能解析为JSON对象")
        return failures
    if "status" in expected and output.get("status") != expected["status"]:
        failures.append(
            f"status expected {expected['status']}, got {output.get('status')}"
        )
    recs = output.get("recommendations", [])
    if "totals" in expected and [r["total_cents"] for r in recs] != expected["totals"]:
        failures.append("推荐总价不符合独立人工计算")
    if expected.get("no_recommendation") and recs:
        failures.append("本应拒绝，却给出推荐")
    if (
        "delta" in expected
        and output.get("difference", {}).get("delta_cents") != expected["delta"]
    ):
        failures.append("修改价差错误")
    if expected.get("preserve_a"):
        if output.get("request", {}).get("people", [{}])[0] != REQUEST["people"][0]:
            failures.append("A的要求被改变")
        if output.get("request", {}).get("budget_cents") != REQUEST["budget_cents"]:
            failures.append("预算被改变")
    for recommendation in recs:
        if recommendation["total_cents"] > output["request"]["budget_cents"]:
            failures.append("推荐越过总预算")
        if any(
            c["status"] != "verified"
            for a in recommendation["assignments"]
            for c in a["checks"]
        ):
            failures.append("推荐包含未核实硬要求")
    if expected.get("private_state"):
        path = STATE / "original.json"
        if path.stat().st_mode & 0o777 != 0o600:
            failures.append("状态文件权限并非600")
    return failures


REQUEST = {
    "context": {
        "store_code": "ACCEPTANCE-DEMO",
        "channel": "pickup",
        "meal_time": "synthetic",
    },
    "budget_cents": 6000,
    "people": [
        {
            "id": pid,
            "hard": {"non_spicy": True, "sugar_free_drink": True},
            "prefer": {"main_meat": "chicken"},
        }
        for pid in ("A", "B")
    ],
}
CATALOG = {
    "mode": "simulation",
    "as_of": "2026-10-19（验收自建模拟，非真实采集）",
    "context": REQUEST["context"],
    "order_fee_cents": 0,
    "price_source": "完全虚构验收价格",
    "offers": [
        offer("chicken", 2150),
        offer("beef", 2900, "beef"),
        offer("unknown", 1200),
        offer("spicy", 1800),
        offer("card", 1300),
    ],
}
CATALOG["offers"][2]["facts"].pop("non_spicy")
CATALOG["offers"][3]["facts"]["non_spicy"]["value"] = False
CATALOG["offers"][4]["card_fee_cents"] = 1200


def main() -> int:
    """Run scenarios as separate real CLI processes, saving original evidence."""
    cases = []
    catalog_path = write_json(INPUTS / "catalog.json", CATALOG)
    request_path = write_json(INPUTS / "request.json", REQUEST)
    patch_path = write_json(
        INPUTS / "patch-beef.json", {"people": {"B": {"hard": {"main_meat": "beef"}}}}
    )
    state_path = str((STATE / "original.json").relative_to(ROOT))

    def add(
        case_id: str, title: str, args: List[str], expected: Dict[str, Any]
    ) -> None:
        """Register one executable acceptance scenario."""
        cases.append((case_id, title, args, expected))

    def plan(
        case_id: str,
        title: str,
        request: Dict[str, Any],
        catalog: Dict[str, Any],
        expected: Dict[str, Any],
    ) -> None:
        """Register a plan with its own inspectable input files."""
        req = write_json(INPUTS / (case_id + "-request.json"), request)
        cat = write_json(INPUTS / (case_id + "-catalog.json"), catalog)
        add(
            case_id,
            title,
            ["plan", "--request", req, "--catalog", cat, "--json"],
            expected,
        )

    add(
        "O01",
        "两人独立选餐并保存600权限状态",
        [
            "plan",
            "--request",
            request_path,
            "--catalog",
            catalog_path,
            "--save",
            state_path,
            "--json",
        ],
        {"status": "simulation", "totals": [4300], "private_state": True},
    )
    add(
        "O02",
        "只改B为牛肉，A与6000分预算保持",
        [
            "update",
            "--state",
            state_path,
            "--patch",
            patch_path,
            "--catalog",
            catalog_path,
            "--json",
        ],
        {"status": "simulation", "totals": [5050], "delta": 750, "preserve_a": True},
    )
    req = copy.deepcopy(REQUEST)
    req["budget_cents"] = 4300
    plan(
        "O03", "恰好等于预算", req, CATALOG, {"status": "simulation", "totals": [4300]}
    )
    req["budget_cents"] = 4299
    plan(
        "O04",
        "差1分超预算仍拒绝",
        req,
        CATALOG,
        {"exit": 2, "status": "over_budget_in_checked_set", "no_recommendation": True},
    )
    req = copy.deepcopy(REQUEST)
    req["people"][1]["prefer"]["main_meat"] = "beef"
    plan("O05", "软偏好提供价格与口味两种取舍", req, CATALOG, {"totals": [4300, 5050]})
    cat = copy.deepcopy(CATALOG)
    cat["offers"] = [cat["offers"][2]]
    plan(
        "O06",
        "只有辣度未知低价餐，不能冒充满足不辣",
        REQUEST,
        cat,
        {"exit": 2, "status": "no_verified_plan", "no_recommendation": True},
    )
    cat = copy.deepcopy(CATALOG)
    cat["offers"] = [cat["offers"][3]]
    plan(
        "O07",
        "只有明确辣味餐，拒绝不辣要求",
        REQUEST,
        cat,
        {"exit": 2, "status": "no_verified_plan", "no_recommendation": True},
    )
    req = copy.deepcopy(REQUEST)
    req["people"] = req["people"][:1]
    cat = copy.deepcopy(CATALOG)
    cat["offers"] = [cat["offers"][4]]
    plan("O08", "卡价1300加新卡1200，应付2500分", req, cat, {"totals": [2500]})
    cat = copy.deepcopy(CATALOG)
    cat.pop("order_fee_cents")
    plan(
        "O09",
        "费用缺失不能补0",
        REQUEST,
        cat,
        {"exit": 2, "status": "needs_price_verification", "no_recommendation": True},
    )
    cat = copy.deepcopy(CATALOG)
    cat["context"] = {**cat["context"], "store_code": "OTHER"}
    plan(
        "O10",
        "换店后旧菜单不可沿用",
        REQUEST,
        cat,
        {"exit": 2, "status": "context_mismatch", "no_recommendation": True},
    )
    for cid, mode in [("O11", "historical"), ("O12", "user_supplied")]:
        cat = copy.deepcopy(CATALOG)
        cat["mode"] = mode
        plan(
            cid,
            "资料来源模式必须保留：" + mode,
            REQUEST,
            cat,
            {"status": mode, "totals": [4300]},
        )
    req = copy.deepcopy(REQUEST)
    req["people"].append({"id": "C"})
    plan(
        "O13",
        "三人超出范围，应返回受控输入错误",
        req,
        CATALOG,
        {"exit": 1, "status": "error"},
    )
    req = copy.deepcopy(REQUEST)
    req["budget_cents"] = True
    plan("O14", "布尔预算不是1分", req, CATALOG, {"exit": 1, "status": "error"})
    req = copy.deepcopy(REQUEST)
    req["people"][1]["id"] = "A"
    plan("O15", "重复用户id拒绝", req, CATALOG, {"exit": 1, "status": "error"})
    req = copy.deepcopy(REQUEST)
    req["people"][0]["hard"]["allergen_free"] = True
    plan(
        "O16",
        "未知过敏安全属性不得保证通过",
        req,
        CATALOG,
        {"exit": 2, "status": "no_verified_plan", "no_recommendation": True},
    )
    add(
        "O17",
        "离线修改未给新菜单应拒绝",
        ["update", "--state", state_path, "--patch", patch_path, "--json"],
        {"exit": 1, "status": "error"},
    )
    add(
        "O18",
        "固定回本1401除200向上取整8次",
        ["break-even", "--fee", "1401", "--saving", "200"],
        {"exit": 0},
    )
    add(
        "O19",
        "每次节省为0不能伪造回本",
        ["break-even", "--fee", "1400", "--saving", "0"],
        {"exit": 0},
    )
    bad_patch = write_json(
        INPUTS / "patch-unknown-person.json",
        {"people": {"C": {"hard": {"main_meat": "beef"}}}},
    )
    add(
        "O20",
        "修改不存在的C拒绝",
        [
            "update",
            "--state",
            state_path,
            "--patch",
            bad_patch,
            "--catalog",
            catalog_path,
            "--json",
        ],
        {"exit": 1, "status": "error"},
    )
    cat = copy.deepcopy(CATALOG)
    cat["offers"] = [None]
    plan(
        "O21",
        "损坏菜单offers元素为null，应受控拒绝",
        REQUEST,
        cat,
        {"exit": 1, "status": "error"},
    )
    cat = copy.deepcopy(CATALOG)
    cat["offers"][0]["items"] = [None]
    plan(
        "O22",
        "损坏餐品元素为null，应受控拒绝",
        REQUEST,
        cat,
        {"exit": 1, "status": "error"},
    )
    cat = copy.deepcopy(CATALOG)
    cat["offers"][0]["facts"] = []
    plan(
        "O23",
        "损坏事实集合为数组，应受控拒绝",
        REQUEST,
        cat,
        {"exit": 1, "status": "error"},
    )
    add("O24", "用绝对路径从其他工作目录运行演示", ["demo", "--json"], {"exit": 0})
    malformed = INPUTS / "malformed.json"
    malformed.write_text('{"budget_cents":')
    add(
        "O25",
        "损坏JSON不得打印原始文件",
        [
            "plan",
            "--request",
            str(malformed.relative_to(ROOT)),
            "--catalog",
            catalog_path,
            "--json",
        ],
        {"exit": 1, "status": "error"},
    )
    budget_patch = write_json(INPUTS / "patch-tight-budget.json", {"budget_cents": 100})
    add(
        "O26",
        "修改后无方案，价差保持null",
        [
            "update",
            "--state",
            state_path,
            "--patch",
            budget_patch,
            "--catalog",
            catalog_path,
            "--json",
        ],
        {
            "exit": 2,
            "status": "over_budget_in_checked_set",
            "delta": None,
            "no_recommendation": True,
        },
    )
    coupon_catalog = copy.deepcopy(CATALOG)
    coupon_catalog["offers"] = coupon_catalog["offers"][:1]
    coupon_catalog["offers"][0]["coupon_key"] = "SYNTHETIC-ONE-COUPON"
    plan(
        "O27",
        "仅有一张券的餐不能同单重复分给两人",
        REQUEST,
        coupon_catalog,
        {"exit": 2, "status": "no_verified_plan", "no_recommendation": True},
    )
    results = []
    for cid, title, args, expected in cases:
        command = [sys.executable, str(ROOT / "scripts/meal_clear.py"), *args]
        started = time.perf_counter()
        process = subprocess.run(
            command,
            cwd="/private/tmp" if cid == "O24" else ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        try:
            parsed = json.loads(process.stdout)
        except json.JSONDecodeError:
            parsed = None
        record = {
            "id": cid,
            "title": title,
            "command": (
                shlex.join(["python3", "scripts/meal_clear.py", *args])
                if cid != "O24"
                else "cd /private/tmp && python3 <项目绝对路径>/scripts/meal_clear.py demo --json"
            ),
            "returncode": process.returncode,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "expected": expected,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "parsed": parsed,
        }
        failures = verify(record, expected)
        if cid in {"O18", "O19"} and parsed != {"visits": 8 if cid == "O18" else None}:
            failures.append("会员回本结果错误")
        if cid == "O24" and (
            parsed["before"]["recommendations"][0]["total_cents"] != 3800
            or parsed["difference"]["delta_cents"] != 500
        ):
            failures.append("异目录演示结果错误")
        record.update(passed=not failures, failures=failures)
        write_json(OUTPUTS / (cid + ".json"), record)
        results.append(
            {
                k: record[k]
                for k in (
                    "id",
                    "title",
                    "command",
                    "returncode",
                    "elapsed_ms",
                    "expected",
                    "passed",
                    "failures",
                )
            }
        )
        print(cid, "PASS" if not failures else "FAIL", title, flush=True)
    write_json(
        HERE / "offline-summary.json",
        {
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "cases": results,
            "passed": sum(r["passed"] for r in results),
            "failed": sum(not r["passed"] for r in results),
        },
    )
    return int(any(not r["passed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
