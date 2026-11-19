"""管理 API（默认只监听 127.0.0.1）。

  GET  /api/health          -> {"ok":true}
  GET  /api/status          -> 隧道/连接/流 的实时快照
  POST /api/kick            -> {"host":"a.b"} 或 {"peer":"1.2.3.4:5678"}，踢掉对应 agent
查询串里可以带 ?token=xxx 代替 Authorization 头，方便 curl（注意别留在 shell 历史里）。
"""

from __future__ import annotations

import hmac
import logging
import time
from urllib.parse import unquote

from . import jsonx

log = logging.getLogger("nifetun.admin")

_encode = jsonx.encode
_decode = jsonx.decode



def _query(query: str) -> dict:
    out = {}
    for item in query.split("&"):
        k, _, v = item.partition("=")
        if k:
            out[k] = unquote(v)
    return out


def _now() -> float:
    return time.time()


class Admin:
    def __init__(self, edge, token: str | None):
        self.edge = edge
        self.token = token or None
        self.started = time.time()

    async def handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (ConnectionError, OSError):
            writer.close()
            return
        try:
            body = await self._dispatch(head)
        except Exception as e:  # noqa: BLE001
            log.warning("admin 请求出错: %r", e)
            body = self._json(500, {"error": "internal"})
        try:
            writer.write(body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    # ---------- 路由 ----------
    async def _dispatch(self, head: bytes) -> bytes:
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return self._json(400, {"error": "bad request"})
        method, target = parts[0], parts[1]
        path, _, query = target.partition("?")
        headers = {}
        for line in lines[1:]:
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        if not self._authorized(headers, query):
            return self._json(401, {"error": "unauthorized"})
        if method != "GET" and method != "POST":
            return self._json(405, {"error": "method not allowed"})

        if path == "/api/health":
            return self._json(200, {"ok": True})
        if path == "/api/status":
            return self._json(200, self.edge.snapshot(self.started))
        if path == "/api/kick" and method == "POST":
            return self._json(200, {"kicked": self.edge.kick(query)})
        q = _query(query)
        if path == "/api/pending":
            return self._json(200, {
                "pending": [
                    {"client": n, "label": p.get("label"), "peer": p["peer"],
                     "waiting_s": round(_now() - p["at"], 1)}
                    for n, p in self.edge.pending.items()
                ]
            })
        if path == "/api/approve" and method == "POST":
            name = q.get("client", "") or q.get("uuid", "")
            if name and name not in self.edge.pending:
                # 允许按 label（主机名）批准
                hit = [u for u, p in self.edge.pending.items() if p.get("label") == name]
                if len(hit) == 1:
                    name = hit[0]
            try:
                token = self.edge.approve(name, q.get("ids"), q.get("hosts"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"approved": name, "token": token,
                                    "ids": q.get("ids") or "*"})
        if path == "/api/deny" and method == "POST":
            who = q.get("client", "") or q.get("uuid", "")
            if who and who not in self.edge.pending:
                hit = [u for u, p in self.edge.pending.items() if p.get("label") == who]
                who = hit[0] if len(hit) == 1 else who
            return self._json(200, {"denied": self.edge.deny(who), })
        return self._json(404, {"error": "not found"})

    def _authorized(self, headers: dict, query: str) -> bool:
        if self.token is None:
            return True
        given = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            for item in query.split("&"):
                k, _, v = item.partition("=")
                if k == "token":
                    given = v
                    break
        return hmac.compare_digest(given, self.token)

    @staticmethod
    def _json(status: int, payload: dict) -> bytes:
        body = _encode(payload)
        head = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        return head + body
