"""真实写基座测试夹具：临时 local-state、合成账本记录、假 TTY（全部合成数据，零网络）。"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from meal_clear.config import CHANNEL_OFFICIAL, TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER  # noqa: E402
from meal_clear.ledger import Ledger  # noqa: E402

T0 = datetime(2026, 10, 20, 3, 0, 0, tzinfo=timezone.utc)
CLAIM, ORDER = TOOL_AUTO_BIND_COUPONS, TOOL_CREATE_ORDER


def digest_of(n):
    return ("%064x" % n)


class FakeTTY(io.StringIO):
    """可注入 isatty 结果的假终端。"""

    def __init__(self, text="", tty=True):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class State:
    """with State() as st: st.dir 是 0700 的 local-state，st.ledger 在其 orders/ 下。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "local-state"
        self.dir.mkdir(mode=0o700)
        os.chmod(self.dir, 0o700)
        self.ledger = Ledger(self.dir / "orders")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def write_config(self, text, mode=0o600):
        path = self.dir / "real-write.json"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def record(self, n, basket="b1"):
        digest = digest_of(n)
        return self.ledger.create(
            digest, basket_key=basket, created_at=T0.isoformat(),
            expires_at=(T0 + timedelta(minutes=10)).isoformat(),
        )

    def official_write(self, n, step="order", end=None, basket="b1"):
        """创建记录并以 official 通道开始一次写；end 为终态（None 保持 pending）。"""
        self.record(n, basket)
        digest = digest_of(n)
        attempt = self.ledger.begin_step(digest, step, ("none",), channel=CHANNEL_OFFICIAL)
        if end:
            self.ledger.finish_step(digest, step, attempt, end)
        return digest
