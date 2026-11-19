#!/usr/bin/env python3
"""基准测试（四个独立进程：源站 / agent / edge / 压测端），数字才有意义。

    uv run python bench.py                 # 默认 4 连接池、64MB
    uv run python bench.py --conns 8 --window 512 --size 128 --edge-procs 2

--edge-procs N：起 N 个 edge 进程（各占一个端口），agent 同时连它们，
等价于生产里 nginx stream upstream 把流量摊到多核。用来观察单进程上限与多进程扩展。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nifetun import runtime

ROOT = Path(__file__).resolve().parent
TOKEN = "bench-token-0123456789abcdef"
HOST = "bench.test"
PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ============ 源站（独立进程）============
async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    chunk = b"x" * (256 << 10)
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
            if method == "POST":
                left = length
                while left:
                    data = await reader.read(min(left, 1 << 18))
                    if not data:
                        break
                    left -= len(data)
                body = f"got:{length - left}".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
            else:
                n = int(path.split("=", 1)[1]) if "=" in path else 0
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: "
                             + str(n).encode() + b"\r\n\r\n")
                left = n
                while left:
                    piece = chunk[: min(left, len(chunk))]
                    writer.write(piece)
                    left -= len(piece)
                    await writer.drain()
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


async def origin_main(port: int) -> None:
    server = await asyncio.start_server(origin_handler, "127.0.0.1", port, backlog=512)
    print("origin ready", flush=True)
    async with server:
        await server.serve_forever()


# ============ 压测端 ============
async def read_head(reader: asyncio.StreamReader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(lines[0].split(" ")[1]), headers


async def download(port: int, n: int, host: str = HOST) -> int:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(f"GET /big={n} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        await w.drain()
        _, headers = await read_head(r)
        need = int(headers["content-length"])
        got = 0
        while got < need:
            b = await r.read(1 << 18)
            if not b:
                break
            got += len(b)
        return got
    finally:
        w.close()


async def upload(port: int, n: int) -> int:
    chunk = b"y" * (256 << 10)
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(f"POST /up HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: {n}\r\n\r\n".encode())
        left = n
        while left:
            piece = chunk[: min(left, len(chunk))]
            w.write(piece)
            left -= len(piece)
            await w.drain()
        await read_head(r)
        return n
    finally:
        w.close()


async def small_requests(port: int, count: int, keepalive: bool) -> float:
    t0 = time.perf_counter()
    if keepalive:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        try:
            for _ in range(count):
                w.write(f"GET /small=0 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
                await w.drain()
                _, headers = await read_head(r)
                n = int(headers.get("content-length") or 0)
                if n:
                    await r.readexactly(n)
        finally:
            w.close()
    else:
        async def one():
            r, w = await asyncio.open_connection("127.0.0.1", port)
            try:
                w.write(f"GET /small=0 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
                await w.drain()
                await read_head(r)
            finally:
                w.close()

        await asyncio.gather(*(one() for _ in range(count)))
    return count / (time.perf_counter() - t0)


async def wait_ready(port: int, host: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"GET /small=0 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            await w.drain()
            status, _ = await asyncio.wait_for(read_head(r), 2)
            w.close()
            if status == 200:   # 必须真的打通，404 说明客户端还没注册上
                return
        except (OSError, asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        await asyncio.sleep(0.1)
    raise SystemExit(f"等待 {port} 就绪超时")


def mbps(nbytes: float, dt: float) -> str:
    return f"{nbytes / dt / 1e6:>6.0f} MB/s"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conns", type=int, default=4)
    ap.add_argument("--window", type=int, default=256, help="每流信用窗口 KB")
    ap.add_argument("--size", type=int, default=64, help="单流体积 MB")
    ap.add_argument("--edge-procs", type=int, default=1)
    ap.add_argument("--origin-only", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s | %(message)s")
    size = args.size << 20
    window = args.window * 1024
    op = free_port()
    public_ports = [free_port() for _ in range(args.edge_procs)]
    agent_ports = [free_port() for _ in range(args.edge_procs)]

    tmp = Path(tempfile.mkdtemp(prefix="nifetun-bench-"))
    procs: list[subprocess.Popen] = []

    def spawn(argv: list[str]) -> subprocess.Popen:
        p = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return p

    try:
        # 源站
        spawn([PY, str(ROOT / "bench.py"), "--origin-only", str(op)])
        # edge（可多个进程）
        for i, (pp, ap_) in enumerate(zip(public_ports, agent_ports)):
            cfg = {
                "token": TOKEN, "allowed_hosts": ["*.test"],
                "public_listen": f"127.0.0.1:{pp}", "client_listen": f"127.0.0.1:{ap_}",
                "window": window, "access_log": False,
            }
            path = tmp / f"edge{i}.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            spawn([PY, str(ROOT / "tunneld.py"), "-c", str(path)])
        # agent（同时连所有 edge）
        acfg = {
            "client": "bench",
            "token": TOKEN,
            "tls": False,
            "servers": [f"127.0.0.1:{p}" for p in agent_ports],
            "connections": args.conns, "window": window, "heartbeat": 30,
            "tunnels": [{"host": HOST, "local": f"127.0.0.1:{op}"}],
        }
        apath = tmp / "client.json"
        apath.write_text(json.dumps(acfg), encoding="utf-8")
        spawn([PY, str(ROOT / "tunnelc.py"), "-c", str(apath)])

        for pp in public_ports:
            await wait_ready(pp, HOST)

        loop = runtime.loop_name()
        print(f"\n事件循环={loop}  连接池={args.conns}  窗口={args.window}KB  "
              f"edge进程={args.edge_procs}  平台={sys.platform}")
        print("-" * 72)

        # 基线：绕过隧道直连源站，说明源站本身的上限
        t0 = time.perf_counter()
        got = await download(op, size, host="127.0.0.1")
        dt = time.perf_counter() - t0
        print(f"基线 直连源站      {args.size:>5}MB  {mbps(got, dt)}")

        pp = public_ports[0]
        t0 = time.perf_counter()
        got = await download(pp, size)
        dt = time.perf_counter() - t0
        print(f"隧道 单流下载      {args.size:>5}MB  {mbps(got, dt)}")

        t0 = time.perf_counter()
        await upload(pp, size)
        dt = time.perf_counter() - t0
        print(f"隧道 单流上传      {args.size:>5}MB  {mbps(size, dt)}")

        for k in (4, 16, 64):
            per = max(size // (k // 4), 4 << 20)
            ports = (public_ports * k)[:k]
            t0 = time.perf_counter()
            res = await asyncio.gather(*(download(p, per) for p in ports))
            dt = time.perf_counter() - t0
            print(f"隧道 {k:>3} 流并发    {sum(res)>>20:>5}MB  {mbps(sum(res), dt)}")

        rps = await small_requests(pp, 300, keepalive=True)
        print(f"keep-alive 小请求 x300     {rps:>8.0f} req/s")
        rps = await small_requests(pp, 300, keepalive=False)
        print(f"新建连接 小请求 x300       {rps:>8.0f} req/s")
        print("-" * 72)
    finally:
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    # --origin-only 必须在进入事件循环之前处理
    if "--origin-only" in sys.argv:
        port = int(sys.argv[sys.argv.index("--origin-only") + 1])
        sys.exit(runtime.run(origin_main(port)))
    sys.exit(runtime.run(main()))
