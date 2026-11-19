#!/usr/bin/env python3
"""把客户端用 Nuitka 编译成单文件二进制。

    uv run --group build python build_bins.py

产物（打到 client/ 下）：
    Windows x64  ->  client/nifetun-client.exe
    Linux   x64  ->  client/nifetun-client

说明：
  * Nuitka 把 Python 编译成 C 再编成原生二进制，**不能交叉编译**：
    exe 必须在 Windows 上打，Linux 的必须在 Linux 上打（本仓库用 WSL Ubuntu 24.04）。
  * Windows 首次构建会自动下载 MinGW64（约 300MB，只下一次，缓存在 %LOCALAPPDATA%\\Nuitka）；
    也可以用 --msvc=latest 走已装的 Visual Studio 2022。
  * Linux 产物依赖构建机的 glibc（24.04 = glibc 2.39）。
  * **装 ccache**（Linux: `apt install ccache`；Windows: 把 ccache.exe 放进 PATH）
    能让重复构建快 2~4 倍：
    Nuitka 的 C 编译结果会被缓存，改 Python 代码后只有变化的模块重编。
  * 嫌慢可以再加 `--onefile-no-compression`：省掉 gzip 那 20 秒，但体积 7MB → 25MB。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "client"


def main() -> int:
    name = "nifetun-client.exe" if sys.platform == "win32" else "nifetun-client"
    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--remove-output",
        "--output-dir", str(OUT),
        "--output-filename", name,
        # 客户端用不到这些，少编几十个 .c 文件
        "--nofollow-import-to=unittest,doctest,pydoc,test,email,http,urllib",
        # 并行编译：默认就是 CPU 核数，显式写出来免得被 low-memory 模式降成 1
        f"--jobs={os.cpu_count() or 4}",
        # 我们的 CLI 用 -c config.json，Nuitka 的自执行保护会误判，关掉它
        "--no-deployment-flag=self-execution",
    ]
    if sys.platform == "win32":
        cmd += ["--windows-console-mode=force", "--assume-yes-for-downloads"]
    else:
        cmd += ["--assume-yes-for-downloads"]
    cmd.append(str(ROOT / "tunnelc.py"))

    print("运行:", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc
    out = OUT / name
    if not out.exists():
        print("未找到产物，检查上面的日志")
        return 1
    print(f"\n完成: {out.relative_to(ROOT)}  {out.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
