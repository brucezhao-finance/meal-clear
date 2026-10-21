"""本地订单账本：先落账再写请求；flock + 原子写 + 0700/0600 + 崩溃恢复。

账本文件按完整 64 位 digest 命名。事件与明细只存事件名、状态、数字错误码和时间，
不存参数、回包、券码、地址或支付链接。仅支持 POSIX（需要 fcntl）。
"""

from __future__ import annotations

import contextlib
import copy
import errno
import json
import os
import re
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from .config import (
    CHANNEL_LOOPBACK,
    CHANNELS,
    LEDGER_MAX_BYTES,
    LEDGER_SCHEMA,
    LEDGER_STEP_STATES,
    LEDGER_WRITE_STEPS,
    RESOLVE_CHOICES,
)
from .mcp import McpError

try:  # 无 fcntl 的平台（如 Windows）不能提供互斥与崩溃恢复
    import fcntl
except ImportError:  # pragma: no cover - 平台相关
    fcntl = None  # type: ignore[assignment]

_DIGEST_PREFIX = re.compile(r"\A[0-9a-f]{12,64}\Z")
_DIGEST_FULL = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_TEXT = re.compile(r"\A[\w.:+\- ]{0,64}\Z")
STEP_STATES = LEDGER_STEP_STATES
WRITE_STEPS = LEDGER_WRITE_STEPS
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class LedgerError(McpError):
    """账本不可用或状态不满足（文案可安全展示）。"""


class LedgerUnavailable(LedgerError):
    """平台不支持账本互斥。"""


class LedgerCorrupt(LedgerError):
    """账本损坏、超限或被篡改；拒绝执行且不覆盖。"""


class LedgerStateError(LedgerError):
    """当前步骤状态不允许该转换。"""

    def __init__(self, message: str, state: Optional[str] = None) -> None:
        super().__init__(message)
        self.state = state


class AttemptId(str):
    """ledger.begin_step 返回的执行句柄（WriteGate 以它强制先落账）。"""

    digest: str
    step: str

    def __new__(cls, value: str, digest: str, step: str) -> "AttemptId":
        obj = super().__new__(cls, value)
        obj.digest = digest
        obj.step = step
        return obj


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def check_digest(digest: object) -> str:
    """文件级 digest 必须是完整 64 位小写十六进制。"""
    if not isinstance(digest, str) or not _DIGEST_FULL.match(digest):
        raise LedgerError("摘要编号格式无效")
    return digest


def _safe_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """明细只允许短文本/数字/布尔，防止把回包或隐私写进账本。"""
    clean: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        ok_value = (
            value is None
            or isinstance(value, (bool, int))
            or (isinstance(value, str) and _SAFE_TEXT.match(value))
        )
        if not isinstance(key, str) or not _SAFE_TEXT.match(key) or not ok_value:
            raise LedgerError("账本明细只允许事件名、状态、数字错误码和时间")
        clean[key] = value
    return clean


class Ledger:
    """以 root 目录为账本根；每个 digest 一个 JSON 文件和一个锁文件。"""

    def __init__(self, root: Any) -> None:
        if fcntl is None:
            raise LedgerUnavailable("当前平台不支持账本互斥（需要 macOS 或 Linux），写命令已拒绝执行")
        self.root = Path(root)
        self.tightened_parent = False
        self._held: Dict[Tuple[str, str], int] = {}
        self._ensure_root()

    # --- 目录与文件 ---
    def _ensure_root(self) -> None:
        root = self.root
        if os.path.islink(root):
            raise LedgerError("账本目录不能是符号链接")
        parent = root.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
        if os.path.islink(parent):
            raise LedgerError("账本上级目录不能是符号链接")
        if stat.S_IMODE(os.lstat(parent).st_mode) & 0o077:
            os.chmod(parent, 0o700)
            self.tightened_parent = True
        if not os.path.lexists(root):
            os.mkdir(root, 0o700)
        info = os.lstat(root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise LedgerError("账本目录无效或不属于当前用户")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(root, 0o700)

    def _path(self, digest: str, suffix: str = ".json") -> Path:
        return self.root / (check_digest(digest) + suffix)

    def _fsync_dir(self) -> None:
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _open_nofollow(self, path: Path, flags: int, mode: int = 0o600) -> int:
        try:
            return os.open(path, flags | _O_NOFOLLOW, mode)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LedgerError("账本文件不能是符号链接") from None
            raise

    @contextlib.contextmanager
    def locked(self, digest: str) -> Iterator[None]:
        """对该 digest 持有独占 flock（独立实例/线程/进程之间互斥）。"""
        fd = self._open_nofollow(self._path(digest, ".lock"), os.O_CREAT | os.O_RDWR)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_raw(self, digest: str) -> Dict[str, Any]:
        path = self._path(digest)
        try:
            fd = self._open_nofollow(path, os.O_RDONLY)
        except FileNotFoundError:
            raise LedgerError("未找到该摘要的账本记录") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise LedgerCorrupt("账本损坏：不是普通文件，已拒绝执行")
            if info.st_size > LEDGER_MAX_BYTES:
                raise LedgerCorrupt("账本损坏：文件超过 1 MB 上限，已拒绝执行")
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, 0o600)
            data = os.read(fd, LEDGER_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > LEDGER_MAX_BYTES:
            raise LedgerCorrupt("账本损坏：文件超过 1 MB 上限，已拒绝执行")
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise LedgerCorrupt("账本损坏：JSON 无法解析，已拒绝执行且不会覆盖") from None
        if (
            not isinstance(record, dict)
            or record.get("schema") != LEDGER_SCHEMA
            or record.get("digest") != digest
            or not isinstance(record.get("steps"), dict)
            or any(
                not isinstance(record["steps"].get(name), dict)
                or record["steps"][name].get("state") not in states
                for name, states in STEP_STATES.items()
            )
        ):
            raise LedgerCorrupt("账本损坏：结构不符，已拒绝执行且不会覆盖")
        return record

    def _write(self, digest: str, record: Dict[str, Any], *, exclusive: bool = False) -> None:
        """同目录临时文件 + fsync + replace + 目录 fsync；中途失败不留半文件。"""
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
        if len(payload) > LEDGER_MAX_BYTES:
            raise LedgerError("账本记录超过 1 MB 上限，拒绝写入")
        final = self._path(digest)
        if exclusive:
            fd = self._open_nofollow(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(final)
                raise
            os.close(fd)
            self._fsync_dir()
            return
        tmp = self.root / (".%s.%d.%s.tmp" % (digest[:12], os.getpid(), secrets.token_hex(4)))
        fd = self._open_nofollow(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._fsync_dir()

    # --- 对外接口 ---
    def create(
        self,
        digest: str,
        *,
        basket_key: str,
        created_at: str,
        expires_at: str,
        options: Optional[Dict[str, Any]] = None,
        summary: Optional[Dict[str, Any]] = None,
        material: Optional[Dict[str, Any]] = None,
        rebuild: Optional[Dict[str, Any]] = None,
        supersedes: Optional[str] = None,
        claim_required: bool = True,
    ) -> Dict[str, Any]:
        """O_EXCL 创建记录；已存在则报错，不覆盖。"""
        check_digest(digest)
        record = {
            "schema": LEDGER_SCHEMA,
            "digest": digest,
            "basket_key": basket_key,
            "created_at": created_at,
            "expires_at": expires_at,
            "options": options or {},
            "summary": summary or {},
            "material": material or {},
            "rebuild": rebuild or {},
            "supersedes": supersedes,
            "superseded_by": None,
            "steps": {
                "claim": self._blank_write_step("none" if claim_required else "not_required"),
                "requote": {"state": "none", "checked_at": None, "changes": []},
                "order": dict(
                    self._blank_write_step("none"), order_id=None, status_label=None
                ),
            },
            "events": [{"at": _iso(_now()), "event": "summary_created"}],
        }
        with self.locked(digest):
            try:
                self._write(digest, record, exclusive=True)
            except FileExistsError:
                raise LedgerError("该摘要的账本记录已存在") from None
        return copy.deepcopy(record)

    @staticmethod
    def _blank_write_step(state: str) -> Dict[str, Any]:
        return {
            "state": state, "attempt_id": None, "started_at": None, "finished_at": None,
            "detail": {}, "channel": None,
        }

    def read(self, digest: str) -> Dict[str, Any]:
        """读取并（必要时）恢复 pending；损坏则拒绝。"""
        check_digest(digest)
        with self.locked(digest):
            return self._recover(digest, self._read_raw(digest))

    def resolve(self, prefix: str) -> str:
        """12–64 位小写十六进制前缀 → 唯一完整 digest。"""
        if not isinstance(prefix, str) or not _DIGEST_PREFIX.match(prefix):
            raise LedgerError("摘要编号格式无效（需 12–64 位小写十六进制）")
        found = sorted(
            p.name[:-5]
            for p in self.root.glob(prefix + "*.json")
            if _DIGEST_FULL.match(p.name[:-5])
        )
        if not found:
            raise LedgerError("未找到该摘要的账本记录")
        if len(found) > 1:
            raise LedgerError("摘要编号前缀不唯一，请提供更长的编号")
        return found[0]

    def mutate(self, digest: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """在锁内读-改-写（供摘要取代、重新比价等非写调用步骤使用）。"""
        check_digest(digest)
        with self.locked(digest):
            record = self._recover(digest, self._read_raw(digest))
            change(record)
            self._write(digest, record)
            return copy.deepcopy(record)

    def add_event(self, digest: str, event: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """追加事件（只存事件名与安全明细）。"""
        entry = dict(_safe_detail(detail), at=_iso(_now()), event=_safe_detail({"e": event})["e"])
        self.mutate(digest, lambda rec: rec["events"].append(entry))

    def _recover(self, digest: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """pending 且持有者已退出（owner 锁可取得）→ 就地改写为 unknown。"""
        changed = False
        for name in WRITE_STEPS:
            step = record["steps"][name]
            if step["state"] != "pending" or self._is_held(digest, step.get("attempt_id")):
                continue
            owner = self._open_nofollow(self._path(digest, ".owner"), os.O_CREAT | os.O_RDWR)
            try:
                try:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue  # 另一进程仍持有：调用仍在进行，保持 pending
                step["state"] = "unknown"
                step["finished_at"] = _iso(_now())
                step["detail"] = dict(step.get("detail") or {}, recovered_from_pending=True)
                record["events"].append(
                    {"at": _iso(_now()), "event": "recovered_from_pending", "step": name}
                )
                changed = True
            finally:
                os.close(owner)
        if changed:
            self._write(digest, record)
        return record

    def _is_held(self, digest: str, attempt_id: Any) -> bool:
        return (digest, attempt_id) in self._held

    def begin_step(
        self,
        digest: str,
        step: str,
        expect: Iterable[str],
        preconditions: Optional[Dict[str, Iterable[str]]] = None,
        channel: str = CHANNEL_LOOPBACK,
    ) -> AttemptId:
        """当前状态须在 expect 内且前置条件满足；写 pending 并 fsync，返回执行句柄。"""
        check_digest(digest)
        if step not in WRITE_STEPS:
            raise LedgerError("未知的账本步骤")
        if channel not in CHANNELS:
            raise LedgerError("未知的账本通道")
        expect = tuple(expect)
        with self.locked(digest):
            record = self._recover(digest, self._read_raw(digest))
            current = record["steps"][step]["state"]
            if current not in expect:
                raise LedgerStateError("该步骤已有执行记录，拒绝重复执行", current)
            for other, allowed in (preconditions or {}).items():
                if other not in STEP_STATES or record["steps"][other]["state"] not in tuple(allowed):
                    raise LedgerStateError("前置步骤尚未满足，拒绝执行", record["steps"][other]["state"])
            attempt = AttemptId(secrets.token_hex(8), digest, step)
            # 先占 owner 锁（崩溃后由内核释放），再落 pending
            owner = self._open_nofollow(self._path(digest, ".owner"), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(owner)
                raise LedgerStateError("该摘要正在被另一个进程执行", "pending") from None
            try:
                entry = record["steps"][step]
                entry.update(
                    state="pending", attempt_id=str(attempt), started_at=_iso(_now()),
                    finished_at=None, channel=channel,
                )
                record["events"].append({"at": _iso(_now()), "event": step + "_pending"})
                self._write(digest, record)
            except BaseException:
                os.close(owner)
                raise
            self._held[(digest, str(attempt))] = owner
        return attempt

    def is_pending(self, attempt: Any) -> bool:
        """句柄是否仍对应账本里的 pending 记录。"""
        if not isinstance(attempt, AttemptId):
            return False
        try:
            with self.locked(attempt.digest):
                record = self._read_raw(attempt.digest)
        except LedgerError:
            return False
        step = record["steps"].get(attempt.step, {})
        return step.get("state") == "pending" and step.get("attempt_id") == str(attempt)

    def finish_step(
        self,
        digest: str,
        step: str,
        attempt_id: Any,
        state: str,
        detail: Optional[Dict[str, Any]] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把 pending 写成终态；句柄必须匹配。"""
        check_digest(digest)
        if step not in WRITE_STEPS or state not in STEP_STATES[step] or state in ("none", "pending"):
            raise LedgerError("账本终态无效")
        if order_id is not None and step != "order":
            raise LedgerError("订单号只能写入下单步骤")
        clean = _safe_detail(detail)
        with self.locked(digest):
            record = self._recover(digest, self._read_raw(digest))
            entry = record["steps"][step]
            if entry["state"] != "pending" or entry.get("attempt_id") != str(attempt_id):
                raise LedgerStateError("执行句柄与账本记录不符，拒绝写入终态", entry["state"])
            entry.update(state=state, finished_at=_iso(_now()), detail=clean)
            if order_id is not None:
                entry["order_id"] = order_id
            record["events"].append({"at": _iso(_now()), "event": step + "_" + state})
            try:
                self._write(digest, record)
            finally:
                owner = self._held.pop((digest, str(attempt_id)), None)
                if owner is not None:
                    os.close(owner)
            return copy.deepcopy(record)

    def release_all(self) -> None:
        """放弃本实例持有的 owner 锁（模拟进程退出；pending 将在下次读取时恢复为 unknown）。"""
        for owner in list(self._held.values()):
            with contextlib.suppress(OSError):
                os.close(owner)
        self._held.clear()

    def find_by_basket(
        self, basket_key: str, window: float, now: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """窗口（秒）内创建、篮子相同的记录列表（按创建时间升序）。"""
        moment = now or _now()
        oldest = moment - timedelta(seconds=window)
        hits = []
        for path in sorted(self.root.glob("*.json")):
            digest = path.name[:-5]
            if not _DIGEST_FULL.match(digest):
                continue
            record = self.read(digest)
            if record.get("basket_key") != basket_key:
                continue
            try:
                created = datetime.fromisoformat(record["created_at"])
            except (KeyError, ValueError, TypeError):
                raise LedgerCorrupt("账本损坏：创建时间无效，已拒绝执行") from None
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= oldest:
                hits.append(record)
        return sorted(hits, key=lambda rec: rec["created_at"])

    # --- P2：通道、全局锁、真实写计数与人工标注 ---
    @staticmethod
    def step_channel(entry: Dict[str, Any]) -> str:
        """步骤通道；P1 旧记录缺键（或为空）一律视为 loopback。"""
        value = entry.get("channel")
        return value if value in CHANNELS else CHANNEL_LOOPBACK

    @contextlib.contextmanager
    def global_locked(self) -> Iterator[None]:
        """账本级独占 flock（<root>/.global.lock）。加锁顺序固定：全局锁 → digest 锁。"""
        fd = self._open_nofollow(self.root / ".global.lock", os.O_CREAT | os.O_RDWR)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _all_records(self) -> Iterator[Dict[str, Any]]:
        for path in sorted(self.root.glob("*.json")):
            digest = path.name[:-5]
            if _DIGEST_FULL.match(digest):
                yield self.read(digest)

    @staticmethod
    def _started_within(entry: Dict[str, Any], oldest: datetime) -> bool:
        raw = entry.get("started_at")
        if not raw:
            return False
        try:
            started = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            raise LedgerCorrupt("账本损坏：开始时间无效，已拒绝执行") from None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started >= oldest

    def count_channel_writes(
        self,
        channel: str,
        step: str,
        states: Iterable[str],
        window: float,
        now: Optional[datetime] = None,
    ) -> int:
        """窗口（秒）内某通道、某写步骤处于 states 的次数；基于账本文件，跨进程有效。"""
        if step not in WRITE_STEPS or channel not in CHANNELS:
            raise LedgerError("未知的账本步骤或通道")
        oldest = (now or _now()) - timedelta(seconds=window)
        states = tuple(states)
        total = 0
        for record in self._all_records():
            entry = record["steps"][step]
            if (
                self.step_channel(entry) == channel
                and entry["state"] in states
                and self._started_within(entry, oldest)
            ):
                total += 1
        return total

    def find_official_unresolved(
        self, basket_key: str, window: float, now: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """窗口内同篮子、official 通道且结果未知（unknown/pending）的记录。"""
        oldest = (now or _now()) - timedelta(seconds=window)
        hits = []
        for record in self._all_records():
            if record.get("basket_key") != basket_key:
                continue
            for name in WRITE_STEPS:
                entry = record["steps"][name]
                if (
                    self.step_channel(entry) == "official"
                    and entry["state"] in ("unknown", "pending")
                    and self._started_within(entry, oldest)
                ):
                    hits.append(record)
                    break
        return sorted(hits, key=lambda rec: rec["created_at"])

    def resolve_unknown(self, digest: str, step: str, outcome: str) -> Dict[str, Any]:
        """人工声明 unknown 的真实结果；仅 official+unknown，写 manual_resolved 事件，不写订单号。"""
        check_digest(digest)
        target = RESOLVE_CHOICES.get((step, outcome))
        if target is None:
            raise LedgerError("人工标注的步骤或结果无效")
        with self.locked(digest):
            record = self._recover(digest, self._read_raw(digest))
            entry = record["steps"][step]
            if self.step_channel(entry) != "official" or entry["state"] != "unknown":
                raise LedgerStateError("只能标注真实通道中结果未知的步骤", entry["state"])
            entry.update(state=target, finished_at=_iso(_now()))
            entry["detail"] = dict(entry.get("detail") or {}, manual_resolved=True)
            record["events"].append(
                {"at": _iso(_now()), "event": "manual_resolved", "step": step, "outcome": outcome}
            )
            self._write(digest, record)
            return copy.deepcopy(record)
