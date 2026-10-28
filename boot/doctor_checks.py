#!/usr/bin/env python3
"""doctor_checks.py —— relay-doctor 机队日检套件（设计与检查目录见 boot/DOCTOR.md）。

架构：采集器（薄、带副作用、逐个 try 兜底成 warn）与判级器（纯函数，被
relay-service/tests/test_doctor.py 密封单测覆盖，进夜间门禁）分离。
doctor.sh 只负责加锁、跑本套件、把日报 DM 出去；本模块负责检查、判级、
趋势采样、状态/历史落盘与日报渲染。

性能红线（机队要求「影响降到最低」）：
- 所有网络调用带紧超时（3~30s）；journalctl 限 24h、du 限指定目录且 60s 封顶；
- engine_smoke 每天至多 2 次真实模型调用（各 120s 上限），DOCTOR_SMOKE=0 可关；
- 其余网关探测走 GET /models（零推理 token）；
- 进程 oneshot 跑完即退（单元另带 Nice/IOSchedulingClass=idle）；
- history 每天一行、reports 只留 30 天，自清理。

安全红线：relay.env / opencode.jsonc 只读取、绝不回显值；日报与日志不含任何密钥。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

# ── 配置（env 可覆盖；默认值即机队标准布局，boot.env 的 DOCTOR_* 透传进来）──
WORKSPACE = os.environ.get("DOCTOR_WORKSPACE", "/mnt/vol-eltaah12/workspace")
DOCTOR_DIR = os.environ.get("DOCTOR_DIR", os.path.join(WORKSPACE, "doctor"))
RELAY_ENV = os.environ.get("DOCTOR_RELAY_ENV", os.path.join(WORKSPACE, "relay.env"))
RELAY_BASE = os.environ.get("DOCTOR_RELAY_BASE", "http://127.0.0.1:8787")
REPO_URL = os.environ.get("DOCTOR_REPO_URL", "git@github.com:koyomaic/business_advisor.git")
CURRENT_LINK = os.environ.get("DOCTOR_CURRENT_LINK", "/mnt/vol-eltaah12/finaagent/relay-service")
AVATAR_DIR = os.environ.get("DOCTOR_AVATAR_DIR", os.path.join(WORKSPACE, "avatar"))
AVATAR_INSTALL = os.environ.get("DOCTOR_AVATAR_INSTALL", "/opt/team/relay-avatar")
BACKUP_DIR = os.environ.get("DOCTOR_BACKUP_DIR", "/mnt/vol-eltaah12/backup")
PLAN_LOG = os.environ.get("DOCTOR_PLAN_LOG", "/var/log/relay-plan.log")
BOOT_LOG = os.environ.get("DOCTOR_BOOT_LOG", "/var/log/relay-boot.log")
OC_CONFIG = os.environ.get("DOCTOR_OPENCODE_CONFIG", "/root/.config/opencode/opencode.jsonc")
HOST = socket.gethostname()
HOST_CN = os.environ.get("DOCTOR_HOST_CN", HOST)


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


DISK_WARN_FREE_PCT = _num("DOCTOR_DISK_WARN_PCT", 15)    # 任一挂载点剩余低于此 → WARN
DISK_CRIT_FREE_PCT = _num("DOCTOR_DISK_CRIT_PCT", 8)
FORECAST_WARN_DAYS = _num("DOCTOR_FORECAST_WARN_DAYS", 14)
FORECAST_CRIT_DAYS = _num("DOCTOR_FORECAST_CRIT_DAYS", 5)
FLEET_MAX_CONCURRENT = int(_num("DOCTOR_FLEET_MAX_CONCURRENT", 8))
DIR_GROW_WARN_MB = _num("DOCTOR_DIR_GROW_WARN_MB", 500)
SMOKE = os.environ.get("DOCTOR_SMOKE", "1") != "0"
HISTORY_KEEP_DAYS = int(_num("DOCTOR_HISTORY_DAYS", 90))
REPORT_KEEP_DAYS = int(_num("DOCTOR_REPORT_DAYS", 30))


def mk(cid: str, cat: str, status: str, detail: str = "", **metrics) -> dict:
    return {"id": cid, "cat": cat, "status": status,
            "detail": str(detail)[:400], "metrics": metrics}


# ══════════════════ 判级器（纯函数，密封单测覆盖）══════════════════

def overall(checks: list[dict]) -> str:
    if any(c["status"] == "crit" for c in checks):
        return "crit"
    if any(c["status"] == "warn" for c in checks):
        return "warn"
    return "ok"


def eval_disk(usages: list[dict], warn_pct: float = DISK_WARN_FREE_PCT,
              crit_pct: float = DISK_CRIT_FREE_PCT) -> dict:
    """usages: [{mount, free_pct, inode_free_pct|None}]。剩余空间/inode 双阈值。"""
    if not usages:
        return mk("disk_space", "res", "warn", "读不到任何真实挂载点")

    def inode(u):
        v = u.get("inode_free_pct")
        return 100.0 if v is None else v

    ordered = sorted(usages, key=lambda u: u["free_pct"])
    parts = []
    for u in ordered:
        seg = f"{u['mount']} 余{u['free_pct']:.0f}%"
        if inode(u) < 20:
            seg += f"(inode余{inode(u):.0f}%)"
        parts.append(seg)
    st = "ok"
    if ordered[0]["free_pct"] < crit_pct or any(inode(u) < 10 for u in usages):
        st = "crit"
    elif ordered[0]["free_pct"] < warn_pct:
        st = "warn"
    return mk("disk_space", "res", st, " · ".join(parts),
              min_free_pct=round(ordered[0]["free_pct"], 1))


def eval_forecast(series_by_mount: dict, warn_days: float = FORECAST_WARN_DAYS,
                  crit_days: float = FORECAST_CRIT_DAYS, now: float | None = None) -> dict:
    """按挂载点做 7 天最小二乘线性外推「几天到 95% 占用」。样本不足只积累不判。"""
    now = now or time.time()
    got, notes, worst = 0, [], None
    for mount, series in (series_by_mount or {}).items():
        pts = [(t, p) for t, p in (series or []) if t and now - t <= 8 * 86400]
        if len(pts) < 3:
            continue
        got += 1
        n = len(pts)
        xs = [t / 86400.0 for t, _ in pts]
        ys = [p for _, p in pts]
        den = n * sum(x * x for x in xs) - sum(xs) ** 2
        if den == 0:
            continue
        slope = (n * sum(x * y for x, y in zip(xs, ys)) - sum(xs) * sum(ys)) / den
        last = ys[-1]
        if slope <= 0.02 or last >= 95:
            continue  # 增长可忽略 / 已满（由 disk_space 直接点名）
        days = (95.0 - last) / slope
        notes.append(f"{mount} 增速{slope:.1f}%/日≈{days:.0f}天到95%")
        if worst is None or days < worst:
            worst = days
    if got == 0:
        return mk("disk_forecast", "res", "info", "外推样本<3 天，积累中")
    st = "ok"
    if worst is not None:
        st = "crit" if worst < crit_days else ("warn" if worst < warn_days else "ok")
    return mk("disk_forecast", "res", st, "; ".join(notes) or "增长平稳",
              days=round(worst, 1) if worst is not None else None)


def eval_tasks(failed: list[dict], crit_n: int = 3) -> dict:
    """failed: 24h 内 failed 任务 [{id,user,err}]。≥1 WARN，≥3 CRIT；错误聚类给前 2 类。"""
    n = len(failed or [])
    to = sum(1 for f in failed
             if re.search(r"timeout|超时|timed out", str(f.get("err") or ""), re.I))
    if n == 0:
        return mk("task_failed_24h", "biz", "ok", "24h 无失败任务", failed=0, timeout=0)
    clusters: dict[str, list] = {}
    for f in failed:
        k = re.sub(r"\s+", " ", str(f.get("err") or "").strip())[:60] or "无错误文本"
        clusters.setdefault(k, []).append(f"#{f.get('id')}({f.get('user', '?')})")
    top = sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:2]
    detail = f"24h 失败 {n}（超时 {to}）：" + "; ".join(
        f"{k}×{len(v)} {' '.join(v[:3])}" for k, v in top)
    return mk("task_failed_24h", "biz", "crit" if n >= crit_n else "warn",
              detail, failed=n, timeout=to)


def eval_queue(queued: int, oldest_age_s: float, active: int, max_concurrent: int) -> dict:
    st = "warn" if (queued > 5 or (queued and oldest_age_s > 600)) else "ok"
    return mk("queue_backlog", "biz", st,
              f"排队 {queued}（最老 {int(oldest_age_s)}s）· 在跑 {active}/{max_concurrent}",
              queued=queued, oldest_age_s=int(oldest_age_s))


def eval_approval(items: list[dict], timeout_min: float = 60) -> dict:
    """pending_approval 滞留：审批窗默认 60min，过半即提醒（超时会被自动 failed）。"""
    n = len(items or [])
    if not n:
        return mk("approval_stuck", "biz", "ok", "无滞留审批")
    detail = "待审批 " + ", ".join(
        f"#{i.get('id')}({int((i.get('age_s') or 0) / 60)}min/{timeout_min:.0f}min窗)"
        for i in items[:4])
    return mk("approval_stuck", "biz", "warn", detail, pending=n)


def eval_backup(age_h, size, prev_sizes, kept, timer_active, dir_exists) -> dict:
    if not dir_exists and not timer_active:
        return mk("backup_fresh", "data", "info",
                  "无备份目录且 relay-backup.timer 未启用（云备份模式？）")
    if not timer_active:
        return mk("backup_fresh", "data", "warn", "relay-backup.timer 未激活")
    if age_h is None or size is None:
        return mk("backup_fresh", "data", "crit", "timer 在跑但找不到备份文件")
    med = sorted(prev_sizes)[len(prev_sizes) // 2] if prev_sizes else None
    st, notes = "ok", [f"{age_h:.1f}h 前 {size / 1e6:.1f}MB kept={kept}"]
    if age_h > 26:
        st = "crit"
        notes.append("超 26h 未出新备份")
    if med and (size < 0.5 * med or size > 3 * med):
        if st == "ok":
            st = "warn"
        notes.append(f"大小异常（近期中位 {med / 1e6:.0f}MB）")
    return mk("backup_fresh", "data", st, " ".join(notes),
              age_h=round(age_h, 1), size_mb=round(size / 1e6, 1))


def eval_versions(old: dict, new: dict) -> dict:
    """依赖 CLI 版本指纹 diff：被升级要看得见（2026-10-28 opencode 事故教训）。"""
    if not old:
        return mk("version_drift", "dep", "info",
                  "版本指纹已建立：" + " ".join(f"{k}={v}" for k, v in new.items()))
    changed = [f"{k} {old[k]}→{v}" for k, v in new.items() if k in old and old[k] != v]
    if not changed:
        return mk("version_drift", "dep", "ok", "依赖版本无变化")
    return mk("version_drift", "dep", "warn", "依赖被升级：" + "; ".join(changed))


def eval_cert(days) -> dict:
    if days is None:
        return mk("cert_expiry", "data", "warn", "读不到 8788 证书到期时间")
    st = "crit" if days < 3 else ("warn" if days < 14 else "ok")
    return mk("cert_expiry", "data", st, f"8788 证书剩 {days:.0f} 天", days=round(days, 1))


def eval_debt(health: dict, fleet_mc: int = FLEET_MAX_CONCURRENT) -> dict:
    """已知技术债点名（可 ack 静默）：auth_legacy 过渡开关、并发偏离机队期望。"""
    issues = []
    if health.get("auth_legacy"):
        issues.append("auth_legacy=on（旧式 bearer 认证未收敛）")
    mc = health.get("max_concurrent")
    if mc is not None and mc != fleet_mc:
        issues.append(f"max_concurrent={mc}≠机队期望{fleet_mc}")
    if issues:
        return mk("debt_tracker", "data", "warn", "技术债未清：" + "; ".join(issues))
    return mk("debt_tracker", "data", "ok", "无已知技术债告警")


def eval_provision(pass_n, warn_n, fail_n, baseline) -> dict:
    if fail_n:
        return mk("provision_recap", "meta", "crit",
                  f"最近 provision FAIL={fail_n}（PASS={pass_n} WARN={warn_n}）")
    if baseline is not None and warn_n is not None and warn_n > baseline:
        return mk("provision_recap", "meta", "warn",
                  f"provision WARN {baseline}→{warn_n}（PASS={pass_n}）")
    return mk("provision_recap", "meta", "ok",
              f"provision PASS={pass_n} WARN={warn_n} FAIL=0", warn=warn_n)


def eval_self(last_run, now: float) -> dict:
    if not last_run:
        return mk("doctor_self", "meta", "info", "首次运行，无历史可比")
    age_h = (now - last_run) / 3600
    if age_h > 26:
        return mk("doctor_self", "meta", "warn", f"上次体检距今 {age_h:.0f}h（>26h，漏跑？）")
    return mk("doctor_self", "meta", "ok", f"上次体检 {age_h:.1f}h 前")


def apply_acks(checks: list[dict], acks: dict, now: float | None = None):
    """ack 生效期内 warn/crit 降级为 info（保留原文），返回 (checks, acked 列表)。"""
    now = now or time.time()
    acked = []
    for c in checks:
        a = (acks or {}).get(c["id"])
        if not a or c["status"] not in ("warn", "crit"):
            continue
        until = float(a.get("until") or 0)
        if until <= now:
            continue
        left = (until - now) / 86400
        c["status"] = "info"
        c["detail"] = (f"🔕已静默({a.get('reason', '')}剩{left:.0f}天) "
                       f"原:{c['detail'][:120]}")
        acked.append((c["id"], round(left)))
    return checks, acked


def render_digest(checks: list[dict], now: float, dur_s: float, report_path: str) -> str:
    """DM 日报（markdown，紧凑）：总评 + crit/warn 行 + 绿色摘要行。"""
    ov = overall(checks)
    icon = {"ok": "✅", "warn": "🟡", "crit": "🔴"}[ov]
    crits = [c for c in checks if c["status"] == "crit"]
    warns = [c for c in checks if c["status"] == "warn"]
    acked = [c for c in checks if c["status"] == "info" and c["detail"].startswith("🔕")]

    def g(cid, k, d="—"):
        for c in checks:
            if c["id"] == cid:
                v = c.get("metrics", {}).get(k)
                return d if v is None else v
        return d

    head = (f"# 🩺 机队体检 · {HOST_CN}({HOST}) · "
            f"{time.strftime('%m-%d %H:%M', time.localtime(now))}\n")
    if ov == "ok":
        return (head +
                f"**✅ 全绿**（{len(checks)} 项 · {dur_s:.0f}s）｜任务 "
                f"{g('task_failed_24h', 'failed')} 失败｜盘最低余 {g('disk_space', 'min_free_pct')}%"
                f"｜服务 {g('service_recap', 'n_ok')}/{g('service_recap', 'n_total')}"
                + (f"｜🔕 已静默 {len(acked)} 项" if acked else "")
                + f"\n📄 {report_path}")[:2200]
    out = [head + f"**{icon} 总评 {len(warns)} WARN / {len(crits)} CRIT**"
                 f"（{len(checks)} 项 · {dur_s:.0f}s）"]
    out += [f"🔴 {c['id']}: {c['detail']}" for c in crits[:6]]
    out += [f"🟡 {c['id']}: {c['detail']}" for c in warns[:8]]
    if len(warns) > 8:
        out.append(f"🟡 …另 {len(warns) - 8} 项见日报")
    out.append(
        f"✅ 任务 {g('task_failed_24h', 'failed')} 失败 · 排队 {g('queue_backlog', 'queued')}"
        f" · 盘余 {g('disk_space', 'min_free_pct')}% · 备份 {g('backup_fresh', 'age_h')}h 前"
        f" · 服务 {g('service_recap', 'n_ok')}/{g('service_recap', 'n_total')}")
    if acked:
        out.append(f"🔕 已静默 {len(acked)} 项")
    out.append(f"📄 {report_path}")
    return "\n".join(out)[:2200]


def render_report(checks: list[dict], now: float, dur_s: float) -> str:
    """完整日报文件：全部检查按类别分组，含指标。"""
    ov = overall(checks)
    lines = [f"# relay-doctor 体检日报 · {HOST_CN}({HOST})",
             f"时间：{time.strftime('%F %T', time.localtime(now))}　总评：**{ov.upper()}**"
             f"　检查 {len(checks)} 项　耗时 {dur_s:.0f}s", ""]
    cat_names = {"biz": "任务与业务面", "res": "资源与空间", "dep": "依赖与漂移",
                 "data": "数据与安全", "meta": "体检自身"}
    for cat in ("biz", "res", "dep", "data", "meta"):
        group = [c for c in checks if c["cat"] == cat]
        if not group:
            continue
        lines.append(f"## {cat_names.get(cat, cat)}")
        for c in group:
            mark = {"ok": "✅", "info": "ℹ️", "skip": "⏭️", "warn": "🟡", "crit": "🔴"}[c["status"]]
            lines.append(f"- {mark} **{c['id']}**｜{c['detail']}")
        lines.append("")
    return "\n".join(lines)


# ══════════════════ 采集器（薄，带副作用；run_all 逐个 guarded 调用）══════════════════

def read_env_file(path: str) -> dict:
    kv = {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                if ln.lstrip().startswith("#"):
                    continue
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$", ln)
                if m:
                    kv[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return kv


def _run(cmd: list, cwd: str | None = None, env: dict | None = None,
         timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "")[:4000], (p.stderr or "")[:4000]
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout {timeout}s"
    except OSError as e:
        return 127, "", str(e)


def http_json(url: str, headers: dict | None = None, timeout: int = 10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _relay_fetch_all(env: dict) -> dict:
    """一次会话把 relay 侧数据全拿完（health/tasks/plans），多个检查共用。"""
    tok = env.get("RELAY_ADMIN_TOKEN", "")
    H = {"Authorization": f"Bearer {tok}"} if tok else {}
    out = {"health": http_json(RELAY_BASE + "/health", timeout=5)}
    for key, path in (("failed", "/tasks?status=failed&limit=50"),
                      ("queued", "/tasks?status=queued&limit=50"),
                      ("approval", "/tasks?status=pending_approval&limit=50"),
                      ("plans", "/plans")):
        try:
            data = http_json(RELAY_BASE + path, H, timeout=10)
            out[key] = data.get("tasks") or data.get("plans") or []
        except Exception:
            out[key] = None  # 单项失败不拖垮整组；对应检查降级
    return out


RELAY_GROUP = ("relay_health", "task_failed_24h", "queue_backlog",
               "approval_stuck", "plan_overdue", "debt_tracker")


def check_relay(env: dict) -> list[dict]:
    try:
        data = _relay_fetch_all(env)
    except Exception as e:
        return [mk("relay_health", "biz", "crit",
                   f"relay 不可达：{type(e).__name__}: {e}"[:200])] + \
               [mk(i, "biz", "skip", "relay 不可达") for i in RELAY_GROUP[1:]]
    h = data["health"]
    now = time.time()
    checks = [mk("relay_health", "biz", "ok",
                 f"healthy v={str(h.get('version'))[:8]} db={h.get('db')} "
                 f"engine={h.get('engine')}")]
    failed = [{"id": t.get("id"), "user": t.get("user"), "err": t.get("result") or ""}
              for t in data["failed"]
              if (t.get("created_at") or 0) >= now - 86400] \
        if data.get("failed") is not None else None
    checks.append(eval_tasks(failed) if failed is not None
                  else mk("task_failed_24h", "biz", "warn", "failed 任务列表读取失败"))
    q = data.get("queued")
    if q is not None:
        oldest = min((t.get("created_at") or now for t in q), default=now)
        checks.append(eval_queue(len(q), now - oldest if q else 0,
                                 h.get("active", 0), h.get("max_concurrent", 0)))
    else:
        checks.append(mk("queue_backlog", "biz", "warn", "排队列表读取失败"))
    ap = data.get("approval")
    if ap is not None:
        checks.append(eval_approval([
            {"id": t.get("id"),
             "age_s": now - (t.get("blocked_at") or t.get("created_at") or now)}
            for t in ap]))
    else:
        checks.append(mk("approval_stuck", "biz", "warn", "审批列表读取失败"))
    pl = data.get("plans")
    if pl is not None:
        overdue = []
        for p in pl:
            if not p.get("enabled"):
                continue
            if p.get("next_run") is None:
                overdue.append(f"#{p.get('id')} {p.get('name')}(无下次触发,表达式坏?)")
            elif p["next_run"] < now - 3600:
                overdue.append(f"#{p.get('id')} {p.get('name')}")
        checks.append(mk("plan_overdue", "biz", "warn" if overdue else "ok",
                         ("逾期>1h 未触发：" + ", ".join(overdue[:5]))[:200]
                         if overdue else f"计划 {len(pl)} 个，无逾期",
                         overdue=len(overdue)))
    else:
        checks.append(mk("plan_overdue", "biz", "warn", "计划列表读取失败"))
    checks.append(eval_debt(h))
    return checks


def check_pg(env: dict) -> dict:
    dburl = env.get("RELAY_DB", "")
    if not dburl:
        return mk("pg_health", "data", "info", "relay.env 无 RELAY_DB（SQLite 模式？）")
    try:
        import psycopg2
    except ImportError:
        return mk("pg_health", "data", "info",
                  "psycopg2 不可用，跳过直连（/health 已含 db 字段）")
    try:
        conn = psycopg2.connect(dburl, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return mk("pg_health", "data", "ok", "PG 直连 SELECT 1 通过")
    except Exception as e:
        return mk("pg_health", "data", "crit",
                  f"PG 直连失败：{type(e).__name__}: {str(e)[:120]}")


def collect_disk() -> list[dict]:
    usages, seen = [], set()
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for ln in f:
                bits = ln.split()
                if len(bits) < 3 or not bits[0].startswith("/dev/"):
                    continue
                mp = bits[1].replace("\\040", " ")
                if mp in seen:
                    continue
                seen.add(mp)
                try:
                    du = shutil.disk_usage(mp)
                    sv = os.statvfs(mp)
                    usages.append({"mount": mp,
                                   "free_pct": du.free / du.total * 100,
                                   "inode_free_pct": (sv.f_ffree / sv.f_files * 100)
                                   if sv.f_files else None})
                except OSError:
                    continue
    except OSError:
        pass
    return usages


def _disk_series(hist: list[dict], usages: list[dict], now: float) -> dict:
    """history 里的逐日占用 + 本次采样，拼成外推输入。"""
    series: dict[str, list] = {}
    for rec in hist[-8:]:
        ts = rec.get("ts") or 0
        for mp, used in (rec.get("m", {}).get("disk", {}) or {}).items():
            series.setdefault(mp, []).append([ts, used])
    for u in usages:
        series.setdefault(u["mount"], []).append([now, 100 - u["free_pct"]])
    return series


def du_mb(path: str) -> float | None:
    rc, out, _ = _run(["du", "-sb", path], timeout=60)
    if rc != 0 or not out.split():
        return None
    try:
        return int(out.split()[0]) / 1e6
    except ValueError:
        return None


def check_dir_growth(prev_dirs: dict) -> dict:
    targets = {"tasks": os.path.join(WORKSPACE, "tasks"),
               "avatar": AVATAR_DIR,
               "releases": os.path.dirname(os.path.dirname(os.path.realpath(CURRENT_LINK))),
               "varlog": "/var/log"}
    cur = {k: v for k, p in targets.items()
           if os.path.isdir(p) and (v := du_mb(p)) is not None}
    if not prev_dirs:
        return mk("dir_growth", "res", "info",
                  "目录基线已建立：" + " ".join(f"{k}={v:.0f}MB" for k, v in cur.items()),
                  dirs=cur)
    deltas = {k: cur[k] - prev_dirs.get(k, cur[k]) for k in cur}
    big = [f"{k}+{d:.0f}MB/日" for k, d in deltas.items() if d > DIR_GROW_WARN_MB]
    return mk("dir_growth", "res", "warn" if big else "ok",
              ("增长异常：" + "; ".join(big)) if big
              else "增长正常：" + " ".join(f"{k}{d:+.0f}MB" for k, d in deltas.items()),
              dirs=cur)


def check_services(state: dict) -> dict:
    units = ["relay", "relay-avatar", "relay-tls-proxy", "claude-proxy"]
    live, total, restarts, notes = 0, 0, 0, []
    prev = state.get("svc_restarts", {})
    cur_rp, rss = {}, {}
    for u in units:
        rc, out, _ = _run(["systemctl", "show", u, "-p", "LoadState", "-p", "ActiveState",
                           "-p", "NRestarts", "-p", "MemoryCurrent", "--no-pager"])
        kv = dict(x.split("=", 1) for x in out.splitlines() if "=" in x)
        if rc != 0 or kv.get("LoadState") != "loaded":
            continue  # 单元不存在（该机未装此组件）不计
        total += 1
        if kv.get("ActiveState") == "active":
            live += 1
        else:
            notes.append(f"{u}:{kv.get('ActiveState')}")
        nr = int(kv.get("NRestarts") or 0)
        cur_rp[u] = nr
        restarts += max(0, nr - int(prev.get(u, nr)))
        mc = kv.get("MemoryCurrent", "")
        if mc.isdigit():
            rss[u] = round(int(mc) / 1e6, 1)
    st = "crit" if total and live < total else ("warn" if restarts else "ok")
    detail = f"服务 {live}/{total} active" + (f" · 24h 重启 {restarts}" if restarts else "")
    if notes:
        detail += " · 不在活：" + ",".join(notes)
    c = mk("service_recap", "res", st, detail, n_ok=live, n_total=total, restarts=restarts)
    c["_side"] = {"svc_restarts": cur_rp, "rss": rss}
    return c


def check_timers() -> dict:
    """机队关键定时器在位且激活（doctor 自己也在名单里——看门狗也要被看）。"""
    units = ["relay-boot.timer", "relay-backup.timer", "relay-plan.timer",
             "relay-doctor.timer"]
    inactive = []
    for t in units:
        if not os.path.exists(f"/etc/systemd/system/{t}"):
            continue  # 该机未装此定时器（如 backup 被裁掉）不计
        rc, _, _ = _run(["systemctl", "is-active", "--quiet", t], timeout=10)
        if rc != 0:
            inactive.append(t)
    return mk("timer_stuck", "meta", "warn" if inactive else "ok",
              ("定时器未激活：" + ",".join(inactive)) if inactive
              else "关键定时器全部激活", inactive=inactive)


def check_journal() -> dict:
    units = ["relay", "relay-avatar", "relay-tls-proxy", "claude-proxy",
             "relay-boot.service", "relay-backup.service", "relay-plan.service"]
    cmd = ["journalctl", "-q", "--no-pager", "--since", "-24h", "-p", "err"]
    for u in units:
        cmd += ["-u", u]
    rc, out, _ = _run(cmd, timeout=30)
    lines = [x for x in (out or "").splitlines() if x.strip()]
    n = len(lines)
    st = "crit" if n > 10 else ("warn" if n else "ok")
    tail = " ‖ ".join(x.strip()[:120] for x in lines[-2:])
    return mk("journal_errors", "res", st,
              f"24h err 日志 {n} 条" + (f"：{tail}" if n else ""), n=n)


_STANDALONE_CACHE: dict[str, bool] = {}


def _standalone_ok(bin_path: str) -> bool:
    hit = _STANDALONE_CACHE.get(bin_path)
    if hit is None:
        rc, out, err = _run([bin_path, "run", "--help"], timeout=10)
        hit = "--standalone" in ((out or "") + (err or ""))
        _STANDALONE_CACHE[bin_path] = hit
    return hit


def check_engine_smoke(env: dict) -> dict:
    """真跑一次 opencode 与 claude 的最小往返——2026-10-28 事故的直接探测器。

    全日预算仅此 2 次模型调用（各 120s 上限）；DOCTOR_SMOKE=0 可关。
    沙箱镜像生产：全新 HOME/XDG_DATA + 真实 XDG_CONFIG（provider 配置所在），
    claude 侧 ANTHROPIC_*/CLAUDE_* 从 relay.env 透传、绝不回显值。
    """
    if not SMOKE:
        return mk("engine_smoke", "dep", "info", "DOCTOR_SMOKE=0，已跳过")
    tmp = tempfile.mkdtemp(prefix="relay-doctor-smoke-", dir="/tmp")
    results = []
    try:
        oc = shutil.which("opencode")
        if oc:
            for d in ("home", "data"):
                os.makedirs(os.path.join(tmp, d), exist_ok=True)
            senv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.path.join(tmp, "home"),
                    "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME") or "/root/.config",
                    "XDG_DATA_HOME": os.path.join(tmp, "data")}
            cmd = [oc, "run", "--auto"]
            if _standalone_ok(oc):
                cmd.append("--standalone")
            cmd.append("Reply with exactly one word: ok")
            rc, out, err = _run(cmd, cwd=tmp, env=senv, timeout=120)
            results.append(("opencode", rc == 0 and bool(out.strip()),
                            (err or out).strip()[:120]))
        cl = shutil.which("claude")
        if cl:
            cenv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.path.join(tmp, "home"),
                    "IS_SANDBOX": "1"}
            for k, v in env.items():  # 与生产同源的网关钥匙（只注入，不打印）
                if k.startswith(("ANTHROPIC_", "CLAUDE_")):
                    cenv[k] = v
            rc, out, err = _run([cl, "-p", "Reply with exactly one word: ok",
                                 "--output-format", "text",
                                 "--dangerously-skip-permissions"],
                                cwd=tmp, env=cenv, timeout=120)
            results.append(("claude", rc == 0 and bool(out.strip()),
                            (err or out).strip()[:120]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not results:
        return mk("engine_smoke", "dep", "warn", "PATH 里找不到 opencode/claude")
    bad = [f"{n} 失败:{msg}" for n, okk, msg in results if not okk]
    good = " ".join(f"{n}✅" for n, okk, _ in results if okk)
    return mk("engine_smoke", "dep", "crit" if bad else "ok",
              (good + (" · " + "; ".join(bad) if bad else ""))[:300])


def _ver(cmd: list) -> str:
    rc, out, err = _run(cmd, timeout=10)
    s = (out or err).strip()
    return s.splitlines()[0][:48] if s else "?"


def check_versions() -> dict:
    fp = {"opencode": _ver(["opencode", "--version"]),
          "claude": _ver(["claude", "--version"]),
          "node": _ver(["node", "--version"]),
          "python": _ver([sys.executable, "-V"])}
    dws_v = _ver(["dws", "--version"])
    if dws_v == "?":
        dws_v = _ver(["dws", "version"])
    fp["dws"] = dws_v
    c = mk("version_drift", "dep", "ok", "")
    c["_side"] = {"versions": fp}
    return c


def check_gateways() -> dict:
    """模型网关可达性探测：GET /models（零推理 token，不占每日 2 次调用预算）。

    区分「钥匙坏(401/403)」与「网络断」；apiKey 若是 {env:...} 占位则退化为
    无鉴权连通性探测（401 视为可达）。绝不回显 key。
    """
    try:
        with open(OC_CONFIG, encoding="utf-8") as f:
            cfg = json.loads(re.sub(r"^\s*//.*$", "", f.read(), flags=re.M))
        providers = cfg.get("provider") or {}
    except Exception as e:
        return mk("model_gateway", "dep", "info",
                  f"opencode.jsonc 解析失败，跳过网关探测：{type(e).__name__}"[:150])
    tested, bad = 0, []
    for name, p in list(providers.items())[:4]:
        opt = p.get("options") or {}
        base, key = str(opt.get("baseURL") or ""), str(opt.get("apiKey") or "")
        if not base.startswith("http"):
            continue
        usable = key and not key.startswith(("{env:", "$"))
        tested += 1
        headers = {"Authorization": f"Bearer {key}"} if usable else {}
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8):
                pass
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and usable:
                bad.append(f"{name}:key 失效(HTTP{e.code})")
            elif e.code >= 500:
                bad.append(f"{name}:网关 5xx({e.code})")
            # 其余状态码（404/405/无鉴权 401…）说明网络与进程都活着
        except Exception:
            bad.append(f"{name}:网络不可达")
    if not tested:
        return mk("model_gateway", "dep", "info", "配置里无可测 provider")
    return mk("model_gateway", "dep", "crit" if bad else "ok",
              f"{tested} 网关" + (f"异常：{'; '.join(bad)}" if bad else "全部可达"))


def check_dws_login() -> dict:
    rc, out, err = _run(["dws", "contact", "user", "me", "--format", "json"],
                        env={**os.environ, "HOME": "/root"}, timeout=30)
    if rc == 0 and ("corpId" in out or "userId" in out):
        return mk("dws_login", "dep", "ok", "dws 登录态可用")
    return mk("dws_login", "dep", "crit",
              f"dws 登录态失效——所有钉钉预警通道都依赖它：{(err or out)[:120]}")


def check_git_remote() -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}
    rc, out, _ = _run(["git", "ls-remote", REPO_URL, "refs/heads/main"],
                      env=env, timeout=30)
    if rc != 0:
        return mk("git_remote", "dep", "warn",
                  "GitHub SSH 拉取失败——今晚 01:00 部署将不可用")
    remote = (out.split()[0] if out.split() else "")[:12]
    cur = ""
    try:
        cur = os.path.basename(os.path.dirname(os.path.realpath(CURRENT_LINK)))[:12]
    except OSError:
        pass
    rel = "同步" if remote == cur else f"远端领先（今晚自动部署 {cur}→{remote}）"
    return mk("git_remote", "dep", "ok", f"SSH 可达 · {rel}")


def check_install_drift() -> dict:
    rel_app = os.path.join(os.path.dirname(os.path.realpath(CURRENT_LINK)),
                           "relay-avatar", "app")
    inst_app = os.path.join(AVATAR_INSTALL, "app")
    if not (os.path.isdir(rel_app) and os.path.isdir(inst_app)):
        return mk("install_drift", "dep", "info", "无对照目录（avatar 未装或 release 缺失）")
    rc, out, _ = _run(["diff", "-rq", "--exclude=__pycache__", inst_app, rel_app],
                      timeout=30)
    if rc == 0:
        return mk("install_drift", "dep", "ok", "安装位与 release 代码一致")
    first = (out or "").splitlines()[:3]
    return mk("install_drift", "dep", "warn",
              f"安装位漂移：{' | '.join(x[:80] for x in first)}")


def check_backup(hist: list[dict]) -> dict:
    timer_rc, _, _ = _run(["systemctl", "is-active", "--quiet", "relay-backup.timer"])
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "relay-backup-*.tar.gz")),
                   key=os.path.getmtime) if os.path.isdir(BACKUP_DIR) else []
    latest = files[-1] if files else None
    age_h = (time.time() - os.path.getmtime(latest)) / 3600 if latest else None
    size = os.path.getsize(latest) if latest else None
    prev_sizes = [r["m"]["backup_mb"] * 1e6 for r in hist[-8:]
                  if (r.get("m") or {}).get("backup_mb")]
    return eval_backup(age_h, size, prev_sizes, len(files),
                       timer_rc == 0, os.path.isdir(BACKUP_DIR or ""))


def check_cert() -> dict:
    try:
        p1 = subprocess.run(["openssl", "s_client", "-connect", "127.0.0.1:8788"],
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10)
        p2 = subprocess.run(["openssl", "x509", "-noout", "-enddate"],
                            input=p1.stdout, capture_output=True, text=True, timeout=10)
        m = re.search(r"notAfter=(.+)", p2.stdout or "")
        if not m:
            return mk("cert_expiry", "data", "warn", "解析 8788 证书到期失败")
        exp = time.mktime(time.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z"))
        return eval_cert((exp - time.time()) / 86400)
    except FileNotFoundError:
        return mk("cert_expiry", "data", "info", "openssl 不可用，跳过证书探测")
    except Exception as e:
        return mk("cert_expiry", "data", "warn", f"证书探测异常：{e}"[:120])


def check_perms() -> dict:
    bad = []
    for p in (RELAY_ENV, os.path.join(WORKSPACE, "shared/secrets/relay-avatar.env")):
        if os.path.isfile(p) and os.stat(p).st_mode & 0o077:
            bad.append(f"{os.path.basename(p)}:{oct(os.stat(p).st_mode & 0o777)}")
    seed = os.path.join(WORKSPACE, "shared/secrets/dws-cli-seed/dws-cli")
    if os.path.isdir(seed) and os.stat(seed).st_mode & 0o077:
        bad.append("dws-seed 目录非 700")
    return mk("secrets_perms", "data", "warn" if bad else "ok",
              ("凭据文件权限放宽：" + "; ".join(bad)) if bad else "凭据权限 600/700 合规")


def _tail(path: str, nbytes: int) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, os.path.getsize(path) - nbytes))
            return f.read()
    except OSError:
        return None


def check_plan_fire_fail() -> dict:
    if not os.path.isfile(PLAN_LOG):
        return mk("plan_fire_fail", "biz", "info", "无 relay-plan.log（无计划或从未触发）")
    tail = _tail(PLAN_LOG, 65536)
    if tail is None:
        return mk("plan_fire_fail", "biz", "warn", "relay-plan.log 读取失败")
    now = time.time()
    hits = []
    for ln in tail.splitlines():
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] FAIL fire plan=(\d+)", ln)
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if now - ts <= 86400:
                hits.append(m.group(2))
    return mk("plan_fire_fail", "biz", "crit" if hits else "ok",
              (f"24h 计划触发失败 {len(hits)} 次（plan {sorted(set(hits))[:5]}）"
               if hits else "24h 计划触发无失败"))


def check_plan_delivery() -> dict:
    audit = os.path.join(AVATAR_DIR, "audit", "audit.log")
    if not os.path.isfile(audit):
        return mk("plan_delivery", "biz", "info", "无 avatar 审计日志（分身未启用？）")
    tail = _tail(audit, 262144)
    if tail is None:
        return mk("plan_delivery", "biz", "warn", "avatar 审计日志读取失败")
    now = time.time()
    gave_up, health = [], []
    for ln in tail.splitlines():
        if '"plan_delivery_gave_up"' not in ln and '"plan_health_alert"' not in ln:
            continue
        try:
            j = json.loads(ln)
            ts = time.mktime(time.strptime(j.get("ts", ""), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if now - ts > 86400:
            continue
        item = f"{j.get('actor', '')}:{str(j.get('detail') or '')[:60]}"
        (gave_up if j.get("action") == "plan_delivery_gave_up" else health).append(item)
    if gave_up:
        return mk("plan_delivery", "biz", "crit",
                  f"24h 投递放弃 {len(gave_up)}：{'; '.join(gave_up[:2])}")
    if health:
        return mk("plan_delivery", "biz", "warn",
                  f"24h 投递体检告警 {len(health)}：{'; '.join(health[:2])}")
    return mk("plan_delivery", "biz", "ok", "24h 计划投递无异常")


def check_provision_flow(state: dict) -> dict:
    tail = _tail(BOOT_LOG, 131072) if os.path.isfile(BOOT_LOG) else None
    ms = re.findall(r"清单结束: PASS=(\d+) WARN=(\d+) FAIL=(\d+)", tail or "")
    if not ms:
        return mk("provision_recap", "meta", "info", "relay-boot.log 中无 provision 清单结果")
    p, w, fl = (int(x) for x in ms[-1])
    baseline = state.get("provision_warn_baseline")
    r = eval_provision(p, w, fl, baseline)
    # 基线取历史最低 WARN 数：一次性噪音不会把基线抬高后掩盖真漂移
    r["_side"] = {"provision_warn_baseline":
                  w if (baseline is None or w < baseline) else baseline}
    return r


# ══════════════════ 状态 / 历史 / 主流程 ══════════════════

def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_text_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [x for x in f.read().splitlines() if x.strip()]
    except OSError:
        return []


def load_history() -> list[dict]:
    out = []
    for ln in load_text_lines(os.path.join(DOCTOR_DIR, "history.ndjson")):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def append_history(rec: dict) -> None:
    """history 每天一行：同日重跑替换当日记录，外推不被重复样本扭曲。"""
    path = os.path.join(DOCTOR_DIR, "history.ndjson")
    os.makedirs(DOCTOR_DIR, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.localtime(rec["ts"]))
    cutoff = time.time() - HISTORY_KEEP_DAYS * 86400
    final = []
    for x in load_text_lines(path) + [json.dumps(rec, ensure_ascii=False)]:
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if r.get("ts", 0) < cutoff:
            continue
        if time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0))) == day:
            continue  # 旧的同日行让位给新行（rec 本身在最后）
        final.append(x)
    final.append(json.dumps(rec, ensure_ascii=False))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(final) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def cleanup_reports() -> None:
    for p in glob.glob(os.path.join(DOCTOR_DIR, "reports", "*.md")):
        try:
            d = datetime.strptime(os.path.basename(p)[:-3], "%Y-%m-%d")
            if (time.time() - d.timestamp()) / 86400 > REPORT_KEEP_DAYS:
                os.remove(p)
        except ValueError:
            continue


def run_all(only: set, skip_smoke: bool) -> tuple[list[dict], dict, dict]:
    """跑全部检查，返回 (checks, 待写回 state 的 side, ctx 本次采样数据)。"""
    env = read_env_file(RELAY_ENV)
    state = load_json(os.path.join(DOCTOR_DIR, "state.json"), {})
    hist = load_history()
    prev = hist[-1] if hist else {}
    now = time.time()
    usages = collect_disk()  # 本地 statfs，便宜；disk_space/forecast/history 共用一次采样
    checks: list[dict] = []
    sides: dict = {}

    def add(ids: tuple, fn):
        if only and not (set(ids) & only):
            return
        try:
            r = fn()
        except Exception as e:  # 采集器异常降级 warn，绝不拖垮整轮
            r = mk(ids[0], "meta", "warn", f"采集器异常：{type(e).__name__}: {e}"[:180])
        for it in (r if isinstance(r, list) else [r]):
            if "_side" in it:
                sides.update(it.pop("_side"))
            checks.append(it)

    add(RELAY_GROUP, lambda: check_relay(env))
    add(("pg_health",), lambda: check_pg(env))
    add(("disk_space",), lambda: eval_disk(usages))
    add(("disk_forecast",), lambda: eval_forecast(_disk_series(hist, usages, now)))
    add(("dir_growth",), lambda: check_dir_growth((prev.get("m") or {}).get("dirs", {})))
    add(("service_recap",), lambda: check_services(state))
    add(("timer_stuck",), check_timers)
    add(("journal_errors",), check_journal)
    add(("engine_smoke",), lambda: (mk("engine_smoke", "dep", "info", "--skip-smoke")
                                    if skip_smoke else check_engine_smoke(env)))
    add(("version_drift",), lambda: _version_flow(state))
    add(("model_gateway",), check_gateways)
    add(("dws_login",), check_dws_login)
    add(("git_remote",), check_git_remote)
    add(("install_drift",), check_install_drift)
    add(("plan_fire_fail",), check_plan_fire_fail)
    add(("plan_delivery",), check_plan_delivery)
    add(("backup_fresh",), lambda: check_backup(hist))
    add(("cert_expiry",), check_cert)
    add(("secrets_perms",), check_perms)
    add(("provision_recap",), lambda: check_provision_flow(state))
    add(("doctor_self",), lambda: eval_self(state.get("last_run"), now))

    seen, uniq = set(), []
    for c in checks:  # 幂等去重（--only 组合调用时同 id 可能重复产出）
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        uniq.append(c)
    return uniq, sides, {"usages": usages}


def _version_flow(state: dict) -> dict:
    c = check_versions()
    new = c.pop("_side", {}).get("versions", {})
    r = eval_versions(state.get("versions", {}), new)
    r["_side"] = {"versions": new}
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="relay-doctor 检查套件")
    ap.add_argument("--json", action="store_true", help="逐检查 NDJSON 输出（调试）")
    ap.add_argument("--dry", action="store_true", help="不落盘不写状态（手动试跑）")
    ap.add_argument("--only", default="", help="逗号分隔的检查 id 过滤")
    ap.add_argument("--skip-smoke", action="store_true", help="跳过引擎真跑探测")
    ap.add_argument("--ack", default="", help="静默某检查 id（配 --days/--reason）")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    if args.ack:
        sp = os.path.join(DOCTOR_DIR, "state.json")
        st = load_json(sp, {})
        st.setdefault("acks", {})[args.ack] = {
            "until": time.time() + args.days * 86400, "reason": args.reason}
        save_json_atomic(sp, st)
        print(json.dumps({"acked": args.ack, "days": args.days}, ensure_ascii=False))
        return 0

    t0 = time.time()
    only = set(x.strip() for x in args.only.split(",") if x.strip())
    checks, sides, ctx = run_all(only, args.skip_smoke)
    now = time.time()
    dur = now - t0
    state = load_json(os.path.join(DOCTOR_DIR, "state.json"), {})
    checks, acked = apply_acks(checks, state.get("acks", {}), now)
    ov = overall(checks)
    report_path = os.path.join(DOCTOR_DIR, "reports",
                               time.strftime("%Y-%m-%d.md", time.localtime(now)))
    digest = render_digest(checks, now, dur, report_path)

    if args.json:
        for c in checks:
            print(json.dumps(c, ensure_ascii=False))
        return 0

    if not args.dry:
        os.makedirs(os.path.join(DOCTOR_DIR, "reports"), exist_ok=True)
        os.chmod(DOCTOR_DIR, 0o700)
        os.chmod(os.path.join(DOCTOR_DIR, "reports"), 0o700)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(render_report(checks, now, dur))
        os.chmod(report_path, 0o600)
        state.update(sides)
        state["last_run"] = now
        save_json_atomic(os.path.join(DOCTOR_DIR, "state.json"), state)
        m = {"disk": {u["mount"]: round(100 - u["free_pct"], 1) for u in ctx["usages"]},
             "rss": sides.get("rss", {}),
             "dirs": next((c.get("metrics", {}).get("dirs") for c in checks
                           if c["id"] == "dir_growth"), {}),
             "backup_mb": next((c.get("metrics", {}).get("size_mb") for c in checks
                                if c["id"] == "backup_fresh"), None),
             "failed": next((c.get("metrics", {}).get("failed") for c in checks
                             if c["id"] == "task_failed_24h"), None)}
        append_history({"ts": now, "overall": ov,
                        "checks": {c["id"]: c["status"] for c in checks}, "m": m})
        cleanup_reports()

    print(json.dumps({
        "overall": ov,
        "n_crit": sum(1 for c in checks if c["status"] == "crit"),
        "n_warn": sum(1 for c in checks if c["status"] == "warn"),
        "n_checks": len(checks),
        "dur_s": round(dur, 1),
        "acked": acked,
        "digest": digest,
        "report_path": report_path,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
