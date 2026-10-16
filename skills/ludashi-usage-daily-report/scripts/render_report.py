#!/usr/bin/env python3
"""把 usage_collect.py 的 facts.json 渲染成《<本机分身>用户日度使用分析报告》Markdown。

报告抬头的分身名**按机取**（facts.display_name，由宿主机 usage_collect.py 写入）：
本机（bot 自称鲁大师）显示「鲁大师」，其他机显示各自 avatar_name——技能机队共享但
抬头不写死鲁大师。人工裸跑无 facts.display_name 时由 self_name.py 兜底解析。

用法：
    render_report.py --facts facts.json [--out report.md] [--mode auto-skill]

渲染原则：
  * 所有数字来自 facts，不二次计算、不猜测。
  * 2026-10-16 用户指示：人员使用情况置顶，汇总精简，完整件控制在约两页内。
  * 需要人判断的建议位输出 {{待填：…}} 标记，由调用方 agent 补齐；绝不自动编造结论。
  * 无数据的维度按 [缺档]/[降级]/[周末基线] 标签如实标注，不留空表。
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

TEMPLATE_VERSION = "v2.0（2026-10-16 依用户指示：人员使用置顶、汇总精简、两页内）"
SRC_ORDER = ["dingtalk", "cli", "cron", "api_server", "system", "replay", "acp", "feishu"]
SRC_LABEL = {
    "cron": "cron", "dingtalk": "dingtalk",
    "api_server": "api_server", "cli": "cli",
    "acp": "acp", "feishu": "飞书", "system": "system",
    "replay": "replay",
}
STATUS_CN = {"done": "完成", "failed": "失败", "cancelled": "中断",
             "review": "待复核", "conflict": "冲突待裁", "queued": "排队", "running": "执行中"}
ABNORMAL_STATUS = ("failed", "cancelled", "review", "conflict")
TOPIC_OTHER = "其他/未归类"


def orgdept(u: dict) -> str:
    """精简版岗位/部门：多部门高管显示岗位，其余优先岗位、回退部门/公司。"""
    if not u:
        return "[缺档]"
    try:
        dc = int(u.get("dept_count") or 0)
    except (TypeError, ValueError):
        dc = 0
    if dc > 3:
        return u.get("title") or u.get("dept") or u.get("org") or "[缺档]"
    return u.get("title") or u.get("dept") or u.get("org") or "[缺档] 通讯录无返回"


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


def short_time(ts: float, default: str = "-") -> str:
    if not ts:
        return default
    return default if ts == "-" else str(ts)[:5]


def topics_text(counter: collections.Counter, limit: int = 2) -> str:
    if not counter:
        return "-"
    return "、".join(f"{t}×{c}" for t, c in counter.most_common(limit))


def status_text(counts: collections.Counter) -> str:
    done = counts.get("done", 0)
    abn = sum(counts.get(s, 0) for s in ABNORMAL_STATUS)
    return f"{done}/{abn}"


def aggregate_users(f: dict) -> tuple[list[dict], int]:
    pg = f.get("pg") or {}
    qs = pg.get("questions") or []
    prev_humans = (pg.get("prev") or {}).get("humans") or {}
    roster_names = set((f.get("roster") or {}).get("names") or [])
    by_name = {u.get("name"): u for u in (f.get("roster_detail") or []) if u.get("name")}
    agg: dict[str, dict] = {}
    unknown = 0
    for q in qs:
        w = q.get("who") or "unknown"
        if w == "unknown":
            unknown += 1
            continue
        a = agg.setdefault(w, {
            "who": w, "q": 0, "tokens": 0, "topics": collections.Counter(),
            "statuses": collections.Counter(), "starts": [], "ends": [],
        })
        a["q"] += 1
        a["tokens"] += int(q.get("tokens") or 0)
        a["topics"][q.get("topic") or TOPIC_OTHER] += 1
        a["statuses"][q.get("status") or "?"] += 1
        if q.get("start"):
            a["starts"].append(q["start"])
        if q.get("end"):
            a["ends"].append(q["end"])
    rows = []
    for w, a in sorted(agg.items(), key=lambda x: (-x[1]["q"], -x[1]["tokens"], x[0])):
        span = "-"
        if a["starts"] and a["ends"]:
            span = f"{short_time(min(a['starts']))}–{short_time(max(a['ends']))}"
        rows.append({
            "who": w,
            "key": "是" if w in roster_names else "否",
            "role": orgdept(by_name.get(w) or {}),
            "q": a["q"],
            "prev": prev_humans.get(w, 0),
            "status": status_text(a["statuses"]),
            "topics": topics_text(a["topics"]),
            "span": span,
            "tokens": f"{a['tokens']:,}",
        })
    return rows, unknown


def channel_text(by_source: dict) -> str:
    items = [(s, c) for s, c in by_source.items() if c]
    items.sort(key=lambda x: (-x[1], SRC_ORDER.index(x[0]) if x[0] in SRC_ORDER else 99))
    return "、".join(f"{SRC_LABEL.get(s, s)} {c}" for s, c in items) or "-"


def anomaly_items(f: dict, limit: int = 5) -> list[str]:
    pg = f.get("pg") or {}
    t = pg.get("today") or {}
    k = pg.get("key_users") or {}
    lg = f.get("logs") or {}
    attr = pg.get("attribution") or {}
    out = []
    if not pg.get("ok"):
        out.append(f"[降级] {(f.get('degraded') or ['PG 不可用'])[0]}")
    if f.get("is_weekend"):
        out.append("[周末基线] 当日为周末，低活跃属正常节律，环比不与工作日直接同比")
    bad = []
    for key, label in (("q_failed", "失败"), ("q_cancelled", "中断"),
                       ("q_review", "待复核"), ("q_conflict", "冲突待裁")):
        if t.get(key):
            bad.append(f"{label} {t[key]}")
    if bad:
        out.append(f"交付质量：提问级成功率 {n(t.get('q_success_rate'))}%，" + "、".join(bad))
    anom = collections.Counter(a.get("action") for a in (lg.get("anomalies") or []) if a.get("action"))
    if anom:
        out.append("审计异常：" + "；".join(f"{a}×{c}" for a, c in anom.most_common(3)))
    over30 = [s.get("name") for s in (k.get("silence") or []) if s.get("over_30d")]
    if over30:
        out.append(f"重点用户静默：{len(over30)} 人超 30 天无主动查询（{'、'.join(over30)}）")
    if attr.get("unknown"):
        out.append(f"归属缺口：{attr['unknown']} 条提问无法归属到人，不参与人员排名")
    return out[:limit]


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
    name = f.get("display_name") or LUDASHI
    w = f.get("window") or {}
    trade = "非交易日（周末）" if weekend else "交易日（未扣除法定节假日）"

    L.append(f"# {name}用户日度使用分析报告（{date}）")
    L.append("")
    L.append(f"**统计范围**：{w.get('start','-')} 至 {w.get('end','-')}"
             f"（{w.get('tz','Asia/Shanghai')}，{f.get('weekday','')}{trade}）｜"
             f"**生成**：{f.get('snapshot_time','-')}／{mode}／{TEMPLATE_VERSION}")
    L.append("")
    if not ok:
        L.append("> **[降级]** " + (f.get("degraded") or ["PG 不可用"])[0])
        L.append("> 本报告仅含本机日志口径，任务级与用户级指标缺失，不可用于经营判断。")
        L.append("")

    # ══ 1. 人员使用情况（置顶）
    L.append("## 1. 人员使用情况")
    L.append("")
    rows, unknown = aggregate_users(f)
    if rows:
        L.append(tbl(
            ["姓名", "重点", "岗位/部门", "当日提问", "前日", "完成/异常", "主要需求", "活跃时段", "token"],
            [[r["who"], r["key"], r["role"], r["q"], r["prev"], r["status"],
              r["topics"], r["span"], r["tokens"]] for r in rows],
        ))
    else:
        L.append("当日无人工提问。")
    if unknown:
        L.append("")
        L.append(f"> `unknown` 另有 {unknown} 条无归属提问，不占人员排名。")
    L.append("")

    sil = [s for s in (k.get("silence") or []) if not s.get("active_today")]
    L.append(f"### 当日无活动重点用户（{len(sil)} 人）")
    L.append("")
    if sil:
        L.append(tbl(
            ["姓名", "岗位/部门", "最近活动", "静默天数", "超30天"],
            [[s.get("name", "-"), orgdept(s), s.get("last_seen", "-"),
              n(s.get("silent_days")), "是" if s.get("over_30d") else "否"] for s in sil],
        ))
    else:
        L.append("重点用户当日均有活动。")
    L.append("")

    # ══ 2. 核心汇总
    L.append("## 2. 核心汇总")
    L.append("")
    per = round(t.get("questions", 0) / t["active_users"], 1) if t.get("active_users") else "-"
    tokens = sum(int(q.get("tokens") or 0) for q in (pg.get("questions") or []))
    inbound_kinds = "、".join(f"{a}{b}" for a, b in (lg.get("inbound_kinds") or {}).items()) or "-"
    L.append(f"- **用量**：人工提问 **{n(t.get('questions'))}** 条（环比 {d_(t.get('questions'), p.get('questions'))}）"
             f"｜活跃 **{n(t.get('active_users'))}** 人｜人均 {per} 条")
    L.append(f"- **重点用户**：{n(k.get('active'))}/{n(k.get('roster_size'))} 活跃"
             f"（{n(k.get('active_rate'))}%，环比 {d_(k.get('active'), k.get('prev_active'))}）"
             f"｜静默 {len(sil)} 人")
    L.append(f"- **交付**：提问级成功率 **{n(t.get('q_success_rate'))}%**"
             f"（完成 {n(t.get('q_done'))}｜失败 {n(t.get('q_failed'))}｜中断 {n(t.get('q_cancelled'))}"
             f"｜待复核 {n(t.get('q_review'))}｜冲突 {n(t.get('q_conflict'))}）")
    L.append(f"- **渠道**：{channel_text(t.get('by_source') or {})}"
             f"（任务行 {n(t.get('task_rows'))}，影子执行去重后 {n(t.get('questions'))} 条提问）")
    L.append(f"- **消息/算力**：入站 {n(lg.get('inbound_msgs'))} 条（{inbound_kinds}）"
             f"｜多轮会话 {n(lg.get('multi_round_sessions'))} 个｜token {tokens:,}")
    L.append("")

    # ══ 3. 异常与建议
    L.append("## 3. 异常与建议")
    L.append("")
    items = anomaly_items(f)
    if items:
        for x in items:
            L.append(f"- {x}")
    else:
        L.append("- 当日无显著异常。")
    L.append("")
    L.append("**建议**：{{待填：针对上述异常给出不超过 3 条可执行建议，每条写清动作、优先级、责任/归属；无异常则填「无」}}")
    L.append("")

    # ══ 4. 口径与缺口
    L.append("## 4. 口径与缺口")
    L.append("")
    L.append("- **口径**：数字取自预采集 `facts.json`；人工提问已按影子执行去重；渠道按 `tasks.project` 判别；"
             "cron / api_server / system 无自然人绑定，不纳入人员排名；报告不留存提问原文与交付正文。")
    L.append("- **缺口**：`prediction_log`、`acp`、`飞书` 本机无数据源，照实标 `[缺档]`；"
             "交易日仅按周末判定，未接法定节假日日历。")
    if f.get("gaps"):
        extra = [x for x in f["gaps"] if "prediction_log" not in x
                 and "--source all" not in x and "acp" not in x]
        if extra:
            L.append(f"- **其他缺口**：{'；'.join(extra[:3])}")
    L.append("")
    return "\n".join(L)


def render_summary(f: dict) -> str:
    """推送用摘要：人员使用置顶，不含任何任务原文。"""
    pg = f.get("pg") or {}
    lg = f.get("logs") or {}
    date = f["report_date"]
    wk = f.get("weekday", "")
    trade = "非交易日" if f.get("is_weekend") else "交易日"
    name = f.get("display_name") or LUDASHI

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
    rows, unknown = aggregate_users(f)
    top = rows[:8]
    top_txt = "、".join(f"{r['who']} {r['q']}" for r in top) or "当日无人工提问"
    if len(rows) > len(top):
        top_txt += f"（共 {len(rows)} 人）"
    if unknown:
        top_txt += f"；unknown {unknown}"
    sil = [s for s in (k.get("silence") or []) if not s.get("active_today")]
    over30 = [s.get("name") for s in sil if s.get("over_30d")]
    per = round(t.get("questions", 0) / t["active_users"], 1) if t.get("active_users") else "-"
    tokens = sum(int(q.get("tokens") or 0) for q in (pg.get("questions") or []))
    anom = anomaly_items(f, limit=3)
    anom_txt = "；".join(anom) or "无"

    L = [
        f"**{name}用户日度使用分析（{date} {wk}·{trade}）**",
        "",
        f"- **人员使用**：{top_txt}",
        f"- **重点用户**：{k.get('active', 0)}/{k.get('roster_size', 0)} 活跃"
        f"（{k.get('active_rate', '-')}%）｜静默 {len(sil)} 人"
        + (f"｜超30天：{'、'.join(over30)}" if over30 else ""),
        f"- **核心汇总**：人工提问 **{t.get('questions', 0)}** 条（环比 {d_(t.get('questions'), p.get('questions'))}）"
        f"｜活跃 **{t.get('active_users', 0)}** 人｜人均 {per} 条｜成功率 **{t.get('q_success_rate', '-')}%**",
        f"- **渠道/算力**：{channel_text(t.get('by_source') or {})}（任务行 {t.get('task_rows', 0)}）"
        f"｜入站 {lg.get('inbound_msgs', 0)} 条｜多轮 {lg.get('multi_round_sessions', 0)} 个｜token {tokens:,}",
        f"- **异常/提示**：{anom_txt}",
        "",
        "> 完整件见附件，按人员使用置顶、两页内精简口径生成；不留存提问原文与交付正文。",
    ]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染本机分身用户日度使用分析报告")
    ap.add_argument("--facts")
    ap.add_argument("--out")
    ap.add_argument("--mode", default="自动-skill", help="人工 / 自动-skill")
    ap.add_argument("--summary", action="store_true",
                    help="只输出推送用摘要（人员使用置顶，不含任务原文）；推送一律「摘要＋文件」")
    ap.add_argument("--name",
                    help="覆盖报告展示名（缺省取 facts.display_name，再缺省按机解析：本机鲁大师/其他机 avatar_name）")
    ap.add_argument("--print-name", action="store_true",
                    help="只打印本机报告展示名（供 shell 拼报告文件名），不渲染报告")
    a = ap.parse_args()

    if a.print_name and not a.facts:
        print(a.name or _resolve_display_name())
        return 0
    if not a.facts:
        ap.error("--facts 必填（除非用 --print-name 单独取展示名）")

    f = json.load(open(a.facts, encoding="utf-8"))
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
        extra = "" if a.summary else f", {md.count(chr(10)) + 1} 行, {todo} 处待填叙述位"
        print(f"{kind} → {a.out}  ({len(md)} 字符{extra})", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
