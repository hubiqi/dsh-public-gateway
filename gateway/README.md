# dsh-public-gateway

把本机 DeepSeek Harness (dsh web) 以账号密码登录页 + 反代方式安全暴露到公网。
登录页带「初始版」开关（默认关闭）：不勾选进完整版，勾选进初始版。

## 架构

```
公网浏览器 --(登录页: 账号+密码+初始版开关)--> dsh-public-gateway :9080
    |-- 完整版模式 --> dsh web profile         :3080 (全部插件)
    |-- 初始版模式 --> dsh web-vanilla profile :3081 (仅官方 bundle)
```

初始版是独立的 dsh 进程（`web-vanilla` profile，仅 `dsh-base` + `dsh-web-app`），
与主后端互相隔离：第三方插件把完整版弄崩也不会影响初始版。

## 位置

- 运行目录: /home/opc/dsh-public-gateway
- 源码仓库: /home/opc/dsh-public-gateway-src
- 配置:     /home/opc/dsh-public-gateway/etc/gateway.conf
- 凭据:     /home/opc/dsh-public-gateway/etc/gateway.env  (chmod 600)
- 会话签名: /home/opc/dsh-public-gateway/etc/gateway.sessionkey (自动生成, chmod 600)
- 网关日志: /home/opc/dsh-public-gateway/logs/gateway.log
- dsh 日志: /var/log/dsh/dsh-web.log 与 dsh-web-vanilla.log (网关从中取 token)

## 抗 dsh 更新/重置的措施

1. token 每次请求前按间隔重新读取, 缓存 2 秒 -> dsh 重启换 token 自动跟随
2. token 获取多策略回退: 日志(按端口匹配) -> 主动探测 -> 上次缓存
3. dsh 日志已从 /tmp 迁到 /var/log/dsh, 重启不丢 token
4. 任一后端未就绪返回 503, 服务常驻, 恢复后自动可用
5. 多线程服务器, WebSocket 长连接不阻塞其它请求
6. 登录失败分级锁定: 3 次锁 60 秒, 再 3 次锁 30 分钟
7. 登录会话为 HMAC 签名 cookie (30 天, 见 session_max_age_days), 密钥落盘持久化
8. 旧 HTTP Basic 客户端仍兼容, 视为完整版模式
9. WebSocket 升级失败自动治愈: 后端 401 时网关用首页 ?token= 兑换新鲜
   dsh-auth cookie 后重试一次, 并把新 cookie 塞进 101 响应; dsh 重启导致
   浏览器旧 cookie 失效后, 常驻标签页无需刷新即可恢复连接
10. 空闲 WebSocket 不再被后端读超时掐断 (握手读完头后 sock 置回阻塞模式)
11. 去掉透传到后端的 accept-encoding (回环链路压缩无收益): 后端回 gzip
    时注入逻辑看不到明文, ownsHost 注入被静默跳过, 非 loopback 页会降
    级为 memory 设置 ("settings are unavailable in this browser")

## 常用命令

```bash
systemctl --user status dsh-public-gateway     # 状态
tail -f /home/opc/dsh-public-gateway/logs/gateway.log  # 日志
systemctl --user restart dsh-public-gateway    # 重启
systemctl --user restart dsh-web               # 重启完整版 dsh
systemctl --user restart dsh-web-vanilla       # 重启初始版 dsh
```

切换版本无需重新输入密码：已登录时访问 `http://<公网IP>:9080/__login`
可直接切换完整版/初始版，或退出登录。

## 升级

```bash
cd /home/opc/dsh-public-gateway-src && bash install.sh
```

重复执行即升级, 不会覆盖已有 gateway.conf。

## 卸载

```bash
bash /home/opc/dsh-public-gateway-src/install.sh --uninstall
```

## 安全提示

当前为 HTTP 明文, 登录密码可被嗅探。公网长期使用建议前置 Caddy/Nginx 做 TLS,
再用安全组限制来源 IP。
