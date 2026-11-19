"""运行时：uvloop（Linux/macOS 有就用）+ 有界收尾 + 统一日志。

关于收尾：Windows 的 Proactor(IOCP) 循环在退出时如果还有挂起的 overlapped I/O，
`asyncio.run` 内部的 `_cancel_all_tasks` 会永远等下去（进程看起来"挂了"），
并且会把真正的异常吞掉。所以这里自己做**有时间上限**的收尾，绝不让进程卡死。
"""

from __future__ import annotations

import asyncio
import logging
import sys

GRACE = 2.0


def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """log_file 给了就写文件（无控制台的打包版本用），否则写 stderr。"""
    fmt = "%(asctime)s %(levelname)-7s %(name)-16s | %(message)s"
    if log_file:
        handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr or sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def loop_name() -> str:
    try:
        import uvloop  # noqa: F401
    except ImportError:
        return "asyncio(proactor)" if sys.platform == "win32" else "asyncio"
    return "uvloop"


def run(coro) -> int:
    """跑主协程并返回它的返回值（异常照常抛出）。"""
    try:
        import uvloop
    except ImportError:
        return _run_bounded(coro)
    return uvloop.run(coro)


def _run_bounded(coro) -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro) or 0
    finally:
        _bounded_shutdown(loop)


def _bounded_shutdown(loop: asyncio.AbstractEventLoop, grace: float = GRACE) -> None:
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=grace))
    except Exception:  # noqa: BLE001 - 收尾阶段不抛
        pass
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:  # noqa: BLE001
        pass
    try:
        loop.close()
    except Exception:  # noqa: BLE001
        pass
