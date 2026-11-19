"""零碎工具：TCP_NODELAY、Host 解析。热路径上不要在这里做多余的事。"""

from __future__ import annotations

import socket


def nodelay(transport) -> None:
    sock = transport.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def peer_str(peer) -> str:
    if isinstance(peer, (tuple, list)) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer or "?")


def _strip_port(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 字面量
        return host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def parse_host(head: bytes) -> str | None:
    """从 HTTP 请求头取 Host（小写、去端口）。只扫前若干个头，不做完整解析。"""
    lines = head.decode("latin-1").split("\r\n")
    for line in lines[1:]:
        if not line:
            break
        if len(line) < 6:
            continue
        if line[0] in "Hh" and line[:5].lower() == "host:":
            return _strip_port(line[5:]) or None
    parts = lines[0].split()
    if len(parts) >= 2 and parts[1].lower().startswith("http://"):  # 绝对形式
        return _strip_port(parts[1][7:].split("/", 1)[0]) or None
    return None
