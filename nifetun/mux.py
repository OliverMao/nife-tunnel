"""多路复用连接：asyncio.Protocol 实现，热路径全同步、无 StreamReader 开销。

关键性能设计：
  * 收发都在 `data_received` 里同步完成，不 await、不排队、不建 Task（避免每 chunk 一次的调度开销）；
  * 背压分三层：源 socket 的 `pause_reading()` ← 流信用窗口 ← 目标 transport 的写水位；
  * 每帧只做一次 `header + payload` 拼接，`transport.write()` 直接进内核缓冲。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from . import jsonx, wire

log = logging.getLogger("nifetun.mux")

_encode = jsonx.encode
_decode = jsonx.decode


class Stream:
    """一条逻辑流（一个方向一个信用窗口）。"""

    __slots__ = ("sid", "mux", "endpoint", "credit", "pending", "rx",
                 "fin_tx", "fin_sent", "fin_rx", "closed", "unacked", "ack_threshold")

    def __init__(self, sid: int, mux: "MuxConn", credit: int):
        self.sid = sid
        self.mux = mux
        self.endpoint = None  # Bridge（真实 socket）
        self.credit = credit  # 对端给的发送窗口（已收到但未用掉的额度）
        self.pending = b""  # 因信用不足而积压的待发字节
        self.rx: deque[bytes] = deque()  # 已收到但目标 socket 还吃不下
        self.fin_tx = False  # 本方向已决定不再发数据
        self.fin_sent = False
        self.fin_rx = False
        self.closed = False
        # 窗口更新攒着发：阈值取 window/4，保证对端信用永远 >= 3/4 窗口，不会卡住
        self.ack_threshold = max(16 * 1024, mux.our_window // 4)
        self.unacked = 0

    # ---------- 收：mux -> 本地 socket ----------
    def ack(self, n: int) -> None:
        self.unacked += n
        if self.unacked >= self.ack_threshold:
            self.mux.send_wu(self.sid, self.unacked)
            self.unacked = 0

    def flush_ack(self) -> None:
        if self.unacked:
            self.mux.send_wu(self.sid, self.unacked)
            self.unacked = 0

    def feed(self, payload: bytes) -> None:
        ep = self.endpoint
        if ep is None or ep.paused:
            self.rx.append(payload)  # 先扣着，等目标可写再把信用还回去
            return
        n = len(payload)
        ep.write_payload(payload)
        if ep.paused:
            self.rx.append(payload)
        else:
            self.ack(n)

    def flush_rx(self) -> None:
        ep = self.endpoint
        if ep is None or ep.paused:
            return
        rx = self.rx
        while rx:
            payload = rx[0]
            ep.write_payload(payload)
            if ep.paused:
                return
            rx.popleft()
            self.ack(len(payload))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Stream #{self.sid} credit={self.credit} rx={len(self.rx)}>"


class MuxConn(asyncio.Protocol):
    """双向多路复用连接。edge 与 agent 共用，差异靠子类钩子。"""

    def __init__(self, window: int = wire.DEFAULT_WINDOW):
        self.transport: asyncio.Transport | None = None
        self.streams: dict[int, Stream] = {}
        self.peer_window = window
        self.our_window = window
        self.peer = "?"
        self._next_sid = 0
        self._buf = b""
        self._paused = False
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self.last_rx = self._loop.time()
        self.closed_event: asyncio.Event = asyncio.Event()
    # ================= asyncio.Protocol =================
    def connection_made(self, transport) -> None:
        self.transport = transport
        peer = transport.get_extra_info("peername")
        if isinstance(peer, (tuple, list)) and len(peer) >= 2:
            self.peer = f"{peer[0]}:{peer[1]}"
        else:
            self.peer = str(peer or "?")
        try:
            transport.set_write_buffer_limits(wire.HIGH_WATER, wire.LOW_WATER)
        except (AttributeError, NotImplementedError):
            pass
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                import socket as _socket

                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
        self.on_connected()

    def data_received(self, data: bytes) -> None:
        self.last_rx = self._loop.time()
        buf = self._buf + data if self._buf else data
        off = 0
        end = len(buf)
        unpack = wire.HDR.unpack_from
        hsize = wire.HDR_SIZE
        dispatch = self._dispatch
        try:
            while end - off >= hsize:
                sid, ftype, length = unpack(buf, off)
                if length > wire.MAX_FRAME:
                    raise ValueError(f"frame too large: {length}")
                nxt = off + hsize + length
                if nxt > end:
                    break
                dispatch(sid, ftype, buf[off + hsize:nxt] if length else b"")
                off = nxt
        finally:
            self._buf = buf[off:] if off < end else b""

    def pause_writing(self) -> None:
        # 对端读得慢：把各流的源 socket 一起掐住
        self._paused = True
        for st in self.streams.values():
            ep = st.endpoint
            if ep is not None:
                ep.pause_reading()

    def resume_writing(self) -> None:
        self._paused = False
        for st in self.streams.values():
            ep = st.endpoint
            if ep is not None and not st.pending:
                ep.resume_reading()

    def connection_lost(self, exc) -> None:
        self._closed = True
        for st in list(self.streams.values()):
            st.closed = True
            ep = st.endpoint
            if ep is not None:
                ep.on_peer_gone()
        self.streams.clear()
        self.closed_event.set()
        self.on_lost(exc)

    # ================= 子类钩子 =================
    def on_connected(self) -> None: ...

    def on_lost(self, exc: BaseException | None) -> None: ...

    def on_stream_opened(self, st: Stream, info: dict) -> None:
        # 默认：不接受对端开流（edge 侧）
        self.close_stream(st, rst=True)

    def on_ctrl(self, msg: dict) -> None: ...

    # ================= 帧分发 =================
    def _dispatch(self, sid: int, ftype: int, payload: bytes) -> None:
        if ftype == wire.DATA:
            st = self.streams.get(sid)
            if st is not None:
                st.feed(payload)
            return
        if ftype == wire.WU:
            st = self.streams.get(sid)
            if st is not None:
                st.credit += wire.U32.unpack(payload)[0]
                self._pump(st)
            return
        if ftype == wire.FIN:
            st = self.streams.get(sid)
            if st is not None:
                st.fin_rx = True
                st.flush_ack()
                ep = st.endpoint
                if ep is not None:
                    ep.on_peer_fin()
                self._maybe_retire(st)
            return
        if ftype == wire.RST:
            st = self.streams.get(sid)
            if st is not None:
                self.close_stream(st, rst=False)
            return
        if ftype == wire.OPEN:
            try:
                info = _decode(payload) if payload else {}
            except Exception:
                info = {}
            if not isinstance(info, dict) or sid in self.streams:
                return
            try:
                credit = int(info.get("w") or self.peer_window)
            except (TypeError, ValueError):
                credit = self.peer_window
            st = Stream(sid, self, credit)
            self.streams[sid] = st
            self.on_stream_opened(st, info)
            return
        if ftype == wire.CTRL:
            try:
                msg = _decode(payload) if payload else {}
            except Exception:
                return
            if isinstance(msg, dict):
                self.on_ctrl(msg)

    # ================= 发 =================
    def write_frame(self, sid: int, ftype: int, payload: bytes = b"") -> None:
        t = self.transport
        if t is not None and not self._closed:
            t.write(wire.HDR.pack(sid, ftype, len(payload)))
            if payload:
                t.write(payload)

    def write_data(self, sid: int, data: bytes) -> None:
        t = self.transport
        if t is None or self._closed:
            return
        n = len(data)
        if n <= wire.MAX_CHUNK:
            # 分两次 write：省掉 header+payload 的那一次整块 memcpy
            t.write(wire.HDR.pack(sid, wire.DATA, n))
            t.write(data)
            return
        for i in range(0, n, wire.MAX_CHUNK):
            chunk = data[i:i + wire.MAX_CHUNK]
            t.write(wire.HDR.pack(sid, wire.DATA, len(chunk)))
            t.write(chunk)

    def send_wu(self, sid: int, n: int) -> None:
        self.write_frame(sid, wire.WU, wire.U32.pack(n))

    # ================= 流操作 =================
    def open_stream(self, info: dict) -> Stream:
        """主动开流（edge）。同步，保证 OPEN 一定先于该流的任何 DATA。"""
        self._next_sid += 1
        sid = self._next_sid
        st = Stream(sid, self, self.peer_window)
        self.streams[sid] = st
        self.write_frame(sid, wire.OPEN, _encode(info))
        return st

    def send_data(self, st: Stream, data: bytes) -> None:
        n = len(data)
        credit = st.credit
        if st.closed:
            return
        if n <= credit:
            st.credit = credit - n
            self.write_data(st.sid, data)
            return
        if credit > 0:
            self.write_data(st.sid, data[:credit])
            st.credit = 0
        st.pending += data[credit:]  # 剩下的等 WU
        ep = st.endpoint
        if ep is not None:
            ep.pause_reading()
        else:
            self._ensure_buffer_limit(st)

    def _ensure_buffer_limit(self, st: Stream) -> None:
        # 没有 socket 可掐时（比如 agent 正在连本地），只能靠 RST 保命
        if len(st.pending) > 8 * self.peer_window:
            log.warning("stream #%d 上游积压过大，重置", st.sid)
            self.close_stream(st, rst=True)

    def _pump(self, st: Stream) -> None:
        pend = st.pending
        if not pend:
            if st.fin_tx and not st.fin_sent:
                self._send_fin(st)
            return
        credit = st.credit
        if credit <= 0:
            return
        take = len(pend) if len(pend) <= credit else credit
        st.pending = pend[take:]
        st.credit = credit - take
        self.write_data(st.sid, pend if take == len(pend) else pend[:take])
        if not st.pending:
            if st.fin_tx and not st.fin_sent:
                self._send_fin(st)
            ep = st.endpoint
            if ep is not None and not self._paused:
                ep.resume_reading()

    def send_fin(self, st: Stream) -> None:
        if st.fin_tx:
            return
        st.fin_tx = True
        if not st.pending:
            self._send_fin(st)

    def _send_fin(self, st: Stream) -> None:
        if st.fin_sent or st.closed:
            return
        st.fin_sent = True
        self.write_frame(st.sid, wire.FIN)
        self._maybe_retire(st)

    def close_stream(self, st: Stream, rst: bool = False) -> None:
        if st.closed:
            return
        st.closed = True
        self.streams.pop(st.sid, None)
        if rst and not st.fin_sent:
            self.write_frame(st.sid, wire.RST)
        ep = st.endpoint
        if ep is not None:
            ep.on_peer_gone()

    def _maybe_retire(self, st: Stream) -> None:
        """两个方向都结束了就收摊。"""
        if st.closed:
            return
        if st.fin_rx and st.fin_tx and not st.pending and not st.rx:
            st.closed = True
            st.flush_ack()
            self.streams.pop(st.sid, None)
            ep = st.endpoint
            if ep is not None:
                ep.on_stream_done()

    # ================= 便利 =================
    @property
    def closed(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        await self.closed_event.wait()

    def close(self) -> None:
        t = self.transport
        if t is not None and not self._closed:
            try:
                t.close()
            except Exception:
                pass
