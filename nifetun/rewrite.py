"""把 HTML 里的根绝对路径改写成带隧道前缀的形式。

场景：前端（Vite/webpack）产物默认是 `src="/assets/x.js"`，挂在 `/t/<id>/` 下时
浏览器会去请求域名根的 `/assets/x.js` → 404。这个模块在 edge 上兜住：
只对 **text/html** 响应做一次整体重写，其他一切原样直通。

安全性/保守策略：
  * 只改 `src= / href= / action=` 且值以**单个 /** 开头的（跳过 `//host`、`http:`、`data:`、`#`）
  * 顺带改内联 CSS 里的 `url(/...)`
  * 压缩过的（gzip/br/zstd）、非 HTML、超过大小上限的：一律原样直通，绝不猜
  * 只有整块响应到齐才改，改不了就把原始字节原样吐回去（字节级回退）
"""

from __future__ import annotations

import re

HDR_MAX = 32 * 1024
BODY_MAX = 2 * 1024 * 1024

_ATTR = re.compile(rb"""(\b(?:src|href|action)\s*=\s*)(["'])(/[^"'\\\s>]*)""", re.I)
_CSS_URL = re.compile(rb"""(url\(\s*)(["']?)(/[^"')\s]+)""", re.I)

_ENC_SKIP = (b"gzip", b"br", b"zstd", b"deflate")


def rewrite_html(body: bytes, prefix: bytes) -> bytes:
    """把 `/x` 改写成 `/t/<id>/x`。prefix 形如 b"/t/abc123"。"""
    if not body or b"/" not in body:
        return body

    def sub_attr(m: re.Match) -> bytes:
        url = m.group(3)
        if url.startswith(b"//"):
            return m.group(0)
        return m.group(1) + m.group(2) + prefix + url

    def sub_css(m: re.Match) -> bytes:
        url = m.group(3)
        if url.startswith(b"//"):
            return m.group(0)
        return m.group(1) + m.group(2) + prefix + url

    out = _ATTR.sub(sub_attr, body)
    out = _CSS_URL.sub(sub_css, out)
    return out


class Rewriter:
    """逐个响应地处理 app -> 浏览器方向的数据。

    用法：把 `write_payload` 里收到的字节交给 `feed()`，`sink(bytes)` 是真正的写出。
    """

    __slots__ = ("prefix", "sink", "raw", "state", "hdr_len", "chunked", "need", "passthrough")

    def __init__(self, prefix: bytes, sink) -> None:
        self.prefix = prefix
        self.sink = sink
        self.raw = bytearray()
        self.state = "headers"  # headers -> body -> 回到 headers
        self.hdr_len = 0
        self.chunked = False
        self.need = 0
        self.passthrough = False

    # ---------- 对外 ----------
    def feed(self, payload: bytes) -> None:
        if self.passthrough or not payload:
            self.sink(payload)
            return
        self.raw += payload
        self._step()

    def flush(self) -> None:
        """流结束/出错：把攒着的东西原样吐出去，别再改了。"""
        if self.raw:
            self.sink(bytes(self.raw))
            self.raw.clear()
        self.passthrough = True

    # ---------- 内部 ----------
    def _reset(self) -> None:
        self.raw.clear()
        self.state = "headers"
        self.hdr_len = 0
        self.chunked = False
        self.need = 0

    def _give_up(self) -> None:
        self.flush()

    def _step(self) -> None:
        while True:
            if self.state == "headers":
                idx = self.raw.find(b"\r\n\r\n")
                if idx < 0:
                    if len(self.raw) > HDR_MAX:
                        self._give_up()
                    return
                self.hdr_len = idx + 4
                head = bytes(self.raw[:idx])
                if not self._want(head):
                    self._give_up()
                    return
                self.state = "body"
                continue

            body_start = self.hdr_len
            if self.chunked:
                body = self._dechunk(bytes(self.raw[body_start:]))
                if body is None:
                    if len(self.raw) > BODY_MAX:
                        self._give_up()
                    return
                self._emit(bytes(self.raw[:self.hdr_len - 4]), body)
                self.raw.clear()
                self.state = "headers"
                return
            if len(self.raw) - body_start < self.need:
                if len(self.raw) > BODY_MAX:
                    self._give_up()
                return
            body = bytes(self.raw[body_start:body_start + self.need])
            extra = bytes(self.raw[body_start + self.need:])
            self._emit(bytes(self.raw[:self.hdr_len - 4]), body)
            self._reset()
            if extra:  # keep-alive 上的下一个响应
                self.raw += extra
                if self.passthrough:
                    return
                continue
            return

    def _want(self, head: bytes) -> bool:
        """这个响应能安全重写吗？"""
        lines = head.split(b"\r\n")
        if not lines:
            return False
        parts = lines[0].split(b" ")
        if len(parts) < 2 or not parts[1].startswith(b"2"):
            return False
        headers: dict[bytes, bytes] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        ctype = headers.get(b"content-type", b"").lower()
        if b"text/html" not in ctype:
            return False
        enc = headers.get(b"content-encoding", b"").lower()
        if any(x in enc for x in _ENC_SKIP):
            return False
        if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
            self.chunked = True
            return True
        try:
            self.need = int(headers.get(b"content-length") or 0)
        except ValueError:
            return False
        return 0 < self.need <= BODY_MAX

    @staticmethod
    def _dechunk(data: bytes) -> bytes | None:
        """解 chunked（不改分块，只判断完整并拼出实体）。不完整返回 None。"""
        out = bytearray()
        i = 0
        n = len(data)
        while True:
            j = data.find(b"\r\n", i)
            if j < 0:
                return None
            try:
                size = int(data[i:j].split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return None
            i = j + 2
            if size == 0:
                # 允许 trailer，等 \r\n\r\n 或 \r\n
                if data[i:i + 2] == b"\r\n":
                    return bytes(out)
                k = data.find(b"\r\n\r\n", i)
                return bytes(out) if k >= 0 else None
            if i + size + 2 > n:
                return None
            out += data[i:i + size]
            i += size + 2

    def _emit(self, head: bytes, body: bytes) -> None:
        new_body = rewrite_html(body, self.prefix) if body else body
        out = bytearray()
        for line in head.split(b"\r\n"):
            low = line.lower()
            if low.startswith(b"transfer-encoding:") or low.startswith(b"content-length:"):
                continue
            out += line + b"\r\n"
        out += b"Content-Length: " + str(len(new_body)).encode() + b"\r\n\r\n"
        out += new_body
        self.sink(bytes(out))
