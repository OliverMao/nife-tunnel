# nifetun nife 插件

让 nife 直接把本机端口穿透到公网：`/tunnel up 8080` → `https://nife-tunnel.yoobit.cn/t/xxxx/`。

这是个标准 nife package（仓库根目录的 `package.json` 里有 `pi` manifest）。

## 安装

```bash
nife install git:github.com/OliverMao/nife-tunnel@v0.2.0
```

一条命令，没有别的步骤：**客户端就在包里，是 Node 脚本**（`client/tunnelc.mjs`，零依赖），
由插件用 nife 自己的 node 拉起 —— 不打包、不下载、不需要额外环境。

```bash
nife -e git:github.com/OliverMao/nife-tunnel   # 只试用一次，不写 settings
nife remove git:github.com/OliverMao/nife-tunnel
nife list
```

## 用法

```bash
nife
> /tunnel up 8080              # 默认占用域名根 → https://<域名>/
> /tunnel up 8080 5000         # 多个端口：第一个占根，其余走 /t/<id>/
> /tunnel up 8080 --prefix     # 强制走 /t/<id>/ 前缀（多应用共用域名时用）
> /tunnel status
> /tunnel down
```

**默认占域名根**：前端不用改（`/assets/…`、`/api/…` 这些根绝对路径原样到达应用）。
同一时刻只有一条 root 隧道，别的客户端来抢会被拒绝。

直接跟 nife 说也行（模型会调工具）：

> 帮我把 3000 端口挂到公网

可用工具：

| 工具 | 说明 |
|---|---|
| `tunnel_up` | `{tunnels: ["8080" 或 "127.0.0.1:8080"], server?}` → 返回公网 URL |
| `tunnel_status` | 列出在跑的实例、PID、URL |
| `tunnel_down` | 停掉（不传 name 全停） |

## 包结构

```
package.json                   name / keywords:["pi-package"] / pi.extensions
nife-extension/index.ts        扩展本体（3 个工具 + /tunnel 命令）
nife-extension/client/tunnelc.mjs   客户端（Node，零依赖，走 CDN 443 的 WebSocket）
```

- 全 Node：不需要编译、不需要按平台下二进制，`nife install` 秒装
- 隧道进程是插件用 `process.execPath` 起的独立后台进程，`stdio: ignore`，
  **不会弹控制台窗口**；nife 退出后它继续跑
- `nife-coding-agent` / `typebox` 是 `peerDependencies` 且标 `optional`：由 nife 提供，
  不让 npm 去装（私有 registry 上装不到也不会让安装失败）


## 行为

- 客户端是**独立后台进程**，nife 退出后隧道继续跑；`/tunnel down` 或重启机器才停。
- 状态在 `~/.nife/agent/nifetun/`：

```
state.json                 实例表（pid/server/tunnels）
<name>.log                 客户端日志
nifetun-credentials.json   凭证（服务端分配 UUID + token）
nifetun-ids.json           隧道 ID（重启后 URL 不变）
```

- 首次使用会触发**邮件审批**：点「批准接入」后客户端自动上线，不用重跑命令。
- 服务端地址默认 `https://nife-tunnel.yoobit.cn`，可用 `NIFETUN_SERVER` 或工具参数覆盖。
- 同一组端口只会保留一个实例（`up` 会先把旧的停掉）。

## 排错

| 现象 | 处理 |
|---|---|
| `找不到客户端脚本` | 设 `NIFETUN_CLIENT` 指向 `client/tunnelc.mjs` |
| 一直"等待审批" | 看邮箱；或管理员 `curl -X POST -H "Authorization: Bearer <token>" 'http://127.0.0.1:9444/api/approve?client=<uuid>'` |
| 换了服务器 | 删掉 `nifetun-credentials.json` 重新审批 |
