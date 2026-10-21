"""50 人紧凑报告与 CLI（T-S9）：行数、同质组汇总、--detail 展开、--json 完整、README 命令可运行。"""
import contextlib
import io
import json
import re
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from meal_clear.cli import main  # noqa: E402
from meal_clear.config import COMPACT_MIN_PEOPLE  # noqa: E402

CATALOG = str(ROOT / "examples/demo-catalog-multi.json")
REQ50 = str(ROOT / "examples/request-multi50.json")
REQ6 = str(ROOT / "examples/request-multi6.json")


def run(*argv):
    """进程内运行 CLI，返回 (退出码, 标准输出)。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class CompactReportTests(unittest.TestCase):
    """紧凑报告与展开。"""

    def test_fifty_people_text_is_compact(self):
        """文本 <120 行，按 4 个同质组汇总，含组标签、人数与硬要求核实状态。"""
        code, text = run("plan", "--request", REQ50, "--catalog", CATALOG)
        self.assertEqual(code, 0)
        self.assertLess(len(text.splitlines()), 120)
        for label in ("G1", "G2", "G3", "G4"):
            self.assertIn(label + "（", text)
        self.assertIn("4 个同质组", text)
        self.assertIn("硬要求全部已核实", text)
        self.assertIn("（14人：", text)
        self.assertNotIn("- P20：", text)
        self.assertIn("同质组", text.split("## 检查范围")[1])

    def test_detail_expands_each_person(self):
        """--detail 展开逐人。"""
        code, text = run("plan", "--request", REQ50, "--catalog", CATALOG, "--detail")
        self.assertEqual(code, 0)
        self.assertGreater(len(text.splitlines()), 300)
        for index in (1, 25, 50):
            self.assertIn(f"- P{index:02d}：", text)

    def test_json_has_all_assignments(self):
        """--json 永远完整：每个名次 50 条 assignments，且不受 --detail 影响。"""
        code, raw = run("plan", "--request", REQ50, "--catalog", CATALOG, "--json")
        self.assertEqual(code, 0)
        data = json.loads(raw)
        self.assertEqual(len(data["request"]["people"]), 50)
        self.assertEqual(len(data["ranking"]), 3)
        for item in data["ranking"]:
            self.assertEqual(len(item["assignments"]), 50)
            self.assertEqual([a["person_id"] for a in item["assignments"]], [f"P{i:02d}" for i in range(1, 51)])
        _, with_detail = run("plan", "--request", REQ50, "--catalog", CATALOG, "--json", "--detail")
        self.assertEqual(raw, with_detail)

    def test_threshold_boundary(self):
        """≤12 人不压缩（6 人输出含逐人行）；阈值常量为 13。"""
        self.assertEqual(COMPACT_MIN_PEOPLE, 13)
        _, text = run("plan", "--request", REQ6, "--catalog", CATALOG)
        self.assertIn("- P6：", text)
        self.assertNotIn("同质组汇总", text)

    def test_max_people_message_and_limit(self):
        """第 51 人被拒并提示『1至50位』（文案由 MAX_PEOPLE 生成）。"""
        data = json.loads(Path(REQ50).read_text(encoding="utf-8"))
        data["people"].append({"id": "P51", "hard": {}, "prefer": {}})
        tmp = ROOT / "tests" / "_tmp_req51.json"
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            code, text = run("plan", "--request", str(tmp), "--catalog", CATALOG)
            self.assertEqual(code, 1)
            self.assertIn("1至50位", text)
        finally:
            tmp.unlink(missing_ok=True)


class ReadmeCommandTests(unittest.TestCase):
    """README 里的 50 人命令必须真实可运行。"""

    def test_readme_plan_commands_exit_zero(self):
        """README 中引用 request-multi50 的 plan 命令逐条运行，退出码 0。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        commands = [
            line.strip()
            for line in readme.splitlines()
            if "meal_clear.py plan" in line and "request-multi50" in line
        ]
        self.assertTrue(commands, "README 缺少 50 人 plan 示例命令")
        for command in commands:
            argv = shlex.split(re.sub(r"^.*?meal_clear\.py\s+", "", command))
            argv = [str(ROOT / a) if a.startswith("examples/") else a for a in argv]
            with self.subTest(command=command):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(argv), 0)


if __name__ == "__main__":
    unittest.main()
