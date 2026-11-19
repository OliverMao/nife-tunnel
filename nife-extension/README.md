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
> /tunnel up 8080              # https://<域名>/t/<服务端分配的ID>/
> /tunnel up 8080 5000         # 多个端口
> /tunnel status
> /tunnel down
```


直接跟 nife 说也行（模型会调工具）：

> 帮我把 3000 端口挂到公网

可用工具：

| 工具 | 说明 |
|---|---|
| `tunnel_up` | `{tunnels: ["8080" 或 "127.0.0.1:8080"], server?}` → 返回公网 URL |
| `tunnel_status` | 列出在跑的实例、PID、URL |
| `tunnel_down` | 停掉（不传 name 全停） |

## 给其它插件用（事件总线）

其它 nife 插件不用去猜 `state.json`，直接走事件总线（nife-webui 就是这样把自己的端口挂上公网的）：

```ts
// 请求
pi.events.emit("nifetun:request", { id, kind: "up", tunnels: ["127.0.0.1:5123"], server? })
pi.events.emit("nifetun:request", { id, kind: "status" })
pi.events.emit("nifetun:request", { id, kind: "down", name? })

// 回复（同一条总线上，按 id 配对）
pi.events.on("nifetun:reply", (reply) => {
  // { id, ok, urls, entries: [{ url, local }], pending, tunnels, text, stopped, error }
})
```

- `id` 由调用方生成，回复原样带回；没装本插件就没人回，自己设超时。
- `up` 是**追加**：已经在跑的实例只把新端口写进它的配置文件（客户端热加载、在原连接上重新注册），
  **不重启进程** —— 重启会把同一客户端上正在用的隧道全断一次（webui 发布一个服务端口时，页面那条
  隧道就是这么被掐断的）。`down` 同理：改配置摘掉端口，最后一个端口摘完才关进程。
- 配置文件在 `~/.nife/agent/nifetun/<name>.json`，`reload_interval: 1`。
- 生命周期：`session_shutdown` 的 `reason === "quit"`（以及进程 `exit`）会关掉所有隧道；`/reload`、
  `/new`、`/resume`、`/fork` 不动它们（进程还在、页面还开着）。下次 `session_start`（`reason ===
  "startup"`）会清掉遗留的客户端——记录里存了拉起它的 nife `owner` pid，owner 还活着的（另一个
  正在跑的 nife）不碰。
- `entries` 把 URL 和本地地址对上（`-> 127.0.0.1:8080`），调用方靠 `local` 认出自己那条。
- `pending: true` 表示在等邮件审批：客户端一连上就写了这句话，`up` 看到就直接返回，不等满 15s；
  之后用 `status` 轮询即可，批准后 `entries` 里就会出现 URL。

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
