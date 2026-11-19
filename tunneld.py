#!/usr/bin/env python3
"""tunneld —— edge 端（公网服务器）。

    uv run tunneld.py -c tunneld.json
    uv run tunneld.py --gen-token
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from nifetun import config, notify, runtime
from nifetun.server import Edge


async def amain(cfg: dict) -> None:
    edge = Edge(cfg)
    await edge.start()
    try:
        await edge.serve_forever()
    finally:
        await edge.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="nifetun edge")
    ap.add_argument("-c", "--config", default="tunneld.json")
    ap.add_argument("--gen-token", action="store_true", help="生成随机 token 后退出")
    ap.add_argument("--test-email", metavar="ADDR",
                    help="发一封测试邮件到 ADDR（验证 SMTP 配置，需 export NIFETUN_SMTP_PASS）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return 0

    if args.test_email:
        runtime.setup_logging(args.verbose)
        cfg = config.load(args.config, "edge")
        ecfg = dict(cfg.get("email") or {})
        ecfg["to"] = [args.test_email]
        subject, html = notify.enroll_mail(
            client="smoke-test", peer="0.0.0.0:0",
            approve_url="https://example.invalid/_nifetun/approve/DEMO",
            deny_url="https://example.invalid/_nifetun/deny/DEMO",
            expires_min=10,
            extra={"这是什么": "邮件审批通道连通性测试，点按钮无效"},
        )
        ok = runtime.run(notify.send_async(ecfg, subject, html))
        print("发送成功" if ok else "发送失败（看上面的日志）")
        return 0 if ok else 1

    runtime.setup_logging(args.verbose)
    cfg = config.load(args.config, "edge")
    import logging

    logging.getLogger("nifetun").info("事件循环: %s", runtime.loop_name())
    try:
        runtime.run(amain(cfg))
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
