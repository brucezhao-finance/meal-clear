"""FEAT-001：开关、配置文件、紧急锁、金额上限、无绕过入口、官方域名严格校验。"""
import ast
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from meal_clear import config, gate, net_guard, realwrite  # noqa: E402
from meal_clear.mcp import ENDPOINT  # noqa: E402
from real_write_support import State  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "scripts" / "meal_clear"


def env(value):
    clean = {k: v for k, v in os.environ.items() if k != config.REAL_WRITE_ENV}
    if value is not None:
        clean[config.REAL_WRITE_ENV] = value
    return patch.dict(os.environ, clean, clear=True)


class SwitchTests(unittest.TestCase):
    def test_default_off_message_is_verbatim(self):
        with State() as st, env(None):
            with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                realwrite.ensure_enabled(st.dir)
            self.assertEqual(str(ctx.exception), gate.MSG_NOT_OPEN)
            self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("switch_off", 4))
            self.assertIsInstance(ctx.exception, gate.WriteDenied)
            self.assertFalse(realwrite.real_write_enabled(st.dir))

    def test_env_only_exact_one_enables(self):
        with State() as st:
            for value, expected in (("1", True), ("true", False), ("0", False), ("", False), (" 1", False), ("1 ", False)):
                with self.subTest(value=value), env(value):
                    self.assertEqual(realwrite.real_write_enabled(st.dir), expected)

    def test_config_file_enable_and_limits_roundtrip(self):
        with State() as st, env(None):
            st.write_config(json.dumps({"enabled": True, "max_order_cents": 3000, "max_orders_24h": 2, "max_claims_24h": 1}))
            cfg = realwrite.load_config(st.dir)
            self.assertEqual(cfg, {"enabled": True, "max_order_cents": 3000, "max_orders_24h": 2, "max_claims_24h": 1})
            self.assertTrue(realwrite.real_write_enabled(st.dir))
            self.assertEqual(realwrite.ensure_enabled(st.dir)["max_order_cents"], 3000)

    def test_missing_file_gives_defaults(self):
        with State() as st:
            self.assertEqual(realwrite.load_config(st.dir),
                             {"enabled": False, "max_order_cents": 10000, "max_orders_24h": 3, "max_claims_24h": 1})

    def test_config_rejects_bad_files(self):
        def bad(label, setup):
            with self.subTest(label), State() as st, env(None):
                setup(st)
                with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.load_config(st.dir)
                self.assertEqual(ctx.exception.reason, "config_invalid")
                self.assertNotIn(str(st.dir), str(ctx.exception))
                with self.assertRaises(realwrite.RealWriteDenied):
                    realwrite.real_write_enabled(st.dir)  # env 即使为 1 也不放行无效配置

        bad("bad_mode", lambda st: st.write_config('{"enabled": true}', 0o644))
        bad("group_write", lambda st: st.write_config('{"enabled": true}', 0o660))
        bad("bad_json", lambda st: st.write_config("{nope"))
        bad("not_object", lambda st: st.write_config("[1]"))
        bad("unknown_key", lambda st: st.write_config('{"enabled": true, "yolo": 1}'))
        bad("enabled_not_bool", lambda st: st.write_config('{"enabled": "yes"}'))
        bad("oversize", lambda st: st.write_config('{"enabled": true, "pad": "%s"}' % ("x" * 70000)))

        def symlink(st):
            target = st.dir / "other.json"
            target.write_text('{"enabled": true}')
            os.chmod(target, 0o600)
            os.symlink(target, st.dir / config.REAL_WRITE_CONFIG_NAME)
        bad("symlink", symlink)

        def bad_dir(st):
            st.write_config('{"enabled": true}')
            os.chmod(st.dir, 0o755)
        bad("bad_dir_mode", bad_dir)

        def directory(st):
            (st.dir / config.REAL_WRITE_CONFIG_NAME).mkdir()
        bad("is_directory", directory)

    def test_limits_above_hard_cap_rejected_not_clamped(self):
        for key, (_default, hard) in config.REAL_LIMITS.items():
            with self.subTest(key=key), State() as st:
                st.write_config(json.dumps({key: hard}))
                self.assertEqual(realwrite.load_config(st.dir)[key], hard)
                st.write_config(json.dumps({key: hard + 1}))
                with self.assertRaises(realwrite.RealWriteDenied):
                    realwrite.load_config(st.dir)

    def test_limits_non_int_or_nonpositive_rejected(self):
        for key in config.REAL_LIMITS:
            for value in (0, -1, 1.5, "3", True, None, [1]):
                with self.subTest(key=key, value=value), State() as st:
                    st.write_config(json.dumps({key: value}))
                    with self.assertRaises(realwrite.RealWriteDenied):
                        realwrite.load_config(st.dir)


class LockTests(unittest.TestCase):
    def test_lock_roundtrip_and_any_form_counts(self):
        with State() as st:
            self.assertFalse(realwrite.is_locked(st.dir))
            realwrite.set_lock(st.dir)
            realwrite.set_lock(st.dir)  # 幂等
            self.assertTrue(realwrite.is_locked(st.dir))
            self.assertTrue(realwrite.clear_lock(st.dir))
            self.assertFalse(realwrite.clear_lock(st.dir))
            os.symlink("/nonexistent-target", realwrite.lock_path(st.dir))
            self.assertTrue(realwrite.is_locked(st.dir))  # 悬空符号链接也算锁定
            realwrite.clear_lock(st.dir)
            realwrite.lock_path(st.dir).mkdir()
            self.assertTrue(realwrite.is_locked(st.dir))
            self.assertTrue(realwrite.clear_lock(st.dir))
            self.assertFalse(realwrite.is_locked(st.dir))

    def test_locked_exit_code_8_via_authorize(self):
        from real_write_support import CLAIM, FakeTTY, T0
        with State() as st, env("1"):
            record = st.record(1)
            realwrite.set_lock(st.dir)
            with patch.object(realwrite, "_io_streams", return_value=(FakeTTY(), FakeTTY())):
                with self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.authorize(st.ledger, CLAIM, record, T0)
            self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("locked", config.EXIT_LOCKED))


class AmountTests(unittest.TestCase):
    def test_amount_cap(self):
        cfg = realwrite.default_config()
        realwrite.check_amount(cfg, 10000)
        with self.assertRaises(realwrite.RealWriteDenied) as ctx:
            realwrite.check_amount(cfg, 10001)
        self.assertEqual((ctx.exception.reason, ctx.exception.exit_code), ("amount_cap", config.EXIT_LIMIT))
        for bad in (-1, 1.5, "100", None, True):
            with self.subTest(bad=bad), self.assertRaises(realwrite.RealWriteDenied):
                realwrite.check_amount(cfg, bad)


class TokenSourceTests(unittest.TestCase):
    def fixed(self, st):
        d = st.dir / "credentials"
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        return d / "mcd_mcp_token"

    def test_token_file_arg_rejected_for_official_write(self):
        with State() as st:
            for arg in ("/tmp/other", str(st.dir / "credentials" / "x"), "", "relative"):
                with self.subTest(arg=arg), self.assertRaises(realwrite.RealWriteDenied) as ctx:
                    realwrite.resolve_write_token_file(st.dir, arg)
                self.assertEqual(ctx.exception.reason, "token_source")

    def test_no_arg_env_or_absent_returns_none(self):
        with State() as st:
            self.assertIsNone(realwrite.resolve_write_token_file(st.dir))
            path = self.fixed(st)
            path.write_text("SYNTH")
            os.chmod(path, 0o644)
            with patch.dict(os.environ, {realwrite.TOKEN_ENV: "SYNTH"}):
                self.assertIsNone(realwrite.resolve_write_token_file(st.dir))  # env 优先，不检查文件

    def test_token_whitelist_file_perm_checks(self):
        with State() as st, patch.dict(os.environ, {realwrite.TOKEN_ENV: ""}):
            path = self.fixed(st)
            path.write_text("SYNTH")
            os.chmod(path, 0o644)
            with self.assertRaises(realwrite.RealWriteDenied):
                realwrite.resolve_write_token_file(st.dir)
            os.chmod(path, 0o600)
            self.assertEqual(realwrite.resolve_write_token_file(st.dir), path)
            self.assertEqual(realwrite.resolve_write_token_file(st.dir, str(path)), path)
            os.chmod(path.parent, 0o755)
            with self.assertRaises(realwrite.RealWriteDenied):
                realwrite.resolve_write_token_file(st.dir)
            os.chmod(path.parent, 0o700)
            path.unlink()
            os.symlink("/etc/hosts", path)
            with self.assertRaises(realwrite.RealWriteDenied):
                realwrite.resolve_write_token_file(st.dir)


class NoBypassTests(unittest.TestCase):
    FLAGS = ("--yes", "--force", "--no-tty", "--assume-tty", "--non-interactive", "--assume-yes", "--skip-confirm")

    def test_no_bypass_flags_in_cli_or_env(self):
        for name in ("cli.py", "realwrite.py", "config.py"):
            text = (SRC / name).read_text(encoding="utf-8")
            for flag in self.FLAGS:
                self.assertNotIn(flag, text, (name, flag))
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"[A-Z][A-Z0-9_]+", node.value) and re.search(r"TTY|CONFIRM|YES|ASSUME", node.value):
                        self.fail("疑似绕过用环境变量名 %s 出现在 %s" % (node.value, name))

    def test_only_expected_env_names_read(self):
        text = (SRC / "realwrite.py").read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r"os\.environ", text))), ["os.environ"])
        self.assertEqual(config.REAL_WRITE_ENV, "MEAL_CLEAR_ENABLE_REAL_WRITE")


class OfficialDomainTests(unittest.TestCase):
    BAD = (
        "https://mcp.mcd.cn.evil.com", "https://mcp.mcd.cn@evil.com", "http://mcp.mcd.cn",
        "https://mcp.mcd.cn:443", "https://mcp.mcd.cn/", "https://MCP.MCD.CN", "HTTPS://mcp.mcd.cn",
        "https://mcp.mcd.cn ", " https://mcp.mcd.cn", "https://mcp.mcd.cn\n", "https://evil.com",
        "https://mcp.mcd.cn?x=1", "https://mcp.mcd.cn#f", "https://mcp.mcd.cn.", "https://xmcp.mcd.cn",
        "https://mcp.mcd.cn/x", "", None, 5, b"https://mcp.mcd.cn", ["https://mcp.mcd.cn"],
    )

    def test_exact_string_only(self):
        self.assertEqual(ENDPOINT, config.OFFICIAL_ENDPOINT)
        self.assertEqual(config.OFFICIAL_ENDPOINT, "https://mcp.mcd.cn")
        self.assertTrue(net_guard.is_official_url("https://mcp.mcd.cn"))
        self.assertEqual(net_guard.assert_official_target("https://mcp.mcd.cn"), "https://mcp.mcd.cn")

    def test_hostname_bypass_all_rejected(self):
        for url in self.BAD:
            with self.subTest(url=url):
                self.assertFalse(net_guard.is_official_url(url))
                with self.assertRaises(net_guard.NetGuardError):
                    net_guard.assert_official_target(url)

    def test_str_subclass_rejected(self):
        class Evil(str):
            def __eq__(self, other):
                return True
        self.assertFalse(net_guard.is_official_url(Evil("https://evil.com")))

    def test_loopback_write_guard_still_rejects_official(self):
        with self.assertRaises(net_guard.NetGuardError):
            net_guard.assert_write_target("https://mcp.mcd.cn")


class ExitCodeTests(unittest.TestCase):
    def test_codes_extended_without_changing_old(self):
        self.assertEqual(
            (config.EXIT_OK, config.EXIT_ERROR, config.EXIT_NO_PLAN, config.EXIT_RECONFIRM,
             config.EXIT_REFUSED, config.EXIT_DUPLICATE, config.EXIT_UNKNOWN, config.EXIT_LIMIT, config.EXIT_LOCKED),
            tuple(range(9)),
        )
        for reason in ("switch_off", "config_invalid", "locked", "not_tty", "phrase_mismatch", "amount_cap",
                       "orders_cap", "claims_cap", "unknown_lock", "bad_endpoint", "token_source"):
            self.assertIn(reason, config.REAL_REFUSAL_REASONS)


if __name__ == "__main__":
    unittest.main()
