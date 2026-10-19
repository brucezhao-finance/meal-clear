"""发布包验收：不含验证记录/凭证，含合规必备文件，条目名为UTF-8。"""

import json
from pathlib import Path
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "dist" / "meal-clear.skill"
REQUIRED = [
    "README.md",
    "CONTEST_DECLARATION.md",
    "MCP_INTEGRATION.md",
    "mcp-config.example.json",
    "LICENSE",
    "SKILL.md",
]


@unittest.skipUnless(SKILL.exists(), "源码仓库外无dist包")
class PackagingTests(unittest.TestCase):
    """针对 dist/meal-clear.skill 的结构断言。"""

    def setUp(self) -> None:
        """读取压缩包条目。"""
        with zipfile.ZipFile(SKILL) as archive:
            self.infos = archive.infolist()
        self.names = [i.filename for i in self.infos]

    def test_no_verification_or_private_entries(self) -> None:
        """包内没有验证记录、.git、凭证、本地状态或工作区私有文件。"""
        for name in self.names:
            with self.subTest(name=name):
                self.assertFalse(name.startswith("meal-clear/verification"))
                self.assertFalse(name.startswith("meal-clear/dist"))
                self.assertFalse(name.startswith("meal-clear/.git/"))
                self.assertFalse(name.startswith("meal-clear/.agents"))
                self.assertNotIn("token", name.lower())
                self.assertNotIn("local-state", name)

    def test_no_cache_files(self) -> None:
        """包内没有缓存目录与编译产物。"""
        for name in self.names:
            with self.subTest(name=name):
                self.assertNotIn(".ruff_cache", name)
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.endswith((".pyc", ".pyo")))
                self.assertNotEqual(Path(name).name, ".DS_Store")

    def test_required_files_and_utf8_names(self) -> None:
        """必备文件齐全；中文文件名带UTF-8标志且可还原。"""
        for item in REQUIRED:
            self.assertIn("meal-clear/" + item, self.names)
        self.assertIn("meal-clear/docs/使用与接入.md", self.names)
        for info in self.infos:
            if not info.filename.isascii():
                self.assertTrue(info.flag_bits & 0x800, info.filename)

    def test_manifest_matches_package(self) -> None:
        """manifest 的文件数与哈希和实际包一致。"""
        import hashlib

        manifest = json.loads((ROOT / "dist/package-manifest.json").read_text())
        self.assertEqual(manifest["file_count"], len(self.names))
        self.assertEqual(manifest["bytes"], SKILL.stat().st_size)
        self.assertEqual(
            manifest["sha256"], hashlib.sha256(SKILL.read_bytes()).hexdigest()
        )
        self.assertIn("verification", manifest["excluded"])


@unittest.skipUnless(SKILL.exists(), "源码仓库外无dist包")
class ReadmeAssetTests(unittest.TestCase):
    """README 引用的图片必须随包提供。"""

    def test_readme_assets_in_package(self) -> None:
        """包内 README 的相对图片/链接目标存在，5 张 SVG 均在且小于100KB。"""
        import re
        with zipfile.ZipFile(SKILL) as archive:
            names = set(archive.namelist())
            readme = archive.read("meal-clear/README.md").decode("utf-8")
            infos = {i.filename: i for i in archive.infolist()}
        targets = re.findall(r"\]\(([^)#\s]+)", readme)
        for target in targets:
            if target.startswith(("http://", "https://")):
                continue
            with self.subTest(target=target):
                self.assertIn("meal-clear/" + target, names)
        self.assertNotIn("assets/", readme)
        for svg in (
            "01-overview.svg",
            "02-terminal-demo.svg",
            "03-change-one-line.svg",
            "04-safety-boundary.svg",
            "05-ranking.svg",
        ):
            key = "meal-clear/docs/images/" + svg
            with self.subTest(svg=svg):
                self.assertIn(key, names)
                self.assertLess(infos[key].file_size, 100 * 1024)


@unittest.skipUnless(SKILL.exists(), "源码仓库外无dist包")
class PackageLinkTests(unittest.TestCase):
    """包内文档链接、缓存目录与验证记录断言。"""

    def test_markdown_relative_links_valid(self) -> None:
        """包内所有 .md 的相对链接目标都在包内。"""
        import posixpath
        import re

        with zipfile.ZipFile(SKILL) as archive:
            names = set(archive.namelist())
            docs = [n for n in names if n.endswith(".md")]
            for doc in docs:
                text = archive.read(doc).decode("utf-8")
                for target in re.findall(r"\]\(([^)\s]+)", text):
                    if target.startswith(("http://", "https://", "#", "mailto:")):
                        continue
                    path = target.split("#")[0]
                    if not path:
                        continue
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(doc), path)
                    )
                    with self.subTest(doc=doc, target=target):
                        self.assertIn(resolved, names)

    def test_no_ruff_cache_or_verification(self) -> None:
        """包内没有 .ruff_cache 与 verification 条目。"""
        with zipfile.ZipFile(SKILL) as archive:
            for name in archive.namelist():
                self.assertNotIn(".ruff_cache", name)
                self.assertNotIn("/verification/", name)

    def test_v2_examples_in_package(self) -> None:
        """外送与得来速示例随包提供，且能通过 v2 校验。"""
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        from meal_clear.request_v2 import validate_request_v2

        with zipfile.ZipFile(SKILL) as archive:
            for name in ("request-delivery.json", "request-drive-thru.json"):
                raw = archive.read("meal-clear/examples/" + name).decode("utf-8")
                self.assertNotIn("/Users/", raw)
                req = validate_request_v2(json.loads(raw))
                self.assertIn(req["fulfillment"]["type"], ("delivery", "drive_thru"))
