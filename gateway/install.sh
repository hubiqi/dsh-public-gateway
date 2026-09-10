#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# dsh-public-gateway 安装 / 升级脚本（幂等，可重复执行）
#
#   bash install.sh              # 安装或升级
#   bash install.sh --uninstall  # 卸载（保留配置）
#
# 做的事：
#   1) 部署 gateway.py 与配置到 /home/opc/dsh-public-gateway
#   2) 把 dsh-web 日志改为持久化路径 /var/log/dsh/dsh-web.log
#      （否则重启后 token 丢失，网关无法工作）
#   3) 安装并启用 systemd user 服务（开机自启）
#   4) 可选：启用 lingering，让服务在未登录时也运行
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/home/opc/dsh-public-gateway"
UNIT_DIR="/home/opc/.config/systemd/user"
LOG_DIR="/var/log/dsh"
UNIT_NAME="dsh-public-gateway.service"

info() { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

# ------------------------------- 卸载 -------------------------------------
if [[ "${1:-}" == "--uninstall" ]]; then
  info "停止并禁用服务"
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload 2>/dev/null || true
  warn "已卸载服务。项目目录 $DEST 与配置保持不变。"
  exit 0
fi

# ---------------------------- 前置检查 ------------------------------------
command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
python3 - <<'PY' || { echo "需要 Python 3.6+"; exit 1; }
import sys
assert sys.version_info >= (3, 6)
PY

# ----------------------------- 部署文件 -----------------------------------
info "创建目录结构"
mkdir -p "$DEST/bin" "$DEST/etc" "$DEST/logs" "$DEST/run"

info "安装程序与配置（已存在的配置不会被覆盖）"
install -m 0644 "$PROJECT_SRC/gateway.py" "$DEST/bin/gateway.py"

if [[ -f "$DEST/etc/gateway.conf" ]]; then
  warn "配置已存在，保留：$DEST/etc/gateway.conf"
else
  install -m 0600 "$PROJECT_SRC/gateway.conf.example" "$DEST/etc/gateway.conf"
  ok "写入默认配置：$DEST/etc/gateway.conf（记得改账号密码）"
fi

# 敏感项：把密码单独放到 env 文件，权限 600（安装后务必改成自己的账号密码）
if [[ ! -f "$DEST/etc/gateway.env" ]]; then
  umask 077
  cat > "$DEST/etc/gateway.env" <<'EOF'
# systemd 注入的敏感变量（优先级高于 gateway.conf）
DSH_GW_USER=change-me
DSH_GW_PASS=change-me
EOF
  ok "写入凭据文件：$DEST/etc/gateway.env（记得改账号密码）"
fi

install -m 0644 "$PROJECT_SRC/README.md" "$DEST/README.md" 2>/dev/null || true

# --------------------- dsh 日志持久化（关键！） ----------------------------
# 原本 dsh-web.service 日志写 /tmp，重启即失，导致网关找不到 token。
info "配置 dsh 日志持久化目录：$LOG_DIR"
sudo mkdir -p "$LOG_DIR"
sudo chown "$USER" "$LOG_DIR" 2>/dev/null || true

DSH_UNIT="$UNIT_DIR/dsh-web.service"
if [[ -f "$DSH_UNIT" ]]; then
  if grep -q '/tmp/dsh-web.log' "$DSH_UNIT"; then
    info "改写 dsh-web.service 的日志路径为 $LOG_DIR"
    cp "$DSH_UNIT" "$DSH_UNIT.bak.$(date +%s)"
    sed -i "s#/tmp/dsh-web\.log#$LOG_DIR/dsh-web.log#g" "$DSH_UNIT"
    ok "已更新 dsh-web.service"
    systemctl --user daemon-reload
  else
    ok "dsh-web.service 日志路径已是持久化路径"
  fi
else
  warn "未找到 $DSH_UNIT，请确认 dsh 服务名"
fi

# --------------------------- 安装服务 -------------------------------------
info "安装 systemd user 服务"
install -m 0644 "$PROJECT_SRC/dsh-public-gateway.service" "$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
ok "已启用开机自启"

# --------------------------- 启动服务 -------------------------------------
info "重启 dsh-web（使日志路径生效）与服务"
systemctl --user restart dsh-web.service 2>/dev/null || warn "dsh-web 重启失败，请手动检查"
sleep 2
systemctl --user restart "$UNIT_NAME"

# --------------------------- lingering ------------------------------------
if command -v loginctl >/dev/null; then
  if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
    info "启用 linger（未登录也保持服务运行，需要 sudo）"
    sudo loginctl enable-linger "$USER" 2>/dev/null \
      && ok "linger 已启用" \
      || warn "启用 linger 失败（可选）。若希望开机无需登录即运行，请手动执行: sudo loginctl enable-linger $USER"
  else
    ok "linger 已启用"
  fi
fi

echo
ok "安装完成。"
echo "  查看状态： systemctl --user status $UNIT_NAME"
echo "  查看日志： journalctl --user -u $UNIT_NAME -f  或  tail -f $DEST/logs/gateway.log"
echo "  修改配置： vi $DEST/etc/gateway.conf  然后 systemctl --user restart $UNIT_NAME"
echo "  卸载：     bash $PROJECT_SRC/install.sh --uninstall"
echo
info "服务状态："
systemctl --user --no-pager status "$UNIT_NAME" | head -12 || true