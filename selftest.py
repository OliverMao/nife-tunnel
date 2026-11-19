#!/usr/bin/env python3
"""端到端回归（正常用例，不做极限压测）。

    uv run python selftest.py

覆盖：连接池就绪 / GET / keep-alive / 1MiB 上传 / 未知 Host / 裸 TCP 隧道 /
HTTPS 按 SNI 透传 / 管理 API（status + kick）/ agent 配置热重载。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from nifetun import runtime
from nifetun.client import Client
from nifetun.server import Edge

TOKEN = "selftest-token-0123456789abcdef"
CLIENT_NAME = "selftest-client"
ADMIN_TOKEN = "admin-secret"
HOST_A = "a.test"
ECHO_HOST = "echo.test"
SECURE_HOST = "secure.test"
TUNNEL_ID = "sf1"
#: 路径前缀模式下，ESA/浏览器看到的 Host
PREFIX_HOST = "nife-tunnel.yoobit.cn"
CONNECTIONS = 2


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
        b"\x03\x03" + b"\x11" * 32 + b"\x00"  # version + random + session_id len
        + b"\x00\x02" + b"\x13\x01"  # cipher_suites
        + b"\x01\x00"  # compression
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


def client_config(origin_port: int, echo_port: int, extra: list[dict] | None = None) -> dict:
    return {
        "client": CLIENT_NAME,
        "token": TOKEN,
        "servers": [],  # main 里填
        "connections": CONNECTIONS,
        "heartbeat": 5,
        "dead_after": 20,
        "reload_interval": 0.2,
        "window": 128 * 1024,
        "tunnels": [
            {"host": HOST_A, "local": f"127.0.0.1:{origin_port}"},
            {"host": ECHO_HOST, "local": f"127.0.0.1:{echo_port}"},
            {"host": SECURE_HOST, "local": f"127.0.0.1:{echo_port}"},
            {"host": "tcp.test", "local": f"127.0.0.1:{echo_port}"},
            {"id": TUNNEL_ID, "local": f"127.0.0.1:{origin_port}"},
            {"local": f"127.0.0.1:{origin_port}", "label": "自动分配"},
        ] + (extra or []),
    }


# ---------------- 主体 ----------------
async def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s | %(message)s")
    ports = {n: free_port() for n in ("origin", "echo", "public", "client_port", "tcp", "sni", "admin")}

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", ports["origin"])
    echo = await asyncio.start_server(echo_handler, "127.0.0.1", ports["echo"])

    edge = Edge(
        {
            "clients": [{"name": CLIENT_NAME, "token": TOKEN,
                         "ids": ["*"], "hosts": ["*.test"]}],
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
    cfg_path = tmp / "client.json"
    acfg = client_config(ports["origin"], ports["echo"])
    acfg["servers"] = [f"tcp://127.0.0.1:{ports['client_port']}"]
    cfg_path.write_text(json.dumps(acfg), encoding="utf-8")

    client = Client(acfg, config_path=str(cfg_path))
    client_task = asyncio.create_task(client.run())

    try:
        for _ in range(200):
            if len(edge.routes.get(HOST_A, ())) >= CONNECTIONS:
                break
            await asyncio.sleep(0.05)
        assert len(edge.routes.get(HOST_A, ())) == CONNECTIONS, "客户端未注册到全部连接"

        pub = ports["public"]

        status, body = await one_call(pub, HOST_A, "/hello")
        assert (status, body) == (200, b"origin:a.test/hello"), (status, body)
        step(f"客户端认证通过 + GET 200（连接池 {CONNECTIONS} 条）")

        reader, writer = await asyncio.open_connection("127.0.0.1", pub)
        for i in range(3):
            writer.write(f"GET /k{i} HTTP/1.1\r\nHost: {HOST_A}\r\n\r\n".encode())
            await writer.drain()
            st, headers = await read_head(reader)
            n = int(headers["content-length"])
            assert st == 200 and (await reader.readexactly(n)).endswith(f"/k{i}".encode())
        writer.close()
        step("keep-alive 单连接复用 3 次")

        blob = bytes(range(256)) * 4096
        status, body = await one_call(pub, HOST_A, "/up", b"Content-Length: 1048576\r\n", blob,
                                      method="POST")
        assert (status, body) == (200, b"posted:1048576"), (status, body)
        step("POST 1MiB 完整到达源站")

        status, _ = await one_call(pub, "nobody.test")
        assert status == 404, status
        step("未知 Host -> 404")

        r, w = await asyncio.open_connection("127.0.0.1", ports["tcp"])
        w.write(b"ping-tcp\n")
        await w.drain()
        assert await asyncio.wait_for(r.read(64), 5) == b"ping-tcp\n"
        w.close()
        step("裸 TCP 端口隧道透传")

        r, w = await asyncio.open_connection("127.0.0.1", ports["sni"])
        hello = make_client_hello(SECURE_HOST)
        w.write(hello)
        await w.drain()
        got = await asyncio.wait_for(r.readexactly(len(hello)), 5)
        assert got == hello, "SNI 透传的字节流被改动"
        w.close()
        step("HTTPS 按 SNI 路由，ClientHello 原样到达源站")

        status, data = await admin_call(ports["admin"], "/api/status")
        assert status == 200 and SECURE_HOST in data["routes"], data
        assert TUNNEL_ID in data["routes_by_id"], data
        assert data["streams_total"] >= 0 and len(data["clients"]) == CONNECTIONS, data
        assert data["clients"][0]["client"] == CLIENT_NAME, data
        step(f"管理 API /api/status（域名 {len(data['routes'])} 条，路径 {len(data['routes_by_id'])} 条）")

        status, body = await one_call(pub, PREFIX_HOST, f"/t/{TUNNEL_ID}/hello")
        assert (status, body) == (200, b"origin:" + PREFIX_HOST.encode() + b"/hello"), (status, body)
        status, _ = await one_call(pub, PREFIX_HOST, "/t/nonexistent/x")
        assert status == 404, status
        step(f"路径前缀路由 /t/{TUNNEL_ID}/ -> 隧道（前缀已从请求行剥除）")

        auto = [sp for sp in client.specs if sp.get("label") == "自动分配"]
        assert auto and auto[0]["id"], "服务端没有分配 ID"
        # 连接池里每条连接都会注册一次，ID 必须只有一个（按 客户端+本地地址 复用）
        auto_ids = {sp["id"] for sp in client.specs if not sp.get("host") and not sp.get("label")}
        assert len(auto_ids) == 1, f"同一隧道被分配了多个 ID: {auto_ids}"
        auto_id = auto[0]["id"]
        assert len(auto_id) >= 12 and auto_id in edge.routes_by_id, (auto_id, edge.routes_by_id)
        status, body = await one_call(pub, PREFIX_HOST, f"/t/{auto_id}/auto")
        assert (status, body) == (200, b"origin:" + PREFIX_HOST.encode() + b"/auto"), (status, body)
        step(f"客户端不写 id -> 服务端分配 {auto_id}，URL 可用、已落盘")

        status, _ = await one_call(pub, "evil.example.com", f"/t/{TUNNEL_ID}/x")
        assert status == 403, f"Host 白名单没生效，返回 {status}"
        step("非白名单 Host -> 403（绕过 CDN 直连无效）")

        # 热重载：往配置文件里加一条隧道，agent 应自动重新注册
        extra_host = "hot.test"
        new_cfg = client_config(ports["origin"], ports["echo"],
                               [{"host": extra_host, "local": f"127.0.0.1:{ports['origin']}"}])
        new_cfg["servers"] = acfg["servers"]
        cfg_path.write_text(json.dumps(new_cfg), encoding="utf-8")
        future = time.time() + 10
        os.utime(cfg_path, (future, future))
        for _ in range(200):
            if extra_host in edge.routes:
                break
            await asyncio.sleep(0.05)
        assert extra_host in edge.routes, "热重载没有生效"
        status, body = await one_call(pub, extra_host, "/hot")
        assert (status, body) == (200, b"origin:hot.test/hot"), (status, body)
        step("改配置文件后新隧道自动上线（无需重启客户端）")

        status, data = await admin_call(ports["admin"], f"/api/kick?host={extra_host}", "POST")
        assert status == 200 and data["kicked"] == CONNECTIONS, data
        step(f"管理 API /api/kick 踢掉 {data['kicked']} 条连接")

        for _ in range(100):
            if len(edge.routes.get(extra_host, ())) >= CONNECTIONS:
                break
            await asyncio.sleep(0.05)
        status, body = await one_call(pub, extra_host, "/back")
        assert status == 200, "被踢后 agent 未能自动重连"
        step("被踢的客户端自动重连并恢复隧道")
    except BaseException as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}", flush=True)
        raise
    finally:
        client.stop()
        # ---- WebSocket 控制通道（客户端走 CDN 443 时用的就是这条路径）----
        ws_cfg = client_config(ports["origin"], ports["echo"])
        ws_cfg["client"] = "wscli"
        ws_cfg["servers"] = [f"ws://127.0.0.1:{pub}/_nifetun/ws"]
        ws_cfg["connections"] = 1
        ws_cfg["tunnels"] = [{"local": f"127.0.0.1:{ports['origin']}", "label": "ws"}]
        ws_cfg["reload_interval"] = 0
        ws_cfg["credentials_file"] = str(tmp / "ws-cred.json")
        ws_cfg["tunnel_state_file"] = str(tmp / "ws-ids.json")
        ws_client = Client(ws_cfg)
        ws_client.token = ws_cfg["token"] = edge.approve("wscli", None, None)
        ws_task = asyncio.create_task(ws_client.run())
        try:
            for _ in range(200):
                if ws_client.specs and ws_client.specs[0]["id"]:
                    break
                await asyncio.sleep(0.05)
            assert ws_client.specs and ws_client.specs[0]["id"], "WS 客户端没拿到 ID"
            ws_id = ws_client.specs[0]["id"]
            status, body = await one_call(pub, PREFIX_HOST, f"/t/{ws_id}/via-ws")
            assert (status, body) == (200, b"origin:" + PREFIX_HOST.encode() + b"/via-ws"), (status, body)
            step(f"WebSocket 控制通道可用（ws://…/_nifetun/ws，ID={ws_id}）")
        finally:
            ws_client.stop()
            ws_task.cancel()

        client_task.cancel()
        edge_task.cancel()
        await edge.close()
        origin.close()
        echo.close()
        await asyncio.sleep(0.05)

    print("\n=== 自测通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(runtime.run(main()))
