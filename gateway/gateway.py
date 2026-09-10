#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-public-gateway
==================

把本机 DeepSeek Harness (dsh web) 以账号密码登录页 + 反代方式安全暴露到公网，
并透明转发 WebSocket（dsh 前端与后端之间使用 /api/remote.mux）。

登录页带「初始版」开关（默认关闭）：不勾选进完整版（含第三方插件），勾选则
进入初始版 —— 另一 dsh 进程（web-vanilla profile，仅官方 bundle）在回环地址
的 vanilla_port 上常驻，与主后端互相隔离，插件把主进程弄崩也不影响初始版。

设计目标（抗 dsh 更新 / 设置重置 / 重启）：
  * 配置全部外置到 etc/gateway.conf，代码升级不影响配置。
  * token 获取采用多策略回退，不依赖 dsh 内部实现细节：
      1) 扫描各后端 dsh 日志中与目标端口匹配的最新 token=...
      2) fallback：直接向对应后端根路径探测（读 Location / Set-Cookie）
      3) 缓存：上一次成功获取到的 token
  * 后端未就绪时返回 503，而不是崩溃；恢复后自动可用。
  * 多线程，单条 WebSocket 长连接不会阻塞其它请求。
  * 鉴权失败分级锁定，防暴力破解。
  * 兼容旧的 HTTP Basic 客户端：仍接受 Authorization 头，视为完整版模式。

仅依赖 Python 3 标准库（兼容 3.6）。
"""

from __future__ import print_function

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import select
import socket
import socketserver
import sys
import threading
import time
try:
    from urllib.parse import parse_qsl, quote
except ImportError:  # Python 2 不再支持，仅为极端回退保留
    from urllib import quote  # type: ignore
    from urlparse import parse_qsl  # type: ignore
from http.server import BaseHTTPRequestHandler, HTTPServer

# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #

# 默认值；可被 etc/gateway.conf 覆盖
CONF = {
    "listen_host": "0.0.0.0",
    "listen_port": "9080",
    "dsh_host": "127.0.0.1",
    "dsh_port": "3080",
    "dsh_log": "/var/log/dsh/dsh-web.log",
    "dsh_log_rotated": "/var/log/dsh/dsh-web.log.1",
    # 初始版后端（web-vanilla profile，仅官方 bundle）
    "vanilla_host": "127.0.0.1",
    "vanilla_port": "3081",
    "vanilla_log": "/var/log/dsh/dsh-web-vanilla.log",
    "vanilla_log_rotated": "/var/log/dsh/dsh-web-vanilla.log.1",
    # 登录会话有效期（天）
    "session_max_age_days": "30",
    "user": "admin",
    "pass": "",
    "stage1_fails": "3",
    "stage1_lock": "60",
    "stage2_fails": "3",
    "stage2_lock": "1800",
    "token_refresh_sec": "2",
    "backend_connect_timeout": "30",
    "backend_read_timeout": "120",
}

def _load_conf(path):
    """极简 key=value 配置解析，支持 `#` 注释与空行。"""
    data = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return data

def _env_override():
    """环境变量优先级最高（便于 systemd 注入密码）。"""
    mapping = {
        "DSH_GW_USER": "user",
        "DSH_GW_PASS": "pass",
        "DSH_GW_LISTEN_PORT": "listen_port",
        "DSH_GW_DSH_PORT": "dsh_port",
        "DSH_GW_VANILLA_PORT": "vanilla_port",
        "DSH_GW_SESSION_KEY": "session_key",
    }
    for env_key, conf_key in mapping.items():
        val = os.environ.get(env_key)
        if val is not None and val != "":
            CONF[conf_key] = val

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONF_PATH = None
# 允许 conf 放在项目根的 etc/ 下
for _cand in (
    os.path.join(_HERE, "..", "etc", "gateway.conf"),
    os.path.join(_HERE, "gateway.conf"),
    "/etc/dsh-public-gateway.conf",
):
    if os.path.isfile(_cand):
        CONF.update(_load_conf(_cand))
        _CONF_PATH = _cand
        break
_env_override()

LISTEN_HOST = CONF["listen_host"]
LISTEN_PORT = int(CONF["listen_port"])
DSH_HOST = CONF["dsh_host"]
DSH_PORT = int(CONF["dsh_port"])
DSH_LOG = CONF["dsh_log"]
DSH_LOG_ROTATED = CONF["dsh_log_rotated"]
VANILLA_HOST = CONF["vanilla_host"]
VANILLA_PORT = int(CONF["vanilla_port"])
VANILLA_LOG = CONF["vanilla_log"]
VANILLA_LOG_ROTATED = CONF["vanilla_log_rotated"]
SESSION_MAX_AGE_SEC = int(float(CONF["session_max_age_days"]) * 24 * 60 * 60)
USERNAME = CONF["user"]
PASSWORD = CONF["pass"]

STAGE1_FAILS = int(CONF["stage1_fails"])
STAGE1_LOCK = int(CONF["stage1_lock"])
STAGE2_FAILS = int(CONF["stage2_fails"])
STAGE2_LOCK = int(CONF["stage2_lock"])
TOKEN_REFRESH_SEC = float(CONF["token_refresh_sec"])
BACKEND_CONNECT_TIMEOUT = float(CONF["backend_connect_timeout"])
BACKEND_READ_TIMEOUT = float(CONF["backend_read_timeout"])

HOP_BY_HOP = set([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])

MODE_FULL = "full"
MODE_VANILLA = "vanilla"

# --------------------------------------------------------------------------- #
# 注入到 HTML 页面的脚本：声明本页 ownsHost，使前端 isLoopback=True，
# 从而启用完整 settings（否则非 loopback 访问会降级为 memory 模式）。
INJECT_JS = (
    "<script>try{globalThis.__DSH_TRANSPORT__="
    "Object.assign({ownsHost:true},globalThis.__DSH_TRANSPORT__||{});}"
    "catch(e){}</script>"
)

# 初始版额外注入：浏览器 tab 标题前缀，明确当前是初始版。
INJECT_JS_VANILLA = (
    "<script>try{(function(){var t='\u3010\u521d\u59cb\u7248\u3011';"
    "function mark(){if(document.title.indexOf(t)!==0)document.title=t+document.title;}"
    "mark();new MutationObserver(mark).observe("
    "document.querySelector('title')||document.documentElement,"
    "{childList:true,subtree:true,characterData:true});})();}"
    "catch(e){}</script>"
)

# 日志
# --------------------------------------------------------------------------- #

def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()

# --------------------------------------------------------------------------- #
# 登录会话（HMAC 签名的无状态 cookie）
# --------------------------------------------------------------------------- #

SESSION_COOKIE = "dsh-gw-session"

def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

def _b64d(text):
    if not re.match(r"^[A-Za-z0-9_\-]*$", text or ""):
        return None
    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (binascii.Error, ValueError):
        return None

def _session_key_file():
    if _CONF_PATH is not None:
        return os.path.join(os.path.dirname(os.path.abspath(_CONF_PATH)),
                            "gateway.sessionkey")
    return os.path.join(_HERE, "..", "etc", "gateway.sessionkey")

def load_session_key():
    """会话签名密钥：环境变量优先，否则落盘持久化（600 权限），网关重启不掉线。"""
    env_key = CONF.get("session_key")
    if env_key:
        return env_key.encode("utf-8")
    path = _session_key_file()
    try:
        with open(path, "r") as fh:
            raw = _b64d(fh.read().strip())
        if raw is not None and len(raw) >= 32:
            return raw
    except Exception:
        pass
    raw = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(_b64e(raw))
    except Exception as exc:
        log("WARN: cannot persist session key (%s); sessions reset on restart" % exc)
    return raw

SESSION_KEY = load_session_key()

def mint_session(user, mode):
    """签发登录会话 cookie 值。"""
    now = int(time.time())
    body = _b64e(json.dumps(
        {"v": 1, "u": user, "m": mode, "iat": now, "exp": now + SESSION_MAX_AGE_SEC},
        separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(SESSION_KEY, body.encode("ascii"),
                         hashlib.sha256).digest())
    return body + "." + sig

def verify_session(value):
    """校验会话 cookie；返回 (user, mode)，无效返回 (None, None)。"""
    try:
        if not value or "." not in value:
            return None, None
        body, sig = value.split(".", 1)
        expect = _b64e(hmac.new(SESSION_KEY, body.encode("ascii"),
                                hashlib.sha256).digest())
        if len(sig) != len(expect) or not hmac.compare_digest(sig, expect):
            return None, None
        raw = _b64d(body)
        if raw is None:
            return None, None
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return None, None
        if payload.get("m") not in (MODE_FULL, MODE_VANILLA):
            return None, None
        now = int(time.time())
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
            return None, None
        if not isinstance(payload.get("u"), str) or not payload["u"]:
            return None, None
        return payload["u"], payload["m"]
    except Exception:
        return None, None

def session_cookie_header(value, max_age):
    return "%s=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax" % (
        SESSION_COOKIE, value, max_age)

def clear_session_cookie_header():
    return "%s=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax" % SESSION_COOKIE

# --------------------------------------------------------------------------- #
# token 获取（多策略 + 缓存 + 定时刷新），每个后端一个实例
# --------------------------------------------------------------------------- #

class TokenProvider(object):
    """线程安全地维护一个后端 dsh 的认证 token。"""

    def __init__(self, port, log_paths):
        self._port = port
        self._log_paths = tuple(log_paths)
        self._lock = threading.Lock()
        self._token = None
        self._last_try = 0.0
        # 匹配形如:  http://127.0.0.1:3080/?token=XXXX
        self._pattern = re.compile(r"token=([A-Za-z0-9_\-]{16,})")

    def get(self):
        with self._lock:
            now = time.time()
            if self._token and (now - self._last_try) < TOKEN_REFRESH_SEC:
                return self._token
            self._last_try = now
            tok = self._from_logs() or self._probe() or self._token
            if tok:
                self._token = tok
            return self._token

    def peek(self):
        """仅读日志的轻量探测（登录页展示后端状态用，不触发网络探测）。"""
        with self._lock:
            return self._token or self._from_logs()

    # --- 策略 1：从日志抓取（最轻量，首选） ---
    def _from_logs(self):
        port_hint = ":%d/?token=" % self._port
        best = None
        for path in self._log_paths:
            try:
                with open(path, "r", errors="ignore") as fh:
                    # 只读尾部，避免大文件全量加载
                    try:
                        fh.seek(0, 2)
                        size = fh.tell()
                        fh.seek(max(0, size - 262144))
                    except Exception:
                        pass
                    text = fh.read()
            except Exception:
                continue
            # 优先匹配目标端口，其次退化为任意 token
            for m in self._pattern.finditer(text):
                seg = text[max(0, m.start() - 40):m.start()]
                if port_hint in seg:
                    best = m.group(1)
            if best is None:
                found = self._pattern.findall(text)
                if found:
                    best = found[-1]
        return best

    # --- 策略 2：向 dsh 直接探测（日志不可用时） ---
    def _probe(self):
        """Probe dsh root path to find token as fallback."""
        try:
            conn = http.client.HTTPConnection(
                BACKENDS_BY_PORT[self._port]["host"], self._port,
                timeout=BACKEND_CONNECT_TIMEOUT)
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read(65536).decode("utf-8", "ignore")
            for header in ("location", "x-dsh-url", "x-auth-url"):
                val = resp.getheader(header)
                if val:
                    m = self._pattern.search(val)
                    if m:
                        return m.group(1)
            m = self._pattern.search(body)
            if m:
                return m.group(1)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return None


# 后端表：登录会话的 mode 决定反代到哪一个 dsh 进程。
BACKENDS = {
    MODE_FULL: {
        "label": "完整版",
        "host": DSH_HOST,
        "port": DSH_PORT,
        "tokens": TokenProvider(DSH_PORT, [DSH_LOG, DSH_LOG_ROTATED]),
        "title_prefix": False,
    },
    MODE_VANILLA: {
        "label": "初始版",
        "host": VANILLA_HOST,
        "port": VANILLA_PORT,
        "tokens": TokenProvider(VANILLA_PORT, [VANILLA_LOG, VANILLA_LOG_ROTATED]),
        "title_prefix": True,
    },
}
BACKENDS_BY_PORT = dict(
    [(DSH_PORT, BACKENDS[MODE_FULL]), (VANILLA_PORT, BACKENDS[MODE_VANILLA])])

# --------------------------------------------------------------------------- #
# IP 锁定
# --------------------------------------------------------------------------- #

_lock_state = {}
_lock_guard = threading.Lock()

def _state(ip):
    st = _lock_state.get(ip)
    if st is None:
        st = {"fails": 0, "until": 0.0, "stage": 1}
        _lock_state[ip] = st
    return st

def locked_for(ip):
    with _lock_guard:
        st = _state(ip)
        now = time.time()
        if st["until"] > now:
            return int(st["until"] - now) + 1
        if st["until"]:
            st["until"] = 0.0
        return 0

def record_failure(ip):
    with _lock_guard:
        st = _state(ip)
        st["fails"] += 1
        if st["stage"] == 1 and st["fails"] >= STAGE1_FAILS:
            st["until"] = time.time() + STAGE1_LOCK
            st["fails"] = 0
            st["stage"] = 2
            log("ip=%s locked %ds (stage1)" % (ip, STAGE1_LOCK))
        elif st["stage"] == 2 and st["fails"] >= STAGE2_FAILS:
            st["until"] = time.time() + STAGE2_LOCK
            st["fails"] = 0
            st["stage"] = 1
            log("ip=%s locked %ds (stage2)" % (ip, STAGE2_LOCK))

def record_success(ip):
    with _lock_guard:
        _lock_state[ip] = {"fails": 0, "until": 0.0, "stage": 1}

# --------------------------------------------------------------------------- #
# 登录页
# --------------------------------------------------------------------------- #

LOGIN_CSS = (
    "*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;"
    "align-items:center;justify-content:center;background:#0f141b;color:#e6edf3;"
    "font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}"
    ".card{width:min(92vw,380px);background:#161d27;border:1px solid #2a3342;"
    "border-radius:12px;padding:28px 26px}"
    "h1{font-size:19px;margin:0 0 4px}.sub{font-size:12px;color:#8b98a9;margin:0 0 18px}"
    "label{display:block;font-size:13px;color:#aeb9c7;margin:12px 0 6px}"
    "input[type=text],input[type=password]{width:100%;padding:10px 12px;font-size:15px;"
    "background:#0d1117;border:1px solid #2a3342;border-radius:8px;color:#e6edf3}"
    ".vanilla{margin-top:16px;background:#0d1117;border:1px solid #2a3342;"
    "border-radius:8px;padding:12px}"
    ".vanilla label{display:flex;gap:9px;align-items:flex-start;margin:0;cursor:pointer}"
    ".vanilla input{width:18px;height:18px;margin-top:1px;accent-color:#1f6feb}"
    ".vanilla b{font-size:14px}.vanilla span{display:block;font-size:12px;"
    "color:#8b98a9;margin-top:3px;line-height:1.5}"
    ".status{display:flex;gap:12px;font-size:12px;color:#8b98a9;margin-top:14px}"
    ".ok{color:#3fb950}.bad{color:#f85149}"
    "button{width:100%;margin-top:18px;padding:11px;font-size:15px;border:0;"
    "border-radius:8px;background:#1f6feb;color:#fff;cursor:pointer}"
    ".err{background:#3d1d1d;border:1px solid #7a2e2e;color:#ffb4b4;font-size:13px;"
    "border-radius:8px;padding:9px 12px;margin-bottom:6px}"
    ".row{display:flex;gap:10px}.row button{flex:1}"
    ".ghost{background:#2a3342}"
    ".cur{font-size:13px;background:#0d1117;border:1px solid #2a3342;"
    "border-radius:8px;padding:10px 12px;margin-bottom:6px}"
)

def _status_dot(ready):
    return ("<span class=\"ok\">\u25cf\u5c31\u7eea</span>" if ready
            else "<span class=\"bad\">\u25cf\u672a\u5c31\u7eea</span>")

def login_page(error=None, mode=MODE_FULL, authed_user=None):
    """渲染登录页。已登录时渲染模式切换卡（免密）。"""
    full_ready = bool(BACKENDS[MODE_FULL]["tokens"].peek())
    vanilla_ready = bool(BACKENDS[MODE_VANILLA]["tokens"].peek())
    status = ("<div class=\"status\"><span>\u5b8c\u6574\u7248 %s</span>"
              "<span>\u521d\u59cb\u7248 %s</span></div>"
              % (_status_dot(full_ready), _status_dot(vanilla_ready)))
    err = "<div class=\"err\">%s</div>" % error if error else ""
    if authed_user is not None:
        cur = ("<div class=\"cur\">\u5df2\u767b\u5f55\uff0c\u5f53\u524d\u6a21\u5f0f\uff1a"
               "<b>%s</b></div>"
               % ("初始版（仅官方插件）" if mode == MODE_VANILLA else "完整版（含第三方插件）"))
        body = (
            "<h1>dsh \u767b\u5f55</h1>"
            "<p class=\"sub\">\u5207\u6362\u7248\u672c后跳回首页</p>"
            "%s%s"
            "<form method=\"post\" action=\"/__login\">"
            "<div class=\"vanilla\"><label><input type=\"checkbox\" name=\"vanilla\" value=\"on\"%s>"
            "<span><b>\u521d\u59cb\u7248\uff08\u4ec5\u5b98\u65b9\u81ea\u5e26\u63d2\u4ef6\uff09</b>"
            "<span>\u63d2\u4ef6\u628a\u5b8c\u6574\u7248\u5f04\u574f\u65f6\u52fe\u9009\u6b64\u9879\u6062\u590d\u8bbf\u95ee</span>"
            "</span></label></div>"
            "<div class=\"row\"><button type=\"submit\" name=\"action\" value=\"switch\">"
            "\u4fdd\u5b58\u5e76\u8fdb\u5165</button>"
            "<button class=\"ghost\" type=\"submit\" name=\"action\" value=\"logout\">"
            "\u9000\u51fa\u767b\u5f55</button></div>"
            "</form>%s" % (cur, err,
                           " checked" if mode == MODE_VANILLA else "", status))
    else:
        body = (
            "<h1>dsh \u767b\u5f55</h1>"
            "<p class=\"sub\">\u8bf7\u8f93\u5165\u8d26\u53f7\u5bc6\u7801</p>"
            "%s"
            "<form method=\"post\" action=\"/__login\">"
            "<label>\u8d26\u53f7</label>"
            "<input type=\"text\" name=\"user\" autocomplete=\"username\">"
            "<label>\u5bc6\u7801</label>"
            "<input type=\"password\" name=\"pass\" autocomplete=\"current-password\">"
            "<div class=\"vanilla\"><label><input type=\"checkbox\" name=\"vanilla\" value=\"on\">"
            "<span><b>\u521d\u59cb\u7248\uff08\u4ec5\u5b98\u65b9\u81ea\u5e26\u63d2\u4ef6\uff09</b>"
            "<span>\u9ed8\u8ba4\u5173\u95ed\uff1b\u63d2\u4ef6\u628a\u5b8c\u6574\u7248\u5f04\u574f\u65f6"
            "\u52fe\u9009\u6b64\u9879\u4fdd\u8bc1\u80fd\u6b63\u5e38\u8fd0\u884c</span>"
            "</span></label></div>"
            "<button type=\"submit\" name=\"action\" value=\"login\">\u767b\u5f55</button>"
            "</form>%s" % (err, status))
    return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>dsh \u767b\u5f55</title><style>" + LOGIN_CSS + "</style></head>"
            "<body><div class=\"card\">" + body + "</div></body></html>")

# --------------------------------------------------------------------------- #
# HTTP / WebSocket 处理
# --------------------------------------------------------------------------- #

def _split_path(path):
    q = path.find("?")
    if q == -1:
        return path, ""
    return path[:q], path[q + 1:]

def _safe_next(query):
    try:
        params = dict(parse_qsl(query, keep_blank_values=True))
    except Exception:
        return "/"
    nxt = params.get("next", "/")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"

class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-public-gateway/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # --- 通用工具 ---

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def session_of_request(self):
        """读会话 cookie；返回 (user, mode)，无效返回 (None, None)。"""
        header = self.headers.get("Cookie", "")
        for segment in header.split(";"):
            name, sep, value = segment.partition("=")
            if sep and name.strip() == SESSION_COOKIE:
                return verify_session(value.strip())
        return None, None

    def basic_of_request(self):
        """兼容旧 Basic 客户端：有效则视为完整版模式。"""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return None
        if ":" not in raw:
            return None
        user, _, pwd = raw.partition(":")
        if user == USERNAME and pwd == PASSWORD:
            return user
        return None

    def send_simple(self, code, text, extra_headers=None):
        body = text.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for k, v in extra_headers:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except Exception:
            pass

    def send_html(self, code, html, extra_headers=None):
        body = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for k, v in extra_headers:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except Exception:
            pass

    def send_redirect(self, location, cookie_headers=None):
        try:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            for header in cookie_headers or []:
                self.send_header("Set-Cookie", header)
            self.end_headers()
        except Exception:
            pass

    def gate(self):
        """鉴权：会话 cookie 优先，旧 Basic 头兼容；返回 (user, mode) 或 None。"""
        user, mode = self.session_of_request()
        if user is not None:
            return user, mode
        basic_user = self.basic_of_request()
        if basic_user is not None:
            record_success(self.client_ip())
            return basic_user, MODE_FULL
        return None

    def gate_or_login(self):
        """鉴权失败时：浏览器导航跳登录页，其它请求回 401。"""
        auth = self.gate()
        if auth is not None:
            return auth
        accept = self.headers.get("Accept", "")
        if self.command == "GET" and "text/html" in accept:
            self.send_redirect("/__login?next=" + quote(self.path, safe=""))
        else:
            self.send_simple(401, "Authentication required; open the login page in a browser.\n")
        return None

    def read_form(self, limit=16384):
        """读并解析 urlencoded 表单；超限或类型不符返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > limit:
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/x-www-form-urlencoded":
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8", "ignore")
            return dict(parse_qsl(raw, keep_blank_values=True))
        except Exception:
            return None

    # --- 方法路由 ---

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.handle_websocket()
        else:
            self.handle_request()

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_OPTIONS = do_GET

    # --- 登录 / 模式切换 ---

    def handle_session_routes(self, route_path, query):
        """处理 /__login 与 /__logout；返回 True 表示已接管响应。"""
        if route_path == "/__logout":
            if self.command == "POST" or self.command == "GET":
                self.send_redirect("/__login", [clear_session_cookie_header()])
                return True
            return False
        if route_path != "/__login":
            return False
        if self.command == "GET":
            user, mode = self.session_of_request()
            if user is None and self.basic_of_request() is not None:
                user, mode = USERNAME, MODE_FULL
            self.send_html(200, login_page(authed_user=user,
                                           mode=mode or MODE_FULL))
            return True
        if self.command != "POST":
            return False
        form = self.read_form()
        if form is None:
            self.send_html(400, login_page(error="表单无效，请重试。"))
            return True
        action = form.get("action", "login")
        want_mode = MODE_VANILLA if form.get("vanilla") == "on" else MODE_FULL
        ip = self.client_ip()
        ua = (self.headers.get("User-Agent", "") or "").replace("\r", " ").replace("\n", " ")[:120]
        # 已登录会话可免密切换模式 / 退出。
        user, _ = self.session_of_request()
        if user is not None and action in ("switch", "logout"):
            if action == "logout":
                record_success(ip)
                self.send_redirect("/__login", [clear_session_cookie_header()])
                return True
            cookie = session_cookie_header(mint_session(user, want_mode),
                                           SESSION_MAX_AGE_SEC)
            self.send_redirect(_safe_next(query), [cookie])
            return True
        # 密码登录。
        remain = locked_for(ip)
        if remain > 0:
            self.send_html(429, login_page(
                error="尝试次数过多，请 %d 秒后重试。" % remain))
            return True
        if form.get("user") == USERNAME and form.get("pass") == PASSWORD:
            record_success(ip)
            cookie = session_cookie_header(mint_session(USERNAME, want_mode),
                                           SESSION_MAX_AGE_SEC)
            log("ip=%s login ok mode=%s ua=%s" % (ip, want_mode, ua))
            self.send_redirect(_safe_next(query), [cookie])
            return True
        record_failure(ip)
        self.send_html(200, login_page(error="账号或密码不正确。"))
        return True

    # --- WebSocket 透传 ---

    def handle_websocket(self):
        auth = self.gate()
        if auth is None:
            self.send_simple(401, "Authentication required.\n")
            return
        backend = BACKENDS[auth[1]]
        token = backend["tokens"].get()
        path = self.path
        cookie = self.headers.get("Cookie", "")
        if token and "dsh-auth-" not in cookie:
            path = _with_token(path, token)

        status, head, upstream = self._ws_attempt(backend, path, cookie)
        healed = []
        if status != 101 and token is not None:
            # 浏览器 dsh cookie 失效时 mux 升级永远 401（升级路径不认
            # ?token=），而 API 的 token 重试同样换不到新 cookie；只有首页
            # 兑换能换到。用兑换来的新 cookie 重试一次，对浏览器透明。
            self._close_quietly(upstream)
            healed = self._exchange_dsh_cookies(backend, token)
            if healed:
                status, head, upstream = self._ws_attempt(
                    backend, path, _merge_cookie(cookie, healed))
        if status != 101 or upstream is None:
            log("ws handshake fail [%s]: backend status %s"
                % (backend["label"], status))
            if head and upstream is not None:
                try:
                    self.connection.sendall(head)
                except Exception:
                    pass
            self._close_quietly(upstream)
            self.close_connection = True
            return

        if healed:
            # 顺手把新 cookie 递给浏览器（有的浏览器收下，有的忽略；
            # 忽略也不影响：下次重连服务端会再次透明治愈）。
            head = _inject_set_cookie(head, healed)
        down = self.connection
        try:
            down.setblocking(True)
        except Exception:
            pass
        try:
            self._pump(upstream, down, first_to_client=head)
        except Exception as exc:
            log("ws pump error [%s]: %s" % (backend["label"], exc))
        finally:
            self._close_quietly(upstream)
            self._close_quietly(down)
        self.close_connection = True

    @staticmethod
    def _close_quietly(sock):
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def _ws_attempt(self, backend, path, cookie):
        """发一次 WS 升级并读回响应头；返回 (status, head, sock)。
        status 为 0 表示连接/发送/读头失败（sock 已关）。读完头后 sock
        置回阻塞模式，空闲长连接不会被读超时掐断。"""
        try:
            upstream = socket.create_connection(
                (backend["host"], backend["port"]), timeout=BACKEND_CONNECT_TIMEOUT)
        except Exception as exc:
            log("ws connect fail [%s]: %s" % (backend["label"], exc))
            return 0, b"", None
        lines = ["GET %s HTTP/1.1" % path]
        for k, v in self.headers.items():
            if k.lower() in ("host", "origin", "referer", "cookie"):
                continue
            lines.append("%s: %s" % (k, v))
        lines.append("Host: %s:%d" % (backend["host"], backend["port"]))
        lines.append("Cookie: %s" % cookie)
        lines.append("X-Forwarded-For: %s" % self.client_ip())
        lines.append("Origin: http://%s:%d" % (backend["host"], backend["port"]))
        try:
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        except Exception as exc:
            log("ws send fail [%s]: %s" % (backend["label"], exc))
            self._close_quietly(upstream)
            return 0, b"", None
        try:
            upstream.settimeout(BACKEND_CONNECT_TIMEOUT)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = upstream.recv(16384)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
        except Exception as exc:
            log("ws head fail [%s]: %s" % (backend["label"], exc))
            self._close_quietly(upstream)
            return 0, b"", None
        if b"\r\n\r\n" not in buf:
            self._close_quietly(upstream)
            return 0, b"", None
        try:
            status = int(buf.split(b"\r\n", 1)[0].split(b" ")[1])
        except Exception:
            self._close_quietly(upstream)
            return 0, b"", None
        try:
            upstream.settimeout(None)
        except Exception:
            pass
        return status, buf, upstream

    def _exchange_dsh_cookies(self, backend, token):
        """用首页 ?token= 兑换一套新鲜 dsh-auth cookie；返回 Set-Cookie
        头值列表（只要 dsh-auth-*）。状态码不限，cookie 照收。"""
        conn = http.client.HTTPConnection(
            backend["host"], backend["port"], timeout=BACKEND_CONNECT_TIMEOUT)
        try:
            conn.request("GET", _with_token("/", token), headers={
                "Host": "%s:%d" % (backend["host"], backend["port"]),
                "Connection": "close",
            })
            resp = conn.getresponse()
            resp.read()
            out = []
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie" and v.strip().startswith("dsh-auth-"):
                    out.append(v.strip())
            return out
        except Exception as exc:
            log("ws cookie exchange fail [%s]: %s" % (backend["label"], exc))
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _pump(self, upstream, down, first_to_client=b""):
        if first_to_client:
            try:
                down.sendall(first_to_client)
            except Exception:
                return
        socks = [upstream, down]
        while True:
            readable, _, errored = select.select(socks, [], socks, 60)
            if errored:
                return
            if not readable:
                continue
            for s in readable:
                try:
                    data = s.recv(65536)
                except Exception:
                    return
                if not data:
                    return
                other = down if s is upstream else upstream
                try:
                    other.sendall(data)
                except Exception:
                    return

    # --- HTTP 反代 ---

    def handle_request(self):
        pure_path, query = _split_path(self.path)
        if pure_path == "/__login" or pure_path == "/__logout":
            if self.handle_session_routes(pure_path, query):
                return
        auth = self.gate_or_login()
        if auth is None:
            return
        backend = BACKENDS[auth[1]]
        # REQLOG removed (debug-only, log grew unbounded)
        token = backend["tokens"].get()
        if not token:
            self.send_simple(503, "%s backend not ready (no token yet).\n"
                             % backend["label"])
            return

        path = self.path
        cookie = self.headers.get("Cookie", "")
        body = self.read_body()

        if "dsh-auth-" not in cookie:
            self.forward(_with_token(path, token), body, backend)
            return

        status, headers, data = self.fetch(path, body, backend)
        if status == 401:
            # cookie 失效：重新注入 token 以刷新
            status, headers, data = self.fetch(_with_token(path, token), body,
                                               backend)
        self.emit(status, headers, data, backend)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        return self.rfile.read(length) if length > 0 else None

    def fetch(self, path, body, backend):
        headers = {}
        for k, v in self.headers.items():
            # accept-encoding 必须去掉：后端回 gzip 时 _inject_html 看不到明文，
            # ownsHost 注入被静默跳过，非 loopback 页会降级为 memory 设置。
            # 回环链路压缩没有收益，直接要明文。
            if k.lower() in HOP_BY_HOP or k.lower() in ("host", "origin", "referer",
                                                       "accept-encoding"):
                continue
            headers[k] = v
        # dsh-market self-restart only accepts a *direct* same-origin loopback
        # request: the peer address must be loopback AND no forwarding header
        # may be present. Strip every forwarding trace for those routes.
        _p = (path or "").split("?")[0]
        _raw_restart = _p in ("/dsh-market/restart", "/dsh-market/api/v1/restart")
        for _k in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
                   "x-real-ip", "forwarded", "x-forwarded-port"):
            headers.pop(_k, None)
            for _hk in list(headers.keys()):
                if _hk.lower() == _k:
                    headers.pop(_hk, None)
        headers["Host"] = "%s:%d" % (backend["host"], backend["port"])
        if not _raw_restart:
            headers["X-Forwarded-For"] = self.client_ip()
            headers["X-Forwarded-Proto"] = "http"
        headers["Origin"] = "http://%s:%d" % (backend["host"], backend["port"])
        conn = http.client.HTTPConnection(
            backend["host"], backend["port"], timeout=BACKEND_READ_TIMEOUT)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.getheaders(), data
        except Exception as exc:
            log("proxy error [%s]: %s" % (backend["label"], exc))
            msg = "Bad gateway: %s\n" % exc
            return 502, [("Content-Type", "text/plain; charset=utf-8")], msg.encode("utf-8")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def emit(self, status, headers, data, backend):
        try:
            data = self._inject_html(status, headers, data, backend)
            ctype2 = ""
            for _k2, _v2 in headers:
                if _k2.lower() == "content-type":
                    ctype2 = _v2.lower()
                    break
            if "text/html" in ctype2:
                headers = self._no_store(headers)

            self.send_response(status)
            for k, v in headers:
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-length":
                    continue
                if lk == "location" and v.startswith("/"):
                    v = "http://%s%s" % (self.headers.get("Host"), v)
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception:
            pass

    def forward(self, path, body, backend):
        status, headers, data = self.fetch(path, body, backend)
        self.emit(status, headers, data, backend)

    def _no_store(self, headers):
        """NOSTORE-PATCH: HTML 必须不被缓存，否则旧版未注入页面会一直复用。"""
        out = []
        for hk, hv in headers:
            if hk.lower() == "cache-control":
                continue
            out.append((hk, hv))
        out.append(("Cache-Control", "no-store, no-cache, must-revalidate"))
        out.append(("Pragma", "no-cache"))
        return out

    def _inject_html(self, status, headers, data, backend):
        if status != 200 or not data:
            return data
        ctype = ""
        for _hk, _hv in headers:
            if _hk.lower() == "content-type":
                ctype = _hv.lower()
                break
        if "text/html" not in ctype:
            return data
        try:
            body_text = data.decode("utf-8", "ignore")
            low = body_text.lower()
            pos = low.find("<head")
            if pos == -1:
                return data
            end = low.find(">", pos)
            if end == -1:
                return data
            extra = INJECT_JS_VANILLA if backend["title_prefix"] else ""
            body_text = (body_text[:end + 1] + INJECT_JS + extra
                         + body_text[end + 1:])
            return body_text.encode("utf-8")
        except Exception:
            return data

def _with_token(path, token):
    sep = "&" if "?" in path else "?"
    return "%s%stoken=%s" % (path, sep, token)

def _merge_cookie(cookie, set_cookie_values):
    """把兑换来的 Set-Cookie 按名并入 Cookie 头，同名替换、缺失追加。"""
    pairs = []
    for header in set_cookie_values:
        name, sep, _ = header.partition(";")[0].partition("=")
        if sep and name.strip():
            pairs.append((name.strip(), header.partition(";")[0].strip()))
    if not pairs:
        return cookie
    kept = []
    for segment in (cookie or "").split(";"):
        name, sep, _ = segment.partition("=")
        if sep and name.strip() in dict(pairs):
            continue
        if segment.strip():
            kept.append(segment.strip())
    for _, pair in pairs:
        kept.append(pair)
    return "; ".join(kept)

def _inject_set_cookie(head, set_cookie_values):
    """往 101 响应头里追加 Set-Cookie 行；失败返回原头。"""
    try:
        sep = head.index(b"\r\n\r\n")
        extra = b"".join(
            b"Set-Cookie: " + v.encode("latin-1") + b"\r\n"
            for v in set_cookie_values)
        return head[:sep] + b"\r\n" + extra + head[sep:]
    except Exception:
        return head

# --------------------------------------------------------------------------- #
# 服务器
# --------------------------------------------------------------------------- #

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ----------------------------------------------------------- #
# Native addon self-check (missing system.node breaks new session /
# message send). Detect + log only; never blocks startup.
# ----------------------------------------------------------- #

NATIVE_ADDON = "/home/opc/deepseek-harness/native/system/packages/linux-arm64/bin/glibc/system.node"


def check_native_addon():
    import os
    if os.path.exists(NATIVE_ADDON):
        return True
    log("WARN: native addon missing: %s" % NATIVE_ADDON)
    log("WARN: fix by running -> cd /home/opc/deepseek-harness/native/system && "
        "export PATH=/home/opc/.nvm/versions/node/v24.21.0/bin:$PATH && "
        "source /opt/rh/gcc-toolset-14/enable && pnpm build:native --host-addon-only")
    return False


def main():
    check_native_addon()
    if not PASSWORD:
        log("FATAL: gateway password not set (etc/gateway.conf 或 DSH_GW_PASS)")
        sys.exit(1)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("listening %s:%d -> full %s:%d + vanilla %s:%d user=%s"
        % (LISTEN_HOST, LISTEN_PORT, DSH_HOST, DSH_PORT,
           VANILLA_HOST, VANILLA_PORT, USERNAME))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
