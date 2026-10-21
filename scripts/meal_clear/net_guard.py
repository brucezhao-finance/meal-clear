"""回环地址严格校验：按解析后的 IP 判定，不做 DNS 猜测。

写路径只认 127.0.0.1 与 [::1] 字面量；localhost 仅供既有只读测试端点，
且要求 getaddrinfo 的全部结果都是回环。其余主机名一律拒绝且不做 DNS 查询。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Optional, Tuple, Union
from urllib.parse import urlsplit

from .config import OFFICIAL_ENDPOINT

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_DOTTED_QUAD = re.compile(r"\A" + _OCTET + r"(?:\." + _OCTET + r"){3}\Z")
_NETLOC = re.compile(r"\A(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+):(\d{1,5})\Z")
WRITE_HOSTS = frozenset({"127.0.0.1", "::1"})


class NetGuardError(ValueError):
    """端点不满足回环要求（文案可安全展示，不含端点原文）。"""


def _reject(reason: str) -> NetGuardError:
    return NetGuardError("端点不是严格的本机回环地址：" + reason)


def _loopback(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """展开 IPv4 映射地址后判断是否回环。"""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _localhost_all_loopback(port: int) -> bool:
    """localhost 的全部解析结果必须是回环；解析失败视为不通过。"""
    try:
        infos = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    try:
        for info in infos:
            host = str(info[4][0]).split("%", 1)[0]
            if not _loopback(ipaddress.ip_address(host)):
                return False
    except ValueError:
        return False
    return True


def parse_loopback(url: object, *, write: bool = False) -> Tuple[str, int]:
    """校验并返回 (规范主机, 端口)；不满足时抛 NetGuardError。"""
    if not isinstance(url, str) or not url.startswith("http://"):
        raise _reject("只允许 http")
    if any(c.isspace() or c == "%" or ord(c) < 32 for c in url):
        raise _reject("含空白或百分号")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _reject("格式无效") from None
    if parts.path or parts.query or parts.fragment or any(c in url for c in "?#"):
        raise _reject("不得含路径、查询或片段")
    if "@" in parts.netloc:
        raise _reject("不得含用户信息")
    match = _NETLOC.match(parts.netloc)
    if not match:
        raise _reject("缺少端口或主机格式无效")
    host, port_text = match.group(1), match.group(2)
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise _reject("端口越界")
    if host.startswith("["):
        try:
            addr6 = ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise _reject("IPv6 字面量无效") from None
        if not _loopback(addr6):
            raise _reject("IPv6 非回环")
        if write and host != "[::1]":
            raise _reject("写路径只认 127.0.0.1 与 [::1]")
        return str(addr6.ipv4_mapped or addr6), port
    if _DOTTED_QUAD.match(host):
        addr4 = ipaddress.IPv4Address(host)
        if not addr4.is_loopback:
            raise _reject("IPv4 非回环")
        if write and host != "127.0.0.1":
            raise _reject("写路径只认 127.0.0.1 与 [::1]")
        return host, port
    if host.lower() == "localhost" and not write:
        if not _localhost_all_loopback(port):
            raise _reject("localhost 未全部解析到回环")
        return "localhost", port
    raise _reject("不接受该主机写法")


def validate_loopback_url(url: object) -> str:
    """只读测试端点校验：通过则原样返回。"""
    parse_loopback(url, write=False)
    return url  # type: ignore[return-value]


def assert_write_target(url: object) -> Tuple[str, int]:
    """写目标校验：只接受 http://127.0.0.1:端口 与 http://[::1]:端口。"""
    return parse_loopback(url, write=True)


def is_loopback_url(url: object, *, write: bool = False) -> bool:
    """布尔形式，供测试和提示使用。"""
    try:
        parse_loopback(url, write=write)
    except NetGuardError:
        return False
    return True


def is_official_url(url: object) -> bool:
    """官方端点只认与常量严格相等的 str；不做规范化、大小写折叠或解析。"""
    return type(url) is str and url == OFFICIAL_ENDPOINT


def assert_official_target(url: object) -> str:
    """官方写目标校验：通过则返回常量，否则抛 NetGuardError（文案不含端点原文）。"""
    if not is_official_url(url):
        raise NetGuardError("端点不是固定的官方地址")
    return OFFICIAL_ENDPOINT
