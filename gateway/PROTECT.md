# 公网网关模块保护说明（修改前必读）

> 本模块是手机/电脑经公网访问 dsh 的唯一入口。dsh 本体更新、重启、
> 重建都不应影响它。**任何改动必须先经机主确认**，禁止顺手改、禁止
> 记忆中"顺带优化"。

## 1. 目录与职责边界

- 源码（唯一可改位置）：`/home/opc/dsh-public-gateway-src/`
  - `gateway.py` 网关本体（Python 3.6 标准库 only，禁 f-string/walrus/三方库）
  - `gateway.conf` 后端地址端口（唯一允许 dsh 侧变化的适配点）
  - `README.md` 行为记录（每次修完追加条目）
  - 本文件：稳定性契约
- 部署（只读拷贝，勿直接改）：`/home/opc/dsh-public-gateway/`
  - `bin/gateway.py`、`README.md` 由源码 `install` 过去
  - `etc/gateway.conf`、`etc/gateway.sessionkey`、`logs/` 是本机状态，永不覆盖
- dsh 本体：`/home/opc/deepseek-harness`（仓库）+ `~/.dsh/profiles/`
  - 网关不在 dsh 树内放任何文件，dsh 更新（git pull / rebuild）碰不到网关文件

## 2. 与 dsh 的契约点（dsh 更新时只核验这些）

1. 后端端口与 Host：只读 `gateway.conf`（full 3080 / vanilla 3081，loopback）。
   dsh 改端口只改 conf，不改代码。
2. token 发现：`/var/log/dsh/dsh-web*.log` 里的 `dsh web: http://127.0.0.1:PORT/?token=...`
   行。dsh 若改日志位置/格式，网关还有主动探测+缓存两层回退，但需按第 4 节核验。
3. 认证交换：首页 `/?token=` 302/303 + `Set-Cookie: dsh-auth-*`；`/api/*` 与
   WS 升级认该 cookie。dsh 若改认证机制，网关必须同步改。
4. systemd 单元 `dsh-public-gateway.service` 与 dsh 单元**无任何绑定**
   （无 PartOf/BindsTo/Wants）。dsh 重启时网关保持运行，后端未就绪回 503，
   恢复后自动可用。绝不加回绑定。
5. dsh 树内的三个本地补丁（网关依赖其行为，dsh 更新若覆盖需重打）：
   - `apps/web/src/compat.ts` + `main.ts` 首行导入（老浏览器补齐）
   - `packages/host/webserver/src/injections.ts`（尾脚本 ponyfill）
   - `packages/client/ui-sidebar-documentpreview` 的 `pdfjs-dist/legacy` 引用

## 3. 禁止事项

- 禁止直接改 `/home/opc/dsh-public-gateway/` 下任何文件（改源码再 install）。
- 禁止重启 `dsh-web` / `dsh-web-vanilla` 来验证网关改动（只重启网关自己）。
- 禁止 `pkill -f`（会误杀执行者自己的 shell），停进程只 kill 精确 PID。
- 禁止把网关逻辑搬进 dsh 插件、preset 或 cordis 组合。
- Python 必须保持 3.6 兼容（ outdoors 系统版本），只用标准库。

## 4. 修改流程（确认后执行）

1. 备份：`cp bin/gateway.py /tmp/gateway.py.bak-$(date +%H%M)`（部署目录）。
2. 改源码，`python3 -m py_compile gateway.py` 必过。
3. 部署：`install -m 0644 src/gateway.py bin/gateway.py`（conf 不覆盖）。
4. 只重启网关：`systemctl --user restart dsh-public-gateway.service`。
5. 验证矩阵（全过才算完）：
   - `curl /__login` 200；新登录进完整版/初始版各一次，工作区与模型正常；
   - 带 `Accept-Encoding: gzip, deflate, br` 取首页仍是明文且含 ownsHost；
   - `journalctl --user -u dsh-public-gateway -n 20` 无 traceback。

## 5. dsh 更新 / 服务器重启后的核验（无需改网关，按序检查）

1. 三服务 active：`dsh-web`、`dsh-web-vanilla`、`dsh-public-gateway`。
2. 新 token 已生成（`tail /var/log/dsh/dsh-web*.log`），网关 2 秒内自动跟上。
3. 线上首页含 `ownsHost`、新壳 hash、尾 ponyfill（见 README 验证命令史）。
4. 三个本地补丁随仓库提交固化（本地 commit `local: keep public-gateway
   clients working on old browsers`，在 master 顶上）。`git pull` 会自动
   合并；`compat.ts` 是新文件不会冲突。post-merge/post-rewrite 钩子
   （`.git/hooks/`，不随仓库提交）会在拉到代码变更后自动重打
   `build:lib:host` + `build:lib:client` + `build:web` 并重启后端，
   日志见 `/var/log/dsh/post-merge-build.log`。若 pull 报冲突，用
   `git status` 看是哪一个补丁撞了上游改动，解决后继续。
5. 服务器重启：三服务均 enabled + linger 生效，开机自起，无需人工干预。

## 6. 回滚

网关：`install -m 0644 /tmp/gateway.py.bak-HHMM bin/gateway.py` + 重启网关。
dsh 本体：与网关无关，按 dsh 自己的流程回滚；回滚期间网关回 503，恢复后自愈。
