#!/usr/bin/env bash
# 把 client/ 下已编好的二进制整理成 GitHub Release 的附件（按平台命名）。
#
#   ./scripts/pack-release.sh
#   gh release create v0.1.0 release/* --title v0.1.0 --notes "nifetun 客户端"
#
# 扩展安装时只下载自己平台那一个（scripts/install.js 和 nife-extension/index.ts 的 downloadBinary）。

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="$(node -p "require('./package.json').version" 2>/dev/null || python3 -c "import json;print(json.load(open('package.json'))['version'])")"
OUT="$ROOT/release"
rm -rf "$OUT" && mkdir -p "$OUT"

copy() { # <源文件> <附件名>
  local src="$1" name="$2"
  if [ -f "$ROOT/$src" ]; then
    cp "$ROOT/$src" "$OUT/$name"
    chmod +x "$OUT/$name" 2>/dev/null || true
    printf '  %-32s %s\n' "$name" "$(du -h "$OUT/$name" | cut -f1)"
  else
    printf '  %-32s (缺)\n' "$name"
  fi
}

echo "打包 v$VERSION -> release/"
copy client/nifetun-client.exe "nifetun-client-win32-x64.exe"
copy client/nifetun-client     "nifetun-client-linux-x64"

if [ -f "$OUT/nifetun-client-linux-x64" ]; then
  tar -C "$OUT" -czf "$OUT/nifetun-client-linux-x64.tar.gz" nifetun-client-linux-x64
  printf '  %-32s %s\n' "nifetun-client-linux-x64.tar.gz" "$(du -h "$OUT/nifetun-client-linux-x64.tar.gz" | cut -f1)"
fi

cat <<EOF

下一步（附件名必须和上面一致）：
  gh release create v$VERSION $OUT/* --title "v$VERSION" --notes "nifetun 客户端 v$VERSION"

或者手动：仓库 → Releases → Draft a new release → tag 填 v$VERSION → 上传 release/ 里的文件。
EOF
