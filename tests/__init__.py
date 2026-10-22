"""使 `python3 -m unittest tests.test_xxx` 也能导入同目录的夹具模块（discover -s tests 不受影响）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
