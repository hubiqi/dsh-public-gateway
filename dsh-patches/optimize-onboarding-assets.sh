#!/bin/sh
# 把 ui-settings-account 的 onboarding 插图压小，修掉 0.2.1 首屏 14MB 的问题。
#
# 背景：该包的 tsdown.config.ts 有个自定义插件把这些图内联成 base64 data URI，
# 而源码图是 3993x2481 / 1785x1530（共 3.6MB）⇒ base64 后 5.2MB 的 client.js，
# 且它在启动批次里，每次打开页面都要下载（0.1.6 没有这个特性，属 0.2.1 回归）。
#
# 修法：不碰代码，只压图 —— 降到 1200px 宽、保留调色板、关抖动。
# 3.6MB -> 0.4MB（省 89%），画质目视无损。
#
# 用法: ./optimize-onboarding-assets.sh /path/to/deepseek-harness
# 依赖: imagemagick (convert)
# 之后需要重新 bundle 该包并重启：
#   DSH_BUILD_FACE=client pnpm --filter @deepseek-ai/dsh-client-ui-settings-account run bundle
#   systemctl --user restart dsh-web
set -eu

ROOT="${1:?用法: $0 /path/to/deepseek-harness}"
ASSETS="$ROOT/packages/client/ui-settings-account/src/client/assets"

[ -d "$ASSETS" ] || { echo "找不到资源目录: $ASSETS" >&2; exit 1; }
command -v convert >/dev/null 2>&1 || { echo "需要 imagemagick 的 convert" >&2; exit 1; }

cd "$ASSETS"
STAMP=$(date +%s)
BACKUP="../assets.bak-$STAMP"
cp -a . "$BACKUP"
echo "原图已备份到 $BACKUP"

BEFORE=$(du -sk . | cut -f1)
tmp=$(mktemp -d)
count=0
for f in *.png; do
  [ -f "$f" ] || continue
  convert "$f" -resize '1200x>' -strip -dither None -colors 255 \
    -define png:compression-level=9 "$tmp/$f"
  if [ -s "$tmp/$f" ]; then
    cp -f "$tmp/$f" "$f"
    count=$((count + 1))
  fi
done
rm -rf "$tmp"
AFTER=$(du -sk . | cut -f1)

echo "已处理 $count 张图"
echo "目录体积: ${BEFORE} KB -> ${AFTER} KB"
echo
echo "下一步（在 dsh checkout 里执行）:"
echo "  DSH_BUILD_FACE=client pnpm --filter @deepseek-ai/dsh-client-ui-settings-account run bundle"
echo "  systemctl --user restart dsh-web"
echo "校验: ls -la packages/client/ui-settings-account/lib/client.js   # 应从 5.2MB 降到 ~0.7MB"
