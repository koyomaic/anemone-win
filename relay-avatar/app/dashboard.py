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

# ── 「用户与权限」页签（dws 用户维护）：样式/结构/脚本，纯字符串常量 ──

USERS_CSS = """
.tabs{max-width:860px;margin:0 auto 14px;display:flex;gap:8px}
.tb{padding:6px 16px;border-radius:8px;border:1px solid #d0d5dd;background:#fff;
color:#475467;font-size:14px;cursor:pointer}
.tb.on{background:#2563eb;border-color:#2563eb;color:#fff}
#view-users{max-width:860px;margin:0 auto}
#view-users h2{margin:20px 0 8px;font-size:15px;color:#344054}
#view-users table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;
overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
#view-users th,#view-users td{padding:7px 10px;text-align:left;
border-bottom:1px solid #f0f1f4;font-size:13px}
#view-users th{background:#f8fafc;color:#667085;font-weight:500}
#view-users td.n,#view-users th.n{text-align:right;font-variant-numeric:tabular-nums}
.dim{color:#98a2b3;font-size:12px}
.chip{display:inline-block;background:#f2f4f7;border-radius:8px;padding:1px 8px;
margin:2px 4px 2px 0;font-size:12px}
.bt{padding:3px 10px;border-radius:6px;border:0;font-size:12px;cursor:pointer;margin-right:6px}
.bt.ok{background:#12b76a;color:#fff}.bt.no{background:#f04438;color:#fff}
.frow{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.frow input,.frow select{padding:7px 10px;border-radius:8px;border:1px solid #d0d5dd;font-size:13px}
.frow input{flex:1;min-width:110px}
#view-users code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
"""

USERS_HTML = """
<div id="view-users" style="display:none">
<div id="u-pend"></div>
<div id="u-dyn"></div>
<h2>手动授权</h2>
<div class="frow">
<input id="g-uid" placeholder="userId（工号）或 openId" style="flex:2">
<input id="g-name" placeholder="姓名（备注用，可空）" style="flex:1">
<select id="g-tier"><option value="L2">L2 · 中转任务</option>
<option value="L3">L3 · 仅文件投递</option></select>
<input id="g-days" type="number" min="1" max="3650" placeholder="有效天数" style="width:110px;flex:none">
<button id="btn-grant" class="bt ok">授权</button>
</div>
<p class="dim">授权/撤销立即生效（策略热读，无需重启）；到期自动回落 guest（静默）。
静态名单（super / L2 白名单 / bot 黑名单）改 policy.toml 后重启生效，本页只读展示。</p>
<div id="u-static"></div>
<div id="u-rate"></div>
</div>
"""

USERS_JS = """
<script>
const TB_KEY="avatar_dash_tab";
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function fmtTs(t){if(!t)return"-";const d=new Date(t*1000);
return d.toLocaleDateString("zh",{month:"2-digit",day:"2-digit"})+" "+d.toLocaleTimeString("zh",{hour12:false})}
function showTab(v){
 document.getElementById("view-host").style.display=v==="host"?"":"none";
 document.getElementById("view-users").style.display=v==="users"?"":"none";
 document.getElementById("tb-host").className="tb"+(v==="host"?" on":"");
 document.getElementById("tb-users").className="tb"+(v==="users"?" on":"");
 try{localStorage.setItem(TB_KEY,v)}catch(e){}
 if(v==="users")loadUsers();
}
document.getElementById("tb-host").addEventListener("click",()=>showTab("host"));
document.getElementById("tb-users").addEventListener("click",()=>showTab("users"));
function uname(uid,d){
 const rec=(d.dynamic||{})[uid]||(d.pending||{})[uid];
 if(rec&&rec.name)return rec.name;
 const c=(d.contacts||{})[uid];
 return c?uid+"（"+c+"）":uid;
}
async function loadUsers(){
 try{
  const r=await fetch("/api/users");if(!r.ok)return;
  const d=await r.json();
  if(d.error){document.getElementById("u-pend").innerHTML='<p class="dim">policy 不可用</p>';return;}
  document.getElementById("g-days").placeholder="有效天数（默认 "+d.grant_valid_days+"）";
  const pk=Object.keys(d.pending||{});
  document.getElementById("u-pend").innerHTML='<h2>待审批申请</h2>'+(pk.length?
   '<table><tr><th>姓名</th><th>userId</th><th>头衔/理由</th><th>时间</th><th>操作</th></tr>'+
   pk.map(uid=>{const p=d.pending[uid];
    return '<tr><td>'+esc(p.name||"-")+'</td><td><code>'+esc(uid)+'</code></td>'+
    '<td class="dim">'+esc(p.title||"")+'</td><td class="dim">'+fmtTs(p.ts)+'</td>'+
    '<td><button class="bt ok" data-a="approve" data-u="'+esc(uid)+'">批准 L2</button>'+
    '<button class="bt ok" data-a="approve_l3" data-u="'+esc(uid)+'">批准 L3</button>'+
    '<button class="bt no" data-a="deny" data-u="'+esc(uid)+'">拒绝</button></td></tr>'}).join("")+"</table>"
   :'<p class="dim">无</p>');
  const dk=Object.keys(d.dynamic||{}),now=Date.now()/1000;
  document.getElementById("u-dyn").innerHTML='<h2>动态白名单</h2>'+(dk.length?
   '<table><tr><th>姓名</th><th>userId/openId</th><th>级别</th><th>授予人</th><th>到期</th><th>操作</th></tr>'+
   dk.map(uid=>{const w=d.dynamic[uid];
    const left=w.expires?Math.ceil((w.expires-now)/86400):null;
    return '<tr><td>'+esc(w.name||"-")+'</td><td><code>'+esc(uid)+'</code></td>'+
    '<td>'+esc(w.tier||"L2")+'</td><td class="dim">'+esc(w.by||"")+'</td>'+
    '<td class="dim">'+(w.expires?fmtTs(w.expires)+(left!==null?"（剩 "+left+" 天）":""):"永久")+'</td>'+
    '<td><button class="bt no" data-a="revoke" data-u="'+esc(uid)+'">撤销</button></td></tr>'}).join("")+"</table>"
   :'<p class="dim">无（自聊审批或本页手动授权后出现在这里）</p>');
  const chip=a=>(a||[]).map(x=>'<span class="chip"><code>'+esc(x)+'</code>'+
   ((d.contacts||{})[x]?" · "+esc(d.contacts[x]):"")+'</span>').join("")||'<span class="dim">-</span>';
  document.getElementById("u-static").innerHTML='<h2>静态名单（policy.toml · 只读）</h2><table>'+
   '<tr><td style="width:110px">super</td><td>'+chip(d.superadmin)+'</td></tr>'+
   '<tr><td>L1 管理员</td><td>'+chip(d.admins)+'</td></tr>'+
   '<tr><td>L2 白名单</td><td>'+chip(d.whitelist)+'</td></tr>'+
   '<tr><td>L3 文件投递</td><td>'+chip(d.file_uploaders)+'</td></tr>'+
   '<tr><td>用户黑名单</td><td>'+chip(d.blacklist)+'</td></tr>'+
   '<tr><td>bot 拦截</td><td>'+chip(d.bot_blocklist)+'</td></tr>'+
   '<tr><td>名单外 guest</td><td class="dim">'+esc(d.guest_mode)+'（silent=彻底静默，不回话）</td></tr></table>';
  const ck=Object.keys(d.today_counts||{});
  document.getElementById("u-rate").innerHTML='<h2>今日用量</h2>'+(ck.length?
   '<table><tr><th>userId</th><th>姓名</th><th class="n">今日任务</th><th class="n">限额</th></tr>'+
   ck.sort((a,b)=>d.today_counts[b]-d.today_counts[a]).map(uid=>
    '<tr><td><code>'+esc(uid)+'</code></td><td>'+esc(uname(uid,d))+'</td>'+
    '<td class="n">'+d.today_counts[uid]+'</td><td class="n">'+d.rate_limit+'</td></tr>').join("")+"</table>"
   :'<p class="dim">今日暂无任务（限额 '+d.rate_limit+'/人/日）</p>');
 }catch(e){}
}
async function userAct(a,uid){
 const r=await fetch("/api/users",{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify({action:a,uid:uid})});
 if(!r.ok){let m="";try{m=JSON.stringify(await r.json())}catch(e){}
  alert("操作失败 "+r.status+" "+m);return;}
 loadUsers();
}
document.getElementById("view-users").addEventListener("click",e=>{
 const b=e.target.closest("button.bt");if(!b)return;
 const a=b.dataset.a,u=b.dataset.u;
 if(a==="revoke"&&!confirm("撤销 "+u+" 的动态白名单？立即生效。"))return;
 if(a==="deny"&&!confirm("拒绝 "+u+" 的申请？"))return;
 userAct(a,u);
});
document.getElementById("btn-grant").addEventListener("click",()=>{
 const uid=document.getElementById("g-uid").value.trim();
 const name=document.getElementById("g-name").value.trim();
 const tier=document.getElementById("g-tier").value;
 const days=parseInt(document.getElementById("g-days").value||"0",10);
 if(!uid){alert("请填写 userId/openId");return;}
 if(!confirm("授权 "+uid+(name?"（"+name+"）":"")+" 为 "+tier+
   (days?"，有效 "+days+" 天":"（默认有效期）")+"？"))return;
 fetch("/api/users",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({action:"grant",uid:uid,name:name,tier:tier,days:days||null})})
  .then(async r=>{if(!r.ok){let m="";try{m=JSON.stringify(await r.json())}catch(e){}
    alert("授权失败 "+r.status+" "+m);return;}
   document.getElementById("g-uid").value="";document.getElementById("g-name").value="";
   document.getElementById("g-days").value="";loadUsers();});
});
setInterval(()=>{if(document.getElementById("view-host").style.display!=="none")location.reload()},30000);
(function(){let v="host";try{v=localStorage.getItem(TB_KEY)||"host"}catch(e){}
 if(v==="users")showTab("users");})();
</script>
"""


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
    def __init__(self, cfg, storage, audit=None, policy=None):
        self.cfg = cfg
        self.st = storage
        self.audit = audit
        self.policy = policy
        self.mon = storage.run / "monitor"
        self.port = int(cfg.get("monitor", "dashboard_port", 8791))
        self.token = str(cfg.env.get("AVATAR_DASHBOARD_TOKEN", "") or "")
        self.guard = LoginGuard(
            str(cfg.env.get("AVATAR_DASHBOARD_PASSWORD", "") or ""), audit)

    # ── 用户与权限（dws 用户维护）──

    def _users_body(self) -> dict:
        """花名册总览：静态（policy.toml 只读）+ 动态白名单/待审（可操作）+ 当日用量。"""
        p = self.policy
        if p is None:
            return {"error": "policy unavailable"}
        rate = {}
        try:
            rj = json.loads((self.st.run / "rate.json").read_text("utf-8"))
            if str(rj.get("date", "")) == time.strftime("%Y%m%d"):
                rate = {str(k): int(v)
                        for k, v in (rj.get("counts") or {}).items()}
        except Exception:
            pass
        contacts = {}
        try:
            contacts = json.loads(
                (self.st.run / "contacts.json").read_text("utf-8"))
        except Exception:
            pass
        return {
            "rate_limit": p.rate_limit,
            "grant_valid_days": p.grant_valid_days,
            "guest_mode": str(self.cfg.get("permissions", "guest_mode",
                                           "silent")),
            "today_counts": rate,
            "contacts": contacts,
            "superadmin": sorted(p.superadmin),
            "admins": sorted(p.admins),
            "whitelist": sorted(p.whitelist),
            "file_uploaders": sorted(p.file_uploaders),
            "blacklist": sorted(p.blacklist),
            "bot_blocklist": sorted(p.bot_blocklist),
            "dynamic": p.load_dynamic_wl(),
            "pending": p.load_pending(),
        }

    def _user_action(self, h, body: dict):
        """写操作（仅人类口令会话）：approve/approve_l3/deny/grant/revoke。

        动态白名单文件被 Policy 每次判定时热读，改完即生效，无需重启。
        静态名单（policy.toml）不在此写——toml 回写风险高，仍走配置文件。
        """
        p = self.policy
        if p is None:
            return h._send(b'{"error":"policy unavailable"}',
                           "application/json", 500)
        act = str(body.get("action", ""))
        uid = str(body.get("uid", "")).strip()[:64]
        if not uid or act not in ("approve", "approve_l3", "deny",
                                  "grant", "revoke"):
            return h._send(b'{"error":"bad action/uid"}',
                           "application/json", 400)
        if act in ("approve", "approve_l3"):
            info = p.load_pending().get(uid, {})
            name = str(body.get("name") or info.get("name") or "")[:40]
            tier = "L3" if act == "approve_l3" else "L2"
            p.grant(uid, name, tier=tier, by_name="dashboard")
            detail = f"grant {tier} name={name}"
        elif act == "deny":
            p.revoke_pending(uid)
            detail = "deny pending"
        elif act == "grant":
            name = str(body.get("name") or "").strip()[:40]
            tier = str(body.get("tier") or "L2")
            if tier not in ("L2", "L3"):
                return h._send(b'{"error":"tier must be L2|L3"}',
                               "application/json", 400)
            try:
                days = int(body.get("days") or p.grant_valid_days)
            except (TypeError, ValueError):
                days = p.grant_valid_days
            days = max(1, min(days, 3650))
            p.grant(uid, name, tier=tier, by_name="dashboard",
                    valid_days=days)
            detail = f"grant {tier} days={days} name={name}"
        else:  # revoke
            if not p.revoke_dynamic(uid):
                return h._send(b'{"error":"not in dynamic whitelist"}',
                               "application/json", 404)
            detail = "revoke dynamic"
        if self.audit:
            try:
                self.audit.log(f"dash_user_{act}", uid, detail)
            except Exception:
                pass
        return h._send(b'{"ok":true}', "application/json")

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
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
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
                ".i{padding:4px 0;font-size:14px}"
                + USERS_CSS + "</style>"
                f"<h1>🛰️ relay-avatar 看板<small>:{self.port}</small>"
                "<a href=/logout>退出</a></h1>"
                '<div class="tabs"><button id="tb-host" class="tb on">主机状态</button>'
                '<button id="tb-users" class="tb">用户与权限</button></div>'
                '<div id="view-host">'
                + (cards or "<p style='text-align:center;color:#9aa4b2'>暂无快照，等待采集…</p>")
                + '</div>'
                + USERS_HTML + USERS_JS)

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
                if path == "/api/users":
                    return self._send(
                        json.dumps(dash._users_body(), ensure_ascii=False,
                                   default=str).encode(),
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
                if path == "/api/users":
                    # 写操作只认人类口令会话；机访 token 仅可读（防脚本误改名单）
                    if not self._session_ok():
                        return self._send(b'{"error":"session required"}',
                                          "application/json", 403)
                    return dash._user_action(self, body)
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
