#!/usr/bin/env bash
# relay-doctor: 机队每日体检编排（06:00 由 relay-doctor.timer 拉起；检查与判级全在 doctor_checks.py）。
# 手动用法: doctor.sh [--dry] [透传给 doctor_checks.py 的其余参数，如 --skip-smoke --only xxx]
#   --dry  跑全套但不落盘、不发 DM（试跑看 digest）
# 设计/检查目录/性能红线见 DOCTOR.md。日报本身即死人开关：无论全绿与否每天一条 DM。
set -uo pipefail

BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/var/log/relay-doctor.log
LOCK=/run/relay-doctor.lock
PY="${DOCTOR_PYTHON:-/mnt/vol-eltaah12/finaagent/.venv/bin/python}"
[ -x "$PY" ] || PY=/usr/bin/python3

# boot.env 为每机私有配置；set -a 使其中的 DOCTOR_*/ALERT_TO/DWS_SEED 自动导出给 python
if [ -f "$BOOT_DIR/boot.env" ]; then
  set -a; # shellcheck source=/dev/null
  source "$BOOT_DIR/boot.env"; set +a
fi

ts()  { date '+%F %T'; }
log() { echo "[$(ts)] $*" >>"$LOG"; }

# DM 发送（复用 boot.sh notify_failure 的 dws 种子登录态模式：临时 HOME 软链种子，
# token 轮换直接发生在种子上；无种子退回本机 root 登录态）。$1=markdown 正文
send_dm() {
  local who="${DOCTOR_ALERT_TO:-${ALERT_TO:-王洪彬}}" rc=1 work
  local seed="${DWS_SEED:-/mnt/vol-eltaah12/workspace/shared/secrets/dws-cli-seed/dws-cli}"
  local dws="${DWS_BIN:-/usr/bin/dws}"
  if [ -d "$seed" ]; then
    work="$(mktemp -d /tmp/relay-doctor-dm-XXXXXX)"
    mkdir -p "$work/.local/share"
    ln -s "$seed" "$work/.local/share/dws-cli"
    HOME="$work" "$dws" chat +dm --to "$who" --content "$1" --yes --format json >>"$LOG" 2>&1 && rc=0
    rm -rf "$work"
  else
    HOME=/root "$dws" chat +dm --to "$who" --content "$1" --yes --format json >>"$LOG" 2>&1 && rc=0
  fi
  [ "$rc" = 0 ] || log "FAIL send dm → $who"
  return $rc
}

# 防重入（timer 对 oneshot 默认不并发，flock 再加一道保险）
exec 9>"$LOCK" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then flock -n 9 || { log "skip: 已有体检在跑"; exit 0; }; fi

DRYARG=(); ARGS=()
for a in "$@"; do
  if [ "$a" = "--dry" ]; then DRYARG=(--dry); else ARGS+=("$a"); fi
done

# ── 跑检查套件（stdout 为单行 summary JSON；崩溃兜底发预警，绝不静默失联）──
if ! summary="$("$PY" "$BOOT_DIR/doctor_checks.py" ${DRYARG[@]+"${DRYARG[@]}"} ${ARGS[@]+"${ARGS[@]}"} 2>>"$LOG")"; then
  log "FAIL suite crashed"
  [ ${#DRYARG[@]} -gt 0 ] || send_dm "【relay体检·$(hostname)】体检套件本身崩溃（doctor_checks.py），请查 $LOG；$(ts)"
  exit 1
fi

digest="$(printf '%s' "$summary" | "$PY" -c 'import sys,json
try:
    print(json.load(sys.stdin).get("digest",""))
except Exception:
    print("")')"
stats="$(printf '%s' "$summary" | "$PY" -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d.get("overall","?"), d.get("n_crit",0), d.get("n_warn",0), d.get("n_checks",0), d.get("dur_s"), sep=" ")
except Exception:
    print("? 0 0 0 ?")')"
log "run $stats dry=${#DRYARG[@]}"

if [ ${#DRYARG[@]} -gt 0 ]; then
  printf '%s\n' "$digest"
  exit 0
fi
[ -n "$digest" ] || { log "FAIL no digest（summary 解析失败）"; exit 1; }

if send_dm "$digest"; then
  log "sent（${stats%% *}）"
fi
exit 0
