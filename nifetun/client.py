"""客户端（跑在内网机器上）。

对 edge 开 N 条连接（`connections`，默认 4）：同一隧道 ID 的多条连接构成一个池，
edge 按 least-stream 分流，于是单连接的 HOL 被摊薄，收发也能吃到更多内核并行度。

认证：收到 edge 的 challenge 后回 `HMAC-SHA256(token, nonce)`，**token 不明文过网**；
TLS 全自动：连域名按系统 CA 校验，连 IP 首次连接自动锁定证书指纹（TOFU）。
断线单槽独立重连，不影响其它槽。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import ssl

from . import config, jsonx, wire, ws
from .bridge import Bridge
from .config import addr as parse_addr
from .mux import MuxConn

log = logging.getLogger("nifetun.client")

_encode = jsonx.encode

#: 路径前缀里的隧道 ID 规则（和 edge 侧一致）
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _mtime(path: str | None) -> float | None:
    if not path:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _parse_tunnels(cfg: dict) -> list[dict]:
    """每条隧道只需给 local；id/host 都可以不写。

    * 写了 host  -> 按域名路由（每隧道一条 DNS 记录的场景）
    * 写了 id    -> 上次服务端分配的 ID，带回来说明 URL 不换
    * 都不写     -> 服务端分配 ID，客户端落盘后以后一直用这个
    """
    specs: list[dict] = []
    for t in cfg.get("tunnels") or []:
        if isinstance(t, str):  # 最简写法："tunnels": ["127.0.0.1:8080"]
            t = {"local": t}
        tid = str(t.get("id") or "").strip().lower()
        host = "" if tid else str(t.get("host") or "").strip().lower().rstrip(".")
        if tid and not ID_RE.match(tid):
            raise SystemExit(f"config: 非法的隧道 id {tid!r}（只允许小写字母/数字/_/-，最长 64）")
        local_raw = str(t["local"])
        if not local_raw:
            raise SystemExit(f"config: tunnel 缺少 local: {t}")
        specs.append({
            "local": parse_addr(local_raw),
            "local_s": local_raw,
            "id": tid or None,
            "host": host or None,
            "label": str(t.get("label") or ""),
        })
    if not specs:
        raise SystemExit("config: tunnels 不能为空")
    return specs


def _hostname() -> str:
    """显示用的名字（审批邮件里能认出是哪台机器）。主机名可能重名，所以不当身份。"""
    import socket as _socket

    try:
        raw = _socket.gethostname().split(".")[0]
    except OSError:
        raw = "client"
    return (re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-")[:48] or "client")


B_HDR_END = chr(13).encode() + chr(10).encode() + chr(13).encode() + chr(10).encode()

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _is_ip(host: str) -> bool:
    return bool(_IPV4.match(host)) or ":" in host


def make_client_ssl(verify: bool) -> ssl.SSLContext:
    """客户端固定用 TLS。

    * 连域名（走 CDN 443）→ 正常按系统 CA 校验（CDN 的公网证书）
    * 连 IP（直连源站）→ 自签证书没法按 CA 校验，改用 TOFU 指纹锁定
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_servers(cfg: dict) -> list[dict]:
    """`servers` 支持三种写法，全部不需要写任何 TLS 参数：

        https://nife-tunnel.yoobit.cn      → WebSocket over 443（穿过 CDN，源站 IP 不暴露）
        wss://nife-tunnel.yoobit.cn/xx     → 同上，可自定义路径
        <服务器IP>:9443 / tls://host:p  → 直连（自签证书，首次连接自动锁定指纹）
    """
    raw = cfg.get("servers") or ([cfg["server"]] if cfg.get("server") else [])
    if not raw:
        raise SystemExit("config: servers (或 server) 必需")
    out: list[dict] = []
    for item in raw:
        text = str(item).strip()
        low = text.lower()
        if low.startswith(("https://", "wss://", "http://", "ws://")):
            secure = low.startswith(("https://", "wss://"))
            body = text.split("://", 1)[1]
            hostport, _, path = body.partition("/")
            host, port = _split_hostport(hostport, 443 if secure else 80)
            out.append({"kind": "ws", "secure": secure, "host": host, "port": port,
                        "path": "/" + path if path else str(cfg.get("ws_path") or "/_nifetun/ws"),
                        "url": text.rstrip("/")})
        elif low.startswith(("tls://", "tcp://")):
            secure = low.startswith("tls://")
            host, port = _split_hostport(text.split("://", 1)[1], 9443 if secure else 9443)
            out.append({"kind": "tcp", "secure": secure, "host": host, "port": port,
                        "path": "", "url": f"{host}:{port}"})
        else:
            host, port = _split_hostport(text, 9443)
            out.append({"kind": "tcp", "secure": True, "host": host, "port": port,
                        "path": "", "url": f"{host}:{port}"})
    return out


def _split_hostport(text: str, default_port: int) -> tuple[str, int]:
    text = text.strip()
    if text.startswith("["):  # [::1]:443
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port else default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, int(port or default_port)
    if ":" in text:  # 纯 IPv6
        return text, default_port
    return text, default_port


class LocalProto(Bridge):
    """客户端侧：连到本地服务的那个 socket。"""

    __slots__ = ("_pending_stream",)

    def __init__(self, stream) -> None:
        super().__init__()
        self._pending_stream = stream

    def on_connected(self) -> None:
        st = self._pending_stream
        self._pending_stream = None
        if st is None or st.closed:
            self.on_peer_gone()
            return
        self.attach(st)


class ServerConn(MuxConn):
    """客户端侧的一条到 edge 的连接。"""

    def __init__(self, client: "Client", window: int):
        super().__init__(window)
        self.client = client
        self.srv: dict = {}
        self.authed = False
        self.auth_failed = False  # token/白名单问题：别疯狂重连
        self._hb: asyncio.Task | None = None
        self._hs: asyncio.TimerHandle | None = None

    # ---------- 握手 ----------
    def on_connected(self) -> None:
        if not self._check_pin():
            self.close()
            return
        if not self.client.token:
            if not self.client.begin_enroll():
                self.close()   # 别的槽正在申请，等它拿到 uuid 再说
                return
            # 没有凭证 → 走申请流程（只发 label，不发任何秘密）
            self.write_frame(0, wire.CTRL, _encode({
                "type": "enroll", "uuid": self.client.uuid or "",
                "label": self.client.label,
            }))
            if not self.client.enrolled_note:
                self.client.enrolled_note = True
                log.info("已提交接入申请（%s），等待管理员批准…", self.client.label)
            self._hs = self._loop.call_later(900, self.close)

    def _on_enroll_ok(self, msg: dict) -> None:
        self.client.adopt_credentials(msg)
        log.info("申请已批准，正在用新凭证重新连接")
        self.close()

    def _check_pin(self) -> bool:
        """连域名 -> CDN 公网证书（按系统 CA 校验）；连 IP -> TOFU 指纹锁定。"""
        obj = self.transport.get_extra_info("ssl_object") if self.transport else None
        host = str((self.srv or {}).get("host") or "")
        if obj is None:
            if self.srv.get("secure", True):
                log.error("当前是明文连接，拒绝发送凭据")
                return False
            return True
        if host and not _is_ip(host):
            return True  # CDN 证书已按 CA 校验，不需要再钉指纹
        try:
            got = hashlib.sha256(obj.getpeercert(binary_form=True)).hexdigest()
        except (ValueError, ssl.SSLError) as e:
            log.error("读取服务器证书失败: %r", e)
            return False
        pin = self.client.peer_pin
        if not pin:
            self.client.save_pin(got)
            return True
        if not hmac.compare_digest(got, pin):
            log.error("服务器证书指纹与首次记录不一致（可能被中间人）")
            log.error("  期望 %s", pin)
            log.error("  实际 %s", got)
            return False
        return True

    def on_ctrl(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "enroll_pending":
            uuid = str(msg.get("uuid") or "").strip().lower()
            if uuid and uuid != self.client.uuid:
                self.client.uuid = uuid
                self.client._enroll_claim = 0.0
                self.client.save_identity()
                log.info("服务端分配的客户端 UUID: %s", uuid)
            log.info("%s", msg.get("note") or "等待管理员批准")
        elif kind == "enroll_ok":
            self._on_enroll_ok(msg)
        elif kind == "challenge":
            if not self.client.token:
                return  # 申请模式：还没凭证，忽略 challenge，等 enroll_ok 推下来
            self._answer_challenge(msg)
        elif kind == "registered":
            self._on_registered(msg)
        elif kind == "error":
            self.auth_failed = True
            log.error("edge 拒绝: %s（检查客户端的 token 和 id/host 白名单）", msg.get("msg"))
            self.close()
        elif kind == "ping":
            self.write_frame(0, wire.CTRL, b'{"type":"pong"}')

    def _answer_challenge(self, msg: dict) -> None:
        try:
            nonce = base64.b64decode(str(msg.get("nonce") or ""), validate=True)
        except Exception:
            log.error("challenge 格式不对")
            self.close()
            return
        proof = hmac.new(self.client.token.encode(), nonce, hashlib.sha256).digest()
        self.write_frame(
            0,
            wire.CTRL,
            _encode({
                "type": "auth",
                "client": self.client.uuid,
                "proof": base64.b64encode(proof).decode(),
                "w": self.our_window,
                "tunnels": self.client.tunnel_payload(),
            }),
        )
        if self._hb is None:
            self._hb = self._loop.create_task(self._heartbeat())

    def _on_registered(self, msg: dict) -> None:
        first = not self.authed
        self.authed = True
        try:
            self.peer_window = int(msg.get("w") or self.peer_window)
        except (TypeError, ValueError):
            pass
        denied = msg.get("denied") or []
        hosts = msg.get("hosts") or []
        ids = msg.get("ids") or []
        if first or msg.get("tunnels"):
            self.client.apply_assignment(msg.get("tunnels"), msg.get("prefix"),
                                          msg.get("public_hosts"))
        extra = ""
        if ids:
            extra = f" [路径: {', '.join(ids)}]"
        if hosts:
            extra += f" [域名: {', '.join(hosts)}]"
        log.info("%s %d 条隧道%s%s",
                 "已注册" if first else "已更新",
                 len(hosts) + len(ids), extra,
                 f"（被拒绝: {', '.join(denied)}）" if denied else "")

    def send_register(self) -> None:
        """热重载用：已认证后再推一次隧道列表。"""
        if not self.authed:
            return
        self.write_frame(
            0, wire.CTRL, _encode({"type": "register", "tunnels": self.client.tunnel_payload()})
        )

    async def _heartbeat(self) -> None:
        interval = self.client.heartbeat
        dead = self.client.dead_after
        try:
            while True:
                await asyncio.sleep(interval)
                if self._loop.time() - self.last_rx > dead:
                    log.warning("%s 静默超时 %.0fs，重连", self.peer, dead)
                    self.close()
                    return
                self.write_frame(0, wire.CTRL, b'{"type":"ping"}')
        except asyncio.CancelledError:
            pass

    def on_stream_opened(self, st, info: dict) -> None:
        key = str(info.get("id") or info.get("host") or "").strip().lower()
        local = self.client.by_key.get(key)
        if local is None:
            log.warning("edge 要求未知隧道: %s", key)
            self.close_stream(st, rst=True)
            return
        self.client.connect_local(self, st, local)

    def on_lost(self, exc: BaseException | None) -> None:
        if self._hb is not None:
            self._hb.cancel()
            self._hb = None


class WsClientProto(asyncio.Protocol):
    """客户端侧：先做 WebSocket 握手，成功后的字节流交给 ServerConn。

    这样客户端只跟 CDN 的 443 说话，源站 IP 不出现在客户端配置里。
    """

    def __init__(self, client: "Client", srv: dict) -> None:
        self.client = client
        self.srv = srv
        self.raw = None
        self.buf = b""
        self.link = None
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def connection_made(self, transport) -> None:
        self.raw = transport
        host = self.srv["host"]
        if self.srv["port"] not in (443, 80):
            host = f"{host}:{self.srv['port']}"
        transport.write(ws.client_request(host, self.srv["path"], self.srv["secure"]))

    def data_received(self, data: bytes) -> None:
        if self.link is not None:
            self.link.feed(data)
            return
        self.buf += data
        idx = self.buf.find(B_HDR_END)
        if idx < 0:
            if len(self.buf) > 8192:
                self._fail("握手响应过大")
            return
        head = self.buf[:idx + 4]
        rest = self.buf[idx + 4:]
        self.buf = b""
        first = head.splitlines()[0] if head else b""
        if b" 101 " not in first:
            self._fail(f"WebSocket 握手失败: {first[:120]!r}")
            return
        mux = ServerConn(self.client, self.client.window)
        mux.srv = self.srv
        self.link = ws.Link(self.raw, mux, mask=True)
        self.link.start(rest)
        if not self.done.done():
            self.done.set_result(mux)

    def _fail(self, why: str) -> None:
        if not self.done.done():
            self.done.set_exception(RuntimeError(why))
        if self.raw is not None:
            self.raw.close()

    def connection_lost(self, exc) -> None:
        if self.link is not None:
            self.link.lost(exc)
        if not self.done.done():
            self.done.set_exception(ConnectionError(exc or "连接被关闭"))


class Client:
    def __init__(self, cfg: dict, config_path: str | None = None):
        self.cfg = cfg
        self.token = "" if str(cfg.get("token") or "") == "change-me" else str(cfg.get("token") or "")
        base = os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()
        self.credentials_file = str(cfg.get("credentials_file") or os.path.join(base, "nifetun-credentials.json"))
        self.enrolled_note = False
        #: 身份：配置里显式给了就用（手工预置的场景），否则用凭证里服务端分配的 uuid，
        #: 都没有就留空 -> 首次 enroll 由服务端分配。**绝不用主机名当身份**。
        self.uuid = str(cfg.get("client") or cfg.get("uuid") or "").strip().lower()
        self._enroll_claim: float = 0.0
        #: 显示名（主机名/备注），只在邮件和日志里出现，不参与唯一性
        self.label = str(cfg.get("label") or "").strip() or _hostname()

        self.servers = _parse_servers(cfg)

        self.specs = _parse_tunnels(cfg)
        self.by_key: dict[str, tuple[str, int]] = {}
        self.state_file = str(cfg.get("tunnel_state_file") or os.path.join(base, "nifetun-ids.json"))
        self.public_hosts: list[str] = []

        self.connections = max(1, int(cfg.get("connections") or 1))
        self.window = int(cfg.get("window") or wire.DEFAULT_WINDOW)
        self.heartbeat = float(cfg.get("heartbeat") or 15)
        self.dead_after = float(cfg.get("dead_after") or 60)
        self.max_delay = float(cfg.get("reconnect_max_delay") or 30)
        # 只保留一个"强制校验 CA"的上下文；连 IP 时用 TOFU 指纹代替
        self.ssl_verify = bool(cfg.get("tls_verify", False)) or True
        self.peer_pin = str(cfg.get("tls_pin") or "").replace(":", "").strip().lower() or None
        self.muxes: set[ServerConn] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # 热重载
        self.config_path = config_path
        self.reload_interval = float(cfg.get("reload_interval") or 0)
        self._cfg_mtime = _mtime(config_path)
        self._load_credentials()
        self.load_state()
        if self.peer_pin is None:
            self.peer_pin = self._state_pin

    # ---------- 凭证 ----------
    _state_pin: str | None = None

    def _load_credentials(self) -> None:
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            log.warning("读 %s 失败: %r", self.credentials_file, e)
            data = {}
        if not self.uuid:
            saved_uuid = str(data.get("uuid") or data.get("client") or "").strip().lower()
            if saved_uuid:
                self.uuid = saved_uuid
        if not str(self.cfg.get("label") or "").strip():
            saved_label = str(data.get("label") or "").strip()
            if saved_label:
                self.label = saved_label
        pin = str(data.get("pin") or "").replace(":", "").strip().lower()
        if pin:
            self._state_pin = pin
        token = str(data.get("token") or "")
        if token and not self.token:
            self.token = token
            log.info("已从 %s 载入凭证", self.credentials_file)

    def save_pin(self, pin: str) -> None:
        """TOFU：第一次连到自签证书的源站时自动记住指纹。"""
        self.peer_pin = pin
        data = {}
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            pass
        data["pin"] = pin
        data["uuid"] = self.uuid
        data["label"] = self.label
        try:
            with open(self.credentials_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.credentials_file, 0o600)
            except OSError:
                pass
            log.info("已记录服务器证书指纹 %s（下次不匹配就拒绝连接）", pin[:16] + "…")
        except OSError as e:
            log.error("保存证书指纹失败: %r", e)

    def begin_enroll(self) -> bool:
        """串行化申请：有 uuid 就不设限，没有时只允许一条连接去领 uuid。"""
        if self.uuid:
            return True
        now = asyncio.get_running_loop().time()
        if self._enroll_claim and now - self._enroll_claim < 15:
            return False
        self._enroll_claim = now
        return True

    def save_identity(self) -> None:
        """只把 uuid 落盘（审批还没下来的阶段用）。"""
        data = {}
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            pass
        data.update({"uuid": self.uuid, "label": self.label})
        try:
            with open(self.credentials_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.credentials_file, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.error("保存 UUID 失败: %r", e)

    def adopt_credentials(self, msg: dict) -> None:
        """审批通过后，把 edge 下发的 token 落盘。"""
        uuid = str(msg.get("uuid") or "").strip().lower()
        if uuid:
            self.uuid = uuid
        token = str(msg.get("token") or "")
        if not token:
            return
        self.token = token
        try:
            with open(self.credentials_file, "w", encoding="utf-8") as fh:
                json.dump({"uuid": self.uuid, "label": self.label, "token": token,
                           "ids": msg.get("ids"), "hosts": msg.get("hosts")},
                          fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.credentials_file, 0o600)
            except OSError:
                pass
            log.info("凭证已保存到 %s（权限 600）", self.credentials_file)
        except OSError as e:
            log.error("保存凭证失败: %r", e)

    # ---------- 隧道清单 ----------
    def tunnel_payload(self) -> list[dict]:
        out = []
        for sp in self.specs:
            item = {"local": sp["local_s"]}
            if sp["host"]:
                item["host"] = sp["host"]
            elif sp["id"]:
                item["id"] = sp["id"]
            if sp["label"]:
                item["label"] = sp["label"]
            out.append(item)
        return out

    def rebuild_keys(self) -> None:
        self.by_key = {}
        for sp in self.specs:
            key = sp["host"] or sp["id"]
            if key:
                self.by_key[key] = sp["local"]

    def load_state(self) -> None:
        """把上次服务端分配的 ID 读回来，保证 URL 不变。"""
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            self.rebuild_keys()
            return
        saved = {}
        for item in data.get("tunnels") or []:
            if isinstance(item, dict) and item.get("local"):
                saved[str(item["local"])] = str(item.get("id") or "")
        for sp in self.specs:
            if not sp["id"] and not sp["host"]:
                got = saved.get(sp["local_s"])
                if got:
                    sp["id"] = got
        self.rebuild_keys()

    def save_state(self) -> None:
        data = {"uuid": self.uuid, "label": self.label,
                "tunnels": [{"local": sp["local_s"], "id": sp["id"]}
                            for sp in self.specs if sp["id"]]}
        try:
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.state_file, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.error("写 %s 失败: %r", self.state_file, e)

    def apply_assignment(self, items, prefix: str, public_hosts) -> None:
        """edge 返回分配结果：把 id 记下来、落盘、打印可访问 URL。"""
        if public_hosts:
            self.public_hosts = list(public_hosts)
        by_local = {sp["local_s"]: sp for sp in self.specs}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            sp = by_local.get(str(item.get("local") or ""))
            if sp is None:
                continue
            tid = str(item.get("id") or "")
            if tid:
                sp["id"] = tid
        self.rebuild_keys()
        self.save_state()
        self.log_urls(prefix)

    def log_urls(self, prefix: str | None = None) -> None:
        if not self.public_hosts:
            return
        base = "https://" + self.public_hosts[0]
        for sp in self.specs:
            if sp["host"]:
                log.info("入口: https://%s/  (Host=%s)", sp["host"], sp["host"])
            elif sp["id"]:
                log.info("入口: %s%s/%s/  ->  %s", base,
                         prefix or "", sp["id"], sp["local_s"])

    # ---------- 热重载 ----------
    def request_reload(self) -> None:
        """比对配置文件，变了就重新注册（SIGHUP 也走这里）。"""
        path = self.config_path
        if not path:
            return
        mtime = _mtime(path)
        if mtime is None or mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            cfg = config.load(path, "client")
            specs = _parse_tunnels(cfg)
        except (SystemExit, KeyError, TypeError) as e:  # noqa: PERF203
            log.error("重载配置失败，保留旧配置: %s", e)
            return
        def _shape(items):
            return sorted((x["local_s"], x["host"] or "") for x in items)

        if _shape(specs) == _shape(self.specs):
            return
        old_locals = {sp["local_s"]: sp["id"] for sp in self.specs}
        for sp in specs:  # 保留已知 id
            if not sp["id"] and old_locals.get(sp["local_s"]):
                sp["id"] = old_locals[sp["local_s"]]
        added = sorted({s["local_s"] for s in specs} - set(old_locals))
        removed = sorted(set(old_locals) - {s["local_s"] for s in specs})
        self.specs = specs
        self.rebuild_keys()
        log.info("配置已变更：新增 [%s] 移除 [%s]，重新注册",
                 ", ".join(added) or "-", ", ".join(removed) or "-")
        for mux in list(self.muxes):
            try:
                mux.send_register()
            except Exception:  # noqa: BLE001
                pass

    async def _watch_config(self) -> None:
        while True:
            await asyncio.sleep(self.reload_interval)
            self.request_reload()

    # ---------- 本地连接 ----------
    def connect_local(self, mux: ServerConn, st, local: tuple[str, int]) -> None:
        task = self._loop.create_task(self._open_local(mux, st, local))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _open_local(self, mux: ServerConn, st, local: tuple[str, int]) -> None:
        try:
            async with asyncio.timeout(10):
                await self._loop.create_connection(lambda: LocalProto(st), local[0], local[1])
        except (OSError, asyncio.TimeoutError, ConnectionError) as e:
            log.debug("连不上本地 %s:%d: %r", local[0], local[1], e)
            if not st.closed:
                mux.close_stream(st, rst=True)

    # ---------- 连接槽 ----------
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        n = self.connections
        log.info("%s 启动 %d 条连接 -> %s，%d 条隧道",
                 f"客户端 '{self.label}'", n,
                 ", ".join(srv["url"] for srv in self.servers), len(self.specs))
        tasks: list = [self._slot(i) for i in range(n)]
        if self.config_path and self.reload_interval > 0:
            tasks.append(self._watch_config())
            log.info("配置热重载：每 %.0fs 检查 %s", self.reload_interval, self.config_path)
        await asyncio.gather(*tasks)

    async def _slot(self, idx: int) -> None:
        loop = self._loop
        srv = self.servers[idx % len(self.servers)]
        delay = 1.0
        while not self._stopping:
            started = loop.time()
            mux: ServerConn | None = None
            try:
                mux = await self._connect(srv)
                self.muxes.add(mux)
                await mux.wait_closed()
            except asyncio.CancelledError:
                raise
            except ssl.SSLError as e:
                log.warning("连接 %s TLS 失败: %r", srv["url"], e)
            except Exception as e:
                if self._stopping:
                    break
                log.warning("连接 %s 失败: %r", srv["url"], e)
            finally:
                if mux is not None:
                    self.muxes.discard(mux)
            if self._stopping:
                break
            if loop.time() - started > 30:
                delay = 1.0
            if mux is not None and mux.auth_failed:
                delay = 60.0  # 配置问题重试也没用，别把 edge 日志刷爆
            log.info("#%d %.0fs 后重连 %s", idx, delay, srv["url"])
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, self.max_delay)

    async def _connect(self, srv: dict) -> ServerConn:
        loop = self._loop
        ctx = None
        if srv["secure"]:
            ctx = make_client_ssl(verify=not _is_ip(srv["host"]))
        snow = srv["host"] if ctx is not None else None
        if srv["kind"] == "ws":
            proto = WsClientProto(self, srv)
            tr, _ = await loop.create_connection(
                lambda: proto, srv["host"], srv["port"], ssl=ctx, server_hostname=snow,
            )
            try:
                return await asyncio.wait_for(proto.done, timeout=20)
            except asyncio.TimeoutError:
                tr.close()
                raise TimeoutError("WebSocket 握手超时")
        mux = ServerConn(self, self.window)
        mux.srv = srv
        await loop.create_connection(
            lambda: mux, srv["host"], srv["port"], ssl=ctx, server_hostname=snow,
        )
        return mux

    def stop(self) -> None:
        self._stopping = True
        for mux in list(self.muxes):
            mux.close()
        for t in list(self._tasks):
            t.cancel()
