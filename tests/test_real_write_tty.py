"""FEAT-001：TTY 与确认短语（假 TTY 注入）、批准单次使用、authorize 预检。"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from meal_clear import config, realwrite  # noqa: E402
from real_write_support import CLAIM, ORDER, T0, FakeTTY, State, digest_of  # noqa: E402

D = digest_of(0xABCDEF123456)  # 末 6 位 123456


def streams(stdin, stdout):
    return patch.object(realwrite, "_io_streams", return_value=(stdin, stdout))


def switch_on():
    return patch.dict(os.environ, {config.REAL_WRITE_ENV: "1"})


class TtyTests(unittest.TestCase):
    def test_non_tty_stdin_refused(self):
        with streams(FakeTTY("123456 确认下单\n", tty=False), FakeTTY()):
            with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                realwrite.require_tty()
        self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("not_tty", 4))

    def test_non_tty_stdout_refused(self):
        with streams(FakeTTY(), FakeTTY(tty=False)), self.assertRaises(realwrite.RealWriteDenied) as ctx:
            realwrite.require_tty()
        self.assertEqual(ctx.exception.reason, "not_tty")

    def test_stream_without_isatty_refused(self):
        with streams(object(), object()), self.assertRaises(realwrite.RealWriteDenied):
            realwrite.require_tty()

    def test_piped_correct_phrase_still_refused(self):
        out = FakeTTY()
        with streams(FakeTTY("123456 确认下单\n", tty=False), out), self.assertRaises(realwrite.RealWriteDenied) as ctx:
            realwrite.human_confirm(["x"], realwrite.expected_phrase(ORDER, D))
        self.assertEqual(ctx.exception.reason, "not_tty")
        self.assertEqual(out.getvalue(), "")

    def test_expected_phrase_binds_tool_and_digest_tail(self):
        self.assertEqual(realwrite.expected_phrase(ORDER, D), "123456 确认下单")
        self.assertEqual(realwrite.expected_phrase(CLAIM, D), "123456 确认领券")
        with self.assertRaises(realwrite.RealWriteDenied):
            realwrite.expected_phrase("cancel-x", D)

    def test_wrong_phrase_refused_no_retry(self):
        good = "123456 确认下单"
        cases = ("", "\n", good + " ", " " + good, "123456  确认下单", "123456 确认领券", "654321 确认下单",
                 "123456 确认下单 \n", "确认下单", good + "x")
        for typed in cases:
            with self.subTest(typed=typed):
                stdin = FakeTTY(typed if typed.endswith("\n") else typed + "\n" + good + "\n")
                with streams(stdin, FakeTTY()), self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.human_confirm(["摘要"], good)
                self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("phrase_mismatch", 4))

    def test_phrase_accepts_exact_and_line_endings(self):
        good = "123456 确认下单"
        for typed in (good + "\n", good + "\r\n", good):
            with self.subTest(typed=typed), streams(FakeTTY(typed), FakeTTY()):
                realwrite.human_confirm(["摘要"], good)

    def test_prompt_shows_lines_without_secrets(self):
        out = FakeTTY()
        with streams(FakeTTY("123456 确认下单\n"), out):
            realwrite.human_confirm(["门店 X", "待支付，不会自动付款"], "123456 确认下单")
        text = out.getvalue()
        self.assertIn("门店 X", text)
        self.assertIn("123456 确认下单", text)
        self.assertNotIn("SIM_TOKEN", text)


class ApprovalTests(unittest.TestCase):
    def session(self, st, tool=ORDER, n=0xABCDEF123456):
        record = st.record(n)
        with switch_on(), streams(FakeTTY(), FakeTTY()):
            return realwrite.authorize(st.ledger, tool, record, T0)

    def approve(self, session, text):
        with streams(FakeTTY(text), FakeTTY()):
            return session.confirm(["摘要"])

    def test_confirm_issues_single_use_bound_approval(self):
        with State() as st:
            sess = self.session(st)
            ap = self.approve(sess, "123456 确认下单\n")
            self.assertTrue(realwrite.approval_valid(ap, ORDER, D))
            self.assertFalse(realwrite.approval_valid(ap, CLAIM, D))
            self.assertFalse(realwrite.approval_valid(ap, ORDER, digest_of(1)))
            self.assertTrue(realwrite.consume_approval(ap, ORDER, D))
            self.assertFalse(realwrite.consume_approval(ap, ORDER, D))
            self.assertFalse(realwrite.approval_valid(ap, ORDER, D))

    def test_phrase_binds_to_digest_and_tool(self):
        with State() as st:
            sess = self.session(st)
            with self.assertRaises(realwrite.RealWriteDenied):
                self.approve(sess, "123456 确认领券\n")

    def test_approval_not_forgeable(self):
        with self.assertRaises(realwrite.RealWriteDenied):
            realwrite.RealWriteApproval(ORDER, D, object())
        for fake in (None, object(), "ok", realwrite._SENTINEL):
            self.assertFalse(realwrite.approval_valid(fake, ORDER, D))

    def test_claim_session_uses_claim_phrase(self):
        with State() as st:
            sess = self.session(st, CLAIM, 0xABCDEF123456)
            self.assertTrue(realwrite.approval_valid(self.approve(sess, "123456 确认领券\n"), CLAIM, D))


class AuthorizeTests(unittest.TestCase):
    def test_off_then_lock_then_tty_then_unknown_order(self):
        with State() as st:
            record = st.record(1, "A")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(config.REAL_WRITE_ENV, None)
                with streams(FakeTTY(), FakeTTY()), self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.authorize(st.ledger, ORDER, record, T0)
                self.assertEqual(ctx.exception.reason, "switch_off")
            with switch_on():
                with streams(FakeTTY(tty=False), FakeTTY()), self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.authorize(st.ledger, ORDER, record, T0)
                self.assertEqual(ctx.exception.reason, "not_tty")
                st.official_write(2, end="unknown", basket="A")
                from datetime import datetime, timezone
                with streams(FakeTTY(), FakeTTY()), self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.authorize(st.ledger, ORDER, record, datetime.now(timezone.utc))
                self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("unknown_lock", config.EXIT_LOCKED))

    def test_unknown_tool_refused(self):
        with State() as st, switch_on():
            record = st.record(1)
            for tool in ("cancel-order", None, 5, ""):
                with self.subTest(tool=tool), self.assertRaises(realwrite.RealWriteDenied):
                    realwrite.authorize(st.ledger, tool, record, T0)

    def test_session_limits_delegate(self):
        with State() as st, switch_on():
            record = st.record(1)
            with streams(FakeTTY(), FakeTTY()):
                sess = realwrite.authorize(st.ledger, ORDER, record, T0)
            sess.check_amount(10000)
            with self.assertRaises(realwrite.RealWriteDenied):
                sess.check_amount(10001)
            sess.check_counts()


if __name__ == "__main__":
    unittest.main()
