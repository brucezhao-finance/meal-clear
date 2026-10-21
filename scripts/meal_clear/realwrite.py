"""真实写第四道门：开关、白名单配置、紧急锁、TTY 人工确认、金额与次数上限。

本模块不发任何网络请求，写工具名只从 config 导入。没有任何命令行参数或环境变量
可以跳过 TTY 与短语确认；开关关闭时的拒绝文案与 gate.MSG_NOT_OPEN 逐字相同。
"""
from __future__ import annotations

import hmac
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from . import gate
from .config import (
    CHANNEL_OFFICIAL,
    CONFIRM_WORDS,
    CONTROLLED_WRITE_TOOLS,
    EXIT_LIMIT,
    EXIT_LOCKED,
    EXIT_REFUSED,
    PHRASE_TAIL_LEN,
    REAL_CONFIG_KEYS,
    REAL_CONFIG_MAX_BYTES,
    REAL_COUNT_LIMIT_KEY,
    REAL_COUNTED_STATES,
    REAL_LIMITS,
    REAL_WINDOW_SECONDS,
    REAL_WRITE_CONFIG_NAME,
    REAL_WRITE_ENV,
    TOOL_STEP,
    UNKNOWN_LOCK_SECONDS,
    WRITE_LOCK_NAME,
    WRITE_TOKEN_REL,
)

_SENTINEL = object()  # 模块私有哨兵：外部拿不到就伪造不了批准
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
TOKEN_ENV = "MCD_MCP_TOKEN"


class RealWriteDenied(gate.WriteDenied):
    """第四道门拒绝：本次没有发出任何写请求；reason/exit_code 供 CLI 区分。"""

    def __init__(self, message: str, reason: str, exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.reason = reason
        self.exit_code = exit_code


def _yuan(cents: int) -> str:
    return "¥%.2f" % (cents / 100)


def _invalid(why: str) -> RealWriteDenied:
    return RealWriteDenied("真实写配置文件无效：" + why + "。本次未发出任何请求。", "config_invalid")


# --- 配置与开关 ---
def default_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {"enabled": False}
    cfg.update({key: pair[0] for key, pair in REAL_LIMITS.items()})
    return cfg


def load_config(state_dir: Any) -> Dict[str, Any]:
    """读取 <state>/real-write.json；文件不存在返回默认值，存在但不合规则拒绝。"""
    state = Path(state_dir)
    path = state / REAL_WRITE_CONFIG_NAME
    cfg = default_config()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return cfg
    except OSError:
        raise _invalid("无法读取") from None
    if stat.S_ISLNK(info.st_mode):
        raise _invalid("不能是符号链接")
    if not stat.S_ISREG(info.st_mode):
        raise _invalid("不是普通文件")
    if info.st_uid != os.geteuid():
        raise _invalid("不属于当前用户")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise _invalid("文件权限必须为 600")
    dir_info = os.lstat(state)
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise _invalid("所在目录无效")
    if dir_info.st_uid != os.geteuid() or stat.S_IMODE(dir_info.st_mode) != 0o700:
        raise _invalid("所在目录权限必须为 700 且属于当前用户")
    if info.st_size > REAL_CONFIG_MAX_BYTES:
        raise _invalid("文件超过 64KB")
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        raise _invalid("无法读取") from None
    try:
        data = os.read(fd, REAL_CONFIG_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise _invalid("不是有效的 JSON") from None
    if not isinstance(raw, dict):
        raise _invalid("顶层必须是对象")
    unknown = [key for key in raw if key not in REAL_CONFIG_KEYS]
    if unknown:
        raise _invalid("含不支持的配置项")
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise _invalid("enabled 必须是布尔值")
        cfg["enabled"] = raw["enabled"]
    for key, (_default, hard) in REAL_LIMITS.items():
        if key not in raw:
            continue
        value = raw[key]
        if type(value) is not int or value <= 0:
            raise _invalid("%s 必须是正整数" % key)
        if value > hard:
            raise _invalid("%s 超过硬上限 %d" % (key, hard))
        cfg[key] = value
    return cfg


def real_write_enabled(state_dir: Any) -> bool:
    """env 恰为 "1" 或合法配置 enabled=true 才算开启；配置文件无效一律拒绝。"""
    cfg = load_config(state_dir)  # 即使 env 已开启，也要求配置文件合规
    return os.environ.get(REAL_WRITE_ENV) == "1" or cfg["enabled"] is True


def ensure_enabled(state_dir: Any) -> Dict[str, Any]:
    """开关关闭抛 gate.MSG_NOT_OPEN（逐字不变）；开启返回生效配置。"""
    cfg = load_config(state_dir)
    if not (os.environ.get(REAL_WRITE_ENV) == "1" or cfg["enabled"] is True):
        raise RealWriteDenied(gate.MSG_NOT_OPEN, "switch_off")
    return cfg


# --- 紧急锁 ---
def lock_path(state_dir: Any) -> Path:
    return Path(state_dir) / WRITE_LOCK_NAME


def is_locked(state_dir: Any) -> bool:
    """锁文件以任何形态存在（含符号链接、目录、悬空链接）都视为已锁定。"""
    return os.path.lexists(lock_path(state_dir))


def set_lock(state_dir: Any) -> None:
    state = Path(state_dir)
    if not os.path.lexists(state):
        state.mkdir(parents=True, mode=0o700)
    path = lock_path(state)
    if os.path.lexists(path):
        return
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def clear_lock(state_dir: Any) -> bool:
    """删除锁（调用方需先通过 human_confirm）；返回是否确有锁被移除。"""
    path = lock_path(state_dir)
    if not os.path.lexists(path):
        return False
    if os.path.isdir(path) and not os.path.islink(path):
        os.rmdir(path)
    else:
        os.unlink(path)
    return True


# --- TTY 与短语 ---
def _io_streams() -> Tuple[Any, Any]:
    """唯一取得交互流的位置；测试通过 patch 本函数注入假 TTY，不提供环境变量或参数。"""
    return sys.stdin, sys.stdout


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def require_tty() -> Tuple[Any, Any]:
    """stdin 与 stdout 都必须是终端。"""
    stdin, stdout = _io_streams()
    if not (_is_tty(stdin) and _is_tty(stdout)):
        raise RealWriteDenied(
            "官方真实写必须在你自己的交互式终端里亲手确认（不能管道、重定向或脚本代输）。本次未发出任何请求。",
            "not_tty",
        )
    return stdin, stdout


def expected_phrase(tool: str, digest: str) -> str:
    """<digest 末 6 位> 确认领券/确认下单。"""
    if not isinstance(tool, str) or tool not in CONTROLLED_WRITE_TOOLS or not isinstance(digest, str):
        raise RealWriteDenied(gate.MSG_TOOL, "bad_tool")
    return "%s %s" % (digest[-PHRASE_TAIL_LEN:], CONFIRM_WORDS[TOOL_STEP[tool]])


def human_confirm(prompt_lines: Iterable[str], phrase: str) -> None:
    """打印要点并读一行；只给一次机会，仅去掉行尾换行，其余必须逐字相等。"""
    stdin, stdout = require_tty()
    for line in prompt_lines:
        stdout.write(str(line) + "\n")
    stdout.write("请逐字输入「%s」后回车：" % phrase)
    stdout.flush()
    answer = stdin.readline()
    if not isinstance(answer, str):
        answer = ""
    for tail in ("\r\n", "\n", "\r"):  # 只去掉一个行尾，其余字符必须逐字相等
        if answer.endswith(tail):
            answer = answer[: -len(tail)]
            break
    if not hmac.compare_digest(answer.encode("utf-8"), phrase.encode("utf-8")):
        raise RealWriteDenied("确认短语不符，本次未发出任何请求。", "phrase_mismatch")


# --- 上限 ---
def check_amount(cfg: Dict[str, Any], total_cents: Any) -> None:
    if type(total_cents) is not int or total_cents < 0:
        raise RealWriteDenied("应付金额无效，拒绝发出请求。", "amount_cap", EXIT_LIMIT)
    if total_cents > cfg["max_order_cents"]:
        raise RealWriteDenied(
            "应付 %s 超过本机单笔上限 %s。本次未发出任何请求。"
            % (_yuan(total_cents), _yuan(cfg["max_order_cents"])),
            "amount_cap",
            EXIT_LIMIT,
        )


def check_counts(ledger: Any, cfg: Dict[str, Any], tool: str, now: datetime) -> None:
    """统计窗口内 official 写次数；调用方应在 ledger.global_locked() 内调用并紧接 begin_step。"""
    step = TOOL_STEP.get(tool) if isinstance(tool, str) else None
    if step is None:
        raise RealWriteDenied(gate.MSG_TOOL, "bad_tool")
    limit = cfg[REAL_COUNT_LIMIT_KEY[step]]
    used = ledger.count_channel_writes(
        CHANNEL_OFFICIAL, step, REAL_COUNTED_STATES[step], REAL_WINDOW_SECONDS, now
    )
    if used >= limit:
        label = "领券" if step == "claim" else "下单"
        raise RealWriteDenied(
            "24 小时内已真实%s %d 次，达到本机上限 %d。本次未发出任何请求。" % (label, used, limit),
            "claims_cap" if step == "claim" else "orders_cap",
            EXIT_LIMIT,
        )


# --- 凭证来源 ---
def resolve_write_token_file(state_dir: Any, token_file_arg: Optional[str] = None) -> Optional[Path]:
    """写命令的凭证来源：MCD_MCP_TOKEN，或固定白名单文件；其他 --token-file 一律拒绝（不读文件）。
    返回白名单文件路径；使用环境变量或没有凭证文件时返回 None。"""
    fixed = Path(state_dir).joinpath(*WRITE_TOKEN_REL)

    def deny(why: str) -> RealWriteDenied:
        return RealWriteDenied("写命令的凭证来源不符合要求：" + why + "。本次未发出任何请求。", "token_source")

    if token_file_arg is not None:
        if not isinstance(token_file_arg, str) or token_file_arg != str(fixed):
            raise deny("写命令不接受任意 --token-file")
    elif os.environ.get(TOKEN_ENV, "").strip() or not os.path.lexists(fixed):
        return None
    try:
        info = os.lstat(fixed)
        dir_info = os.lstat(fixed.parent)
    except OSError:
        raise deny("凭证文件不存在或无法访问") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise deny("凭证文件必须是普通文件且不能是符号链接")
    if stat.S_ISLNK(dir_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        raise deny("凭证目录无效")
    if info.st_uid != os.geteuid() or dir_info.st_uid != os.geteuid():
        raise deny("凭证文件或目录不属于当前用户")
    if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(dir_info.st_mode) != 0o700:
        raise deny("凭证目录权限必须为 700、文件权限必须为 600")
    return fixed


# --- 批准与会话 ---
class RealWriteApproval:
    """一次官方写的人工批准：私有哨兵、绑定 (tool, digest)、单次使用。"""

    __slots__ = ("tool", "digest", "_key", "_used")

    def __init__(self, tool: str, digest: str, key: object) -> None:
        if key is not _SENTINEL:
            raise RealWriteDenied(gate.MSG_TOOL, "bad_tool")
        self.tool = tool
        self.digest = digest
        self._key = key
        self._used = False


def approval_valid(approval: object, tool: str, digest: str) -> bool:
    """批准是否由本模块签发、与 (tool, digest) 匹配且尚未使用。"""
    return (
        isinstance(approval, RealWriteApproval)
        and getattr(approval, "_key", None) is _SENTINEL
        and approval._used is False
        and approval.tool == tool
        and approval.digest == digest
        and tool in CONTROLLED_WRITE_TOOLS
    )


def consume_approval(approval: object, tool: str, digest: str) -> bool:
    """校验并标记已使用；第二次调用返回 False。"""
    if not approval_valid(approval, tool, digest):
        return False
    approval._used = True  # type: ignore[union-attr]
    return True


class RealWriteSession:
    """authorize 通过后的会话：持有生效配置，负责金额、次数与人工确认。"""

    def __init__(self, ledger: Any, tool: str, digest: str, basket_key: str, cfg: Dict[str, Any], now: datetime) -> None:
        self.ledger = ledger
        self.tool = tool
        self.digest = digest
        self.basket_key = basket_key
        self.cfg = cfg
        self.now = now

    def check_amount(self, total_cents: Any) -> None:
        check_amount(self.cfg, total_cents)

    def check_counts(self) -> None:
        check_counts(self.ledger, self.cfg, self.tool, self.now)

    def confirm(self, summary_view: Iterable[str]) -> RealWriteApproval:
        """打印摘要要点并校验短语，成功后签发单次批准。"""
        human_confirm(summary_view, expected_phrase(self.tool, self.digest))
        return RealWriteApproval(self.tool, self.digest, _SENTINEL)


def authorize(ledger: Any, tool: str, record: Dict[str, Any], now: datetime) -> RealWriteSession:
    """离线第四道门预检（开关→配置→紧急锁→TTY→unknown 篮子锁）；通过返回会话。"""
    if not isinstance(tool, str) or tool not in CONTROLLED_WRITE_TOOLS:
        raise RealWriteDenied(gate.MSG_TOOL, "bad_tool")
    state_dir = Path(ledger.root).parent
    cfg = ensure_enabled(state_dir)
    if is_locked(state_dir):
        raise RealWriteDenied(
            "真实写已被 write-lock 锁定。请先用 write-lock --status 查看，由你本人解除。本次未发出任何请求。",
            "locked",
            EXIT_LOCKED,
        )
    require_tty()
    basket = record.get("basket_key")
    digest = record.get("digest")
    if ledger.find_official_unresolved(basket, UNKNOWN_LOCK_SECONDS, now):
        raise RealWriteDenied(
            "该篮子有结果未知的真实写操作：先用 order-status 或 App 核实，再用 order-resolve 标注。"
            "本次未发出任何请求。",
            "unknown_lock",
            EXIT_LOCKED,
        )
    return RealWriteSession(ledger, tool, digest, basket, cfg, now)
