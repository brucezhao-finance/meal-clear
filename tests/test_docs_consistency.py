"""文档一致性（T-X3）：如实表述、SVG 合法、相对链接有效、仓库文本无本机路径。"""
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    "README.md",
    "SKILL.md",
    "MCP_INTEGRATION.md",
    "CONTEST_DECLARATION.md",
    "AGENTS.md",
    "references/data-contract.md",
    "references/evaluation-cases.md",
    "docs/使用与接入.md",
    "docs/真实验证与能力边界.md",
    "docs/真实写操作启用指南.md",
]
NEW_SVGS = ["06-confirm-flow.svg", "07-safety-gates.svg"]
SKIP_DIRS = {".git", "dist", "verification", "local-state", "__pycache__", ".ruff_cache"}
# 这两个测试把本机路径前缀作为"不得出现"的断言字面量，属于允许的例外
PATH_LITERAL_EXEMPT = {"tests/test_packaging.py", "tests/test_sim_write_tools.py"}
TEXT_SUFFIXES = {".md", ".py", ".json", ".svg", ".toml", ".sh", ".yaml", ".yml", ".txt"}
FORBIDDEN_CLAIMS = (
    "已支持真实下单", "真实下单已验证", "全网最低价已", "全网最低价格",
    "已验证真实下单", "真实领券已验证", "真实下单已通过", "已支持真实领券", "真实下单已支持",
)
TIER3 = "模拟已验证 / 真实只读待用户验证 / 真实领券下单代码已具备但默认关闭、未经真实验证"


def read(rel: str) -> str:
    """读取仓库内文本。"""
    return (ROOT / rel).read_text(encoding="utf-8")


class DocsConsistencyTests(unittest.TestCase):
    """文档不得夸大能力。"""

    def test_limit_50_and_not_open(self) -> None:
        """核心文档写明 50 人上限与真实环境未开放。"""
        for rel in ("README.md", "SKILL.md", "docs/使用与接入.md", "docs/真实验证与能力边界.md"):
            with self.subTest(doc=rel):
                self.assertIn("50", read(rel))
        for rel in ("README.md", "SKILL.md", "MCP_INTEGRATION.md", "CONTEST_DECLARATION.md", "docs/真实验证与能力边界.md"):
            with self.subTest(doc=rel):
                self.assertRegex(read(rel), "真实(环境|领券)[^。\n]{0,12}(未开放|默认关闭)")

    def test_no_overclaim(self) -> None:
        """不含已支持/已验证真实下单、全网最低价承诺。"""
        for rel in DOCS:
            text = read(rel)
            for phrase in FORBIDDEN_CLAIMS:
                with self.subTest(doc=rel, phrase=phrase):
                    self.assertNotIn(phrase, text)
            with self.subTest(doc=rel, phrase="全网最低"):
                for line in text.splitlines():
                    if "全网最低" in line:
                        self.assertRegex(line, "不承诺|不称|不是|不为什么|为什么不叫|不.{0,6}全网最低")

    def test_tool_counts_and_confirm_flow(self) -> None:
        """工具数量与确认式流程的关键要求在文档中。"""
        self.assertIn("12 个只读", read("MCP_INTEGRATION.md"))
        self.assertIn("2 个受控写", read("CONTEST_DECLARATION.md"))
        skill = read("SKILL.md")
        for phrase in ("不得代用户确认", "等待用户明确同意", "不得重试", "摘要过期", "--confirm-digest"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        self.assertNotIn("11 个", read("MCP_INTEGRATION.md").replace("11 个工具", ""))

    def test_new_svgs_valid(self) -> None:
        """新 SVG 是合法 XML 且含 title/desc。"""
        ns = "{http://www.w3.org/2000/svg}"
        for name in NEW_SVGS + ["01-overview.svg", "04-safety-boundary.svg", "05-ranking.svg"]:
            with self.subTest(svg=name):
                root = ET.parse(ROOT / "docs/images" / name).getroot()
                self.assertTrue(root.find(ns + "title") is not None and (root.find(ns + "title").text or "").strip())
                self.assertTrue(root.find(ns + "desc") is not None and (root.find(ns + "desc").text or "").strip())
                self.assertEqual(root.get("role"), "img")
        for name in NEW_SVGS:
            self.assertIn("模拟数据", read("docs/images/" + name))
            self.assertIn("默认关闭", read("docs/images/" + name))
        self.assertIn("四道门", read("docs/images/07-safety-gates.svg"))

    def test_old_svg_wording_updated(self) -> None:
        """01/04 不再写“不下单·不领券”这类与新能力冲突的措辞。"""
        self.assertNotIn("不下单 · 不领券", read("docs/images/01-overview.svg"))
        self.assertIn("逐步确认", read("docs/images/01-overview.svg"))
        self.assertIn("未经确认", read("docs/images/04-safety-boundary.svg"))

    def test_readme_and_skill_reference_new_images(self) -> None:
        """README 与 SKILL 引用新图。"""
        for rel in ("README.md", "SKILL.md"):
            for name in NEW_SVGS:
                with self.subTest(doc=rel, svg=name):
                    self.assertIn("docs/images/" + name, read(rel))

    def test_real_write_default_off_wording(self) -> None:
        """真实写通道文档如实写明默认关闭、未经真实验证与终端检查局限；README 含三档原文。"""
        self.assertIn(TIER3, read("README.md"))
        for rel in ("README.md", "SKILL.md", "MCP_INTEGRATION.md", "CONTEST_DECLARATION.md", "docs/真实写操作启用指南.md"):
            text = read(rel)
            with self.subTest(doc=rel):
                self.assertIn("默认关闭", text)
                self.assertIn("未经真实验证", text)
        for rel in ("README.md", "MCP_INTEGRATION.md", "CONTEST_DECLARATION.md", "docs/真实写操作启用指南.md", "docs/真实验证与能力边界.md"):
            with self.subTest(doc=rel, topic="tty"):
                self.assertIn("无法区分", read(rel))

    def test_four_gates_documented(self) -> None:
        """README、接入说明、指南、SKILL 写四道门。"""
        for rel in ("README.md", "SKILL.md", "MCP_INTEGRATION.md", "docs/真实写操作启用指南.md"):
            with self.subTest(doc=rel):
                self.assertIn("四道门", read(rel))
        self.assertIn("真实写操作启用指南.md", read("README.md"))

    def test_skill_forbids_agent_tty_bypass(self) -> None:
        """SKILL.md 写明 Agent 禁令与用户亲手确认提示。"""
        skill = read("SKILL.md")
        for phrase in ("不得代用户在终端确认", "pty", "不得向用户索要 Token", "MEAL_CLEAR_ENABLE_REAL_WRITE",
                       "real-write.json", "WRITE_LOCK", "order-resolve", "write-lock --off", "请在你自己的终端运行并亲手确认"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        self.assertIn("| 7 |", read("references/data-contract.md"))

    def test_relative_links_valid(self) -> None:
        """文档里的相对链接目标存在。"""
        for rel in DOCS:
            base = (ROOT / rel).parent
            for target in re.findall(r"\]\(([^)\s]+)", read(rel)):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                path = target.split("#")[0]
                if not path:
                    continue
                with self.subTest(doc=rel, target=target):
                    self.assertTrue((base / path).resolve().exists())

    def test_no_local_absolute_paths(self) -> None:
        """仓库文本文件不含本机用户目录路径（断言字面量用拼接构造）。"""
        needle = "/" + "Users/"
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
                continue
            if path.relative_to(ROOT).as_posix() in PATH_LITERAL_EXEMPT:
                continue
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotIn(needle, path.read_text(encoding="utf-8", errors="ignore"))


if __name__ == "__main__":
    unittest.main()
