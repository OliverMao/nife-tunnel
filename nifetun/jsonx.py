"""JSON 编解码：装了 msgspec 就用它（edge 侧控制帧更快），没有就退回标准库。

客户端单文件版（`dist/tunnelc.py`）靠这个 try/except 做到零第三方依赖。
"""

from __future__ import annotations

import json

try:
    import msgspec

    encode = msgspec.json.encode
    decode = msgspec.json.decode
    HAS_MSGSPEC = True
except ImportError:  # pragma: no cover - 单文件客户端走这里
    HAS_MSGSPEC = False

    def encode(obj) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode()

    def decode(data: bytes):
        return json.loads(data)


__all__ = ["encode", "decode", "HAS_MSGSPEC"]
