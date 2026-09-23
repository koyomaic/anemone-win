"""app/dashboard.py —— 监控看板（零依赖 http.server，:8791）。

认证双轨（参考 relay 8788 面板同款交互与防爆破）：
- 人：口令登录页（POST /login）→ 会话 cookie（avatar_dash，httponly+strict，12h）。
  防爆破：同 IP 滑动窗口 15 分钟内失败 5 次 → 锁定，429+Retry-After；
  口令比对 secrets.compare_digest；失败/锁定写审计。
- 机：X-Token（env AVATAR_DASHBOARD_TOKEN）——probe POST /report 与脚本 API 用。
- /health 开放（provision 探活）；未设口令时回落纯 token 模式（兼容旧部署）。
主进程内 daemon 线程运行（marker 关=服务停=看板停，provision 只在启用时探活）。
"""
from __future__ import annotations

import html
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ICON = {"ok": "🟢", "warn": "🟠", "down": "🔴", "critical": "🔴",
        "stale": "🟠", "bad_credentials": "🔴", "stalled": "🔴"}
ZOMBIE_H = 6

COOKIE = "avatar_dash"
DASH_TTL = 12 * 3600
LOGIN_MAX_FAILS = 5            # 窗口内同 IP 失败达此次数 → 锁定（防爆破底线）
LOGIN_LOCK_SEC = 15 * 60       # 锁定与滑动窗口时长：15 分钟

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>relay-avatar 监控看板</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font:15px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:36px 40px;width:340px}
h1{font-size:17px;margin:0 0 6px}p{font-size:13px;color:#94a3b8;margin:0 0 22px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #475569;
background:#0f172a;color:#e2e8f0;font-size:15px;margin-bottom:14px}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#2563eb;color:#fff;
font-size:15px;cursor:pointer}button:hover{background:#1d4ed8}
.err{color:#f87171;font-size:13px;margin-bottom:12px}
</style></head><body>
<form class="card" method="post" action="/login">
<h1>🛰️ relay-avatar 监控看板</h1><p>输入访问口令</p>
__ERR__
<input type="password" name="password" placeholder="访问口令" autofocus required>
<button type="submit">进入</button>
</form></body></html>"""

LOCK_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>尝试次数过多</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#0f172a;color:#e2e8f0;font:15px/1.6
system-ui,sans-serif"><div style="text-align:center">尝试次数过多，已临时锁定
<br>请约 __MIN__ 分钟后再试</div></body></html>"""


class LoginGuard:
    """口令校验 + 会话 + 防爆破（与 relay 面板同参数：5 次/15 分钟锁）。"""

    def __init__(self, password: str, audit=None):
        self.password = str(password or "")
        self.audit = audit
        self.sessions: dict[str, float] = {}
        self.fails: dict[str, list] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def locked(self, ip: str) -> int:
        """滑动窗口内失败达上限 → 返回剩余锁定秒数；0=未锁。"""
        now = time.time()
        ts = [t for t in self.fails.get(ip, ()) if now - t < LOGIN_LOCK_SEC]
        if ts:
            self.fails[ip] = ts
        else:
            self.fails.pop(ip, None)
        if len(ts) >= LOGIN_MAX_FAILS:
            return max(1, int(LOGIN_LOCK_SEC - (now - ts[0])) + 1)
        return 0

    def try_login(self, ip: str, password: str):
        """返回 (状态, 值)：('locked', 剩余秒) / ('fail', 累计次数) / ('ok', 会话token)。"""
        left = self.locked(ip)
        if left:
            self._audit("login_locked", f"ip={ip} retry={left}")
            return "locked", left
        if not secrets.compare_digest(str(password or ""), self.password):
            fails = self.fails.setdefault(ip, [])
            fails.append(time.time())
            self._audit("login_fail", f"ip={ip} n={len(fails)}")
            return "fail", len(fails)
        self.fails.pop(ip, None)
        tok = secrets.token_urlsafe(24)
        self.sessions[tok] = time.time() + DASH_TTL
        self._audit("login_ok", f"ip={ip}")
        return "ok", tok

    def check(self, tok: str) -> bool:
        if not tok:
            return False
        now = time.time()
        for k in [k for k, exp in self.sessions.items() if exp < now]:
            self.sessions.pop(k, None)
        exp = self.sessions.get(tok)
        return bool(exp and exp > now)

    def logout(self, tok: str):
        self.sessions.pop(tok, None)

    def _audit(self, action: str, detail: str):
        try:
            if self.audit:
                self.audit.log(action, "__dashboard__", detail)
        except Exception:
            pass


class Dashboard:
    def __init__(self, cfg, storage, audit=None):
        self.cfg = cfg
        self.mon = storage.run / "monitor"
        self.port = int(cfg.get("monitor", "dashboard_port", 8791))
        self.token = str(cfg.env.get("AVATAR_DASHBOARD_TOKEN", "") or "")
        self.guard = LoginGuard(
            str(cfg.env.get("AVATAR_DASHBOARD_PASSWORD", "") or ""), audit)

    def load_snaps(self) -> list:
        out = []
        for f in sorted(self.mon.glob("snapshot-*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        now = time.time()
        return [s for s in out if now - s.get("ts", 0) < ZOMBIE_H * 3600]

    def render(self) -> str:
        snaps = self.load_snaps()
        cards = ""
        for s in sorted(snaps, key=lambda x: x.get("host", "")):
            st = s.get("overall", "ok")
            rows = ""
            for svc in s.get("services", []):
                rows += (f'<div class="r"><span>{ICON.get(svc["status"], "⚪")}'
                         f'</span><b>{html.escape(str(svc.get("label", svc["name"])))}'
                         f'</b><i>{html.escape(str(svc.get("detail", "")))}</i></div>')
            iss = "".join(f'<div class="i">{"🔴" if i["level"] == "critical" else "🟠"} '
                          f'{html.escape(str(i.get("label", i["target"])))}：'
                          f'{html.escape(str(i["msg"]))}</div>'
                          for i in s.get("issues", []))
            r = s.get("resources", {})
            age = int((time.time() - s.get("ts", time.time())) / 60)
            cards += (f'<div class="c {st}"><h2>{html.escape(str(s.get("host", "?")))} '
                      f'{ICON.get(st, "🟢")} <small>{age}min 前</small></h2>'
                      f'<div class="res">磁盘 {r.get("disk_used_pct", "?")}% · '
                      f'内存 {r.get("mem_used_pct", "?")}%</div>'
                      f'{iss}{rows}</div>')
        return ("<!doctype html><meta charset=utf-8>"
                "<meta http-equiv=refresh content=30>"
                "<title>relay-avatar 看板</title><style>"
                "body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;"
                "background:#f4f6f9;margin:0;padding:20px}"
                ".c{background:#fff;border-radius:12px;padding:14px 18px;margin:0 auto 14px;"
                "max-width:860px;border-left:5px solid #30a46c;box-shadow:0 1px 3px rgba(0,0,0,.06)}"
                ".c.warn{border-left-color:#f5a623}.c.critical{border-left-color:#e5484d}"
                "h1{max-width:860px;margin:0 auto 16px;display:flex;align-items:baseline}"
                "h1 small{font-size:13px;color:#9aa4b2;margin-left:8px}"
                "h1 a{margin-left:auto;font-size:13px;color:#9aa4b2;text-decoration:none}"
                "h2{margin:0 0 6px;font-size:17px}h2 small{color:#9aa4b2;font-weight:400}"
                ".res{color:#6b7280;font-size:13px;margin-bottom:8px}"
                ".r{display:flex;gap:8px;padding:4px 0;border-top:1px solid #f0f1f4;font-size:14px}"
                ".r i{margin-left:auto;color:#9aa4b2;font-style:normal;font-family:monospace;font-size:12px}"
                ".i{padding:4px 0;font-size:14px}</style>"
                f"<h1>🛰️ relay-avatar 看板<small>:{self.port}</small>"
                "<a href=/logout>退出</a></h1>"
                + (cards or "<p style='text-align:center;color:#9aa4b2'>暂无快照，等待采集…</p>"))

    def _handler(self):
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            # ── 认证 ──
            def _cookie_tok(self) -> str:
                try:
                    c = SimpleCookie(self.headers.get("Cookie", ""))
                    return c[COOKIE].value if COOKIE in c else ""
                except Exception:
                    return ""

            def _token_ok(self) -> bool:
                if not dash.token:
                    return False
                got = self.headers.get("X-Token", "")
                if not got and "token=" in self.path:
                    got = self.path.split("token=", 1)[1].split("&")[0]
                return bool(got) and secrets.compare_digest(got, dash.token)

            def _session_ok(self) -> bool:
                return dash.guard.check(self._cookie_tok())

            def _authed(self) -> bool:
                return self._session_ok() or self._token_ok()

            # ── 基础响应 ──
            def _send(self, body: bytes, ctype: str, code: int = 200,
                      headers: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, loc: str, code: int = 303,
                          headers: dict | None = None):
                self.send_response(code)
                self.send_header("Location", loc)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _login_page(self, err: str = ""):
                page = LOGIN_HTML.replace("__ERR__", err)
                self._send(page.encode("utf-8"), "text/html; charset=utf-8")

            # ── 路由 ──
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/health":
                    return self._send(b'{"ok":true}', "application/json")
                if path == "/logout":
                    dash.guard.logout(self._cookie_tok())
                    return self._redirect("/")
                if path == "/login":
                    return self._redirect("/")
                if dash.guard.enabled and not self._authed():
                    q = urllib.parse.parse_qs(
                        self.path.split("?", 1)[1] if "?" in self.path else "")
                    e = (q.get("e") or [""])[0]
                    if e == "1":
                        return self._login_page(
                            '<div class="err">口令错误，请重试</div>')
                    if e == "2":
                        return self._login_page(
                            '<div class="err">尝试次数过多，请 15 分钟后再试</div>')
                    return self._login_page()
                if not dash.guard.enabled and dash.token and \
                        not self._token_ok():
                    return self._send(b'{"error":"bad token"}',
                                      "application/json", 403)
                if path == "/api/snapshots":
                    return self._send(
                        json.dumps(dash.load_snaps(),
                                   ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")
                self._send(dash.render().encode(), "text/html; charset=utf-8")

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                if path == "/login" and dash.guard.enabled:
                    return self._do_login()
                if not self._authed() and not (dash.token and self._token_ok()):
                    return self._send(b'{"error":"bad token"}',
                                      "application/json", 403)
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n).decode())
                except Exception:
                    return self._send(b'{"error":"bad body"}',
                                      "application/json", 400)
                if path == "/report":
                    host = re.sub(r"[^\w一-龥-]", "",
                                  str(body.get("host") or "unknown"))[:40]
                    dash.mon.mkdir(parents=True, exist_ok=True)
                    (dash.mon / f"snapshot-{host or 'unknown'}.json").write_text(
                        json.dumps(body, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                    return self._send(b'{"ok":true}', "application/json")
                self._send(b'{"error":"not found"}', "application/json", 404)

            def _do_login(self):
                ip = self.client_address[0] if self.client_address else "?"
                left = dash.guard.locked(ip)
                if left:
                    return self._send(
                        LOCK_HTML.replace("__MIN__", str(max(1, left // 60)))
                        .encode("utf-8"), "text/html; charset=utf-8", 429,
                        {"Retry-After": str(left)})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    form = urllib.parse.parse_qs(
                        self.rfile.read(n).decode("utf-8"))
                except Exception:
                    form = {}
                password = (form.get("password") or [""])[0]
                status, val = dash.guard.try_login(ip, password)
                if status == "locked":
                    return self._send(
                        LOCK_HTML.replace("__MIN__", str(max(1, val // 60)))
                        .encode("utf-8"), "text/html; charset=utf-8", 429,
                        {"Retry-After": str(val)})
                if status == "fail":
                    return self._redirect("/?e=1")
                self._redirect("/", 302, {"Set-Cookie": (
                    f"{COOKIE}={val}; Max-Age={DASH_TTL}; Path=/; "
                    "HttpOnly; SameSite=Strict")})

        return H

    def serve_thread(self) -> threading.Thread:
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        return t

    def _serve(self):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
            mode = "口令登录" if self.guard.enabled else "token"
            print(f"[dashboard] :{self.port} ({mode})", flush=True)
            srv.serve_forever()
        except Exception as e:
            print("[dashboard] 启动失败:", e, flush=True)
