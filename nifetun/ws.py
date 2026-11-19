"""最小 RFC6455 WebSocket 实现：只做二进制帧，不带扩展/压缩。

为什么要它：客户端到 edge 的控制连接如果直连 `源站IP:9443`，源站 IP 就暴露在
客户端配置里了。套一层 WebSocket 就能走 CDN 的 443（ESA 在 443 上终止 TLS），
源站 IP 只剩 CDN 知道。

零第三方依赖，stdlib 足够：
  * 只用 opcode BIN/CONT/CLOSE/PING/PONG，不做分片聚合（我们自己按帧发数据，
    对面也按帧发，不需要聚合）
  * 客户端发出去的帧必须加 mask（RFC 要求），服务端不加
  * mask 用整数异或（C 层一次算完），256KB 的帧也不会掉性能
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 1 << 24  # 16MB，超过就当对端发疯


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept = base64(sha1(key + GUID))"""
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")


def _mask_bytes(payload: bytes, mk: bytes) -> bytes:
    n = len(payload)
    if not n:
        return b""
    full = mk * (n // 4) + mk[: n % 4]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")


def frame(opcode: int, payload: bytes = b"", mask: bool = False) -> bytes:
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        hdr = bytes((0x80 | opcode, mbit | n))
    elif n < 65536:
        hdr = bytes((0x80 | opcode, mbit | 126)) + struct.pack("!H", n)
    else:
        hdr = bytes((0x80 | opcode, mbit | 127)) + struct.pack("!Q", n)
    if not mask:
        return hdr + payload
    mk = os.urandom(4)
    return hdr + mk + _mask_bytes(payload, mk)


class Parser:
    """增量帧解析。feed() 返回 [(opcode, fin, payload), ...]"""

    __slots__ = ("buf", "expect_mask", "max_frame")

    def __init__(self, expect_mask: bool, max_frame: int = MAX_FRAME) -> None:
        self.buf = bytearray()
        self.expect_mask = expect_mask
        self.max_frame = max_frame

    def feed(self, data: bytes) -> list[tuple[int, bool, bytes]]:
        buf = self.buf
        buf += data
        out: list[tuple[int, bool, bytes]] = []
        unpack_h = struct.unpack_from
        while True:
            n = len(buf)
            if n < 2:
                break
            b0 = buf[0]
            b1 = buf[1]
            op = b0 & 0x0F
            fin = bool(b0 & 0x80)
            masked = bool(b1 & 0x80)
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if n < 4:
                    break
                ln = unpack_h("!H", buf, 2)[0]
                off = 4
            elif ln == 127:
                if n < 10:
                    break
                ln = unpack_h("!Q", buf, 2)[0]
                off = 10
            if ln > self.max_frame:
                raise ValueError(f"ws 帧过大: {ln}")
            mk = b""
            if masked:
                if n < off + 4:
                    break
                mk = bytes(buf[off:off + 4])
                off += 4
            if n < off + ln:
                break
            payload = bytes(buf[off:off + ln])
            del buf[:off + ln]
            if mk and payload:
                payload = _mask_bytes(payload, mk)
            out.append((op, fin, payload))
        return out


class WsTransport:
    """把一个真实 Transport 伪装成 Transport，写出去的数据自动包成 WS 帧。

    MuxConn/Bridge 只用到 write/close/get_extra_info/pause_reading 这几个方法，
    所以不需要继承 asyncio.Transport。
    """

    __slots__ = ("raw", "mask", "_peer", "_closed")

    def __init__(self, raw, mask: bool, peer=None) -> None:
        self.raw = raw
        self.mask = mask
        self._peer = peer
        self._closed = False

    # --- 写 ---
    def write(self, data) -> None:
        if self._closed or not data:
            return
        self.raw.write(frame(OP_BIN, data, self.mask))

    def writelines(self, seq) -> None:
        for d in seq:
            self.write(d)

    # --- 生命周期 ---
    def is_closing(self) -> bool:
        return self._closed or self.raw.is_closing()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.raw.write(frame(OP_CLOSE, b"\x03\xe8", self.mask))
        except Exception:
            pass
        try:
            self.raw.close()
        except Exception:
            pass

    def abort(self) -> None:
        self._closed = True
        try:
            self.raw.abort()
        except Exception:
            pass

    # --- 内省 ---
    def get_extra_info(self, name, default=None):
        if name == "peername" and self._peer is not None:
            return self._peer
        return self.raw.get_extra_info(name, default)

    def set_write_buffer_limits(self, high=None, low=None) -> None:
        try:
            self.raw.set_write_buffer_limits(high, low)
        except Exception:
            pass

    def get_write_buffer_size(self) -> int:
        try:
            return self.raw.get_write_buffer_size()
        except Exception:
            return 0

    # --- 背压（透传给真实连接）---
    def pause_reading(self) -> None:
        try:
            self.raw.pause_reading()
        except Exception:
            pass

    def resume_reading(self) -> None:
        try:
            self.raw.resume_reading()
        except Exception:
            pass

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        pass


class Link:
    """把一条 WS 连接接到任意 asyncio.Protocol（这里就是 MuxConn）上。"""

    __slots__ = ("raw", "inner", "transport", "parser", "_lost")

    def __init__(self, raw, inner, mask: bool, peer=None) -> None:
        self.raw = raw
        self.inner = inner
        self.transport = WsTransport(raw, mask, peer)
        self.parser = Parser(expect_mask=not mask)
        self._lost = False

    def start(self, leftover: bytes = b"") -> None:
        self.inner.connection_made(self.transport)
        if leftover:
            self.feed(leftover)

    def feed(self, data: bytes) -> None:
        if self._lost:
            return
        try:
            events = self.parser.feed(data)
        except ValueError as e:
            self.transport.abort()
            self.inner.connection_lost(e)
            self._lost = True
            return
        for op, _fin, payload in events:
            if op == OP_BIN or op == OP_CONT:
                if payload:
                    self.inner.data_received(payload)
            elif op == OP_PING:
                try:
                    self.raw.write(frame(OP_PONG, payload, self.transport.mask))
                except Exception:
                    pass
            elif op == OP_CLOSE:
                self.transport.close()
                self.inner.connection_lost(None)
                self._lost = True
                return

    def lost(self, exc: BaseException | None) -> None:
        if self._lost:
            return
        self._lost = True
        self.transport._closed = True
        self.inner.connection_lost(exc)


# ---------- 握手 ----------


def client_request(host: str, path: str, secure: bool = True) -> bytes:
    """客户端握手请求。"""
    import os as _os

    key = base64.b64encode(_os.urandom(16)).decode("ascii")
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: nifetun\r\n"
        "\r\n"
    ).encode("ascii")


def server_reply(head: bytes) -> tuple[bytes, str] | None:
    """校验客户端的握手请求，返回 (101 响应, Sec-WebSocket-Accept) 或 None。"""
    key = ""
    upgrade = False
    for line in head.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        value = value.strip()
        if name == b"sec-websocket-key":
            key = value.decode("ascii", "ignore")
        elif name == b"upgrade" and value.lower() == b"websocket":
            upgrade = True
    if not key or not upgrade:
        return None
    acc = accept_key(key)
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {acc}\r\n"
        "\r\n"
    ).encode("ascii")
    return resp, acc
