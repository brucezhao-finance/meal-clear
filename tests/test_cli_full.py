"""Full command routing, rendering, exit codes, privacy and atomic state tests."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.cli import client_from_args, main, read_json, render, save_json
from meal_clear.core import compose
from meal_clear.mcp import McpError
from full_fixtures import SyntheticClient, request


class CliFullTests(unittest.TestCase):
    """Run the actual CLI entrypoint in-process, isolating only filesystem and network."""

    def setUp(self) -> None:
        """Make temporary request, catalog, state and patch fixtures."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.request = json.loads((ROOT / "examples/request.json").read_text())
        self.catalog = json.loads((ROOT / "examples/demo-catalog.json").read_text())
        self.req = self.write("request.json", self.request)
        self.cat = self.write("catalog.json", self.catalog)
        self.state = self.write("state.json", compose(self.request, self.catalog))
        self.patch = self.write(
            "patch.json", {"people": {"B": {"hard": {"main_meat": "beef"}}}}
        )

    def write(self, name: str, data: object) -> str:
        """Write an isolated synthetic JSON fixture."""
        path = self.directory / name
        path.write_text(json.dumps(data, ensure_ascii=False))
        return str(path)

    def invoke(self, args: list, fake: object = None) -> tuple:
        """Capture actual stdout, stderr and command exit, never a real credential."""
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            if fake is None:
                code = main(args)
            else:
                with patch("meal_clear.cli.client_from_args", return_value=fake):
                    code = main(args)
        return code, output.getvalue(), error.getvalue()

    def test_Q01_all_help_entries_are_available(self) -> None:
        """Each advertised subcommand has discoverable parser help."""
        for args in [["--help"]] + [
            [name, "--help"]
            for name in (
                "demo",
                "plan",
                "update",
                "stores",
                "live-plan",
                "inspect-tools",
                "break-even",
            )
        ]:
            with (
                self.subTest(args=args),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(args)
            self.assertEqual(caught.exception.code, 0)
        for args in [
            [],
            ["unknown"],
            ["plan"],
            ["break-even", "--fee", "bad", "--saving", "1"],
        ]:
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                main(args)
            self.assertEqual(caught.exception.code, 2)

    def test_Q02_demo_text_and_json_are_consistent(self) -> None:
        """Both modes reproduce 38→43 without a network client."""
        for args in [["demo"], ["demo", "--json"]]:
            with patch(
                "meal_clear.cli.client_from_args", side_effect=AssertionError("network")
            ):
                code, out, err = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertFalse(err)
            if "--json" in args:
                self.assertEqual(json.loads(out)["difference"]["delta_cents"], 500)
            else:
                self.assertIn("模拟", out)
                self.assertIn("43.00", out)

    def test_Q03_plan_save_update_text_json_and_exit_codes(self) -> None:
        """A saved plan can be amended in both presentation modes; a failed plan exits 2."""
        saved = str(self.directory / "nested" / "saved.json")
        code, out, _ = self.invoke(
            [
                "plan",
                "--request",
                self.req,
                "--catalog",
                self.cat,
                "--save",
                saved,
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(Path(saved).exists())
        self.assertEqual(json.loads(out)["status"], "simulation")
        for flag in [[], ["--json"]]:
            code, out, _ = self.invoke(
                [
                    "update",
                    "--state",
                    saved,
                    "--patch",
                    self.patch,
                    "--catalog",
                    self.cat,
                    *flag,
                ]
            )
            self.assertEqual(code, 0)
            if flag:
                self.assertEqual(json.loads(out)["difference"]["delta_cents"], 500)
            else:
                self.assertIn("变化说明", out)
        failed = self.write("failed.json", {**self.request, "budget_cents": 1})
        code, out, _ = self.invoke(["plan", "--request", failed, "--catalog", self.cat])
        self.assertEqual(code, 2)
        self.assertIn("没有可确认", out)
        self.assertIn("需要说明", out)

    def test_Q04_file_and_json_errors_are_sanitized(self) -> None:
        """Missing/bad/top-level-array/non-UTF8 files return controlled JSON with no trace."""
        paths = [str(self.directory / "missing.json"), self.write("list.json", [])]
        bad = self.directory / "bad.json"
        bad.write_text('{"private":"DO-NOT-EXPOSE"')
        paths.append(str(bad))
        bad = self.directory / "bytes.json"
        bad.write_bytes(b"\xff\xfePRIVATE-SENTINEL")
        paths.append(str(bad))
        for file in paths:
            code, out, err = self.invoke(
                ["plan", "--request", file, "--catalog", self.cat, "--json"]
            )
            with self.subTest(path=Path(file).name):
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["status"], "error")
                self.assertNotIn("DO-NOT-EXPOSE", out)
                self.assertNotIn("PRIVATE-SENTINEL", out)
                self.assertFalse(err)

    def test_Q05_offline_update_without_catalog_and_missing_state_fields(self) -> None:
        """Missing menu or malformed saved state cannot silently reuse or invent prices."""
        code, out, _ = self.invoke(
            ["update", "--state", self.state, "--patch", self.patch, "--json"]
        )
        self.assertEqual(code, 1)
        self.assertIn("--catalog", out)
        bad = self.write("bad-state.json", {})
        code, out, _ = self.invoke(
            ["update", "--state", bad, "--patch", self.patch, "--json"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "error")

    def test_Q06_inspect_stores_and_card_subcommands(self) -> None:
        """Tool discovery exposes only whitelist names; store output drops private fields."""
        fake = SyntheticClient()
        fake.schemas["tools"].append({"name": "create-order"})
        code, out, _ = self.invoke(["inspect-tools"], fake)
        self.assertEqual(code, 0)
        self.assertNotIn("create-order", json.loads(out)["available_read_only"])
        code, out, _ = self.invoke(
            ["stores", "--city", "模拟城市", "--keyword", "模拟"], fake
        )
        self.assertEqual(code, 0)
        self.assertNotIn("DO-NOT-EXPOSE", out)
        self.assertEqual(json.loads(out)["stores"][0]["storeCode"], "12345")
        code, out, _ = self.invoke(["break-even", "--fee", "1401", "--saving", "200"])
        self.assertEqual((code, json.loads(out)), (0, {"visits": 8}))

    def test_Q07_live_plan_and_live_update_requery_instead_of_reuse(self) -> None:
        """The actual live CLI path saves state and a later patch performs fresh quotes."""
        fake = SyntheticClient()
        req = self.write("live-request.json", request())
        state = str(self.directory / "live.json")
        code, out, _ = self.invoke(
            ["live-plan", "--request", req, "--save", state, "--json"], fake
        )
        self.assertEqual(code, 0)
        old = json.loads(out)
        patchfile = self.write("live-patch.json", {"budget_cents": 9000})
        code, out, _ = self.invoke(
            ["update", "--state", state, "--patch", patchfile, "--json"], fake
        )
        result = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotEqual(old["request_id"], result["request_id"])
        self.assertFalse(result["difference"]["old_quote_reused"])
        self.assertGreater(fake.quote_count, 3)
        self.assertEqual(sum(a["tool"] == "query-meals" for a in fake.audit), 2)

    def test_Q08_client_initialization_failure_is_controlled(self) -> None:
        """Client initialization failures return a safe error instead of printing secrets."""
        with patch("meal_clear.cli.McpClient", side_effect=McpError("模拟认证失败")):
            code, out, err = self.invoke(["inspect-tools"])
        self.assertEqual(code, 1)
        self.assertIn("模拟认证失败", out)
        self.assertFalse(err)
        fake = SyntheticClient()
        with patch("meal_clear.cli.McpClient", return_value=fake):
            import argparse

            self.assertIs(client_from_args(argparse.Namespace(token_file=None)), fake)

    def test_Q09_render_all_modes_diagnostics_quote_errors_and_unknown_fees(
        self,
    ) -> None:
        """Rendering explains provenance, hard checks, pricing time and missing breakdowns."""
        for mode, label in [
            ("simulation", "模拟"),
            ("historical", "历史资料"),
            ("user_supplied", "用户提供资料"),
        ]:
            cat = {**self.catalog, "mode": mode}
            out = render(compose(self.request, cat))
            self.assertIn(label, out)
        result = compose(self.request, self.catalog)
        result.update(
            mode="live",
            diagnostics=["模拟文字诊断", {"reason": "模拟结构化诊断"}],
            quote_errors=["模拟核价失败"],
        )
        result["recommendations"][0].update(
            quoted_at="2026-10-19T00:00:00+00:00", fee_breakdown={"deliveryPrice": None}
        )
        complete = json.loads(json.dumps(result))
        complete["recommendations"][0]["fee_breakdown"] = {"deliveryPrice": 0}
        self.assertNotIn("保留未知", render(complete))
        out = render(result)
        for text in [
            "只读实时查询",
            "官方核价时间",
            "保留未知",
            "核价异常",
            "已核实",
            "模拟结构化诊断",
        ]:
            self.assertIn(text, out)

    def test_Q10_state_save_failure_preserves_original_and_cleans_temporary(
        self,
    ) -> None:
        """Atomic replace failure leaves the previous file untouched and removes partial output."""
        path = self.directory / "atomic.json"
        path.write_text("ORIGINAL")
        with (
            patch("pathlib.Path.replace", side_effect=OSError("模拟磁盘失败")),
            self.assertRaises(OSError),
        ):
            save_json(str(path), {"new": 1})
        self.assertEqual(path.read_text(), "ORIGINAL")
        self.assertEqual(list(self.directory.glob("atomic.json*.tmp")), [])

    def test_Q11_state_save_does_not_follow_predictable_temp_symlink(self) -> None:
        """A pre-existing .tmp symlink cannot overwrite another user's chosen file."""
        victim = self.directory / "victim.txt"
        victim.write_text("KEEP-ORIGINAL")
        target = self.directory / "safe.json"
        target.with_name(target.name + ".tmp").symlink_to(victim)
        try:
            save_json(str(target), {"synthetic": 1})
        except OSError:
            pass
        self.assertEqual(victim.read_text(), "KEEP-ORIGINAL")

    def test_Q12_state_save_permissions_and_serialization_failure(self) -> None:
        """Both new and overwritten states are private; serialization failure keeps old state."""
        path = self.directory / "private.json"
        save_json(str(path), {"version": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        save_json(str(path), {"version": 2})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(TypeError):
            save_json(str(path), {"bad": object()})
        self.assertEqual(read_json(str(path)), {"version": 2})

    def test_Q13_actual_script_entrypoint_and_import_guard(self) -> None:
        """The distributable entry script runs as __main__ and stays inert when imported."""
        import runpy

        output = io.StringIO()
        with (
            patch("sys.argv", ["meal_clear.py", "demo", "--json"]),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(str(ROOT / "scripts/meal_clear.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["difference"]["delta_cents"], 500
        )
        with patch(
            "meal_clear.cli.main",
            side_effect=AssertionError("CLI called during import"),
        ):
            runpy.run_path(
                str(ROOT / "scripts/meal_clear.py"), run_name="import_only_test"
            )
