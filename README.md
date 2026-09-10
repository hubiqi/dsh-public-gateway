# dsh-public-gateway 公网访问 dsh 整套方案

English summary below.

把内网 dsh Web GUI 安全地暴露到公网的完整方案：Python 标准库反向网关
（表单登录 + 双后端 + WebSocket 透传）以及公网访问逼出来的全部 dsh 侧兼容修复。

## 包含什么

- `gateway/` 网关模块（Python 3.6+ 零依赖）：
  - `gateway.py` 反向代理：`/__login` 表单登录（HMAC 会话 cookie，30 天）、
    完整版/原版双后端切换、WS 透传（断线自愈、stale cookie 自动换新）、
    120s 读取超时、UA 日志。gzip 响应的 `accept-encoding` 会剥离，
    保证 ownsHost 注入不丢失（否则远端浏览器设置页不可用）。
    切换页可勾选“同时释放另一端占用”（重启另一端后端放会话锁，
    用前确认那边没有正在跑的任务）。
  - `gateway.conf.example` 配置模板；`install.sh` 一键安装/升级/卸载；
    `dsh-public-gateway.service` 与后端解耦的 systemd user 单元
    （dsh 更新重启不影响网关）；`README.md` / `PROTECT.md` 模块文档。
- `backends/` 两个后端的重建材料：`web-vanilla-profile/`（仅官方 bundle 的
  兜底 profile，直拷进 `$DSH_HOME/profiles/`）、`dsh-web.service.example` /
  `dsh-web-vanilla.service.example`（trusted-host 与持久化日志是网关工作的前提）。
- `dsh-patches/` dsh 侧补丁（公网使用逼出来的修复）：
  - `0001-*` 旧浏览器兼容：`Promise.withResolvers` / `AbortSignal.any` 垫片、
    host 注入启动尾内联 ponyfill、PDF 预览切 legacy 构建、
    settings 启动修复。没有它，Chrome 111 及更早浏览器会永久重连、
    模型页报 `settings are unavailable in this browser`、PDF 预览白屏。
  - `0002-*` 双后端同会话撞锁报 `session/in-use`（中文 toast 明示去占用端
    继续或重启另一端），替代晦涩的 `gateway/internal`；`0003-*` 修正其文案
    （关标签页不释放服务端锁）。
  - `git-hooks/` dsh 更新后自动重建前端产物并重启后端的 hook，
    保证 `git pull` 不打断公网服务。

## 安装

```sh
# 0. 两个后端（完整版 3080 + 原版 3081，网关双模式都要）
cp -r backends/web-vanilla-profile $DSH_HOME/profiles/web-vanilla
# 按 backends/dsh-web.service.example 与 dsh-web-vanilla.service.example 建好
# 两个 systemd user 单元：把 <你的公网IP> 换成服务器公网 IP（trusted-host 必需，
# 否则网关转发的请求会被后端拒绝），WorkingDirectory/PATH 换成你的实际路径，
# 日志必须落在 /var/log/dsh（网关从中取 token，不要写 /tmp）。
# vanilla profile 保持零插件：它是插件弄崩完整版时的兜底。

# 1. 网关
cd gateway && bash install.sh
vi /home/opc/dsh-public-gateway/etc/gateway.env   # 改成自己的账号密码
vi /home/opc/dsh-public-gateway/etc/gateway.conf  # 改端口/后端，按需
systemctl --user restart dsh-public-gateway

# 2. dsh 侧补丁（在 dsh checkout 上）
cd ../dsh-patches && ./apply.sh /path/to/deepseek-harness
cd /path/to/deepseek-harness
pnpm run build && systemctl --user restart dsh-web dsh-web-vanilla

# 3. 更新 hook（可选，防 git pull 打断服务）
./git-hooks/install-hooks.sh /path/to/deepseek-harness
```

默认网关监听 `0.0.0.0:9080`，后端 3080（完整版）/ 3081（原版）。

## 验证

- 公网访问登录页，完整版/原版切换正常，会话、模型、设置可用。
- 用 Chrome ≤118 打开：无永久重连、无报错。
- `git pull` 后看 `/var/log/dsh/post-merge-build.log`，产物自动重建。

## 适用基线

dsh-patches 基于上游 `master @ aa8262ec09`（2026-09-10），`git am --3way`
验证通过；网关模块不依赖 dsh 版本。

License: same as deepseek-harness.

---

## English summary

A complete kit for exposing the dsh Web GUI to the public internet: a
stdlib-only Python reverse gateway (form login, full/vanilla dual backends,
self-healing WebSocket proxy) plus every dsh-side compatibility fix that
public access forced (pre-119 browser shims, gzip-safe host injection,
legacy PDF build, settings boot fix) and post-update rebuild hooks.
