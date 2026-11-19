"""从 TLS ClientHello 里抠出 SNI。

HTTPS 透传要用：edge 只看 ClientHello 的 server_name 决定把字节流转给哪个隧道，
TLS 握手和证书完全由内网源站自己完成，edge 既不需要证书也不解密。
"""

from __future__ import annotations

HANDSHAKE = 0x16
CLIENT_HELLO = 0x01
EXT_SERVER_NAME = 0x0000
MAX_HELLO = 16 * 1024


def parse_sni(buf: bytes) -> tuple[str | None, bool]:
    """返回 (sni, done)。

    done=False 表示"数据还不够，继续等"；done=True 但 sni=None 表示这不是
    可识别的 TLS ClientHello（或没有 SNI 扩展），调用方应当拒绝。
    """
    if len(buf) < 5:
        return None, False
    if buf[0] != HANDSHAKE:
        return None, True  # 不是 TLS 握手记录
    rec_len = int.from_bytes(buf[3:5], "big")
    if len(buf) < 5 + rec_len:
        return None, False
    hs = buf[5:5 + rec_len]
    if len(hs) < 4 or hs[0] != CLIENT_HELLO:
        return None, True
    hs_len = int.from_bytes(hs[1:4], "big")
    if hs_len + 4 > len(hs):
        # ClientHello 跨了多条 TLS 记录，这种我们不处理（实际极少见）
        return None, True

    p = 4
    try:
        p += 2 + 32  # client_version + random
        p += 1 + hs[p]  # session_id
        p += 2 + int.from_bytes(hs[p:p + 2], "big")  # cipher_suites
        p += 1 + hs[p]  # compression_methods
        if p + 2 > len(hs):
            return None, True  # 没有扩展块
        ext_end = min(p + 2 + int.from_bytes(hs[p:p + 2], "big"), len(hs))
        p += 2
    except IndexError:
        return None, True

    while p + 4 <= ext_end:
        etype = int.from_bytes(hs[p:p + 2], "big")
        elen = int.from_bytes(hs[p + 2:p + 4], "big")
        p += 4
        if etype == EXT_SERVER_NAME:
            # 扩展数据：name_list_len(2) + name_type(1) + name_len(2) + name
            q = p + 2
            if q + 3 > len(hs):
                return None, True
            name_type = hs[q]
            nlen = int.from_bytes(hs[q + 1:q + 3], "big")
            if name_type != 0 or q + 3 + nlen > len(hs):
                return None, True
            host = hs[q + 3:q + 3 + nlen].decode("ascii", "ignore").lower().rstrip(".")
            return (host or None), True
        p += elen
    return None, True
