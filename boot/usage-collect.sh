#!/usr/bin/env bash
# 鲁大师用户日度使用分析 · 宿主机侧预采集（两段式的第一段）
#
# 为什么必须在宿主机跑：本地任务沙箱的 local_sandbox_hide 整体隐藏 relay.env
# （policy.toml 明写「凭据在沙箱内不可见，不是可见但禁读」，是出网不限后的剩余防线）。
# 沙箱内的计划任务读不到 RELAY_DB → 采集器必然降级 logs_only → 日报只剩入站消息数。
# 故：宿主机带凭据采集 facts → 沙箱内只渲染（render_report.py 纯标准库，不需 psycopg2）。
#
# 数据源：RELAY_DB（PostgreSQL tasks/plans）+ avatar 的 audit/events/rounds
# 产物：$WORKSPACE/shared/tmp/usage/facts-<报告日>.json（沙箱可读：不在 hide 列表）
# 用法：usage-collect.sh [--date YYYY-MM-DD] [--dry-run]
#       不传 --date 默认昨天；REPORT_DATE 环境变量同义
# 日志：/var/log/ludashi-usage-collect.log（失败钉钉 DM 预警王洪彬，同 daily-report.sh 惯例）
set -uo pipefail
export HOME=/root LC_ALL=C.UTF-8
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy

LOG="${USAGE_LOG:-/var/log/ludashi-usage-collect.log}"
ALERT_USER="${USAGE_ALERT_USER:-王洪彬}"
WORKSPACE="${RELAY_WORKSPACE:-/mnt/vol-eltaah12/workspace}"
VENV="${RELAY_VENV:-/mnt/vol-eltaah12/finaagent/.venv}"
SKILL="${USAGE_SKILL_DIR:-$WORKSPACE/shared/skills/ludashi-usage-daily-report}"
OUTDIR="${USAGE_OUT_DIR:-$WORKSPACE/shared/tmp/usage}"
KEEP_DAYS="${USAGE_KEEP_DAYS:-30}"
DRY=""

log(){ echo "[$(date '+%F %T')] $*" >>"$LOG" 2>/dev/null || echo "[$(date '+%F %T')] $*"; }
alert(){
  [ -n "$DRY" ] && return 0
  timeout 60 dws chat +dm --to "$ALERT_USER" --yes --content "**鲁大师使用日报·预采集失败**
- 时间：$(date '+%F %T')
- 主机：$(hostname)
- 原因：$1
- 日志：$LOG

沙箱内的渲染任务会因缺 facts 而降级，当日日报不可用。" >/dev/null 2>&1 || true
}

while [ $# -gt 0 ]; do
  case "$1" in
    --date)    REPORT_DATE="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *)         log "FAIL 未知参数 $1"; exit 2 ;;
  esac
done

PY="$VENV/bin/python"
COLLECT="$SKILL/scripts/usage_collect.py"
DATE="${REPORT_DATE:-$(date -d 'yesterday' +%F)}"

# ---- 前置检查：缺任一项都硬失败并预警，不静默产出残缺 facts ----
if [ ! -x "$PY" ]; then log "FAIL venv python 不存在: $PY"; alert "venv python 不存在: $PY"; exit 1; fi
if [ ! -f "$COLLECT" ]; then
  log "FAIL 采集器不存在: $COLLECT（技能未同步？跑 provision 21b）"
  alert "采集器不存在: $COLLECT"; exit 1
fi
if [ ! -f "$WORKSPACE/relay.env" ]; then
  log "FAIL relay.env 不存在: $WORKSPACE/relay.env"; alert "relay.env 不存在"; exit 1
fi

mkdir -p "$OUTDIR" 2>/dev/null
OUT="$OUTDIR/facts-$DATE.json"

# ---- 采集 ----
# 采集器自己会 load relay.env 取 RELAY_DB；这里不导出、不回显任何凭据。
if err="$("$PY" "$COLLECT" --date "$DATE" --out "$OUT" 2>&1)"; then
  :
else
  rc=$?
  log "FAIL 采集退出码 $rc: $err"
  alert "采集退出码 $rc（详见日志）"
  exit "$rc"
fi

# ---- 校验产物：必须是真的 pg_authoritative，降级件等于没采到 ----
if [ ! -s "$OUT" ]; then
  log "FAIL 产物为空: $OUT"; alert "产物为空: $OUT"; exit 1
fi
SRC="$("$PY" -c "
import json,sys
try:
    f=json.load(open('$OUT',encoding='utf-8'))
    pg=f.get('pg') or {}
    t=pg.get('today') or {}
    print(f\"{f.get('source','?')}|{t.get('task_rows','?')}|{t.get('questions','?')}|{t.get('active_users','?')}\")
except Exception as e:
    print(f'parse_error|{type(e).__name__}||')
" 2>>"$LOG")"
IFS='|' read -r S_MODE S_ROWS S_Q S_USERS <<<"$SRC"

if [ "$S_MODE" != "pg_authoritative" ]; then
  # 降级件仍保留（渲染器会打 [降级] 横幅），但必须预警：这种报告不能交领导
  log "WARN 采集降级 source=$S_MODE（PG 不可达？）产物=$OUT"
  alert "采集降级为 $S_MODE：PG 不可达或 psycopg2 缺失，当日日报只有日志口径、不可用于经营判断"
fi

# ---- 清理旧 facts ----
find "$OUTDIR" -name 'facts-*.json' -mtime +"$KEEP_DAYS" -delete 2>/dev/null

log "OK 预采集完成 报告日=$DATE source=$S_MODE 任务行=$S_ROWS 提问=$S_Q 活跃用户=$S_USERS 产物=$OUT"
[ -n "$DRY" ] && echo "[dry-run] $DATE source=$S_MODE rows=$S_ROWS questions=$S_Q users=$S_USERS → $OUT"
exit 0
