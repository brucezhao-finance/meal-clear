"""可复现地生成 dist/meal-clear.skill 与 dist/package-manifest.json（仅标准库）。

用法：python3 scripts/build_skill.py
打包内容为 Skill 运行所需文件；验证记录(verification/)、本地状态、凭证、
缓存与工作区私有目录均被排除。zip 条目名使用 UTF-8（flag 0x800）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
import zipfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "meal-clear"
FIXED_TIME = (2026, 10, 19, 0, 0, 0)
EXCLUDED_DIRS = {
    "verification",
    "dist",
    ".git",
    ".agents",
    "local-state",
    "live-private",
    "__pycache__",
    ".pytest_cache",
    ".venv",
}
EXCLUDED_NAMES = {".env", ".contest-plan.md"}
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}"),
    re.compile("/" + "Users/[A-Za-z0-9_.-]+/"),
]


def excluded(path: Path) -> bool:
    """判断相对路径是否不得进入发布包。"""
    parts = path.relative_to(ROOT).parts
    name = path.name
    return (
        any(part in EXCLUDED_DIRS for part in parts)
        or name in EXCLUDED_NAMES
        or "token" in name.lower()
        or name.endswith(".pyc")
    )


def collect() -> List[Path]:
    """按字典序收集待打包文件，保证条目顺序固定。"""
    files = [p for p in ROOT.rglob("*") if p.is_file() and not excluded(p)]
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def scan(files: List[Path]) -> bool:
    """对打包文件做基础敏感串扫描；二进制文件跳过。"""
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            return False
    return True


def main() -> int:
    """生成压缩包与清单。"""
    files = collect()
    passed = scan(files)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"{PACKAGE}.skill"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            name = f"{PACKAGE}/{file.relative_to(ROOT).as_posix()}"
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.flag_bits |= 0x800
            archive.writestr(info, file.read_bytes())
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    manifest = {
        "package": target.name,
        "file_count": len(files),
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "secret_scan": "passed" if passed else "failed",
        "excluded": [
            "verification",
            "dist",
            "local-state",
            "Token/凭证",
            "缓存",
            "工作区私有目录(.agents)",
        ],
        "offline_behavior_tests": suite.countTestCases(),
        "note": "bytes 与 sha256 针对 .skill 文件本身；验证记录见源码仓库 verification/，不随包发布。",
    }
    (dist / "package-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
