#!/usr/bin/env python3
"""端到端回归（正常用例，不做极限压测）。客户端是 Node 版（nife-extension/client/tunnelc.mjs）。

    uv run selftest.py

覆盖：WebSocket 控制通道 / 连接池 / GET / keep-alive / 1MiB 上传 / 未知 Host /
裸 TCP 隧道 / HTTPS 按 SNI 透传 / 管理 API（status + kick）/ 配置热重载 / 服务端分配 ID。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from nifetun import runtime
from nifetun.server import Edge

TOKEN = "selftest-token-0123456789abcdef"
CLIENT_NAME = "selftest-client"
ADMIN_TOKEN = "admin-secret"
HOST_A = "a.test"
ECHO_HOST = "echo.test"
SECURE_HOST = "secure.test"
TUNNEL_ID = "sf1"
PREFIX_HOST = "nife-tunnel.yoobit.cn"
CONNECTIONS = 2

ROOT = Path(__file__).resolve().parent
NODE = shutil.which("node") or "node"
CLIENT_SCRIPT = ROOT / "nife-extension" / "client" / "tunnelc.mjs"


def step(msg: str) -> None:
    print(f"[step] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_client_hello(sni: str) -> bytes:
    """造一个结构合法的 TLS ClientHello（只为验证 SNI 解析与透传）。"""
    name = sni.encode()
    sni_ext = (
        b"\x00\x00"
        + (len(name) + 5).to_bytes(2, "big")
        + (len(name) + 3).to_bytes(2, "big")
        + b"\x00"
        + len(name).to_bytes(2, "big")
        + name
    )
    body = (
        b"\x03\x03" + b"\x11" * 32 + b"\x00"
        + b"\x00\x02" + b"\x13\x01"
        + b"\x01\x00"
        + len(sni_ext).to_bytes(2, "big") + sni_ext
    )
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


# ---------------- 测试用源站 ----------------
async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
            headers = {}
            for line in lines[1:]:
                if not line:
                    break
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            payload = (f"posted:{len(body)}" if method == "POST"
                       else f"origin:{headers.get('host')}{path}").encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode()
                + b"\r\nConnection: keep-alive\r\n\r\n" + payload
            )
            await writer.drain()
            if headers.get("connection", "").lower() == "close":
                break
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


# ---------------- 小工具 ----------------
async def read_head(reader: asyncio.StreamReader, timeout: float = 15.0):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(lines[0].split(" ")[1]), headers


async def one_call(port: int, host: str, path: str = "/", extra: bytes = b"", body: bytes = b"",
                   method: str = "GET") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n".encode() + extra + b"\r\n" + body)
        await writer.drain()
        status, headers = await read_head(reader)
        n = int(headers.get("content-length") or 0)
        return status, (await reader.readexactly(n) if n else b"")
    finally:
        writer.close()


async def admin_call(port: int, path: str, method: str = "GET") -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: admin\r\n"
            f"Authorization: Bearer {ADMIN_TOKEN}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()
        status, headers = await read_head(reader)
        n = int(headers.get("content-length") or 0)
        raw = await reader.readexactly(n) if n else b""
        return status, json.loads(raw or b"{}")
    finally:
        writer.close()


def tunnel_specs(origin_port: int, echo_port: int) -> list:
    return [
        {"host": HOST_A, "local": f"127.0.0.1:{origin_port}"},
        {"host": ECHO_HOST, "local": f"127.0.0.1:{echo_port}"},
        {"host": SECURE_HOST, "local": f"127.0.0.1:{echo_port}"},
        {"host": "tcp.test", "local": f"127.0.0.1:{echo_port}"},
        {"id": TUNNEL_ID, "local": f"127.0.0.1:{origin_port}"},
        {"local": f"127.0.0.1:{origin_port}", "label": "自动分配"},
    ]


class NodeClient:
    """起一个 Node 客户端进程（配置文件方式，才能测热重载）。"""

    def __init__(self, workdir: Path, cfg: dict, token: str | None = None) -> None:
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.dir / "tunnelc.json"
        self.ids_path = self.dir / "nifetun-ids.json"
        self.cred_path = self.dir / "nifetun-credentials.json"
        self.log_path = self.dir / "client.log"
        self.proc: subprocess.Popen | None = None
        self.write_cfg(cfg)
        if token:
            self.cred_path.write_text(
                json.dumps({"uuid": CLIENT_NAME, "label": "selftest", "token": token}), encoding="utf-8"
            )

    def write_cfg(self, cfg: dict) -> None:
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [NODE, str(CLIENT_SCRIPT), "-c", str(self.cfg_path), "--log-file", str(self.log_path)],
            cwd=str(self.dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def ids(self) -> dict[str, str]:
        try:
            data = json.loads(self.ids_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {t["local"]: t["id"] for t in data.get("tunnels") or []}

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------- 主体 ----------------
async def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s | %(message)s")
    if not CLIENT_SCRIPT.exists():
        print(f"找不到客户端脚本 {CLIENT_SCRIPT}")
        return 1
    ports = {n: free_port() for n in ("origin", "echo", "public", "client_port", "tcp", "sni", "admin")}

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", ports["origin"])
    echo = await asyncio.start_server(echo_handler, "127.0.0.1", ports["echo"])

    edge = Edge(
        {
            "clients": [{"name": CLIENT_NAME, "token": TOKEN, "ids": ["*"], "hosts": ["*.test"]}],
            "allowed_hosts": ["*.test"],
            "public_listen": f"127.0.0.1:{ports['public']}",
            "client_listen": f"127.0.0.1:{ports['client_port']}",
            "admin_listen": f"127.0.0.1:{ports['admin']}",
            "admin_token": ADMIN_TOKEN,
            "window": 128 * 1024,
            "idle_timeout": 30,
            "access_log": False,
            "prefix_hosts": ["*.test", PREFIX_HOST],
            "tcp_listeners": [
                {"listen": f"127.0.0.1:{ports['tcp']}", "host": "tcp.test"},
                {"listen": f"127.0.0.1:{ports['sni']}", "mode": "sni"},
            ],
        }
    )
    await edge.start()
    edge_task = asyncio.create_task(edge.serve_forever())

    tmp = Path(tempfile.mkdtemp(prefix="nifetun-selftest-"))
    pub = ports["public"]
    cfg = {
        "client": CLIENT_NAME,
        "label": "selftest",
        "server": f"ws://127.0.0.1:{pub}",  # 客户端只有 WS 一种接入方式（edge 公网口）
        "connections": CONNECTIONS,
        "heartbeat": 5,
        "dead_after": 20,
        "reload_interval": 0.5,
        "window": 128 * 1024,
        "tunnels": tunnel_specs(ports["origin"], ports["echo"]),
    }
    client = NodeClient(tmp / "c1", cfg, token=TOKEN)
    client.start()

    try:
        for _ in range(200):
            if len(edge.routes.get(HOST_A, ())) >= CONNECTIONS:
                break
            await asyncio.sleep(0.05)
        assert len(edge.routes.get(HOST_A, ())) == CONNECTIONS, (
            f"客户端未注册到全部连接，日志:\n{client.log()[-800:]}"
        )

        status, body = await one_call(pub, HOST_A, "/hello")
        assert (status, body) == (200, b"origin:a.test/hello"), (status, body)
        step(f"Node 客户端经 WebSocket 认证通过 + GET 200（连接池 {CONNECTIONS} 条）")

        reader, writer = await asyncio.open_connection("127.0.0.1", pub)
        for i in range(3):
            writer.write(f"GET /k{i} HTTP/1.1\r\nHost: {HOST_A}\r\n\r\n".encode())
            await writer.drain()
            st, headers = await read_head(reader)
            n = int(headers.get("content-length") or 0)
            payload = await reader.readexactly(n)
            assert (st, payload) == (200, f"origin:a.test/k{i}".encode()), (st, payload)
        writer.close()
        step("keep-alive 单连接复用 3 次")

        big = b"x" * (1024 * 1024)
        status, body = await one_call(pub, HOST_A, "/upload", method="POST",
                                      extra=f"Content-Length: {len(big)}\r\n".encode(), body=big)
        assert (status, body) == (200, b"posted:1048576"), (status, body)
        step("POST 1MiB 完整到达源站")

        status, _ = await one_call(pub, "nope.test", "/")
        assert status == 404, status
        step("未知 Host -> 404")

        reader, writer = await asyncio.open_connection("127.0.0.1", ports["tcp"])
        writer.write(b"ping-over-tcp")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(64), 10) == b"ping-over-tcp"
        writer.close()
        step("裸 TCP 端口隧道透传")

        reader, writer = await asyncio.open_connection("127.0.0.1", ports["sni"])
        hello = make_client_hello(SECURE_HOST)
        writer.write(hello)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4096), 10) == hello
        writer.close()
        step("HTTPS 按 SNI 路由，ClientHello 原样到达源站")

        status, data = await admin_call(ports["admin"], "/api/status")
        assert status == 200 and data["routes"].get(HOST_A) or data.get("routes_by_id"), data
        step(f"管理 API /api/status（域名 {len(data.get('routes', {}))} 条，路径 {len(data.get('routes_by_id', {}))} 条）")

        status, body = await one_call(pub, PREFIX_HOST, f"/t/{TUNNEL_ID}/x")
        assert (status, body) == (200, b"origin:" + PREFIX_HOST.encode() + b"/x"), (status, body)
        step(f"路径前缀路由 /t/{TUNNEL_ID}/ -> 隧道（前缀已从请求行剥除）")

        for _ in range(100):
            ids = client.ids()
            if ids.get(f"127.0.0.1:{ports['origin']}"):
                break
            await asyncio.sleep(0.05)
        auto_id = client.ids().get(f"127.0.0.1:{ports['origin']}")
        assert auto_id, f"服务端没分配 ID，日志:\n{client.log()[-500:]}"
        assert len(auto_id) >= 12 and auto_id in edge.routes_by_id, (auto_id, list(edge.routes_by_id))
        status, body = await one_call(pub, PREFIX_HOST, f"/t/{auto_id}/auto")
        assert (status, body) == (200, b"origin:" + PREFIX_HOST.encode() + b"/auto"), (status, body)
        step(f"客户端不写 id -> 服务端分配 {auto_id}，URL 可用、已落盘")

        status, _ = await one_call(pub, "evil.example.com", "/")
        assert status == 403, status
        step("非白名单 Host -> 403（绕过 CDN 直连无效）")

        # 热重载：往配置文件里加一条隧道，客户端应自己重新注册
        extra_host = "hot.test"
        cfg["tunnels"] = cfg["tunnels"] + [{"host": extra_host, "local": f"127.0.0.1:{ports['origin']}"}]
        client.write_cfg(cfg)
        for _ in range(200):
            if len(edge.routes.get(extra_host, ())) >= CONNECTIONS:
                break
            await asyncio.sleep(0.05)
        assert len(edge.routes.get(extra_host, ())) == CONNECTIONS, (
            f"热重载没生效，日志:\n{client.log()[-500:]}"
        )
        status, body = await one_call(pub, extra_host, "/back")
        assert (status, body) == (200, b"origin:hot.test/back"), (status, body)
        step("改配置文件后新隧道自动上线（无需重启客户端）")

        status, data = await admin_call(ports["admin"], "/api/kick?peer=all", "POST")
        assert status == 200, data
        step("管理 API /api/kick 踢掉连接")

        for _ in range(200):
            if len(edge.routes.get(HOST_A, ())) >= CONNECTIONS:
                break
            await asyncio.sleep(0.05)
        status, body = await one_call(pub, HOST_A, "/again")
        assert status == 200, "被踢后客户端未能自动重连"
        step("被踢的客户端自动重连并恢复隧道")
    except BaseException as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}", flush=True)
        raise
    finally:
        client.stop()
        edge_task.cancel()
        await edge.close()
        origin.close()
        echo.close()
        await asyncio.sleep(0.05)

    print("\n=== 自测通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(runtime.run(main()))
