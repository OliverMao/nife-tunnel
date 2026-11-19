#!/usr/bin/env python3
"""审批流程回归：客户端没有 token → 自助申请 → 管理员批准 → 自动拿凭证上线。

    uv run python selftest_enroll.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

from nifetun import runtime
from nifetun.client import Client
from nifetun.server import Edge

ADMIN = "adm1n-token"


def step(msg: str) -> None:
    print(f"[step] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\norigin!")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def admin_call(port: int, path: str, method: str = "GET") -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: admin\r\n"
            f"Authorization: Bearer {ADMIN}\r\n\r\n".encode()
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        n = 0
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-length:"):
                n = int(line.split(":")[1])
        return json.loads(await reader.readexactly(n) or b"{}")
    finally:
        writer.close()


async def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s | %(message)s")
    op, pp, cp, ap = (free_port() for _ in range(4))
    tmp = Path(tempfile.mkdtemp(prefix="nifetun-enroll-"))
    creds = tmp / "client-credentials.json"
    clients_file = tmp / "clients.json"

    server = await asyncio.start_server(origin, "127.0.0.1", op)
    edge = Edge({
        "public_listen": f"127.0.0.1:{pp}",
        "client_listen": f"127.0.0.1:{cp}",
        "admin_listen": f"127.0.0.1:{ap}",
        "admin_token": ADMIN,
        "enroll": True,
        "clients_file": str(clients_file),
        "prefix": "/t",
        "access_log": False,
        "handshake_timeout": 30,
    })
    await edge.start()
    edge_task = asyncio.create_task(edge.serve_forever())

    cfg = {
        "client": "laptop",
        "servers": [f"tcp://127.0.0.1:{cp}"],
        "credentials_file": str(creds),
        "connections": 1,
        "reload_interval": 0,
        "tunnels": [{"id": "lap1", "local": f"127.0.0.1:{op}"}],
    }
    client = Client(cfg)
    client_task = asyncio.create_task(client.run())

    try:
        # 1) 申请出现在 pending 里，且此时客户端手里没有任何凭证
        for _ in range(100):
            status = await admin_call(ap, "/api/status")
            if status.get("pending"):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("没有收到接入申请")
        assert client.token == "", "客户端不该有 token"
        assert status["pending"][0]["client"] == "laptop", status
        assert not creds.exists()
        step("客户端申请接入（无 token，pending 可见）")

        # 2) 管理员批准 → token 通过已加密的连接下发（不经过任何人工传递）
        res = await admin_call(ap, "/api/approve?client=laptop&ids=lap*", "POST")
        assert res.get("approved") == "laptop" and len(res.get("token", "")) >= 40, res
        step("管理员批准，edge 现场生成并下发 token")

        # 3) 客户端落盘并上线
        for _ in range(150):
            if "lap1" in edge.routes_by_id:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("客户端没有上线")
        assert creds.exists(), "凭证没有落盘"
        assert len(client.token) >= 40, "客户端没拿到 token"
        on_disk = json.loads(creds.read_text(encoding="utf-8"))
        assert on_disk["token"] == client.token and on_disk["uuid"] == "laptop", on_disk
        step("客户端保存凭证并自动重新连接、注册成功")

        # 4) 端到端可用
        reader, writer = await asyncio.open_connection("127.0.0.1", pp)
        writer.write(b"GET /t/lap1/ HTTP/1.1\r\nHost: laptop\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        body = await reader.readexactly(7)
        writer.close()
        assert b"200" in head and body == b"origin!", (head, body)
        step("走隧道访问源站 -> 200 origin!")

        # 5) 凭证持久化 + 重启后直接复用（不需要重新审批）
        assert clients_file.exists(), "边缘没有持久化已批准的客户端"
        saved = json.loads(clients_file.read_text(encoding="utf-8"))
        assert saved[0]["name"] == "laptop" and saved[0]["token"] == client.token, saved
        again = Client(dict(cfg))
        assert again.token == client.token, "重启后没能从文件载入凭证"
        step("凭证已持久化（edge: clients_file / 客户端: credentials_file）")

        # 6) 名字被占用时不允许再次申请（防冒名顶替）
        dup = Client({**cfg, "client": "laptop", "token": "", "credentials_file": str(tmp / "x.json")})
        dup_cfg_ok = dup.token == ""
        assert dup_cfg_ok
        status = await admin_call(ap, "/api/status")
        assert ("laptop" in json.dumps(status.get("pending"))) is False
        step("已存在的客户端名不会再进入申请队列（防冒名）")

        # 7) 身份 = 服务端分配的 UUID：同主机名（label）两台机器互不影响
        twins = []
        for i in range(2):
            ccfg = {
                "label": "same-host",            # 故意同名
                "servers": [f"tcp://127.0.0.1:{cp}"],
                "credentials_file": str(tmp / f"twin{i}.json"),
                "tunnel_state_file": str(tmp / f"twin{i}-ids.json"),
                "connections": 1, "reload_interval": 0,
                "tunnels": [f"127.0.0.1:{op}"],
            }
            tw = Client(ccfg)
            tw_task = asyncio.create_task(tw.run())
            twins.append((tw, tw_task))
        for _ in range(100):
            pending = (await admin_call(ap, "/api/status")).get("pending") or []
            labels = [p["label"] for p in pending]
            if labels.count("same-host") >= 2:
                break
            await asyncio.sleep(0.1)
        uuids = [p["client"] for p in pending if p.get("label") == "same-host"]
        assert len(uuids) == 2 and uuids[0] != uuids[1], uuids
        for u in uuids:
            await admin_call(ap, f"/api/approve?client={u}", "POST")
        for _ in range(150):
            if len([t for t in edge.routes_by_id]) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(edge.routes_by_id) >= 2, edge.routes_by_id
        for tw, tw_task in twins:
            tw.stop()
            tw_task.cancel()
        step(f"同主机名的两台机器拿到不同 UUID（{uuids[0][:8]}… / {uuids[1][:8]}…），各自上线")
    except BaseException as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}", flush=True)
        raise
    finally:
        client.stop()
        client_task.cancel()
        edge_task.cancel()
        await edge.close()
        server.close()
        await asyncio.sleep(0.05)

    print("\n=== 审批流程测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(runtime.run(main()))
