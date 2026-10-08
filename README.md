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
  - `check-plugin-api.py` 放行第三方插件前的 API 核查工具
    （peer 范围只是声明，真正决定能否运行的是符号有没有变，见「已知坑 3」）。

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

### 2. merge-forward 之后必须重新构建（跨大版本升级更狠）

上游合并常留下“半成品”：源码已更新、依赖已安装，但 `lib/` 还是旧的。症状：

- 权限预设加载失败：`@deepseek-ai/dsh-permission-presets exports "./typert" but
  importing lib/typert.host.js failed: ERR_MODULE_NOT_FOUND`；
- 启动日志刷出一串 `X (@deepseek-ai/dsh-…): failed to import` /
  `N entries did not activate`（严重时 `llm-deepseek` 都起不来）；
- **后端直接起不来**（0.1.6 → 0.2.1 实测）：`dsh.bundle.patch` 由字符串改成数组，
  旧 `app-boot/lib` 仍在 `path.join(packageDir, declared)` 上吃数组 →
  `TypeError [ERR_INVALID_ARG_TYPE]: The "path" argument must be of type string.
  Received an instance of Array`，vanilla 后端在 `activating / auto-restart` 里反复重启。

修法（跨大版本时三步都要做，缺一不可）：

```sh
# ① 清增量构建缓存，逼 tsc 全量重编 —— 否则过期 lib 不会被覆盖
find . -name "*.tsbuildinfo" -not -path "*/node_modules/*" -delete

# ② 清“空壳残留目录”：上游把包删了，本地 lib/ + node_modules/ 还在。
#    tsc 忽略它们（不是有效包），tsdown 的 workspace glob packages/*/* 却会扫到，
#    读到过期 lib 就报 [MISSING_EXPORT]，构建直接失败（实测 13 个）。
find packages -mindepth 2 -maxdepth 2 -type d -not -exec test -f {}/package.json \; -print
#    ↑ 先列出来核对（都是 git 不跟踪、只剩 lib/node_modules 的目录），备份后删掉

# ③ 全量构建 + 重启
pnpm run build && systemctl --user restart dsh-web dsh-web-vanilla
```

只跑 `pnpm install` 不够 —— 新包的 `lib/*.js` 与 typert host 产物都是构建出来的。

### 3. 插件版本门禁（0.2.1+）

0.2.1 起 dsh 按 peerDependencies 拦第三方插件，启动日志出现：

```
dsh: skipping profile bundle "X": Plugin X@ver is incompatible with dsh <runtime>:
peerDependencies {...}. … grant the exact-version exemption for X@ver on dsh <runtime>
with `dsh plugin allow-version` …
```

**先找有没有兼容的新版**（最干净），实测结果：

| 插件 | 结论 |
|---|---|
| `dshmarket` | 升到 `1.66.9`（peer 含 `^0.2.0-rc.1`）即通过，**无需豁免** |
| `@openviking/dsh-memory-plugin` | 最新 0.5.16 的 peer 仍止步 `^0.1.7-rc.2`，需豁免 |
| `@dsh-external/dsh-normify` | 本地包，peer 写死 `<0.2.0`，需豁免 |

真需要放行时（写进 `<profile>/compatibility.json`，可撤销）：

```sh
dsh plugin --profile web allow-version "<pkg>@<ver>" --dsh-version <exact-runtime> --accept-risk
dsh plugin --profile web revoke-version "<pkg>@<ver>" --dsh-version <exact-runtime>
dsh plugin --profile web version-exemptions      # 查看当前豁免
```

放行前**用证据判断 API 是否真的还在**——peer 范围只是声明：

```sh
# 列出插件从 @deepseek-ai/* 具名导入的符号，逐个对 dsh 的 lib/*.d.ts 校验
python3 dsh-patches/check-plugin-api.py
```

（normify 的 `@deepseek-ai/cosmokit` 是它自己 vendor 的，报缺的 `Binary/Dict` 是类型，属误报。）

### 4. 前端 module 脚本的 `crossorigin`（网关侧已剥离）

dsh 前端产物给 `<script type="module">` 与 `modulepreload` 打了 `crossorigin=""`，
浏览器因此以 CORS 模式取**本已同源**的资源；经 Python 网关（ThreadingHTTPServer）
时这类请求会挂起不返回，应用永远停在 “Loading plugins…”。
`gateway.py` 的 `_inject_html` 已顺手剥离（同源资源不需要 CORS）：

```python
body_text = re.sub(r'\s+crossorigin(?:="[^"]*")?', '', body_text)
```

**判断前端到底有没有起来，用服务器上的 chromium，别用手机网络下结论**
（弱网/跨网时 fetch 会整片超时，看起来像“白屏坏了”）：

```sh
TOK=$(grep -o "token=[A-Za-z0-9_-]*" /var/log/dsh/dsh-web.log | tail -1 | cut -d= -f2)
timeout 75 chromium-browser --headless=new --no-sandbox --disable-gpu \
  --dump-dom "http://127.0.0.1:3080/?token=$TOK" > /tmp/dom.html
grep -oE 'aria-label="[^"]{2,20}"' /tmp/dom.html | sort -u
```

DOM 里出现 `aria-label="Send message" / "New session" / "Settings"` 加 `contenteditable`
即应用完整启动。⚠️ 别与 `--virtual-time-budget` 同用，会挂死超时、误判成“页面坏了”。

### 5. `??` 批量模块 URL 不能被追加 `?token=`（网关已修）

dsh 前端把几十个模块打包成一条 URL 取：

```
/plugins/??@deepseek-ai/dsh-client-ui-open-in-app/client.js,@deepseek-ai/dsh-api-gateway/client.js,…&rev=…
```

（注意：不是标准查询串，`?` 之后直接跟包名，多个包用 `,` 分隔，最后 `&rev=` 收尾。）

**该路由只认 cookie，不接受 `token` 参数**——实测任何形式都不行：

| 直连后端的请求 | 结果 |
| --- | --- |
| `…??<pkgs>&rev=xxx` | 200 |
| `…??<pkgs>&rev=xxx&token=<valid>` | **404** |
| `…??<pkgs>&token=<valid>&rev=xxx` | **404** |

网关原本对「没有 `dsh-auth-` cookie 的客户端」一律走 `_with_token()` 追加 token，
于是这类客户端的模块请求全部 404，前端永远停在 “Loading plugins…”。
修法：这类 URL 改走**服务端兑换 cookie**（`_exchange_dsh_cookies`）取内容，
并把兑换到的 cookie 一并递给浏览器：

```python
if _pure.startswith("/plugins/") and "??" in _pure:
    _healed = self._exchange_dsh_cookies(backend, token)
    if _healed:
        self.headers["Cookie"] = _merge_cookie(cookie, _healed)
        _st, _hd, _data = self.fetch(path, body, backend)
        self.emit(_st, _append_set_cookie(_hd, _healed), _data, backend)
        return
```

**排查提示**：这类 URL 里的 `&` 在 HTML 里是 `&amp;`，直接从 HTML 抠出来用它测会
一路 404 —— 先 `html.unescape()` 再请求，否则会误判成网关坏了。

### 6. 端到端验证：用 CDP 驱动服务器 chromium 走网关

`--dump-dom` 只能验“渲染出来没渲染出来”，看不到运行时报错。要看清，用 CDP
（服务器 Node 24 自带全局 `WebSocket`/`fetch`，不需要 puppeteer）：

```
启动 chromium --headless=new --remote-debugging-port=9334 --remote-allow-origins=*
→ GET /json/version 拿 browser WS
→ Target.attachToTarget {flatten:true} 拿 sessionId（之后所有命令都要带它；
  浏览器级 WS 上直接调 Network.setCookie 会报 method not found）
→ Network.setCookie 注入网关会话 cookie（curl 的 cookie 文件里 HttpOnly 行是
  '#HttpOnly_' 前缀，别当注释跳过）
→ Page.navigate + Runtime.enable 收异常
```

实测（经网关带会话）：`mode=live`、`composer=true`、29 个资源全 200、运行时错误 0，
页面停在应用主界面 —— 服务侧链路是通的。

### 7. 不要剥掉 `accept-encoding`（网关已改）

网关为了让 `_inject_html` 看到明文 HTML，原本对**所有**请求剥掉 `accept-encoding`。
但它是给**公网客户端**用的：不压缩等于把十几 MB 的前端产物原样推过公网，
弱网（手机）下极慢甚至“打不开”。实测压缩收益：

| 资源 | 原样 | gzip | 省 |
| --- | --- | --- | --- |
| `index-*.js` | 634 KB | 238 KB | 62% |
| `vendor-*.js` | 741 KB | 210 KB | 72% |
| 两个 CSS | 106 KB | 27 KB | 74% |
| **主要静态资源合计** | **1.48 MB** | **475 KB** | **68%** |
| `??` 大模块包 | 11.1 MB | 5.5 MB | 50% |

现在的策略：**只向客户端声明支持的编码里协商 gzip**，HTML 解压 → 注入 → 摘掉
`content-encoding`（`_decompress_body()`，stdlib 能解 gzip/deflate，**解不了 br**，
所以不协商 br）；JS/CSS 等原样透传压缩流，网关不解不重压。
解压失败则原样透传并记日志——宁可少一次注入，也不能白屏。

### 8. 先判服务，再判网络

“打不开”时按这个顺序排除，别一上来就怀疑本机网络：

```sh
# ① 服务器本地直连后端
curl -o /dev/null -w "%{http_code} %{time_total}s\n" http://127.0.0.1:3080/
# ② 服务器本地访问网关（未登录 401 属正常）
curl -o /dev/null -w "%{http_code} %{time_total}s\n" http://127.0.0.1:9080/
# ③ 服务器用【公网 IP】访问自己 —— 这条通说明端口转发/回环没问题
curl -o /dev/null -w "%{http_code} %{time_total}s\n" http://<公网IP>:9080/
```

三条都正常 ⇒ 服务侧与端口转发都没问题，问题在客户端到服务器的网络路径
（用第 6 条的 CDP 法在服务器上复现一次，能直接看到真实客户端视角）。

### 9. 0.2.1 的 onboarding 图片把首屏撑到 14 MB（已修）

0.2.1 引入 desktop-onboarding 后，`packages/client/ui-settings-account` 的
`tsdown.config.ts` 里有个自定义插件，把 onboarding 插图**内联成 base64 data URI**：

```js
return `export default ${JSON.stringify(`data:image/png;base64,${data.toString('base64')}`)}`
```

源码里是 **8 张 3993×2481 / 1785×1530 的 PNG（共 3.6 MB）**，base64 再膨胀 33%
⇒ 单个 `client.js` **5.2 MB**。而这个模块在**启动批次**里，每次打开页面都要下载，
哪怕用户根本不会看到 onboarding。0.1.6 没有这个特性，属 0.2.1 回归。

**修法（改图片本身，不动代码，零风险）**：降到 1200px 宽 + 保调色板 + 关抖动：

```sh
for f in *.png; do
  convert "$f" -resize '1200x>' -strip -dither None -colors 255 \
    -define png:compression-level=9 "out/$f"
done
```

3.6 MB → 0.4 MB（省 89%），画质目视无损。重新 bundle 后 `client.js` 5.2 MB → 720 KB：

```sh
DSH_BUILD_FACE=client pnpm --filter @deepseek-ai/dsh-client-ui-settings-account run bundle
systemctl --user restart dsh-web
```

**整页效果（实测经网关，含今天全部修复）**：

| 阶段 | 首屏传输 |
| --- | --- |
| 修复前（网关不压缩 + 5.2 MB 内联图） | ~14.3 MB |
| 网关 gzip 修复后 | ~7 MB |
| onboarding 图片压缩后 | **2.5 MB**（CDP 实测，约 6 秒打开） |

### 10. 0.2.1 新增插件版本门禁（详见第 3 条）

另注：界面若报 `400 Model is unavailable`，是 `settings.yaml` 里配了网关
不存在的模型 id（例如 `space-bunny-free`/`Union Alpha Free`/`DeepSeek V4.1 Flash`，
网关实际只有 `space-bunny`/`deepseek-v4.1-flash` 等）。用
`curl .../v1/models` 核对真实 id 再改配置。

### 11. 事件流（SSE）必须逐块透传，不能缓冲（网关已修）

dsh 前端用 `GET /plugins/events` 订阅事件流：响应是
`content-type: text/event-stream` + `Transfer-Encoding: chunked`，
**永远不结束**。网关原先对它也走 `fetch()`，而 `fetch()` 里是

```python
resp = conn.getresponse()
data = resp.read()      # ← 等整个响应读完
```

流式响应永远读不完 ⇒ 网关干等 120s ⇒ 超时回 502 ⇒ **前端永远停在「载入历史…」**，
对话怎么都出不来。日志里能看到（超时日志带上了路径，一抓就准）：

```
proxy error [完整版]: GET /plugins/events: timed out
```

修法：识别客户端是否在要 SSE（`EventSource` 会带 `Accept: text/event-stream`），
命中就换成逐块透传、用 chunked 收尾，连接保持：

```python
def _wants_stream(self):
    return "text/event-stream" in (self.headers.get("Accept") or "").lower()
```

```python
self.send_header("Transfer-Encoding", "chunked")
self.end_headers()
while True:
    chunk = resp.read1(8192)
    if not chunk: break
    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
    self.wfile.flush()
```

验证：修复前 120s 后 502；修复后**首字节 3.7ms**、正确收到 28KB，
且日志中此类超时**彻底消失**（修复前每几分钟一条）。
另注意：`GET /plugins/events` 这类长连接会占住一个处理线程，这是预期行为。

**结论**：任何“界面转圈、对话/历史出不来”的现象，先看网关日志里
`proxy error ...: <路径>: timed out` —— 那条路径就是卡住的请求。

### 12. 排查用：这条路径链最能定位问题

```
浏览器 → 网关(9080) → 后端(3080/3081) → 事件流(/plugins/events)
```

- 页面骨架出不来 → 看静态资源与模块 URL（第 4/5 条）
- 骨架出来但内容空转 → 看事件流是否被缓冲（第 11 条）
- 全都快但整体慢 → 看体积（第 7/9 条）



- 公网访问登录页，完整版/原版切换正常，会话、模型、设置可用。
- 用 Chrome ≤118 打开：无永久重连、无报错。
- `git pull` 后看 `/var/log/dsh/post-merge-build.log`，产物自动重建。

## 适用基线

dsh-patches 基于上游 `master @ aa8262ec09`（2026-09-10），`git am --3way`
验证通过；网关模块不依赖 dsh 版本。

已在两代上游上 merge-forward 验证：

- `master @ ddefc45fbc`（0.1.6-alpha.2）：补丁 0001 保留，0002/0003 由上游
  `session/writer-held` 取代；
- `master @ 5badb15009`（**0.2.1-alpha.1**）：0001 仍需保留；后端入口、全量重建、
  插件版本门禁、`crossorigin` 四类坑已写入「已知坑」（1–4）。

License: same as deepseek-harness.

---

## English summary

A complete kit for exposing the dsh Web GUI to the public internet: a
stdlib-only Python reverse gateway (form login, full/vanilla dual backends,
self-healing WebSocket proxy) plus every dsh-side compatibility fix that
public access forced (pre-119 browser shims, gzip-safe host injection,
legacy PDF build, settings boot fix) and post-update rebuild hooks.

Twelve traps are documented under "已知坑":

1. Boot the backends from the built CLI (`apps/cli/lib/bin.js`), never from the
   tsx source entry `pnpm dsh` — the source entry loads some workspace packages
   from `src/` and others from `lib/`, so duplicate module instances make the
   unique tool-scheduler symbol unresolvable and every PTC (`run_code`) turn
   dies with "Cannot read properties of undefined (reading 'prepare')".
2. A merge-forward is only finished after `pnpm run build` — and across a major
   version you must also drop every `*.tsbuildinfo` (or tsc's incremental cache
   keeps the stale `lib/`) and delete the stale package directories that upstream
   removed but that linger locally with only `lib/` + `node_modules/`: tsc
   ignores them, while tsdown's `packages/*/*` workspace glob reads their outdated
   output and fails the build with `[MISSING_EXPORT]`.
3. dsh 0.2.1+ gates third-party plugins on `peerDependencies` and silently skips
   incompatible ones at boot. Prefer upgrading the plugin; otherwise grant an
   exact-version exemption with `dsh plugin allow-version … --accept-risk`, and
   check `dsh-patches/check-plugin-api.py` first to see whether the APIs the
   plugin imports still exist.
4. dsh's frontend emits `crossorigin=""` on its module scripts, which makes the
   browser request same-origin resources in CORS mode; those requests hang
   through the Python gateway. The gateway now strips the attribute, and the
   README documents the headless-chromium check that tells a real frontend
   failure apart from a bad client network.
5. dsh fetches dozens of modules as one `/plugins/??pkg1,pkg2&rev=` URL, and that
   route accepts cookies only — appending `?token=` returns 404. The gateway used
   to do exactly that for clients without a `dsh-auth-` cookie, so their module
   requests all failed and the app sat on "Loading plugins…"; it now exchanges a
   cookie server-side instead.
6. How to reproduce a client-side failure from the server with CDP (attach to the
   page target, inject the gateway session cookie) — the reliable way to tell a
   broken frontend from a bad network path.
7. The gateway must not strip `accept-encoding`: it serves public clients, so an
   uncompressed response pushes the whole (multi-MB) frontend over the internet.
   It now negotiates gzip and decompresses only the HTML it has to rewrite —
   measured 68% off the main static assets and 50% off the big module bundle.
8. A diagnostics order for "the site won't open": check the backend and gateway
   locally, then from the server against its own public IP (which proves port
   forwarding), before blaming the client's network.
9. dsh 0.2.1's new desktop onboarding inlines eight 4K PNGs as base64 into the
   settings-account client bundle (3.6 MB of source art → a 5.2 MB bundle that the
   boot batch loads on every visit), pushing the first paint to ~14 MB. Resizing
   them to 1200px and keeping the palette cuts that 89%; with the two gateway
   fixes the first load went from ~14.3 MB to 2.5 MB.
10. A `400 Model is unavailable` in the UI usually means `settings.yaml` names a
   model id the provider gateway does not have; check `/v1/models` for the real
   ids.
11. Server-Sent Events must be streamed, not buffered. The gateway read every
   response to completion before forwarding it, so `GET /plugins/events` (an
   endless `text/event-stream` the client subscribes to) blocked until the 120s
   timeout and returned 502 — the conversation simply never loaded
   ("Loading history…" forever). It now detects `Accept: text/event-stream` and
   pumps chunks through with chunked framing; first byte went from a 120s stall
   to 3.7ms. The timeout log now names the method and path, which is what made
   this findable at all.
12. The diagnostic chain: browser → gateway (9080) → backend (3080/3081) →
   event stream (/plugins/events). No page skeleton means static assets or module
   URLs; skeleton but empty content means the event stream; everything correct
   yet slow means payload size.
