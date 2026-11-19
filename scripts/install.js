#!/usr/bin/env node
/**
 * nife install 时自动跑：只下载**当前平台**的 nifetun-client，放到
 * nife-extension/bin/<platform>-<arch>/ 下。
 *
 * 失败不阻断安装（离线也能装，扩展首次使用时会再试一次；也可以用
 * NIFETUN_CLIENT 指向已有的二进制）。
 */

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const ROOT = path.resolve(__dirname, "..");
const PKG = require(path.join(ROOT, "package.json"));
const VERSION = PKG.version;

function repoSlug() {
	if (process.env.NIFETUN_REPO) return process.env.NIFETUN_REPO;
	const r = PKG.repository;
	if (typeof r === "string") return r.replace(/^github:/, "").replace(/\.git$/, "");
	if (r && r.url) return r.url.replace(/^github:/, "").replace(/\.git$/, "");
	return "OliverMao/nife-tunnel";
}

const PLAT = `${process.platform}-${process.arch}`;
const EXT = process.platform === "win32" ? ".exe" : "";
const NAME = `nifetun-client${EXT}`;
const DEST_DIR = path.join(ROOT, "nife-extension", "bin", PLAT);
const DEST = path.join(DEST_DIR, NAME);

function magicOk(buf) {
	if (process.platform === "win32") return buf[0] === 0x4d && buf[1] === 0x5a; // MZ
	return buf[0] === 0x7f && buf[1] === 0x45 && buf[2] === 0x4c && buf[3] === 0x46; // ELF
}

async function tryDownload(url) {
	const res = await fetch(url, { redirect: "follow" });
	if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
	const buf = Buffer.from(await res.arrayBuffer());
	if (buf.length < 1024 * 1024) throw new Error(`文件太小（${buf.length}B），可能不是二进制: ${url}`);
	if (!magicOk(buf)) throw new Error(`文件头不对，不是 ${PLAT} 的可执行文件: ${url}`);
	fs.mkdirSync(DEST_DIR, { recursive: true });
	fs.writeFileSync(DEST, buf);
	if (process.platform !== "win32") fs.chmodSync(DEST, 0o755);
	return buf.length;
}

async function main() {
	if (process.env.NIFETUN_CLIENT && fs.existsSync(process.env.NIFETUN_CLIENT)) {
		console.log(`[nifetun] 已通过 NIFETUN_CLIENT 指定客户端，跳过下载`);
		return;
	}
	if (fs.existsSync(DEST) && magicOk(fs.readFileSync(DEST))) {
		console.log(`[nifetun] 客户端已就位: ${path.relative(ROOT, DEST)}`);
		return;
	}
	if (!["win32", "linux", "darwin"].includes(process.platform)) {
		console.log(`[nifetun] 不支持的平台 ${PLAT}，请自行设置 NIFETUN_CLIENT`);
		return;
	}

	const slug = repoSlug();
	const asset = `nifetun-client-${PLAT}${EXT}`;
	const urls = [
		process.env.NIFETUN_DOWNLOAD_BASE
			? `${process.env.NIFETUN_DOWNLOAD_BASE.replace(/\/$/, "")}/${asset}`
			: null,
		`https://github.com/${slug}/releases/download/v${VERSION}/${asset}`,
		`https://github.com/${slug}/releases/latest/download/${asset}`,
	].filter(Boolean);

	for (const url of urls) {
		try {
			const size = await tryDownload(url);
			console.log(
				`[nifetun] 已下载 ${PLAT} 客户端 ${(size / 1024 / 1024).toFixed(1)} MB -> ${path.relative(ROOT, DEST)}`,
			);
			return;
		} catch (e) {
			console.log(`[nifetun] 下载失败（${url}）: ${e.message}`);
		}
	}

	console.log(
		[
			`[nifetun] 没能自动下载 ${PLAT} 客户端（装好扩展了，但还缺可执行文件）。`,
			"  三个办法任选其一：",
			`    1) 手动下载放到 ${path.relative(ROOT, DEST)}`,
			"    2) 设环境变量 NIFETUN_CLIENT 指向已有的 nifetun-client",
			`    3) 设 NIFETUN_DOWNLOAD_BASE 指向你们自己的镜像目录再重跑：npm rebuild ${PKG.name}`,
		].join(os.EOL),
	);
}

main().catch((e) => {
	console.log(`[nifetun] 安装脚本出错（不影响安装）: ${e && e.message}`);
});
