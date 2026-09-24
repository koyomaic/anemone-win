#!/usr/bin/env bash
# relay 计划触发器：每分钟由 relay-plan.timer 拉起。
# 扫本机 relay 的 due 计划 → 逐个 POST /plans/{id}/fire（服务端原子认领+提交任务）。
# 纯 HTTP 打本机 relay admin 接口：不碰 DB 驱动、不碰 systemd 单元、不写 /etc。
# 失败只记日志并非零退出；timer 的 Persistent=true 会在下次补跑。
set -uo pipefail

LOG=/var/log/relay-plan.log
LOCK=/run/relay-plan.lock
ENVF="${RELAY_ENV:-/mnt/vol-eltaah12/workspace/relay.env}"
BASE="${RELAY_BASE:-http://127.0.0.1:8787}"
PY="${PYTHON:-/usr/bin/python3}"

# 防重入（timer 对 oneshot 默认不并发，flock 再加一道保险）
exec 9>"$LOCK" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then flock -n 9 || exit 0; fi

ts()  { date '+%F %T'; }
log() { echo "[$(ts)] $*" >>"$LOG"; }

# 从 relay.env 读 admin token（root 可读；本单元不在 relay 沙箱内，不受 InaccessiblePaths 限制）
TOK="$("$PY" - "$ENVF" <<'PY'
import re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        for ln in f:
            m = re.match(r"\s*RELAY_ADMIN_TOKEN\s*=\s*(.+?)\s*$", ln)
            if m:
                print(m.group(1).strip().strip('"').strip("'"))
                break
except OSError:
    pass
PY
)"
if [ -z "$TOK" ]; then log "FAIL no RELAY_ADMIN_TOKEN in $ENVF"; exit 1; fi

due="$(curl -sf -m 10 -H "Authorization: Bearer $TOK" "$BASE/plans/due")" \
  || { log "FAIL fetch $BASE/plans/due"; exit 1; }

ids="$(printf '%s' "$due" | "$PY" -c 'import sys,json
try:
    print(" ".join(str(p["id"]) for p in json.load(sys.stdin).get("plans", [])))
except Exception:
    print("")')"

[ -n "$ids" ] || exit 0
for id in $ids; do
  res="$(curl -sf -m 60 -X POST -H "Authorization: Bearer $TOK" "$BASE/plans/$id/fire")" \
    || { log "FAIL fire plan=$id"; continue; }
  log "fired plan=$id $res"
done
exit 0
