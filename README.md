# nifetun

自建内网穿透。固定一个域名，把内网服务挂到 `https://<域名>/t/<id>/`。

```
浏览器 ─443→ CDN ─回源→ 服务器(edge) ─帧→ 内网客户端 → 本地服务
```

## 装服务器

```bash
cp tunneld.example.json tunneld.json   # 改端口、开 enroll、填邮箱
uv run tunneld.py -c tunneld.json
```

线上用的是 systemd（`deploy/`）：`./start.sh` `./status.sh` `./tail.sh` `./restart.sh`。

## 装客户端

客户端是 Node 的，就在 nife 插件里（`nife-extension/client/tunnelc.mjs`）：

```bash
nife install git:github.com/OliverMao/nife-tunnel@v0.3.0
```

```
/tunnel up 8080        # → https://<域名>/t/<ID>/
/tunnel up 8080 --root # 占域名根 → https://<域名>/（第三方前端免改）
/tunnel status
/tunnel down
```

首次在新机器上会发审批邮件，点「批准接入」后自动上线；隧道 ID 服务端分配，重启后 URL 不变。

## 运维

```bash
./status.sh      # 状态 + 当前隧道
./tail.sh        # 看日志
./restart.sh
systemctl status nifetun-edge
```

管理 API（只监听 127.0.0.1，要 Bearer token）：`/api/status`、`/api/approve`、`/api/kick`。

## 特点

- **快**：零 await 热路径、每流信用窗口、连接池、帧头不拷贝负载
- **不暴露源站 IP**：浏览器和客户端都只跟 CDN 说话
- **安全**：每客户端独立 token + challenge-response；邮件审批；Host 白名单；ID 服务端分配
- **客户端零依赖**：单文件二进制（Win/Linux x64），不需要 systemd

## 作为 nife 插件

`nife-extension/` 是一个 nife 包（**纯 Node，客户端脚本就在包里**，不需要编译和下载）：

```bash
nife install git:github.com/OliverMao/nife-tunnel@v0.2.0
```

```
/tunnel up 8080        # → https://<域名>/t/<ID>/
/tunnel up 8080 --root # 占域名根 → https://<域名>/（第三方前端免改）
/tunnel status
/tunnel down
```

也可以直接跟 nife 说“帮我把 3000 端口挂到公网”。详见 [nife-extension/README.md](nife-extension/README.md)。

## 测试

```bash
uv run selftest.py         # 13 项端到端（驱动 Node 客户端）
uv run selftest_enroll.py  # 审批流程 7 项
```

---

细节（协议、部署拓扑、全部配置项、设计取舍）见 [docs/DESIGN.md](docs/DESIGN.md)；客户端用户手册见 [client/README.md](client/README.md)。
