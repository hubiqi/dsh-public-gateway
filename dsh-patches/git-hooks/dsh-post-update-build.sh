#!/bin/bash
# dsh 更新后自动重建对外服务的产物并重启后端（local-only，不随仓库提交）。
# 由 .git/hooks/post-merge 与 post-rewrite 调用；重活放后台，前台只做路径判断。
# 日志：/var/log/dsh/post-merge-build.log
REPO=/home/opc/deepseek-harness
LOG=/var/log/dsh/post-merge-build.log
LOCK=/tmp/dsh-post-update-build.lock

# post-rewrite 只响应 rebase（amend 等不触发重建）
if [ "${1:-}" != "" ] && [ "${1:-}" != "rebase" ]; then
  exit 0
fi

cd "$REPO" || exit 0
# 合并/变基前的位置：ORIG_HEAD；取不到则保守起见直接重建
if CHANGED=$(git diff --name-only ORIG_HEAD HEAD 2>/dev/null); then
  echo "$CHANGED" | grep -qE '^(packages/[^/]+/[^/]+/(src|tsdown.config.ts)|apps/[^/]+/(src|vite.config.ts)|vendor/|package.json|pnpm-lock.yaml|pnpm-workspace.yaml|tsconfig[^/]*.json|tsdown.config.ts)' || exit 0
fi

mkdir "$LOCK" 2>/dev/null || exit 0  # 已有构建在跑，本次跳过
(
  echo "=== post-update build $(date -u '+%F %T') ===" >>"$LOG" 2>&1
  cd "$REPO" || { echo "CD FAILED" >>"$LOG"; exit 0; }
  FAIL=0
  pnpm install >>"$LOG" 2>&1 || { echo "INSTALL FAILED" >>"$LOG"; FAIL=1; }
  pnpm run build:lib:host >>"$LOG" 2>&1 || { echo "HOST LIB BUILD FAILED" >>"$LOG"; FAIL=1; }
  pnpm run build:lib:client >>"$LOG" 2>&1 || { echo "CLIENT LIB BUILD FAILED" >>"$LOG"; FAIL=1; }
  pnpm run build:web >>"$LOG" 2>&1 || { echo "WEB BUILD FAILED" >>"$LOG"; FAIL=1; }
  if [ "$FAIL" = "0" ]; then
    systemctl --user restart dsh-web.service dsh-web-vanilla.service >>"$LOG" 2>&1 \
      || echo "BACKEND RESTART FAILED" >>"$LOG"
    echo "OK backends restarted" >>"$LOG"
  else
    echo "BUILD FAILED, backends NOT restarted (gateway keeps serving old build)" >>"$LOG"
  fi
  echo "=== done $(date -u '+%F %T') ===" >>"$LOG"
  rmdir "$LOCK" 2>/dev/null
) >>"$LOG" 2>&1 &
disown 2>/dev/null
exit 0
