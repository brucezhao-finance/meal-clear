"""P0 输出基线护栏：≤6 人的既有命令输出必须逐字节不变（T-S1）。

基线取自 main 分支 adf194b（stdout 的 sha256 前 16 位）。任何修改若让这些哈希变化，
说明改变了既有用户可见行为，必须先确认再更新。
"""
import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from meal_clear.cli import main  # noqa: E402

CATALOG = str(ROOT / "examples/demo-catalog-multi.json")
REQ3 = str(ROOT / "examples/request-multi3.json")
REQ6 = str(ROOT / "examples/request-multi6.json")

# (说明, 参数, 哈希前16位)
BASELINES = (
    ("demo", ("demo",), "52cdb0150591d5be"),
    ("demo --json", ("demo", "--json"), "e315dc421da65114"),
    ("demo-multi", ("demo-multi",), "249f5e7ec89a2c4b"),
    ("demo-multi --json", ("demo-multi", "--json"), "68d50f42f4cdca89"),
    ("plan multi6 文本", ("plan", "--request", REQ6, "--catalog", CATALOG), "adad11d99fbf6264"),
    ("plan multi6 --json", ("plan", "--request", REQ6, "--catalog", CATALOG, "--json"), "250500cc08c3783e"),
    ("plan multi3 --json --top 5",
     ("plan", "--request", REQ3, "--catalog", CATALOG, "--json", "--top", "5"), "b1234e3e18bec23e"),
)
DEMO_MULTI_REQUEST_ID = "c5b318f77bfe4631"


def run(*argv):
    """进程内运行 CLI，返回 (退出码, 标准输出)。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


def digest16(text: str) -> str:
    """stdout 的 sha256 前 16 位。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class P0RegressionTests(unittest.TestCase):
    """≤6 人输出基线。"""

    def test_output_hashes_unchanged(self):
        """七条既有命令的输出哈希与退出码保持基线。"""
        for label, argv, expected in BASELINES:
            with self.subTest(label):
                code, text = run(*argv)
                self.assertEqual(code, 0)
                self.assertEqual(digest16(text), expected)

    def test_output_deterministic(self):
        """同一命令连续运行两次输出完全相同。"""
        for label, argv, _ in BASELINES:
            with self.subTest(label):
                self.assertEqual(run(*argv), run(*argv))

    def test_demo_still_38_to_43(self):
        """demo 仍是 ¥38.00 → ¥43.00、delta_cents 500、退出码 0。"""
        code, text = run("demo")
        self.assertEqual(code, 0)
        self.assertIn("¥38.00", text)
        self.assertIn("¥43.00", text)
        code, text = run("demo", "--json")
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual(data["difference"]["delta_cents"], 500)
        self.assertEqual(data["before"]["recommendations"][0]["total_cents"], 3800)
        self.assertEqual(data["after"]["recommendations"][0]["total_cents"], 4300)

    def test_demo_multi_request_id_stable(self):
        """demo-multi 的 request_id 不因新增常量而变化。"""
        code, text = run("demo-multi", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["request_id"], DEMO_MULTI_REQUEST_ID)


if __name__ == "__main__":
    unittest.main()
