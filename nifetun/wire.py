"""nifetun wire protocol v2。

一条 TCP/TLS 长连接上多路复用多条流。帧头固定 9 字节：

    +-----------+--------+----------+---------+
    | stream_id | type   | length   | payload |
    | uint32 BE | uint8  | uint32 BE| length B|
    +-----------+--------+----------+---------+

相比 v1 增加的两件事，都是为了"多个隧道同时跑 + 要性能"：
  * **WU（窗口更新）帧**：每条流独立的信用窗口流控，慢流不会把快流堵死（消除 HOL blocking）；
  * **多连接池**：agent 对 edge 开 N 条连接，同一 host 的流按 least-stream 轮流分配，
    等于把单连接的 HOL 再除以 N，同时把内核收发并行度提上去。
"""

from __future__ import annotations

import struct

HDR = struct.Struct("!IBI")
HDR_SIZE = HDR.size
U32 = struct.Struct("!I")

# 帧类型
OPEN = 1
DATA = 2
FIN = 3
RST = 4
CTRL = 5
WU = 6

CTRL_SID = 0

MAX_FRAME = 1 << 20  # 单帧上限 1MiB（DATA 实际用 MAX_CHUNK）
MAX_CHUNK = 64 * 1024  # 单个 DATA 帧最大载荷
MAX_HEAD = 16 * 1024  # 只解析请求头，超过就 431

# 每条流的信用窗口：对端最多能积压这么多未确认字节
DEFAULT_WINDOW = 256 * 1024
# 本地 socket 写缓冲的高低水位，决定何时向上游施加背压
HIGH_WATER = 256 * 1024
LOW_WATER = 64 * 1024

#: 队列中的哨兵（agent 侧本地连接已建立前的缓冲用不到它，保留给 RST 传播）
ABORT = object()
