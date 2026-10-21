"""FEAT-001：账本 channel、24h 窗口、跨进程计数、全局锁、unknown 篮子锁、人工标注。"""
import json
import subprocess
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from meal_clear import config, realwrite  # noqa: E402
from meal_clear.ledger import Ledger, LedgerError, LedgerStateError  # noqa: E402
from real_write_support import CLAIM, ORDER, T0, State, digest_of  # noqa: E402

OFFICIAL, LOOP = config.CHANNEL_OFFICIAL, config.CHANNEL_LOOPBACK
DAY = config.REAL_WINDOW_SECONDS
ORDER_STATES = config.REAL_COUNTED_STATES["order"]


def count(st, step="order", now=None):
    return st.ledger.count_channel_writes(OFFICIAL, step, config.REAL_COUNTED_STATES[step], DAY, now or _now())


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


class ChannelTests(unittest.TestCase):
    def test_default_loopback_and_not_counted(self):
        with State() as st:
            st.record(1)
            st.ledger.begin_step(digest_of(1), "order", ("none",))
            entry = st.ledger.read(digest_of(1))["steps"]["order"]
            self.assertEqual(entry["channel"], LOOP)
            self.assertEqual(count(st), 0)

    def test_official_channel_recorded_and_counted(self):
        with State() as st:
            st.official_write(1, "order")
            self.assertEqual(st.ledger.read(digest_of(1))["steps"]["order"]["channel"], OFFICIAL)
            self.assertEqual(count(st), 1)

    def test_old_record_without_channel_reads_as_loopback(self):
        with State() as st:
            st.record(1)
            path = st.ledger.root / (digest_of(1) + ".json")
            data = json.loads(path.read_text())
            for step in ("claim", "order"):
                data["steps"][step].pop("channel", None)
            path.write_text(json.dumps(data))
            record = st.ledger.read(digest_of(1))
            self.assertEqual(st.ledger.step_channel(record["steps"]["order"]), LOOP)
            self.assertEqual(count(st), 0)

    def test_bad_channel_rejected(self):
        with State() as st:
            st.record(1)
            with self.assertRaises(LedgerError):
                st.ledger.begin_step(digest_of(1), "order", ("none",), channel="evil")

    def test_failed_not_counted_others_counted(self):
        with State() as st:
            st.official_write(1, end="failed")
            st.official_write(2, end="created")
            st.official_write(3, end="unknown")
            st.official_write(4, end=None)
            self.assertEqual(count(st), 3)

    def test_claim_and_order_counted_separately(self):
        with State() as st:
            st.official_write(1, "claim", end="succeeded")
            st.official_write(2, "order", end="created")
            self.assertEqual((count(st, "claim"), count(st, "order")), (1, 1))


class WindowTests(unittest.TestCase):
    def test_window_rolls_after_24h(self):
        with State() as st:
            st.official_write(1, end="created")
            started = st.ledger.read(digest_of(1))["steps"]["order"]["started_at"]
            from datetime import datetime
            t = datetime.fromisoformat(started)
            self.assertEqual(count(st, now=t + timedelta(hours=23, minutes=59)), 1)
            self.assertEqual(count(st, now=t + timedelta(seconds=DAY)), 1)  # 恰好边界仍计入
            self.assertEqual(count(st, now=t + timedelta(seconds=DAY + 1)), 0)

    def test_check_counts_caps_and_rolls(self):
        with State() as st:
            cfg = dict(realwrite.default_config(), max_orders_24h=2)
            st.official_write(1, end="created")
            realwrite.check_counts(st.ledger, cfg, ORDER, _now())
            st.official_write(2, end="unknown")
            with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                realwrite.check_counts(st.ledger, cfg, ORDER, _now())
            self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("orders_cap", config.EXIT_LIMIT))
            realwrite.check_counts(st.ledger, cfg, ORDER, _now() + timedelta(days=2))  # 窗口滚出
            realwrite.check_counts(st.ledger, cfg, CLAIM, _now())  # 领券独立计数

    def test_claims_cap_default_one(self):
        with State() as st:
            cfg = realwrite.default_config()
            st.official_write(1, "claim", end="succeeded")
            with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                realwrite.check_counts(st.ledger, cfg, CLAIM, _now())
            self.assertEqual(ctx.exception.reason, "claims_cap")


class CrossProcessTests(unittest.TestCase):
    def test_count_sees_other_process_writes(self):
        with State() as st:
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from meal_clear.ledger import Ledger\n"
                "from datetime import datetime, timedelta, timezone\n"
                "led = Ledger(%r)\n"
                "now = datetime.now(timezone.utc)\n"
                "for n in (1, 2):\n"
                "    d = '%%064x' %% n\n"
                "    led.create(d, basket_key='b', created_at=now.isoformat(), expires_at=(now+timedelta(minutes=9)).isoformat())\n"
                "    a = led.begin_step(d, 'order', ('none',), channel='official')\n"
                "    led.finish_step(d, 'order', a, 'created')\n"
            ) % (str(Path(__file__).resolve().parents[1] / "scripts"), str(st.ledger.root))
            subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
            self.assertEqual(count(st), 2)
            cfg = dict(realwrite.default_config(), max_orders_24h=2)
            with self.assertRaises(realwrite.RealWriteDenied):
                realwrite.check_counts(st.ledger, cfg, ORDER, _now())

    def test_concurrent_begin_respects_cap(self):
        with State() as st:
            for n in (1, 2, 3, 4):
                st.record(n)
            cfg = dict(realwrite.default_config(), max_orders_24h=1)
            results = []

            def worker(n):
                led = Ledger(st.ledger.root)  # 独立实例，模拟独立进程
                try:
                    with led.global_locked():
                        realwrite.check_counts(led, cfg, ORDER, _now())
                        led.begin_step(digest_of(n), "order", ("none",), channel=OFFICIAL)
                    results.append("ok")
                except realwrite.RealWriteDenied:
                    results.append("denied")

            threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2, 3, 4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(sorted(results), ["denied", "denied", "denied", "ok"])
            self.assertEqual(count(st), 1)

    def test_global_lock_file_created_0600(self):
        with State() as st:
            with st.ledger.global_locked():
                pass
            path = st.ledger.root / ".global.lock"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with st.ledger.global_locked():  # 可重复获取（非嵌套）
                with st.ledger.locked(digest_of(1)):  # 顺序：全局锁 → digest 锁
                    pass


class UnknownLockTests(unittest.TestCase):
    def test_finds_unknown_and_pending_same_basket_only(self):
        with State() as st:
            st.official_write(1, end="unknown", basket="A")
            st.official_write(2, end=None, basket="B")
            st.official_write(3, end="created", basket="A")
            self.assertEqual([r["digest"] for r in st.ledger.find_official_unresolved("A", DAY, _now())], [digest_of(1)])
            self.assertEqual(len(st.ledger.find_official_unresolved("B", DAY, _now())), 1)
            self.assertEqual(st.ledger.find_official_unresolved("C", DAY, _now()), [])
            self.assertEqual(st.ledger.find_official_unresolved("A", DAY, _now() + timedelta(days=2)), [])

    def test_loopback_unknown_does_not_lock(self):
        with State() as st:
            st.record(1, "A")
            a = st.ledger.begin_step(digest_of(1), "order", ("none",))
            st.ledger.finish_step(digest_of(1), "order", a, "unknown")
            self.assertEqual(st.ledger.find_official_unresolved("A", DAY, _now()), [])


class ResolveTests(unittest.TestCase):
    def test_not_created_unlocks_and_logs_event(self):
        with State() as st:
            d = st.official_write(1, end="unknown", basket="A")
            rec = st.ledger.resolve_unknown(d, "order", "not-created")
            self.assertEqual(rec["steps"]["order"]["state"], "failed")
            self.assertIsNone(rec["steps"]["order"].get("order_id"))
            events = [e for e in rec["events"] if e["event"] == "manual_resolved"]
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0]["step"], events[0]["outcome"]), ("order", "not-created"))
            self.assertEqual(st.ledger.find_official_unresolved("A", DAY, _now()), [])
            self.assertEqual(count(st), 0)

    def test_created_counts_and_keeps_no_order_id(self):
        with State() as st:
            d = st.official_write(1, end="unknown")
            rec = st.ledger.resolve_unknown(d, "order", "created")
            self.assertEqual(rec["steps"]["order"]["state"], "created")
            self.assertIsNone(rec["steps"]["order"].get("order_id"))
            self.assertEqual(count(st), 1)

    def test_claim_choices(self):
        with State() as st:
            d = st.official_write(1, "claim", end="unknown")
            self.assertEqual(st.ledger.resolve_unknown(d, "claim", "claimed")["steps"]["claim"]["state"], "succeeded")
            d = st.official_write(2, "claim", end="unknown")
            self.assertEqual(st.ledger.resolve_unknown(d, "claim", "not-claimed")["steps"]["claim"]["state"], "failed")

    def test_rejects_non_unknown_loopback_and_bad_choice(self):
        with State() as st:
            d = st.official_write(1, end="created")
            with self.assertRaises(LedgerStateError):
                st.ledger.resolve_unknown(d, "order", "not-created")
            st.record(2)
            a = st.ledger.begin_step(digest_of(2), "order", ("none",))
            st.ledger.finish_step(digest_of(2), "order", a, "unknown")
            with self.assertRaises(LedgerStateError):
                st.ledger.resolve_unknown(digest_of(2), "order", "not-created")
            u = st.official_write(3, end="unknown")
            for step, outcome in (("order", "claimed"), ("claim", "created"), ("order", "x"), ("bad", "created")):
                with self.subTest(step=step, outcome=outcome), self.assertRaises(LedgerError):
                    st.ledger.resolve_unknown(u, step, outcome)
            self.assertEqual(st.ledger.read(u)["steps"]["order"]["state"], "unknown")

    def test_ledger_never_stores_token_or_args(self):
        with State() as st:
            d = st.official_write(1, end="unknown")
            st.ledger.resolve_unknown(d, "order", "not-created")
            text = (st.ledger.root / (d + ".json")).read_text()
            for needle in ("SIM_TOKEN", "Authorization", "arguments", "Bearer"):
                self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
