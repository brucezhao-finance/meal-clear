"""订单相关测试用：禁止任何非回环网络连接（非回环 connect 直接抛错）。"""
from __future__ import annotations

import contextlib
import ipaddress
import socket
from typing import Any, Iterator
from unittest.mock import patch


class NetBlocked(AssertionError):
    """测试中出现了非回环网络连接。"""


def _is_loopback_host(host: Any) -> bool:
    try:
        addr = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _check(address: Any) -> None:
    if isinstance(address, tuple) and address and _is_loopback_host(address[0]):
        return
    if isinstance(address, (str, bytes)) and not isinstance(address, tuple):
        return  # AF_UNIX 本机套接字
    raise NetBlocked("测试禁止非回环连接：%r" % (address if not isinstance(address, tuple) else address[:1],))


@contextlib.contextmanager
def block_network() -> Iterator[None]:
    """上下文内非回环 connect / create_connection 一律抛 NetBlocked。"""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def connect(self: socket.socket, address: Any) -> Any:
        _check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        _check(address)
        return real_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0]
        if not _is_loopback_host(host) and host != "localhost":
            raise NetBlocked("测试禁止非回环连接：%r" % (host,))
        return real_create(address, *args, **kwargs)

    with patch.object(socket.socket, "connect", connect), patch.object(
        socket.socket, "connect_ex", connect_ex
    ), patch.object(socket, "create_connection", create_connection):
        yield
