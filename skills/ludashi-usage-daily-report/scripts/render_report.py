#!/usr/bin/env python3
"""把 usage_collect.py 的 facts.json 渲染成《<本机分身>用户日度使用分析报告》Markdown。

报告抬头的分身名**按机取**（facts.display_name，由宿主机 usage_collect.py 写入）：
本机（bot 自称鲁大师）显示「鲁大师」，其他机显示各自 avatar_name——技能机队共享但
抬头不写死鲁大师。人工裸跑无 facts.display_name 时由 self_name.py 兜底解析。

用法：
    render_report.py --facts facts.json [--out report.md] [--mode auto-skill]

渲染原则：
  * 所有数字来自 facts，不二次计算、不猜测。
  * 需要人判断的叙述位（一句话结论、填写说明、观察与建议）输出 {{待填：…}} 标记，
    由调用方 agent 补齐；绝不自动编造结论。
  * 无数据的维度按附录 B 约定打 [缺档]/[降级]/[周末基线] 标签，不留空表。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

# 本机分身展示名解析器（同目录 self_name.py）。正常路径下 display_name 已由宿主机
# usage_collect.py 写进 facts，这里只在 facts 缺该字段（如人工裸跑）时兜底解析。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from self_name import LUDASHI, resolve_display_name as _resolve_display_name
except Exception:                       # 解析器缺失也不让渲染中断
    LUDASHI = "鲁大师"

    def _resolve_display_name() -> str:
        return LUDASHI

TEMPLATE_VERSION = "v1.0（2026-10-12 依用户模板固化）"
SRC_ORDER = ["cron", "dingtalk", "api_server", "cli", "acp", "feishu", "system", "replay"]
SRC_LABEL = {
    "cron": "cron（计划触发）", "dingtalk": "dingtalk（钉钉人工）",
    "api_server": "api_server（token 直连）", "cli": "cli（本人持 token）",
    "acp": "acp", "feishu": "飞书", "system": "system（管理/探针）",
    "replay": "replay（回放测试）",
}
STATUS_CN = {"done": "完成", "failed": "失败", "cancelled": "中断",
             "review": "待复核", "conflict": "冲突待裁", "queued": "排队", "running": "执行中"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 需求类型由采集器归类后放进 facts 的 topic 字段（原文不出采集器，见 usage_collect.TOPIC_RULES）。
# 渲染器只消费类别，不再自己分类——否则原文得先流到这一层，违背数据最小化。
TOPIC_OTHER = "其他/未归类"


def idx_mark(i: int) -> str:
    return CIRCLED[i - 1] if 1 <= i <= len(CIRCLED) else f"({i})"


def orgdept(u: dict) -> str:
    """公司/部门列的取值。

    多部门高管（如邹雄 34 个部门）的 dept 是任意挑的，展示出来会误导；
    这类人改显示岗位（orgTitle），语义更准也更稳定。
    """
    if not u:
        return "[缺档] 不在名单，需 dws contact 反查"
    org = u.get("org") or ""
    try:
        dc = int(u.get("dept_count") or 0)
    except (TypeError, ValueError):
        dc = 0
    if dc > 3:
        second = u.get("title") or u.get("dept") or ""
        tail = f"（兼 {dc} 部门，取岗位）" if second else f"（兼 {dc} 部门）"
        return "／".join(x for x in (org, second) if x) + tail
    second = u.get("dept") or u.get("title") or ""
    return "／".join(x for x in (org, second) if x) or "[缺档] 通讯录无返回"


def g(d, *ks, default=None):
    for k in ks:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def n(v, dash="-"):
    return dash if v is None else v


def d_(cur, prev):
    if cur is None or prev is None:
        return "-"
    d = cur - prev
    if d == 0:
        return "持平"
    return f"{d:+d}" if isinstance(d, int) and isinstance(prev, int) else f"{d:+.1f}"


def dp(cur, prev):
    """百分比环比，带 pp 单位。"""
    if cur is None or prev is None:
        return "-"
    d = round(cur - prev, 1)
    return "持平" if d == 0 else f"{d:+.1f}pp"


def tbl(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def render(f: dict, mode: str) -> str:
    L = []
    pg = f.get("pg") or {}
    ok = pg.get("ok")
    t = pg.get("today") or {}
    p = pg.get("prev") or {}
    k = pg.get("key_users") or {}
    lg = f.get("logs") or {}
    date = f["report_date"]
    weekend = f.get("is_weekend")
    name = f.get("display_name") or LUDASHI   # 本机分身展示名（宿主机已写进 facts）

    # ── 抬头
    L.append(f"# {name}用户日度使用分析报告（{date}）")
    L.append("")
    w = f.get("window") or {}
    trade = "非交易日（周末）" if weekend else "交易日（未扣除法定节假日，见附录 A）"
    L.append(f"**统计范围**：{w.get('start','-')} 至 {w.get('end','-')}"
             f"（本地时区 {w.get('tz','Asia/Shanghai')}，完整自然日，{f.get('weekday','')}{trade}）  ")
    L.append(f"**生成信息**：数据快照 {f.get('snapshot_time','-')} ｜ 生成方式 {mode} ｜ 版本 {TEMPLATE_VERSION}")
    L.append("")
    if not ok:
        L.append("> **[降级]** " + (f.get("degraded") or ["PG 不可用"])[0])
        L.append("> 本报告仅含本机日志口径，任务级与用户级指标缺失，不可用于经营判断。")
        L.append("")

    # ── 数据口径
    L.append("**数据口径**（一句话版，详见附录 A）：")
    L.append("")
    L.append(tbl(["口径", "覆盖范围", "适用章节"], [
        ["全渠道", "PG tasks 全量（cron / dingtalk / api_server / cli / system 全部 project）", "第 2 节"],
        ["用户级", "project ∈ {avatar-local, relay-avatar} ＋ cli（本人持 token）；均绑定真实发起人", "第 3、4 节"],
    ]))
    L.append("")
    L.append("> 姓名/公司/部门以 `config/key_users.toml`（由 `dws contact user get` 反查生成）为准；"
             "cron / api_server / system 无自然人绑定，不纳入用户级统计。")
    L.append("")

    # ══ 1. 摘要盖帽
    L.append("## 1. 摘要盖帽（领导 15 秒速览）")
    L.append("")
    top = sorted((t.get("humans") or {}).items(), key=lambda x: -x[1])
    subj = max((t.get("by_source") or {}).items(), key=lambda x: x[1])[0] if t.get("by_source") else "-"
    L.append(f"**一句话结论**：{{{{待填：一句话概括当天——主体渠道是 {subj}、"
             f"人工真实提问 {n(t.get('questions'))} 条、重点用户活跃 {n(k.get('active'))}/{n(k.get('roster_size'))} 人、"
             f"最活跃是 {top[0][0] if top else '无'}（{top[0][1] if top else 0} 条）、有无异常}}}}")
    L.append("")
    anom = lg.get("anomalies") or []
    acnt = {}
    for a in anom:
        acnt[a["action"]] = acnt.get(a["action"], 0) + 1
    anom_txt = "；".join(f"[{a}]×{c}" for a, c in acnt.items()) or "无"
    if weekend:
        anom_txt = "[周末基线] 低活跃属正常节律，不与工作日直接同比" + ("；" + anom_txt if acnt else "")
    L.append(tbl(["核心指标", "当日", f"环比 {f.get('prev_date','')}", "备注"], [
        ["全渠道任务行数", n(t.get("task_rows")), d_(t.get("task_rows"), p.get("task_rows")),
         f"主体渠道 {subj}；含影子执行双行"],
        ["人工真实提问数", n(t.get("questions")), d_(t.get("questions"), p.get("questions")),
         "已按 desc 核心去重影子对（avatar-local＋relay-avatar 算 1 条）"],
        ["活跃用户数（用户级）", n(t.get("active_users")), d_(t.get("active_users"), p.get("active_users")),
         f"人均 {round(t.get('questions',0)/t['active_users'],1) if t.get('active_users') else '-'} 条"],
        ["重点用户活跃", f"{n(k.get('active'))}/{n(k.get('roster_size'))}",
         d_(k.get("active"), k.get("prev_active")), f"活跃率 {n(k.get('active_rate'))}%"],
        ["交付成功率（提问级）", f"{n(t.get('q_success_rate'))}%", dp(t.get("q_success_rate"), p.get("q_success_rate")),
         f"失败 {n(t.get('q_failed'))}、中断 {n(t.get('q_cancelled'))}、待复核 {n(t.get('q_review'))}"],
        ["cron 占比", f"{n(round((t.get('by_source') or {}).get('cron',0)*100.0/t['task_rows'],1)) if t.get('task_rows') else '-'}%",
         "-", "周末基线见下" if weekend else "工作日基线"],
        ["入站消息数（钉钉）", n(lg.get("inbound_msgs")), "-",
         "、".join(f"{k} {v}" for k, v in (lg.get("inbound_kinds") or {}).items())
         + "；含未成任务的消息"],
    ]))
    L.append("")
    L.append(f"**当日异常/提示**：{anom_txt}")
    L.append("")
    for d in (f.get("degraded") or []):
        L.append(f"> **[降级]** {d}")
    L.append("")

    # ══ 2. 全渠道概览
    L.append("## 2. 全渠道概览")
    L.append("")
    L.append("### 2.1 当日总体指标（全渠道）")
    L.append("")
    L.append(tbl(["指标", "数值", "说明"], [
        ["任务行总数", n(t.get("task_rows")), "全 project 合计；影子执行使人工提问落 2 行"],
        ["人工真实提问", n(t.get("questions")), "去重后；见 3.3 逐条卡片"],
        ["定时任务行", n((t.get("by_source") or {}).get("cron")), "构成见 2.3"],
        ["入站消息总数", n(lg.get("inbound_msgs")), "钉钉 events 去重后；含非任务消息"],
        ["会话轮次文件", n(lg.get("rounds")), f"其中多轮 {n(lg.get('multi_round_sessions'))} 个"],
        ["token 消耗", n(sum(q.get("tokens", 0) for q in (pg.get("questions") or []))), "仅人工提问部分"],
    ]))
    L.append("")
    L.append(f"> **填写说明**：{{{{待填：一句话点明当天主体，如「{f.get('weekday','')}，"
             f"{'定时任务为绝对主体' if subj=='cron' else '人工提问为主体'}，"
             f"人工真实提问 {n(t.get('questions'))} 条」}}}}")
    L.append("")

    L.append("### 2.2 渠道分布（全渠道）")
    L.append("")
    bs, bps = t.get("by_source") or {}, p.get("by_source") or {}
    rows = []
    for s in SRC_ORDER:
        cur, prv = bs.get(s, 0), bps.get(s, 0)
        if not cur and not prv:
            continue
        note = {"cron": "构成见 2.3", "dingtalk": "有真实发起人，计入用户级",
                "api_server": "token 账号，无自然人", "cli": "本人持 token 直连",
                "system": "管理接口/探针", "replay": "回放测试，不计入人工"}.get(s, "")
        if s in ("acp", "feishu"):
            note = "[缺档] 本机无独立日志"
        rows.append([SRC_LABEL.get(s, s), cur,
                     f"{round(cur*100.0/t['task_rows'],1) if t.get('task_rows') else '-'}%",
                     d_(cur, prv), note])
    for s in ("acp", "feishu"):
        if s not in bs and s not in bps:
            rows.append([SRC_LABEL[s], 0, "0%", "-", "[缺档] 本机无独立日志，恒为 0"])
    rows.append(["**合计**", f"**{n(t.get('task_rows'))}**", "**100%**",
                 d_(t.get("task_rows"), p.get("task_rows")), ""])
    L.append(tbl(["渠道 source", "任务行", "占比", "环比", "备注"], rows))
    L.append("")
    L.append("> **填写说明**：{{待填：表下用 1–2 句解释主要变化，如「api_server 由 N 降至 M，"
             "与周末非交易日一致」}}")
    L.append("")

    L.append("### 2.3 cron 会话构成")
    L.append("")
    cbp = pg.get("cron_by_plan") or {}
    if cbp:
        plans = {x["name"]: x for x in (pg.get("plans") or [])}
        rows = []
        for name, cnt in cbp.items():
            pl = plans.get(name) or {}
            rows.append([name, cnt, pl.get("calendar") or "-",
                         "启用" if pl.get("enabled") else ("停用" if pl else "-")])
        rows.append(["**合计**", f"**{sum(cbp.values())}**", "", ""])
        L.append(tbl(["定时任务名", "触发次数", "日历表达式", "状态"], rows))
    else:
        L.append("当日无计划触发任务。")
    L.append("")

    # ══ 3. 用户级分析
    L.append("## 3. 用户级分析（dingtalk ＋ cli）")
    L.append("")
    L.append("### 3.1 用户级总体指标")
    L.append("")
    attr = pg.get("attribution") or {}
    humans = t.get("humans") or {}
    unk = humans.get("unknown", 0)
    L.append(tbl(["指标", "数值", "说明"], [
        ["人工提问数", n(t.get("questions")), f"dingtalk {bs.get('dingtalk',0)} 行 ＋ cli {bs.get('cli',0)} 行，去重后"],
        ["归属可追溯", f"{attr.get('by_requester',0)}＋{attr.get('by_audit',0)}",
         f"requester 列 {attr.get('by_requester',0)} ＋ audit.log 回补 {attr.get('by_audit',0)}"],
        ["无归属（unknown）", n(attr.get("unknown")), "归入 unknown，不参与排名"],
        ["活跃用户数", n(t.get("active_users")), f"{len([x for x in humans if x!='unknown'])} 人"],
        ["人均提问", f"{round(t.get('questions',0)/t['active_users'],1) if t.get('active_users') else '-'} 条", ""],
        ["追问结构", f"多轮会话 {n(lg.get('multi_round_sessions'))} 个",
         f"rounds 共 {n(lg.get('rounds'))} 个；与前日对比见 {{待填}}"],
        ["单提问平均 token", f"{round(sum(q.get('tokens',0) for q in (pg.get('questions') or []))/max(len(pg.get('questions') or [1]),1)):,}",
         "含工具调用"],
    ]))
    L.append("")

    L.append("### 3.2 活跃用户 Top 榜")
    L.append("")
    qs = pg.get("questions") or []
    agg = {}
    for q in qs:
        w = q["who"]
        if w == "unknown":
            continue
        a = agg.setdefault(w, {"q": 0, "rows": 0, "tokens": 0, "done": 0})
        a["q"] += 1
        a["rows"] += len(q.get("task_rows") or [])
        a["tokens"] += q.get("tokens", 0)
        if q.get("status") == "done":
            a["done"] += 1
    roster_names = set(f.get("roster", {}).get("names") or [])
    by_name = {u.get("name"): u for u in (f.get("roster_detail") or []) if u.get("name")}
    rows = []
    for i, (w, a) in enumerate(sorted(agg.items(), key=lambda x: (-x[1]["q"], -x[1]["tokens"])), 1):
        u = by_name.get(w) or {}
        rows.append([i, w, orgdept(u),
                     a["q"], a["rows"], f"{a['tokens']:,}",
                     "是" if w in roster_names else "否", f"{a['done']}/{a['q']}"])
    if rows:
        L.append(tbl(["排名", "姓名", "公司/部门", "提问数", "任务行", "token", "是否重点用户", "完成"], rows))
    else:
        L.append("当日无人工提问。")
    if unk:
        L.append("")
        L.append(f"> `unknown` 另有 {unk} 条无归属提问，单列说明、不占排名。")
    L.append("")
    L.append("> **填写说明**：按提问数降序（同数按 token 降序）。")
    L.append("")

    L.append("### 3.3 当日会话清单（结构化卡片）")
    L.append("")
    L.append("> **数据最小化**：本文件**不留存**用户提问原文与交付正文——报告要落 "
             "`shared/report/` 并进台账（机队可见、长期留存），而原文含预算数字、期货策略、"
             "头寸限额等经营敏感信息，不该随使用度量一起持久化。故每张卡片只记"
             "**需求类型（自动归类）＋提问长度＋时效＋状态**；要看具体内容请回原任务"
             "（卡片里给了任务行号，可 `store q --task <id>` 或查 relay 台账追溯）。")
    L.append("")
    if not qs:
        L.append("当日无人工会话。")
    for i, q in enumerate(qs, 1):
        dur = None
        if q.get("start_ts") and q.get("end_ts"):
            dur = round(q["end_ts"] - q["start_ts"])
        st = STATUS_CN.get(q.get("status"), q.get("status") or "-")
        L.append(f"**{idx_mark(i)} {q['who']}（{q['source']}）· {q.get('topic') or TOPIC_OTHER}**")
        L.append("")
        proj = ", ".join(q.get("projects") or []) or "-"
        L.append(f"- **时间/轮次**：{q.get('start','-')} 至 {q.get('end','-')}"
                 f"｜任务行 {'+'.join(str(x) for x in (q.get('task_rows') or []))}（{proj}）")
        L.append(f"- **提问规模**：{q.get('desc_len', 0)} 字（原文不留存）")
        L.append(f"- **耗时/时效**：{f'{dur//60}分{dur%60}秒' if dur else '未完成/无记录'}"
                 f"｜engine={q.get('engine') or '-'}｜token={q.get('tokens',0):,}")
        L.append(f"- **状态**：{st}")
        err = (q.get("error") or "").strip()
        if err and q.get("status") != "done":
            # 只在非完成态显示 error：完成态里残留的多是历史提示（如「升级 relay 门禁」），
            # 不是失败原因，显示出来会让领导误读成交付有问题。
            L.append(f"- **备注**：`{err[:180]}`")
        L.append("")

    sysq = [q for q in qs if q["who"] == "unknown"]
    if sysq or bs.get("api_server"):
        L.append("**系统级会话（api_server / 无归属）**")
        L.append("")
        L.append(f"- api_server 任务行 {bs.get('api_server',0)} 条（token 账号，无自然人绑定）")
        if sysq:
            L.append(f"- 无归属提问 {len(sysq)} 条：任务行 "
                     + ", ".join(str(x) for q in sysq for x in (q.get("task_rows") or [])))
        L.append("- 状态：{{待填}}｜降级：{{如「周六 synthesis 未落盘，回退至上一交易日 pm 档」}}")
        L.append("")

    # ══ 4. 重点用户详情
    L.append(f"## 4. 重点用户详情（{n(k.get('roster_size'))} 人清单）")
    L.append("")
    if f.get("roster", {}).get("size") != 17:
        L.append(f"> **[缺档]** 模板原文写「17 人」，但全盘无该名单定义（T760 排查已确认）。"
                 f"现用可追溯出处重建的真实名单 **{n(k.get('roster_size'))} 人**"
                 f"（policy 静态白名单＋动态授权＋历史活跃）。活跃率按实际人数算，不写死 17。"
                 f"业务方若另有原始名单，追加进 `config/key_users.toml` 即自动生效。")
        L.append("")
    L.append("### 4.1 重点用户活跃概览")
    L.append("")
    L.append(tbl(["指标", "当日", f"环比 {f.get('prev_date','')}"], [
        ["活跃人数", f"{n(k.get('active'))}/{n(k.get('roster_size'))}",
         f"{n(k.get('prev_active'))}/{n(k.get('roster_size'))}（{d_(k.get('active'), k.get('prev_active'))}）"],
        ["活跃率", f"{n(k.get('active_rate'))}%",
         f"{n(k.get('prev_active_rate'))}%（{dp(k.get('active_rate'), k.get('prev_active_rate'))}）"],
        ["提问数", n(k.get("questions")),
         f"{n(k.get('prev_questions'))}（{d_(k.get('questions'), k.get('prev_questions'))}）"],
    ]))
    L.append("")
    L.append("### 4.2 有活动重点用户详情")
    L.append("")
    L.append("> 同样不留提问原文，只给**用量画像**：时段、需求类型构成、状态构成、耗时与 token。")
    L.append("")
    act = k.get("active_names") or {}
    if not act:
        L.append("**当日无。**")
    else:
        for name, cnt in sorted(act.items(), key=lambda x: -x[1]):
            mine = [q for q in qs if q["who"] == name]
            if not mine:
                continue
            topics = collections.Counter(q.get("topic") or TOPIC_OTHER for q in mine)
            stats = collections.Counter(STATUS_CN.get(q.get("status"), q.get("status") or "-")
                                        for q in mine)
            toks = sum(q.get("tokens", 0) for q in mine)
            durs = [round(q["end_ts"] - q["start_ts"]) for q in mine
                    if q.get("start_ts") and q.get("end_ts")]
            span = f"{mine[0].get('start','-')}–{mine[-1].get('end','-')}"
            L.append(f"**{name}**（{cnt} 条提问｜{span}）")
            L.append("")
            L.append(f"- 需求类型：{'、'.join(f'{t}×{c}' for t, c in topics.most_common())}")
            L.append(f"- 状态：{'、'.join(f'{s}×{c}' for s, c in stats.most_common())}")
            avg = round(sum(durs) / len(durs)) if durs else None
            L.append(f"- 耗时：均 {f'{avg//60}分{avg%60}秒' if avg is not None else '-'}"
                     f"｜最长 {f'{max(durs)//60}分{max(durs)%60}秒' if durs else '-'}"
                     f"｜token {toks:,}")
            L.append("")
    L.append("")
    L.append("### 4.3 当日无活动重点用户（静默追踪）")
    L.append("")
    sil = [s for s in (k.get("silence") or []) if not s.get("active_today")]
    if sil:
        rows = [[s["name"], orgdept(s), s["last_seen"],
                 n(s["silent_days"]), "是" if s.get("over_30d") else "否", s.get("source", "-")]
                for s in sil]
        L.append(tbl(["姓名", "公司/部门", "最近一次活动", "静默天数", "超 30 天", "名单出处"], rows))
        over = [s["name"] for s in sil if s.get("over_30d")]
        L.append("")
        L.append(f"> **填写说明**：按静默天数降序。{len(sil)} 人当日无活动，"
                 f"其中 {len(over)} 人超 30 天无主动查询"
                 + (f"（{'、'.join(over)}）" if over else "") + "。")
    else:
        L.append("全员当日均有活动。")
    L.append("")

    # ══ 5. 观察与建议
    L.append("## 5. 观察与建议（优先级可追踪）")
    L.append("")
    L.append(tbl(["编号", "发现", "建议动作", "优先级", "责任/归属"], [
        [1, "{{待填：观察，必须带当天数据支撑}}", "{{待填：可执行建议}}", "{{高/中/低}}", "{{建议归属}}"],
        [2, "", "", "", ""],
    ]))
    L.append("")
    L.append("> **填写说明**：每条「发现」必须带当天数据支撑；「建议」要可执行、可落地，避免空泛。")
    L.append("")
    L.append("**可由数据直接支撑的候选观察**（供撰写第 5 节时取用，勿照抄）：")
    L.append("")
    cands = []
    if t.get("q_failed") or t.get("q_cancelled"):
        cands.append(f"- 交付质量：提问级失败 {t.get('q_failed')} 条、中断 {t.get('q_cancelled')} 条，"
                     f"成功率 {n(t.get('q_success_rate'))}%")
    if acnt.get("store_add_failed"):
        cands.append(f"- 台账完整性：`store_add_failed` ×{acnt['store_add_failed']}，产出未登记台账")
    if acnt.get("send_refused"):
        cands.append(f"- 权限边界：`send_refused` ×{acnt['send_refused']}，有越权发送被拦")
    if acnt.get("guest_ignored"):
        cands.append(f"- 拉新缺口：`guest_ignored` ×{acnt['guest_ignored']}，未授权用户被忽略（无主动开通路径）")
    over30 = [s["name"] for s in (k.get("silence") or []) if s.get("over_30d")]
    if over30:
        cands.append(f"- 重点用户流失：{len(over30)} 人超 30 天无主动查询（{'、'.join(over30)}）")
    if attr.get("unknown"):
        cands.append(f"- 归属缺口：{attr['unknown']} 条提问无法归属到人，影响用户级统计准确性")
    if t.get("task_rows") and t.get("questions") and t["task_rows"] > t["questions"] * 1.5:
        cands.append(f"- 算力放大：{t['task_rows']} 任务行仅对应 {t['questions']} 条提问"
                     f"（影子执行 {round(t['task_rows']/t['questions'],1)}×），token 成本翻倍")
    if weekend:
        cands.append("- [周末基线] 当日为周末，低活跃属正常节律，环比不与工作日直接同比")
    L.extend(cands or ["- 当日无显著异常"])
    L.append("")

    # ══ 附录 A
    L.append("## 附录 A · 口径与数据源")
    L.append("")
    L.append(tbl(["项", "实际来源", "说明"], [
        ["全渠道口径", "PG `tasks` 全量（无 `--source all` 命令，该 CLI 不存在）",
         "由 `scripts/usage_collect.py` 实现聚合"],
        ["用户级口径", "PG `tasks` WHERE project ∈ {avatar-local, relay-avatar} ＋ cli",
         "模板写的 `--source \"dingtalk,api_server\"` 不存在，按 project 判别"],
        ["渠道判别", "`tasks.project`（**不是** `tasks.user`）",
         "user 对分身代投任务恒为「鲁大师」，无区分力"],
        ["影子执行去重", "剥掉「--- 【投递铁律…」尾巴后按 (发起人, desc 前 120 字) 归并",
         "avatar-local ＋ relay-avatar 两行算 1 条提问"],
        ["姓名/公司/部门", "`config/key_users.toml`（由 `dws contact user get --ids …` 反查生成）",
         "**employee.db 全盘不存在**，模板该项无数据源"],
        ["历史归属回补", "`avatar/audit/audit.log` 的 task_id→actor（local_shadow/task_submit）",
         "requester 列 2026-10-10 才上线，此前靠 audit，实测覆盖 97%"],
        ["重点用户名单", "`config/key_users.toml`（12 人，出处逐条标注）",
         "**「17 人」清单全盘无定义**，见第 4 节 [缺档] 说明"],
        ["入站消息", "`avatar/events-<YYYYMMDD>.ndjson`（按 event_id 去重）", "2026-09-24 起有档"],
        ["会话轮次", "`avatar/rounds/<task_id>.jsonl`（`t` 是 epoch float）", "多轮＝submit>1"],
        ["cron 构成", "PG `plans`（name/on_calendar/enabled）＋描述精确匹配",
         "计划改名后旧任务归「(计划已改名/删除，无法回溯)」"],
        ["消息数口径", "含工具调用消息", "与模板一致"],
        ["预测质量追溯", "**prediction_log 全盘不存在**", "[缺档] 需业务方提供落盘路径"],
        ["交易日判定", "仅按周末判非交易日", "[缺档] 未接法定节假日日历，调休日会误判"],
    ]))
    L.append("")
    L.append("**取数命令**：")
    L.append("```bash")
    L.append("PY=/mnt/vol-eltaah12/finaagent/.venv/bin/python")
    L.append("S=/mnt/vol-eltaah12/finaagent/relay-dev/skills/ludashi-usage-daily-report/scripts")
    L.append(f"$PY $S/usage_collect.py --date {date} --out /tmp/facts.json")
    L.append("$PY $S/render_report.py --facts /tmp/facts.json --out /tmp/report.md")
    L.append("```")
    L.append("")
    if f.get("gaps"):
        L.append("**本机数据缺口（如实标注，未编造）**：")
        L.append("")
        for x in f["gaps"]:
            L.append(f"- {x}")
        L.append("")

    # ══ 附录 B
    L.append("## 附录 B · 全局标注约定")
    L.append("")
    L.append(tbl(["标签", "含义"], [
        ["[降级]", "数据回退到上一交易日/上一档，需在正文明说锚定日期"],
        ["[缺档]", "非交易日或 artifact 未落盘／该维度本机无数据源"],
        ["[周末基线]", "周六/周日，低活跃属正常节律，不与工作日直接同比"],
        ["[模拟]", "使用模拟数据（须首屏横幅＋逐值标注）"],
    ]))
    L.append("")
    L.append(f"环比默认对比前一自然日（{f.get('prev_date','-')}）；跨周末/节假日须显式说明。")
    L.append("")
    return "\n".join(L)


def render_summary(f: dict) -> str:
    """推送用摘要：只聚焦**用户使用量**，不含任何任务原文。

    2026-10-12 用户指示：推送一律「摘要＋文件」，摘要聚焦使用量、再精简。
    故这里刻意不放第 5 节观察与建议整表（那是文件里的内容），异常压成一行。
    """
    pg = f.get("pg") or {}
    lg = f.get("logs") or {}
    date = f["report_date"]
    wk = f.get("weekday", "")
    trade = "非交易日" if f.get("is_weekend") else "交易日"
    name = f.get("display_name") or LUDASHI   # 本机分身展示名

    if not pg.get("ok"):
        return (f"**{name}用户日度使用分析（{date} {wk}）**\n\n"
                f"> **[降级]** {(f.get('degraded') or ['PG 不可用'])[0]}\n"
                f">\n"
                f"> 当日仅有机日志口径：入站消息 {lg.get('inbound_msgs', 0)} 条、"
                f"审计异常 {len(lg.get('anomalies') or [])} 项。任务级与用户级指标缺失，"
                f"不可用于经营判断，请查 /var/log/ludashi-usage-collect.log。")

    t = pg["today"]
    p = pg.get("prev") or {}
    k = pg.get("key_users") or {}
    bs = t.get("by_source") or {}

    top = [(w, c) for w, c in (t.get("humans") or {}).items() if w != "unknown"]
    top = sorted(top, key=lambda x: -x[1])[:5]
    anom = collections.Counter(a["action"] for a in (lg.get("anomalies") or []))
    anom_txt = "；".join(f"{a} ×{c}" for a, c in anom.most_common(3)) or "无"
    if f.get("is_weekend"):
        anom_txt = "[周末基线] 低活跃属正常节律，不与工作日同比" + (f"；{anom_txt}" if anom else "")

    chan = " · ".join(f"{SRC_LABEL.get(s, s).split('（')[0]} {c} 行"
                      for s, c in sorted(bs.items(), key=lambda x: -x[1]) if c)
    per = round(t["questions"] / t["active_users"], 1) if t.get("active_users") else "-"

    L = [
        f"**{name}用户日度使用分析（{date} {wk}·{trade}）**",
        "",
        f"- **用量**：人工提问 **{t.get('questions', 0)}** 条（环比 {d_(t.get('questions'), p.get('questions'))}）"
        f"｜活跃 **{t.get('active_users', 0)}** 人｜人均 {per} 条",
        f"- **重点用户**：{k.get('active', 0)}/{k.get('roster_size', 0)} 活跃"
        f"（{k.get('active_rate', '-')}%）｜静默 {len([s for s in (k.get('silence') or []) if not s.get('active_today')])} 人",
        f"- **渠道**：{chan or '-'}（任务行 {t.get('task_rows', 0)}，影子执行去重后 {t.get('questions', 0)} 条提问）",
        f"- **交付**：提问级成功率 **{t.get('q_success_rate', '-')}%**"
        f"（失败 {t.get('q_failed', 0)}｜中断 {t.get('q_cancelled', 0)}｜待复核 {t.get('q_review', 0)}）",
        f"- **Top 用户**：{' · '.join(f'{w} {c}' for w, c in top) or '当日无人工提问'}",
        f"- **入站消息**：{lg.get('inbound_msgs', 0)} 条"
        f"（{'、'.join(f'{a}{b}' for a, b in (lg.get('inbound_kinds') or {}).items())}）"
        f"｜多轮会话 {lg.get('multi_round_sessions', 0)} 个",
        f"- **异常/提示**：{anom_txt}",
        "",
        "> 完整件见附件。按数据最小化要求，文件不留存提问原文与交付正文，只记需求类型/时效/状态/用量；"
        "需追溯具体内容用卡片里的任务行号查 relay 台账。",
    ]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染本机分身用户日度使用分析报告")
    ap.add_argument("--facts")
    ap.add_argument("--out")
    ap.add_argument("--mode", default="自动-skill", help="人工 / 自动-skill")
    ap.add_argument("--summary", action="store_true",
                    help="只输出推送用摘要（聚焦用户使用量，不含任务原文）；推送一律「摘要＋文件」")
    ap.add_argument("--name",
                    help="覆盖报告展示名（缺省取 facts.display_name，再缺省按机解析：本机鲁大师/其他机 avatar_name）")
    ap.add_argument("--print-name", action="store_true",
                    help="只打印本机报告展示名（供 shell 拼报告文件名），不渲染报告")
    a = ap.parse_args()

    # --print-name 可脱离 facts 单独取展示名（拼文件名时 facts 可能还没生成）
    if a.print_name and not a.facts:
        print(a.name or _resolve_display_name())
        return 0
    if not a.facts:
        ap.error("--facts 必填（除非用 --print-name 单独取展示名）")

    f = json.load(open(a.facts, encoding="utf-8"))
    # 展示名优先级：--name 覆盖 > facts.display_name（宿主机采集时写入）> 按机兜底解析
    if a.name:
        f["display_name"] = a.name
    elif not f.get("display_name"):
        f["display_name"] = _resolve_display_name()

    if a.print_name:
        print(f["display_name"])
        return 0

    md = render_summary(f) if a.summary else render(f, a.mode)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(md)
        todo = md.count("{{待填")
        kind = "summary" if a.summary else "report"
        extra = "" if a.summary else f", {todo} 处待填叙述位"
        print(f"{kind} → {a.out}  ({len(md)} 字符{extra})", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
