"""Run all offline tests and optional branch coverage with inspectable event records."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import time
import unittest
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


class RecordedResult(unittest.TextTestResult):
    """Record method outcomes and subcases without multiplying the method count."""

    def startTest(self, test: unittest.TestCase) -> None:
        """Mark the start of an actual test execution."""
        super().startTest(test)
        self.started = time.perf_counter()

    def addSuccess(self, test: unittest.TestCase) -> None:
        """Retain successful method identity, purpose and elapsed time."""
        super().addSuccess(test)
        self.events.append(
            {
                "id": test.id(),
                "kind": "method",
                "passed": True,
                "description": test.shortDescription(),
                "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
            }
        )

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any
    ) -> None:
        """Keep each data subcase distinct, with bounded synthetic parameter descriptions."""
        super().addSubTest(test, subtest, err)
        params = {str(key): repr(value)[:180] for key, value in subtest.params.items()}
        self.events.append(
            {
                "id": test.id(),
                "kind": "subcase",
                "parameters": params,
                "passed": err is None,
            }
        )

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        """Keep failure status; the standard log carries the full traceback."""
        super().addFailure(test, err)
        self.events.append({"id": test.id(), "kind": "method", "passed": False})

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        """Record errors distinctly in the standard unittest log."""
        super().addError(test, err)
        self.events.append({"id": test.id(), "kind": "method", "passed": False})


def main() -> int:
    """Collect unit, contract, oracle, integration-fixture and CLI results together."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    coverage = None
    if args.coverage:
        import coverage as coverage_module

        coverage = coverage_module.Coverage(
            source=[str(ROOT / "scripts")],
            branch=True,
            data_file=str(ROOT / "local-state/fulltest/.coverage"),
        )
        coverage.start()
    start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    elapsed = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    RecordedResult.events = []
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream, verbosity=2, resultclass=RecordedResult
    ).run(suite)
    (HERE / "logs/final-tests.txt").write_text(
        stream.getvalue().replace(str(ROOT), "<项目目录>")
    )
    events = result.events
    summary: Dict[str, Any] = {
        "started_at": start,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": platform.python_version(),
        "elapsed_ms": round((time.perf_counter() - elapsed) * 1000),
        "test_methods_run": result.testsRun,
        "failed_methods": len(result.failures),
        "errored_methods": len(result.errors),
        "subcases_observed": sum(e["kind"] == "subcase" for e in events),
        "passed": result.wasSuccessful(),
        "source_commit_before_changes": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "runtime_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "scripts").rglob("*.py"))
        },
    }
    if coverage is not None:
        coverage.stop()
        coverage.save()
        coverage.json_report(outfile=str(HERE / "coverage/final.json"))
        text = io.StringIO()
        coverage.report(file=text, precision=2)
        (HERE / "coverage/final.txt").write_text(text.getvalue())
        data = json.loads((HERE / "coverage/final.json").read_text())
        summary["coverage"] = data["totals"]
        # Retain the original defensive guard instead of fabricating an impossible retry-loop exit.
        code = (ROOT / "scripts/meal_clear/mcp.py").read_text().splitlines()
        guard = next(
            i
            for i, line in enumerate(code, 1)
            if 'raise McpError("未完成请求")' in line
        )
        missing = {}
        for name, details in data["files"].items():
            bad = [
                line
                for line in details["missing_lines"]
                if not (name.endswith("scripts/meal_clear/mcp.py") and line == guard)
            ]
            arcs = [
                arc
                for arc in details["missing_branches"]
                if not (name.endswith("scripts/meal_clear/mcp.py") and arc[1] == guard)
            ]
            if bad or arcs:
                missing[name] = {"lines": bad, "branches": arcs}
        summary["reachable_coverage_gate_passed"] = not missing
        summary["unexpected_gaps"] = missing
        summary["retained_unreachable_guard"] = {
            "file": "scripts/meal_clear/mcp.py",
            "line": guard,
            "reason": "三次迭代内每次成功均return，最终一次允许的暂时故障必raise；正常range(3)不可能到达循环后兜底",
        }
        summary["passed"] = summary["passed"] and not missing
    (HERE / "test-events.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2) + "\n"
    )
    (HERE / "automated-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(not summary["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
