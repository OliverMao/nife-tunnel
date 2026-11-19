#!/usr/bin/env python3
"""打包：把 edge / agent 各自打成一个自包含的单文件。

    uv run python build.py

产物：
    dist/tunnelc.py   单文件 agent。**零第三方依赖**，只需要 Python 3.10+，
                      直接拷到内网机器上 `python tunnelc.py -c tunnelc.json` 就能跑。
    dist/tunneld.py   单文件 edge。装了 uvloop / msgspec 会自动用上（没装也能跑）。

原理：从入口脚本出发做一次 import 图遍历，只收集真正用到的模块，
把 `from .x import y` 改写成绝对导入，再依次 exec 进内存里的 `nifetun` 包。
所以客户端产物里不会有 server.py / admin.py / sni.py。
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "nifetun"
DIST = ROOT / "dist"

REL_IMPORT = re.compile(r"^from \.(\w+)? import (.*)$", re.M)
FUTURE = re.compile(r"^from __future__ import annotations\n", re.M)


def module_src(name: str) -> str:
    return (PKG / f"{name}.py").read_text(encoding="utf-8")


def deps_of(name: str) -> list[str]:
    out: list[str] = []
    for m in REL_IMPORT.finditer(module_src(name)):
        sub, names = m.group(1), m.group(2)
        if sub:
            out.append(sub)
        else:
            out.extend(n.strip() for n in names.split(",") if n.strip())
    return out


def order(roots: list[str]) -> list[str]:
    seen: set[str] = set()
    seq: list[str] = []

    def visit(name: str) -> None:
        if name in seen:
            return
        if not (PKG / f"{name}.py").exists():
            raise SystemExit(f"找不到模块 nifetun.{name}")
        seen.add(name)
        for d in deps_of(name):
            visit(d)
        seq.append(name)

    for r in roots:
        visit(r)
    return seq


def rewrite(src: str) -> str:
    src = re.sub(r"^from \.(\w+) import", r"from nifetun.\1 import", src, flags=re.M)
    src = re.sub(r"^from \. import", "from nifetun import", src, flags=re.M)
    leftover = [ln for ln in src.splitlines() if ln.startswith("from .")]
    if leftover:
        raise SystemExit(f"还有相对导入没处理: {leftover}")
    return src


def embed(src: str) -> str:
    """用原始三引号字符串原样嵌入，保持产物可读（源码里没有 ''' 也没有行尾续行）。"""
    if "'''" in src or re.search(r"\\\n", src):
        return repr(src)
    return "r'''" + src + "'''"


HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""{title}

自动生成，请勿手改。源码在 nifetun/，重新打包：uv run python build.py
{usage}
"""

import sys
import types as _types

_PKG = _types.ModuleType("nifetun")
_PKG.__path__ = []
sys.modules["nifetun"] = _PKG


def _load(_fullname, _src):
    _m = _types.ModuleType(_fullname)
    _m.__package__ = _fullname.rpartition(".")[0]
    sys.modules[_fullname] = _m
    setattr(_PKG, _fullname.rpartition(".")[2], _m)
    exec(compile(_src, "<" + _fullname + ">", "exec"), _m.__dict__)


'''


def build(entry: str, roots: list[str], title: str, usage: str) -> Path:
    entry_src = (ROOT / entry).read_text(encoding="utf-8")
    entry_src = FUTURE.sub("", rewrite(entry_src), count=1)

    banner = title + chr(10) + "打包时间: " + str(datetime.date.today())
    parts = [HEADER.format(title=banner, usage=usage)]
    mods = order(roots)
    for name in mods:
        src = rewrite(module_src(name))
        parts.append(f'_load("nifetun.{name}", {embed(src)})\n\n')
    parts.append("# " + "=" * 70 + "\n# 入口：" + entry + "\n# " + "=" * 70 + "\n")
    parts.append(entry_src)

    DIST.mkdir(exist_ok=True)
    out = DIST / entry
    out.write_text("".join(parts), encoding="utf-8")

    try:
        compile(out.read_text(encoding="utf-8"), str(out), "exec")
    except SyntaxError as e:  # pragma: no cover
        out.unlink()
        raise SystemExit(f"打包结果有语法错误: {e}")

    size = out.stat().st_size
    print(f"{out.relative_to(ROOT)}  {size / 1024:.1f} KB  模块 {len(mods)} 个: {', '.join(mods)}")
    return out


def main() -> int:
    build(
        "tunnelc.py",
        ["config", "runtime", "client"],
        "nifetun agent（单文件，零第三方依赖）",
        "用法:  python tunnelc.py -c tunnelc.json",
    )
    build(
        "tunneld.py",
        ["config", "runtime", "server"],
        "nifetun edge（单文件；装 uvloop/msgspec 会自动加速）",
        "用法:  python tunneld.py -c tunneld.json",
    )
    # 顺手把最小配置样例放进去，拷客户端时只用拿两个文件
    for name in ("tunnelc.example.json", "tunneld.example.json"):
        (DIST / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    print(f"dist/ 就绪：客户端只需 dist/tunnelc.py + dist/tunnelc.example.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
