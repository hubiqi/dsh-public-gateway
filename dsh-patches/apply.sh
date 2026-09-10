#!/bin/bash
# 把 dsh-public-gateway 的 dsh 侧兼容补丁打到一个 dsh checkout 上。
# 用法: ./apply.sh /path/to/deepseek-harness
set -e
KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:?用法: ./apply.sh /path/to/deepseek-harness}"

if ! git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
  echo "错误: $TARGET 不是 git 仓库" >&2
  exit 1
fi
cd "$TARGET"
if [ -n "$(git status --porcelain)" ]; then
  echo "错误: 工作区不干净，先提交或 stash 你的改动" >&2
  exit 1
fi

echo "基线: $(git rev-parse --short HEAD)"
for p in "$KIT_DIR"/*.patch; do
  echo "应用: $(basename "$p")"
  git am --3way "$p"
done
echo "补丁已应用。下一步: 重编前端产物并重启后端（见上层 README）。"
