# nifetun

自建的 Cloudflare Tunnel 风格内网穿透 —— 面向**多隧道同时在线 + 要性能**的场景。

```
浏览器 ──> edge(你的服务器) ──── N 条长连接(N=连接池) ────> 客户端（内网） ──> 127.0.0.1:8080
           公网入口 8443            每条连接多路复用           主动拨出        本地服务
           按 Host/端口路由          每流独立信用窗口流控
```

- 内网侧不需要公网 IP、不做端口映射，只有出站连接。
- 一条连接上多路复用所有请求；**每条流独立信用窗口**，慢流堵不住快流。
- HTTP / WebSocket / SSE / keep-alive 全透传 —— 一条浏览器连接 = 一条隧道流，只解析开头 4KB 的 `Host`。
- 裸 TCP 隧道（SSH / MySQL / RDP / 游戏服）同样支持。
- **HTTPS 透传**：按 TLS ClientHello 的 SNI 路由，证书与握手完全由内网源站自己完成。
- **管理 API**：`/api/status`、`/api/health`、`/api/kick`（踢掉某个 客户端）。
- **配置热重载**：改 客户端的 `tunnels` 几秒内自动生效，不用重启、不用断已有连接。
- 多核扩展：跑 N 个 edge 进程，前面 nginx `stream` 摊流量，客户端同时连全部进程。
- **服务端与客户端彻底分离**：客户端能打成一个单文件，零第三方依赖。

## 部署：服务端 / 客户端分离

| | 需要什么 |
|---|---|
| **服务端**（公网机器） | 整个仓库，`uv sync` 装依赖（uvloop/msgspec 提速），跑 `tunneld.py` |
| **客户端**（内网机器） | Node 脚本 `nife-extension/client/tunnelc.mjs`，零第三方依赖；用 nife 插件一行命令装上 |

```bash
uv run python build.py        # 在开发机上打包
```

```
nife-extension/client/tunnelc.mjs   Node 客户端（零依赖，走 CDN 443 的 WebSocket）
dist/tunneld.py    50 KB   单文件 edge，装了 uvloop/msgspec 会自动用上
```

打包器会做一次 import 图遍历，**客户端产物里不会有 server.py / admin.py / sni.py**，
只包含它真用到的 8 个模块。拷到内网机器后：

```bash
/tunnel up 8080                          # 装了 nife 插件就这么用
```

> 也可以用单文件 `dist/tunneld.py` 部署 edge（Docker 镜像很小、或者不想在服务器上放源码时用），
> 功能完全一样，只是拿不到 uvloop 以外的加速。

## 技术栈

Python **3.12**（`uv` 管理虚拟环境），依赖全部为当前最新版：

| 依赖 | 用途 | 说明 |
|---|---|---|
| `uvloop` | 事件循环 | 仅 Linux/macOS，吞吐显著高于原生 asyncio；Windows 自动跳过 |
| `msgspec` | 控制帧/配置的 JSON 编解码 | 比 stdlib `json` 快数倍 |

```bash
uv sync --python 3.12        # 建 .venv 并装依赖
uv run tunneld.py --gen-token
```

## 快速开始

```bash
# 1. 服务器：复制 tunneld.example.json → tunneld.json（开 enroll + email），启动
uv run tunneld.py -c tunneld.json

# 2. 内网机器：tunnelc.json 就写两行（见下），启动 → 自动发审批邮件
nife   # 然后 /tunnel up 8080

# 3. 端到端自测（14 项断言）+ 性能基准
uv run selftest.py

```

## 部署拓扑

### A. 自己占 8443 端口的 TLS

```json
{
  "token": "<token>",
  "public_listen": "0.0.0.0:8443",
  "client_listen": "0.0.0.0:9443",
  "public_tls_cert": "/etc/letsencrypt/live/tun.example.com/fullchain.pem",
  "public_tls_key":  "/etc/letsencrypt/live/tun.example.com/privkey.pem",
  "allowed_hosts": ["*.tun.example.com"]
}
```

DNS：`*.tun.example.com A <服务器IP>`；泛域名证书用 certbot 的 DNS-01：

```bash
certbot certonly --manual --preferred-challenges dns -d '*.tun.example.com' -d tun.example.com
```

访问 `https://app.tun.example.com:8443`。防火墙只放行 8443；9443 只给 客户端 用（也可像本仓库的部署那样，让客户端走 CDN 443 的 WebSocket，源站 IP 完全不暴露）。

### B. nginx 已占 443（推荐，URL 不带端口）

nginx 终止 TLS，nifetun 完全不碰证书：

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name *.tun.example.com;
    ssl_certificate     /etc/letsencrypt/live/tun.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tun.example.com/privkey.pem;

    client_max_body_size 0;
    proxy_buffering off;              # SSE / 大文件必须
    proxy_request_buffering off;
    proxy_read_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

`tunneld.json` 里 `public_tls_cert/key` 留 `null`，`public_listen` 用 `127.0.0.1:8443`。

### C. 多核扩展（edge 多进程）

单进程 Python 只能吃一个核。要榨干多核，就跑 N 个 edge 进程（各一个端口）+ nginx L4 分摊；
客户端 的 `servers` 写上全部端口，它会同时连每一台，于是每个进程都有完整的路由表：

```nginx
# nginx.conf — L4 分摊（连 TLS 都不用拆，SNI 透传）
stream {
    upstream edges { least_conn; server 127.0.0.1:8443; server 127.0.0.1:8444; }
    server { listen 443; proxy_pass edges; }
}
```

```json
// tunnelc.json
"servers": ["tun.example.com:9443", "tun.example.com:9444"],
"connections": 4
```

`selftest.py` 里的多通道用例就是这套拓扑的本地验证。

### D. 裸 TCP 隧道

```json
// tunneld.json
"tcp_listeners": [{ "listen": "0.0.0.0:2222", "host": "ssh-local" }]
// tunnelc.json
{ "host": "ssh-local", "local": "127.0.0.1:22" }
```

`allowed_hosts` 里要放行 `ssh-local`，然后 `ssh -p 2222 you@服务器IP`。

### E. HTTPS 透传（源站自己持证书）

想给一个内网服务上 HTTPS，但不想把私钥放到公网 edge 上？用 SNI 透传：

```json
// tunneld.json —— 443 不配证书，完全靠 SNI 找隧道
"tcp_listeners": [{ "listen": "0.0.0.0:443", "mode": "sni" }],
"allowed_hosts": ["secure.example.com"]
```

```json
// tunnelc.json —— 源站 127.0.0.1:8443 自己终结 TLS
{ "host": "secure.example.com", "local": "127.0.0.1:8443" }
```

edge 只做两件事：从 ClientHello 里读出 `server_name`，然后把整条字节流（包括 ClientHello
本身）原样转给对应 客户端。证书、握手、加密全部在内网源站完成，edge 看不到明文。

> 限制：只识别标准 ClientHello（单个 TLS 记录内、带 SNI 扩展）；客户端不支持 SNI（很老的库）或
> 用 IP 直连时无法路由。

## 管理 API 与热重载

```json
// tunneld.json
"admin_listen": "127.0.0.1:9444",
"admin_token": "<随机串>"     // 不设就是无鉴权，只能本地用
```

```bash
curl -s localhost:9444/api/health
curl -s -H 'Authorization: Bearer <token>' localhost:9444/api/status | jq
# {"uptime_s":12.3,"prefix":"/t",
#  "clients":[{"peer":"1.2.3.4:5566","client":"home","ids":["as981923"],
#              "hosts":[],"streams":2,"connected_s":11.0}],
#  "routes_by_id":{"as981923":["1.2.3.4:5566"]},"streams_total":2}

# 踢掉某个 客户端（它会自动重连并重新注册）
curl -s -X POST -H 'Authorization: Bearer <token>' \
     'localhost:9444/api/kick?host=app.tun.example.com'
```

**热重载**：客户端 默认每 `reload_interval`（5s）比对配置文件 mtime，改了 `tunnels` 就向所有
edge 连接重新发一次 register，edge 增量更新路由表（新增/撤下 host），**已有连接和流不受影响**。
Linux 上也可以 `kill -HUP <客户端进程号>` 立即生效。

### F. 阿里云 ESA / CDN（只有一个域名时的正确做法）

**不要**每次连接动态加子域名 —— 云解析免费版 TTL 最低 600 秒，ESA 加速域名配置下发也是分钟级，
1 小时十几次的 churn 根本跟不上（而且会堆出一堆僵尸加速域名、耗尽额度）。

正确做法：**固定一个加速域名 + 路径前缀**，DNS/ESA/证书永不改动，churn 只改内存路由表。

```
nife-tunnel.yoobit.cn  ──CNAME──>  阿里分配的 ESA 地址    （源站 = 你的服务器IP:8443）
```

`tunneld.json`：

```json
{
  "clients": [{"name": "home", "token": "<自动生成>", "ids": ["*"], "hosts": ["*"]}],
  "clients_file": "/root/NifeTunnel/clients.json",
  "ids_file": "/root/NifeTunnel/tunnel-ids.json",
  "public_listen": "0.0.0.0:8443",
  "client_listen": "0.0.0.0:9443",
  "prefix": "/t",
  "strip_prefix": true,
  "prefix_hosts": ["nife-tunnel.yoobit.cn"]
}
```

`tunnelc.json`（客户端只写两个字段，ID 由服务端分配）：

```json
{
  "server": "https://nife-tunnel.yoobit.cn",
  "tunnels": ["127.0.0.1:8080", "192.168.1.10:5000"]
}
```

访问：`https://nife-tunnel.yoobit.cn/t/<edge 分配的 ID>/`（客户端日志里会打印完整 URL，
分配结果存 `nifetun-ids.json`，**重连后 URL 不变**）
- 客户端频繁上下线 **零成本**：edge 只在内存里加/删一条映射，不动 DNS/ESA/证书。
- ESA 那边只用配一次：加速域名 `nife-tunnel.yoobit.cn` + 源站 `你的IP:8443`（回源协议 HTTP 即可，
  证书由 ESA 对客户端提供）。大陆节点加速要求该域名已备案（子域继承主域名 `yoobit.cn` 的备案）。
- `strip_prefix: true` 时源站看到的是干净路径，同时会带上 `X-Forwarded-Prefix: /t/as981923`。
  **注意**：如果源站会回 `Location: /login` 这种根相对跳转，浏览器会丢掉前缀而 404 —— 两个解法：
  ① 应用里用相对路径，或读 `X-Forwarded-Prefix` 拼 URL；② 把 `strip_prefix` 设为 `false`，
  让前缀保留在源站路径里（应用需支持 base path）。
- 如果安全上想收紧：`public_listen` 绑内网 + 只允许 ESA 回源 IP，或给 `allowed_ids` 写白名单。

## 性能设计

热路径全在 `asyncio.Protocol` 回调里同步完成，**不建 Task、不 await、不排队**：

| 机制 | 作用 |
|---|---|
| 一条公网 TCP 连接 = 一条流 | 只解析开头 4KB 取 `Host`，之后纯字节搬运，不做 HTTP 状态机 |
| 每流信用窗口（默认 256KB） | 慢流吃满自己的窗口就停，不会占住整条连接（消除 HOL） |
| 窗口更新攒批发（阈值 window/4） | 每 64KB 数据只回一个 13 字节的 WU 帧 |
| 连接池（`connections`，默认 4） | 同一 host 多条连接，edge 按 least-stream 分流，进一步摊薄 HOL |
| 三层背压 | 源 socket `pause_reading()` ← 信用窗口 ← 目标 transport 写水位（256KB/64KB） |
| 帧头发送不拷贝负载 | `transport.write(hdr)` + `transport.write(payload)`，省掉一次 memcpy |
| `TCP_NODELAY` + 关闭压缩 | 每个连接建立时设置 |

本机实测（Windows 开发机，源站/客户端/edge/压测端四个独立进程；该机基线抖动很大）：

```
基线 直连源站     64MB   105~167 MB/s
隧道 单流下载     64MB    43~66  MB/s
隧道 4~16 流并发         ~56 MB/s
keep-alive 小请求        ~800 req/s
```

即隧道本身大约带来 2 倍开销（多一次进程内拷贝 + 帧处理），单流能跑到直连的一半左右。
**生产在 Linux + uvloop 上会明显更好**，请在服务器上自测。

## 协议

帧头固定 9 字节：`stream_id(u32) | type(u8) | length(u32)` + payload。

| type | 名称 | 说明 |
|---|---|---|
| 1 | OPEN | edge→客户端 开流，payload 为 `{host, w, remote}`，`w` 是本端接收窗口 |
| 2 | DATA | 原始字节，≤64KB/帧 |
| 3 | FIN | 半关闭（等价 TCP FIN），双向都 FIN 后流结束 |
| 4 | RST | 立即废弃 |
| 5 | CTRL | stream 0 上的 JSON：challenge / auth / register / registered / error / ping / pong |
| 6 | WU | 窗口更新，payload 为 u32 增量 |

## 安全性

### 1. 客户端身份：challenge-response，token 不过网

```
edge                                  客户端
 │  CTRL {type:challenge, nonce:<16B>} ─→
 │  ←─ CTRL {type:auth, client, proof:HMAC-SHA256(token, nonce)}
 │  比对 + nonce 立即作废（防重放）
```

抓包只能拿到 `nonce` 和 `proof`，**拿不到 token 本身**。每个客户端一个独立 token，
泄漏一个只影响一台机器，可以单独吊销（从 `clients` 里删掉即可）。

### 2. 接入审批制：申请 → 邮件按钮 → token 自动下发

```
客户端(无 token)                 edge                       管理员邮箱
 │ enroll {client:"home"} ─→  pending 表 ─→ 邮件(批准/拒绝按钮)
 │                              ←─ 点「批准接入」(_nifetun/approve/<nonce>)
 │ ←─ CTRL {type:enroll_ok, token}   ── token 只在 TLS 通道里传
 │ 写进 nifetun-credentials.json，重连
```

- nonce 一次性、10 分钟过期；点过就作废，转发给别人也没用。
- **token 从不经过聊天工具/人工传递**，解决了"发给被监控机器"时的泄漏问题。
- 已知客户端名不能重复申请（防冒名顶替），管理员看到邮件里显示的是主机名。

### 3. 源站 IP 不暴露

客户端和浏览器都只跟 CDN（阿里云 ESA）说话：

```
浏览器 ──443──→ ESA ──回源 http:8443──→ edge ──帧──→ 客户端 ──→ 内网服务
客户端 ──443──→ ESA ──回源 http:8443──→ edge（/_nifetun/ws，WebSocket）
```

- 客户端的 `server` 写的是**域名**，源站 IP 只出现在 ESA 的回源配置里。
- 客户端控制通道走 WebSocket（RFC6455，自己实现的 300 行，零依赖），
  路径 `/_nifetun/ws`，和普通网站流量共用 443。
- 想直连也可以（`servers: ["<服务器IP>:9443"]`），但那样客户端配置里就有源站 IP 了。

### 4. TLS：全自动，配置里没有 `tls_*`

| 连的地址 | 信任方式 |
|---|---|
| `https://域名` | CDN 的公网证书，按系统 CA 校验（`check_hostname=True`） |
| `IP:9443` | 自签证书没法走 CA → **TOFU**：首次连接记下 sha256 指纹（存 `nifetun-credentials.json`），以后不一致直接拒绝 |

### 5. 其它

- **Host 白名单**：`prefix_hosts: ["nife-tunnel.yoobit.cn"]`，绕过 CDN 直连回源端口发请求 → 403。
- **隧道 ID 服务端分配**：12 位（48 bit 熵），`ids_file` 里记归属，别的客户端带同一个 ID 来注册会被拒绝。
- **管理 API** 只监听 `127.0.0.1`，要 Bearer token。
- 轮换建议：`tunneld.json` 里的邮箱授权码、阿里云 AK、服务器 root 密码都应定期换。

## 客户端配置（全部字段）

最简（**推荐，就这么写**）：

```json
{
  "server": "https://nife-tunnel.yoobit.cn",
  "tunnels": ["127.0.0.1:8080"]
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `server` | 必填 | `https://域名` 走 CDN 443（WebSocket）；也可写 `IP:9443` 直连、`tls://host:port`、`tcp://host:port`（明文，仅调试） |
| `servers` | — | 数组形式，多台 edge 同时连（每台都拿到完整路由表） |
| `tunnels` | 必填 | `["ip:port", ...]`；也可写对象：`{"local": "127.0.0.1:8080", "host": "app.example.com"}`（走独立域名） |
| `client` | 主机名 | 审批邮件/日志里显示的名字 |
| `connections` | 4 | 到 edge 的连接条数（连接池） |
| `window` | 262144 | 每流信用窗口（字节） |
| `heartbeat` / `dead_after` | 15 / 60 | 心跳间隔 / 判死（秒） |
| `reconnect_max_delay` | 30 | 重连退避上限（秒） |
| `reload_interval` | 5 | 配置热重载检查间隔（0 = 关） |

自动生成、不用手写的文件（都在配置文件同目录）：

| 文件 | 内容 |
|---|---|
| `nifetun-credentials.json` | token（审批后自动写入）+ 直连时的证书指纹；权限 600 |
| `nifetun-ids.json` | 服务端分配的隧道 ID，保证重启后 URL 不变 |

## 服务端配置（全部字段）

```json
{
  "clients": [{"name": "home", "token": "<自动生成>", "ids": ["*"], "hosts": ["*"]}],
  "clients_file": "/root/NifeTunnel/clients.json",
  "ids_file": "/root/NifeTunnel/tunnel-ids.json",
  "enroll": true,
  "email": {"host": "smtp.163.com", "port": 465, "user": "x@163.com",
            "password": "<授权码>", "to": ["x@163.com"],
            "approve_base": "https://nife-tunnel.yoobit.cn"},
  "public_listen": "0.0.0.0:8443",
  "client_listen": "0.0.0.0:9443",
  "admin_listen": "127.0.0.1:9444",
  "admin_token": "<随机串>",
  "prefix": "/t",
  "strip_prefix": true,
  "prefix_hosts": ["nife-tunnel.yoobit.cn"],
  "id_len": 12,
  "window": 262144
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `public_listen` | `0.0.0.0:8443` | 公网入口（ESA 回源到这里） |
| `client_listen` | `0.0.0.0:9443` | 客户端拨入（直连模式才需要外网可达） |
| `admin_listen` / `admin_token` | 无 | 管理 API，建议只绑 127.0.0.1 |
| `clients` / `clients_file` | — | 每客户端 token + `ids`/`hosts` 白名单；落盘文件 |
| `enroll` / `email` / `enroll_timeout` | — | 邮件审批 |
| `ids_file` / `id_len` | — / 12 | 隧道 ID 归属表 / 长度 |
| `prefix` / `strip_prefix` / `prefix_hosts` | `/t` / true / `[]` | 路径前缀路由、是否从请求里剥掉、Host 白名单 |
| `tcp_listeners` | `[]` | 裸 TCP 端口隧道 / `mode:"sni"` 的 HTTPS 透传 |
| `window` / `backlog` / `idle_timeout` | 262144 / 2048 / 90 | 窗口、监听 backlog、空闲超时 |

## 运维

```bash
cd /root/NifeTunnel
./status.sh          # systemctl 状态 + 最近日志 + 当前隧道
./restart.sh         # 重启（客户端会自动重连，URL 不变）
./tail.sh            # tail -f logs/edge.log
systemctl status nifetun-edge
journalctl -u nifetun-edge -f
```

- 日志：`logs/edge.log`（systemd `StandardOutput=append:`），logrotate 50MB × 7，`copytruncate`，不重启服务。
- 管理 API：

```bash
curl -H "Authorization: Bearer <admin_token>" http://127.0.0.1:9444/api/status
curl -X POST -H "Authorization: Bearer <admin_token>" \
     "http://127.0.0.1:9444/api/approve?client=home"     # 手动批准（不用邮件）
curl -X POST -H "Authorization: Bearer <admin_token>" \
     -d '{"host":"a.example.com"}' http://127.0.0.1:9444/api/kick
```

## 测试

```bash
uv run selftest.py         # 14 项：路由/前缀/TCP/SNI/上传/热重载/踢线重连/WS 通道/服务端分配 ID
uv run selftest_enroll.py  # 6 项：邮件审批全流程

```

## 目录

```
nifetun/
  wire.py        帧格式常量 + 9 字节帧头 struct
  mux.py         多路复用 + 每流信用窗口（edge/客户端共用）
  bridge.py      socket ↔ 流的桥（零 await 热路径）
  edge_proto.py  公网接入：HTTP 前缀/Host 路由、裸 TCP、SNI 透传、WebSocket 升级
  ws.py          最小 RFC6455 实现（客户端控制通道走 CDN 443 用）
  sni.py         只读 ClientHello 里的 SNI
  server.py      Edge + ClientConn（认证/审批/ID 分配/路由表）
  client.py      客户端（连接池、热重载、TOFU 指纹、本地服务桥）
  admin.py       管理 API
  notify.py      邮件审批（smtplib）
  config.py / jsonx.py / util.py / runtime.py   配置、JSON、杂项、事件循环
tunneld.py                  edge 入口
build.py / build_bins.py    单文件 .py 打包 / Nuitka 二进制造
deploy/                     服务器 systemd unit + 运维脚本 + 配置模板
client/                     客户端二进制 + 配置 + 使用说明
tools/esa_setup.py          阿里云 ESA 加速域名 / 回源规则（幂等，check/setup）
```

## 已知限制

- 一个公网连接 = 一条流，HTTP/1.1 keep-alive 能复用，但 WebSocket/SSE 会占住一条连接（连接池可缓解）。
- 不支持 UDP / HTTP/3 回源；QUIC 隧道做过又删了（纯 Python 逐包加密比内核 TLS 慢）。
- 亚洲节点加速的域名必须已备案（子域继承主域名）。
- 客户端需要 Node（nife 本身就是 Node 应用，装了 nife 就有）。
