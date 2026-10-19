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
