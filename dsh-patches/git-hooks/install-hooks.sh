#!/bin/bash
# 把 dsh 更新后自动重建的 hook 装进指定 dsh checkout 的 .git/hooks。
# 用法: ./install-hooks.sh /path/to/deepseek-harness
set -e
KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:?用法: ./install-hooks.sh /path/to/deepseek-harness}"
if ! GIT_DIR="$(git -C "$TARGET" rev-parse --absolute-git-dir 2>/dev/null)"; then
  echo "错误: $TARGET 不是 git 仓库" >&2
  exit 1
fi
HOOKS="$GIT_DIR/hooks"
mkdir -p "$HOOKS"
install -m 0755 "$KIT_DIR/dsh-post-update-build.sh" "$HOOKS/dsh-post-update-build.sh"
printf '#!/bin/sh\nexec "%s/dsh-post-update-build.sh"\n' "$HOOKS" > "$HOOKS/post-merge"
printf '#!/bin/sh\nexec "%s/dsh-post-update-build.sh" "$@"\n' "$HOOKS" > "$HOOKS/post-rewrite"
chmod 0755 "$HOOKS/post-merge" "$HOOKS/post-rewrite"
# hook 里的 REPO 指向本次安装的 checkout；LOG 路径按需再改
sed -i "s#^REPO=.*#REPO=$TARGET#" "$HOOKS/dsh-post-update-build.sh"
echo "已安装（REPO=$TARGET）。日志路径默认 /var/log/dsh/post-merge-build.log，按需修改。"
