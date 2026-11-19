/**
 * nifetun —— 让 nife 直接把本机端口穿透到公网。
 *
 * 安装：把本目录拷到 ~/.nife/agent/extensions/nifetun/
 *      （或者 nife install 一个包含它的包）
 * 依赖：同目录 bin/ 下放 nifetun-client(.exe)，或者设环境变量 NIFETUN_CLIENT 指向二进制
 *
 * 提供：
 *   工具 tunnel_up / tunnel_status / tunnel_down
 *   命令 /tunnel up 8080 5000 | status | down
 */

import { spawn, spawnSync } from "node:child_process";
import {
	chmodSync,
	closeSync,
	existsSync,
	mkdirSync,
	openSync,
	readFileSync,
	readdirSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "nife-coding-agent";
import { Type } from "typebox";

const DEFAULT_SERVER = "https://nife-tunnel.yoobit.cn";

type TunnelRec = {
	pid: number;
	server: string;
	tunnels: string[];
	log: string;
	startedAt: number;
};

type State = { tunnels: Record<string, TunnelRec> };

function here(): string {
	// jiti 下 import.meta.url 可用；拿不到就退回 cwd
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return process.cwd();
	}
}

function stateDir(): string {
	const dir = join(homedir(), ".nife", "agent", "nifetun");
	mkdirSync(dir, { recursive: true });
	return dir;
}

function statePath(): string {
	return join(stateDir(), "state.json");
}

function loadState(): State {
	try {
		const raw = JSON.parse(readFileSync(statePath(), "utf8"));
		if (raw && typeof raw === "object" && raw.tunnels) return raw as State;
	} catch {
		// 没有就空状态
	}
	return { tunnels: {} };
}

function saveState(state: State): void {
	writeFileSync(statePath(), JSON.stringify(state, null, 2));
}

function alive(pid: number): boolean {
	if (!Number.isInteger(pid) || pid <= 0) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

const PLAT = `${process.platform}-${process.arch}`;
const IS_WIN = process.platform === "win32";

function pkgMeta(): { version: string; repo: string } {
	const file = join(here(), "..", "package.json");
	try {
		const pkg = JSON.parse(readFileSync(file, "utf8"));
		const r = typeof pkg.repository === "string" ? pkg.repository : pkg.repository?.url || "";
		return {
			version: pkg.version || "0.1.0",
			repo: String(r).replace(/^github:/, "").replace(/\.git$/, "") || "OliverMao/nife-tunnel",
		};
	} catch {
		return { version: "0.1.0", repo: "OliverMao/nife-tunnel" };
	}
}

function magicOk(buf: Uint8Array): boolean {
	if (IS_WIN) return buf[0] === 0x4d && buf[1] === 0x5a;
	return buf[0] === 0x7f && buf[1] === 0x45 && buf[2] === 0x4c && buf[3] === 0x46;
}

/** 包里没带二进制时，按当前平台下一个（只下自己那份）。 */
export async function downloadBinary(): Promise<string> {
	const name = IS_WIN ? "nifetun-client.exe" : "nifetun-client";
	const destDir = join(here(), "bin", PLAT);
	const dest = join(destDir, name);
	const { version, repo } = pkgMeta();
	const asset = `nifetun-client-${PLAT}${IS_WIN ? ".exe" : ""}`;
	const base = process.env.NIFETUN_DOWNLOAD_BASE?.replace(/\/$/, "");
	const urls = [
		base ? `${base}/${asset}` : "",
		`https://github.com/${repo}/releases/download/v${version}/${asset}`,
		`https://github.com/${repo}/releases/latest/download/${asset}`,
	].filter(Boolean);

	const errors: string[] = [];
	for (const url of urls) {
		try {
			const res = await fetch(url, { redirect: "follow" });
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const buf = new Uint8Array(await res.arrayBuffer());
			if (buf.length < 1024 * 1024) throw new Error(`只有 ${buf.length}B`);
			if (!magicOk(buf)) throw new Error("文件头不对（平台不符？）");
			mkdirSync(destDir, { recursive: true });
			writeFileSync(dest, buf);
			if (!IS_WIN) {
				try {
					chmodSync(dest, 0o755);
				} catch {
					// 非 POSIX 平台忽略
				}
			}
			return dest;
		} catch (e) {
			errors.push(`${url} -> ${(e as Error).message}`);
		}
	}
	throw new Error(
		`找不到 ${PLAT} 的 nifetun-client，自动下载也失败了。\n` +
			errors.map((x) => `  ${x}`).join("\n") +
			`\n  手动下载放到 ${dest}，或设 NIFETUN_CLIENT 指向已有二进制`,
	);
}

async function ensureBinary(): Promise<string> {
	try {
		return findBinary();
	} catch {
		return downloadBinary();
	}
}

function findBinary(): string {
	const env = process.env.NIFETUN_CLIENT;
	if (env && existsSync(env)) return env;
	const dir = here();
	const names = process.platform === "win32" ? ["nifetun-client.exe"] : ["nifetun-client"];
	// 二进制按平台分目录放（bin/win32-x64 / bin/linux-x64），避免和同目录的另一种产物混淆
	const plat = `${process.platform}-${process.arch}`;
	for (const n of names) {
		for (const p of [join(dir, "bin", plat, n), join(dir, "bin", n), join(dir, n)]) {
			if (existsSync(p)) return p;
		}
	}
	// 最后看 PATH
	for (const n of names) {
		const which = spawnSync(process.platform === "win32" ? "where" : "which", [n], { encoding: "utf8" });
		if (which.status === 0 && which.stdout.trim()) return which.stdout.trim().split(/\r?\n/)[0];
	}
	throw new Error(
		`找不到 nifetun-client。把二进制放到 ${join(dir, "bin")}，或设置环境变量 NIFETUN_CLIENT`,
	);
}

function tail(path: string, lines = 12): string {
	try {
		const text = readFileSync(path, "utf8").trimEnd().split(/\r?\n/);
		return text.slice(-lines).join("\n");
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
		// 日志还没写
	}
	return [...new Set(out)];
}

function killTree(pid: number): void {
	if (!alive(pid)) return;
	if (process.platform === "win32") {
		spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
	} else {
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
}

function startTunnel(bin: string, server: string, tunnels: string[]): { name: string; rec: TunnelRec } {
	const dir = stateDir();
	const name = tunnels
		.map((t) => t.replace(/[^a-zA-Z0-9]+/g, "-"))
		.join("_")
		.slice(0, 40) || "default";
	const logPath = join(dir, `${name}.log`);
	const fd = openSync(logPath, "w");

	const cfg = { server, tunnels, reload_interval: 0 };
	const child = spawn(bin, ["--json", JSON.stringify(cfg)], {
		cwd: dir, // 凭证/隧道 ID 落在 ~/.nife/agent/nifetun/，重启后 URL 不变
		detached: true,
		stdio: ["ignore", fd, fd],
		windowsHide: true,
	});
	closeSync(fd);
	let spawnError = "";
	child.on("error", (err) => {
		spawnError = String((err as Error)?.message || err);
		writeFileSync(logPath, `启动失败: ${spawnError}
二进制: ${bin}
`, { flag: "a" });
	});
	child.unref();

	const rec: TunnelRec = {
		pid: child.pid ?? 0,
		server,
		tunnels,
		log: logPath,
		startedAt: Date.now(),
	};
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

export default function (pi: ExtensionAPI) {
	function statusAll(): string {
		const state = loadState();
		const names = Object.keys(state.tunnels);
		if (names.length === 0) return "还没有在跑的隧道（用 tunnel_up 或 /tunnel up 8080 起一个）";
		return names.map((n) => fmtRec(n, state.tunnels[n], parseUrls(state.tunnels[n].log))).join("\n");
	}

	pi.registerTool({
		name: "tunnel_up",
		label: "隧道上线",
		description:
			"把本机端口通过 nifetun 暴露成公网 https 地址。传本地端口/地址列表，返回分配到的公网 URL。" +
			"首次使用会触发邮件审批，审批通过后自动上线（凭证会落盘，之后不再审批）。",
		parameters: Type.Object({
			tunnels: Type.Array(
				Type.String({ description: "本地地址，如 127.0.0.1:8080 或 8080" }),
				{ description: "要暴露的本地服务" },
			),
			server: Type.Optional(Type.String({ description: `edge 地址，默认 ${DEFAULT_SERVER}` })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const server = params.server || DEFAULT_SERVER;
			const tunnels = (params.tunnels || []).map((t) =>
				/^\d+$/.test(t.trim()) ? `127.0.0.1:${t.trim()}` : t.trim(),
			);
			if (tunnels.length === 0) throw new Error("至少要给一个本地端口");

			const state = loadState();
			for (const [name, rec] of Object.entries(state.tunnels)) {
				if (alive(rec.pid)) {
					killTree(rec.pid); // 同一组端口只保留一个实例
					delete state.tunnels[name];
				}
			}

			const bin = await ensureBinary();
			const { name, rec } = startTunnel(bin, server, tunnels);
			state.tunnels[name] = rec;
			saveState(state);

			let urls: string[] = [];
			for (let i = 0; i < 30; i++) {
				await sleep(500);
				urls = parseUrls(rec.log);
				if (urls.length) break;
				if (!alive(rec.pid)) break;
			}

			const log = tail(rec.log, 20);
			if (urls.length) {
				ctx.ui.notify(`隧道已上线: ${urls.join(" ")}`, "success");
				return {
					content: [
						{
							type: "text",
							text: `${fmtRec(name, rec, urls)}\n\n日志:\n${log}`,
						},
					],
					details: { name, urls, ...rec },
				};
			}
			const pending = /等待管理员批准|等待管理员在 edge 上执行/.test(log);
			ctx.ui.notify(pending ? "隧道等待审批" : "隧道没起来，看日志", pending ? "info" : "warning");
			return {
				content: [
					{
						type: "text",
						text: pending
							? `${fmtRec(name, rec, [])}\n\n还没批准：审批邮件已发到管理员邮箱，点「批准接入」后会自动上线（不用重跑）。\n\n日志:\n${log}`
							: `${fmtRec(name, rec, [])}\n\n没拿到 URL，日志:\n${log}`,
					},
				],
				details: { name, urls, ...rec },
			};
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
		parameters: Type.Object({
			name: Type.Optional(Type.String({ description: "tunnel_up 返回的实例名" })),
		}),
		async execute(_id, params, _s, _u, ctx) {
			const state = loadState();
			const names = params.name ? [params.name] : Object.keys(state.tunnels);
			const done: string[] = [];
			for (const n of names) {
				const rec = state.tunnels[n];
				if (!rec) continue;
				killTree(rec.pid);
				delete state.tunnels[n];
				done.push(n);
			}
			saveState(state);
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
			if (sub === "status") {
				ctx.ui.notify(statusAll(), "info");
				return;
			}
			if (sub === "down") {
				const state = loadState();
				const n = parts[1];
				const names = n ? [n] : Object.keys(state.tunnels);
				for (const name of names) {
					const rec = state.tunnels[name];
					if (rec) {
						killTree(rec.pid);
						delete state.tunnels[name];
					}
				}
				saveState(state);
				ctx.ui.notify(names.length ? `已停止 ${names.join(", ")}` : "没有在跑的隧道", "info");
				return;
			}
			if (sub === "up" || sub === "start") {
				const ports = parts.slice(1);
				if (ports.length === 0) {
					ctx.ui.notify("用法: /tunnel up 8080 [5000 ...]", "warning");
					return;
				}
				ctx.ui.notify(`正在启动隧道: ${ports.join(", ")}…`, "info");
				const server = process.env.NIFETUN_SERVER || DEFAULT_SERVER;
				const tunnels = ports.map((p) => (/^\d+$/.test(p) ? `127.0.0.1:${p}` : p));
				const state = loadState();
				for (const [name, rec] of Object.entries(state.tunnels)) {
					if (alive(rec.pid)) {
						killTree(rec.pid);
						delete state.tunnels[name];
					}
				}
				const bin = await ensureBinary();
				const { name, rec } = startTunnel(bin, server, tunnels);
				state.tunnels[name] = rec;
				saveState(state);
				let urls: string[] = [];
				for (let i = 0; i < 30; i++) {
					await sleep(500);
					urls = parseUrls(rec.log);
					if (urls.length || !alive(rec.pid)) break;
				}
				if (urls.length) {
					ctx.ui.notify(`已上线:\n${urls.join("\n")}`, "success");
				} else {
					ctx.ui.notify(`还没拿到 URL（可能在等审批）。日志:\n${tail(rec.log, 6)}`, "info");
				}
				return;
			}
			ctx.ui.notify("用法: /tunnel up 8080 5000 | /tunnel status | /tunnel down", "warning");
		},
	});

	// 打开 nife 时把现状放进状态栏
	pi.on("session_start", async (_event, ctx) => {
		const state = loadState();
		const live = Object.entries(state.tunnels).filter(([, r]) => alive(r.pid));
		if (live.length === 0) return;
		const urls = live.flatMap(([, r]) => parseUrls(r.log));
		ctx.ui.setStatus("nifetun", `隧道 ${live.length} 条${urls.length ? ` · ${urls[0]}` : ""}`);
	});
}

/** 清掉已经退出的实例记录（status/up 时顺手调用） */
export function pruneDead(): number {
	const state = loadState();
	let n = 0;
	for (const [name, rec] of Object.entries(state.tunnels)) {
		if (!alive(rec.pid)) {
			delete state.tunnels[name];
			n++;
		}
	}
	if (n) saveState(state);
	return n;
}

/** 供外部调试用 */
export function listLogs(): string[] {
	try {
		return readdirSync(stateDir())
			.filter((f) => f.endsWith(".log"))
			.map((f) => join(stateDir(), f))
			.filter((p) => statSync(p).isFile());
	} catch {
		return [];
	}
}
