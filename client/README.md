# nifetun 客户端（独立版）

> **用 nife 的话不需要这个** —— `nife install git:github.com/OliverMao/nife-tunnel` 自带 Node 客户端。
> 这个版本给"完全不想装 Node"的机器用，协议一样。

把内网服务挂到 `https://nife-tunnel.yoobit.cn/t/<id>/`。单文件二进制，不用装 Python、不用装服务。

```
nifetun-client.exe   Windows x64
nifetun-client       Linux  x64
tunnelc.json         配置
```

## 跑起来

```bash
# Windows
nifetun-client.exe -c tunnelc.json

# Linux
chmod +x nifetun-client && ./nifetun-client -c tunnelc.json
```

也可以不写文件，直接把配置从命令行丢进去：

```bash
nifetun-client --json '{"server":"https://nife-tunnel.yoobit.cn","tunnels":["127.0.0.1:8080"]}'
echo '{...}' | nifetun-client --json -      # 或者从 stdin 读
```

要后台就自己套一层：Linux `nohup ... > logs.txt 2>&1 &`，Windows 用任务计划程序（计算机启动时）。

## 配置

```json
{
  "server": "https://nife-tunnel.yoobit.cn",
  "tunnels": ["127.0.0.1:8080"]
}
```

就这两个字段。

- `server`：服务器地址，写域名（走 CDN，不暴露源站 IP）
- `tunnels`：要暴露的内网服务，`["ip:port", ...]`，想加就加一行，**5 秒生效**，不用重启

第一次启动会发审批邮件，点「批准接入」就上线。之后 token 存在 `nifetun-credentials.json`，
隧道 ID 存在 `nifetun-ids.json`，**重启后 URL 不变**。这两个文件别手改，删掉就是重新走审批。

日志里会直接打出地址：

```
入口: https://nife-tunnel.yoobit.cn/t/01cc7676ca46/  ->  127.0.0.1:8080
```

## 出问题看这里

| 现象 | 处理 |
|---|---|
| 一直 `等待管理员批准` | 去邮箱点批准按钮（10 分钟内有效），或让管理员 `POST /api/approve?client=<主机名>` |
| `服务器证书指纹与首次记录不一致` | 服务器换过证书。确认真换过，删掉 `nifetun-credentials.json` 里的 `pin` |
| `edge 拒绝: bad proof` | token 不对，删掉 `nifetun-credentials.json` 重新审批 |

可选参数（一般不用）：`connections`（连接数，默认 4）、`window`（窗口字节）、`client`（名字，默认主机名）。
其余见 [../docs/DESIGN.md](../docs/DESIGN.md)。
