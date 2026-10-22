"""团餐命令：group-info、demo-group、delivery-stores --be-type 与示例文件（全部合成数据、回环模拟服务）。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from meal_clear import cli  # noqa: E402
from meal_clear.request_v2 import validate_request_v2  # noqa: E402
from sim_mcp import SimMcpServer  # noqa: E402
from sim_support import SENTINELS, request, run_cli, write_json  # noqa: E402
GROUP_FUL = {"type": "group", "be_code": "GBE30001", "address_index": 1, "gm_service_code": "GM-STD"}
def people(n=20):
    return [{"id": f"P{i:02d}", "hard": {}, "prefer": {}} for i in range(n)]
def local_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()
class GroupInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def path(self, **kwargs):
        kwargs.setdefault("fulfillment", GROUP_FUL)
        return write_json(self.dir, "g%d.json" % len(list(self.dir.glob("g*.json"))),
                          request(people=people(), store="30001", budget=kwargs.pop("budget", 200050), **kwargs))
    def test_text_output(self):
        with SimMcpServer() as sim:
            code, out = run_cli(sim, "group-info", "--request", self.path())
            self.assertEqual(code, 0, out)
            for text in ("模拟标准送达", "GM-ALT", "不可用", "以官方核价为准", "¥100.02", "余 10 分", "官方默认项"):
                self.assertIn(text, out)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, out)
            tools = [c["tool"] for c in sim.calls]
            self.assertIn("query-meal-assistance", tools)
            self.assertIn("query-promotions", tools)
            self.assertEqual(sim.write_attempts, [])
            self.assertEqual([c["args"]["beType"] for c in sim.calls if c["tool"] == "delivery-query-stores"], [6])
    def test_json_output_and_choice_status(self):
        cases = {"valid": "GM-STD", "unavailable": "GM-PRO", "not_in_list": "NOPE", "missing": None}
        for status, gm in cases.items():
            ful = dict(GROUP_FUL)
            if gm is None:
                del ful["gm_service_code"]
            else:
                ful["gm_service_code"] = gm
            with self.subTest(status), SimMcpServer() as sim:
                code, out = run_cli(sim, "group-info", "--request", self.path(fulfillment=ful), "--json")
                data = json.loads(out)
                self.assertEqual(code, 0, out)
                self.assertEqual(data["assistance"]["user_choice"]["status"], status)
                self.assertEqual(data["budget"]["per_person_cents"], 10002)
                self.assertEqual(data["budget"]["remainder_cents"], 10)
                self.assertEqual(len(data["assistance"]["options"]), 3)
                self.assertTrue(data["promotions"])
                self.assertNotIn("calculate-price", [c["tool"] for c in sim.calls])
                self.assertNotIn("query-meals", [c["tool"] for c in sim.calls])
    def test_requires_group_request_and_deliverable_store(self):
        with SimMcpServer() as sim:
            code, out = run_cli(sim, "group-info", "--request", self.path(fulfillment={"type": "pickup"}))
            self.assertEqual(code, 1, out)
            self.assertIn("团餐", out)
        with SimMcpServer() as sim:
            bad = dict(GROUP_FUL, be_code="NOT-DELIVERABLE")
            code, out = run_cli(sim, "group-info", "--request", self.path(fulfillment=bad))
            self.assertEqual(code, 1, out)
            self.assertIn("可配送列表", out)
            self.assertNotIn("query-meal-assistance", [c["tool"] for c in sim.calls])
class DeliveryStoresBeTypeTests(unittest.TestCase):
    def test_default_is_2_and_6_is_group(self):
        with SimMcpServer() as sim:
            code, out = run_cli(sim, "delivery-stores", "--address-index", "1")
            self.assertEqual(code, 0, out)
            self.assertIn("BE30001", out)
            code, out = run_cli(sim, "delivery-stores", "--address-index", "1", "--be-type", "6")
            self.assertEqual(code, 0, out)
            self.assertIn("GBE30001", out)
            types = [c["args"]["beType"] for c in sim.calls if c["tool"] == "delivery-query-stores"]
            self.assertEqual(types, [2, 6])
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, out)
class DemoGroupTests(unittest.TestCase):
    def test_demo_group_text(self):
        code, out = local_cli("demo-group")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[0], "模拟数据，非真实菜单/价格")
        for text in ("20 位用餐者", "¥100.02", "余 10 分", "主食＋小食＋小食＋饮料", "以官方核价为准", "助餐服务：模拟标准送达"):
            self.assertIn(text, out)
    def test_demo_group_json(self):
        code, out = local_cli("demo-group", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["notice"], "模拟数据，非真实菜单/价格")
        self.assertEqual(data["group"]["budget"]["per_person_cents"], 10002)
        self.assertEqual(data["group"]["assistance"]["selected"]["gm_service_code"], "GM-DEMO-1")
        self.assertEqual(len(data["ranking"]), 3)
    def test_old_demo_unchanged(self):
        code, out = local_cli("demo", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["difference"]["delta_cents"], 500)
class ExampleFileTests(unittest.TestCase):
    def test_examples_validate(self):
        for name in ("request-group-demo.json", "request-group-meal.json"):
            with self.subTest(name):
                req = validate_request_v2(json.loads((ROOT / "examples" / name).read_text(encoding="utf-8")))
                self.assertEqual(req["fulfillment"]["type"], "group")
                self.assertEqual(len(req["people"]), 20)
                self.assertEqual(req["context"]["meal_time"], "now" if "meal" in name else "demo-time")
    def test_live_example_uses_per_person_budget(self):
        req = validate_request_v2(json.loads((ROOT / "examples" / "request-group-meal.json").read_text(encoding="utf-8")))
        self.assertEqual(req["per_person_budget_cents"], 10000)
        self.assertEqual(req["budget_cents"], 200000)
class GroupDocsTests(unittest.TestCase):
    """团餐文档与配图 08：如实、不夸大。"""
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")
    def test_no_stale_group_wording(self):
        for rel in ("README.md", "SKILL.md", "MCP_INTEGRATION.md", "references/data-contract.md",
                    "docs/使用与接入.md", "docs/真实验证与能力边界.md", "docs/真实写操作启用指南.md"):
            text = self.read(rel)
            for phrase in ("团餐未实现", "团餐尚未实现", "团餐为后续阶段", "团餐（group）尚未实现"):
                with self.subTest(doc=rel, phrase=phrase):
                    self.assertNotIn(phrase, text)
    def test_readme_skill_reference_08_and_closed_wording(self):
        for rel in ("README.md", "SKILL.md"):
            text = self.read(rel)
            with self.subTest(doc=rel):
                self.assertIn("docs/images/08-group-meal.svg", text)
                self.assertIn("团餐真实下单未开放", text)
    def test_svg_08_valid(self):
        import xml.etree.ElementTree as ET
        path = ROOT / "docs/images/08-group-meal.svg"
        root = ET.parse(path).getroot()
        ns = "{http://www.w3.org/2000/svg}"
        self.assertEqual(root.get("role"), "img")
        self.assertTrue((root.find(ns + "title").text or "").strip())
        self.assertTrue((root.find(ns + "desc").text or "").strip())
        raw = path.read_text(encoding="utf-8")
        for text in ("模拟数据", "团餐真实下单未开放"):
            self.assertIn(text, raw)
    def test_mcp_integration_tools(self):
        text = self.read("MCP_INTEGRATION.md")
        for needle in ("12 个只读", "query-promotions", "query-meal-assistance"):
            self.assertIn(needle, text)
if __name__ == "__main__":
    unittest.main()
