"""edge（跑在公网服务器上的那一端）。

客户端接入认证流程（token 不明文过网、nonce 一次性防重放）：

    edge  → 客户端   {"type":"challenge","nonce":"<32 字节随机>"}
    客户端 → edge    {"type":"auth","client":"<名字>","proof":"HMAC-SHA256(token, nonce)",
                      "w":<接收窗口>,"tunnels":[...]}
    edge  → 客户端   {"type":"registered","ids":[...],"hosts":[...],"w":...}

每个客户端一个 token，只能认领自己白名单里的 id / 域名。认证通过后，
edge 维护两张路由表（按路径 ID、按域名），公网请求按 least-conn 分流到某条连接。
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import time

from . import jsonx, notify, wire
from .admin import Admin
from .edge_proto import PublicProto, SniProto, TcpProto
from .config import addr as parse_addr
from .mux import MuxConn

log = logging.getLogger("nifetun.edge")

_encode = jsonx.encode

#: 路径前缀里的隧道 ID：小写字母/数字/下划线/中划线，最长 64
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEFAULT_404 = (
    "<!doctype html><meta charset=utf-8><title>nifetun</title>"
    "<style>body{font:16px/1.7 system-ui,sans-serif;margin:12vh auto;max-width:38rem;"
    "padding:0 1rem;color:#333}h1{font-size:1.3rem;font-weight:600}</style>"
    "<h1>404 — 这里没有隧道</h1>"
    "<p>该地址没有已注册的隧道，或者对应的客户端当前不在线。</p>"
).encode()


def make_server_ssl(cert, key) -> ssl.SSLContext | None:
    if not cert or not key:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


class ClientConn(MuxConn):
    """一条已建立的客户端连接（未认证前只能做 challenge/auth）。"""

    def __init__(self, edge: "Edge", window: int):
        super().__init__(window)
        self.edge = edge
        self.hosts: list[str] = []
        self.ids: list[str] = []
        self.client_name = ""
        self.authed = False
        self.connected_at = time.monotonic()
        self._nonce: bytes | None = None
        self._hs_timer: asyncio.TimerHandle | None = None

    # ---------- 握手 ----------
    def on_connected(self) -> None:
        self._nonce = secrets.token_bytes(32)
        self.write_frame(
            0, wire.CTRL, _encode({"type": "challenge", "nonce": base64.b64encode(self._nonce).decode()})
        )
        self._hs_timer = self._loop.call_later(self.edge.handshake_timeout, self._on_hs_timeout)

    def _on_hs_timeout(self) -> None:
        if not self.authed:
            log.warning("%s 认证超时", self.peer)
            self.close()

    def on_ctrl(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "enroll":
            self._enroll(msg)
        elif kind == "auth":
            self._auth(msg)
        elif kind == "register":
            if not self.authed:  # 未认证不接受任何注册
                self._fail("not authenticated")
                return
            self._apply_tunnels(msg)
        elif kind == "ping":
            self.write_frame(0, wire.CTRL, b'{"type":"pong"}')

    def _enroll(self, msg: dict) -> None:
        """客户端申请接入。

        身份 = **服务端分配的 UUID**（主机名可能重名，绝不能当身份用）；
        `label`（主机名/备注）只用于邮件和日志显示，不参与唯一性。
        """
        if not self.edge.enroll_enabled:
            self._fail("enrollment disabled on server")
            return
        uuid = str(msg.get("uuid") or msg.get("client") or "").strip().lower()
        label = str(msg.get("label") or "").strip()[:64]
        if uuid and not ID_RE.match(uuid):
            self._fail("bad client uuid")
            return
        if uuid and self.edge.find_client(uuid) is not None:
            self._fail("client already registered")
            return
        if not uuid:
            uuid = self.edge.new_uuid()      # 老客户端没 uuid 的就新发一个
        self._nonce = None  # 申请模式下不需要 challenge
        if self._hs_timer is not None:
            self._hs_timer.cancel()
            self._hs_timer = None
        self.edge.add_pending(uuid, self, label)
        self.write_frame(0, wire.CTRL, _encode({
            "type": "enroll_pending", "uuid": uuid, "client": uuid, "label": label,
            "note": "等待审批",
        }))
        # 批准前的兜底超时
        self._hs_timer = self._loop.call_later(self.edge.enroll_timeout, self.close)

    def _fail(self, reason: str) -> None:
        log.warning("%s 认证失败: %s", self.peer, reason)
        try:
            self.write_frame(0, wire.CTRL, _encode({"type": "error", "msg": reason}))
        except Exception:
            pass
        self.close()

    def _auth(self, msg: dict) -> None:
        if self.authed or self._nonce is None:
            self._fail("unexpected auth")
            return
        name = str(msg.get("client") or "default").strip()
        cred = self.edge.find_client(name)
        if cred is None:
            self._fail("unknown client")
            return
        nonce, self._nonce = self._nonce, None  # 一次性
        try:
            given = base64.b64decode(str(msg.get("proof") or ""), validate=True)
        except Exception:
            self._fail("bad proof encoding")
            return
        expect = hmac.new(str(cred["token"]).encode(), nonce, hashlib.sha256).digest()
        if not hmac.compare_digest(expect, given):
            self._fail("bad proof")
            return

        self.client_name = name
        self._allowed_ids = cred.get("ids") or self.edge.allowed_id_patterns
        self._allowed_hosts = cred.get("hosts") or self.edge.allowed_patterns
        self.authed = True
        self.edge.authenticated(self)
        if self._hs_timer is not None:
            self._hs_timer.cancel()
            self._hs_timer = None
        log.info("%s 客户端 '%s' 认证通过", self.peer, name)
        self._apply_tunnels(msg)

    # ---------- 注册隧道（首次 + 热重载都走这里）----------
    def _apply_tunnels(self, msg: dict) -> None:
        try:
            self.peer_window = int(msg.get("w") or self.peer_window)
        except (TypeError, ValueError):
            pass

        denied: list[str] = []
        hosts: list[str] = []
        ids: list[str] = []
        assigned: list[dict] = []
        for t in msg.get("tunnels") or []:
            tid = str(t.get("id") or "").strip().lower()
            host = "" if tid else str(t.get("host") or "").strip().lower().rstrip(".")
            local = str(t.get("local") or "?")
            if tid:
                # 客户端带回来的是上次服务端分配的 ID
                if not ID_RE.match(tid) or not _match_any(tid, self._allowed_ids):
                    denied.append(tid)
                    continue
                if not self.edge.claim_id(tid, self.client_name):
                    log.warning("%s 试图复用别人的隧道 ID %s，拒绝", self.peer, tid)
                    denied.append(tid)
                    continue
                if tid not in ids:
                    ids.append(tid)
                    assigned.append({"id": tid, "local": local})
                    log.info("%s 隧道 %s/%s -> %s（沿用）", self.peer, self.edge.prefix, tid, local)
            elif host:
                if not _match_any(host, self._allowed_hosts):
                    denied.append(host)
                    continue
                if host not in hosts:
                    hosts.append(host)
                    log.info("%s 认领域名 %s -> %s", self.peer, host, local)
            else:
                # 既没 id 也没 host：服务端分配一个
                try:
                    tid = self.edge.allocate_id(self.client_name, local)
                except RuntimeError as e:
                    log.error("%s", e)
                    continue
                ids.append(tid)
                assigned.append({"id": tid, "local": local})
                log.info("%s 分配隧道 %s/%s -> %s", self.peer, self.edge.prefix, tid, local)

        add_h = [h for h in hosts if h not in self.hosts]
        del_h = [h for h in self.hosts if h not in hosts]
        add_i = [i for i in ids if i not in self.ids]
        del_i = [i for i in self.ids if i not in ids]
        if del_h:
            self.edge.remove_routes(self, del_h)
            log.info("%s 撤下域名 %s", self.peer, ", ".join(del_h))
        if del_i:
            self.edge.remove_id_routes(self, del_i)
            log.info("%s 撤下路径 %s", self.peer, ", ".join(del_i))
        if add_h:
            self.edge.add_routes(self, add_h)
        if add_i:
            self.edge.add_id_routes(self, add_i)
        self.hosts = hosts
        self.ids = ids
        self.write_frame(
            0,
            wire.CTRL,
            _encode({
                "type": "registered",
                "hosts": hosts,
                "ids": ids,
                "denied": denied,
                "w": self.our_window,
                "prefix": self.edge.prefix,
                "tunnels": assigned,
                "public_hosts": self.edge.prefix_hosts,
            }),
        )

    def on_stream_opened(self, st, info: dict) -> None:
        self.close_stream(st, rst=True)  # edge 不接对端开流

    def on_lost(self, exc: BaseException | None) -> None:
        if self._hs_timer is not None:
            self._hs_timer.cancel()
            self._hs_timer = None
        self.edge.unregister(self)
        log.info("%s 断开 (域名=%s 路径=%s)", self.peer,
                 ",".join(self.hosts) or "-", ",".join(self.ids) or "-")


def _match_any(value: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


class Edge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.window = int(cfg.get("window") or wire.DEFAULT_WINDOW)

        # --- 客户端凭证 ---
        self.clients_cfg = [c for c in (cfg.get("clients") or []) if isinstance(c, dict)]
        self.has_clients = bool(self.clients_cfg)
        legacy = str(cfg.get("token") or "")
        enroll = bool(cfg.get("enroll", False))
        if not self.has_clients and (not legacy or legacy == "change-me") and not enroll                 and not cfg.get("clients_file"):
            raise SystemExit(
                "config: 必须配置 clients（每客户端一个 token）或用 token 配单 token 模式\n"
                "        生成 token: uv run tunneld.py --gen-token"
            )
        self.legacy = {
            "name": "default",
            "token": legacy,
            "ids": cfg.get("allowed_ids") or ["*"],
            "hosts": cfg.get("allowed_hosts") or ["*"],
        }
        self.allowed_patterns = [str(p).lower() for p in (cfg.get("allowed_hosts") or ["*"])]
        self.allowed_id_patterns = [str(p).lower() for p in (cfg.get("allowed_ids") or ["*"])]

        # --- 公网入口 ---
        self.prefix = "/" + str(cfg.get("prefix") or "t").strip("/")
        self.strip_prefix = bool(cfg.get("strip_prefix", True))
        self.prefix_hosts = [str(h).lower().rstrip(".") for h in (cfg.get("prefix_hosts") or [])]

        not_found = cfg.get("not_found_html") or ""
        if not_found:
            self.not_found = not_found.encode()
        else:
            hint = f"<p>也可以走路径前缀：<code>{self.prefix}/&lt;隧道ID&gt;/</code></p>".encode()
            self.not_found = DEFAULT_404.replace(b"</p>", hint + b"</p>", 1)

        self.handshake_timeout = float(cfg.get("handshake_timeout") or 10)
        self.enroll_timeout = float(cfg.get("enroll_timeout") or 600)
        self.email_cfg = dict(cfg.get("email") or {})
        self.email_enabled = bool(self.email_cfg.get("enabled"))
        self.idle_timeout = float(cfg.get("idle_timeout") or 90)
        self.backlog = int(cfg.get("backlog") or 2048)
        self.access_log = bool(cfg.get("access_log", True))
        self.ws_path = str(cfg.get("ws_path") or "/_nifetun/ws").encode()
        self.id_owner: dict[str, str] = {}
        #: "客户端|本地地址" -> 隧道 ID，保证同一客户端的多条连接拿到同一个 URL
        self.id_by_local: dict[str, str] = {}
        self.id_len = max(6, int(cfg.get("id_len") or 12))
        self.pending: dict[str, dict] = {}
        self.enroll_enabled = bool(cfg.get("enroll", False))
        self.routes: dict[str, list[ClientConn]] = {}
        self.routes_by_id: dict[str, list[ClientConn]] = {}
        self.clients: set[ClientConn] = set()
        self._servers: list[asyncio.AbstractServer] = []
        self._tasks: list[asyncio.Task] = []
        self._admin: Admin | None = None

    # ---------- 客户端凭证 ----------
    def new_client_conn(self) -> "ClientConn":
        return ClientConn(self, self.window)

    def find_client(self, name: str) -> dict | None:
        for c in self.clients_cfg:
            if str(c.get("name")) == name:
                return c if c.get("token") else None
        if not self.has_clients and self.legacy["token"]:
            return self.legacy  # 单 token 模式：所有客户端共用
        return None

    # ---------- 隧道 ID 分配 ----------
    def load_ids(self) -> None:
        path = self.cfg.get("ids_file")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("读 %s 失败: %r", path, e)
            return
        if isinstance(data, dict) and "owner" in data:
            self.id_owner = {str(k): str(v) for k, v in (data.get("owner") or {}).items()}
            self.id_by_local = {str(k): str(v) for k, v in (data.get("local") or {}).items()}
        elif isinstance(data, dict):  # 老格式：{id: owner}
            self.id_owner = {str(k): str(v) for k, v in data.items()}
        log.info("从 %s 载入 %d 个隧道 ID 归属", path, len(self.id_owner))

    def save_ids(self) -> None:
        path = self.cfg.get("ids_file")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"owner": self.id_owner, "local": self.id_by_local},
                          fh, ensure_ascii=False, indent=2, sort_keys=True)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.error("写 %s 失败: %r", path, e)

    def allocate_id(self, owner: str, local: str = "") -> str:
        """由服务端分配隧道 ID（客户端不决定 URL 里那段）。

        同一个客户端的同一个本地地址，**永远拿到同一个 ID** —— 连接池里 4 条连接
        同时注册、客户端重连、进程重启都一样，否则 URL 会一次冒出一堆。
        """
        key = f"{owner}|{local}" if local else ""
        if key:
            known = self.id_by_local.get(key)
            if known and self.id_owner.get(known) == owner:
                return known
        for _ in range(64):
            tid = secrets.token_hex(8)[: self.id_len]
            if tid in self.id_owner:
                continue
            self.id_owner[tid] = owner
            if key:
                self.id_by_local[key] = tid
            self.save_ids()
            return tid
        raise RuntimeError("隧道 ID 分配失败（空间耗尽？）")

    def claim_id(self, tid: str, owner: str) -> bool:
        """客户端带着上次分配的 ID 回来：只允许原主人复用。"""
        cur = self.id_owner.get(tid)
        if cur is None:
            self.id_owner[tid] = owner
            self.save_ids()
            return True
        return cur == owner

    # ---------- 申请 / 审批 ----------
    def new_uuid(self) -> str:
        """服务端分配客户端 UUID（16 位十六进制，64 bit 熵，不会重）。"""
        while True:
            uuid = secrets.token_hex(8)
            if uuid not in self.clients_cfg and uuid not in self.pending:
                return uuid

    def add_pending(self, uuid: str, conn: "ClientConn | None", label: str = "") -> None:
        cur = self.pending.get(uuid)
        if cur is not None:
            # 同一个 uuid 又来了（客户端重连/重启）：换掉连接，不重复发邮件
            cur["conn"] = conn
            cur["peer"] = conn.peer if conn else cur["peer"]
            if label:
                cur["label"] = label
            log.info("客户端 %s (%s) 重新申请接入，沿用待审批条目", uuid, cur["peer"])
            return
        self.pending[uuid] = {
            "at": time.time(),
            "peer": conn.peer if conn else "?",
            "conn": conn,
            "label": label or "?",
            "nonce": secrets.token_urlsafe(24),
        }
        log.info("客户端 %s（%s，来自 %s）申请接入，等待批准",
                 uuid, label or "?", conn.peer if conn else "?")
        if self.email_enabled:
            asyncio.create_task(self.notify_pending(uuid))

    # ---------- 邮件审批 ----------
    def pending_by_nonce(self, nonce: str) -> str | None:
        for name, p in self.pending.items():
            if hmac.compare_digest(str(p.get("nonce") or ""), nonce):
                return name
        return None

    async def notify_pending(self, name: str) -> None:
        p = self.pending.get(name)
        if not p:
            return
        base = str(self.email_cfg.get("approve_base") or "").rstrip("/")
        if not base:
            log.warning("email.approve_base 没配，无法生成审批链接")
            return
        nonce = p["nonce"]
        subject, html = notify.enroll_mail(
            client=name,
            label=str(p.get("label") or "?"),
            peer=p["peer"],
            approve_url=f"{base}/_nifetun/approve/{nonce}",
            deny_url=f"{base}/_nifetun/deny/{nonce}",
            expires_min=int(self.enroll_timeout // 60),
            extra={"隧道前缀": self.prefix + "/<ID>/"},
        )
        ok = await notify.send_async(self.email_cfg, subject, html)
        if ok:
            log.info("审批邮件已发给 %s", ", ".join(self.email_cfg.get("to") or []))
        else:
            log.error("审批邮件发送失败，可用 /api/approve?client=%s 手动批准", name)

    def decide_by_nonce(self, nonce: str, action: str) -> tuple[bool, str]:
        """按钮点进来：批准/拒绝。返回 (是否成功, 提示文案)。"""
        name = self.pending_by_nonce(nonce)
        if name is None:
            return False, "链接无效或已过期（可能已经用过，或申请已超时）"
        if action == "deny":
            self.deny(name)
            return True, f"已拒绝客户端「{name}」的接入申请"
        try:
            self.approve(name, None, None)
        except ValueError as e:
            return False, f"批准失败：{e}"
        return True, f"已批准客户端「{name}」，token 已直接下发到它那边，可以关闭本页"

    def approve(self, name: str, ids: str | None, hosts: str | None) -> str:
        """批准一个申请：现场生成 token，推给等着的客户端，并持久化。"""
        name = name.strip()
        if not ID_RE.match(name):
            raise ValueError("非法客户端名")
        if self.find_client(name) is not None:
            raise ValueError("该客户端已存在")
        token = secrets.token_urlsafe(32)
        pending = self.pending.pop(name, None) or {}
        entry = {
            "name": name,
            "label": str(pending.get("label") or ""),
            "token": token,
            "ids": [x for x in (ids or "*").split(",") if x] or ["*"],
            "hosts": [x for x in (hosts or "").split(",") if x] or [],
        }
        self.clients_cfg.append(entry)
        self.has_clients = True
        conn = pending.get("conn")
        if conn is not None and not conn.closed:
            conn.write_frame(0, wire.CTRL, _encode({
                "type": "enroll_ok", "uuid": name, "client": name, "token": token,
                "ids": entry["ids"], "hosts": entry["hosts"], "prefix": self.prefix,
            }))
            log.info("已把凭证推送给 '%s' (%s)", name, conn.peer)
        else:
            log.warning("'%s' 已批准，但申请方已离线；token 见 clients 存储", name)
        self._persist_clients()
        return token

    def deny(self, name: str) -> bool:
        p = self.pending.pop(name, None)
        if p and p.get("conn") is not None and not p["conn"].closed:
            try:
                p["conn"].write_frame(0, wire.CTRL, _encode({
                    "type": "error", "msg": "enroll denied by admin"}))
            except Exception:
                pass
        log.info("已拒绝 '%s' 的申请", name)
        return p is not None

    def _persist_clients(self) -> None:
        path = self.cfg.get("clients_file")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.clients_cfg, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            log.info("客户端凭证已写入 %s", path)
        except OSError as e:
            log.error("写 %s 失败: %r", path, e)

    def authenticated(self, conn: ClientConn) -> None:
        self.clients.add(conn)

    # ---------- 路由 ----------
    def host_ok(self, host: str | None) -> bool:
        if not self.prefix_hosts:
            return True
        return host is not None and _match_any(host, self.prefix_hosts)

    def add_routes(self, conn: ClientConn, hosts) -> None:
        for host in hosts:
            bucket = self.routes.setdefault(host, [])
            if conn not in bucket:
                bucket.append(conn)

    def add_id_routes(self, conn: ClientConn, ids) -> None:
        for tid in ids:
            bucket = self.routes_by_id.setdefault(tid, [])
            if conn not in bucket:
                bucket.append(conn)

    def remove_routes(self, conn: ClientConn, hosts) -> None:
        for host in hosts:
            bucket = self.routes.get(host)
            if not bucket or conn not in bucket:
                continue
            bucket.remove(conn)
            if not bucket:
                self.routes.pop(host, None)

    def remove_id_routes(self, conn: ClientConn, ids) -> None:
        for tid in ids:
            bucket = self.routes_by_id.get(tid)
            if not bucket or conn not in bucket:
                continue
            bucket.remove(conn)
            if not bucket:
                self.routes_by_id.pop(tid, None)

    def unregister(self, conn: ClientConn) -> None:
        self.clients.discard(conn)
        self.remove_routes(conn, conn.hosts)
        self.remove_id_routes(conn, conn.ids)

    @staticmethod
    def _least(bucket: list[ClientConn] | None) -> ClientConn | None:
        """least-conn：选正在跑的流最少的那条连接。"""
        if not bucket:
            return None
        if len(bucket) == 1:
            return bucket[0]
        best = bucket[0]
        best_n = len(best.streams)
        for conn in bucket[1:]:
            n = len(conn.streams)
            if n < best_n:
                best, best_n = conn, n
        return best

    def pick(self, host: str) -> ClientConn | None:
        return self._least(self.routes.get(host))

    def pick_id(self, tid: str) -> ClientConn | None:
        return self._least(self.routes_by_id.get(tid))

    def access(self, peer: str, tag: str, head: bytes) -> None:
        if not self.access_log:
            return
        nl = head.find(b"\r\n")
        line = head[:nl].decode("latin-1", "replace") if nl > 0 else ""
        log.info("%s %s %s", peer, tag, line)

    # ---------- 路径前缀 ----------
    def match_prefix(self, target: bytes) -> str | None:
        """`/t/<id>/rest` -> id；不是前缀形式则返回 None（走域名路由）。"""
        base = self.prefix.encode() + b"/"
        if not target.startswith(base):
            return None
        rest = target[len(base):]
        for i, ch in enumerate(rest):
            if ch in (0x2F, 0x3F, 0x23):  # '/' '?' '#'
                rest = rest[:i]
                break
        if not rest:
            return None
        try:
            tid = rest.decode("ascii").lower()
        except UnicodeDecodeError:
            return None
        return tid if ID_RE.match(tid) else None

    def strip_prefix_payload(self, head: bytes, request_line: bytes, parts: list, tid: str) -> bytes:
        """把请求行里的 `/t/<id>` 去掉，并补一个 X-Forwarded-Prefix。"""
        if not self.strip_prefix:
            return head
        cut = len(self.prefix) + 1 + len(tid)
        rest = parts[1][cut:]
        if not rest.startswith(b"/"):
            rest = b"/" + rest
        new_line = b" ".join((parts[0], rest, *parts[2:]))
        tail = head[len(request_line) + 2:]
        return b"%s\r\nX-Forwarded-Prefix: %s/%s\r\n%s" % (
            new_line, self.prefix.encode(), tid.encode(), tail,
        )

    # ---------- 管理 API ----------
    def snapshot(self, started: float) -> dict:
        clients = [
            {
                "peer": c.peer,
                "client": c.client_name,
                "hosts": c.hosts,
                "ids": c.ids,
                "streams": len(c.streams),
                "connected_s": round(time.monotonic() - c.connected_at, 1),
            }
            for c in self.clients
        ]
        return {
            "uptime_s": round(time.time() - started, 1),
            "public_listen": self.cfg.get("public_listen"),
            "client_listen": self.cfg.get("client_listen"),
            "prefix": self.prefix,
            "public_hosts": self.prefix_hosts,
            "clients": clients,
            "routes": {h: [c.peer for c in b] for h, b in self.routes.items()},
            "routes_by_id": {i: [c.peer for c in b] for i, b in self.routes_by_id.items()},
            "pending": [
                {"client": n, "label": p.get("label"), "peer": p["peer"],
                 "waiting_s": round(time.time() - p["at"], 1)}
                for n, p in self.pending.items()
            ],
            "streams_total": sum(len(c.streams) for c in self.clients),
        }

    def kick(self, query: str) -> int:
        """按 host / id / peer / client 踢掉连接，返回踢掉的连接数。"""
        host = tid = peer = name = ""
        for item in query.split("&"):
            k, _, v = item.partition("=")
            if k == "host":
                host = v.strip().lower().rstrip(".")
            elif k == "id":
                tid = v.strip().lower()
            elif k == "peer":
                peer = v.replace("%3A", ":").replace("%3a", ":").strip()
            elif k == "client":
                name = v.strip()
        n = 0
        for conn in list(self.clients):
            if (
                (host and host in conn.hosts)
                or (tid and tid in conn.ids)
                or (peer and conn.peer == peer)
                or (name and conn.client_name == name)
                or (not host and not tid and not peer and not name)
            ):
                conn.close()
                n += 1
        if n:
            log.info("admin 踢掉 %d 条客户端连接 (host=%s id=%s client=%s)",
                     n, host or "-", tid or "-", name or "-")
        return n

    # ---------- 启动 ----------
    def load_clients_file(self) -> None:
        path = self.cfg.get("clients_file")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                extra = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("读 %s 失败: %r", path, e)
            return
        known = {str(c.get("name")) for c in self.clients_cfg}
        for c in extra if isinstance(extra, list) else []:
            if isinstance(c, dict) and str(c.get("name")) not in known:
                self.clients_cfg.append(c)
                known.add(str(c.get("name")))
        self.has_clients = bool(self.clients_cfg)
        log.info("从 %s 载入 %d 个客户端凭证", path, len(self.clients_cfg))

    async def start(self) -> None:
        self.load_clients_file()
        self.load_ids()
        loop = asyncio.get_running_loop()
        client_ssl = make_server_ssl(self.cfg.get("client_tls_cert"), self.cfg.get("client_tls_key"))
        public_ssl = make_server_ssl(self.cfg.get("public_tls_cert"), self.cfg.get("public_tls_key"))
        if public_ssl is not None:
            public_ssl.set_alpn_protocols(["http/1.1"])

        ahost, aport = parse_addr(self.cfg["client_listen"])
        self._servers.append(
            await loop.create_server(
                lambda: ClientConn(self, self.window),
                ahost, aport, ssl=client_ssl, backlog=256, reuse_address=True,
            )
        )
        log.info("客户端接入 %s:%d%s", ahost, aport, " [TLS]" if client_ssl else " ⚠ 明文")

        phost, pport = parse_addr(self.cfg["public_listen"])
        self._servers.append(
            await loop.create_server(
                lambda: PublicProto(self),
                phost, pport, ssl=public_ssl, backlog=self.backlog, reuse_address=True,
            )
        )
        log.info("公网入口 %s:%d%s", phost, pport, " [TLS]" if public_ssl else "")
        if self.prefix_hosts:
            log.info("只接受 Host: %s", ", ".join(self.prefix_hosts))

        for item in self.cfg.get("tcp_listeners") or []:
            lhost, lport = parse_addr(item["listen"])
            mode = str(item.get("mode") or "tcp").lower()
            key = str(item.get("host") or "").strip().lower()
            ctx = make_server_ssl(item.get("tls_cert"), item.get("tls_key"))
            if mode == "sni":
                factory = lambda: SniProto(self)  # noqa: E731
                label = "HTTPS 透传 (按 SNI 路由)"
            else:
                if not key:
                    raise SystemExit(f"config: tcp_listeners[{item['listen']}] 缺 host")
                factory = lambda k=key: TcpProto(self, k)  # noqa: E731
                label = f"TCP -> {key}"
            self._servers.append(
                await loop.create_server(
                    factory, lhost, lport, ssl=ctx, backlog=256, reuse_address=True,
                )
            )
            log.info("%-28s %s:%d%s", label, lhost, lport, " [TLS]" if ctx else "")

        admin_listen = self.cfg.get("admin_listen")
        if admin_listen:
            self._admin = Admin(self, self.cfg.get("admin_token"))
            dhost, dport = parse_addr(admin_listen)
            self._servers.append(
                await asyncio.start_server(
                    self._admin.handle, dhost, dport, backlog=64, reuse_address=True
                )
            )
            log.info("管理 API %s:%d%s", dhost, dport,
                     " [需 token]" if self.cfg.get("admin_token") else " [无鉴权，仅限本机]")

        self._tasks.append(asyncio.create_task(self._watchdog()))

    async def serve_forever(self) -> None:
        await asyncio.gather(*(s.serve_forever() for s in self._servers))

    async def _watchdog(self) -> None:
        interval = max(5.0, self.idle_timeout / 3)
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(interval)
                now = loop.time()
                for conn in list(self.clients):
                    if now - conn.last_rx > self.idle_timeout:
                        log.warning("%s 静默 %.0fs，断开", conn.peer, now - conn.last_rx)
                        conn.close()
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for s in self._servers:
            s.close()
        for conn in list(self.clients):
            conn.close()


__all__ = ["Edge", "ClientConn", "make_server_ssl"]
