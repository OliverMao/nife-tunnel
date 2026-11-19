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

/** 事件总线的频道名：别的插件按这个约定调用本插件 */
const REQUEST = "nifetun:request";
const REPLY = "nifetun:reply";

/** 最近一次 session_start 的上下文，只用它刷状态栏（事件总线没有 ctx）。 */
let statusCtx: { ui: { setStatus: (key: string, text: string | undefined) => void } } | undefined;

type TunnelRec = {
	pid: number;
	server: string;
	tunnels: string[];
	log: string;
	/** 客户端配置文件的路径：改它就能加/删隧道，不用重启进程 */
	config?: string;
	/** 拉起这个客户端的 nife 进程：它还活着就说明这是另一个会话的隧道，不能当遗留清掉 */
	owner?: number;
	startedAt: number;
};
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
	return [...new Set(parseEntries(logPath).map((entry) => entry.url))];
}

/**
 * 入口 URL 和它对应的本地地址。日志行是 `入口: https://host/t/<id>/  ->  127.0.0.1:8080`，
 * 别的插件（nife-webui）要靠 `local` 认出“哪个 URL 是我的端口”。
 */
type TunnelEntry = { url: string; local: string };

function parseEntries(logPath: string): TunnelEntry[] {
	const out: TunnelEntry[] = [];
	try {
		for (const line of readFileSync(logPath, "utf8").split(/\r?\n/)) {
			const m = line.match(/入口:\s*(\S+)\s*->\s*([^\s（]+)/);
			if (m) out.push({ url: m[1], local: m[2] });
		}
	} catch {
		// 日志还没生成
	}
	return out;
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

/**
 * 写客户端的配置文件。客户端每 reload_interval 秒看一次 mtime，变了就在**原连接上**重新注册，
 * 所以加/删端口不用重启进程 —— 重启会掐断同一客户端上正在用的隧道（比如 webui 的页面）。
 */
function writeConfig(path: string, server: string, tunnels: string[]): void {
	writeFileSync(path, JSON.stringify({ server, tunnels, reload_interval: 1 }, null, 2));
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
	const configPath = join(dir, `${name}.json`);

	// 这次运行的日志从空开始：旧内容里的 URL / “等待审批”属于上一个进程，会让 parseUrls 和
	// 下面的 pending 判断读到过期状态。
	writeFileSync(logPath, "");
	writeConfig(configPath, server, tunnels);

	// 用当前 node 跑脚本，stdio 全部不接（没有控制台窗口），日志由脚本自己写文件
	const child = spawn(process.execPath, [script, "-c", configPath, "--log-file", logPath], {
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

	const rec: TunnelRec = {
		pid: child.pid ?? 0,
		server,
		tunnels,
		log: logPath,
		config: configPath,
		owner: process.pid,
		startedAt: Date.now(),
	};
	return { name, rec };
}

/** 在跑着的客户端上追加端口（热加载，不断线） */
function growTunnel(rec: TunnelRec, tunnels: string[]): void {
	const merged = [...new Set([...rec.tunnels, ...tunnels])];
	if (merged.length === rec.tunnels.length) return;
	if (rec.config) writeConfig(rec.config, rec.server, merged);
	rec.tunnels = merged;
}

/** 从在跑的客户端上摘掉端口；剩下的交给调用方决定是否关进程 */
function shrinkTunnel(rec: TunnelRec, tunnels: string[]): string[] {
	const keep = rec.tunnels.filter((tunnel) => !tunnels.includes(tunnel));
	if (keep.length === rec.tunnels.length) return keep;
	if (keep.length && rec.config) writeConfig(rec.config, rec.server, keep);
	rec.tunnels = keep;
	return keep;
}

/** 要的每一个本地地址都拿到 URL 了吗 */
function covered(entries: TunnelEntry[], wanted: string[]): boolean {
	return wanted.every((local) => entries.some((entry) => entry.local === local));
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
	const live = Object.entries(state.tunnels).filter(([, rec]) => alive(rec.pid));
	let name: string;
	let rec: TunnelRec;
	let spawned = false;
	if (live.length === 1 && live[0][1].config) {
		// 已经在跑：追加端口而不是重启。webui 发布一个服务端口时，页面那条隧道必须一根毫毛都不动。
		[name, rec] = live[0];
		growTunnel(rec, tunnels);
		state.tunnels[name] = rec;
		saveState(state);
	} else {
		for (const [old, record] of Object.entries(state.tunnels)) {
			if (alive(record.pid)) killTree(record.pid);
			delete state.tunnels[old];
		}
		const started = startTunnel(server, tunnels);
		name = started.name;
		rec = started.rec;
		spawned = true;
		state.tunnels[name] = rec;
		saveState(state);
	}

	let urls: string[] = [];
	let log = "";
	let pending = false;
	for (let i = 0; i < 30; i++) {
		await sleep(500);
		urls = parseUrls(rec.log);
		if (covered(parseEntries(rec.log), rec.tunnels) || !alive(rec.pid)) break;
		// 审批中不会有 URL，等下去也没用：客户端连上就写了这句话，早点把状态交回去
		log = tail(rec.log);
		if (spawned && /等待审批|等待管理员批准/.test(log)) {
			pending = true;
			break;
		}
	}
	if (!log) log = tail(rec.log);
	if (!pending && spawned) pending = /等待审批|等待管理员批准/.test(log);
	return { name, rec, urls, log, pending };
}

export default function (pi: ExtensionAPI) {
	/** 刷新底部状态：没有活隧道就清掉（setStatus 传 undefined） */
	function refreshStatus(ctx: { ui: { setStatus: (key: string, text: string | undefined) => void } }): void {
		const state = loadState();
		const live = Object.entries(state.tunnels).filter(([, r]) => alive(r.pid));
		if (!live.length) {
			ctx.ui.setStatus("nifetun", undefined);
			return;
		}
		// 只留一段短标记：⚡ /t/ab12cd 或 ⚡ 3 条
		const urls = live.flatMap(([, r]) => parseUrls(r.log));
		const tags = urls.map((u) => {
			const m = u.match(/\/t\/([^/]+)\/?$/);
			return m ? `/t/${m[1].slice(0, 6)}` : u.replace(/^https?:\/\//, "");
		});
		const text = tags.length ? `⚡ ${tags.join(" ")}` : `⚡ ${live.length} 条`;
		ctx.ui.setStatus("nifetun", text);
	}

	/** 事件总线没有 ctx：拿最近一次 session_start 的那个刷状态栏就够了。 */
	function refreshStatusQuietly(): void {
		if (!statusCtx) return;
		try {
			refreshStatus(statusCtx);
		} catch {
			// 会话已经换了，状态栏不重要
		}
	}

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
			refreshStatus(ctx);
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
			refreshStatus(ctx);
			ctx.ui.notify(done.length ? `已停止 ${done.join(", ")}` : "没有在跑的隧道", "info");
			return {
				content: [{ type: "text", text: done.length ? `已停止: ${done.join(", ")}` : "没有在跑的隧道" }],
				details: { stopped: done },
			};
		},
	});

	pi.registerCommand("tunnel", {
		description: "内网穿透：/tunnel up 8080 | status | down",
		handler: async (args, ctx) => {
			const parts = (args || "").trim().split(/\s+/).filter(Boolean);
			const sub = parts[0] || "status";
			if (sub === "status") return ctx.ui.notify(statusAll(), "info");
			if (sub === "down") {
				const done = downAll(parts[1]);
				refreshStatus(ctx);
				return ctx.ui.notify(done.length ? `已停止 ${done.join(", ")}` : "没有在跑的隧道", "info");
			}
			if (sub === "up" || sub === "start") {
				const ports = parts.slice(1);
				if (!ports.length) return ctx.ui.notify("用法: /tunnel up 8080 [5000 ...]", "warning");
				const tunnels = ports.map((p) => (/^\d+$/.test(p) ? `127.0.0.1:${p}` : p));
				ctx.ui.notify(`正在启动隧道: ${tunnels.join(", ")}…`, "info");
				const { urls, log, pending } = await up(process.env.NIFETUN_SERVER || DEFAULT_SERVER, tunnels);
				refreshStatus(ctx);
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
	pi.on("session_start", async (event, ctx) => {
		statusCtx = ctx;
		const state = loadState();
		let changed = false;
		for (const [name, rec] of Object.entries(state.tunnels)) {
			// 上一个 nife 挂了（崩溃、被 kill -9）时客户端会活下来：这次启动一律收掉，端口别被旧
			// 客户端占着。但 owner 还活着的记录属于另一个正在跑的 nife，不能动。
			const leftover = event.reason === "startup" && !(rec.owner && alive(rec.owner));
			if (leftover && alive(rec.pid)) killTree(rec.pid);
			if (leftover || !alive(rec.pid)) {
				delete state.tunnels[name];
				changed = true;
			}
		}
		if (changed) saveState(state);
		refreshStatus(ctx);
	});

	// 真的退出才收：/reload、/new、/resume、/fork 只是换会话，进程还在，隧道（和开着它的页面）要留着
	pi.on("session_shutdown", (event) => {
		if (event.reason !== "quit") return;
		downAll();
	});
	// Ctrl+C / Ctrl+D 走的是进程退出，不是事件：这里再兜一次（downAll 是同步的，重复调用没关系）
	process.on("exit", () => downAll());

	// 其它插件通过事件总线用隧道（nife-webui 靠它把自己的端口挂上公网）：
	//   emit(REQUEST, { id, kind: "up" | "status" | "down", tunnels?, server?, name? })
	//   ← emit(REPLY,   { id, ok, urls?, entries?, pending?, tunnels?, text?, stopped?, error? })
	pi.events.on(REQUEST, async (raw) => {
		const req = (raw ?? {}) as {
			id?: unknown;
			kind?: unknown;
			tunnels?: unknown;
			server?: unknown;
			name?: unknown;
		};
		if (typeof req.id !== "string") return;
		const reply = (body: Record<string, unknown>) => pi.events.emit(REPLY, { id: req.id, ...body });
		try {
			if (req.kind === "status") {
				const state = loadState();
				// 每个实例都带上 alive：调用方要能分清“审批中还没 URL”和“客户端已经没了”
				const instances = Object.entries(state.tunnels).map(([name, rec]) => ({
					name,
					pid: rec.pid,
					alive: alive(rec.pid),
					tunnels: rec.tunnels,
					entries: parseEntries(rec.log),
				}));
				const entries = instances.filter((inst) => inst.alive).flatMap((inst) => inst.entries);
				return reply({ ok: true, entries, instances, text: statusAll() });
			}
			if (req.kind === "down") {
				const wanted = normalizeTunnels(req.tunnels);
				if (wanted.length) {
					// 只摘掉指定端口：调用方（webui）不该顺手把别人正在用的隧道一起关了。还有别的
					// 端口要留的，就改配置文件热加载，不重启 —— 重启会把同一客户端上的隧道全断一次。
					const state = loadState();
					const stopped: string[] = [];
					for (const [name, rec] of Object.entries(state.tunnels)) {
						const drop = rec.tunnels.filter((tunnel) => wanted.includes(tunnel));
						if (!drop.length) continue;
						const keep = shrinkTunnel(rec, drop);
						stopped.push(name);
						if (keep.length) state.tunnels[name] = rec;
						else {
							killTree(rec.pid);
							delete state.tunnels[name];
						}
					}
					saveState(state);
					refreshStatusQuietly();
					return reply({ ok: true, stopped });
				}
				const stopped = downAll(typeof req.name === "string" ? req.name : undefined);
				refreshStatusQuietly();
				return reply({ ok: true, stopped });
			}
			if (req.kind !== "up") return reply({ ok: false, error: `unknown kind: ${String(req.kind)}` });

			const wanted = normalizeTunnels(req.tunnels);
			if (!wanted.length) return reply({ ok: false, error: "no tunnels" });
			const live = Object.values(loadState().tunnels).filter((rec) => alive(rec.pid));
			const running = live.flatMap((rec) => rec.tunnels);
			// 已经盖住要的端口就别重启：重启会断开别人正在用的连接
			if (wanted.every((tunnel) => running.includes(tunnel))) {
				const entries = live.flatMap((rec) => parseEntries(rec.log));
				if (entries.length) {
					return reply({ ok: true, urls: [...new Set(entries.map((entry) => entry.url))], entries, tunnels: running });
				}
			}
			// 追加而不是替换：调用方只想加自己的端口，不该顺手把别人正在用的隧道掉线
			const tunnels = [...new Set([...running, ...wanted])];
			const server = typeof req.server === "string" && req.server ? req.server : process.env.NIFETUN_SERVER || DEFAULT_SERVER;
			const { name, rec, urls, pending } = await up(server, tunnels);
			refreshStatusQuietly();
			return reply({ ok: true, name, pid: rec.pid, tunnels, urls, entries: parseEntries(rec.log), pending });
		} catch (error) {
			return reply({ ok: false, error: error instanceof Error ? error.message : String(error) });
		}
	});
}


/** `8080` / `127.0.0.1:8080` / 混着非字符串的数组 -> 去重后的本地地址 */
function normalizeTunnels(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	const out = value
		.filter((item): item is string => typeof item === "string")
		.map((item) => item.trim())
		.filter(Boolean)
		.map((item) => (/^\d+$/.test(item) ? `127.0.0.1:${item}` : item));
	return [...new Set(out)];
}
