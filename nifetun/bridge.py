"""Bridge —— 把一个真实 socket（asyncio.Transport）和一条 mux 流焊在一起。

这是**客户端和服务端共用的**核心桥：两种协议回调都在这里，热路径没有 Task/await。
  * edge 侧的子类在 `edge_proto.py`（公网 HTTP / 裸 TCP / SNI 透传）
  * agent 侧的子类在 `client.py`（连本地服务）
"""

from __future__ import annotations

import asyncio

from . import wire
from .util import nodelay


class Bridge(asyncio.Protocol):
    """socket <-> stream 的双向桥。"""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.stream = None  # mux.Stream
        self.mux = None  # mux.MuxConn
        self.paused = False  # 目标 transport 超过高水位
        self._is_ssl = False

    # ---------- asyncio.Protocol ----------
    def connection_made(self, transport) -> None:
        self.transport = transport
        # 只在健康的时候问一次：关闭中再调 get_extra_info 可能抛异常
        self._is_ssl = transport.get_extra_info("ssl_object") is not None
        try:
            transport.set_write_buffer_limits(wire.HIGH_WATER, wire.LOW_WATER)
        except (AttributeError, NotImplementedError):
            pass
        nodelay(transport)
        self.on_connected()

    def on_connected(self) -> None: ...

    def data_received(self, data: bytes) -> None:
        st = self.stream
        if st is None or st.closed:
            return
        st.mux.send_data(st, data)

    def eof_received(self) -> bool:
        st = self.stream
        if st is not None and not st.closed:
            st.mux.send_fin(st)
        # TLS transport 不支持半关闭，返回 True 只会让 asyncio 打警告
        return not self._is_ssl

    def pause_writing(self) -> None:
        self.paused = True

    def resume_writing(self) -> None:
        self.paused = False
        st = self.stream
        if st is not None and not st.closed:
            st.flush_rx()

    def connection_lost(self, exc: BaseException | None) -> None:
        self.transport = None
        st = self.stream
        if st is not None and not st.closed:
            st.mux.close_stream(st, rst=True)

    # ---------- 供 mux.Stream 调用 ----------
    def attach(self, st) -> None:
        self.stream = st
        self.mux = st.mux
        st.endpoint = self
        if st.rx:
            st.flush_rx()

    def write_payload(self, payload: bytes) -> None:
        t = self.transport
        if t is not None:
            t.write(payload)

    def pause_reading(self) -> None:
        t = self.transport
        if t is not None:
            try:
                t.pause_reading()
            except (AttributeError, RuntimeError):
                pass

    def resume_reading(self) -> None:
        t = self.transport
        if t is not None:
            try:
                t.resume_reading()
            except (AttributeError, RuntimeError):
                pass

    def on_peer_fin(self) -> None:
        """对端不再发数据了：把 rx 里剩的吐出去，再半关闭我们的写方向。"""
        st = self.stream
        if st is not None:
            st.flush_rx()
        t = self.transport
        if t is None:
            return
        try:
            if t.can_write_eof():
                t.write_eof()
                return
        except (OSError, RuntimeError, NotImplementedError):
            pass
        # TLS 等不支持半关闭：close() 会先把写缓冲刷干净再关
        try:
            t.close()
        except (OSError, RuntimeError):
            pass

    def on_peer_gone(self) -> None:
        t = self.transport
        if t is not None:
            try:
                t.close()
            except (OSError, RuntimeError):
                pass

    def on_stream_done(self) -> None:
        self.on_peer_fin()
