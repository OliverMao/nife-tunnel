#!/usr/bin/env node
/**
 * nifetun 客户端（Node 版，零第三方依赖）
 *
 * 跟 edge 之间只跑一条 WebSocket（走 CDN 的 443，源站 IP 不暴露），
 * 上面复用 9 字节帧头的多路协议：一条 WS 连接里并行跑多条隧道流。
 *
 *   node tunnelc.mjs --json '{"server":"https://nife-tunnel.yoobit.cn","tunnels":["127.0.0.1:8080"]}'
 *   node tunnelc.mjs -c tunnelc.json --log-file tunnel.log
 *
 * 由 nife 插件拉起时不带控制台窗口（stdio 全 ignore，日志写文件）。
 */

import { createHmac, createHash, randomBytes } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { connect as netConnect } from "node:net";
import { dirname, join, resolve } from "node:path";

// ---------------- 协议常量（和 nifetun/wire.py 一致） ----------------
const OPEN = 1;
const DATA = 2;
const FIN = 3;
const RST = 4;
const CTRL = 5;
const WU = 6;
const HDR = 9;
const MAX_CHUNK = 64 * 1024;
const DEFAULT_WINDOW = 256 * 1024;

const WS_PATH = "/_nifetun/ws";
const DEFAULT_SERVER = "https://nife-tunnel.yoobit.cn";

// ---------------- 日志 ----------------
let logStream = null;
let verbose = false;
function log(level, msg) {
	const line = `${new Date().toTimeString().slice(0, 8)} ${level.padEnd(7)} nifetun.client   | ${msg}\n`;
	if (logStream) {
		try {
			appendFileSync(logStream, line);
		} catch {
			// 磁盘满之类，别再炸
		}
	} else {
		(level === "ERROR" || level === "WARNING" ? process.stderr : process.stdout).write(line);
	}
}
const info = (m) => log("INFO", m);
const warn = (m) => log("WARNING", m);
const error = (m) => log("ERROR", m);
const debug = (m) => verbose && log("DEBUG", m);

// ---------------- 帧编解码 ----------------
function frame(sid, type, payload) {
	const hdr = Buffer.allocUnsafe(HDR);
	hdr.writeUInt32BE(sid, 0);
	hdr.writeUInt8(type, 4);
	hdr.writeUInt32BE(payload.length, 5);
	return Buffer.concat([hdr, payload]);
}
const ctrl = (msg) => frame(0, CTRL, Buffer.from(JSON.stringify(msg), "utf8"));

class FrameParser {
	constructor() {
		this.buf = Buffer.alloc(0);
	}
	push(chunk) {
		this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : Buffer.from(chunk);
		const out = [];
		while (this.buf.length >= HDR) {
			const sid = this.buf.readUInt32BE(0);
			const type = this.buf.readUInt8(4);
			const len = this.buf.readUInt32BE(5);
			if (this.buf.length < HDR + len) break;
			const payload = this.buf.subarray(HDR, HDR + len);
			this.buf = this.buf.subarray(HDR + len);
			out.push({ sid, type, payload });
		}
		return out;
	}
}

// ---------------- 配置 ----------------
function parseConfig(argv) {
	const args = { config: null, json: null, logFile: null, verbose: false };
	for (let i = 2; i < argv.length; i++) {
		const a = argv[i];
		if (a === "-c" || a === "--config") args.config = argv[++i];
		else if (a === "--json") args.json = argv[++i];
		else if (a === "--log-file") args.logFile = argv[++i];
		else if (a === "-v" || a === "--verbose") args.verbose = true;
		else if (a === "-h" || a === "--help") {
			console.log("用法: tunnelc.mjs -c config.json | --json '{...}' [--log-file PATH] [-v]");
			process.exit(0);
		}
	}
	let cfg = {};
	if (args.json != null) {
		const text = args.json.trim() === "-" ? readFileSync(0, "utf8") : args.json;
		cfg = JSON.parse(text);
	} else if (args.config) {
		cfg = JSON.parse(readFileSync(args.config, "utf8"));
	} else if (existsSync("tunnelc.json")) {
		cfg = JSON.parse(readFileSync("tunnelc.json", "utf8"));
	} else {
		throw new Error("没有配置：用 -c config.json 或 --json '{...}'");
	}
	for (const k of Object.keys(cfg)) if (k.startsWith("_")) delete cfg[k];
	return { cfg, args };
}

function parseTunnels(cfg) {
	const list = cfg.tunnels || [];
	if (!list.length) throw new Error("config: tunnels 不能为空");
	return list.map((t) => {
		if (typeof t === "string") t = { local: t };
		const local = String(t.local || "");
		if (!local) throw new Error(`config: tunnel 缺少 local: ${JSON.stringify(t)}`);
		return {
			local,
			id: (t.id || "").toLowerCase() || null,
			host: (t.host || "").toLowerCase() || null,
			label: t.label || "",
		};
	});
}

function serverSpec(raw) {
	const text = String(raw || DEFAULT_SERVER).trim();
	const m = text.match(/^(wss?|https?|tls|tcp):\/\/([^/]+)(\/.*)?$/i);
	if (!m) return { url: `wss://${text}${WS_PATH}`, plain: false };
	const scheme = m[1].toLowerCase();
	const host = m[2];
	const path = m[3] || WS_PATH;
	if (scheme === "ws") return { url: `ws://${host}${path}`, plain: true };
	if (scheme === "http") return { url: `ws://${host}${path}`, plain: true };
	if (scheme === "wss" || scheme === "https") return { url: `wss://${host}${path}`, plain: false };
	// tls:// / tcp:// 直连 edge 的 client_listen（不走 WS），暂不支持
	throw new Error(`server 只支持 https:// / wss:// 形式（走 CDN），收到 ${text}`);
}

// ---------------- 凭证 / 隧道 ID ----------------
class Store {
	constructor(cfg, cwd) {
		this.credFile = resolve(cwd, cfg.credentials_file || "nifetun-credentials.json");
		this.idsFile = resolve(cwd, cfg.tunnel_state_file || "nifetun-ids.json");
		this.cred = { uuid: "", label: "", token: "", pin: "" };
		this.ids = {};
		this.load();
	}
	load() {
		try {
			const c = JSON.parse(readFileSync(this.credFile, "utf8"));
			this.cred = {
				uuid: String(c.uuid || c.client || "").toLowerCase(),
				label: String(c.label || ""),
				token: String(c.token || ""),
				pin: String(c.pin || "").toLowerCase(),
			};
		} catch {
			// 没有凭证 = 首次申请
		}
		try {
			const s = JSON.parse(readFileSync(this.idsFile, "utf8"));
			for (const t of s.tunnels || []) if (t.local && t.id) this.ids[t.local] = t.id;
		} catch {
			// 还没有 ID
		}
	}
	saveCred() {
		try {
			mkdirSync(dirname(this.credFile), { recursive: true });
			writeFileSync(this.credFile, JSON.stringify(this.cred, null, 2), { mode: 0o600 });
		} catch (e) {
			error(`保存凭证失败: ${e.message}`);
		}
	}
	saveIds(label) {
		try {
			const tunnels = Object.entries(this.ids).map(([local, id]) => ({ local, id }));
			writeFileSync(
				this.idsFile,
				JSON.stringify({ uuid: this.cred.uuid, label: label || this.cred.label, tunnels }, null, 2),
			);
		} catch (e) {
			error(`保存隧道 ID 失败: ${e.message}`);
		}
	}
}

// ---------------- 单条流 ----------------
class Stream {
	constructor(sid, mux, credit, target) {
		this.sid = sid;
		this.mux = mux;
		this.credit = credit; // 对端允许我们发的字节数
		this.pending = []; // 信用不足时积压
		this.pendingLen = 0;
		this.unacked = 0; // 已收未回 WU 的字节数
		this.finRx = false;
		this.finSent = false;
		this.closed = false;
		this.sock = null;
		this.target = target;
		this.open();
	}

	open() {
		const { host, port } = this.target;
		const sock = netConnect({ host, port });
		this.sock = sock;
		sock.setNoDelay(true);
		sock.on("connect", () => debug(`流 #${this.sid} 连上 ${host}:${port}`));
		sock.on("data", (chunk) => this.mux.sendData(this, chunk));
		sock.on("end", () => this.mux.sendFin(this));
		sock.on("error", (e) => {
			debug(`流 #${this.sid} 本地出错: ${e.message}`);
			this.mux.reset(this);
		});
		sock.on("close", () => {
			this.closed = true;
			this.mux.drop(this.sid);
		});
	}

	write(chunk) {
		if (this.closed || !this.sock) return;
		if (!this.sock.write(chunk)) {
			this.sock.once("drain", () => debug(`流 #${this.sid} drain`));
		}
	}

	end() {
		if (this.closed) return;
		try {
			this.sock.end();
		} catch {
			// 已经没了
		}
	}

	destroy() {
		this.closed = true;
		try {
			this.sock.destroy();
		} catch {
			// 忽略
		}
	}
}

// ---------------- 一条 WS 连接（多路复用） ----------------
class Mux {
	constructor(client) {
		this.client = client;
		this.ws = null;
		this.parser = new FrameParser();
		this.streams = new Map();
		this.nextSid = 0;
		this.peerWindow = client.window;
		this.ourWindow = client.window;
		this.ackThreshold = Math.max(16 * 1024, client.window >> 2);
		this.authed = false;
		this.authFailed = false;
		this.closed = false;
		this.lastRx = Date.now();
		this.pendingNoted = false;
		this.byKey = new Map(); // id/host -> {host, port}
		this.closedPromise = new Promise((r) => (this._done = r));
	}

	async connect(spec) {
		await new Promise((resolveConn, rejectConn) => {
			const ws = new WebSocket(spec.url);
			ws.binaryType = "arraybuffer";
			this.ws = ws;
			let settled = false;
			const done = (err) => {
				if (settled) return;
				settled = true;
				err ? rejectConn(err) : resolveConn();
			};
			ws.onopen = () => {
				debug(`已连上 ${spec.url}`);
				done();
			};
			ws.onerror = () => done(new Error(`连接 ${spec.url} 失败`));
			ws.onclose = (ev) => {
				this.closed = true;
				this.cleanup();
				done(new Error(`连接关闭 (${ev.code})`));
				this._done();
			};
			ws.onmessage = (ev) => {
				if (typeof ev.data === "string") return;
				this.onChunk(Buffer.from(ev.data));
			};
		});
	}

	onChunk(buf) {
		this.lastRx = Date.now();
		for (const { sid, type, payload } of this.parser.push(buf)) {
			try {
				this.onFrame(sid, type, payload);
			} catch (e) {
				warn(`处理帧出错: ${e.message}`);
			}
		}
	}

	onFrame(sid, type, payload) {
		if (type === CTRL) {
			if (!payload.length) return;
			this.onCtrl(JSON.parse(payload.toString("utf8")));
			return;
		}
		if (type === WU) {
			const st = this.streams.get(sid);
			if (st) {
				st.credit += payload.readUInt32BE(0);
				this.flushPending(st);
			}
			return;
		}
		if (type === OPEN) {
			const info = payload.length ? JSON.parse(payload.toString("utf8")) : {};
			const credit = Number(info.w) || this.peerWindow;
			const key = String(info.id || info.host || "").toLowerCase();
			const target = this.byKey.get(key);
			if (!target) {
				warn(`收到未知隧道 ${key} 的流，重置`);
				this.sendFrame(sid, RST, Buffer.alloc(0));
				return;
			}
			const st = new Stream(sid, this, credit, target);
			this.streams.set(sid, st);
			return;
		}
		const st = this.streams.get(sid);
		if (!st) return;
		if (type === DATA) {
			st.write(payload);
			st.unacked += payload.length;
			if (st.unacked >= this.ackThreshold) {
				this.sendFrame(sid, WU, u32(st.unacked));
				st.unacked = 0;
			}
			return;
		}
		if (type === FIN) {
			st.finRx = true;
			st.end();
			return;
		}
		if (type === RST) {
			st.destroy();
			this.streams.delete(sid);
		}
	}

	onCtrl(msg) {
		switch (msg.type) {
			case "challenge": {
				if (!this.client.store.cred.token) return;
				const nonce = Buffer.from(String(msg.nonce || ""), "base64");
				const proof = createHmac("sha256", this.client.store.cred.token).update(nonce).digest();
				this.send(
					ctrl({
						type: "auth",
						client: this.client.store.cred.uuid,
						proof: proof.toString("base64"),
						w: this.ourWindow,
						tunnels: this.client.tunnelPayload(),
					}),
				);
				return;
			}
			case "enroll_pending": {
				const uuid = String(msg.uuid || "").toLowerCase();
				if (uuid && uuid !== this.client.store.cred.uuid) {
					this.client.store.cred.uuid = uuid;
					this.client.store.saveCred();
					info(`服务端分配的客户端 UUID: ${uuid}`);
				}
				if (!this.pendingNoted) {
					this.pendingNoted = true;
					info(String(msg.note || "等待审批"));
				}
				return;
			}
			case "enroll_ok": {
				this.client.store.cred.uuid = String(msg.uuid || this.client.store.cred.uuid).toLowerCase();
				this.client.store.cred.token = String(msg.token || "");
				this.client.store.saveCred();
				info("申请已批准，正在用新凭证重新连接");
				this.close();
				return;
			}
			case "registered": {
				const first = !this.authed;
				this.authed = true;
				this.peerWindow = Number(msg.w) || this.peerWindow;
				const assigned = msg.tunnels || [];
				const changed = this.client.applyAssignment(assigned, msg.prefix, msg.public_hosts);
				const ids = msg.ids || [];
				const hosts = msg.hosts || [];
				const denied = msg.denied || [];
				if (!first && !changed && !denied.length) return; // 没变化就别刷屏
				const extra =
					(ids.length ? ` [路径: ${ids.join(", ")}]` : "") + (hosts.length ? ` [域名: ${hosts.join(", ")}]` : "");
				info(
					`${first ? "已注册" : "已更新"} ${ids.length + hosts.length} 条隧道${extra}` +
						(denied.length ? `（被拒绝: ${denied.join(", ")}）` : ""),
				);
				if (first) this.client.logUrls(msg.prefix);
				return;
			}
			case "ping":
				this.send(ctrl({ type: "pong" }));
				return;
			case "error":
				this.authFailed = true;
				error(`edge 拒绝: ${msg.msg}`);
				this.close();
				return;
			default:
		}
	}

	// ---- 发送 ----
	send(buf) {
		if (this.closed || !this.ws || this.ws.readyState !== 1) return;
		try {
			this.ws.send(buf);
		} catch (e) {
			debug(`发送失败: ${e.message}`);
		}
	}

	sendFrame(sid, type, payload) {
		this.send(frame(sid, type, payload));
	}

	sendData(st, chunk) {
		if (st.closed) return;
		let buf = chunk;
		if (st.credit < buf.length) {
			const can = Math.max(0, st.credit);
			if (can > 0) {
				this.writeData(st.sid, buf.subarray(0, can));
				st.credit = 0;
			}
			st.pending.push(buf.subarray(can));
			st.pendingLen += buf.length - can;
			return;
		}
		st.credit -= buf.length;
		this.writeData(st.sid, buf);
	}

	writeData(sid, chunk) {
		for (let off = 0; off < chunk.length; off += MAX_CHUNK) {
			this.sendFrame(sid, DATA, chunk.subarray(off, Math.min(off + MAX_CHUNK, chunk.length)));
		}
	}

	flushPending(st) {
		while (st.pending.length && st.credit > 0) {
			const head = st.pending[0];
			if (head.length <= st.credit) {
				st.credit -= head.length;
				st.pendingLen -= head.length;
				st.pending.shift();
				this.writeData(st.sid, head);
			} else {
				const part = head.subarray(0, st.credit);
				st.pending[0] = head.subarray(st.credit);
				st.pendingLen -= part.length;
				st.credit = 0;
				this.writeData(st.sid, part);
				break;
			}
		}
	}

	sendFin(st) {
		if (st.finSent || st.closed) return;
		st.finSent = true;
		this.sendFrame(st.sid, FIN, Buffer.alloc(0));
		if (st.finRx) {
			st.destroy();
			this.streams.delete(st.sid);
		}
	}

	reset(st) {
		if (st.closed) return;
		st.closed = true;
		this.sendFrame(st.sid, RST, Buffer.alloc(0));
		st.destroy();
		this.streams.delete(st.sid);
	}

	drop(sid) {
		this.streams.delete(sid);
	}

	register(payload) {
		if (this.authed) this.send(ctrl({ type: "register", tunnels: payload }));
	}

	close() {
		this.closed = true;
		try {
			this.ws?.close();
		} catch {
			// 忽略
		}
		this.cleanup();
		this._done();
	}

	cleanup() {
		for (const st of this.streams.values()) st.destroy();
		this.streams.clear();
	}
}

function u32(n) {
	const b = Buffer.allocUnsafe(4);
	b.writeUInt32BE(n >>> 0, 0);
	return b;
}

// ---------------- 客户端主体 ----------------
class Client {
	constructor(cfg, cwd, configPath = null) {
		this.cfg = cfg;
		this.configPath = configPath;
		this.store = new Store(cfg, cwd);
		this.label = String(cfg.label || this.store.cred.label || hostname());
		this.specs = parseTunnels(cfg);
		this.servers = [].concat(cfg.server || cfg.servers || [DEFAULT_SERVER]).map(serverSpec);
		this.connections = Math.max(1, Number(cfg.connections) || 4);
		this.window = Number(cfg.window) || DEFAULT_WINDOW;
		this.heartbeat = Number(cfg.heartbeat) || 15;
		this.deadAfter = Number(cfg.dead_after) || 60;
		this.maxDelay = Number(cfg.reconnect_max_delay) || 30;
		this.muxes = new Set();
		this.enrollClaim = 0;
		this.stopping = false;
		this.reloadEvery = Number(cfg.reload_interval ?? 5);
		this.lastMtime = this.configPath ? mtime(this.configPath) : null;
		this.applyStoredIds();
	}

	/** 配置文件变了就重新注册（新增/删除隧道不用重启） */
	watchConfig() {
		if (!this.configPath || !(this.reloadEvery > 0)) return;
		const timer = setInterval(() => {
			const now = mtime(this.configPath);
			if (now === this.lastMtime) return;
			this.lastMtime = now;
			try {
				const cfg = JSON.parse(readFileSync(this.configPath, "utf8"));
				for (const k of Object.keys(cfg)) if (k.startsWith("_")) delete cfg[k];
				const specs = parseTunnels(cfg);
				const shape = (list) => list.map((x) => `${x.local}|${x.host || ""}`).sort().join(",");
				if (shape(specs) === shape(this.specs)) return;
				const known = new Map(this.specs.map((sp) => [sp.local, sp.id]));
				for (const sp of specs) if (!sp.id && !sp.host && known.get(sp.local)) sp.id = known.get(sp.local);
				this.specs = specs;
				this.applyStoredIds();
				for (const mux of this.muxes) {
					this.rebuildKeys(mux);
					mux.register(this.tunnelPayload());
				}
				info(`配置已变更，重新注册 ${this.specs.length} 条隧道`);
			} catch (e) {
				error(`重载配置失败（保留旧的）: ${e.message}`);
			}
		}, Math.max(500, this.reloadEvery * 1000));
		timer.unref?.();
	}

	applyStoredIds() {
		for (const sp of this.specs) {
			if (!sp.id && !sp.host && this.store.ids[sp.local]) sp.id = this.store.ids[sp.local];
		}
	}

	tunnelPayload() {
		return this.specs.map((sp) => {
			const item = { local: sp.local };
			if (sp.host) item.host = sp.host;
			else if (sp.id) item.id = sp.id;
			if (sp.label) item.label = sp.label;
			return item;
		});
	}

	rebuildKeys(mux) {
		mux.byKey.clear();
		for (const sp of this.specs) {
			const key = sp.host || sp.id;
			if (!key) continue;
			const [host, port] = splitLocal(sp.local);
			mux.byKey.set(key.toLowerCase(), { host, port });
		}
	}

	applyAssignment(list, prefix, publicHosts) {
		if (publicHosts?.length) this.publicHosts = publicHosts;
		const byLocal = new Map(this.specs.map((sp) => [sp.local, sp]));
		let changed = false;
		for (const item of list || []) {
			const sp = byLocal.get(String(item.local || ""));
			const id = String(item.id || "").toLowerCase();
			if (sp && id && sp.id !== id) {
				sp.id = id;
				this.store.ids[sp.local] = id;
				changed = true;
			}
		}
		if (changed) {
			for (const mux of this.muxes) this.rebuildKeys(mux);
			this.store.saveIds(this.label);
		}
		return changed;
	}

	logUrls(prefix = "/t") {
		const base = (this.publicHosts && this.publicHosts[0]) || new URL(this.servers[0].url).host;
		for (const sp of this.specs) {
			if (sp.host) info(`入口: https://${sp.host}/  ->  ${sp.local}`);
			else if (sp.id) info(`入口: https://${base}${prefix}/${sp.id}/  ->  ${sp.local}`);
		}
	}

	/** 串行化申请：只有一条连接去领 UUID */
	beginEnroll() {
		if (this.store.cred.uuid) return true;
		const now = Date.now();
		if (this.enrollClaim && now - this.enrollClaim < 15000) return false;
		this.enrollClaim = now;
		return true;
	}

	async run() {
		info(
			`客户端 '${this.label}' 启动 ${this.connections} 条连接 -> ` +
				this.servers.map((s) => s.url).join(", ") +
				`，${this.specs.length} 条隧道`,
		);
		this.watchConfig();
		if (this.configPath && this.reloadEvery > 0) info(`配置热重载：每 ${this.reloadEvery}s 检查 ${this.configPath}`);
		await Promise.all([...Array(this.connections).keys()].map((i) => this.slot(i)));
	}

	async slot(idx) {
		const spec = this.servers[idx % this.servers.length];
		let delay = 1;
		while (!this.stopping) {
			const started = Date.now();
			const mux = new Mux(this);
			this.rebuildKeys(mux);
			try {
				await mux.connect(spec);
				this.muxes.add(mux);
				this.hello(mux);
				this.watchdog(mux);
				await mux.closedPromise;
			} catch (e) {
				if (!this.stopping) debug(`#${idx} ${e.message}`);
			} finally {
				this.muxes.delete(mux);
			}
			if (this.stopping) break;
			if (Date.now() - started > 30000) delay = 1;
			if (mux.authFailed) delay = 60;
			debug(`#${idx} ${Math.round(delay)}s 后重连`);
			await sleep(delay * 1000);
			delay = Math.min(delay * 2, this.maxDelay);
		}
	}

	hello(mux) {
		if (!this.store.cred.token) {
			if (!this.beginEnroll()) {
				mux.close();
				return;
			}
			mux.send(ctrl({ type: "enroll", uuid: this.store.cred.uuid || "", label: this.label }));
			info(`已提交接入申请（${this.label}），等待审批`);
			return;
		}
		// 有 token：等 edge 发 challenge
	}

	watchdog(mux) {
		const timer = setInterval(() => {
			if (mux.closed) {
				clearInterval(timer);
				return;
			}
			const idle = Date.now() - mux.lastRx;
			if (idle > this.deadAfter * 1000) {
				warn(`连接空闲 ${Math.round(idle / 1000)}s，重连`);
				mux.close();
				clearInterval(timer);
				return;
			}
			if (mux.authed && idle > this.heartbeat * 1000) mux.send(ctrl({ type: "ping" }));
		}, Math.max(2000, this.heartbeat * 500));
		timer.unref?.();
	}

	stop() {
		this.stopping = true;
		for (const mux of this.muxes) mux.close();
	}
}

function splitLocal(local) {
	const text = String(local);
	let host;
	let port;
	if (text.startsWith("[")) {
		const [h, p] = text.slice(1).split("]:");
		host = h;
		port = p;
	} else {
		const i = text.lastIndexOf(":");
		host = i > 0 ? text.slice(0, i) : "127.0.0.1";
		port = i > 0 ? text.slice(i + 1) : text;
	}
	const n = Number(port);
	if (!n) throw new Error(`地址格式应为 host:port，收到 ${local}`);
	return [host || "127.0.0.1", n];
}

function hostname() {
	try {
		const os = require("node:os");
		return os.hostname().split(".")[0] || "client";
	} catch {
		return "client";
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function mtime(path) {
	try {
		return statSync(path).mtimeMs;
	} catch {
		return null;
	}
}

// ---------------- 入口 ----------------
async function main() {
	let parsed;
	try {
		parsed = parseConfig(process.argv);
	} catch (e) {
		error(`配置有问题: ${e.message}`);
		process.exit(2);
	}
	const { cfg, args } = parsed;
	verbose = args.verbose || !!cfg.verbose;
	logStream = args.logFile || cfg.log_file || null;
	if (logStream) {
		try {
			mkdirSync(dirname(resolve(logStream)), { recursive: true });
			writeFileSync(logStream, ""); // 每次启动清空
		} catch (e) {
			logStream = null;
			error(`日志文件不可写（改成控制台）: ${e.message}`);
		}
	}

	const client = new Client(cfg, process.cwd(), args.json == null ? args.config || "tunnelc.json" : null);
	for (const fn of ["SIGINT", "SIGTERM"]) process.on(fn, () => (client.stop(), process.exit(0)));
	await client.run();
}

main().catch((e) => {
	error(`启动失败: ${e.stack || e.message}`);
	process.exit(1);
});
