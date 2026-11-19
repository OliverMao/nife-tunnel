"""配置加载与归一化。"""

from __future__ import annotations

from . import jsonx

_decode = jsonx.decode

EDGE_DEFAULTS: dict = {
    # 监听
    "public_listen": "0.0.0.0:8443",
    "client_listen": "0.0.0.0:9443",
    "client_tls_cert": None,
    "client_tls_key": None,
    "admin_listen": None,
    "admin_token": None,
    # 客户端认证：每个客户端一个 token，绑定各自的 id/域名白名单
    "clients": [],
    #: 允许客户端自助申请接入（管理员用 /api/approve 批准后自动下发 token）
    "enroll": False,
    #: 批准后的凭证落盘位置（不填只存内存，重启需重新批准）
    "clients_file": None,
    "enroll_timeout": 600,
    #: 邮件审批（口令请用环境变量 NIFETUN_SMTP_PASS，别写这里）
    "email": {},
    # 兼容单 token 模式（所有客户端共用一个 token，走全局 allowed_*）
    "token": None,
    "allowed_hosts": ["*"],
    "allowed_ids": ["*"],
    # 公网入口
    "public_tls_cert": None,
    "public_tls_key": None,
    "prefix": "/t",
    "strip_prefix": True,
    #: 只接受这些 Host（fnmatch）。走 CDN/ESA 时填自己的入口域名，防止绕过 CDN 直连
    "prefix_hosts": [],
    #: 服务端分配的隧道 ID 长度（十六进制字符数）
    "id_len": 12,
    #: 隧道 ID 归属表（哪个 ID 属于哪个客户端），建议和 clients_file 放一起
    "ids_file": None,
    "tcp_listeners": [],
    # 性能/超时
    "window": 262144,
    "backlog": 2048,
    "access_log": True,
    "handshake_timeout": 10,
    "idle_timeout": 90,
    "not_found_html": "",
}

CLIENT_DEFAULTS: dict = {
    #: 最简写法：{"server": "https://...", "tunnels": ["127.0.0.1:8080"]}
    #: 其余全部可以省略，下面的默认值都是"能用"的
    "server": None,
    "servers": [],
    "client": None,      #: 省略 = 用主机名（审批邮件里能认出是哪台机器）
    "token": None,
    #: 没有 token 时自动申请；批准后写到这里
    "credentials_file": None,    #: 省略 = 配置文件旁边的 nifetun-credentials.json
    #: 服务端地址。写 https://域名 就走 CDN 443（WebSocket，源站 IP 不暴露）；
    #: 写 IP:9443 是直连备用路径。TLS 全自动，没有任何 tls_* 参数要填。
    "tunnel_state_file": None,   #: 省略 = 配置文件旁边的 nifetun-ids.json
    "tunnels": [],
    "connections": 4,
    "window": 262144,
    "heartbeat": 15,
    "dead_after": 60,
    "reconnect_max_delay": 30,
    "reload_interval": 5,
}


def loads(text: str | bytes, kind: str) -> dict:
    """从字符串解析配置（`--json` 用），规则和读文件完全一致。"""
    try:
        cfg = _decode(text if isinstance(text, bytes) else text.encode("utf-8"))
    except Exception as e:
        raise SystemExit(f"配置不是合法 JSON: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit("配置顶层必须是对象")
    return _merge(cfg, kind)


def _merge(cfg: dict, kind: str) -> dict:
    defaults = EDGE_DEFAULTS if kind == "edge" else CLIENT_DEFAULTS
    merged = dict(defaults)
    for k, v in cfg.items():
        if k.startswith("_"):  # 注释字段
            continue
        merged[k] = v
    return merged


def load(path: str, kind: str) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise SystemExit(f"找不到配置文件: {path}")
    try:
        cfg = _decode(raw)
    except Exception as e:
        raise SystemExit(f"配置文件不是合法 JSON: {path}: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit("配置文件顶层必须是对象")
    return _merge(cfg, kind)


def addr(value: str) -> tuple[str, int]:
    text = str(value).strip()
    if text.startswith("["):  # [::1]:8080
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = text.rpartition(":")
    if not port.isdigit():
        raise SystemExit(f"地址格式应为 host:port，收到 {value!r}")
    return (host or "0.0.0.0", int(port))
