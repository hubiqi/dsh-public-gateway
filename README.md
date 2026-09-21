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
  `dsh-web-vanilla.service.example`（trusted-host 与持久化日志是网关工作的前提；
  **入口必须用构建版 `apps/cli/lib/bin.js`，不能用 `pnpm dsh`，原因见「已知坑」**）。
- `dsh-patches/` dsh 侧补丁（公网使用逼出来的修复）：
  - `0001-*` 旧浏览器兼容：`Promise.withResolvers` / `AbortSignal.any` 垫片、
    host 注入启动尾内联 ponyfill、PDF 预览切 legacy 构建、
    settings 启动修复。没有它，Chrome 111 及更早浏览器会永久重连、
    模型页报 `settings are unavailable in this browser`、PDF 预览白屏。
  - `0002-*` 双后端同会话撞锁报 `session/in-use`（中文 toast 明示去占用端
    继续或重启另一端），替代晦涩的 `gateway/internal`；`0003-*` 修正其文案
    （关标签页不释放服务端锁）。
    ⚠️ **这两条已被上游吸收，0.1.6 及以后不要再打**：上游自己实现了
    `session/writer-held`（同样携带 sessionId、客户端按 code 判别），
    merge-forward 时直接采用上游实现即可；硬打 0002/0003 会与上游冲突。
    `0001-*`（旧浏览器兼容）仍然是必需的。
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
# ★ 入口写构建版：<node> /path/to/deepseek-harness/apps/cli/lib/bin.js web ...
#   （别用 `pnpm dsh web`，理由见「已知坑」）
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

## 已知坑（公网 + merge-forward 必读）

### 1. 服务入口：必须用构建版，别用 `pnpm dsh`

`pnpm dsh` = `node --import tsx/esm apps/cli/src/bin.ts`，是**源码优先**入口。
tsx 经 tsconfig paths 会把一部分 workspace 包解析到 `src/`，而另一些
（例如 `@deepseek-ai/dsh-agent-loop`）仍解析到 `lib/` —— 同一个包被加载成两份
模块实例，`Symbol('@deepseek-ai/dsh-tools.scheduler')` 这类 unique symbol
两边不相等。

症状：**PTC 模式（`run_code`）一执行工具就整轮失败**

```
本轮运行失败  Cannot read properties of undefined (reading 'prepare')
```

（失败点是 `agent-loop` 的 `ctx.tools[TOOL_RUNTIME_SCHEDULER].prepare(...)`，
拿到 undefined；native 工具模式不受影响，所以很容易被误判成权限或模型问题。）

修法：两个 systemd 单元都走构建版入口

```
ExecStart=<node> /path/to/deepseek-harness/apps/cli/lib/bin.js web --port 3080 ... --no-open
```

自检：`node apps/cli/lib/bin.js headless "用 run_code 执行 echo hello"`
能正常返回结果即健康。

### 2. merge-forward 之后必须重新构建

上游合并常留下“半成品”：源码已更新、依赖已安装，但 `lib/` 还是旧的。此时

- 权限预设加载失败：`@deepseek-ai/dsh-permission-presets exports "./typert" but
  importing lib/typert.host.js failed: ERR_MODULE_NOT_FOUND`；
- 启动日志刷出一串 `X (@deepseek-ai/dsh-…): failed to import` /
  `N entries did not activate`（严重时 `llm-deepseek` 都起不来）。

修法：解决完冲突后 `pnpm run build`，再
`systemctl --user restart dsh-web dsh-web-vanilla`。
只跑 `pnpm install` 不够 —— 新包的 `lib/*.js` 与 typert host 产物都是构建出来的。

## 验证

- 公网访问登录页，完整版/原版切换正常，会话、模型、设置可用。
- 用 Chrome ≤118 打开：无永久重连、无报错。
- `git pull` 后看 `/var/log/dsh/post-merge-build.log`，产物自动重建。

## 适用基线

dsh-patches 基于上游 `master @ aa8262ec09`（2026-09-10），`git am --3way`
验证通过；网关模块不依赖 dsh 版本。已在 `master @ ddefc45fbc`
（0.1.6-alpha.2）上 merge-forward 验证：补丁 0001 保留，0002/0003 由上游
`session/writer-held` 取代。

License: same as deepseek-harness.

---

## English summary

A complete kit for exposing the dsh Web GUI to the public internet: a
stdlib-only Python reverse gateway (form login, full/vanilla dual backends,
self-healing WebSocket proxy) plus every dsh-side compatibility fix that
public access forced (pre-119 browser shims, gzip-safe host injection,
legacy PDF build, settings boot fix) and post-update rebuild hooks.

Two traps are documented under "已知坑": boot the backends from the built CLI
(`apps/cli/lib/bin.js`), never from the tsx source entry `pnpm dsh` — the source
entry loads some workspace packages from `src/` and others from `lib/`, so
duplicate module instances make the unique tool-scheduler symbol unresolvable
and every PTC (`run_code`) turn dies with
"Cannot read properties of undefined (reading 'prepare')". And a merge-forward
is only finished after `pnpm run build`, otherwise the new packages' `lib/`
artifacts and typert host bundles are missing.
