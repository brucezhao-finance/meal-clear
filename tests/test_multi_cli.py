"""v2 离线 CLI：demo-multi、plan/update 分派、--top/--priority、输出隐私（T-F1）。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from meal_clear.cli import main  # noqa: E402

CATALOG = str(ROOT / "examples/demo-catalog-multi.json")
REQ3 = str(ROOT / "examples/request-multi3.json")
REQ6 = str(ROOT / "examples/request-multi6.json")
BANNED = ("全网最低", "一定买得到", "库存保证", "couponCode", "couponId", "SENTINEL")


def run(*argv):
    """在进程内运行 CLI，返回 (退出码, 标准输出)。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class MultiCliTests(unittest.TestCase):
    """真实 CLI 入口。"""

    def test_demo_multi_text(self):
        """文本含名次表头、理由、统计说明、模拟标签、边界提示。"""
        code, text = run("demo-multi")
        self.assertEqual(code, 0)
        for needle in ("名次 | 总价", "理由：", "不是全局最优", "模拟演示", "不下单", "第1名", "第3名"):
            self.assertIn(needle, text)
        for word in BANNED:
            self.assertNotIn(word, text)

    def test_plan_json_has_ranking_and_legacy_fields(self):
        """--json 可解析，含 ranking 与 v1 旧字段；6 人也可用。"""
        code, text = run("plan", "--request", REQ6, "--catalog", CATALOG, "--json")
        self.assertEqual(code, 0)
        data = json.loads(text)
        for key in ("ranking", "ranking_meta", "no_result", "recommendations",
                    "plans", "search", "request_id", "mode", "as_of", "status",
                    "diagnostics", "coupon_preview", "schema_version"):
            self.assertIn(key, data)
        self.assertEqual(len(data["ranking"][0]["assignments"]), 6)
        for word in BANNED:
            self.assertNotIn(word, text)

    def test_top_and_priority_options(self):
        """--top 控制名次数；--priority 改变规则文案。"""
        code, text = run("plan", "--request", REQ3, "--catalog", CATALOG,
                         "--json", "--top", "5", "--priority", "preference")
        data = json.loads(text)
        self.assertEqual(len(data["ranking"]), 5)
        self.assertEqual(data["ranking_meta"]["priority"], "preference")
        prefs = [i["preference_matches"] for i in data["ranking"]]
        self.assertEqual(prefs, sorted(prefs, reverse=True))

    def test_v1_unaffected_and_rank_flags_rejected_for_v1(self):
        """v1 请求走旧路径；v1 使用 --top 报错。"""
        code, text = run("plan", "--request", str(ROOT / "examples/request.json"),
                         "--catalog", str(ROOT / "examples/demo-catalog.json"),
                         "--top", "2")
        self.assertEqual(code, 1)
        self.assertIn("仅适用于 schema_version=2", text)
        code, text = run("demo", "--json")
        data = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(data["difference"]["delta_cents"], 500)

    def test_update_v2_roundtrip_and_no_change(self):
        """保存 → update 修改 → 回显差异；无变化不重算。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state.json")
            patch = Path(tmp) / "patch.json"
            code, _ = run("plan", "--request", REQ3, "--catalog", CATALOG, "--save", state)
            self.assertEqual(code, 0)
            patch.write_text(json.dumps({"budget_cents": 20000}), encoding="utf-8")
            code, text = run("update", "--state", state, "--patch", str(patch),
                             "--catalog", CATALOG, "--json")
            self.assertEqual(code, 0)
            data = json.loads(text)
            self.assertEqual(data["request"]["budget_cents"], 20000)
            self.assertIn("difference", data)
            patch.write_text(json.dumps({"budget_cents": 7000}), encoding="utf-8")
            code, text = run("update", "--state", state, "--patch", str(patch),
                             "--catalog", CATALOG)
            self.assertEqual(json.loads(text)["status"], "no_change")
            patch.write_text(json.dumps({"people": {"C": {"hard": {"main_meat": "tofu"}}}}),
                             encoding="utf-8")
            code, text = run("update", "--state", state, "--patch", str(patch),
                             "--catalog", CATALOG)
            self.assertEqual(code, 2)
            self.assertIn("没有可确认的预算内方案", text)

    def test_live_plan_v2_not_yet_available(self):
        """v2 live-plan 本特性未实现，明确报错且不联网。"""
        code, text = run("live-plan", "--request", REQ3)
        self.assertEqual(code, 1)
        self.assertIn("后续特性", text)


if __name__ == "__main__":
    unittest.main()
