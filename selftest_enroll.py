#!/usr/bin/env python3
"""审批流程回归（Node 客户端）：没有凭证 → 自助申请 → 管理员批准 → 自动拿 token 上线。

    uv run selftest_enroll.py
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

ADMIN = "adm1n-token"
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
            f"Authorization: Bearer {ADMIN}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length") or 0)
        return json.loads(await reader.readexactly(n) if n else b"{}")
    finally:
        writer.close()


async def http_get(port: int, host: str, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        n = 0
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "content-length":
                n = int(v.strip())
        return status, (await reader.readexactly(n) if n else b"")
    finally:
        writer.close()


class NodeClient:
    """不起眼的 Node 客户端包装：配置文件 + 日志 + 状态文件都放在自己目录里。"""

    def __init__(self, workdir: Path, cfg: dict) -> None:
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.dir / "tunnelc.json"
        self.ids_path = self.dir / "nifetun-ids.json"
        self.cred_path = self.dir / "nifetun-credentials.json"
        self.log_path = self.dir / "client.log"
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [NODE, str(CLIENT_SCRIPT), "-c", str(self.cfg_path), "--log-file", str(self.log_path)],
            cwd=str(self.dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def cred(self) -> dict:
        try:
            return json.loads(self.cred_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def ids(self) -> dict:
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


async def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s | %(message)s")
    if not CLIENT_SCRIPT.exists():
        print(f"找不到客户端脚本 {CLIENT_SCRIPT}")
        return 1

    op, cp, pp, ap = free_port(), free_port(), free_port(), free_port()
    server = await asyncio.start_server(origin, "127.0.0.1", op)
    edge = Edge(
        {
            "enroll": True,
            "public_listen": f"127.0.0.1:{pp}",
            "client_listen": f"127.0.0.1:{cp}",
            "admin_listen": f"127.0.0.1:{ap}",
            "admin_token": ADMIN,
            "clients_file": str(Path(tempfile.mkdtemp(prefix="nifetun-enroll-")) / "clients.json"),
            "prefix": "/t",
            "access_log": False,
            "handshake_timeout": 30,
            "enroll_timeout": 120,
        }
    )
    await edge.start()
    edge_task = asyncio.create_task(edge.serve_forever())

    tmp = Path(tempfile.mkdtemp(prefix="nifetun-enroll-cli-"))
    cfg = {
        "label": "laptop",
        "server": f"ws://127.0.0.1:{pp}",
        "tunnels": [f"127.0.0.1:{op}"],
        "connections": 1,
        "reload_interval": 0,
    }
    client = NodeClient(tmp / "c1", cfg)
    client.start()
    twins: list[NodeClient] = []

    try:
        # 1) 无凭证 → 出现在 pending，且客户端手里没有 token
        for _ in range(150):
            status = await admin_call(ap, "/api/status")
            if status.get("pending"):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(f"没有收到接入申请，日志:\n{client.log()[-600:]}")
        assert not client.cred().get("token"), "客户端不该有 token"
        uuid0 = status["pending"][0]["client"]
        assert len(uuid0) == 16 and status["pending"][0]["label"] == "laptop", status
        assert "等待审批" in client.log(), client.log()[-300:]
        step("客户端申请接入（无凭证，pending 可见，日志只说「等待审批」）")

        # 2) 批准 → token 通过已加密的 WS 通道下发
        res = await admin_call(ap, f"/api/approve?client={uuid0}", "POST")
        assert res.get("approved") == uuid0 and len(res.get("token", "")) >= 40, res
        step("管理员批准，edge 现场生成并下发 token")

        # 3) 客户端落盘凭证并上线
        for _ in range(200):
            if client.ids():
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(f"客户端没有上线，日志:\n{client.log()[-600:]}")
        cred = client.cred()
        assert cred.get("uuid") == uuid0 and len(cred.get("token", "")) >= 40, cred
        tid = client.ids()[f"127.0.0.1:{op}"]
        step(f"客户端保存凭证并自动重连、注册成功（/t/{tid}/）")

        status, body = await http_get(pp, "whatever.test", f"/t/{tid}/")
        assert (status, body) == (200, b"origin!"), (status, body)
        step("走隧道访问源站 -> 200")

        # 4) 凭证已持久化：重启客户端不需要重新审批
        client.stop()
        client.start()
        for _ in range(200):
            if client.ids():
                break
            await asyncio.sleep(0.1)
        assert len(edge.routes_by_id) >= 1, list(edge.routes_by_id)
        assert "已提交接入申请" not in client.log().split("已注册")[-1].split("启动")[-1], "重启后又申请了"
        step("凭证已持久化（edge: clients_file / 客户端: nifetun-credentials.json）")

        # 5) 已注册的 uuid 不会再进申请队列（防冒名）
        again = NodeClient(tmp / "c2", {**cfg, "label": "laptop"})
        (tmp / "c2").mkdir(parents=True, exist_ok=True)
        (tmp / "c2" / "nifetun-credentials.json").write_text(
            json.dumps({"uuid": uuid0, "label": "laptop"}), encoding="utf-8"
        )
        again.start()
        await asyncio.sleep(3)
        status = await admin_call(ap, "/api/status")
        assert not any(p["client"] == uuid0 for p in status.get("pending") or []), status
        again.stop()
        step("已注册的客户端不会再进入申请队列（防冒名）")

        # 6) 身份是服务端分配的 UUID：同一个 label 的两台机器互不影响
        for i in range(2):
            tw = NodeClient(tmp / f"twin{i}", {**cfg, "label": "same-host"})
            tw.start()
            twins.append(tw)
        uuids: list[str] = []
        for _ in range(200):
            pending = (await admin_call(ap, "/api/status")).get("pending") or []
            uuids = [p["client"] for p in pending if p.get("label") == "same-host"]
            if len(uuids) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(uuids) == 2 and uuids[0] != uuids[1], (uuids, pending)
        for u in uuids:
            await admin_call(ap, f"/api/approve?client={u}", "POST")
        for _ in range(200):
            if len(edge.routes_by_id) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(edge.routes_by_id) >= 2, edge.routes_by_id
        step(f"同 label 的两台机器拿到不同 UUID（{uuids[0][:8]}… / {uuids[1][:8]}…），各自上线")
    except BaseException as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}", flush=True)
        raise
    finally:
        client.stop()
        for tw in twins:
            tw.stop()
        edge_task.cancel()
        await edge.close()
        server.close()
        await asyncio.sleep(0.05)

    print("\n=== 审批流程测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(runtime.run(main()))
