#!/usr/bin/env python3
"""鲁大师用户日度使用分析 · 数据采集器。

权威源＝relay 现役 PG 的 tasks/plans 表；本机 audit.log 补历史归属；
events/rounds 补消息级明细。输出 JSON facts 供渲染报告，不直接产成品。

用法：
    usage_collect.py --date 2026-10-11 [--out facts.json] [--no-pg]

═══ 口径（实测确立，勿凭直觉改）═══

1) 渠道判别看 **project**，不是 user。
   tasks.user 对分身代投的任务恒为「鲁大师」（bot 自己的 token 名），无区分力。
     project='avatar-local'  → dingtalk（本地执行）
     project='relay-avatar'  → dingtalk（relay 执行）
     project='avatar-replay' → replay（回放/测试，单列不计入人工）
     project=''  且 user='鲁大师'      → cron（plans.owner_name 全是鲁大师）
     project=''  且 user∈服务账号      → api_server（如「中鲁运营」云端运维）
     project=''  且 user∈系统账号      → system（__admin__/canary）
     project=''  且 user=真人名        → cli（本人持 token 直连 API）

2) **影子执行**：一个人工提问会落两行任务（avatar-local + relay-avatar），
   故「任务行数 ≈ 提问数 × 2」。提问数必须按 desc 核心去重，否则翻倍虚高。
   relay 侧描述会追加「--- 【投递铁律｜由分身代投…】」，去重前先剥掉。

3) 归属三级回退：requester（PG 新列，2026-10-10 起）→ audit.log 的
   task_id→actor（覆盖历史，实测 97%）→ unknown。actor 是 userId 或 openId，
   用名单的 name_by_id 反查姓名。

4) 模板要求但本机无源的维度（prediction_log / --source all CLI /
   cli·acp·飞书 独立日志）一律输出 gap 标记，不编造。

红线：绝不回显 RELAY_DB 连接串或任何凭据。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
WORKSPACE = os.environ.get("RELAY_WORKSPACE", "/mnt/vol-eltaah12/workspace")
AVATAR = os.path.join(WORKSPACE, "avatar")
RELAY_ENV = os.path.join(WORKSPACE, "relay.env")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 名单运行时缓存优先放 shared/tmp/usage/：预采集单元可写、沙箱渲染任务可读，
# 且不在技能目录内，避免 provision 覆盖后名单回退。旧 config/key_users.toml 仅作兜底。
RUNTIME_ROSTER = os.path.join(WORKSPACE, "shared", "tmp", "usage", "key-users.toml")
LEGACY_ROSTER = os.path.join(SKILL_DIR, "config", "key_users.toml")


def resolve_roster_path() -> str:
    for p in (os.environ.get("USAGE_ROSTER", ""), RUNTIME_ROSTER, LEGACY_ROSTER):
        if p and os.path.exists(p):
            return p
    return RUNTIME_ROSTER


ROSTER = resolve_roster_path()

# 本机分身展示名解析器（同目录 self_name.py）。宿主机采集时解析并写进 facts，
# 渲染端（沙箱）只读 facts.display_name，避免沙箱读不到 identity.local.json/policy.toml。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from self_name import resolve_display_name as _resolve_display_name
except Exception:                       # 解析器缺失也不让采集中断，兜底鲁大师
    def _resolve_display_name() -> str:
        return "鲁大师"

SYSTEM_ACCOUNTS = {"__admin__", "canary", "__plan_tick__", ""}
DINGTALK_PROJECTS = {"avatar-local", "relay-avatar"}
# relay 侧代投任务描述里追加的投递铁律尾巴，去重前剥掉
DESC_TAIL = re.compile(r"\s*---\s*【投递铁律.*$", re.S)

# 需求类型归类：只输出**类别**，不输出用户原文。
# 数据最小化要求（2026-10-12 用户指示）：日报文件不留存具体用户的任务内容——
# 报告要落 shared/report/ 并进台账（机队可见、长期留存），而提问原文与交付正文里
# 含预算数字、期货策略、头寸限额等经营敏感信息，不该随使用度量一起持久化。
# 故采集阶段就把原文压成类别，facts.json 与报告文件里都不出现原文。
TOPIC_RULES = [
    ("数据归档/台账", ("归档", "台账", "补登记", "落盘", "store add")),
    ("文件接收/暂存", ("已收到《", "暂存", "不支持的文件类型")),
    ("预算/目标设定", ("预算", "目标设定", "限额", "经营计划")),
    ("行情/价格预测", ("预测", "行情", "价格", "原油", "成品油", "头寸")),
    ("审核/复核", ("审核", "复核", "校验", "核对")),
    ("权限/名单查询", ("权限", "名单", "白名单", "授权")),
    ("报告/方案撰写", ("报告", "大纲", "方案", "研判", "建议")),
    ("投递/转发", ("撤回", "单独发给", "发到", "转发")),
    ("图片/附件消息", ("[图片消息]", "mediaId")),
    ("系统/元操作", ("你能做什么", "能力清单", "说人话")),
]
TOPIC_OTHER = "其他/未归类"


def topic_of(desc: str) -> str:
    """把提问原文压成需求类型类别（原文不出采集器）。"""
    d = desc or ""
    for label, kws in TOPIC_RULES:
        if any(k in d for k in kws):
            return label
    return TOPIC_OTHER


# ---------------------------------------------------------------- 基础工具
def load_env() -> None:
    if os.environ.get("RELAY_DB"):
        return
    try:
        for line in open(RELAY_ENV, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ("RELAY_DB", "RELAY_ADMIN_TOKEN"):
                os.environ.setdefault(k, v)
    except OSError:
        pass


def day_bounds(date: dt.date) -> tuple[float, float]:
    start = dt.datetime.combine(date, dt.time.min, tzinfo=TZ)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(float(ts), TZ).strftime("%H:%M:%S")


def pct(part, whole) -> float | None:
    return round(part * 100.0 / whole, 1) if whole else None


def delta(cur, prev):
    if cur is None or prev is None:
        return "-"
    d = cur - prev
    if d == 0:
        return "持平"
    return f"{d:+d}" if isinstance(d, int) else f"{d:+.1f}"


def desc_core(desc: str) -> str:
    """剥掉代投尾巴 + 归一空白，用于影子对去重。"""
    d = DESC_TAIL.sub("", desc or "")
    d = re.sub(r"\s+", " ", d).strip()
    return d[:120]


# ---------------------------------------------------------------- 名单
def load_roster(path: str | None = None) -> dict:
    """读重点用户名单 + 服务账号。极简解析，不依赖 tomllib。"""
    path = path or ROSTER
    out = {"users": [], "service_accounts": [], "name_by_id": {}, "id_by_name": {},
           "by_name": {}}
    if not os.path.exists(path):
        out["gap"] = f"名单文件缺失: {path}"
        return out
    cur, bucket = None, None
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[[user]]"):
            cur, bucket = {}, out["users"]
            bucket.append(cur)
            continue
        if line.startswith("[[service_account]]"):
            cur, bucket = {}, out["service_accounts"]
            bucket.append(cur)
            continue
        if line.startswith("["):
            cur = None
            continue
        if "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"').strip("'")
    for u in out["users"]:
        if u.get("id"):
            out["name_by_id"][u["id"]] = u.get("name") or u["id"]
        if u.get("name"):
            out["id_by_name"][u["name"]] = u.get("id", "")
            out["by_name"][u["name"]] = u
    return out


def load_audit_actor() -> dict:
    """audit.log → {task_id: actor}。取每个 task_id 最早的非空 actor。

    实测 local_shadow(325) + task_submit(113) 合计可归属 442 个 task_id，
    对 452 个人工任务覆盖 97%。
    """
    best: dict[str, tuple[str, str]] = {}
    al = os.path.join(AVATAR, "audit", "audit.log")
    if not os.path.exists(al):
        return {}
    for line in open(al, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        a = (r.get("actor") or "").strip()
        if not a:
            continue
        tid = str(r.get("task_id") or (r.get("detail") or "").lstrip("#") or "")
        if not tid.isdigit():
            continue
        ts = r.get("ts") or ""
        if tid not in best or ts < best[tid][1]:
            best[tid] = (a, ts)
    return {k: v[0] for k, v in best.items()}


def load_audit_last_seen(cutoff: float | None = None) -> dict:
    """audit.log → {actor: 截止 cutoff 前的最晚活动 epoch}。

    静默追踪必须用这个：赵兴峰等跨组织人员无 userId，PG 的 user/requester
    列查不到，但 audit 的 actor 是 openId，能查到。只用 PG 会把人误判成"无记录"。

    ⚠ cutoff 必须在**取 max 之前**过滤。若先全局取 max 再过滤，跑历史日报时
      某人今天刚活跃过，他 cutoff 前的真实活动会被整条丢掉 → 误判"无记录"。
    """
    out: dict[str, float] = {}
    al = os.path.join(AVATAR, "audit", "audit.log")
    if not os.path.exists(al):
        return out
    for line in open(al, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        a = (r.get("actor") or "").strip()
        ts = str(r.get("ts") or "").strip()
        if not a or len(ts) < 19:
            continue
        try:
            e = dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue
        if cutoff is not None and e >= cutoff:
            continue
        if e > out.get(a, 0):
            out[a] = e
    return out


# ---------------------------------------------------------------- PG 采集
def collect_pg(date: dt.date, prev_date: dt.date, roster: dict, audit_actor: dict,
               audit_last: dict, keep_content: bool = False) -> dict:
    try:
        import psycopg2
    except ImportError:
        return {"error": "psycopg2 未安装（用 relay 的 .venv 跑）"}
    dsn = os.environ.get("RELAY_DB")
    if not dsn:
        return {"error": "RELAY_DB 未配置"}
    try:
        conn = psycopg2.connect(dsn, connect_timeout=8)
    except Exception as exc:
        return {"error": f"PG 连接失败: {type(exc).__name__}"}  # 不泄露 DSN

    svc = {s.get("name") for s in roster.get("service_accounts", []) if s.get("name")}
    name_by_id = roster.get("name_by_id", {})
    roster_names = {u.get("name") for u in roster.get("users", []) if u.get("name")}
    lo, hi = day_bounds(date)
    plo, phi = day_bounds(prev_date)

    def q(cur, sql, args=()):
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    try:
        cur = conn.cursor()
        plans = q(cur, "SELECT id,name,owner_name,on_calendar,action,enabled FROM plans")
        plan_owners = {p["owner_name"] for p in plans if p["owner_name"]}
        # (owner, 计划动作描述) 指纹 → 精确识别计划触发的任务；同时供 cron 构成归并复用
        plan_sigs, desc2plan = set(), {}
        for p in plans:
            try:
                act = json.loads(p["action"]) if isinstance(p["action"], str) else (p["action"] or {})
            except Exception:
                act = {}
            d = (act.get("description") or "").strip()
            if d:
                plan_sigs.add((p["owner_name"], d))
                desc2plan[d] = p["name"]
        sel = """SELECT id,"user",requester,description,status,engine,project,result,
                        created_at,started_at,finished_at,error,tokens,cost,changed_files
                 FROM tasks WHERE created_at >= %s AND created_at < %s ORDER BY created_at"""
        tasks = q(cur, sel, (lo, hi))
        prev_tasks = q(cur, sel, (plo, phi))
        # 名单内每人最近活动：必须截到报告日 24:00，否则跑历史日报会算出负静默天数
        last_rows = q(cur, 'SELECT "user",requester,max(created_at) m FROM tasks '
                           'WHERE created_at < %s GROUP BY 1,2', (hi,))
        conn.close()
    except Exception as exc:
        return {"error": f"PG 查询失败: {type(exc).__name__}: {str(exc)[:120]}"}

    last_seen: dict[str, float] = {}
    for r in last_rows:
        for key in (r["requester"], r["user"]):
            if key and key not in SYSTEM_ACCOUNTS:
                last_seen[key] = max(last_seen.get(key, 0.0), float(r["m"] or 0))
    # 并入 audit 的 actor 维度（openId 用户 PG 查不到），并同样截到报告日 24:00
    for actor, e in audit_last.items():
        if e >= hi:
            continue
        nm = name_by_id.get(actor, actor)
        for key in (nm, actor):
            if key not in SYSTEM_ACCOUNTS:
                last_seen[key] = max(last_seen.get(key, 0.0), e)

    def classify(t: dict) -> str:
        proj = (t.get("project") or "").strip()
        user = (t.get("user") or "").strip()
        if proj in DINGTALK_PROJECTS:
            return "dingtalk"
        if proj == "avatar-replay":
            return "replay"
        # ⚠ 计划判定必须**早于**系统账号判定：计划的 owner_name 取决于建计划时用谁的
        #   凭据（admin token → __admin__，分身设备 → 鲁大师），而 __admin__ 同时在
        #   SYSTEM_ACCOUNTS 里。若先判系统账号，admin 建的计划任务会被误归 system，
        #   cron 占比就漏统计。先按 (owner, 计划动作描述) 精确匹配，再按 owner 兜底。
        desc = (t.get("description") or "").strip()
        if (user, desc) in plan_sigs or user in plan_owners:
            return "cron"
        if user in SYSTEM_ACCOUNTS:
            return "system"
        if user in svc:
            return "api_server"
        return "cli"  # 真人持自己 token 直连

    def who(t: dict) -> str:
        """真实发起人姓名。requester 优先，回退 audit actor 反查，再回退 user。"""
        r = (t.get("requester") or "").strip()
        if r:
            return r
        a = audit_actor.get(str(t.get("id")), "")
        if a:
            return name_by_id.get(a, a)  # userId/openId → 姓名
        u = (t.get("user") or "").strip()
        return u if u and u not in SYSTEM_ACCOUNTS else ""

    def summarize(items: list[dict], keep_content: bool = False) -> dict:
        by_src = collections.Counter()
        by_status = collections.Counter()
        by_engine = collections.Counter()
        humans = collections.Counter()
        questions: dict[tuple, dict] = {}   # 影子对去重
        rows_out = []
        for t in items:
            src = classify(t)
            by_src[src] += 1
            by_status[t.get("status") or "?"] += 1
            by_engine[t.get("engine") or "(空)"] += 1
            if src not in ("dingtalk", "cli"):
                continue
            w = who(t)
            if not w:
                w = "unknown"
            core = desc_core(t.get("description") or "")
            key = (w, core)
            if key in questions:
                questions[key]["task_rows"].append(t["id"])
                questions[key]["projects"].add((t.get("project") or "").strip())
                # 状态取"更终态"的那个：done > review/conflict > failed > cancelled
                order = {"done": 4, "review": 3, "conflict": 3, "failed": 2, "cancelled": 1}
                if order.get(t.get("status"), 0) > order.get(questions[key]["status"], 0):
                    questions[key]["status"] = t.get("status")
                if t.get("finished_at") and (not questions[key]["end"] or
                                             t["finished_at"] > questions[key]["end_ts"]):
                    questions[key]["end"] = fmt_ts(t["finished_at"])
                    questions[key]["end_ts"] = float(t["finished_at"])
                # 影子对里哪一行有产出就用哪一行的（仅排障模式下才留原文）
                if keep_content and not questions[key].get("_result_head"):
                    questions[key]["_result_head"] = re.sub(
                        r"\s+", " ", (t.get("result") or "")).strip()[:400]
                if not questions[key].get("error"):
                    questions[key]["error"] = (t.get("error") or "").strip()[:200]
                questions[key]["tokens"] += int(t.get("tokens") or 0)
                questions[key]["cost"] += float(t.get("cost") or 0)
                questions[key]["desc_len"] = max(
                    questions[key]["desc_len"],
                    len(re.sub(r"\s+", "", DESC_TAIL.sub("", t.get("description") or ""))))
            else:
                humans[w] += 1
                raw_desc = t.get("description") or ""
                questions[key] = {
                    "who": w, "source": src,
                    # 只留类别与长度，不留原文（数据最小化，见 TOPIC_RULES 注释）
                    "topic": topic_of(raw_desc),
                    "desc_len": len(re.sub(r"\s+", "", DESC_TAIL.sub("", raw_desc))),
                    "task_rows": [t["id"]], "projects": {(t.get("project") or "").strip()},
                    "status": t.get("status"), "engine": t.get("engine") or "",
                    "start": fmt_ts(t.get("created_at")), "start_ts": float(t.get("created_at") or 0),
                    "end": fmt_ts(t.get("finished_at")), "end_ts": float(t.get("finished_at") or 0),
                    "error": (t.get("error") or "").strip()[:200],
                    "tokens": int(t.get("tokens") or 0), "cost": float(t.get("cost") or 0),
                }
                if keep_content:      # 仅排障用，默认关；开了就别把 facts 交出去
                    questions[key]["_desc"] = raw_desc.strip()
                    questions[key]["_result_head"] = re.sub(
                        r"\s+", " ", (t.get("result") or "")).strip()[:400]
        total = len(items)
        done = by_status.get("done", 0)
        qs = list(questions.values())
        q_done = sum(1 for x in qs if x["status"] == "done")
        return {
            "task_rows": total,
            "questions": len(qs),
            "by_source": dict(by_src),
            "by_status": dict(by_status),
            "by_engine": dict(by_engine),
            "done": done, "failed": by_status.get("failed", 0),
            "cancelled": by_status.get("cancelled", 0),
            "review": by_status.get("review", 0),
            "conflict": by_status.get("conflict", 0),
            "success_rate": pct(done, total),
            "q_success_rate": pct(q_done, len(qs)),
            "q_done": q_done,
            "q_failed": sum(1 for x in qs if x["status"] == "failed"),
            "q_cancelled": sum(1 for x in qs if x["status"] == "cancelled"),
            # 提问级的待复核/冲突：摘要里跟「提问级成功率」并列展示，
            # 不能混用行级的 review/conflict（影子对会让行级数翻倍）。
            "q_review": sum(1 for x in qs if x["status"] == "review"),
            "q_conflict": sum(1 for x in qs if x["status"] == "conflict"),
            "active_users": len([h for h in humans if h != "unknown"]),
            "humans": dict(humans.most_common()),
            "_questions": qs,
        }

    cur_sum = summarize(tasks, keep_content)
    prev_sum = summarize(prev_tasks, keep_content)
    qs = cur_sum.pop("_questions")
    prev_sum.pop("_questions", None)
    # set 不能直接 JSON 序列化（default=str 会把它变成字符串，渲染时被逐字符迭代）
    for x in qs:
        x["projects"] = sorted(p for p in x.get("projects") or [] if p)

    # 重点用户活跃 / 静默
    key_active = {n: c for n, c in cur_sum["humans"].items() if n in roster_names}
    prev_key_active = {n: c for n, c in (prev_sum.get("humans") or {}).items() if n in roster_names}
    ref = dt.datetime.combine(date, dt.time.min, tzinfo=TZ).timestamp() + 86400  # 报告日 24:00
    silence = []
    for u in roster.get("users", []):
        n = u.get("name")
        if not n:
            continue
        ls = last_seen.get(n)
        days = round((ref - ls) / 86400, 1) if ls else None
        silence.append({
            "name": n, "id": u.get("id", ""), "org": u.get("org", ""),
            "dept": u.get("dept", ""), "title": u.get("title", ""),
            "dept_count": u.get("dept_count", ""),
            "tier": u.get("tier", ""), "source": u.get("source", ""),
            "last_seen": dt.datetime.fromtimestamp(ls, TZ).strftime("%Y-%m-%d") if ls else "无记录",
            "silent_days": days,
            "over_30d": bool(days is not None and days > 30),
            "active_today": n in key_active,
            "questions_today": key_active.get(n, 0),
        })
    silence.sort(key=lambda x: (x["silent_days"] is None, -(x["silent_days"] or 0)))

    # cron 构成：按 plan 名归并（描述精确匹配；匹配不上的归「计划已改名/删除」）
    # desc2plan 已在上面与 plan_sigs 一并构建，此处直接复用，勿重复解析 action。
    cron_by_plan = collections.Counter()
    for t in tasks:
        if classify(t) == "cron":
            cron_by_plan[desc2plan.get((t.get("description") or "").strip(),
                                       "(计划已改名/删除，无法回溯)")] += 1

    return {
        "ok": True,
        "plans": [{"id": p["id"], "name": p["name"], "owner": p["owner_name"],
                   "calendar": p["on_calendar"], "enabled": p["enabled"]} for p in plans],
        "cron_by_plan": dict(cron_by_plan.most_common()),
        "today": cur_sum, "prev": prev_sum,
        "key_users": {
            "roster_size": len(roster_names),
            "active": len(key_active),
            "active_names": key_active,
            "active_rate": pct(len(key_active), len(roster_names)),
            "questions": sum(key_active.values()),
            "prev_active": len(prev_key_active),
            "prev_active_names": prev_key_active,
            "prev_active_rate": pct(len(prev_key_active), len(roster_names)),
            "prev_questions": sum(prev_key_active.values()),
            "silence": silence,
        },
        "questions": sorted(qs, key=lambda x: x["start_ts"]),
        "attribution": {
            "by_requester": sum(1 for t in tasks if (t.get("requester") or "").strip()),
            "by_audit": sum(1 for t in tasks if not (t.get("requester") or "").strip()
                            and str(t.get("id")) in audit_actor),
            "unknown": sum(1 for x in qs if x["who"] == "unknown"),
        },
    }


# ---------------------------------------------------------------- 日志采集
def collect_logs(date: dt.date) -> dict:
    ds = date.strftime("%Y-%m-%d")
    out = {"inbound_msgs": 0, "inbound_by_sender": {}, "inbound_kinds": {},
           "audit_actions": {}, "anomalies": [], "rounds": 0,
           "multi_round_sessions": 0, "gaps": []}

    ef = os.path.join(AVATAR, f"events-{date.strftime('%Y%m%d')}.ndjson")
    if os.path.exists(ef):
        seen, senders, kinds = set(), collections.Counter(), collections.Counter()
        for line in open(ef, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") not in ("user_im_message_receive_o2o_all", "user_im_message_receive_at"):
                continue
            eid = r.get("event_id")
            if eid and eid in seen:
                continue
            seen.add(eid)
            if (r.get("create_time") or "")[:10] != ds:
                continue
            senders[r.get("sender") or "?"] += 1
            kinds["群@" if r.get("type").endswith("_at") else "单聊"] += 1
        out["inbound_msgs"] = sum(senders.values())
        out["inbound_by_sender"] = dict(senders.most_common())
        out["inbound_kinds"] = dict(kinds)
    else:
        out["gaps"].append(f"events 文件缺失: {os.path.basename(ef)}")

    al = os.path.join(AVATAR, "audit", "audit.log")
    if os.path.exists(al):
        acts, anom = collections.Counter(), []
        watch = {"send_refused", "guest_ignored", "store_add_failed", "plan_denied",
                 "plan_disable_bad_calendar", "plan_bad_action"}
        for line in open(al, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if str(r.get("ts") or "")[:10] != ds:
                continue
            a = r.get("action", "?")
            acts[a] += 1
            if a in watch:
                anom.append({"action": a, "actor": r.get("actor") or "-",
                             "detail": str(r.get("detail") or "")[:160], "ts": r.get("ts")})
        out["audit_actions"] = dict(acts.most_common())
        out["anomalies"] = anom
    else:
        out["gaps"].append("audit.log 缺失")

    rf = os.path.join(AVATAR, "rounds")
    if os.path.isdir(rf):
        multi = 0
        for f in glob.glob(os.path.join(rf, "*.jsonl")):
            kinds = collections.Counter()
            day_hit = False
            for line in open(f, encoding="utf-8", errors="replace"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                try:  # rounds 的 t 是 epoch float，不是字符串
                    d = dt.datetime.fromtimestamp(float(r.get("t")), TZ).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError):
                    d = str(r.get("t"))[:10]
                if d == ds:
                    day_hit = True
                kinds[r.get("kind")] += 1
            if day_hit:
                out["rounds"] += 1
                if kinds.get("submit", 0) > 1:
                    multi += 1
        out["multi_round_sessions"] = multi
    else:
        out["gaps"].append("rounds/ 目录缺失")
    return out


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="鲁大师用户日度使用分析采集器")
    ap.add_argument("--date", help="报告日 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--out", help="JSON 输出路径（默认 stdout）")
    ap.add_argument("--no-pg", action="store_true", help="跳过 PG，仅用本机日志")
    ap.add_argument("--roster", help="重点用户名单路径")
    ap.add_argument("--keep-content", action="store_true",
                    help="排障用：在 facts 里保留提问原文与交付正文（_desc/_result_head）。\n默认关闭——日报要落 shared/report 并进台账，不得留存用户任务内容。开了就别外发 facts。")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    date = dt.date.fromisoformat(args.date) if args.date else today - dt.timedelta(days=1)
    prev = date - dt.timedelta(days=1)

    load_env()
    roster = load_roster(args.roster)
    audit_actor = load_audit_actor()
    # 静默追踪的截止＝报告日 24:00，跑历史日报时不能把之后的活动算进来
    audit_last = load_audit_last_seen(cutoff=day_bounds(date)[1])

    facts = {
        "report_date": date.isoformat(),
        "prev_date": prev.isoformat(),
        "display_name": _resolve_display_name(),   # 本机分身展示名（标题/摘要/文件名用）
        "weekday": "周" + "一二三四五六日"[date.weekday()],
        "is_weekend": date.weekday() >= 5,
        "snapshot_time": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"start": f"{date} 00:00:00", "end": f"{date} 23:59:59", "tz": "Asia/Shanghai"},
        "roster": {"size": len(roster.get("users", [])), "gap": roster.get("gap"),
                   "names": [u.get("name") for u in roster.get("users", [])]},
        "roster_detail": roster.get("users", []),
        "service_accounts": [s.get("name") for s in roster.get("service_accounts", [])],
        "audit_actor_coverage": len(audit_actor),
        "degraded": [], "gaps": [],
    }

    pg = None if args.no_pg else collect_pg(date, prev, roster, audit_actor,
                                                audit_last, args.keep_content)
    if pg and pg.get("ok"):
        facts["pg"] = pg
        facts["source"] = "pg_authoritative"
    else:
        facts["source"] = "logs_only"
        facts["degraded"].append(
            f"PG 不可用（{(pg or {}).get('error', '已跳过')}）→ 降级为日志口径："
            "任务级/用户级指标缺失，只能给入站消息与审计异常")
        facts["pg"] = {"error": (pg or {}).get("error", "skipped")}

    facts["logs"] = collect_logs(date)
    facts["gaps"] += facts["logs"].get("gaps", [])

    for name, why in [
        ("prediction_log", "全盘不存在；预测质量追溯需业务方提供落盘路径"),
        ("--source all CLI", "不存在此聚合命令；本 skill 的 usage_collect.py 即其替代实现"),
        ("acp / 飞书 渠道", "本机无独立日志，恒为 0，按 [缺档] 标注"),
    ]:
        facts["gaps"].append(f"[缺档] {name}: {why}")

    payload = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"facts → {args.out}  ({len(payload)} bytes)", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
