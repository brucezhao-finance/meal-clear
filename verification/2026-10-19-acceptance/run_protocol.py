"""Exercise real MCP client error handling with simulated transport failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
from unittest.mock import Mock, patch
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from meal_clear.mcp import McpClient, McpError  # noqa: E402


def main() -> int:
    """Check attempt counts and fail-closed behavior without network access."""
    records = []
    for case_id, status, recovery, expected_calls in [
        ("P01", 401, False, 1),
        ("P02", 403, False, 1),
        ("P03", 503, False, 3),
        ("P04", 503, True, 2),
    ]:
        with patch.dict(
            os.environ, {"MCD_MCP_TOKEN": "SYNTHETIC-NOT-A-REAL-CREDENTIAL"}
        ):
            client = McpClient()
        error = urllib.error.HTTPError(
            "https://mcp.mcd.cn", status, "SIMULATED", {}, None
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        response.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        transport = Mock(side_effect=[error, response] if recovery else error)
        client._opener.open = transport
        observed: Dict[str, Any] = {}
        with patch("meal_clear.mcp.time.sleep") as sleep:
            try:
                observed["result"] = client._post("tools/list", {})
                observed["error"] = None
            except McpError as caught:
                observed["error"] = str(caught)
            observed["attempts"] = transport.call_count
            observed["retry_delays"] = [call.args[0] for call in sleep.call_args_list]
        passed = (
            observed["attempts"] == expected_calls
            and (observed["error"] is None) == recovery
        )
        records.append(
            {
                "id": case_id,
                "mode": "simulated_transport",
                "http_status": status,
                "recovery": recovery,
                "expected_attempts": expected_calls,
                "observed": observed,
                "passed": passed,
            }
        )
    with patch.dict(os.environ, {"MCD_MCP_TOKEN": "SYNTHETIC-NOT-A-REAL-CREDENTIAL"}):
        client = McpClient()
    client._opener.open = Mock(
        side_effect=AssertionError("Write tool reached transport")
    )
    blocked = []
    for name in (
        "create-order",
        "auto-bind-coupons",
        "cancel-order",
        "pay-order",
        "redeem-points",
    ):
        try:
            client.call(name, {})
        except McpError:
            blocked.append(name)
    records.append(
        {
            "id": "P05",
            "mode": "simulated_transport",
            "blocked_tools": blocked,
            "network_attempts": client._opener.open.call_count,
            "passed": len(blocked) == 5 and client._opener.open.call_count == 0,
        }
    )
    summary = {
        "cases": records,
        "passed": sum(r["passed"] for r in records),
        "failed": sum(not r["passed"] for r in records),
    }
    (Path(__file__).resolve().parent / "protocol-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(summary["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
