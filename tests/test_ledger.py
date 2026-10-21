"""T-E1 / T-E2（前半）：账本权限、拒绝规则、原子写、并发与崩溃恢复。"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.ledger import (  # noqa: E402
    AttemptId, Ledger, LedgerCorrupt, LedgerError, LedgerStateError,
)

D1 = hashlib.sha256(b"one").hexdigest()
D2 = hashlib.sha256(b"two").hexdigest()


def mk(ledger: Ledger, digest: str = D1, **kw) -> dict:
    args = dict(basket_key="bk", created_at="2099-01-01T00:00:00+00:00",
                expires_at="2099-01-01T00:10:00+00:00")
    args.update(kw)
    return ledger.create(digest, **args)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "local-state" / "orders"
        self.ledger = Ledger(self.root)
        self.addCleanup(self.ledger.release_all)

    # --- T-E1 ---
    def test_permissions(self) -> None:
        mk(self.ledger)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / (D1 + ".json")).st_mode), 0o600)
        self.ledger.begin_step(D1, "claim", ("none",))
        self.assertEqual(stat.S_IMODE(os.stat(self.root / (D1 + ".json")).st_mode), 0o600)
        for p in self.root.iterdir():
            self.assertFalse(stat.S_IMODE(p.stat().st_mode) & 0o077, p.name)
        self.assertEqual([p.name for p in self.root.glob("*.tmp")], [])

    def test_loose_parent_and_root_tightened(self) -> None:
        base = self.base / "other"
        parent = base / "local-state"
        parent.mkdir(parents=True)
        os.chmod(parent, 0o755)
        (parent / "orders").mkdir(mode=0o755)
        os.chmod(parent / "orders", 0o755)
        led = Ledger(parent / "orders")
        self.assertTrue(led.tightened_parent)
        self.assertEqual(stat.S_IMODE(os.stat(parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(parent / "orders").st_mode), 0o700)

    def test_symlink_root_rejected(self) -> None:
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real)
        with self.assertRaises(LedgerError):
            Ledger(link)

    def test_symlink_record_rejected(self) -> None:
        outside = self.base / "outside.json"
        outside.write_text("{}")
        (self.root / (D1 + ".json")).symlink_to(outside)
        with self.assertRaises(LedgerError):
            self.ledger.read(D1)
        with self.assertRaises(LedgerError):
            mk(self.ledger)
        self.assertEqual(outside.read_text(), "{}")

    def test_corrupt_rejected_not_overwritten(self) -> None:
        path = self.root / (D1 + ".json")
        for content in (b"{not json", b"[]", b'{"schema": "x"}', b"\xff\xfe"):
            with self.subTest(content=content):
                path.write_bytes(content)
                os.chmod(path, 0o600)
                with self.assertRaises(LedgerCorrupt):
                    self.ledger.read(D1)
                with self.assertRaises(LedgerCorrupt):
                    self.ledger.begin_step(D1, "claim", ("none",))
                self.assertEqual(path.read_bytes(), content)

    def test_oversize_rejected(self) -> None:
        path = self.root / (D1 + ".json")
        path.write_bytes(b" " * (1_000_000 + 1))
        os.chmod(path, 0o600)
        with self.assertRaises(LedgerCorrupt):
            self.ledger.read(D1)
        path.unlink()
        with self.assertRaises(LedgerError):
            mk(self.ledger, summary={"x": "y" * 1_000_001})
        self.assertFalse(path.exists())

    def test_invalid_digests(self) -> None:
        for bad in ("../x", "../" + D1, "abc123", "g" * 64, D1.upper(), D1[:11], D1 + "0", "", None, "a/" + D1[2:]):
            with self.subTest(bad=bad):
                with self.assertRaises(LedgerError):
                    self.ledger.read(bad)
                with self.assertRaises(LedgerError):
                    self.ledger.begin_step(bad, "claim", ("none",))
                with self.assertRaises(LedgerError):
                    mk(self.ledger, bad)
        for bad in ("../x", "abc", "g" * 12, D1[:11], D1 + "0"):
            with self.subTest(prefix=bad), self.assertRaises(LedgerError):
                self.ledger.resolve(bad)
        self.assertEqual(sorted(p.name for p in self.base.rglob("*") if p.is_file()), [])

    def test_atomic_write_failure_leaves_no_partial(self) -> None:
        mk(self.ledger)
        before = (self.root / (D1 + ".json")).read_bytes()
        with patch("meal_clear.ledger.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.ledger.begin_step(D1, "claim", ("none",))
        self.assertEqual((self.root / (D1 + ".json")).read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertEqual(self.ledger.read(D1)["steps"]["claim"]["state"], "none")
        # 失败后仍可重新开始（owner 锁已释放）
        self.ledger.begin_step(D1, "claim", ("none",))

    def test_create_does_not_overwrite(self) -> None:
        mk(self.ledger)
        with self.assertRaises(LedgerError):
            mk(self.ledger, summary={"a": 1})
        self.assertEqual(self.ledger.read(D1)["summary"], {})

    def test_state_machine_and_detail_whitelist(self) -> None:
        mk(self.ledger, claim_required=False)
        att = self.ledger.begin_step(D1, "order", ("none",), {"claim": ("succeeded", "not_required")})
        self.assertIsInstance(att, AttemptId)
        self.assertTrue(self.ledger.is_pending(att))
        with self.assertRaises(LedgerStateError):
            self.ledger.begin_step(D1, "order", ("none",))
        with self.assertRaises(LedgerError):
            self.ledger.finish_step(D1, "order", att, "created", {"url": "http://x/y?z=1"})
        with self.assertRaises(LedgerError):
            self.ledger.finish_step(D1, "claim", att, "succeeded", order_id="O1")
        with self.assertRaises(LedgerStateError):
            self.ledger.finish_step(D1, "order", "wrong", "created")
        rec = self.ledger.finish_step(D1, "order", att, "created", {"code": 0}, order_id="O-1")
        self.assertEqual(rec["steps"]["order"]["state"], "created")
        self.assertEqual(rec["steps"]["order"]["order_id"], "O-1")
        self.assertFalse(self.ledger.is_pending(att))
        self.assertNotIn("O-1", json.dumps(rec["events"]) + json.dumps(rec["summary"]))

    def test_precondition_blocks(self) -> None:
        mk(self.ledger)
        with self.assertRaises(LedgerStateError):
            self.ledger.begin_step(D1, "order", ("none",), {"claim": ("succeeded", "not_required")})
        self.assertEqual(self.ledger.read(D1)["steps"]["order"]["state"], "none")

    def test_resolve_and_basket(self) -> None:
        mk(self.ledger)
        mk(self.ledger, D2, basket_key="other")
        self.assertEqual(self.ledger.resolve(D1[:12]), D1)
        with self.assertRaises(LedgerError):
            self.ledger.resolve("0" * 12)
        from datetime import datetime, timezone
        now = datetime(2099, 1, 1, 12, tzinfo=timezone.utc)
        self.assertEqual([r["digest"] for r in self.ledger.find_by_basket("bk", 24 * 3600, now)], [D1])
        self.assertEqual(self.ledger.find_by_basket("bk", 60, now), [])

    # --- T-E2 前半 ---
    def test_concurrent_begin_step_exactly_one(self) -> None:
        mk(self.ledger)
        n = 8
        barrier = threading.Barrier(n)
        results = []

        def worker() -> None:
            led = Ledger(self.root)
            barrier.wait()
            try:
                att = led.begin_step(D1, "claim", ("none",))
                results.append(("ok", att, led))
            except LedgerStateError:
                results.append(("denied", None, led))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(results), n)
        self.assertEqual(len(oks), 1)
        for _, _, led in results:
            led.release_all()

    # --- 崩溃恢复 ---
    def test_crashed_pending_becomes_unknown(self) -> None:
        mk(self.ledger)
        code = (
            "import os,sys\nsys.path.insert(0, %r)\n"
            "from meal_clear.ledger import Ledger\n"
            "l=Ledger(%r)\nl.begin_step(%r,'order',('none',))\nos._exit(0)\n"
        ) % (str(ROOT / "scripts"), str(self.root), D1)
        subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
        rec = self.ledger.read(D1)
        step = rec["steps"]["order"]
        self.assertEqual(step["state"], "unknown")
        self.assertTrue(step["detail"]["recovered_from_pending"])
        with self.assertRaises(LedgerStateError):
            self.ledger.begin_step(D1, "order", ("none",))

    def test_live_pending_not_recovered(self) -> None:
        """另一个仍持有 owner 锁的实例在调用中：保持 pending，不误判为崩溃。"""
        mk(self.ledger)
        att = self.ledger.begin_step(D1, "order", ("none",))
        other = Ledger(self.root)
        self.addCleanup(other.release_all)
        self.assertEqual(other.read(D1)["steps"]["order"]["state"], "pending")
        self.assertEqual(self.ledger.read(D1)["steps"]["order"]["state"], "pending")
        self.ledger.release_all()  # 模拟持有者退出
        self.assertEqual(other.read(D1)["steps"]["order"]["state"], "unknown")
        with self.assertRaises(LedgerStateError):
            self.ledger.finish_step(D1, "order", att, "created")


if __name__ == "__main__":
    unittest.main()
