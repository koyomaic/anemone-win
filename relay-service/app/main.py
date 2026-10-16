from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditLog
from . import shared_knowledge
from . import plans as plan_util
from .config import AUTO_DENY_REASONS, Settings
from .db import DB
from .events import TERMINAL, EventBus

# 执行结束即关闭 SSE 流（review 表示执行完成、待人工确认）
STREAM_END = TERMINAL | {"review"}

# ── 长任务分段续跑：段号编码进 resume_hint，不新增 DB 列 ──
# 为什么不加列：加列要迁移 schema（PG + SQLite 双后端各一套），而段号是**执行期状态**，
# 与 resume_hint 同生命周期（续跑时读、终态时清）。编码进文本即零迁移，且进程重启后
# 仍能还原长任务归类——进程内的 mark_long 标记会丢，hint 不会。
SEG_MARK = "[seg:"


def _segment_of(hint: str) -> int:
    """从 resume_hint 读回段号；无标记返回 0（＝首段／非续跑）。"""
    i = (hint or "").find(SEG_MARK)
    if i < 0:
        return 0
    j = hint.find("]", i)
    if j < 0:
        return 0
    try:
        return max(0, int(hint[i + len(SEG_MARK):j]))
    except ValueError:
        return 0


def _with_segment(hint: str, seg: int) -> str:
    """把段号写进 hint（先剥旧标记，防多次续跑叠加成一串）。"""
    i = (hint or "").find(SEG_MARK)
    if i >= 0:
        j = hint.find("]", i)
        if j >= 0:
            hint = (hint[:i] + hint[j + 1:]).strip()
    return f"{SEG_MARK}{seg}]\n{hint}".strip()


def _restart_interrupted(shutting_down: bool, timed_out: bool,
                         exit_code: int) -> bool:
    """这个任务是否被**计划内重启**打断（→ 应退回 queued，而不是判 failed）。

    三个条件缺一不可：
    - shutting_down：只有 lifespan 收尾置位时为真。硬杀（kill -9/OOM）走不到这里，
      那条路由 lifespan 开头的启动恢复逻辑接回（行留在 running → queued）。
    - not timed_out：超时也是 SIGTERM(143)，但那是任务自己的预算用尽，重新排队
      只会再超时一次，属于真失败。
    - signal_name(exit_code)：确实被信号打死。agent 自己报错退出（code 1 等）
      与重启无关，必须照常判 failed。

    抽成纯函数是为了可测：这段判定埋在 run_task 闭包深处，起真服务才能碰到。
    """
    return bool(shutting_down and not timed_out and signal_name(exit_code))


# 启动时往回看多久，甄别"上一轮进程因重启把任务误判成 failed"。窗口只需覆盖
# 「旧进程写 failed」到「新进程 lifespan 跑恢复」这几秒，给到 120s 是留足慢盘/
# 慢启动余量；太长会把无关的历史信号失败（如 agent 被 OOM 杀）也捞回来重跑。
RESTART_RECOVER_WINDOW_SEC = 120.0
_EXIT_CODE_RE = re.compile(r"agent exited with code (-?\d+)")


def _exit_code_of(err: str) -> int:
    """从 error 文案里抠回退出码；抠不到返回 0。

    只在**启动恢复**这条冷路径上用（热路径走 AgentResult.exit_code，不解析字符串）。
    超时错误是 "timeout after Ns"、不含本模式，故天然被排除——超时是预算用尽，
    重新排队只会再超时一次。
    """
    m = _EXIT_CODE_RE.search(err or "")
    return int(m.group(1)) if m else 0

DASH_COOKIE = "relay_dash"
DASH_TTL = 12 * 3600
MAX_FILE_BYTES = 10 * 1024 * 1024     # 文件上传/下载通道单文件上限 10MB
MAX_SHARED_BYTES = 1 * 1024 * 1024    # 共享知识（skill 文件 / memory）单文件上限 1MB（注入全员上下文，保守）
LOGIN_MAX_FAILS = 5               # dashboard 登录：窗口内同 IP 失败达此次数 → 锁定（防爆破底线）
LOGIN_LOCK_SEC = 15 * 60          # 锁定与滑动窗口时长：15 分钟
ACTIVATE_MAX_FAILS = 10           # /auth/activate：窗口内同 IP 无效码达此次数 → 429（防御纵深）
ACTIVATE_LOCK_SEC = 15 * 60

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>团队中转服务 · 登录</title>
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
<h1>团队中转服务</h1><p>输入访问口令</p>
__ERR__
<input type="password" name="password" placeholder="访问口令" autofocus required>
<button type="submit">进入</button>
</form></body></html>"""


DASH_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>团队中转服务 · 调用情况</title>
<style>
body{margin:0;background:#0f172a;color:#e2e8f0;font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
h1{font-size:18px;margin:0}
sub{font-size:12px;color:#64748b;font-weight:400}
a{color:#60a5fa;text-decoration:none;font-size:13px}
nav{margin-left:20px}nav a{margin-right:18px;font-size:14px;color:#94a3b8}
nav a.on{color:#e2e8f0;border-bottom:2px solid #60a5fa;padding-bottom:3px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
input{padding:8px 10px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:14px}
button{padding:8px 16px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
a.act{margin-right:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 14px}
.card .k{font-size:12px;color:#94a3b8}.card .v{font-size:22px;font-weight:600;margin-top:2px}
.card .s{font-size:12px;color:#64748b}
h2{font-size:14px;color:#94a3b8;margin:22px 0 8px}
table{width:100%;border-collapse:collapse;background:#1e293b;border:1px solid #334155;border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #293548;font-size:13px}
th{color:#94a3b8;font-weight:500;background:#16213a}
tr:last-child td{border-bottom:0}
td.desc{max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.b{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.done{background:#14532d;color:#4ade80}.review{background:#1e3a8a;color:#93c5fd}
.running{background:#164e63;color:#67e8f9}.queued{background:#334155;color:#cbd5e1}
.failed{background:#7f1d1d;color:#fca5a5}.conflict{background:#78350f;color:#fcd34d}
.cancelled{background:#334155;color:#94a3b8}.pending_approval{background:#713f12;color:#fde047}
.local{background:#3b0764;color:#d8b4fe}
.num{text-align:right;font-variant-numeric:tabular-nums}
h2.sec{display:flex;align-items:baseline}
.rngs{margin-left:auto;display:flex;gap:6px}
.rng{padding:2px 10px;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-size:12px;cursor:pointer}
.rng.on{background:#2563eb;border-color:#2563eb;color:#fff}
.sub2{font-size:12px;color:#64748b;font-weight:400;margin-left:10px}
select{padding:6px 10px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:13px}
#modal{position:fixed;inset:0;background:rgba(2,6,23,.72);align-items:center;justify-content:center;z-index:50}
#modal-box{background:#1e293b;border:1px solid #334155;border-radius:12px;max-width:880px;width:92%;max-height:84vh;display:flex;flex-direction:column}
#modal-head{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #293548;font-size:14px}
#modal-body{padding:14px 16px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:12px;margin:0;color:#cbd5e1}
tr.trow{cursor:pointer}
tr.trow:hover td{background:#22304a}
</style></head><body><div class="wrap">
<header><h1>团队中转服务 <sub id="upd"></sub></h1>
<nav><a href="#" id="tab-metrics" class="on">调用情况</a><a href="#" id="tab-tokens">凭证管理</a><a href="#" id="tab-shared">共享知识</a><a href="#" id="tab-plans">计划</a></nav>
<a href="/logout">退出</a></header>
<div id="view-metrics">
<div class="cards" id="cards"></div>
<h2 class="sec">任务趋势<span class="rngs"><button id="rng7" class="rng on">7 天</button><button id="rng30" class="rng">30 天</button></span></h2>
<div id="trend"></div>
<h2>各状态任务</h2><div id="dist" style="font-size:13px"></div>
<h2 class="sec">用户运营<span id="urange" class="sub2"></span></h2><div id="users"></div>
<h2>最近任务</h2>
<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
<select id="fstatus"><option value="">全部状态</option></select>
<select id="fuser"><option value="">全部用户</option></select>
<span style="font-size:12px;color:#64748b;align-self:center">点击行查看详情</span>
</div>
<div id="tasks"></div>
</div>
<div id="view-tokens" style="display:none">
<h2>新建凭证</h2>
<div style="display:flex;gap:8px;margin-bottom:14px">
<input id="newname" placeholder="成员姓名，如：王洪彬" style="flex:1">
<button id="btn-new">创建</button>
</div>
<div id="newtok" style="display:none;background:#14532d;border:1px solid #166534;border-radius:10px;padding:14px;margin-bottom:16px"></div>
<h2>现有凭证</h2><div id="toks"></div>
<p style="font-size:12px;color:#64748b;margin-top:14px">token 是成员的<b>一次性激活码</b>：本人首次使用即核销并绑定设备，此后请求走设备签名，泄露的已核销 token 无法冒用；「未核销」＝还没人激活，请通过私聊发给本人。重置＝换发新激活码（旧码立即失效，已激活设备不受影响）；删除后该成员不能再提交任务，历史任务记录保留。</p>
</div>
<div id="view-shared" style="display:none">
<p style="font-size:13px;color:#94a3b8">共享知识<b>取消隔离</b>：任何人上传即团队全员可用，<b>下一个任务自动加载、立即生效</b>（无需重启服务）。
技能→agent 技能列表；MCP→工具服务器；记忆→注入每次会话的 AGENTS.md。</p>
<h2>技能（Skills）</h2><div id="sh-skills"></div>
<div style="display:flex;gap:8px;margin:10px 0 18px;flex-wrap:wrap">
<input id="sh-skill-name" placeholder="技能名，如 my-tool" style="flex:1;min-width:140px">
<input id="sh-skill-file" placeholder="skill 内文件路径（默认 SKILL.md，可传 assets/xx）" style="flex:2;min-width:200px">
<input type="file" id="sh-skill-f">
<button id="btn-sh-skill">上传</button>
</div>
<h2>MCP 服务器</h2><div id="sh-mcp"></div>
<div style="display:flex;gap:8px;margin:10px 0 6px">
<input id="sh-mcp-name" placeholder="服务器名，如 ping" style="flex:1;min-width:140px">
<button id="btn-sh-mcp">上传</button>
</div>
<textarea id="sh-mcp-json" rows="3" placeholder='opencode mcp 条目 JSON，如 {"type":"local","command":["python3","/path/server.py"],"enabled":true} 或 {"type":"remote","url":"http://..."}' style="width:100%;box-sizing:border-box;margin-bottom:18px;font-family:ui-monospace,Menlo,monospace;font-size:12px"></textarea>
<h2>团队记忆（Memory）</h2><div id="sh-memory"></div>
<div style="display:flex;gap:8px;margin:10px 0 6px">
<input id="sh-mem-name" placeholder="记忆名，如 project-conventions" style="flex:1;min-width:160px">
<button id="btn-sh-mem">上传</button>
</div>
<textarea id="sh-mem-text" rows="4" placeholder="Markdown 正文（会原样注入每个成员 agent 的 AGENTS.md）" style="width:100%;box-sizing:border-box;margin-bottom:18px"></textarea>
<div id="sh-view" style="display:none"><h2 id="sh-view-title"></h2>
<pre id="sh-view-body" style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px;white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:420px;overflow:auto"></pre>
<a href="#" id="sh-view-close">关闭预览</a></div>
</div>
<div id="view-plans" style="display:none">
<p style="font-size:13px;color:#94a3b8">计划＝定时触发的 relay 任务。root 侧 <code>relay-plan.timer</code> 每分钟扫库，到点自动提交任务（大脑/成员也可经 <code>POST /plans</code> 创建）。表达式用 systemd OnCalendar：<code>*-*-* 08:00:00</code>（每天8点）、<code>Mon *-*-* 09:30:00</code>（每周一9:30）、<code>*:0/30:00</code>（每半小时）。</p>
<h2>新建计划</h2>
<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
<input id="pl-name" placeholder="计划名，如 daily-brief" style="flex:1;min-width:160px">
<input id="pl-cal" placeholder="OnCalendar，如 *-*-* 08:00:00" style="flex:2;min-width:220px">
</div>
<textarea id="pl-desc" rows="2" placeholder="到点要执行的任务描述（提交给 relay 跑）" style="width:100%;box-sizing:border-box;margin-bottom:8px"></textarea>
<div style="display:flex;gap:14px;margin-bottom:14px;align-items:center">
<label style="font-size:13px;color:#94a3b8"><input type="checkbox" id="pl-urgent"> 加急</label>
<label style="font-size:13px;color:#94a3b8"><input type="checkbox" id="pl-ro"> 只读</label>
<button id="btn-pl-new">创建</button>
</div>
<h2>现有计划</h2><div id="plans"></div>
<h2 style="margin-top:22px">系统定时器（只读）</h2>
<p style="font-size:12px;color:#94a3b8">本机 systemd 定时器一览，仅供查看，不在此启停。</p>
<div id="timers"></div>
</div>
</div>
<div id="modal" style="display:none"><div id="modal-box">
<div id="modal-head"><b id="modal-title"></b><a href="#" id="modal-close">关闭</a></div>
<pre id="modal-body"></pre></div></div>
<script>
const ST={done:"已完成",review:"待检查",running:"执行中",queued:"排队中",failed:"失败",
conflict:"冲突",cancelled:"已取消",pending_approval:"待审批"};
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function badge(s){return `<span class="b ${esc(s)}">${ST[s]||esc(s)}</span>`}
function fmtT(t){if(!t)return"-";const d=new Date(t*1000);
return d.toLocaleDateString("zh",{month:"2-digit",day:"2-digit"})+" "+d.toLocaleTimeString("zh",{hour12:false})}
let DAYS=7,LAST=null,TOKU=[];
const C_TREND={done:"#4ade80",failed:"#f87171",conflict:"#fcd34d",pending_approval:"#fde047",cancelled:"#64748b",other:"#60a5fa"};
const LIVE_ST=["queued","running","pending_approval","review"];
function fmtDur(t){
 const s=t.started_at||t.created_at;
 const live=LIVE_ST.includes(t.status);
 const e=t.finished_at||(live?Date.now()/1000:0);
 if(!s||!e)return"-";
 const d=Math.max(0,e-s);
 const str=d<60?Math.round(d)+"秒":d<3600?Math.floor(d/60)+"分"+Math.round(d%60)+"秒":Math.floor(d/3600)+"时"+Math.floor(d%3600/60)+"分";
 return live?str+"…":str;
}
function sparkSVG(days){
 if(!days.length)return"";
 const mx=Math.max(1,...days.map(d=>d.submitted||0)),w=days.length*7;
 return `<svg width="${w}" height="24" style="display:block;margin-top:6px">${days.map((d,i)=>{
  const h=Math.round((d.submitted||0)/mx*20);
  return `<rect x="${i*7}" y="${22-h}" width="5" height="${Math.max(h,(d.submitted||0)?2:0)}" rx="1" fill="${(d.failed||0)+(d.conflict||0)?"#f87171":"#4ade80"}" opacity=".85"><title>${d.day} 提交${d.submitted||0} 完成${d.done||0}</title></rect>`;}).join("")}</svg>`;
}
function trendSVG(days){
 if(!days.length)return'<p style="font-size:13px;color:#64748b">暂无数据</p>';
 const mx=Math.max(1,...days.map(d=>d.submitted||0));
 const bw=Math.max(12,Math.min(34,Math.floor(820/days.length))),gap=Math.max(3,Math.round(bw*0.25));
 const H=150,w=days.length*(bw+gap)+36;
 const keys=["done","failed","conflict","pending_approval","cancelled"];
 let bars="",labels="";
 const step=days.length>16?Math.ceil(days.length/8):1;
 days.forEach((d,i)=>{
  const x=30+i*(bw+gap);let y=H;
  const segs=[];let rest=d.submitted||0;
  keys.forEach(k=>{const v=d[k]||0;if(v>0){segs.push([k,v]);rest-=v;}});
  if(rest>0)segs.push(["other",rest]);
  segs.forEach(([k,v])=>{const h=Math.max(2,v/mx*H);y-=h;
   bars+=`<rect x="${x}" y="${y.toFixed(1)}" width="${bw}" height="${h.toFixed(1)}" fill="${C_TREND[k]||C_TREND.other}"><title>${d.day} ${ST[k]||"进行中/排队"}: ${v}（当日提交 ${d.submitted}）</title></rect>`;});
  if(i%step===0)labels+=`<text x="${x+bw/2}" y="${H+16}" fill="#64748b" font-size="10" text-anchor="middle">${d.day.slice(5)}</text>`;
 });
 const legend=[["done","完成"],["failed","失败"],["conflict","冲突"],["pending_approval","待审批"],["cancelled","取消"],["other","进行中/排队"]]
  .map(([k,t])=>`<span style="margin-right:14px"><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${C_TREND[k]};margin-right:4px"></i>${t}</span>`).join("");
 return `<div style="font-size:12px;color:#94a3b8;margin-bottom:6px">${legend}</div>`+
  `<svg width="100%" viewBox="0 0 ${w} ${H+24}" style="max-width:${w}px"><line x1="26" y1="${H}" x2="${w-4}" y2="${H}" stroke="#334155"/>${bars}${labels}</svg>`;
}
function renderCards(m,rep){
 const days=rep.by_day||[];
 const sub=days.reduce((a,d)=>a+(d.submitted||0),0);
 const dn=days.reduce((a,d)=>a+(d.done||0),0);
 const rate=sub?Math.round(dn/sub*100):0,td=m.today;
 document.getElementById("cards").innerHTML=[
  ["运行/排队",`${m.active} / ${m.queued}`,"上限 "+m.max_concurrent,""],
  ["今日任务",td.submitted,"完成率 "+Math.round((td.done_rate||0)*100)+"%",""],
  [`近 ${rep.days||DAYS} 天`,sub,`完成率 ${rate}%`,sparkSVG(days.slice(-14))],
  ["已运行",Math.floor(m.uptime_seconds/3600)+"h "+Math.floor(m.uptime_seconds%3600/60)+"m",
   esc(m.engine)+" · "+esc(String(m.version||"").slice(0,12)),""],
 ].map(([k,v,s,sp])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div>${sp}</div>`).join("");
}
function renderUsers(rep){
 const byName={};(rep.by_user||[]).forEach(u=>byName[u.user]=u);
 const names=[...new Set([...Object.keys(byName),...TOKU.map(u=>u.name)])];
 const now=Date.now()/1000;
 const rows=names.map(n=>{
  const s=byName[n]||{user:n,tasks:0,done:0,failed:0,tokens:0,done_rate:0,last_active:0};
  const t=TOKU.find(x=>x.name===n);
  let st,stc,ord;
  if(!t){st="—";stc="#64748b";ord=s.tasks>0?0:1}
  else if(!t.consumed_at){st="⏳ 未激活";stc="#fbbf24";ord=2}
  else if(s.tasks>0&&(now-(s.last_active||0))<7*86400){st="🔥 活跃";stc="#4ade80";ord=0}
  else{st="💤 沉默";stc="#94a3b8";ord=1}
  return{...s,st,stc,ord};
 });
 rows.sort((a,b)=>a.ord-b.ord||(b.last_active||0)-(a.last_active||0));
 document.getElementById("users").innerHTML=`<table><tr><th>用户</th><th>状态</th><th class="num">任务</th>
  <th class="num">完成</th><th class="num">完成率</th><th class="num">tokens</th><th>最近活跃</th></tr>`+
  (rows.map(u=>`<tr><td>${esc(u.user)}</td><td style="color:${u.stc};font-size:12px">${u.st}</td>
   <td class="num">${u.tasks}</td><td class="num">${u.done}</td>
   <td class="num">${Math.round((u.done_rate||0)*100)}%</td><td class="num">${u.tokens||0}</td>
   <td style="font-size:12px;color:#94a3b8">${u.last_active?fmtT(u.last_active):"-"}</td></tr>`).join("")||
   '<tr><td colspan="7" style="color:#64748b">暂无用户</td></tr>')+"</table>";
 const cnt=o=>rows.filter(r=>r.ord===o).length;
 document.getElementById("urange").textContent=
  `近 ${rep.days||DAYS} 天 · 🔥活跃 ${cnt(0)} · 💤沉默 ${cnt(1)} · ⏳未激活 ${cnt(2)}`;
}
function populateFilters(ts){
 const fs=document.getElementById("fstatus"),fu=document.getElementById("fuser");
 const curS=fs.value,curU=fu.value;
 const sts=[...new Set(ts.map(t=>t.status))];
 const usrs=[...new Set(ts.map(t=>t.user))].sort();
 fs.innerHTML='<option value="">全部状态</option>'+sts.map(s=>`<option value="${esc(s)}">${ST[s]||esc(s)}</option>`).join("");
 fu.innerHTML='<option value="">全部用户</option>'+usrs.map(u=>`<option value="${esc(u)}">${esc(u)}</option>`).join("");
 if(sts.includes(curS))fs.value=curS;
 if(usrs.includes(curU))fu.value=curU;
}
function renderTasks(){
 if(!LAST)return;
 const fs=document.getElementById("fstatus").value,fu=document.getElementById("fuser").value;
 let ts=LAST.tasks||[];
 if(fs)ts=ts.filter(t=>t.status===fs);
 if(fu)ts=ts.filter(t=>t.user===fu);
 document.getElementById("tasks").innerHTML=`<table><tr><th>#</th><th>用户</th><th>状态</th><th>备注</th>
   <th>任务</th><th>提交</th><th>耗时</th><th class="num">tokens</th><th>操作</th></tr>`+
  (ts.map(t=>{
    const blocked=t.status==="pending_approval";
    const bc=t.blocked_cmd||t.error||"";
    const act=blocked?`<a href="#" class="act" data-k="appr" data-n="${t.id}" title="${esc(bc)}" style="color:#4ade80">批准</a>
     <a href="#" class="act" data-k="deny" data-n="${t.id}" style="color:#f87171">拒绝</a>`
     :`<a href="#" class="act" data-k="det" data-n="${t.id}">详情</a>`;
    return `<tr class="trow" data-id="${t.id}"><td>${t.id}</td><td>${esc(t.user)}</td><td>${badge(t.status)}${t.engine==="local"?` <span class="b local" title="本机 local 通路：作用域写（仅 report/work 文本件）、无 Bash、不过门禁审批、无 baseline 快照；changed_files 为 agent 自报、未经核验">local</span>`:""}</td>
   <td style="font-size:12px;color:#94a3b8" title="${esc(t.error||'')}">${esc((t.error||'').slice(0,24))}</td>
   <td class="desc" title="${esc(t.description)}">${esc(t.description)}</td>
   <td>${fmtT(t.created_at)}</td><td style="font-size:12px">${fmtDur(t)}</td><td class="num">${t.tokens||0}</td>
   <td>${act}</td></tr>`;
  }).join("")||'<tr><td colspan="9" style="color:#64748b">无匹配任务</td></tr>')+"</table>";
}
async function openDetail(id){
 const r=await fetch("/dashboard/tasks/"+encodeURIComponent(id));
 if(!r.ok){alert("读取失败: "+r.status);return;}
 const t=await r.json();
 document.getElementById("modal-title").textContent=`任务 #${t.id} · ${t.user} · ${ST[t.status]||t.status}`;
 const L=["描述：",t.description||"（空）",""];
 if(t.result)L.push("结果（节选）：",t.result,"");
 if(t.error)L.push("错误：",t.error,"");
 if(t.blocked_cmd)L.push("拦截命令：",t.blocked_cmd,"");
 L.push(`状态: ${t.status}　引擎: ${t.engine||"-"}　只读: ${t.read_only?"是":"否"}　优先级: ${t.priority||"normal"}`);
 L.push(`提交: ${fmtT(t.created_at)}　开始: ${fmtT(t.started_at)}　结束: ${fmtT(t.finished_at)}　耗时: ${fmtDur(t)}`);
 L.push(`tokens: ${t.tokens||0}${t.resume_from?`　续跑自: #${t.resume_from}`:""}`);
 if((t.changed_files||[]).length)L.push("","改动文件：",t.changed_files.join("\\n"));
 if((t.conflicts||[]).length)L.push("","冲突：",JSON.stringify(t.conflicts,null,1));
 document.getElementById("modal-body").textContent=L.join("\\n");
 document.getElementById("modal").style.display="flex";
}
async function refresh(){
 try{
  const [r,tr]=await Promise.all([fetch("/dashboard/data?days="+DAYS),fetch("/dashboard/tokens")]);
  if(!r.ok)throw 0;
  const d=await r.json();
  LAST=d;
  if(tr.ok){try{TOKU=(await tr.json()).users||[]}catch(e){}}
  renderCards(d.metrics,d.report);
  document.getElementById("trend").innerHTML=trendSVG(d.report.by_day||[]);
  document.getElementById("dist").innerHTML=Object.entries(d.metrics.tasks)
   .map(([s,n])=>`${badge(s)} ${n}`).join("　")||"无任务";
  renderUsers(d.report);
  populateFilters(d.tasks||[]);
  renderTasks();
  document.getElementById("upd").textContent="更新于 "+new Date().toLocaleTimeString("zh",{hour12:false});
 }catch(e){}
}
function setDays(n){DAYS=n;
 document.getElementById("rng7").className="rng"+(n===7?" on":"");
 document.getElementById("rng30").className="rng"+(n===30?" on":"");
 refresh();}
document.getElementById("rng7").addEventListener("click",()=>setDays(7));
document.getElementById("rng30").addEventListener("click",()=>setDays(30));
document.getElementById("fstatus").addEventListener("change",renderTasks);
document.getElementById("fuser").addEventListener("change",renderTasks);
document.getElementById("modal-close").addEventListener("click",e=>{e.preventDefault();document.getElementById("modal").style.display="none";});
document.getElementById("modal").addEventListener("click",e=>{if(e.target.id==="modal")document.getElementById("modal").style.display="none";});
function showTab(v){
  document.getElementById("view-metrics").style.display=v==="m"?"":"none";
  document.getElementById("view-tokens").style.display=v==="t"?"":"none";
  document.getElementById("view-shared").style.display=v==="s"?"":"none";
  document.getElementById("view-plans").style.display=v==="p"?"":"none";
  document.getElementById("tab-metrics").className=v==="m"?"on":"";
  document.getElementById("tab-tokens").className=v==="t"?"on":"";
  document.getElementById("tab-shared").className=v==="s"?"on":"";
  document.getElementById("tab-plans").className=v==="p"?"on":"";
  if(v==="t")refreshTokens();
  if(v==="s")refreshShared();
  if(v==="p"){refreshPlans();refreshTimers();}
}
document.getElementById("tab-metrics").addEventListener("click",e=>{e.preventDefault();showTab("m")});
document.getElementById("tab-tokens").addEventListener("click",e=>{e.preventDefault();showTab("t")});
document.getElementById("tab-shared").addEventListener("click",e=>{e.preventDefault();showTab("s")});
document.getElementById("tab-plans").addEventListener("click",e=>{e.preventDefault();showTab("p")});
document.getElementById("tasks").addEventListener("click",async e=>{
 const a=e.target.closest("a.act");
 if(a){
  e.preventDefault();
  const k=a.dataset.k,n=a.dataset.n;
  if(k==="det"){return openDetail(n);}
  if(k!=="appr"&&k!=="deny")return;
  const dec=k==="appr"?"approve":"deny";
  const msg=(dec==="approve"
    ?`批准放行任务 ${n} 的拦截命令并续跑？\n\n`
    :`拒绝任务 ${n}？将被置为 failed（不续跑）。\n\n`)+(a.title||"");
  if(!confirm(msg))return;
  const r=await fetch(`/dashboard/tasks/${encodeURIComponent(n)}/approve`,
    {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision:dec})});
  if(!r.ok){alert("操作失败: "+r.status);return;}
  refresh();
  return;
 }
 const tr=e.target.closest("tr.trow");
 if(tr&&tr.dataset.id)openDetail(tr.dataset.id);
});
async function refreshShared(){
 try{
  const r=await fetch("/dashboard/shared");if(!r.ok)throw 0;const d=await r.json();
  document.getElementById("sh-skills").innerHTML=d.skills.length
   ?`<table><tr><th>名称</th><th>说明</th><th>文件</th><th>操作</th></tr>`+d.skills.map(s=>
     `<tr><td><code>${esc(s.name)}</code></td>
      <td style="font-size:12px;color:#94a3b8" title="${esc(s.description)}">${esc((s.description||"").slice(0,70))}</td>
      <td style="font-size:12px;color:#94a3b8">${esc(s.files.join(", ").slice(0,90))}</td>
      <td><a href="#" class="act" data-k="view-skill" data-n="${esc(s.name)}" data-f="SKILL.md">查看</a>
      <a href="#" class="act" data-k="del-skill" data-n="${esc(s.name)}" style="color:#f87171">删除</a></td></tr>`).join("")+"</table>"
   :'<p style="font-size:13px;color:#64748b">暂无技能。上传后出现在全员 agent 的技能列表（SKILL.md 需含 name/description frontmatter）</p>';
  document.getElementById("sh-mcp").innerHTML=d.mcp.length
   ?`<table><tr><th>名称</th><th>概要</th><th>操作</th></tr>`+d.mcp.map(s=>
     `<tr><td><code>${esc(s.name)}</code></td>
      <td style="font-size:12px;color:#94a3b8">${esc(s.summary||JSON.stringify(s.config||{}).slice(0,80))}</td>
      <td><a href="#" class="act" data-k="view-mcp" data-n="${esc(s.name)}">查看</a>
      <a href="#" class="act" data-k="del-mcp" data-n="${esc(s.name)}" style="color:#f87171">删除</a></td></tr>`).join("")+"</table>"
   :'<p style="font-size:13px;color:#64748b">暂无 MCP 服务器。上传后全员 agent 下次任务即可调用其工具</p>';
  document.getElementById("sh-memory").innerHTML=d.memory.length
   ?`<table><tr><th>名称</th><th class="num">大小</th><th>更新</th><th>操作</th></tr>`+d.memory.map(s=>
     `<tr><td><code>${esc(s.name)}</code></td><td class="num">${s.size}B</td><td>${fmtT(s.mtime)}</td>
      <td><a href="#" class="act" data-k="view-mem" data-n="${esc(s.name)}">查看</a>
      <a href="#" class="act" data-k="del-mem" data-n="${esc(s.name)}" style="color:#f87171">删除</a></td></tr>`).join("")+"</table>"
   :'<p style="font-size:13px;color:#64748b">暂无团队记忆。上传后自动注入每个成员 agent 的 AGENTS.md</p>';
 }catch(e){}
}
document.getElementById("view-shared").addEventListener("click",async e=>{
 const a=e.target.closest("a.act");if(!a)return;e.preventDefault();
 const k=a.dataset.k,n=a.dataset.n;
 if(k.startsWith("view-")){
  const file=a.dataset.f||"";
  const r=await fetch(`/dashboard/shared/content?kind=${k.slice(5)}&name=${encodeURIComponent(n)}&file=${encodeURIComponent(file)}`);
  if(!r.ok){alert("读取失败: "+r.status);return;}
  const d=await r.json();
  document.getElementById("sh-view-title").textContent=`${d.kind} / ${d.name}${file?" / "+file:""}`;
  document.getElementById("sh-view-body").textContent=d.content;
  document.getElementById("sh-view").style.display="";
  return;
 }
 const delMap={"del-skill":["skills",`删除技能 ${n}？全员将不再加载它。`],
               "del-mcp":["mcp",`删除 MCP ${n}？全员将不再使用它。`],
               "del-mem":["memory",`删除记忆 ${n}？`]};
  const m=delMap[k];if(!m)return;
  if(!confirm(m[1]))return;
  const r=await fetch(`/dashboard/shared/${m[0]}/${encodeURIComponent(n)}`,{method:"DELETE"});
  if(!r.ok){alert("删除失败: "+r.status);return;}
  refreshShared();
});
document.getElementById("sh-view-close").addEventListener("click",e=>{
 e.preventDefault();document.getElementById("sh-view").style.display="none";
});
document.getElementById("btn-sh-skill").addEventListener("click",async()=>{
 const name=document.getElementById("sh-skill-name").value.trim();
 const f=document.getElementById("sh-skill-f").files[0];
 const fp=document.getElementById("sh-skill-file").value.trim()||"SKILL.md";
 if(!name||!f){alert("请填写技能名并选择文件");return;}
 const b64=await new Promise((res,rej)=>{
  const fr=new FileReader();fr.onload=()=>res(String(fr.result).split(",")[1]);
  fr.onerror=rej;fr.readAsDataURL(f);
 });
 const r=await fetch("/dashboard/shared/skills",{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify({name,file:fp,content_b64:b64})});
 if(!r.ok){alert("上传失败: "+r.status);return;}
 alert("上传成功，立即生效（下一个任务自动加载）");
 document.getElementById("sh-skill-f").value="";
 refreshShared();
});
document.getElementById("btn-sh-mcp").addEventListener("click",async()=>{
 const name=document.getElementById("sh-mcp-name").value.trim();
 const txt=document.getElementById("sh-mcp-json").value.trim();
 let cfg;try{cfg=JSON.parse(txt)}catch(e){alert("JSON 格式错误");return;}
 if(!name||typeof cfg!=="object"||Array.isArray(cfg)){alert("请填写服务器名和有效 JSON 对象");return;}
 const r=await fetch("/dashboard/shared/mcp",{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify({name,config:cfg})});
 if(!r.ok){alert("上传失败: "+r.status);return;}
 alert("上传成功，立即生效（下一个任务自动加载）");
 refreshShared();
});
document.getElementById("btn-sh-mem").addEventListener("click",async()=>{
 const name=document.getElementById("sh-mem-name").value.trim();
 const txt=document.getElementById("sh-mem-text").value;
 if(!name||!txt.trim()){alert("请填写记忆名和内容");return;}
 const r=await fetch("/dashboard/shared/memory",{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify({name,content:txt})});
 if(!r.ok){alert("上传失败: "+r.status);return;}
 alert("上传成功，立即生效（下一个任务自动注入全员 agent）");
 refreshShared();
});
async function refreshTokens(){
 try{
  const r=await fetch("/dashboard/tokens");if(!r.ok)throw 0;const d=await r.json();
  const us=d.users||[];
  const rows=us.map(u=>`<tr><td>${esc(u.name)}</td><td><code>${esc(u.token)}</code></td>
   <td>${u.consumed_at
     ?`<span style="color:#4ade80">已核销</span> <span style="font-size:11px;color:#64748b">${fmtT(u.consumed_at)}</span>`
     :'<span style="color:#fbbf24" title="激活码尚未使用，持有者仍可激活">未核销</span>'}</td>
   <td class="num" title="已激活绑定的设备数">${u.devices||0}</td>
   <td>${fmtT(u.created_at)}</td><td class="num">${u.tasks_7d}</td><td class="num">${u.done_7d}</td>
   <td><a href="#" class="act" data-a="regen" data-n="${esc(u.name)}">重置</a>
   <a href="#" class="act" data-a="del" data-n="${esc(u.name)}" style="color:#f87171">删除</a></td></tr>`).join("");
  document.getElementById("toks").innerHTML=rows
   ?`<table><tr><th>用户</th><th>token（激活码）</th><th>核销状态</th><th class="num">设备</th><th>签发时间</th><th class="num">近7天任务</th><th class="num">完成</th><th>操作</th></tr>${rows}</table>`
   :'<p style="font-size:13px;color:#64748b">暂无成员</p>';
 }catch(e){}
}
function showNewTok(d){
 const box=document.getElementById("newtok");
 box.style.display="";
 box.innerHTML=`<div style="font-size:13px;color:#86efac;margin-bottom:8px"><b>${esc(d.name)}</b> 的凭证已生成。token 仅显示这一次，请立即通过私聊发给本人保存：</div>
 <div style="display:flex;gap:8px;align-items:flex-start"><code id="toktxt" style="flex:1;font-size:13px;color:#e2e8f0;background:#0f172a;padding:8px 10px;border-radius:8px;word-break:break-all">${esc(d.token)}</code>
 <button onclick="copyTok()" style="background:#16a34a">复制</button></div>`;
}
function copyTok(){
 const t=document.getElementById("toktxt").textContent;
 const done=()=>alert("已复制到剪贴板");
 const bad=()=>alert("自动复制失败，请手动选中复制");
 if(navigator.clipboard && window.isSecureContext){
  navigator.clipboard.writeText(t).then(done,bad);
 }else{
  const ta=document.createElement("textarea");
  ta.value=t;ta.style.position="fixed";ta.style.opacity="0";
  document.body.appendChild(ta);ta.select();
  let ok=false;try{ok=document.execCommand("copy")}catch(e){}
  document.body.removeChild(ta);ok?done():bad();
 }
}
document.getElementById("toks").addEventListener("click",async e=>{
 const a=e.target.closest("a.act");if(!a)return;e.preventDefault();
 const n=a.dataset.n;
 if(a.dataset.a==="regen"){
  if(!confirm(`重置 ${n} 的 token？旧 token 立即失效，需要把新 token 重新发给 ${n}。`))return;
  const r=await fetch(`/dashboard/tokens/${encodeURIComponent(n)}/regenerate`,{method:"POST"});
  if(!r.ok){alert("重置失败: "+r.status);return;}
  showNewTok(await r.json());refreshTokens();
 }else if(a.dataset.a==="del"){
  if(!confirm(`删除 ${n} 的凭证？删除后其 token 立即失效（历史任务记录保留）。`))return;
  const r=await fetch(`/dashboard/tokens/${encodeURIComponent(n)}`,{method:"DELETE"});
  if(!r.ok){alert("删除失败: "+r.status);return;}
  refreshTokens();
 }
});
document.getElementById("btn-new").addEventListener("click",async()=>{
 const n=document.getElementById("newname").value.trim();if(!n)return;
 const r=await fetch("/dashboard/tokens",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:n})});
 if(r.status===409){alert("该用户已存在");return;}
 if(!r.ok){alert("创建失败: "+r.status);return;}
 const d=await r.json();
 document.getElementById("newname").value="";
 showNewTok(d);refreshTokens();
});
function fmtNext(t){if(!t)return"-";const d=new Date(t*1000);
 return d.toLocaleString("zh",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false})}
async function refreshPlans(){
 try{
  const r=await fetch("/dashboard/plans");if(!r.ok)throw 0;const d=await r.json();
  const ps=d.plans||[];
  document.getElementById("plans").innerHTML=ps.length
   ?`<table><tr><th>名称</th><th>OnCalendar</th><th>下次</th><th>上次</th><th>状态</th><th>任务</th><th>操作</th></tr>`+ps.map(p=>{
     const a=p.action||{};const desc=a.description||"";
     return `<tr><td><code>${esc(p.name)}</code><div style="font-size:11px;color:#64748b">${esc(p.owner_name)}</div></td>
      <td style="font-size:12px">${esc(p.on_calendar)}</td>
      <td style="font-size:12px">${p.enabled?fmtNext(p.next_run):"-"}</td>
      <td style="font-size:12px">${fmtNext(p.last_run)}</td>
      <td>${p.enabled?'<span class="b done">启用</span>':'<span class="b cancelled">停用</span>'}</td>
      <td class="desc" title="${esc(desc)}">${esc(desc.slice(0,40))}</td>
      <td><a href="#" class="act" data-a="toggle" data-n="${p.id}">${p.enabled?"停用":"启用"}</a>
      <a href="#" class="act" data-a="del" data-n="${p.id}" style="color:#f87171">删除</a></td></tr>`;
    }).join("")+"</table>"
   :'<p style="font-size:13px;color:#64748b">暂无计划</p>';
  }catch(e){}
 }
 async function refreshTimers(){
  try{
   const r=await fetch("/dashboard/timers");if(!r.ok)throw 0;const d=await r.json();
   const ts=d.timers||[];
   document.getElementById("timers").innerHTML=ts.length
    ?`<table><tr><th>定时器</th><th>激活单元</th><th>下次</th><th>上次</th><th>状态</th></tr>`+ts.map(t=>{
      return `<tr><td><code>${esc(t.unit)}</code></td>
       <td style="font-size:12px">${esc(t.activates||"")}</td>
       <td style="font-size:12px">${t.next?fmtNext(t.next):"-"}</td>
       <td style="font-size:12px">${t.last?fmtNext(t.last):"-"}</td>
       <td>${t.running?'<span class="b done">运行中</span>':'<span class="b cancelled">已停止</span>'}</td></tr>`;
     }).join("")+"</table>"
    :'<p style="font-size:13px;color:#64748b">无定时器</p>';
  }catch(e){}
 }
 document.getElementById("plans").addEventListener("click",async e=>{
 const a=e.target.closest("a.act");if(!a)return;e.preventDefault();
 const id=a.dataset.n,act=a.dataset.a;
 if(act==="toggle"){
  const r=await fetch(`/dashboard/plans/${encodeURIComponent(id)}/toggle`,{method:"POST"});
  if(!r.ok){alert("操作失败: "+r.status);return;}
  refreshPlans();
 }else if(act==="del"){
  if(!confirm("删除该计划？"))return;
  const r=await fetch(`/dashboard/plans/${encodeURIComponent(id)}`,{method:"DELETE"});
  if(!r.ok){alert("删除失败: "+r.status);return;}
  refreshPlans();
 }
});
document.getElementById("btn-pl-new").addEventListener("click",async()=>{
 const name=document.getElementById("pl-name").value.trim();
 const cal=document.getElementById("pl-cal").value.trim();
 const desc=document.getElementById("pl-desc").value.trim();
 if(!name||!cal||!desc){alert("请填写计划名、OnCalendar 和任务描述");return;}
 const action={type:"task",description:desc,
  priority:document.getElementById("pl-urgent").checked?"urgent":"normal",
  read_only:document.getElementById("pl-ro").checked};
 const r=await fetch("/dashboard/plans",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({name,on_calendar:cal,action,enabled:true})});
 if(r.status===409){alert("计划名已存在");return;}
 if(!r.ok){let m=r.status;try{m=await r.text()}catch(e){}alert("创建失败: "+m);return;}
 document.getElementById("pl-name").value="";document.getElementById("pl-cal").value="";
 document.getElementById("pl-desc").value="";document.getElementById("pl-urgent").checked=false;
 document.getElementById("pl-ro").checked=false;
 refreshPlans();
});
refresh();setInterval(()=>{refresh();if(document.getElementById("view-tokens").style.display!=="none")refreshTokens();if(document.getElementById("view-plans").style.display!=="none"){refreshPlans();refreshTimers()}},10000);
</script></body></html>"""
from .executor import Executor, signal_name
from .scheduler import Scheduler
from .targets import find_conflicts

# 纯读声明识别：显式 read_only 字段（客户端 ≥1.5.0）或描述带只读标记（存量客户端零改动）。
# 事前防碰对纯读直接放行；事后扫描兜底——声明只读却改了文件 → conflict 人工核查。
READ_ONLY_MARKERS = ("只读", "read-only", "read only", "readonly")


def _declares_read_only(description: str) -> bool:
    d = (description or "").lower()
    return any(m in d for m in READ_ONLY_MARKERS)


def _prefix_match(rel: str, prefixes: list[str]) -> bool:
    rel = (rel or "").replace(os.sep, "/")
    for p in prefixes or []:
        p = (p or "").replace(os.sep, "/").strip()
        if not p:
            continue
        if not p.endswith("/"):
            p += "/"
        if rel.startswith(p):
            return True
    return False


def auto_done_reason(cfg: Settings, row: dict, scan: dict, result_text: str,
                     real_conflicts: list[dict], ro_overwritten: list[str]) -> str:
    """低风险新建产出 → 自动 done，不再进 review。

    旧策略把“任何实质文件变更”都置 review，导致大量只是生成报告/临时数据/系统
    盘点文件的任务等人确认。新策略只放行**新增**且全部落在低风险前缀下的产出；
    修改/删除既有文件、跨任务冲突、只读覆盖、越界任务号、批量过大、无结果文本
    仍保留人工核查。
    """
    if getattr(cfg, "auto_done_mode", "off") != "on":
        return ""
    if real_conflicts or ro_overwritten:
        return ""
    if scan.get("modified") or scan.get("deleted"):
        return ""
    if scan.get("foreign"):
        return ""
    created = scan.get("created") or []
    if not created:
        return ""
    max_files = int(getattr(cfg, "auto_done_max_files", 0) or 0)
    if max_files > 0 and len(created) > max_files:
        return ""
    if not (result_text or "").strip():
        return ""
    prefixes = getattr(cfg, "auto_done_prefixes", []) or []
    if not all(_prefix_match(rel, prefixes) for rel in created):
        return ""
    kind = "只读任务新建低风险产出" if row.get("read_only") else "低风险新建产出"
    return f"自动done（{kind} {len(created)} 个，无需人工确认）"


def _session_if_same_engine(task_row: dict | None, engine: str) -> str | None:
    """仅当历史任务由当前引擎执行时才复用其 session_id（会话格式不跨引擎）。

    存量行 engine 为空 → 历史上只有 opencode 在跑，按 opencode 归属。
    """
    if not task_row:
        return None
    sid = task_row.get("session_id") or None
    if not sid:
        return None
    return sid if (task_row.get("engine") or "opencode") == engine else None
from .workspace import Workspace


def build_prompt(row: dict, cfg: Settings) -> str:
    targets = ", ".join(row["targets"]) or "（未声明；按任务描述判断，尽量不触碰共享区）"
    tid = row["id"]
    p = (
        f"任务：{row['description']}\n"
        f"本任务号：#{tid}\n"
        f"本任务声明的改动目标：{targets}\n"
        f"团队共享区：{cfg.shared_dir}\n"
        f"  - 共享知识已自动加载进本次会话：技能 {cfg.shared_dir}/skills/、"
        f"MCP 工具 {cfg.shared_dir}/mcp/、团队记忆 {cfg.shared_dir}/memory/（已并入 AGENTS.md）\n"
        f"  - 知识库文档 {cfg.shared_dir}/knowledge/：动手前可先查阅；"
        f"产生的可复用经验沉淀到对应主题目录\n"
        # 产出命名铁律。为什么必须显式写进 prompt：AGENTS.md 虽已规定
        # `report : <YYYYMMDD>_<taskId>_<topic>_v<N>.<ext>`，但**此前 prompt 从不
        # 告知 agent 自己的任务号**，它无从遵守。2026-10-10 事故：同一需求经三个
        # 会话触发 #689/#690/#691，后两个看到磁盘上已有 `_T689_` 的报告就接着写，
        # 三个任务全写进同一路径 → 触发冲突守卫 → 双双卡在人工核查。
        f"  - 产出命名铁律：report/data 文件名里的任务号段**只能写本任务号 T{tid}**"
        f"（无关联任务才写 T0）。禁止创建或覆盖带其它任务号的文件（如 T689）——"
        f"那是别的任务的交付件，覆盖会触发冲突核查并把双方一起卡住。"
        f"同一产出要改版就 vN+1，不要同名覆盖。\n"
        f"完成后用 2-5 句话总结：做了什么、改了哪些文件。"
    )
    if row.get("resume_from"):
        p = f"接续任务 #{row['resume_from']} 的会话，追加要求：\n" + p
    return p


class NewTask(BaseModel):
    description: str
    project: str = ""
    targets: list[str] = Field(default_factory=list)
    priority: str = "normal"  # normal | urgent
    force: bool = False
    read_only: bool = False  # 纯读声明：跳过事前防碰；事后若发现文件改动仍会置 conflict
    resume_from: int | None = None
    # 真实发起人（钉钉昵称）。avatar 全机队共用一个 relay token，故 user 恒为
    # 机器人「鲁大师」；不带上这个字段就看不出任务是谁提的，冲突提示还会把
    # 「同一人重复提交导致的自我撞车」写成「与鲁大师冲突」（2026-10-10 排障
    # 被它误导过一整轮）。仅作展示与归因，不参与鉴权/配额。
    requester: str = ""
    # relay=正常派发执行（默认）；local=只建**影子记录**不派发——调用方（avatar）
    # 在自己进程里跑 claude，relay 仅负责让它在任务列表可见、可查状态变更历史。
    backend: str = "relay"


class LocalStatus(BaseModel):
    """local 影子记录的状态回报（avatar → relay）。"""
    status: str                       # done | failed | cancelled
    result: str = ""
    error: str = ""
    tokens: int = 0
    cost: float = 0.0
    changed_files: list[str] = Field(default_factory=list)  # agent 自报，未核验


class NewUser(BaseModel):
    name: str


class ActivateBody(BaseModel):
    token: str                 # 一次性激活码（即管理员发放的 ta_ token）
    device_id: str = ""        # 客户端生成的设备标识（缺省服务端代生成）
    device_name: str = ""      # 设备名（主机名等，仅展示/审计用）


class ConfirmBody(BaseModel):
    outcome: str = "done"  # done | conflict


class ApproveBody(BaseModel):
    decision: str  # approve | deny


class NewPlan(BaseModel):
    name: str
    on_calendar: str            # systemd OnCalendar 表达式，如 *-*-* 08:00:00
    action: dict                # 白名单动作，目前仅 {"type":"task","description":...,...}
    enabled: bool = True


class PlanPatch(BaseModel):
    name: str | None = None
    on_calendar: str | None = None
    action: dict | None = None
    enabled: bool | None = None


class UploadFileBody(BaseModel):
    path: str
    content_b64: str


class SharedSkillUpload(BaseModel):
    name: str
    content_b64: str
    file: str = "SKILL.md"  # skill 目录内相对路径（默认 SKILL.md，可传 assets/xxx）


class SharedMcpUpload(BaseModel):
    name: str
    config: dict  # opencode mcp 条目，如 {"type":"local","command":[...],"enabled":true}


class SharedMemoryUpload(BaseModel):
    name: str
    content: str  # markdown 正文


class DashTokenBody(BaseModel):
    name: str


def _shared_path(shared_dir: str, rel: str) -> str:
    """把相对路径解析到共享区内；拒绝绝对路径、任何路径穿越与 secrets/ 凭证目录。"""
    rel = (rel or "").strip()
    if not rel or rel.startswith("/") or "\x00" in rel:
        raise HTTPException(status_code=400, detail="path must be relative to shared dir")
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep):
        raise HTTPException(status_code=400, detail="path traversal not allowed")
    base = os.path.realpath(shared_dir)
    full = os.path.realpath(os.path.join(base, norm))
    if not (full == base or full.startswith(base + os.sep)):
        raise HTTPException(status_code=400, detail="path escapes shared dir")
    secrets_root = os.path.realpath(os.path.join(base, "secrets"))
    if full == secrets_root or full.startswith(secrets_root + os.sep):
        raise HTTPException(status_code=403,
                            detail="secrets dir is not accessible via files API")
    return full


def _service_version() -> str:
    """读取部署时写入的 VERSION（git SHA）；开发树无此文件时为 dev。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()[:64] or "dev"
    except OSError:
        return "dev"


def _launch_boot(boot: str) -> tuple[bool, str]:
    """在独立 cgroup 中触发 bootloader（须活过 relay 自身重启）。

    首选 systemd-run 瞬态单元（同名单元防并发重入）；无 systemd 时退化为
    脱离会话的后台进程（开发环境用；生产重启时可能被 cgroup 连带回收）。
    """
    if not os.environ.get("RELAY_BOOT_NOSYSTEMD"):
        try:
            p = subprocess.run(
                ["systemd-run", "--unit=relay-boot-manual", "--collect", boot, "update"],
                capture_output=True, text=True, timeout=15)
            if p.returncode == 0:
                return True, "systemd-run:relay-boot-manual"
            if "already" in (p.stderr or "").lower():
                return False, "update already in progress"
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        lf = open("/tmp/relay-boot-manual.log", "ab")
        subprocess.Popen([boot, "update"], stdout=lf, stderr=lf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True, "detached"
    except OSError as e:
        return False, f"launch failed: {e!r}"


def _list_system_timers() -> list[dict]:
    """本机 systemd 定时器只读一览（供 dashboard 展示，不做启停）。

    解析 `systemctl list-timers --all --output=json`；时间戳为微秒，
    next 为空表示未排程（timer 未运行）。任何异常退化为空列表，
    绝不影响 dashboard 其余部分。
    """
    try:
        p = subprocess.run(
            ["systemctl", "list-timers", "--all", "--output=json"],
            capture_output=True, text=True, timeout=5)
        raw = json.loads(p.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    out = []
    for t in raw:
        nxt = t.get("next") or None
        last = t.get("last") or None
        out.append({
            "unit": t.get("unit"),
            "activates": t.get("activates"),
            "next": nxt / 1_000_000 if nxt else None,
            "last": last / 1_000_000 if last else None,
            "running": bool(nxt),
        })
    out.sort(key=lambda r: (not r["running"], r["next"] if r["next"] is not None else 0))
    return out


def sweep_stale(db, bus, audit, cfg, ws) -> list[int]:
    """超时清扫：review 停留过久 → 自动 done；待审批停留过久 → 自动 **failed**。

    两者必须分开：review 是"活已干完、等人过目"，超时置 done 无损；pending_approval
    是"活干到一半被门禁拦住"，此前与 review 共用一条 UPDATE 被置成 done，等于把
    **没办完的任务标成办完了**（#299 若无人手工拒批，24h 后就会静默变成 done）。

    性能：用两个 cutoff 里**较短的那个查一次**，再在 Python 里按 status 与各自的
    阈值分流——不增加查询次数（sweep 每 sweep_interval_sec 才跑一次）。

    另兜 local 影子记录的孤儿：avatar 崩溃/被 kill 时来不及回报终态，记录会永远
    停在 running，超 local_stale_minutes 即判 failed（否则任务列表里挂一条僵尸行，
    谁也看不出它是死了还是在跑）。

    返回被清扫的 task_id 列表。
    """
    now = time.time()
    rcut = now - cfg.stale_timeout_hours * 3600            # review 阈值
    acut = now - cfg.approval_timeout_minutes * 60         # 待审批阈值
    swept: list[int] = []
    # stale_tasks 的条件是 `created_at < cutoff`，故要同时覆盖两个阈值必须取
    # **较大**（较近）的 cutoff——它捞出的集合是两者的超集，再在 Python 里按
    # status 与各自阈值分流。取 min 会漏掉全部待审批任务（实测踩到）。
    for t in db.stale_tasks(["review", "pending_approval"], max(rcut, acut)):
        if t["status"] == "pending_approval":
            if (t.get("created_at") or 0) >= acut:
                continue                                   # 未到待审批阈值
            err = (f"待审批超过 {cfg.approval_timeout_minutes:g} 分钟无人处理，"
                   f"自动终止（被拦命令：{(t.get('blocked_cmd') or '')[:120]}）")
            if db.mark_approval_timeout(t["id"], err):
                bus.publish(t["id"], t="status", s="failed", error=err)
                audit.line(t["id"], t["user"], "approval_timeout", err[:200])
                ws.cleanup_terminal(t)
                swept.append(t["id"])
            continue
        if (t.get("created_at") or 0) >= rcut:
            continue                                       # 未到 review 阈值
        if db.mark_stale_done(t["id"], "超时自动done"):
            bus.publish(t["id"], t="status", s="done", error="超时自动done")
            audit.line(t["id"], t["user"], "auto_done_timeout",
                       f"停留超过 {cfg.stale_timeout_hours:g}h，超时自动done")
            ws.cleanup_terminal(t)
            swept.append(t["id"])
    lcut = time.time() - cfg.local_stale_minutes * 60
    for t in db.stale_local_tasks(lcut):
        err = (f"avatar 未回报（running 超过 {cfg.local_stale_minutes:g} 分钟），"
               f"判为进程重启或崩溃所致")
        db.set(t["id"], status="failed", finished_at=time.time(), error=err)
        bus.publish(t["id"], t="status", s="failed", error=err)
        audit.line(t["id"], t["user"], "local_orphan_swept", err)
        swept.append(t["id"])
    return swept


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or Settings.from_env()
    if cfg.admin_token in ("", "change-me"):
        raise RuntimeError(
            "RELAY_ADMIN_TOKEN 未设置或仍为默认值 change-me，拒绝启动"
            "（admin 接口可触发部署 /admin/update）。"
            "请在 relay.env 设置强随机 token，如：openssl rand -hex 24")
    for d in (cfg.shared_dir, cfg.task_root, cfg.users_root):
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(cfg.shared_dir, "knowledge"), exist_ok=True)

    db = DB(cfg.db_path)
    bus = EventBus(cfg.event_history_max)
    ws = Workspace(cfg, db)
    ex = Executor(cfg)
    audit = AuditLog(cfg.log_path)
    cancel_events: dict[int, asyncio.Event] = {}
    # 侦察类命令的**自动拒绝**续跑计数（Tier 2）。进程内即可：重启丢计数最坏只是
    # 多给两次机会，且任务总时限（1800s）本身就是硬上界。用 dict 而非 DB 列是为了
    # 零迁移、零额外查询——热路径上不碰数据库。
    auto_deny_counts: dict[int, int] = {}
    # 分段续跑的**进展签名**（task_id → 上一段结束时的产出签名）。同样是进程内 dict：
    # 热路径不碰 DB，重启丢签名最坏只是 fail-open 多给一段（段号在 resume_hint 里，
    # 重启不丢，硬上界仍然生效）。
    seg_progress: dict[int, tuple] = {}
    sched: Scheduler | None = None  # set in lifespan
    # 计划内重启标志，lifespan 收尾时置位。run_task 据此把「被 SIGTERM 打断」的任务
    # 退回 queued 而不是判 failed：否则每次部署都把在跑的任务打成失败、还回用户一句
    # 「❌ 没办成」（#149 实测——已续到第 2/3 段，被部署杀掉，前 30 分钟产出白费）。
    # 硬杀（kill -9/OOM）走的是另一条路：行留在 running，由 lifespan 开头的启动恢复
    # 逻辑接回。两条路此前只有后者能活，计划内重启反而更致命，正好是反的。
    shutting_down = False
    started_at = time.monotonic()

    async def run_task(task_id: int) -> None:
        row = db.task(task_id)
        if row is None or row["status"] != "queued":
            return
        prev = db.task(row["resume_from"]) if row.get("resume_from") else None
        self_resume = bool(row.get("resume_hint"))
        if self_resume:
            reuse_workdir = row.get("workdir") or None
            resume_session = _session_if_same_engine(row, cfg.engine)
            data_dir = os.path.join(row["workdir"], "data") if row.get("workdir") else None
            prompt = row["resume_hint"]
            exempt_cmd = row.get("blocked_cmd") or None
        else:
            reuse_workdir = prev["workdir"] if (prev and prev.get("workdir")) else None
            resume_session = _session_if_same_engine(prev, cfg.engine)
            data_dir = os.path.join(prev["workdir"], "data") if (prev and prev.get("workdir")) else None
            prompt = build_prompt(row, cfg)
            exempt_cmd = None
        workdir = ws.prepare(row, reuse_workdir=reuse_workdir)
        row = db.task(task_id)  # 刷新：拿到 prepare 写入的 workdir
        cancel = cancel_events.setdefault(task_id, asyncio.Event())
        db.set(task_id, status="running", started_at=time.time())
        audit.line(task_id, row["user"], "start", workdir)
        bus.publish(task_id, t="status", s="running")
        seg = _segment_of(row.get("resume_hint") or "")

        def _on_long(tid: int, elapsed: float) -> None:
            """executor 在单段跑到 T（默认 600s）时回调，每任务每段最多一次。

            shadow 模式**只记审计不改行为**——影子期的意义就是量出"若启用会怎样"的
            精确率/召回，一旦让它动调度，测出来的数就被自己的改动污染了。
            """
            audit.line(tid, row["user"], "long_task",
                       f"elapsed={elapsed:.0f}s seg={seg} mode={cfg.long_task_mode}")
            if cfg.long_task_mode == "on" and sched is not None:
                sched.mark_long(tid)

        res = await ex.run(
            task_id=task_id,
            workdir=workdir,
            prompt=prompt,
            resume_session=resume_session,
            data_dir=data_dir,
            cancel=cancel,
            publish=bus.publish,
            exempt_cmd=exempt_cmd,
            user=row["user"],
            on_long=_on_long,
        )
        finished = time.time()
        db.set(task_id,
               session_id=res.session_id or "",
               engine=cfg.engine,
               result=res.text[-4000:],
               tokens=res.tokens,
               cost=res.cost,
               resume_hint="")
        if res.error and res.error.startswith("blocked:"):
            # ── Tier 2：侦察类自动拒绝 + 回注提示续跑（不打扰用户）──
            # 分类靠比对 executor 已算出的原因字符串（frozenset O(1)），不新增正则。
            # 为什么不给用户看：业务用户无法判断 `ls -laR /opt/team/relay-avatar/`
            # 安不安全，只会「点确认让它闭嘴」或「点拒绝」——#299 就是被困惑的拒绝
            # 杀掉整个任务（卡 76 分钟，而它的正事其实早已办完）。
            #
            # 复用既有 self_resume 机制：写 resume_hint 即自动续跑同一 workdir/session。
            # **必须同时清空 blocked_cmd**——self_resume 分支会把它当 exempt_cmd，
            # 不清空等于给这条命令发通行证，下次直接放行。
            n = auto_deny_counts.get(task_id, 0)
            if res.block_reason in AUTO_DENY_REASONS and n < cfg.auto_deny_max:
                auto_deny_counts[task_id] = n + 1
                hint = (
                    "上一条命令被系统**自动拒绝**（非人工审批，无需等待任何人）：它属于"
                    "对中转/分身自身（凭据、安装目录、进程环境）的侦察，超出本任务范围；"
                    "任务环境里本来就没有这些凭据，继续找只会白烧时限。\n"
                    f"被拒命令：{(res.blocked_cmd or '')[:200]}\n"
                    f"（本任务已自动拒绝 {n + 1}/{cfg.auto_deny_max} 次，"
                    "超限后转人工审批）\n\n"
                    "请**不要重试该命令、也不要换写法绕过**，直接继续完成原任务。"
                    "若你原本想查/建/改定时计划，正规入口是在结果正文出 ```plan``` "
                    "围栏块（见 AGENTS.md「定时任务／计划」节），由分身带凭据代办。"
                    "若原任务确实无法在不访问该路径的前提下完成，请如实说明「做不到"
                    "及原因」，不要静默降级糊弄。")
                db.set(task_id, status="queued", error="", resume_hint=hint,
                       blocked_cmd="")
                bus.publish(task_id, t="status", s="queued")
                audit.line(task_id, row["user"], "auto_deny",
                           f"{n + 1}/{cfg.auto_deny_max} "
                           f"{(res.blocked_cmd or '')[:160]}")
                await sched.submit(task_id, row["priority"])
                return
            db.set(task_id, status="pending_approval", error=res.error,
                   blocked_cmd=res.blocked_cmd)
            bus.publish(task_id, t="status", s="pending_approval",
                        error=res.error, blocked_cmd=res.blocked_cmd)
            audit.line(task_id, row["user"], "block", (res.blocked_cmd or "")[:200])
            return
        row = db.task(task_id)  # 刷新
        scan = ws.scan(row)
        # 越界写入：产出文件名带了别的任务号。不单独置 conflict（内容一致已由
        # identical_version 放行、内容分歧由下面的冲突守卫兜住），但必须留痕——
        # 它是"两个任务撞在同一交付件上"的直接证据，排障时比通用冲突提示有用得多。
        for rel, other_id in (scan.get("foreign") or []):
            audit.line(task_id, row["user"], "foreign_task_file",
                       f"{rel} 带的是 #{other_id} 的任务号（本任务 #{task_id}）")
        real_conflicts = _check_conflicts(row, scan, finished)
        conflicts = list(real_conflicts)
        # 只读声明的事后兜底按风险分级：覆盖/删除既有文件 → conflict（有数据损失风险）；
        # 仅新建文件 → 保留核查说明，低风险前缀下可自动 done，其余仍 review。
        ro_overwritten = sorted(set(scan["modified"]) | set(scan["deleted"])) \
            if row.get("read_only") else []
        if row.get("read_only") and scan["all"]:
            conflicts.append({
                "file": ", ".join((ro_overwritten or scan["all"])[:5]),
                "with_task": task_id,
                "with_user": _who(row),
                "backup": "",
                "note": ("任务声明只读（read_only）却覆盖/删除了既有文件，请人工核查"
                         if ro_overwritten else
                         "任务声明只读（read_only）但新建了文件（未覆盖既有内容），请人工核查"),
            })
        # ── 长任务分段续跑：单段跑满不再「斩立决」，改为带进展门控地接续 ──
        # 依据：3/108 任务死在 30.0m 整、产出全丢、tokens 记 0；#108 被砍后重跑为
        # #111，同样工作 24.3m 就完成——差的是时间预算不是能力。复用已在生产验证的
        # self_resume 机制（Tier 2 侦察类自动拒绝续跑走的就是这条路），零 DB 迁移。
        sig = (tuple(sorted(scan["all"])), tuple(sorted(scan["modified"])),
               tuple(sorted(scan["deleted"])), len(res.text or ""),
               res.tool_calls)
        if (res.timed_out and res.long_flagged and sched is not None
                and cfg.long_task_mode == "on"
                and seg + 1 < cfg.long_task_max_segments):
            prev_sig = seg_progress.get(task_id)
            # 进展门：本段相对上一段有新产出、或本段确实在干活（工具调用达阈值）才续。
            # 为什么必须有工具调用这一路：被砍的段拿不到 result 事件，tokens=0、最终
            # 文本也常是空的——#146 第 1 段实测 files=0 text=0，其实一直在跑工具查数据。
            # 只看文件与文本会把"正在采集数据"误判成"毫无进展"而停掉续跑。
            # 签名未知（首段／进程重启后丢失）→ fail-open 放行一段，硬上界仍由
            # resume_hint 里的段号兜住（重启不丢）。专治 #112 那类丢上下文的糊涂指令。
            progressed = (prev_sig is None or sig[:3] != prev_sig[:3]
                          or sig[3] - prev_sig[3] >= 200
                          or sig[4] >= cfg.long_task_min_tool_calls)
            seg_progress[task_id] = sig
            if progressed:
                nxt = seg + 1
                # 段号按**人读的序号**写：nxt 是"已完成段数"，正在跑的是第 nxt+1 段。
                # 直接写 nxt 会让第一次续跑显示"第 1/3 段"（实际是第 2 段），用户以为
                # 任务没被拆过。续跑总次数仍由 nxt 控制（nxt+1 < max ⇒ 最多 3 段）。
                hint = _with_segment(
                    f"上一段跑满 {cfg.task_timeout / 60:.0f} 分钟被系统中断（**不是失败**，"
                    f"这是第 {nxt + 1}/{cfg.long_task_max_segments} 段）。请**从中断处继续**："
                    "先快速确认已产出的文件与已有结论，再补齐剩余工作，"
                    "**不要重做已完成的部分**。\n"
                    "若其实已接近完成，直接产出最终交付件并收尾，不要为填满时间而扩大范围。\n"
                    f"（已续跑 {nxt - 1} 次，最多 {cfg.long_task_max_segments} 段。"
                    "若确实无法在预算内完成，如实说明已完成到哪一步、还差什么，"
                    "不要静默降级糊弄。）", nxt)
                db.set(task_id, status="queued", error="", resume_hint=hint,
                       blocked_cmd="")
                bus.publish(task_id, t="status", s="queued", segment=nxt)
                audit.line(task_id, row["user"], "segment_continue",
                           f"{nxt}/{cfg.long_task_max_segments} "
                           f"elapsed={res.elapsed:.0f}s files={len(scan['all'])} "
                           f"text={len(res.text or '')} tools={res.tool_calls}")
                # 续跑段降级排队（prio=2）并归入 long lane：它已产出过一部分，边际
                # 紧迫度低于用户刚发来的新活；降级同时把「续跑放大 3 倍耗时」摊开。
                await sched.submit(task_id, "continuation", long=True)
                return
            audit.line(task_id, row["user"], "segment_stop",
                       f"本段无进展，不续跑（seg={seg} elapsed={res.elapsed:.0f}s "
                       f"files={len(scan['all'])} text={len(res.text or '')} "
                       f"tools={res.tool_calls}）")
        seg_progress.pop(task_id, None)
        low_risk_done = auto_done_reason(cfg, row, scan, res.text,
                                         real_conflicts, ro_overwritten)
        if res.error == "cancelled":
            status, err = "cancelled", "cancelled by user"
        elif _restart_interrupted(shutting_down, res.timed_out, res.exit_code):
            # 计划内重启打断：不是任务的错，退回 queued 让启动恢复逻辑接回去
            # （lifespan 开头把 queued 重新 submit，段号从 resume_hint 还原，故续跑中
            # 的长任务重启后仍归 long lane）。**不发 failed 事件**——否则分身会回用户
            # 一句「❌ 没办成」，而其实几秒后它就接着跑了。
            # 排除 timed_out：超时也是 SIGTERM(143)，但那是任务自己的预算用尽，
            # 重新排队只会再超时一次。
            db.set(task_id, status="queued", error="服务重启，任务重新排队",
                   blocked_cmd="")
            bus.publish(task_id, t="status", s="queued", reason="restart")
            audit.line(task_id, row["user"], "restart_requeue",
                       f"exit={res.exit_code}({signal_name(res.exit_code)}) "
                       f"elapsed={res.elapsed:.0f}s seg={seg} "
                       f"tools={res.tool_calls}")
            # cancel 事件必须清掉：run_task 用 setdefault 复用同一事件，若关闭瞬间
            # 恰好有人取消（事件已 set），重新排队的任务会一启动就被自己杀掉。
            cancel_events.pop(task_id, None)
            auto_deny_counts.pop(task_id, None)
            return
        elif res.error:
            status, err = "failed", res.error
        elif real_conflicts or ro_overwritten:
            status, err = "conflict", ""
        elif not scan["all"]:
            status, err = "done", ("自动done（仅工具缓存变动，无实质产出）"
                                   if scan.get("volatile") else "自动done（只读，无文件改动）")
        elif low_risk_done:
            status, err = "done", low_risk_done
        else:
            status, err = "review", ""
        db.set(task_id, status=status, error=err, finished_at=finished,
               conflicts=json.dumps(conflicts, ensure_ascii=False) if conflicts else "[]")
        bus.publish(task_id, t="status", s=status,
                    changed=scan["all"][:200], conflicts=conflicts)
        audit.line(task_id, row["user"], "stop", status)
        if low_risk_done and status == "done":
            audit.line(task_id, row["user"], "auto_done_low_risk",
                       f"{len(scan.get('created') or [])} files: "
                       + ", ".join((scan.get("created") or [])[:5]))
        cancel_events.pop(task_id, None)
        auto_deny_counts.pop(task_id, None)   # 终态即清，防计数表随任务数无界增长
        if status in TERMINAL:
            ws.cleanup_terminal(row)

    def _who(row: dict) -> str:
        """任务归属的**展示名**：优先真实发起人，回落 token 身份。

        avatar 全机队共用一个 relay token，故 user 恒为机器人「鲁大师」。冲突提示
        若只写 user，同一人重复提交造成的自我撞车会被写成「与鲁大师冲突」——
        2026-10-10 排障时被这条误导了整整一轮（先怀疑跨任务竞争，实际是同一
        发起人 6 分钟内从三个会话各触发一次）。
        """
        r = str((row or {}).get("requester") or "").strip()
        u = str((row or {}).get("user") or "").strip()
        if r and r != u:
            return f"{r}（经 {u}）"
        return r or u or "?"

    def _check_conflicts(row: dict, scan: dict, finished_at: float) -> list[dict]:
        """同文件冲突判定：仅比对时间窗内（conflict_window_hours）仍有活动的任务。

        窗外的先后写入属正常演进（后者基于前者产出继续改），不置 conflict；
        工具自动重写的缓存/凭据已在 scan 阶段剔除，不会进入 scan["all"]。
        窗内命中后还要过**内容哈希**这一关：字节相同即无覆盖损失，不算冲突
        （见 Workspace.identical_version）。判定依据是内容分歧，不是路径重合。
        """
        conflicts: list[dict] = []
        win_h = cfg.conflict_window_hours
        since = finished_at - win_h * 3600
        for rel in scan["all"]:
            for other in db.tasks_with_file(rel, exclude=row["id"], since=since):
                if other["status"] == "cancelled":
                    continue
                if not other["changed_files"]:
                    continue
                if ws.identical_version(row, other, rel):
                    # 内容字节相同 → 无覆盖损失，不是真冲突。同一需求经多个会话
                    # 重复触发时，各任务往往收敛出完全一致的产出，此前会被误判
                    # conflict/review 并卡死在人工核查（且不外发），发起人只看到沉默。
                    audit.line(row["id"], row["user"], "conflict_skipped",
                               f"{rel} 与 #{other['id']} 内容一致")
                    continue
                entry = {
                    "file": rel,
                    "with_task": other["id"],
                    "with_user": _who(other),
                    "backup": "",
                    "note": f"两个任务在 {win_h:g}h 时间窗内都改动了同一文件",
                }
                if other.get("finished_at") and other["finished_at"] < finished_at:
                    bak, why = ws.backup_version(other, rel)
                    entry["backup"] = bak
                    if bak:
                        entry["note"] = (f"任务 #{other['id']}（{_who(other)}）先完成，"
                                         f"其版本已备份，磁盘保留后写入版本")
                    else:
                        entry["note"] = (f"任务 #{other['id']}（{_who(other)}）先完成，未备份"
                                         f"（{why or '源版本已不在 .result 中'}）")
                else:
                    entry["note"] = (f"任务 #{other['id']}（{_who(other)}）"
                                     f"同时段也在改动该文件，完成时将互相备份")
                if not any(c["with_task"] == other["id"] and c["file"] == rel for c in conflicts):
                    conflicts.append(entry)
                oc = list(other["conflicts"])
                back = {
                    "file": rel,
                    "with_task": row["id"],
                    "with_user": _who(row),
                    "note": "有其它任务也在改动该文件，请人工核查",
                }
                if not any(c.get("with_task") == row["id"] and c.get("file") == rel for c in oc):
                    oc.append(back)
                    db.set(other["id"], conflicts=oc)
        return conflicts

    def _on_run_error(task_id: int, exc: Exception) -> None:
        row = db.task(task_id)
        if row and row["status"] not in TERMINAL:
            db.set(task_id, status="failed", error=f"internal error: {exc!r}",
                   finished_at=time.time())
            bus.publish(task_id, t="status", s="failed", error=str(exc))
            ws.cleanup_terminal(row)
        audit.line(task_id, row["user"] if row else "?", "error", repr(exc))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal sched, shutting_down
        # long lane 只在 mode=on 时生效：shadow/notify 阶段必须**零行为变化**，
        # 否则影子期测出来的精确率/召回就不是"若启用会怎样"而是被调度改动污染过的。
        lane_on = cfg.long_task_mode == "on"
        sched = Scheduler(cfg.max_concurrent, run_fn=run_task, on_error=_on_run_error,
                          long_lane_cap=cfg.long_lane_cap if lane_on else 0,
                          long_wait_promote_sec=cfg.long_wait_promote_sec)
        await sched.start()
        # 重启恢复：进程重启后旧子进程必死，DB 里 queued/running 的任务重新入队
        for t in db.active_tasks():
            if t["status"] == "running":
                db.set(t["id"], status="queued", error="服务重启，任务重新排队")
        # 收尾竞态兜底：systemd KillMode=control-group 把 SIGTERM **同时**发给整个
        # cgroup，agent 子进程可能在 lifespan 置 shutting_down 之前就死了，于是被
        # 上一轮进程判成 failed（#151 实测：status=failed，error 却明写 SIGTERM）。
        # 故按 finished_at 把刚因信号终止的行接回 queued。只认信号打死：agent 自己
        # 报错退出（code 1 等）与超时（error 不含退出码模式）都不在此列。
        for t in db.failed_since(time.time() - RESTART_RECOVER_WINDOW_SEC):
            code = _exit_code_of(t.get("error") or "")
            if not signal_name(code):
                continue
            db.set(t["id"], status="queued", error="服务重启，任务重新排队",
                   finished_at=None, blocked_cmd="")
            audit.line(t["id"], t["user"], "restart_requeue_boot",
                       f"exit={code}({signal_name(code)}) "
                       f"prev_error={(t.get('error') or '')[:120]}")
        for t in db.active_tasks():
            if t["status"] == "queued":
                # 续跑段带 long=True 重新入队：进程内的长任务标记随重启丢了，
                # 但段号编码在 resume_hint 里（刻意不新增 DB 列），据此还原归类，
                # 否则重启后 lane 会漏掉正在续跑的长任务。
                seg = _segment_of(t.get("resume_hint") or "")
                await sched.submit(t["id"], t.get("priority") or "normal",
                                   long=seg > 0)
        stop = asyncio.Event()

        async def sweeper():
            while True:
                try:
                    sweep_stale(db, bus, audit, cfg, ws)
                except Exception as e:  # 清扫异常不拖垮服务
                    audit.line(0, "__sweeper__", "error", repr(e))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.sweep_interval_sec)
                except asyncio.TimeoutError:
                    continue
                break  # stop 已置位，退出

        sweeper_task = asyncio.create_task(sweeper())
        yield
        # 必须在 sched.stop() **之前**置位：stop() 会 cancel 在跑的任务，被 cancel 的
        # run_task 随即走到终态判定，那时就要能看出"这是计划内重启，不是任务失败"。
        shutting_down = True
        stop.set()
        await sweeper_task
        await sched.stop()
        audit.close()
        db.close()

    app = FastAPI(title="team-agent relay", lifespan=lifespan)

    # 设备签名认证：nonce 缓存（防时间窗内重放）与旧式 bearer 计数（P3 关闭 legacy 的依据）
    _seen_nonces: dict[tuple[str, str], float] = {}   # (device_id, nonce) → 过期时刻
    _legacy_auths: dict[str, int] = {}                # user → 旧式认证次数

    def _prune_nonces(now: float) -> None:
        for k in [k for k, exp in _seen_nonces.items() if exp < now]:
            _seen_nonces.pop(k, None)

    async def _device_auth(request: Request, dev_id: str, ts_s: str,
                           nonce: str, sig: str) -> dict:
        now = time.time()
        try:
            ts = float(ts_s)
        except ValueError:
            raise HTTPException(status_code=401, detail="bad timestamp")
        if abs(now - ts) > cfg.sign_window:
            raise HTTPException(status_code=401, detail="timestamp outside window")
        dev = db.device_get(dev_id)
        if not dev or dev.get("revoked_at"):
            raise HTTPException(status_code=401, detail="unknown or revoked device")
        body = await request.body()
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        digest = hashlib.sha256(body).hexdigest()
        msg = f"{ts_s}\n{request.method}\n{path}\n{digest}\n{nonce}"
        want = hmac.new(dev["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
        ip = request.client.host if request.client else ""
        if not hmac.compare_digest(want, sig):
            audit.line(0, dev["user_name"], "auth_sig_fail", f"device={dev_id} ip={ip}")
            raise HTTPException(status_code=401, detail="bad signature")
        _prune_nonces(now)
        key = (dev_id, nonce)
        if key in _seen_nonces:
            audit.line(0, dev["user_name"], "auth_replay", f"device={dev_id} ip={ip}")
            raise HTTPException(status_code=401, detail="replayed request")
        _seen_nonces[key] = now + cfg.sign_window * 2
        if not dev.get("last_seen") or now - dev["last_seen"] > 60:
            db.touch_device(dev_id, ip)  # 限频回写，避免每请求一 UPDATE
        return {"name": dev["user_name"], "admin": False, "device": dev_id}

    async def auth(request: Request, authorization: str = Header(default="")) -> dict:
        dev_id = request.headers.get("x-device-id", "")
        sig = request.headers.get("x-signature", "")
        ts_s = request.headers.get("x-timestamp", "")
        nonce = request.headers.get("x-nonce", "")
        if dev_id and sig and ts_s and nonce:
            return await _device_auth(request, dev_id, ts_s, nonce, sig)
        tok = authorization.strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        if tok and tok == cfg.admin_token:
            return {"name": "__admin__", "admin": True}
        if not cfg.auth_legacy:
            raise HTTPException(status_code=401,
                                detail="bearer token auth disabled; activate a device first"
                                       " (POST /auth/activate)")
        u = db.user_by_token(tok) if tok else {}
        if not u or u.get("consumed_at"):
            raise HTTPException(status_code=401, detail="invalid or missing token")
        _legacy_auths[u["name"]] = _legacy_auths.get(u["name"], 0) + 1
        return {"name": u["name"], "admin": False}

    def require_admin(principal: dict = Depends(auth)) -> None:
        if not principal["admin"]:
            raise HTTPException(status_code=403, detail="admin token required")

    # ---- 用户/凭证 ----

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "engine": cfg.engine,
            "version": _service_version(),
            "db": db.backend,
            "active": sched.active if sched else 0,
            "queued": sched.pending if sched else 0,
            "max_concurrent": cfg.max_concurrent,
            # 长任务异步化的可观测面：影子期靠这三个字段对账（模式／lane 上限／
            # 当前占着长任务名额的数量），机队各机是否一致也一眼可比。
            "long_task_mode": cfg.long_task_mode,
            "long_lane_cap": cfg.long_lane_cap,
            "active_long": sched.active_long if sched else 0,
            "min_client_version": cfg.min_client_version or None,
            "auth_legacy": cfg.auth_legacy,
        }

    @app.get("/users")
    async def list_users(_: None = Depends(require_admin)):
        return {"users": db.list_users()}

    @app.post("/users", status_code=201)
    async def create_user(body: NewUser, _: None = Depends(require_admin)):
        if db.user_by_name(body.name):
            raise HTTPException(status_code=409, detail="user exists")
        token = "ta_" + secrets.token_urlsafe(24)
        db.create_user(body.name, token)
        return {"name": body.name, "token": token}

    @app.delete("/users/{name}")
    async def delete_user(name: str, _: None = Depends(require_admin)):
        if not db.user_by_name(name):
            raise HTTPException(status_code=404, detail="user not found")
        db.delete_user(name)
        return {"ok": True, "name": name}

    # ---- 设备激活与管理（token=一次性激活码，激活后换设备凭证签名认证） ----

    activate_fails: dict[str, list[float]] = {}  # ip -> 窗口内无效码时间戳（限频纵深）

    def _activate_locked(ip: str) -> int:
        """滑动窗口内无效激活码达上限 → 返回剩余锁定秒数；0=未锁。"""
        now = time.time()
        ts = [t for t in activate_fails.get(ip, ()) if now - t < ACTIVATE_LOCK_SEC]
        if ts:
            activate_fails[ip] = ts
        else:
            activate_fails.pop(ip, None)
        if len(ts) >= ACTIVATE_MAX_FAILS:
            return max(1, int(ACTIVATE_LOCK_SEC - (now - ts[0])) + 1)
        return 0

    @app.post("/auth/activate")
    async def activate_device(body: ActivateBody, request: Request):
        tok = body.token.strip()
        ip = request.client.host if request.client else ""
        left = _activate_locked(ip)
        if left:
            audit.line(0, "__unknown__", "activate_locked", f"ip={ip} retry={left}")
            raise HTTPException(status_code=429,
                                detail=f"无效激活码尝试过多，请 {left} 秒后再试",
                                headers={"Retry-After": str(left)})
        u = db.user_by_token(tok)
        if not u:
            activate_fails.setdefault(ip, []).append(time.time())
            audit.line(0, "__unknown__", "activate_fail", f"invalid token ip={ip}")
            raise HTTPException(status_code=401, detail="invalid token")
        if u.get("consumed_at") or not db.consume_token(tok):
            audit.line(0, u["name"], "activate_fail", f"token consumed ip={ip}")
            raise HTTPException(status_code=409,
                                detail="token 已被激活使用（一次性）；请联系管理员补发")
        activate_fails.pop(ip, None)
        dev_id = body.device_id.strip() or ("dev_" + secrets.token_urlsafe(12))
        dev_name = body.device_name.strip()[:60]
        secret = secrets.token_urlsafe(32)
        db.device_upsert(dev_id, u["name"], dev_name, secret, ip)
        audit.line(0, u["name"], "device_activate",
                   f"device={dev_id} name={dev_name} ip={ip}")
        return {"ok": True, "user": u["name"],
                "device_id": dev_id, "device_secret": secret}

    @app.post("/users/{name}/token", status_code=201)
    async def reissue_user_token(name: str, _: None = Depends(require_admin)):
        if not db.user_by_name(name):
            raise HTTPException(status_code=404, detail="user not found")
        token = "ta_" + secrets.token_urlsafe(24)
        db.reissue_token(name, token)
        audit.line(0, "__admin__", "token_reissue", name)
        return {"name": name, "token": token,
                "note": "一次性激活码，激活后失效；仅本次显示。已激活设备不受影响"}

    @app.get("/users/{name}/devices")
    async def list_user_devices(name: str, _: None = Depends(require_admin)):
        return {"devices": db.devices_by_user(name)}

    @app.delete("/users/{name}/devices/{device_id}")
    async def revoke_user_device(name: str, device_id: str,
                                 _: None = Depends(require_admin)):
        if not db.revoke_device(name, device_id):
            raise HTTPException(status_code=404, detail="device not found or already revoked")
        audit.line(0, "__admin__", "device_revoke", f"{name}/{device_id}")
        return {"ok": True, "name": name, "device_id": device_id}

    # ---- 任务 ----

    @app.post("/tasks", status_code=202)
    async def submit_task(body: NewTask, principal: dict = Depends(auth)):
        if body.priority not in ("normal", "urgent"):
            raise HTTPException(status_code=422, detail="priority must be normal|urgent")
        if body.backend not in ("relay", "local"):
            raise HTTPException(status_code=422, detail="backend must be relay|local")
        if body.backend == "local":
            # 影子记录：只登记不派发。**绝不调 sched.submit**——relay 不执行它，
            # 它也没有 workdir/baseline。直接以 running 落库，终态由 avatar 经
            # POST /tasks/{id}/local_status 回报；avatar 若死掉由 sweep_stale 兜底。
            task_id = db.create_task(
                user=principal["name"], description=body.description,
                project=body.project or "avatar-local", targets=[],
                priority=body.priority, resume_from=None, read_only=True,
                status="running", engine="local", started_at=time.time(),
                requester=body.requester[:40])
            audit.line(task_id, principal["name"], "submit_local",
                       body.description[:120])
            bus.publish(task_id, t="status", s="running")
            return {"task_id": task_id, "status": "running",
                    "backend": "local", "warnings": None}
        if body.resume_from is not None and not db.task(body.resume_from):
            raise HTTPException(status_code=404, detail="resume_from task not found")
        read_only = body.read_only or _declares_read_only(body.description)
        warnings = []
        if body.targets and not read_only:  # 纯读任务直接放行（事后扫描兜底）
            warnings = find_conflicts(body.targets, db.active_tasks())
            if warnings and not body.force:
                raise HTTPException(status_code=409, detail={
                    "error": "targets overlap with in-flight tasks",
                    "overlaps": warnings,
                    "hint": "确认无误后带 force=true 重新提交，或等待在途任务完成；"
                            "纯读任务在描述中注明「只读」即可直接放行",
                })
        task_id = db.create_task(
            user=principal["name"],
            description=body.description,
            project=body.project,
            targets=body.targets,
            priority=body.priority,
            resume_from=body.resume_from,
            read_only=read_only,
            requester=body.requester[:40],
        )
        audit.line(task_id, principal["name"], "submit", body.description[:120])
        bus.publish(task_id, t="status", s="queued")
        await sched.submit(task_id, body.priority)
        return {"task_id": task_id, "status": "queued", "warnings": warnings or None}

    @app.get("/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50,
                         _: dict = Depends(auth)):
        return {"tasks": db.list_tasks(limit=limit, status=status)}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: int, _: dict = Depends(auth)):
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        return row

    @app.get("/tasks/{task_id}/diff")
    async def task_diff(task_id: int, _: dict = Depends(auth)):
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task_id": task_id, "changed_files": row["changed_files"],
                "conflicts": row["conflicts"]}

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: int, principal: dict = Depends(auth)):
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] == "queued":
            db.set(task_id, status="cancelled", error="cancelled before start")
            audit.line(task_id, principal["name"], "cancel", "queued")
            bus.publish(task_id, t="status", s="cancelled")
            return {"task_id": task_id, "status": "cancelled"}
        if row["status"] == "running":
            cancel_events.setdefault(task_id, asyncio.Event()).set()
            audit.line(task_id, principal["name"], "cancel", "running")
            return {"task_id": task_id, "status": "cancelling"}
        raise HTTPException(status_code=409,
                            detail=f"task already {row['status']}, cannot cancel")

    @app.post("/tasks/{task_id}/local_status")
    async def local_status(task_id: int, body: LocalStatus,
                           principal: dict = Depends(auth)):
        """local 影子记录的终态回报（avatar → relay）。

        幂等：已是终态则原样返回、不重复写——avatar 重试与 sweep_stale 兜底可能
        并发到达，谁先落谁算，后者不得覆盖（否则一次崩溃会被记成两种结局）。
        只接受 engine='local' 的记录：普通 relay 任务的状态由 executor 自己写，
        外部不得改。
        """
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row.get("engine") != "local":
            raise HTTPException(status_code=409,
                                detail="not a local shadow record")
        if body.status not in ("done", "failed", "cancelled"):
            raise HTTPException(status_code=422,
                                detail="status must be done|failed|cancelled")
        if row["status"] in ("done", "failed", "cancelled", "conflict"):
            return {"task_id": task_id, "status": row["status"],
                    "already_terminal": True}
        fields: dict = {"status": body.status, "finished_at": time.time()}
        if body.result:
            fields["result"] = body.result
        if body.error:
            fields["error"] = body.error
        if body.tokens:
            fields["tokens"] = body.tokens
        if body.cost:
            fields["cost"] = body.cost
        if body.changed_files:      # agent 自报，未经 baseline 核验
            fields["changed_files"] = body.changed_files
        db.set(task_id, **fields)
        audit.line(task_id, principal["name"], "local_status", body.status)
        bus.publish(task_id, t="status", s=body.status)
        return {"task_id": task_id, "status": body.status,
                "already_terminal": False}

    @app.post("/tasks/{task_id}/confirm")
    async def confirm_task(task_id: int, body: ConfirmBody, _: dict = Depends(auth)):
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] not in ("review", "conflict"):
            raise HTTPException(status_code=409,
                                detail=f"task is {row['status']}, confirm only from review/conflict")
        if body.outcome not in ("done", "conflict"):
            raise HTTPException(status_code=422, detail="outcome must be done|conflict")
        db.set(task_id, status=body.outcome)
        bus.publish(task_id, t="status", s=body.outcome)
        audit.line(task_id, row["user"], "confirm", body.outcome)
        ws.cleanup_terminal(row)
        return db.task(task_id)

    async def _do_approve(task_id: int, decision: str, who: str) -> dict:
        """审批 pending_approval 任务（放行续跑 / 拒绝置 failed）。

        放行：带 resume_hint 重新入队，exempt_cmd=blocked_cmd 让同一条命令
        这次过门控（_exempt 按发送目标签名匹配，agent resume 重排格式也不漏判）。
        由 token 认证端点与 dashboard（cookie）端点共用，逻辑单一。
        """
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] != "pending_approval":
            raise HTTPException(status_code=409,
                                detail=f"task is {row['status']}, only pending_approval can be approved")
        if decision not in ("approve", "deny"):
            raise HTTPException(status_code=422, detail="decision must be approve|deny")
        if decision == "deny":
            db.set(task_id, status="failed", error="user denied blocked command",
                   finished_at=time.time())
            bus.publish(task_id, t="status", s="failed", error="user denied blocked command")
            audit.line(task_id, who, "deny", (row["blocked_cmd"] or "")[:200])
            return db.task(task_id)
        hint = (f"上一次执行因高危命令被拦截，人工已批准执行以下命令：\n{row['blocked_cmd']}\n"
                "请从该命令继续完成原任务。")
        db.set(task_id, status="queued", error="", resume_hint=hint)
        audit.line(task_id, who, "approve", (row["blocked_cmd"] or "")[:200])
        bus.publish(task_id, t="status", s="queued")
        await sched.submit(task_id, row["priority"])
        return {"task_id": task_id, "status": "queued"}

    @app.post("/tasks/{task_id}/approve")
    async def approve_task(task_id: int, body: ApproveBody, principal: dict = Depends(auth)):
        return await _do_approve(task_id, body.decision, principal["name"])

    @app.post("/dashboard/tasks/{task_id}/approve")
    async def dashboard_approve(task_id: int, body: ApproveBody, request: Request):
        """监控页（cookie 登录）审批入口：放行/拒绝 pending_approval 任务。"""
        _dash_auth(request)
        return await _do_approve(task_id, body.decision, "__dashboard__")

    @app.get("/tasks/{task_id}/stream")
    async def stream_task(task_id: int, _: dict = Depends(auth)):
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        history, q = bus.subscribe(task_id)

        async def gen():
            try:
                async def emit(ev: dict):
                    payload = json.dumps(ev, ensure_ascii=False)
                    yield f"data: {payload}\n\n"

                for ev in history:
                    async for chunk in emit(ev):
                        yield chunk
                    if ev.get("t") == "status" and ev.get("s") in STREAM_END:
                        return
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    async for chunk in emit(ev):
                        yield chunk
                    if ev.get("t") == "status" and ev.get("s") in STREAM_END:
                        return
            finally:
                bus.unsubscribe(task_id, q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- 计划（定时任务）----
    # 大脑/成员只写声明（POST /plans），root 侧 relay-plan.timer 每分钟扫库、
    # 原子认领并 POST /tasks 触发。大脑全程不碰 systemctl / 不写 /etc，
    # relay.service 的 ProtectSystem=strict 硬边界原样保留。

    def _plan_owner_ok(principal: dict, plan: dict) -> bool:
        return bool(principal.get("admin")) or principal.get("name") == plan["owner_name"]

    @app.post("/plans", status_code=201)
    async def create_plan(body: NewPlan, principal: dict = Depends(auth)):
        name = (body.name or "").strip()
        if not plan_util.NAME_RE.match(name):
            raise HTTPException(status_code=422,
                                detail="name 需为 1-64 个字母/数字/中文/._- 字符")
        if db.plan_by_name(name):
            raise HTTPException(status_code=409, detail="plan name exists")
        err = plan_util.validate_action(body.action)
        if err:
            raise HTTPException(status_code=422, detail=err)
        on_cal = (body.on_calendar or "").strip()
        nxt, cal_err = plan_util.next_elapse(on_cal)
        if nxt is None:
            raise HTTPException(status_code=422, detail=f"on_calendar 无效: {cal_err}")
        plan_id = db.create_plan(name=name, owner_name=principal["name"],
                                 on_calendar=on_cal, action=body.action,
                                 enabled=body.enabled,
                                 next_run=nxt if body.enabled else None)
        audit.line(0, principal["name"], "plan_create", f"{name} @ {on_cal}")
        return db.plan(plan_id)

    @app.get("/plans")
    async def list_plans(_: dict = Depends(auth)):
        return {"plans": db.list_plans()}

    @app.get("/plans/due")
    async def plans_due(_: None = Depends(require_admin)):
        return {"plans": db.due_plans(time.time())}

    @app.get("/plans/{plan_id}")
    async def get_plan(plan_id: int, _: dict = Depends(auth)):
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        return p

    @app.patch("/plans/{plan_id}")
    async def patch_plan(plan_id: int, body: PlanPatch, principal: dict = Depends(auth)):
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        if not _plan_owner_ok(principal, p):
            raise HTTPException(status_code=403, detail="only owner or admin")
        fields: dict = {}
        if body.name is not None:
            nm = body.name.strip()
            if not plan_util.NAME_RE.match(nm):
                raise HTTPException(status_code=422, detail="bad name")
            other = db.plan_by_name(nm)
            if other and other["id"] != plan_id:
                raise HTTPException(status_code=409, detail="plan name exists")
            fields["name"] = nm
        if body.action is not None:
            err = plan_util.validate_action(body.action)
            if err:
                raise HTTPException(status_code=422, detail=err)
            fields["action"] = body.action
        if body.on_calendar is not None:
            on_cal = body.on_calendar.strip()
            nxt, cal_err = plan_util.next_elapse(on_cal)
            if nxt is None:
                raise HTTPException(status_code=422, detail=f"on_calendar 无效: {cal_err}")
            fields["on_calendar"] = on_cal
            fields["next_run"] = nxt
        if body.enabled is not None:
            fields["enabled"] = body.enabled
            if body.enabled:
                if "next_run" not in fields:  # 重新启用且未改日历 → 重算下次触发
                    nxt, cal_err = plan_util.next_elapse(p["on_calendar"])
                    if nxt is None:
                        raise HTTPException(status_code=422,
                                            detail=f"on_calendar 无效: {cal_err}")
                    fields["next_run"] = nxt
            else:
                fields["next_run"] = None  # 停用即不再到点
        if not fields:
            return p
        db.update_plan(plan_id, **fields)
        audit.line(0, principal["name"], "plan_update",
                   f"{p['name']} {sorted(fields.keys())}")
        return db.plan(plan_id)

    @app.delete("/plans/{plan_id}")
    async def delete_plan(plan_id: int, principal: dict = Depends(auth)):
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        if not _plan_owner_ok(principal, p):
            raise HTTPException(status_code=403, detail="only owner or admin")
        db.delete_plan(plan_id)
        audit.line(0, principal["name"], "plan_delete", p["name"])
        return {"ok": True, "id": plan_id}

    @app.post("/plans/{plan_id}/fire")
    async def fire_plan(plan_id: int, _: None = Depends(require_admin)):
        """tick 专用：原子认领（推进 next_run）后提交任务。先认领后提交 → 至多一次，
        即使 tick 重入/并发也不会双发。"""
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        if not p["enabled"]:
            raise HTTPException(status_code=409, detail="plan disabled")
        now = time.time()
        nxt, cal_err = plan_util.next_elapse(p["on_calendar"])
        if nxt is None:
            # 日历已坏：停用，避免每分钟空转刷日志
            db.update_plan(plan_id, enabled=False, next_run=None)
            audit.line(0, "__plan_tick__", "plan_disable_bad_calendar",
                       f"{p['name']}: {cal_err}")
            raise HTTPException(status_code=422, detail=f"on_calendar 无效: {cal_err}")
        if not db.claim_plan(plan_id, now, nxt):
            return {"skipped": True, "id": plan_id}  # 已被认领/未到点
        action = p["action"] or {}
        if action.get("type") != "task":
            audit.line(0, "__plan_tick__", "plan_bad_action", p["name"])
            return {"skipped": True, "id": plan_id, "reason": "bad action"}
        task_id = db.create_task(
            user=p["owner_name"],
            description=action["description"],
            project=action.get("project", ""),
            targets=action.get("targets", []),
            priority=action.get("priority", "normal"),
            read_only=bool(action.get("read_only", False)),
        )
        db.update_plan(plan_id, last_task_id=task_id)
        audit.line(task_id, p["owner_name"], "plan_fire", p["name"])
        bus.publish(task_id, t="status", s="queued")
        await sched.submit(task_id, action.get("priority", "normal"))
        return {"task_id": task_id, "next_run": nxt}

    # ---- 文件上传/下载（共享区，单文件 ≤10MB） ----

    @app.post("/files", status_code=201)
    async def upload_file(body: UploadFileBody, principal: dict = Depends(auth)):
        full = _shared_path(cfg.shared_dir, body.path)
        try:
            data = base64.b64decode(body.content_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid base64 content")
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"file size {len(data)} exceeds limit {MAX_FILE_BYTES} bytes (10MB)")
        os.makedirs(os.path.dirname(full) or cfg.shared_dir, exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        audit.line(0, principal["name"], "file_upload",
                   f"{body.path} ({len(data)}B sha256={hashlib.sha256(data).hexdigest()})")
        return {"ok": True, "path": body.path, "size": len(data)}

    @app.get("/files")
    async def download_file(path: str, principal: dict = Depends(auth)):
        full = _shared_path(cfg.shared_dir, path)
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail="file not found")
        size = os.path.getsize(full)
        if size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"file size {size} exceeds limit {MAX_FILE_BYTES} bytes (10MB)")
        with open(full, "rb") as f:
            data = f.read()
        audit.line(0, principal["name"], "file_download", path)
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{os.path.basename(full)}"'})

    # ---- 共享知识（skills / MCP / memory）：个人上传即团队可用，下次任务自动加载 ----

    def _check_shared_name(name: str) -> str:
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}", name):
            raise HTTPException(status_code=400,
                                detail="name: 1-50 chars, letters/digits/._-, start alphanumeric")
        return name

    def _shared_skill_dir(name: str) -> str:
        base = os.path.realpath(os.path.join(cfg.shared_dir, "skills"))
        full = os.path.realpath(os.path.join(base, name))
        if full != base and not full.startswith(base + os.sep):
            raise HTTPException(status_code=400, detail="path escapes skills dir")
        return full

    @app.get("/dashboard/shared")
    async def dashboard_shared(request: Request):
        _dash_auth(request)
        skills, mcp, memory = [], [], []
        sroot = os.path.realpath(os.path.join(cfg.shared_dir, "skills"))
        if os.path.isdir(sroot):
            for n in sorted(os.listdir(sroot)):
                d = os.path.join(sroot, n)
                if not os.path.isdir(d):
                    continue
                files: list[str] = []
                for dirpath, dirnames, filenames in os.walk(d):
                    dirnames[:] = [x for x in dirnames if x != "__pycache__"]
                    for fn in filenames:
                        files.append(os.path.relpath(os.path.join(dirpath, fn), d))
                desc = ""
                sp = os.path.join(d, "SKILL.md")
                if os.path.isfile(sp):
                    try:
                        with open(sp, encoding="utf-8", errors="replace") as f:
                            m = re.search(r"^description:\s*(.+)$", f.read(), re.M)
                        if m:
                            desc = m.group(1).strip()
                    except OSError:
                        pass
                skills.append({"name": n, "files": sorted(files), "description": desc[:160]})
        mroot = os.path.realpath(os.path.join(cfg.shared_dir, "mcp"))
        if os.path.isdir(mroot):
            for fn in sorted(os.listdir(mroot)):
                if not fn.endswith(".json"):
                    continue
                name = fn[:-5]
                try:
                    with open(os.path.join(mroot, fn), encoding="utf-8") as f:
                        obj = json.load(f)
                except (OSError, ValueError):
                    obj = None
                entry = shared_knowledge.mcp_entry_from_file(name, obj) or obj or {}
                summ = ""
                if isinstance(entry, dict):
                    if entry.get("command"):
                        summ = " ".join(str(x) for x in entry["command"][:3])
                    elif entry.get("url"):
                        summ = str(entry["url"])
                mcp.append({"name": name, "config": entry, "summary": summ})
        memroot = os.path.realpath(os.path.join(cfg.shared_dir, "memory"))
        if os.path.isdir(memroot):
            for fn in sorted(os.listdir(memroot)):
                p = os.path.join(memroot, fn)
                if os.path.isfile(p):
                    memory.append({
                        "name": fn[:-3] if fn.endswith(".md") else fn,
                        "size": os.path.getsize(p),
                        "mtime": int(os.path.getmtime(p)),
                    })
        return {"skills": skills, "mcp": mcp, "memory": memory}

    @app.post("/dashboard/shared/skills", status_code=201)
    async def shared_skill_upload(request: Request, body: SharedSkillUpload):
        _dash_auth(request)
        name = _check_shared_name(body.name)
        sdir = _shared_skill_dir(name)
        rel = (body.file or "SKILL.md").strip()
        norm = os.path.normpath(rel)
        if (not rel or rel.startswith("/") or "\x00" in rel
                or ".." in norm.split(os.sep)):
            raise HTTPException(status_code=400, detail="bad file path (relative, no ..)")
        full = os.path.realpath(os.path.join(sdir, norm))
        if not full.startswith(sdir + os.sep):
            raise HTTPException(status_code=400, detail="path escapes skill dir")
        try:
            data = base64.b64decode(body.content_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid base64 content")
        if len(data) > MAX_SHARED_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"file size {len(data)} exceeds limit {MAX_SHARED_BYTES} bytes (1MB)")
        os.makedirs(os.path.dirname(full) or sdir, exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        audit.line(0, "__dashboard__", "shared_skill_upload",
                   f"{name}/{norm} ({len(data)}B sha256={hashlib.sha256(data).hexdigest()})")
        return {"ok": True, "name": name, "file": norm, "size": len(data),
                "note": "立即生效：下一个任务自动加载"}

    @app.delete("/dashboard/shared/skills/{name}")
    async def shared_skill_delete(name: str, request: Request):
        _dash_auth(request)
        name = _check_shared_name(name)
        d = _shared_skill_dir(name)
        if not os.path.isdir(d):
            raise HTTPException(status_code=404, detail="skill not found")
        shutil.rmtree(d, ignore_errors=True)
        audit.line(0, "__dashboard__", "shared_skill_delete", name)
        return {"ok": True, "name": name, "note": "立即生效：下一个任务起不再加载"}

    @app.post("/dashboard/shared/mcp", status_code=201)
    async def shared_mcp_upload(request: Request, body: SharedMcpUpload):
        _dash_auth(request)
        name = _check_shared_name(body.name)
        if not isinstance(body.config, dict) or not body.config:
            raise HTTPException(status_code=422, detail="config must be a non-empty JSON object")
        root = os.path.join(cfg.shared_dir, "mcp")
        os.makedirs(root, exist_ok=True)
        raw = json.dumps(body.config, ensure_ascii=False, indent=2).encode("utf-8")
        with open(os.path.join(root, name + ".json"), "wb") as f:
            f.write(raw)
        audit.line(0, "__dashboard__", "shared_mcp_upload",
                   f"{name} ({len(raw)}B sha256={hashlib.sha256(raw).hexdigest()})")
        return {"ok": True, "name": name, "note": "立即生效：下一个任务自动加载"}

    @app.delete("/dashboard/shared/mcp/{name}")
    async def shared_mcp_delete(name: str, request: Request):
        _dash_auth(request)
        name = _check_shared_name(name)
        p = os.path.realpath(os.path.join(cfg.shared_dir, "mcp", name + ".json"))
        if not os.path.isfile(p):
            raise HTTPException(status_code=404, detail="mcp not found")
        os.unlink(p)
        audit.line(0, "__dashboard__", "shared_mcp_delete", name)
        return {"ok": True, "name": name, "note": "立即生效：下一个任务起不再加载"}

    @app.post("/dashboard/shared/memory", status_code=201)
    async def shared_memory_upload(request: Request, body: SharedMemoryUpload):
        _dash_auth(request)
        name = _check_shared_name(body.name)
        if len(body.content.encode("utf-8")) > MAX_SHARED_BYTES:
            raise HTTPException(status_code=413, detail="content exceeds 1MB")
        root = os.path.join(cfg.shared_dir, "memory")
        os.makedirs(root, exist_ok=True)
        raw = body.content.encode("utf-8")
        with open(os.path.join(root, name + ".md"), "wb") as f:
            f.write(raw)
        audit.line(0, "__dashboard__", "shared_memory_upload",
                   f"{name} ({len(raw)}B sha256={hashlib.sha256(raw).hexdigest()})")
        return {"ok": True, "name": name,
                "note": "立即生效：下一个任务起自动注入全员 agent 的 AGENTS.md"}

    @app.delete("/dashboard/shared/memory/{name}")
    async def shared_memory_delete(name: str, request: Request):
        _dash_auth(request)
        name = _check_shared_name(name)
        p = os.path.realpath(os.path.join(cfg.shared_dir, "memory", name + ".md"))
        if not os.path.isfile(p):
            raise HTTPException(status_code=404, detail="memory not found")
        os.unlink(p)
        audit.line(0, "__dashboard__", "shared_memory_delete", name)
        return {"ok": True, "name": name, "note": "立即生效：下一个任务起不再注入"}

    @app.get("/dashboard/shared/content")
    async def shared_content(request: Request, kind: str, name: str, file: str = ""):
        _dash_auth(request)
        name = _check_shared_name(name)
        if kind == "memory":
            p = os.path.realpath(os.path.join(cfg.shared_dir, "memory", name + ".md"))
        elif kind == "mcp":
            p = os.path.realpath(os.path.join(cfg.shared_dir, "mcp", name + ".json"))
        elif kind == "skill":
            sdir = _shared_skill_dir(name)
            rel = os.path.normpath(file or "SKILL.md")
            if not file or file.startswith("/") or ".." in rel.split(os.sep):
                raise HTTPException(status_code=400, detail="bad file path")
            p = os.path.realpath(os.path.join(sdir, rel))
            if not p.startswith(sdir + os.sep):
                raise HTTPException(status_code=400, detail="path escapes skill dir")
        else:
            raise HTTPException(status_code=400, detail="kind must be skill|mcp|memory")
        if not os.path.isfile(p):
            raise HTTPException(status_code=404, detail="not found")
        with open(p, encoding="utf-8", errors="replace") as f:
            return {"name": name, "kind": kind, "content": f.read()[:200000]}

    # ---- 报表与监控 ----

    @app.get("/admin/report")
    async def admin_report(days: int = 7, _: None = Depends(require_admin)):
        rep = db.report(days)
        rep["audit_tail"] = audit.tail(10)
        rep["legacy_auths"] = dict(_legacy_auths)  # 旧式 bearer 认证计数（归零后可关 RELAY_AUTH_LEGACY）
        return rep

    @app.post("/admin/update")
    async def admin_update(_: None = Depends(require_admin)):
        """人工指令即时更新：触发 bootloader（git 拉新→测试→切换→重启→健康检查→失败回滚）。"""
        boot = cfg.boot_cmd
        if not boot or not os.path.isfile(boot):
            raise HTTPException(status_code=501,
                                detail=f"bootloader not installed: {boot or '(unset)'}")
        ok, how = _launch_boot(boot)
        if not ok:
            raise HTTPException(status_code=409, detail=how)
        audit.line(0, "__admin__", "update_trigger", how)
        return {"ok": True, "trigger": how, "version_now": _service_version(),
                "note": "更新已后台触发；轮询 GET /health 的 version 字段确认到达新 git SHA"}

    dash_sessions: dict[str, float] = {}
    login_fails: dict[str, list[float]] = {}  # ip -> 窗口内失败时间戳（登录防爆破）

    def _login_locked(ip: str) -> int:
        """滑动窗口内失败达上限 → 返回剩余锁定秒数；0=未锁。"""
        now = time.time()
        ts = [t for t in login_fails.get(ip, ()) if now - t < LOGIN_LOCK_SEC]
        if ts:
            login_fails[ip] = ts
        else:
            login_fails.pop(ip, None)
        if len(ts) >= LOGIN_MAX_FAILS:
            return max(1, int(LOGIN_LOCK_SEC - (now - ts[0])) + 1)
        return 0

    def _dash_ok(request: Request) -> bool:
        if not cfg.dashboard_password:
            return False
        now = time.time()
        for k in [k for k, exp in dash_sessions.items() if exp < now]:
            dash_sessions.pop(k, None)
        tok = request.cookies.get(DASH_COOKIE, "")
        exp = dash_sessions.get(tok)
        return bool(tok) and bool(exp and exp > now)

    def _metrics_body() -> dict:
        now = time.time()
        today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        return {
            "uptime_seconds": round(time.monotonic() - started_at, 1),
            "engine": cfg.engine,
            "max_concurrent": cfg.max_concurrent,
            "version": _service_version(),
            "active": sched.active if sched else 0,
            "queued": sched.pending if sched else 0,
            "tasks": db.status_counts(),
            "today": db.period_stats(today_start),
            "last_7d": db.period_stats(now - 7 * 86400),
            "recent_task_ids": db.recent_task_ids(60, 20),
        }

    @app.get("/metrics")
    async def metrics():
        return _metrics_body()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        if not cfg.dashboard_password:
            raise HTTPException(status_code=404, detail="not found")
        if _dash_ok(request):
            return DASH_HTML
        e = request.query_params.get("e")
        err = ""
        if e == "1":
            err = '<div class="err">口令错误，请重试</div>'
        elif e == "2":
            err = '<div class="err">尝试次数过多，请 15 分钟后再试</div>'
        return LOGIN_HTML.replace("__ERR__", err)

    @app.post("/login")
    async def dashboard_login(request: Request, password: str = Form(default="")):
        if not cfg.dashboard_password:
            raise HTTPException(status_code=404, detail="not found")
        ip = request.client.host if request.client else "?"
        left = _login_locked(ip)
        if left:
            audit.line(0, "__dashboard__", "login_locked", f"ip={ip} retry={left}")
            return HTMLResponse(
                '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
                "<title>尝试次数过多</title></head>"
                '<body style="margin:0;min-height:100vh;display:flex;align-items:center;'
                'justify-content:center;background:#0f172a;color:#e2e8f0;font:15px/1.6 '
                'system-ui,sans-serif"><div style="text-align:center">尝试次数过多，已临时锁定'
                f"<br>请约 {max(1, left // 60)} 分钟后再试</div></body></html>",
                status_code=429, headers={"Retry-After": str(left)})
        if not secrets.compare_digest(password, cfg.dashboard_password):
            fails = login_fails.setdefault(ip, [])
            fails.append(time.time())
            audit.line(0, "__dashboard__", "login_fail", f"ip={ip} n={len(fails)}")
            return RedirectResponse("/?e=1", status_code=303)
        login_fails.pop(ip, None)
        tok = secrets.token_urlsafe(24)
        dash_sessions[tok] = time.time() + DASH_TTL
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(DASH_COOKIE, tok, max_age=DASH_TTL, httponly=True,
                        samesite="strict", path="/",
                        secure=(request.url.scheme == "https"))
        return resp

    @app.get("/logout")
    async def dashboard_logout():
        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie(DASH_COOKIE, path="/")
        return resp

    @app.get("/dashboard/data")
    async def dashboard_data(request: Request, days: int = 7):
        if not cfg.dashboard_password:
            raise HTTPException(status_code=404, detail="not found")
        if not _dash_ok(request):
            raise HTTPException(status_code=401, detail="login required")
        days = 30 if days >= 30 else 7  # 只开 7/30 两档，防任意区间扫库
        tasks = db.list_tasks(limit=50)
        for t in tasks:
            t["description"] = (t.get("description") or "")[:120]
            t.pop("result", None)
            t.pop("workdir", None)
        rep = db.report(days)
        rep.pop("audit_tail", None)
        return {"metrics": _metrics_body(), "tasks": tasks, "report": rep}

    @app.get("/dashboard/tasks/{task_id}")
    async def dashboard_task_detail(task_id: int, request: Request):
        """任务详情（看板口令会话专用）：/tasks/{id} 走设备签名认证，
        浏览器 cookie 过不了，这里补一个 dashboard 认证的同源视图。
        长文本截断防撑爆页面；workdir/session_id 属内部实现，不回显。"""
        _dash_auth(request)
        row = db.task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        row = dict(row)
        for k in ("description", "result", "error"):
            v = row.get(k)
            if isinstance(v, str) and len(v) > 4000:
                row[k] = v[:4000] + "…（截断）"
        row.pop("workdir", None)
        row.pop("session_id", None)
        return row

    # ---- 凭证管理（dashboard 口令会话内，代理 admin 的 users 管理） ----

    def _dash_auth(request: Request) -> None:
        if not cfg.dashboard_password:
            raise HTTPException(status_code=404, detail="not found")
        if not _dash_ok(request):
            raise HTTPException(status_code=401, detail="login required")

    def _token_mask(tok: str) -> str:
        return tok

    @app.get("/dashboard/tokens")
    async def dashboard_tokens(request: Request):
        _dash_auth(request)
        stats = {u["user"]: u for u in db.report(7)["by_user"]}
        dev_counts = db.device_counts()
        users = []
        for u in db.list_users():
            s = stats.get(u["name"], {})
            users.append({
                "name": u["name"],
                "token": _token_mask(u["token"]),
                "created_at": u["created_at"],
                "consumed_at": u.get("consumed_at"),
                "devices": dev_counts.get(u["name"], 0),
                "tasks_7d": s.get("tasks", 0),
                "done_7d": s.get("done", 0),
            })
        return {"users": users}

    @app.post("/dashboard/tokens", status_code=201)
    async def dashboard_token_create(request: Request, body: DashTokenBody):
        _dash_auth(request)
        name = body.name.strip()
        if not name or len(name) > 50:
            raise HTTPException(status_code=422, detail="name required (1-50 chars)")
        if db.user_by_name(name):
            raise HTTPException(status_code=409, detail="user exists")
        token = "ta_" + secrets.token_urlsafe(24)
        db.create_user(name, token)
        audit.line(0, "__dashboard__", "token_create", name)
        return {"name": name, "token": token,
                "note": "token 仅本次显示，请立即发给本人保存"}

    @app.post("/dashboard/tokens/{name}/regenerate")
    async def dashboard_token_regenerate(name: str, request: Request):
        _dash_auth(request)
        if not db.user_by_name(name):
            raise HTTPException(status_code=404, detail="user not found")
        token = "ta_" + secrets.token_urlsafe(24)
        db.reissue_token(name, token)  # 保留已激活设备，仅换发激活码
        audit.line(0, "__dashboard__", "token_regenerate", name)
        return {"name": name, "token": token,
                "note": "旧激活码立即失效；已激活设备不受影响；新码仅本次显示"}

    @app.delete("/dashboard/tokens/{name}")
    async def dashboard_token_delete(name: str, request: Request):
        _dash_auth(request)
        if not db.user_by_name(name):
            raise HTTPException(status_code=404, detail="user not found")
        db.delete_user(name)
        audit.line(0, "__dashboard__", "token_delete", name)
        return {"ok": True, "name": name}

    # ---- 计划管理（dashboard 口令会话内，代理 admin 的 plans 管理） ----

    @app.get("/dashboard/plans")
    async def dashboard_plans(request: Request):
        _dash_auth(request)
        return {"plans": db.list_plans()}

    @app.get("/dashboard/timers")
    async def dashboard_timers(request: Request):
        _dash_auth(request)
        return {"timers": await asyncio.to_thread(_list_system_timers)}

    @app.post("/dashboard/plans", status_code=201)
    async def dashboard_plan_create(request: Request, body: NewPlan):
        _dash_auth(request)
        name = (body.name or "").strip()
        if not plan_util.NAME_RE.match(name):
            raise HTTPException(status_code=422,
                                detail="name 需为 1-64 个字母/数字/中文/._- 字符")
        if db.plan_by_name(name):
            raise HTTPException(status_code=409, detail="plan name exists")
        err = plan_util.validate_action(body.action)
        if err:
            raise HTTPException(status_code=422, detail=err)
        on_cal = (body.on_calendar or "").strip()
        nxt, cal_err = plan_util.next_elapse(on_cal)
        if nxt is None:
            raise HTTPException(status_code=422, detail=f"on_calendar 无效: {cal_err}")
        plan_id = db.create_plan(name=name, owner_name="__dashboard__",
                                 on_calendar=on_cal, action=body.action,
                                 enabled=body.enabled,
                                 next_run=nxt if body.enabled else None)
        audit.line(0, "__dashboard__", "plan_create", f"{name} @ {on_cal}")
        return db.plan(plan_id)

    @app.post("/dashboard/plans/{plan_id}/toggle")
    async def dashboard_plan_toggle(plan_id: int, request: Request):
        _dash_auth(request)
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        enabled = not p["enabled"]
        fields: dict = {"enabled": enabled}
        if enabled:
            nxt, cal_err = plan_util.next_elapse(p["on_calendar"])
            if nxt is None:
                raise HTTPException(status_code=422, detail=f"on_calendar 无效: {cal_err}")
            fields["next_run"] = nxt
        else:
            fields["next_run"] = None
        db.update_plan(plan_id, **fields)
        audit.line(0, "__dashboard__", "plan_toggle", f"{p['name']} -> {enabled}")
        return db.plan(plan_id)

    @app.delete("/dashboard/plans/{plan_id}")
    async def dashboard_plan_delete(plan_id: int, request: Request):
        _dash_auth(request)
        p = db.plan(plan_id)
        if not p:
            raise HTTPException(status_code=404, detail="plan not found")
        db.delete_plan(plan_id)
        audit.line(0, "__dashboard__", "plan_delete", p["name"])
        return {"ok": True, "id": plan_id}

    return app
