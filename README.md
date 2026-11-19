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

配置就两个字段：

```json
{
  "server": "https://nife-tunnel.yoobit.cn",
  "tunnels": ["127.0.0.1:8080"]
}
```

```bash
./nifetun-client -c tunnelc.json
# 或者不写文件：
./nifetun-client --json '{"server":"https://nife-tunnel.yoobit.cn","tunnels":["127.0.0.1:8080"]}'
```

- 首次启动发**审批邮件**，点「批准接入」就上线，token 自动落盘
- 隧道 ID 服务端分配，日志里直接给完整 URL，重启不变
- 加服务就往 `tunnels` 加一行，**5 秒生效**，服务器不用动
- TLS / token / 名字 / 端口全都不用配

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

`nife-extension/` 是一个自包含的 nife 扩展（含二进制），拷到 `~/.nife/agent/extensions/nifetun/` 就能用：

```
/tunnel up 8080        # 本机 8080 → https://nife-tunnel.yoobit.cn/t/xxxx/
/tunnel status
/tunnel down
```

也可以直接跟 nife 说“帮我把 3000 端口挂到公网”。详见 [nife-extension/README.md](nife-extension/README.md)。

## 测试

```bash
uv run selftest.py         # 14 项端到端
uv run selftest_enroll.py  # 审批流程 6 项
uv run bench.py --conns 4 --size 64
```

---

细节（协议、部署拓扑、全部配置项、设计取舍）见 [docs/DESIGN.md](docs/DESIGN.md)；客户端用户手册见 [client/README.md](client/README.md)。
