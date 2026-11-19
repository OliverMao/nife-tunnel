# nifetun nife 插件

让 nife 直接把本机端口穿透到公网：`/tunnel up 8080` → `https://nife-tunnel.yoobit.cn/t/xxxx/`。

这是个标准 nife package（仓库根目录的 `package.json` 里有 `pi` manifest）。

## 安装

```bash
nife install git:github.com/OliverMao/nife-tunnel@v0.1.0
```

就这一条。安装时 `postinstall` 会**只下载你当前平台**的客户端（win32-x64 或 linux-x64，约 7 MB），
放到 `nife-extension/bin/<平台>/`，装完直接用，不用另外准备什么。

- 换服务器/换仓库：设 `NIFETUN_DOWNLOAD_BASE`（镜像目录）或 `NIFETUN_REPO`
- 已经有客户端：设 `NIFETUN_CLIENT=/path/to/nifetun-client`，安装时跳过下载
- 下载没成功也不影响安装：**第一次 `/tunnel up` 时会再自动下一次**，仍不行才报错提示手动放

```bash
nife -e git:github.com/OliverMao/nife-tunnel   # 只试用一次，不写 settings
nife remove git:github.com/OliverMao/nife-tunnel
nife list
```

装完重开 nife（或 `/reload`）即生效。

## 用法

```bash
nife
> /tunnel up 8080              # 把 127.0.0.1:8080 暴露出去
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

## 包结构

```
package.json                     name / keywords:["pi-package"] / pi.extensions / postinstall
scripts/install.js               安装时按平台下载客户端（只下自己那份）
scripts/pack-release.sh          把 client/ 的二进制整理成 Release 附件
nife-extension/index.ts          扩展本体（3 个工具 + /tunnel 命令，缺二进制时会自动补下）
```

- 二进制**不进 git**（`nife-extension/bin/` 已 gitignore），走 GitHub Releases：
  附件名 `nifetun-client-<platform>-<arch>[.exe]`
- `nife-coding-agent` / `typebox` 是 `peerDependencies` 且标 `optional`：
  由 nife 自己提供，不让 npm 去装（私有 registry 上装不到也不会让安装失败）

### 发版

```bash
./scripts/pack-release.sh                 # 生成 release/ 下三个附件
gh release create v0.1.0 release/* --title v0.1.0 --notes "nifetun 客户端"
```

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
| `找不到 nifetun-client` | 确认 `bin/<平台>-<架构>/` 下有二进制，或设 `NIFETUN_CLIENT` |
| `spawn UNKNOWN` / 立即退出 | 二进制不是本平台的（比如把 Linux ELF 命名成 .exe），换对的 |
| 一直"等待审批" | 看邮箱；或管理员 `curl -X POST -H "Authorization: Bearer <token>" 'http://127.0.0.1:9444/api/approve?client=<uuid>'` |
| 换了服务器 | 删掉 `nifetun-credentials.json` 重新审批 |
