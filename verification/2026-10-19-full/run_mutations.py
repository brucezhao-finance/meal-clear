"""Verify tests detect eight deliberately introduced faults in isolated copies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
CASES = [
    (
        "M01",
        "未知属性冒充通过",
        "core.py",
        'status = "unknown"',
        'status = "verified"',
        "test_core_full.py",
    ),
    (
        "M02",
        "恰好预算被误拒绝",
        "core.py",
        'plan["total_cents"] <= req["budget_cents"]',
        'plan["total_cents"] < req["budget_cents"]',
        "test_behavior.py",
    ),
    (
        "M03",
        "漏算本次新卡费",
        "core.py",
        'sum(o["price_cents"] + o.get("card_fee_cents", 0) for o in offers)',
        'sum(o["price_cents"] for o in offers)',
        "test_behavior.py",
    ),
    (
        "M04",
        "写工具白名单被移除",
        "mcp.py",
        "if name not in READ_ONLY:",
        "if False:",
        "test_protocol_full.py",
    ),
    (
        "M05",
        "布尔RPC编号误当整数",
        "mcp.py",
        'type(result.get("id")) is not type(rid) or result.get("id") != rid',
        'result.get("id") != rid',
        "test_protocol_full.py",
    ),
    (
        "M06",
        "实时改单复用旧会话",
        "cli.py",
        "result = LivePlanner(client_from_args(args)).plan(request)",
        "result = state",
        "test_cli_full.py",
    ),
    (
        "M07",
        "错误上下文报价被接受",
        "core.py",
        'q and q.get("context") == req["context"] and q.get("signature") == sig',
        "q",
        "test_behavior.py",
    ),
    (
        "M08",
        "会员回本错误向下取整",
        "core.py",
        "(card_fee_cents + saving_per_visit_cents - 1) // saving_per_visit_cents",
        "card_fee_cents // saving_per_visit_cents",
        "test_behavior.py",
    ),
]


def main() -> int:
    """Require each targeted suite to pass unchanged and fail after one injected fault."""
    parser_output = Path(__file__).resolve().parent / "mutations"
    parser_output.mkdir(exist_ok=True)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment.pop("MCD_MCP_TOKEN", None)
    records = []
    for cid, title, module, original, replacement, pattern in CASES:
        with tempfile.TemporaryDirectory(prefix="meal-clear-mutation-") as temporary:
            stage = Path(temporary)
            for folder in ("scripts", "tests", "examples"):
                shutil.copytree(
                    ROOT / folder,
                    stage / folder,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            command = [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                pattern,
            ]
            baseline = subprocess.run(
                command,
                cwd=stage,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            file = stage / "scripts/meal_clear" / module
            source = file.read_text()
            assert original in source, cid + " mutation no longer matches source"
            file.write_text(source.replace(original, replacement, 1))
            mutated = subprocess.run(
                command,
                cwd=stage,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            text = (mutated.stdout + mutated.stderr).replace(str(stage), "<隔离副本>")
            (parser_output / (cid + ".txt")).write_text(text)
            row: Dict[str, Any] = {
                "id": cid,
                "fault": title,
                "module": module,
                "target_suite": pattern,
                "baseline_exit": baseline.returncode,
                "mutated_exit": mutated.returncode,
                "detected": baseline.returncode == 0 and mutated.returncode != 0,
            }
            records.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {
        "method": "八个预先定义的关键缺陷变异；不是整个代码空间的自动变异覆盖率",
        "count": len(records),
        "detected": sum(r["detected"] for r in records),
        "cases": records,
    }
    (parser_output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    return int(summary["detected"] != summary["count"])


if __name__ == "__main__":
    raise SystemExit(main())
