/**
 * nifetun —— 让 nife 直接把本机端口穿透到公网。
 *
 * 全部是 Node：客户端脚本就在本包 client/tunnelc.mjs，由插件用当前 node 拉起，
 * 不打包、不下载、不弹控制台窗口。
 *
 * 安装：nife install git:github.com/OliverMao/nife-tunnel@<tag>
 * 用法：/tunnel up 8080 [5000 ...] | /tunnel status | /tunnel down
 * 工具：tunnel_up / tunnel_status / tunnel_down
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "nife-coding-agent";
import { Type } from "typebox";

const DEFAULT_SERVER = "https://nife-tunnel.yoobit.cn";

type TunnelRec = { pid: number; server: string; tunnels: string[]; log: string; startedAt: number };
type State = { tunnels: Record<string, TunnelRec> };

function here(): string {
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return process.cwd();
	}
}

/** 客户端脚本位置：包内 client/tunnelc.mjs，可用 NIFETUN_CLIENT 覆盖 */
function clientScript(): string {
	const override = process.env.NIFETUN_CLIENT;
	if (override && existsSync(override)) return override;
	const p = join(here(), "client", "tunnelc.mjs");
	if (!existsSync(p)) {
		throw new Error(`找不到客户端脚本 ${p}（可设 NIFETUN_CLIENT 指定）`);
	}
	return p;
}

function stateDir(): string {
	const dir = join(homedir(), ".nife", "agent", "nifetun");
	mkdirSync(dir, { recursive: true });
	return dir;
}

const statePath = () => join(stateDir(), "state.json");

function loadState(): State {
	try {
		const raw = JSON.parse(readFileSync(statePath(), "utf8"));
		if (raw && typeof raw === "object" && raw.tunnels) return raw as State;
	} catch {
		// 空状态
	}
	return { tunnels: {} };
}

const saveState = (s: State) => writeFileSync(statePath(), JSON.stringify(s, null, 2));

function alive(pid: number): boolean {
	if (!Number.isInteger(pid) || pid <= 0) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

function tail(path: string, lines = 20): string {
	try {
		return readFileSync(path, "utf8").trimEnd().split(/\r?\n/).slice(-lines).join("\n");
	} catch {
		return "";
	}
}

/** 从客户端日志里扒出 edge 分配的入口 URL */
function parseUrls(logPath: string): string[] {
	const out: string[] = [];
	try {
		for (const line of readFileSync(logPath, "utf8").split(/\r?\n/)) {
			const m = line.match(/入口:\s*(\S+)/);
			if (m) out.push(m[1]);
		}
	} catch {
		// 日志还没生成
	}
	return [...new Set(out)];
}

function killTree(pid: number): void {
	if (!alive(pid)) return;
	if (process.platform === "win32") {
		spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
		return;
	}
	try {
		process.kill(-pid, "SIGTERM");
	} catch {
		try {
			process.kill(pid, "SIGTERM");
		} catch {
			// 已经没了
		}
	}
}

function startTunnel(server: string, tunnels: string[]): { name: string; rec: TunnelRec } {
	const script = clientScript();
	const dir = stateDir();
	const name =
		tunnels
			.map((t) => t.replace(/[^a-zA-Z0-9]+/g, "-"))
			.join("_")
			.slice(0, 40) || "default";
	const logPath = join(dir, `${name}.log`);

	const cfg = { server, tunnels, reload_interval: 0 };
	// 用当前 node 跑脚本，stdio 全部不接（没有控制台窗口），日志由脚本自己写文件
	const child = spawn(process.execPath, [script, "--json", JSON.stringify(cfg), "--log-file", logPath], {
		cwd: dir, // 凭证/隧道 ID 落在 ~/.nife/agent/nifetun/，重启后 URL 不变
		detached: true,
		stdio: "ignore",
		windowsHide: true,
	});
	child.on("error", (err) => {
		try {
			writeFileSync(logPath, `启动失败: ${(err as Error).message}\n脚本: ${script}\n`, { flag: "a" });
		} catch {
			// 忽略
		}
	});
	child.unref();

	const rec: TunnelRec = { pid: child.pid ?? 0, server, tunnels, log: logPath, startedAt: Date.now() };
	return { name, rec };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function fmtRec(name: string, rec: TunnelRec, urls: string[]): string {
	const up = alive(rec.pid) ? "运行中" : "已退出";
	const secs = Math.round((Date.now() - rec.startedAt) / 1000);
	const lines = [
		`[${name}] ${up} pid=${rec.pid} 已运行 ${secs}s  server=${rec.server}`,
		`  本地: ${rec.tunnels.join(", ")}`,
	];
	if (urls.length) lines.push(`  公网: ${urls.join("  ")}`);
	return lines.join("\n");
}

/** 起一组隧道并等到拿到 URL（或判定在等审批/失败） */
async function up(server: string, tunnels: string[]) {
	const state = loadState();
	for (const [name, rec] of Object.entries(state.tunnels)) {
		if (alive(rec.pid)) {
			killTree(rec.pid); // 同一批端口只保留一个实例
			delete state.tunnels[name];
		}
	}
	const { name, rec } = startTunnel(server, tunnels);
	state.tunnels[name] = rec;
	saveState(state);

	let urls: string[] = [];
	for (let i = 0; i < 30; i++) {
		await sleep(500);
		urls = parseUrls(rec.log);
		if (urls.length || !alive(rec.pid)) break;
	}
	const log = tail(rec.log);
	const pending = /等待审批|等待管理员批准/.test(log);
	return { name, rec, urls, log, pending };
}

export default function (pi: ExtensionAPI) {
	function statusAll(): string {
		const state = loadState();
		const names = Object.keys(state.tunnels);
		if (!names.length) return "还没有在跑的隧道（用 tunnel_up 或 /tunnel up 8080 起一个）";
		return names.map((n) => fmtRec(n, state.tunnels[n], parseUrls(state.tunnels[n].log))).join("\n");
	}

	function downAll(only?: string): string[] {
		const state = loadState();
		const names = only ? [only] : Object.keys(state.tunnels);
		const done: string[] = [];
		for (const n of names) {
			const rec = state.tunnels[n];
			if (!rec) continue;
			killTree(rec.pid);
			delete state.tunnels[n];
			done.push(n);
		}
		saveState(state);
		return done;
	}

	pi.registerTool({
		name: "tunnel_up",
		label: "隧道上线",
		description:
			"把本机端口通过 nifetun 暴露成公网 https 地址，返回分配到的 URL。" +
			"首次在新机器上会触发邮件审批，批准后自动上线（凭证落盘，之后不再审批）。",
		parameters: Type.Object({
			tunnels: Type.Array(Type.String({ description: "本地地址，如 127.0.0.1:8080 或 8080" })),
			server: Type.Optional(Type.String({ description: `edge 地址，默认 ${DEFAULT_SERVER}` })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const server = params.server || process.env.NIFETUN_SERVER || DEFAULT_SERVER;
			const tunnels = (params.tunnels || []).map((t) =>
				/^\d+$/.test(t.trim()) ? `127.0.0.1:${t.trim()}` : t.trim(),
			);
			if (!tunnels.length) throw new Error("至少要给一个本地端口");

			const { name, rec, urls, log, pending } = await up(server, tunnels);
			if (urls.length) ctx.ui.notify(`隧道已上线: ${urls.join(" ")}`, "success");
			else ctx.ui.notify(pending ? "隧道等待审批" : "隧道没起来，看日志", pending ? "info" : "warning");

			const body = urls.length
				? `${fmtRec(name, rec, urls)}\n\n日志:\n${log}`
				: pending
					? `${fmtRec(name, rec, [])}\n\n还在等待审批：审批邮件已发出，点「批准接入」后会自动上线（不用重跑）。`
					: `${fmtRec(name, rec, [])}\n\n没拿到 URL，日志:\n${log}`;
			return { content: [{ type: "text", text: body }], details: { name, urls, pending, ...rec } };
		},
	});

	pi.registerTool({
		name: "tunnel_status",
		label: "隧道状态",
		description: "列出当前通过 nifetun 暴露的端口、PID 和公网 URL。",
		parameters: Type.Object({}),
		async execute() {
			return { content: [{ type: "text", text: statusAll() }], details: {} };
		},
	});

	pi.registerTool({
		name: "tunnel_down",
		label: "隧道下线",
		description: "停掉 nifetun 隧道。不传 name 就全停。",
		parameters: Type.Object({ name: Type.Optional(Type.String({ description: "tunnel_up 返回的实例名" })) }),
		async execute(_id, params, _s, _u, ctx) {
			const done = downAll(params.name);
			ctx.ui.notify(done.length ? `已停止 ${done.join(", ")}` : "没有在跑的隧道", "info");
			return {
				content: [{ type: "text", text: done.length ? `已停止: ${done.join(", ")}` : "没有在跑的隧道" }],
				details: { stopped: done },
			};
		},
	});

	pi.registerCommand("tunnel", {
		description: "内网穿透：/tunnel up 8080 5000 | status | down",
		handler: async (args, ctx) => {
			const parts = (args || "").trim().split(/\s+/).filter(Boolean);
			const sub = parts[0] || "status";
			if (sub === "status") return ctx.ui.notify(statusAll(), "info");
			if (sub === "down") {
				const done = downAll(parts[1]);
				return ctx.ui.notify(done.length ? `已停止 ${done.join(", ")}` : "没有在跑的隧道", "info");
			}
			if (sub === "up" || sub === "start") {
				const ports = parts.slice(1);
				if (!ports.length) return ctx.ui.notify("用法: /tunnel up 8080 [5000 ...]", "warning");
				const tunnels = ports.map((p) => (/^\d+$/.test(p) ? `127.0.0.1:${p}` : p));
				ctx.ui.notify(`正在启动隧道: ${tunnels.join(", ")}…`, "info");
				const { urls, log, pending } = await up(process.env.NIFETUN_SERVER || DEFAULT_SERVER, tunnels);
				if (urls.length) return ctx.ui.notify(`已上线:\n${urls.join("\n")}`, "success");
				return ctx.ui.notify(
					pending ? "等待审批：点邮件里的「批准接入」后会自动上线" : `没拿到 URL。日志:\n${tail(log ? "" : "", 6) || log}`,
					pending ? "info" : "warning",
				);
			}
			ctx.ui.notify("用法: /tunnel up 8080 5000 | /tunnel status | /tunnel down", "warning");
		},
	});

	// 打开 nife 时把现状放进状态栏
	pi.on("session_start", async (_event, ctx) => {
		const state = loadState();
		const live = Object.entries(state.tunnels).filter(([, r]) => alive(r.pid));
		if (!live.length) return;
		const urls = live.flatMap(([, r]) => parseUrls(r.log));
		ctx.ui.setStatus("nifetun", `隧道 ${live.length} 条${urls.length ? ` · ${urls[0]}` : ""}`);
	});
}
