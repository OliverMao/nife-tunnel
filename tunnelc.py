#!/usr/bin/env python3
"""tunnelc —— 客户端（内网机器）。

    uv run tunnelc.py -c tunnelc.json
    uv run tunnelc.py --json '{"server":"https://nife-tunnel.yoobit.cn","tunnels":["127.0.0.1:8080"]}'
    uv run tunnelc.py --fingerprint nife-tunnel.yoobit.cn:443   # 排查用：打印对端证书指纹

配置文件改动会在几秒内自动生效（新增/删除隧道无需重启）；Linux 上 `kill -HUP <pid>` 可立即重载。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ssl
import sys

from nifetun import config, runtime
from nifetun.client import Client


async def amain(cfg: dict, config_path: str | None) -> None:
    client = Client(cfg, config_path=config_path)
    try:
        loop = asyncio.get_running_loop()
        try:
            import signal

            loop.add_signal_handler(signal.SIGHUP, client.request_reload)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            pass  # Windows 不支持，靠 mtime 轮询就行
        await client.run()
    finally:
        client.stop()


async def fingerprint(host: str, port: int, sni: str | None) -> int:
    """连一次，打印对端证书的 sha256 指纹（排查用；配置里不需要填）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni or host), 15
        )
    except Exception as e:
        print(f"连接 {host}:{port} 失败: {e!r}", file=sys.stderr)
        return 1
    obj = writer.get_extra_info("ssl_object")
    der = obj.getpeercert(binary_form=True) if obj else None
    writer.close()
    if not der:
        print("没能拿到证书（对方可能没开 TLS）", file=sys.stderr)
        return 1
    digest = hashlib.sha256(der).hexdigest()
    print(digest)
    print("指纹: %s" % digest, file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="nifetun 客户端")
    ap.add_argument("-c", "--config", default=None,
                    help="配置文件路径（默认 tunnelc.json）")
    ap.add_argument("--json", metavar="JSON", default=None,
                    help='直接把配置当字符串传进来，形如 {"server": "https://域名", "tunnels": ["127.0.0.1:8080"]}；'
                         '写 - 表示从 stdin 读')
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", metavar="PATH", default=None,
                    help="日志写文件而不是控制台（无窗口版本用）")
    ap.add_argument("--fingerprint", metavar="HOST:PORT", help="打印服务器证书 sha256 指纹")
    ap.add_argument("--sni", help="配合 --fingerprint 指定 SNI")
    args = ap.parse_args(argv)

    if args.fingerprint:
        host, _, port = args.fingerprint.rpartition(":")
        if not port.isdigit():
            ap.error("--fingerprint 需要 host:port")
        return runtime.run(fingerprint(host or "127.0.0.1", int(port), args.sni))

    runtime.setup_logging(args.verbose, args.log_file)

    if args.json is not None:
        # 命令行直接给 JSON：不写文件，也就没有热重载
        text = sys.stdin.read() if args.json.strip() == "-" else args.json
        cfg = config.loads(text, "client")
        try:
            runtime.run(amain(cfg, None))
        except KeyboardInterrupt:
            print("bye")
        return 0

    path = args.config or "tunnelc.json"
    cfg = config.load(path, "client")
    try:
        runtime.run(amain(cfg, path))
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
