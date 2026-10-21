"""受控写通道的三重门：执行意图、摘要确认、写目标回环。

只有本模块能签发 WriteGrant；只有 WriteGate.call 会调用 McpClient.call_write。
工具名用白名单（CONTROLLED_WRITE_TOOLS）判定，其余一律拒绝。
"""

from __future__ import annotations

import hmac
import re
from typing import Any, Dict, Optional

from . import net_guard
from .config import CONTROLLED_WRITE_TOOLS
from .mcp import ENDPOINT, McpError

MSG_NOT_OPEN = "真实领券/下单尚未开放，计划在 P2 由用户亲自验证后启用。本次未发出任何请求。"
MSG_NOT_LOOPBACK = "写操作只允许发往本机回环地址（127.0.0.1 或 [::1]）。本次未发出任何请求。"
MSG_NO_EXECUTE = "缺少 --execute：当前只是预演，本次未发出任何写请求。"
MSG_DIGEST = "确认摘要缺失或与记录不符，请核对完整摘要编号后重新确认。本次未发出任何写请求。"
MSG_EXPIRED = "摘要已过期，请重新生成并确认。本次未发出任何写请求。"
MSG_TOOL = "该工具不在受控写白名单内，永久拒绝。本次未发出任何请求。"
MSG_NO_ATTEMPT = "尚未先写入账本（缺少有效的执行记录句柄），拒绝发出写请求。"

_DIGEST_FULL = re.compile(r"\A[0-9a-f]{64}\Z")
_SENTINEL = object()  # 模块私有哨兵：外部拿不到就签发不了授权


class WriteDenied(McpError):
    """写门拒绝：本次没有发出任何写请求。"""


def check_write_target(endpoint: object, *, session: object = None) -> None:
    """写目标必须是严格回环；官方端点仅在持有第四道门会话（realwrite.authorize）时放行，否则给出『尚未开放』说明。"""
    try:
        net_guard.assert_write_target(endpoint)
    except net_guard.NetGuardError:
        if not net_guard.is_official_url(endpoint):
            raise WriteDenied(MSG_NOT_LOOPBACK) from None
        from . import realwrite  # 延迟导入：realwrite 顶层导入本模块

        if session is not None and realwrite.is_session(session):
            return
        raise WriteDenied(MSG_NOT_OPEN) from None


class WriteGrant:
    """一次写调用的授权；构造需要模块私有哨兵，只有 WriteGate 会创建。"""

    __slots__ = ("tool", "_key", "approval", "official")

    def __init__(self, tool: str, key: object, approval: object = None, official: bool = False) -> None:
        if key is not _SENTINEL:
            raise WriteDenied(MSG_TOOL)
        self.tool = tool
        self._key = key
        self.approval = approval  # 官方通道：已被消费的 RealWriteApproval
        self.official = official is True


def grant_is_valid(grant: object, tool: str) -> bool:
    """McpClient.call_write 用：授权必须由本模块签发且与工具匹配。"""
    return (
        isinstance(grant, WriteGrant)
        and getattr(grant, "_key", None) is _SENTINEL
        and grant.tool == tool
        and tool in CONTROLLED_WRITE_TOOLS
    )


def grant_is_official(grant: object, tool: str) -> bool:
    """McpClient.call_write 在官方端点上用：授权有效、标记官方通道，且携带已消费的人工批准。"""
    from . import realwrite

    return (
        grant_is_valid(grant, tool)
        and getattr(grant, "official", False) is True
        and realwrite.approval_consumed(getattr(grant, "approval", None), tool)
    )


class WriteGate:
    """构造时校验三重门，每次调用前再校验，并强制先落账。"""

    def __init__(
        self,
        client: Any,
        *,
        execute: bool,
        confirm_digest: str,
        record_digest: str,
        expires_at: Any,
        now: Any,
        budget: Any = None,
        ledger: Any = None,
        approval: Any = None,
    ) -> None:
        # 优先保证 BudgetedClient 的计数，同时拿到底层客户端
        if budget is None and hasattr(client, "spend") and hasattr(client, "_client"):
            budget, client = client, client._client
        self._client = client
        self._execute = execute
        self._confirm = confirm_digest
        self._record = record_digest
        self._expires_at = expires_at
        self._now = now
        self._budget = budget
        self._ledger = ledger
        self._approval = approval
        self._check()

    @classmethod
    def open(cls, client: Any, **kwargs: Any) -> "WriteGate":
        """校验 1–3 门后返回；失败抛 WriteDenied。"""
        return cls(client, **kwargs)

    def _check(self) -> None:
        """门 1：execute；门 2：digest 与有效期；门 3：写目标回环。"""
        if self._execute is not True:
            raise WriteDenied(MSG_NO_EXECUTE)
        confirm, record = self._confirm, self._record
        if (
            not isinstance(confirm, str)
            or not isinstance(record, str)
            or not _DIGEST_FULL.match(record)
            or not _DIGEST_FULL.match(confirm)
            or not hmac.compare_digest(confirm.encode(), record.encode())
        ):
            raise WriteDenied(MSG_DIGEST)
        try:
            expired = not (self._now < self._expires_at)
        except TypeError:
            expired = True
        if expired:
            raise WriteDenied(MSG_EXPIRED)
        endpoint = getattr(self._client, "endpoint", None)
        if net_guard.is_official_url(endpoint):
            self._check_official()
            return
        check_write_target(endpoint)

    def _check_official(self) -> None:
        """官方通道：必须持有绑定本摘要、尚未使用的人工批准（realwrite 签发）。"""
        from . import realwrite

        ap = self._approval
        if not realwrite.approval_valid(ap, getattr(ap, "tool", None), self._record):
            raise WriteDenied(MSG_NOT_OPEN)

    def _attempt_ok(self, attempt: Any) -> bool:
        """句柄必须来自 ledger.begin_step 且属于本摘要；有账本时核对仍为 pending。"""
        from .ledger import AttemptId

        if not isinstance(attempt, AttemptId) or not str(attempt):
            return False
        if attempt.digest != self._record:
            return False
        if self._ledger is not None:
            return bool(self._ledger.is_pending(attempt))
        return True

    def call(self, tool: str, args: Dict[str, Any], attempt: Any) -> Dict[str, Any]:
        """每次写调用前重新校验；白名单外工具一律拒绝。"""
        if not isinstance(tool, str) or tool not in CONTROLLED_WRITE_TOOLS:
            raise WriteDenied(MSG_TOOL)
        self._check()
        if not self._attempt_ok(attempt):
            raise WriteDenied(MSG_NO_ATTEMPT)
        if self._budget is not None:
            self._budget.spend()
        if net_guard.is_official_url(getattr(self._client, "endpoint", None)):
            from . import realwrite

            if not realwrite.consume_approval(self._approval, tool, self._record):
                raise WriteDenied(MSG_NOT_OPEN)  # 批准单次使用，且绑定工具与摘要
            grant = WriteGrant(tool, _SENTINEL, approval=self._approval, official=True)
        else:
            grant = WriteGrant(tool, _SENTINEL)
        return self._client.call_write(tool, args, grant=grant)
