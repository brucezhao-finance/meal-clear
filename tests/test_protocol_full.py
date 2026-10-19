"""Full read-only protocol behavior with deterministic simulated transport."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.mcp import (
    ENDPOINT,
    READ_ONLY,
    McpClient,
    McpError,
    NoRedirect,
    decode_response,
    unwrap_tool,
)


def response(payload: object, session: str = "") -> Mock:
    """Return a context-managed byte response with no actual network socket."""
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.headers = {"Mcp-Session-Id": session} if session else {}
    value.read.return_value = (
        payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    )
    return value


def client() -> McpClient:
    """Construct a client with a nonfunctional synthetic credential confined to memory."""
    with patch.dict(os.environ, {"MCD_MCP_TOKEN": "SYNTHETIC-NOT-A-REAL-CREDENTIAL"}):
        return McpClient()


class ProtocolFullTests(unittest.TestCase):
    """Cover authentication, decoding, HTTP retries, schema paging and audit privacy."""

    def test_P01_credentials_environment_file_precedence_and_rejection(self) -> None:
        """Missing, ambiguous and whitespace credentials fail without revealing content."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "credential.txt"
            file.write_text("SYNTHETIC-FILE-CREDENTIAL-000000")
            with patch.dict(os.environ, {"MCD_MCP_TOKEN": ""}):
                self.assertEqual(
                    McpClient(token_file=str(file))._token,
                    "SYNTHETIC-FILE-CREDENTIAL-000000",
                )
                with self.assertRaises(McpError):
                    McpClient()
                file.write_text(
                    "SYNTHETIC-FILE-CREDENTIAL-000000\nSYNTHETIC-OTHER-CREDENTIAL-000000"
                )
                with self.assertRaises(McpError) as error:
                    McpClient(token_file=str(file))
                self.assertNotIn("SYNTHETIC-FILE", str(error.exception))
            with (
                patch.dict(os.environ, {"MCD_MCP_TOKEN": "HAS INTERNAL SPACE"}),
                self.assertRaises(McpError),
            ):
                McpClient()
            with patch.dict(os.environ, {"MCD_MCP_TOKEN": "SYNTHETIC-ENV"}):
                self.assertEqual(
                    McpClient(token_file="/not-a-file")._token, "SYNTHETIC-ENV"
                )

    def test_P02_json_sse_bom_multiline_and_invalid_bytes(self) -> None:
        """JSON and SSE select the right ID; malformed payloads fail with safe errors."""
        self.assertEqual(
            decode_response(b'\xef\xbb\xbf{"id":1,"result":{}}', 1)["id"], 1
        )
        raw = b': keepalive\r\n\r\ndata: nope\r\n\r\ndata: {"id":99}\r\n\r\ndata: {"id":1,\r\ndata: "result":{"ok":true}}\r\n\r\n'
        self.assertTrue(decode_response(raw, 1)["result"]["ok"])
        for raw in [
            b"[]",
            b"not-json",
            b"data: []\n\n",
            b'data: {"id":2}\n\n',
            b"\xff\xfe",
        ]:
            with self.subTest(raw=raw), self.assertRaises(McpError):
                decode_response(raw, 1)

    def test_P03_tool_content_modes_and_business_errors_sanitize(self) -> None:
        """Tool failures never expose account messages; both structured/text formats work."""
        self.assertEqual(
            unwrap_tool({"structuredContent": {"code": 0, "data": 1}})["data"], 1
        )
        self.assertEqual(
            unwrap_tool(
                {
                    "content": [
                        {"type": "image"},
                        {"type": "text", "text": "nope"},
                        {"type": "text", "text": "[]"},
                        {"type": "text", "text": '{"code":"200","data":2}'},
                    ]
                }
            )["data"],
            2,
        )
        cases = [
            None,
            {"isError": True},
            {"content": []},
            {
                "structuredContent": {
                    "code": "PRIVATE-SENTINEL",
                    "message": "PRIVATE-SENTINEL",
                }
            },
            {"structuredContent": {"code": 500, "message": "PRIVATE-SENTINEL"}},
            {"content": None},
            {"content": [None]},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(McpError) as caught:
                unwrap_tool(data)
            self.assertNotIn("PRIVATE-SENTINEL", str(caught.exception))

    def test_P04_post_rpc_errors_ids_types_size_and_notification(self) -> None:
        """Invalid envelopes, oversized responses and mismatched IDs are not accepted."""
        for payload in [
            {"id": 1, "error": {"message": "PRIVATE-SENTINEL"}},
            {"id": 2, "result": {}},
            {"id": True, "result": {}},
            {"id": 1, "result": []},
            b"x" * (8 * 1024 * 1024 + 1),
        ]:
            c = client()
            c._opener.open = Mock(return_value=response(payload))
            with (
                self.subTest(kind=str(type(payload))),
                self.assertRaises(McpError) as error,
            ):
                c._post("tools/list", {})
            self.assertNotIn("PRIVATE-SENTINEL", str(error.exception))
        c = client()
        c._opener.open = Mock(return_value=response(b""))
        self.assertEqual(c._post("notifications/initialized", notification=True), {})
        sent = json.loads(c._opener.open.call_args.args[0].data)
        self.assertNotIn("id", sent)
        self.assertNotIn("params", sent)

    def test_P05_initialization_protocol_and_session_reused(self) -> None:
        """Initialize/notification propagate the negotiated protocol and opaque session."""
        c = client()
        c._opener.open = Mock(
            side_effect=[
                response(
                    {
                        "id": 1,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "serverInfo": {"name": "synthetic"},
                        },
                    },
                    "SYNTHETIC-SESSION",
                ),
                response(b""),
            ]
        )
        self.assertEqual(c.initialize(), {"name": "synthetic"})
        second = c._opener.open.call_args.args[0]
        headers = {k.lower(): v for k, v in second.headers.items()}
        self.assertEqual(headers["mcp-session-id"], "SYNTHETIC-SESSION")
        self.assertEqual(headers["mcp-protocol-version"], "2025-03-26")
        self.assertEqual(second.full_url, ENDPOINT)
        self.assertEqual(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.invalid"
            ),
            None,
        )

    def test_P06_http_and_network_retry_budget(self) -> None:
        """Authentication, redirect and bad request stop; transient errors retry at most twice."""
        for code, attempts in [
            (400, 1),
            (401, 1),
            (403, 1),
            (302, 1),
            (429, 3),
            (500, 3),
            (502, 3),
            (503, 3),
            (504, 3),
        ]:
            c = client()
            c._opener.open = Mock(
                side_effect=urllib.error.HTTPError(
                    ENDPOINT, code, "PRIVATE-SENTINEL", {}, None
                )
            )
            with (
                self.subTest(code=code),
                patch("meal_clear.mcp.time.sleep") as wait,
                self.assertRaises(McpError) as caught,
            ):
                c._post("tools/list", {})
            self.assertEqual(c._opener.open.call_count, attempts)
            self.assertEqual(wait.call_count, attempts - 1)
            self.assertNotIn("PRIVATE-SENTINEL", str(caught.exception))
        for error in [
            urllib.error.URLError("PRIVATE-SENTINEL"),
            TimeoutError("PRIVATE-SENTINEL"),
            OSError("PRIVATE-SENTINEL"),
        ]:
            c = client()
            c._opener.open = Mock(side_effect=error)
            with (
                self.subTest(error=type(error).__name__),
                patch("meal_clear.mcp.time.sleep"),
                self.assertRaises(McpError),
            ):
                c._post("tools/list", {})
            self.assertEqual(c._opener.open.call_count, 3)
        c = client()
        c._opener.open = Mock(
            side_effect=[TimeoutError(), response({"id": 1, "result": {"ok": True}})]
        )
        with patch("meal_clear.mcp.time.sleep"):
            self.assertTrue(c._post("tools/list", {})["ok"])

    def test_P07_tools_paginate_and_reject_cycles_and_overflow(self) -> None:
        """All pages are combined once; repeated cursors and overlong sequences stop."""
        c = client()
        c._post = Mock(
            side_effect=[
                {"tools": [{"name": "a"}], "nextCursor": "one"},
                {"tools": [{"name": "b"}]},
            ]
        )
        self.assertEqual([t["name"] for t in c.list_tools()["tools"]], ["a", "b"])
        c = client()
        c._post = Mock(return_value={"tools": [], "nextCursor": "repeat"})
        with self.assertRaises(McpError):
            c.list_tools()
        self.assertEqual(c._post.call_count, 2)
        c = client()
        c._post = Mock(
            side_effect=[{"tools": [], "nextCursor": str(i)} for i in range(12)]
        )
        with self.assertRaises(McpError):
            c.list_tools()
        self.assertEqual(c._post.call_count, 11)
        for malformed in [
            {"tools": None},
            {"tools": [None]},
            {"tools": [], "nextCursor": []},
        ]:
            c = client()
            c._post = Mock(return_value=malformed)
            with self.subTest(malformed=malformed), self.assertRaises(McpError):
                c.list_tools()

    def test_P08_whitelist_and_audit_exclude_payloads_and_credentials(self) -> None:
        """Every allowed tool uses readonly transport; every mutation remains unreachable."""
        c = client()
        c._post = Mock(
            return_value={
                "structuredContent": {
                    "code": 0,
                    "data": {"private": "PRIVATE-SENTINEL"},
                }
            }
        )
        for name in READ_ONLY:
            self.assertIn("data", c.call(name, {}))
        self.assertEqual(len(c.audit), len(READ_ONLY))
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(c.audit))
        self.assertNotIn(c._token, json.dumps(c.audit))
        c._post = Mock(side_effect=McpError("模拟只读失败"))
        with self.assertRaises(McpError):
            c.call("query-meals", {})
        self.assertFalse(c.audit[-1]["success"])
        for name in (
            "create-order",
            "auto-bind-coupons",
            "cancel-order",
            "pay-order",
            "mall-create-order",
            "draw-lottery",
            "unknown",
        ):
            count = c._post.call_count
            with self.subTest(name=name), self.assertRaises(McpError):
                c.call(name, {})
            self.assertEqual(c._post.call_count, count)
