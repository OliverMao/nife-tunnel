"""edge 专有的三种"公网接入"协议实现（客户端单文件版不会包含这个文件）。

  * PublicProto  —— HTTP：按 Host 或路径前缀路由，之后纯字节转发
  * TcpProto     —— 裸 TCP：端口即路由
  * SniProto     —— HTTPS 透传：只读 ClientHello 里的 SNI，证书由内网源站自己处理
"""

from __future__ import annotations

import logging

from . import wire, ws
from .rewrite import Rewriter
from .bridge import Bridge
from .sni import MAX_HELLO, parse_sni
from .util import parse_host, peer_str

log = logging.getLogger("nifetun.edge")

_CRLF = bytes((13, 10))

_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
}


class PublicProto(Bridge):
    """edge 公网 HTTP 连接。

    两种路由方式（同一个监听上共存）：
      1. 路径前缀：`/t/<隧道ID>/...` —— 固定一个加速域名时用，**推荐**
      2. Host 头：按域名路由 —— 每个隧道一条 DNS 记录时用
    路由确定后开一条流，之后整条连接就是纯字节搬运，不再解析任何东西。
    """

    __slots__ = ("edge", "pre", "resolved", "peer", "link", "rw")

    def __init__(self, edge) -> None:
        super().__init__()
        self.edge = edge
        self.pre = b""
        self.resolved = False
        self.peer = "?"
        self.link = None
        self.rw: Rewriter | None = None

    def on_connected(self) -> None:
        self.peer = peer_str(self.transport.get_extra_info("peername"))

    def data_received(self, data: bytes) -> None:
        if self.link is not None:   # 已经是 WebSocket 客户端通道
            self.link.feed(data)
            return
        if self.resolved:
            st = self.stream
            if st is not None and not st.closed:
                st.mux.send_data(st, data)
            return

        pre = self.pre + data if self.pre else data
        idx = pre.find(b"\r\n\r\n")
        if idx < 0:
            if len(pre) > wire.MAX_HEAD:
                self._reject(431, b"header too large")
                return
            self.pre = pre
            return

        self.pre = b""
        self.resolved = True
        head = pre[:idx + 4]
        rest = pre[idx + 4:]
        host = parse_host(head)

        # --- 0) 客户端控制通道（WebSocket over 443，源站 IP 不暴露）---
        # 它不走隧道路由，所以放在 Host 白名单之前；安全性靠 token + HMAC。
        row = head.splitlines()[0].split(b" ") if head else []
        ws_target = row[1] if len(row) >= 2 else b""
        if ws_target.split(b"?", 1)[0] == self.edge.ws_path:
            self._handle_ws(head, rest)
            return

        # Host 白名单：走 CDN/ESA 时只认自己的入口域名，
        # 这样即使回源端口被直连，也进不来。
        if not self.edge.host_ok(host):
            log.info("%s Host=%s 不在白名单，403", self.peer, host)
            self._reject(403, b"host not allowed")
            return

        # --- 请求行 ---
        eol = head.find(b"\r\n")
        request_line = head[:eol] if eol > 0 else head
        parts = request_line.split(b" ")
        target = parts[1] if len(parts) >= 3 else b""

        # --- 1) 邮件审批按钮（/_nifetun/approve|deny/<nonce>）---
        if target.startswith(b"/_nifetun/"):
            self._handle_decide(target)
            return

        # --- 2) 路径前缀路由 ---
        tid = self.edge.match_prefix(target)
        if tid is not None:
            mux = self.edge.pick_id(tid)
            if mux is None:
                self._reject(404, self.edge.not_found)
                return
            self.edge.access(self.peer, self.edge.prefix + "/" + tid, request_line)
            payload = self.edge.strip_prefix_payload(head, request_line, parts, tid)
            try:
                st = mux.open_stream({"id": tid, "w": mux.our_window, "remote": self.peer})
            except Exception:
                self._reject(502, b"tunnel unavailable")
                return
            self.attach(st)
            self._enable_rewrite(tid)
            mux.send_data(st, payload)
            if rest:
                mux.send_data(st, rest)
            return

        # --- 3) Host 路由 ---
        mux = self.edge.pick(host) if host else None
        if mux is None:
            self._reject(404, self.edge.not_found)
            return
        self.edge.access(self.peer, host, request_line)
        try:
            st = mux.open_stream({"host": host, "w": mux.our_window, "remote": self.peer})
        except Exception:
            self._reject(502, b"tunnel unavailable")
            return
        self.attach(st)
        mux.send_data(st, head)
        if rest:
            mux.send_data(st, rest)

    def _enable_rewrite(self, tid: str) -> None:
        """前缀路由：把回程的 HTML 里的 /xxx 改写成 /t/<id>/xxx。"""
        if not getattr(self.edge, "prefix_rewrite", False):
            return
        if self.transport is None:
            return
        self.rw = Rewriter(f"{self.edge.prefix}/{tid}".encode(), self.transport.write)

    def write_payload(self, payload: bytes) -> None:
        if self.rw is not None:
            self.rw.feed(payload)
            return
        super().write_payload(payload)

    def on_stream_done(self) -> None:
        if self.rw is not None:
            self.rw.flush()
            self.rw = None
        super().on_stream_done()

    def _handle_ws(self, head: bytes, rest: bytes) -> None:
        """把这条 HTTP 连接升级成 WebSocket，之后就是一条普通的客户端控制连接。

        意义：客户端连的是 CDN 的 443，源站 IP 只存在 CDN 回源配置里，
        客户端配置文件和被抓包的流量里都看不到。
        """
        got = ws.server_reply(head)
        if got is None:
            self._reject(400, b"websocket handshake failed")
            return
        resp, _acc = got
        peer = self.peer
        for line in head.splitlines():
            name, _, value = line.partition(b":")
            low = name.strip().lower()
            if low in (b"x-forwarded-for", b"cf-connecting-ip", b"x-real-ip"):
                text = value.strip().decode("utf-8", "replace")
                if text:
                    peer = text.split(",")[0].strip()
        self.transport.write(resp)
        conn = self.edge.new_client_conn()
        self.link = ws.Link(self.transport, conn, mask=False, peer=peer)
        self.resolved = True  # 之后不再解析 HTTP
        log.info("%s 客户端控制通道升级为 WebSocket", peer)
        self.link.start(rest)

    def connection_lost(self, exc) -> None:
        if self.rw is not None:
            self.rw.flush()
            self.rw = None
        if self.link is not None:
            self.link.lost(exc)
        super().connection_lost(exc)

    def _handle_decide(self, target: bytes) -> None:
        """处理邮件里的批准/拒绝链接：一次性 nonce，点完即失效。"""
        path = target.split(b"?", 1)[0].decode("latin-1")
        parts = [p for p in path.split("/") if p]
        action = parts[1] if len(parts) > 1 else ""
        nonce = parts[2] if len(parts) > 2 else ""
        if action not in ("approve", "deny") or not nonce:
            self._html(404, "<h2>链接不完整</h2>")
            return
        ok, message = self.edge.decide_by_nonce(nonce, action)
        log.info("%s 点击了 %s 链接: %s", self.peer, action, message)
        color = "#0b7a3b" if ok else "#a00"
        self._html(200 if ok else 400,
                   f'<h2 style="color:{color}">{message}</h2>'
                   f'<p style="color:#666">nifetun</p>')

    def _html(self, status: int, body_html: str) -> None:
        t = self.transport
        self.resolved = True
        self.stream = None
        if t is None:
            return
        body = ("<!doctype html><meta charset=utf-8>"
                '<div style="font:16px/1.7 system-ui,sans-serif;max-width:34rem;'
                'margin:12vh auto;padding:0 1rem">' + body_html + "</div>").encode()
        head = b"".join([
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}".encode(), _CRLF,
            b"Content-Type: text/html; charset=utf-8", _CRLF,
            f"Content-Length: {len(body)}".encode(), _CRLF,
            b"Connection: close", _CRLF, _CRLF,
        ])
        try:
            t.write(head + body)
            t.close()
        except (OSError, RuntimeError):
            pass

    def _reject(self, status: int, body: bytes) -> None:
        t = self.transport
        self.resolved = True
        self.stream = None
        if t is None:
            return
        payload = body or b""
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\nServer: nifetun\r\n\r\n"
        ).encode()
        try:
            t.write(head + payload)
            t.close()
        except (OSError, RuntimeError):
            pass


class TcpProto(Bridge):
    """edge 裸 TCP 连接：端口即路由，直接绑流。"""

    __slots__ = ("edge", "key")

    def __init__(self, edge, key: str) -> None:
        super().__init__()
        self.edge = edge
        self.key = key

    def on_connected(self) -> None:
        mux = self.edge.pick(self.key)
        if mux is None:
            self.on_peer_gone()
            return
        try:
            st = mux.open_stream(
                {
                    "host": self.key,
                    "w": mux.our_window,
                    "proto": "tcp",
                    "remote": peer_str(self.transport.get_extra_info("peername")),
                }
            )
        except Exception:
            self.on_peer_gone()
            return
        self.attach(st)


class SniProto(Bridge):
    """edge HTTPS 透传：只解析 ClientHello 的 SNI 决定路由，字节流原样转给内网源站。"""

    __slots__ = ("edge", "buf", "peer")

    def __init__(self, edge) -> None:
        super().__init__()
        self.edge = edge
        self.buf = b""
        self.peer = "?"

    def on_connected(self) -> None:
        self.peer = peer_str(self.transport.get_extra_info("peername"))

    def data_received(self, data: bytes) -> None:
        if self.stream is not None:
            st = self.stream
            if not st.closed:
                st.mux.send_data(st, data)
            return

        buf = self.buf + data if self.buf else data
        host, done = parse_sni(buf)
        if not done:
            if len(buf) > MAX_HELLO:
                log.info("%s ClientHello 超过 %dB，丢弃", self.peer, MAX_HELLO)
                self.on_peer_gone()
                return
            self.buf = buf
            return

        self.buf = b""
        if not host:
            log.info("%s 不是可识别的 TLS ClientHello（或没有 SNI），丢弃", self.peer)
            self.on_peer_gone()
            return
        mux = self.edge.pick(host)
        if mux is None:
            log.info("%s SNI=%s 没有对应隧道", self.peer, host)
            self.on_peer_gone()
            return
        try:
            st = mux.open_stream(
                {"host": host, "w": mux.our_window, "proto": "sni", "remote": self.peer}
            )
        except Exception:
            self.on_peer_gone()
            return
        self.edge.access(self.peer, host, b"TLS / " + host.encode())
        self.attach(st)
        mux.send_data(st, buf)
