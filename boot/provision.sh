#!/usr/bin/env bash
# provision.sh — relay 全机队 安装/体检 顺序清单
# 流程：检测 → PASS → 下一项；检测 → 缺失 → 安装 → 验证 → PASS → 下一项
#
# 用法:
#   provision.sh [install|check]
#     install (默认)  幂等自愈：缺什么装什么并验证；可重复执行。
#                     两个入口：① 新服务器一键安装 ② boot.sh 每日守护轮次自动调用
#     check           只读体检：只报告不改动（硬项缺失=FAIL，配置漂移=WARN）
#
# 新服务器一键安装（root 执行）:
#   curl -fsSL https://raw.githubusercontent.com/koyomaic/business_advisor/main/boot/provision.sh \
#     -o /tmp/provision.sh && bash /tmp/provision.sh install
#
# 退出码: 0=全 PASS（允许 WARN）  1=有 FAIL  2=用法错误
#
# 约定:
#   - 绝不覆盖已存在的数据/密钥（relay.env、relay.db、opencode.jsonc、claude-proxy config 只在缺失时创建）
#   - 密钥不进 git：relay.env 首装随机生成；opencode.jsonc 落模板后需人工填 apiKey
#   - 旧机迁移：把旧机 /mnt/vol-eltaah12/backup/relay-backup-*.tar.gz 放到新机同路径，
#     install 模式会自动用最新一份补齐 workspace 缺失文件（--keep-old-files，不覆盖已有）
#   - 本机私有附加检查（内网 MCP 连通等）写 /opt/team/relay-boot/provision-local.sh（可选，不进 git）
set -uo pipefail

MODE="${1:-install}"
{ [ "$MODE" = install ] || [ "$MODE" = check ]; } || { echo "usage: provision.sh [install|check]" >&2; exit 2; }

# ---------- 配置（本机 boot.env 优先；缺省 = 机队标准布局） ----------
BOOT_DIR=/opt/team/relay-boot
# shellcheck source=/dev/null
[ -f "$BOOT_DIR/boot.env" ] && source "$BOOT_DIR/boot.env"
REPO_URL="${REPO_URL:-git@github.com:koyomaic/business_advisor.git}"  # 机队默认 SSH（deploy key）；HTTPS+PAT 备用见 boot.env.example
BRANCH="${BRANCH:-main}"
RELEASE_ROOT="${RELEASE_ROOT:-/opt/team/relay-deploy/releases}"
CURRENT_LINK="${CURRENT_LINK:-/opt/team/relay-deploy/current}"
VENV="${VENV:-/mnt/vol-eltaah12/finaagent/.venv}"
UNIT="${UNIT:-relay}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8787/health}"
WORKSPACE="${RELAY_WORKSPACE:-/mnt/vol-eltaah12/workspace}"
BACKUP_DIR="${BACKUP_DIR:-/mnt/vol-eltaah12/backup}"
ALERT_TO="${ALERT_TO:-王洪彬}"
OPENCODE_VER="${OPENCODE_VER:-1.18.29}"
DWS_VER="${DWS_VER:-1.0.61}"
PIP_MIRROR="${PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"
DWS_SEED="${DWS_SEED:-$WORKSPACE/shared/secrets/dws-cli-seed/dws-cli}"
UNIT_DIR=/etc/systemd/system
PROXY_DIR=/root/.claude-proxy
OC_CONFIG=/root/.config/opencode/opencode.jsonc
LOCAL_HOOK="$BOOT_DIR/provision-local.sh"

export GIT_TERMINAL_PROMPT=0 DEBIAN_FRONTEND=noninteractive

# ---------- 报告 ----------
N=0; C_PASS=0; C_WARN=0; C_FAIL=0; FAILED=""; LAST=""
step()  { N=$((N+1)); LAST="$1"; printf '[%02d] %-22s ' "$N" "$1"; }
ok()    { echo "PASS  ${*:-}"; C_PASS=$((C_PASS+1)); }
warn()  { echo "WARN  ${*:-}"; C_WARN=$((C_WARN+1)); }
bad()   { echo "FAIL  ${*:-}"; C_FAIL=$((C_FAIL+1)); FAILED="$FAILED [$N]$LAST"; }
miss()  { printf 'MISS → %s ... ' "$*"; }
heal()  { [ "$MODE" = install ]; }
summary_exit() {
  echo "=================================================="
  echo "清单结束: PASS=$C_PASS WARN=$C_WARN FAIL=$C_FAIL (mode=$MODE host=$(hostname) $(date '+%F %T'))"
  [ -n "$FAILED" ] && echo "FAIL 项:$FAILED"
  [ "$C_FAIL" -eq 0 ] || exit 1
  exit 0
}
abort() { bad "$@"; echo "-- install 模式中止：前置项失败，后续检测不再继续"; summary_exit; }

pkg_install() { # 尽力而为的系统包安装（apt/dnf）
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq "$@" >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q "$@" >/dev/null 2>&1
  else
    return 1
  fi
}

wait_healthy() { # $1=秒数；双探：TLS 直听 https:8788 优先，回落 HEALTH_URL（明文/回环转发）
  local i u; for i in $(seq 1 "${1:-30}"); do
    for u in "https://127.0.0.1:8788/health" "$HEALTH_URL"; do
      curl -sfk -m 3 "$u" 2>/dev/null | grep -q '"ok"[[:space:]]*:[[:space:]]*true' && return 0
    done
    sleep 1
  done; return 1; }

health_version() {
  local u v; for u in "https://127.0.0.1:8788/health" "$HEALTH_URL"; do
    v="$(curl -sfk -m 3 "$u" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("version",""))' 2>/dev/null)" || true
    [ -n "$v" ] && { echo "$v"; return 0; }
  done; echo ""
}

restore_from_backup() { # 用最新备份包补齐 workspace 缺失文件；成功时 echo 包名
  local pkg; pkg="$(ls -1t "$BACKUP_DIR"/relay-backup-*.tar.gz 2>/dev/null | head -1)"
  [ -n "$pkg" ] || return 1
  mkdir -p "$WORKSPACE"
  tar -xzf "$pkg" -C "$WORKSPACE" --keep-old-files >/dev/null 2>&1 || true
  echo "$pkg"
}

render_unit() { sed -e "s|__CURRENT_LINK__|$CURRENT_LINK|g" -e "s|__VENV__|$VENV|g" -e "s|__WORKSPACE__|$WORKSPACE|g" "$1"; }

# ---------- 资源树（单元模板/requirements/代理脚本的来源：release、现役树或临时 clone） ----------
RES_ROOT=""; TMP_RES=""
resolve_res() {
  local sd; sd="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$sd/../relay-service/requirements.txt" ]; then RES_ROOT="$(cd "$sd/.." && pwd)"; return 0; fi
  if [ -L "$CURRENT_LINK" ]; then
    local rel; rel="$(dirname "$(readlink -f "$CURRENT_LINK")")"
    [ -f "$rel/boot/boot.sh" ] && { RES_ROOT="$rel"; return 0; }
  elif [ -f "$CURRENT_LINK/requirements.txt" ] && [ -f "$(dirname "$CURRENT_LINK")/boot/boot.sh" ]; then
    RES_ROOT="$(dirname "$CURRENT_LINK")"; return 0
  fi
  return 1
}
trap '[ -n "$TMP_RES" ] && rm -rf "$TMP_RES"' EXIT

echo "== relay provision 清单 v1 (mode=$MODE host=$(hostname) $(date '+%F %T')) =="

# ---------- 01 root + systemd ----------
step "root+systemd"
if [ "$(id -u)" != 0 ]; then abort "需 root 运行"
elif [ ! -d /run/systemd/system ]; then abort "需要 systemd 环境"
else ok; fi

# ---------- 02 目录布局 ----------
step "目录布局"
DIRS="/mnt/vol-eltaah12/finaagent $WORKSPACE $BACKUP_DIR $RELEASE_ROOT $BOOT_DIR $PROXY_DIR /root/.config/opencode /opt/team/skills"
heal && mkdir -p $DIRS
MISSING=""
for d in $DIRS; do [ -d "$d" ] || MISSING="$MISSING $d"; done
if [ -z "$MISSING" ]; then ok
elif heal; then abort "无法创建:$MISSING"
else bad "缺失:$MISSING"; fi

# ---------- 03-09 基础命令与运行时（检测→缺失→安装→验证） ----------
chk_git()      { command -v git >/dev/null 2>&1; }
ins_git()      { pkg_install git; }
chk_tools()    { command -v curl >/dev/null && command -v tar >/dev/null && command -v openssl >/dev/null; }
ins_tools()    { pkg_install curl tar openssl; }
chk_sqlite3()  { command -v sqlite3 >/dev/null 2>&1; }
ins_sqlite3()  { pkg_install sqlite3; }
chk_python3()  { command -v python3 >/dev/null && python3 -c 'import sys,venv,ensurepip; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; }
ins_python3()  { pkg_install python3 python3-venv; }
chk_node()     { command -v node >/dev/null && command -v npm >/dev/null && node -e 'process.exit(parseInt(process.versions.node)>=20?0:1)' 2>/dev/null; }
ins_node()     {
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh && bash /tmp/nodesource_setup.sh >/dev/null 2>&1 && apt-get install -y -qq nodejs >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    dnf module install -y nodejs:22 >/dev/null 2>&1 || dnf install -y -q nodejs npm >/dev/null 2>&1
  else return 1; fi; }
chk_opencode() { # 版本 >= 要求即通过（sort -V 取最小者等于要求版本 ⇒ 实际 ≥ 要求），避免新版被降级重装
  command -v opencode >/dev/null || return 1
  local v; v="$(opencode --version 2>/dev/null | head -1)"
  [ -n "$v" ] && [ "$(printf '%s\n%s\n' "$OPENCODE_VER" "$v" | sort -V | head -1)" = "$OPENCODE_VER" ]
}
ins_opencode() { # npm 偶发跳过 postinstall（bin 占位符报 "postinstall script was not run"），装完验证、失败则手动补跑修复
  npm install -g --no-fund --no-audit "opencode-ai@$OPENCODE_VER" >/dev/null 2>&1
  if ! opencode --version >/dev/null 2>&1; then
    (cd "$(npm root -g)/opencode-ai" && node postinstall.mjs) >/dev/null 2>&1 || true
  fi
}
chk_dws()      { command -v dws >/dev/null && npm ls -g dingtalk-workspace-cli 2>/dev/null | grep -q "dingtalk-workspace-cli@$DWS_VER"; }
ins_dws()      { npm install -g --no-fund --no-audit "dingtalk-workspace-cli@$DWS_VER" >/dev/null 2>&1; }

try() { # try <项目名> <检测函数> <安装函数>
  step "$1"
  if "$2"; then ok; return 0; fi
  if heal; then
    miss "$1"
    if "$3" && "$2"; then ok "已安装"; return 0; else abort "安装失败（手动排查后重跑）"; fi
  else bad "缺失"; return 1; fi
}
try "git"                 chk_git      ins_git
try "curl/tar/openssl"    chk_tools    ins_tools
try "sqlite3"             chk_sqlite3  ins_sqlite3
try "python3>=3.10+venv"  chk_python3  ins_python3
try "node>=20+npm"        chk_node     ins_node
try "opencode>=$OPENCODE_VER" chk_opencode ins_opencode
try "dws@$DWS_VER"        chk_dws      ins_dws

# ---------- 10 资源树 ----------
step "资源树(git)"
if resolve_res; then ok "$RES_ROOT"
elif heal; then
  miss "本地无资源树，临时 clone"
  TMP_RES="$(mktemp -d /tmp/provision-res-XXXXXX)"
  if timeout 300 git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_RES/repo" && [ -f "$TMP_RES/repo/boot/boot.sh" ]; then
    RES_ROOT="$TMP_RES/repo"; ok "$RES_ROOT (临时)"
  else abort "git clone 失败（github 不可达？）"; fi
else bad "无 release/现役树，check 模式不 clone"; fi

# ---------- 11 venv + 依赖 ----------
step "venv+依赖"
chk_venv() { [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import fastapi,uvicorn,httpx,pytest,psycopg2' 2>/dev/null; }
if chk_venv; then ok "$VENV"
elif heal; then
  miss "venv 或依赖"
  if { [ -x "$VENV/bin/pip" ] || python3 -m venv "$VENV"; } \
     && "$VENV/bin/pip" install -q -r "$RES_ROOT/relay-service/requirements.txt" -i "$PIP_MIRROR" >/dev/null 2>&1 \
     && chk_venv; then ok "已安装"
  else abort "venv/依赖安装失败"; fi
else bad "缺失"; fi

# ---------- 12 boot.env ----------
step "boot.env"
if [ -f "$BOOT_DIR/boot.env" ]; then ok
elif heal; then
  miss "生成机队标准布局"
  cat > "$BOOT_DIR/boot.env" <<EOF
# provision.sh 生成于 $(date '+%F %T')：机队标准布局；ALERT_TO 等按需修改
# REPO_URL 默认 SSH（deploy key，公钥登记见 INSTALL.md）；HTTPS+PAT 备用：https://github.com/koyomaic/business_advisor.git
REPO_URL=$REPO_URL
BRANCH=$BRANCH
RELEASE_ROOT=$RELEASE_ROOT
CURRENT_LINK=$CURRENT_LINK
VENV=$VENV
UNIT=$UNIT
HEALTH_URL=$HEALTH_URL
KEEP=3
LOG=/opt/team/relay-deploy/boot.log
ALERT_TO=$ALERT_TO
EOF
  ok "已生成"
else bad "缺失"; fi

# ---------- 13 boot.sh ----------
step "boot.sh"
if [ -f "$BOOT_DIR/boot.sh" ] && cmp -s "$RES_ROOT/boot/boot.sh" "$BOOT_DIR/boot.sh"; then ok
elif heal; then
  miss "从资源树同步"
  cp "$RES_ROOT/boot/boot.sh" "$BOOT_DIR/boot.sh" && chmod 700 "$BOOT_DIR/boot.sh" && ok "已同步" || abort "boot.sh 同步失败"
elif [ -f "$BOOT_DIR/boot.sh" ]; then warn "与资源树有漂移（install 模式自动同步）"
else bad "缺失"; fi

# ---------- 13b TLS 布局迁移：旧 socat 占 8788 先让位（relay.service 改为 TLS 直听，真实客户端 IP 不再被代理遮蔽；8787 明文口随后收敛为仅回环） ----------
if systemctl is-active --quiet relay-tls-proxy 2>/dev/null \
   && grep -q "OPENSSL-LISTEN:8788" "$UNIT_DIR/relay-tls-proxy.service" 2>/dev/null; then
  step "TLS布局迁移"
  miss "停旧 socat(0.0.0.0:8788)，让位 relay.service TLS 直听"
  systemctl stop relay-tls-proxy && ok "已停（新回环转发单元在 24 节同步）" || warn "停止失败（单元同步时将重试）"
fi

# ---------- 14-16 systemd 单元（仓库真源，渲染→比对→同步） ----------
sync_unit() { # sync_unit <项目名> <模板路径> <目标名> <是否渲染> <变更后是否重启relay>
  step "$1"
  local want have dst="$UNIT_DIR/$3"
  if [ "$4" = render ]; then want="$(render_unit "$2")"; else want="$(cat "$2")"; fi
  have="$(cat "$dst" 2>/dev/null)"
  if [ "$want" = "$have" ]; then ok; return 0; fi
  if ! heal; then [ -n "$have" ] && warn "与仓库模板漂移（install 模式自动同步）" || bad "缺失"; return 0; fi
  miss "安装/更新 $3"
  [ -n "$have" ] && cp "$dst" "$dst.provision-bak"
  printf '%s\n' "$want" > "$dst" && systemctl daemon-reload || { abort "$3 写入失败"; }
  if [ "$5" = restart ] && [ -e "$CURRENT_LINK" ] && systemctl is-active --quiet "$UNIT"; then
    systemctl restart "$UNIT"
    if wait_healthy 30; then ok "已更新+重启健康"
    else
      [ -n "$have" ] && { cp "$dst.provision-bak" "$dst"; systemctl daemon-reload; systemctl restart "$UNIT"; }
      warn "更新后不健康，已回退旧单元"
    fi
  else ok "已安装"; fi
}
sync_unit "relay.service"        "$RES_ROOT/boot/relay.service.template"        relay.service        render restart
sync_unit "relay-backup.service" "$RES_ROOT/boot/relay-backup.service.template" relay-backup.service render no
sync_unit "relay-backup.timer"   "$RES_ROOT/boot/relay-backup.timer"            relay-backup.timer   copy   no
sync_unit "relay-boot.service"   "$RES_ROOT/boot/relay-boot.service"            relay-boot.service   copy   no
sync_unit "relay-boot.timer"     "$RES_ROOT/boot/relay-boot.timer"              relay-boot.timer     copy   no

# ---------- 17 relay.env（密钥，SOFT） ----------
step "relay.env(密钥)"
chk_relayenv() { [ -f "$WORKSPACE/relay.env" ] && grep -q '^RELAY_ADMIN_TOKEN=.' "$WORKSPACE/relay.env" && grep -q '^RELAY_DASHBOARD_PASSWORD=.' "$WORKSPACE/relay.env"; }
if chk_relayenv; then ok
else
  PKG=""
  heal && PKG="$(restore_from_backup || true)"
  if chk_relayenv; then ok "从备份恢复 ${PKG##*/}"
  elif heal; then
    ( umask 077; cat > "$WORKSPACE/relay.env" <<EOF
RELAY_ADMIN_TOKEN=$(openssl rand -hex 32)
RELAY_DASHBOARD_PASSWORD=$(openssl rand -hex 16)
EOF
    )
    warn "已生成新随机密钥；旧机迁移请用备份包覆盖后 systemctl restart $UNIT"
  else warn "缺失（install 模式可从备份恢复或随机生成）"; fi
fi

# ---------- 18 数据库（relay.env RELAY_DB=postgresql:// 时为 PG 模式，否则 SQLite） ----------
RELAY_DB_VAL="$(sed -n 's/^RELAY_DB=//p' "$WORKSPACE/relay.env" 2>/dev/null | tail -1 || true)"
PG_MODE=0
case "$RELAY_DB_VAL" in postgres*://*) PG_MODE=1 ;; esac
step "数据库"
if [ "$PG_MODE" = 1 ]; then
  PG_MASKED="$(printf '%s' "$RELAY_DB_VAL" | sed -E 's#://[^@]*@#://***@#')"
  if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" - "$RELAY_DB_VAL" <<'PYEOF'
import sys, psycopg2
conn = psycopg2.connect(sys.argv[1], connect_timeout=5)
cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); conn.close()
PYEOF
  then ok "PG 连通 $PG_MASKED"
  else bad "PG 不可达（$PG_MASKED）：relay 无法工作，检查内网/服务端"; fi
else
  if [ -f "$WORKSPACE/relay.db" ]; then ok "SQLite"
  else
    PKG=""
    heal && PKG="$(restore_from_backup || true)"
    if [ -f "$WORKSPACE/relay.db" ]; then ok "SQLite 从备份恢复 ${PKG##*/}"
    else warn "缺失：relay 首启会自建新库，成员 token 需从旧机迁移或重新发放"; fi
  fi
fi

# ---------- 18b pg_dump 客户端（仅 PG 模式且本机负责 DB 备份时必需；RELAY_BACKUP_SKIP_DB=1 时跳过） ----------
SKIP_DB_VAL="$(sed -n 's/^RELAY_BACKUP_SKIP_DB=//p' "$WORKSPACE/relay.env" 2>/dev/null | tail -1 || true)"
if [ "$PG_MODE" = 1 ] && [ "$SKIP_DB_VAL" != 1 ]; then
  step "pg_dump客户端"
  if command -v pg_dump >/dev/null 2>&1; then ok
  elif heal; then
    miss "postgresql-client"
    if pkg_install postgresql-client && command -v pg_dump >/dev/null 2>&1; then ok "已安装"
    else bad "安装失败（PG 模式备份不可用，需人工处理）"; fi
  else bad "缺失（PG 模式备份必需，install 模式自动安装）"; fi
fi

# ---------- 19 dws 种子（SOFT） ----------
step "dws登录态种子"
if [ -d "$DWS_SEED" ]; then ok
else warn "缺失：钉钉预警/dws 不可用；从旧机拷贝 $DWS_SEED 或本机 dws 登录"; fi

# ---------- 19b dws-refresh 小时级刷新（脚本+单元+定时器，SOFT） ----------
# 每小时整点（±60s 抖动）软链共享种子跑 auth status，刷新原地发生在种子上；
# 日志 /var/log/dws-refresh.log；登录态失效时本项 WARN，需人工设备码重授权
step "dws-refresh脚本"
if [ -f "$BOOT_DIR/dws-refresh.sh" ] && cmp -s "$RES_ROOT/boot/dws-refresh.sh" "$BOOT_DIR/dws-refresh.sh"; then ok
elif heal; then
  miss "从资源树同步"
  cp "$RES_ROOT/boot/dws-refresh.sh" "$BOOT_DIR/dws-refresh.sh" && chmod 700 "$BOOT_DIR/dws-refresh.sh" && ok "已同步" || bad "同步失败"
else bad "缺失"; fi
sync_unit "dws-refresh.service" "$RES_ROOT/boot/dws-refresh.service" dws-refresh.service copy no
sync_unit "dws-refresh.timer"   "$RES_ROOT/boot/dws-refresh.timer"   dws-refresh.timer   copy no
step "dws-refresh定时器"
if systemctl is-enabled --quiet dws-refresh.timer 2>/dev/null && systemctl is-active --quiet dws-refresh.timer; then ok
elif heal; then
  miss "启用"
  systemctl enable --now dws-refresh.timer 2>/dev/null
  systemctl is-active --quiet dws-refresh.timer && ok "已启用" || bad "无法激活"
else bad "未启用"; fi

# ---------- 20 opencode.jsonc（模型 provider+密钥，SOFT） ----------
step "opencode.jsonc"
if [ -f "$OC_CONFIG" ] && ! grep -q 'sk-REPLACE_ME' "$OC_CONFIG"; then ok
elif [ -f "$OC_CONFIG" ]; then warn "apiKey 仍是占位符，填写后 systemctl restart $UNIT"
elif heal; then
  miss "落模板"
  cp "$RES_ROOT/boot/opencode.jsonc.example" "$OC_CONFIG" && chmod 600 "$OC_CONFIG" \
    && warn "已落模板，填写两处 apiKey 后 systemctl restart $UNIT" || bad "模板拷贝失败"
else warn "缺失（install 模式落模板后需填 apiKey）"; fi

# ---------- 21 workspace AGENTS.md ----------
step "workspace AGENTS.md"
if [ -f "$WORKSPACE/AGENTS.md" ]; then ok
elif heal && [ -f "$RES_ROOT/boot/workspace-AGENTS.md" ] && cp "$RES_ROOT/boot/workspace-AGENTS.md" "$WORKSPACE/AGENTS.md"; then ok "已从仓库模板安装"
else warn "缺失且无模板"; fi

# ---------- 21a 系统级提示词 CEO_PROMPT.md（claude 引擎 --append-system-prompt-file 注入） ----------
# 与 AGENTS.md 同模式：缺失则从仓库模板自愈，已有不覆盖（本机可演进，仓库为兜底基线）。
step "workspace CEO_PROMPT.md"
if [ -f "$WORKSPACE/CEO_PROMPT.md" ]; then ok
elif heal && [ -f "$RES_ROOT/boot/CEO_PROMPT.md" ] && cp "$RES_ROOT/boot/CEO_PROMPT.md" "$WORKSPACE/CEO_PROMPT.md"; then ok "已从仓库模板安装"
else warn "缺失且无模板（claude 引擎将不注入系统提示词）"; fi

# ---------- 21b 基础技能（人为标记入库到仓库 skills/ 的机队共享技能；git 为真源） ----------
# 只有仓库 skills/ 里存在的技能才检查/同步（入库即标记）；各安装位下其余本地/实验技能不碰。
# 安装位：技能目录内 .dest 文件（单行绝对路径）指定目标父目录；无 .dest → 默认 $WORKSPACE/shared/skills。
#   例：钉钉套件 .dest=/root/.agents/skills，dws .dest=/opt/team/skills。
# 逐文件比对（.dest 为元数据，不参与比对/下发），漂移/缺失 → 本地旧版备份 *.bak-provision-* 后同步仓库版。
step "基础技能(git)"
SK_DST_DEFAULT="$WORKSPACE/shared/skills"
if ! ls -d "$RES_ROOT"/skills/*/ >/dev/null 2>&1; then
  warn "仓库无基础技能（skills/ 为空），跳过"
else
  SK_MISS=""; SK_DRIFT=""; SK_N=0
  sk_dest() { # $1=repo技能目录；输出该技能的目标父目录（读 .dest，缺省默认）
    if [ -f "$1/.dest" ]; then
      local d; d="$(head -1 "$1/.dest" | tr -d '[:space:]')"
      [ -n "$d" ] && { printf '%s\n' "$d"; return; }
    fi
    printf '%s\n' "$SK_DST_DEFAULT"
  }
  sk_drift_files() { # $1=repo技能目录 $2=目标技能目录；输出缺失/漂移的相对文件列表（排除 .dest）
    local f rel
    while IFS= read -r f; do
      rel="${f#"$1"}"
      [ "$rel" = ".dest" ] && continue
      cmp -s "$f" "$2/$rel" || printf '%s\n' "$rel"
    done < <(find "$1" -type f)
  }
  for src in "$RES_ROOT"/skills/*/; do
    name="$(basename "$src")"; SK_N=$((SK_N+1)); dst="$(sk_dest "$src")"
    if [ ! -d "$dst/$name" ]; then SK_MISS="$SK_MISS $name"; continue; fi
    d="$(sk_drift_files "$src" "$dst/$name")"
    [ -n "$d" ] && SK_DRIFT="$SK_DRIFT $name($(echo "$d" | tr '\n' ' '))"
  done
  if [ -z "$SK_MISS$SK_DRIFT" ]; then ok "$SK_N 个基础技能与仓库一致"
  elif ! heal; then warn "与仓库不一致:${SK_MISS:+ 缺失$SK_MISS}${SK_DRIFT:+ 漂移$SK_DRIFT}（install 模式自动同步）"
  else
    miss "同步${SK_MISS}${SK_DRIFT}"
    stamp="$(date +%Y%m%d-%H%M)"; n_sync=0
    for src in "$RES_ROOT"/skills/*/; do
      name="$(basename "$src")"; dst="$(sk_dest "$src")"; mkdir -p "$dst/$name"
      while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        [ -f "$dst/$name/$rel" ] && cp "$dst/$name/$rel" "$dst/$name/$rel.bak-provision-$stamp"
        mkdir -p "$(dirname "$dst/$name/$rel")"
        cp "$src/$rel" "$dst/$name/$rel" || abort "技能 $name/$rel 同步失败"
        n_sync=$((n_sync+1))
      done < <(sk_drift_files "$src" "$dst/$name")
    done
    ok "$SK_N 个技能，同步 $n_sync 文件（本地旧版备份 *.bak-provision-$stamp）"
  fi
fi

# ---------- 21c opencode 全局语境（~/.config/opencode/AGENTS.md，模板 git 为真源；所有 opencode 会话默认加载） ----------
step "opencode全局语境"
OC_GLOBAL="/root/.config/opencode/AGENTS.md"
OC_TMPL="$RES_ROOT/boot/opencode-global-AGENTS.md"
if [ ! -f "$OC_TMPL" ]; then warn "仓库无模板 boot/opencode-global-AGENTS.md，跳过"
elif [ -f "$OC_GLOBAL" ] && cmp -s "$OC_TMPL" "$OC_GLOBAL"; then ok
elif ! heal; then warn "缺失或与模板漂移（install 模式自动同步）"
else
  miss "同步"
  mkdir -p "$(dirname "$OC_GLOBAL")"
  [ -f "$OC_GLOBAL" ] && cp "$OC_GLOBAL" "$OC_GLOBAL.bak-provision-$(date +%Y%m%d-%H%M)"
  if cp "$OC_TMPL" "$OC_GLOBAL"; then ok "已安装/同步（旧版备份 *.bak-provision-*）"; else warn "同步失败"; fi
fi

# ---------- 21d team-agent-relay ca.pem（公开 CA 证书，不入 git；统一从 RELAY_CA_SRC 的 TLS 链提取） ----------
step "relay技能ca.pem"
RELAY_CA_SRC="${RELAY_CA_SRC:-127.0.0.1:8788}"
TAR_SRC="$RES_ROOT/skills/team-agent-relay"
if [ ! -d "$TAR_SRC" ]; then warn "仓库无 team-agent-relay，跳过"
else
  TAR_DST="$(head -1 "$TAR_SRC/.dest" 2>/dev/null | tr -d '[:space:]')"
  TAR_DST="${TAR_DST:-$WORKSPACE/shared/skills}/team-agent-relay"
  if [ ! -d "$TAR_DST" ]; then warn "安装位 $TAR_DST 不存在（基础技能未同步？），跳过"
  elif ! command -v openssl >/dev/null; then warn "缺 openssl，无法提取"
  else
    CA_PEM="$(echo | timeout 10 openssl s_client -connect "$RELAY_CA_SRC" -showcerts 2>/dev/null \
      | awk '/-----BEGIN CERTIFICATE-----/{n++} n==2{print} /-----END CERTIFICATE-----/&&n==2{exit}')"
    CA_SUBJ="$(printf '%s\n' "$CA_PEM" | openssl x509 -noout -subject 2>/dev/null)"
    CA_ISSR="$(printf '%s\n' "$CA_PEM" | openssl x509 -noout -issuer 2>/dev/null)"
    if [ -z "$CA_PEM" ] || [ -z "$CA_SUBJ" ] || [ "${CA_SUBJ#subject=}" != "${CA_ISSR#issuer=}" ]; then
      if [ -f "$TAR_DST/ca.pem" ]; then ok "提取源 $RELAY_CA_SRC 不可达，沿用现有 ca.pem"
      else warn "无法从 $RELAY_CA_SRC 提取自签 CA（不可达或链不完整）"; fi
    else
      NEW_FP="$(printf '%s\n' "$CA_PEM" | openssl x509 -noout -fingerprint -sha256 2>/dev/null)"
      OLD_FP="$([ -f "$TAR_DST/ca.pem" ] && openssl x509 -in "$TAR_DST/ca.pem" -noout -fingerprint -sha256 2>/dev/null)"
      if [ -n "$OLD_FP" ] && [ "$NEW_FP" = "$OLD_FP" ]; then ok "与 $RELAY_CA_SRC 一致"
      elif ! heal; then warn "缺失或与 $RELAY_CA_SRC 漂移（install 模式自动提取）"
      else
        miss "提取写入"
        [ -f "$TAR_DST/ca.pem" ] && cp "$TAR_DST/ca.pem" "$TAR_DST/ca.pem.bak-provision-$(date +%Y%m%d-%H%M)"
        if printf '%s\n' "$CA_PEM" > "$TAR_DST/ca.pem"; then
          chmod 644 "$TAR_DST/ca.pem"; ok "ca.pem 已写入（${CA_SUBJ#subject=}）"
        else warn "写入失败"; fi
      fi
    fi
  fi
fi

# ---------- 21e store 台账 CLI（业务存储统一索引，SOFT） ----------
# 三层架构：磁盘为文件真身、PostgreSQL（relay 同库 public schema、store_ 前缀）为权威索引、
# index/manifest.jsonl 为降级快照。约定见 workspace AGENTS.md「共享区约定」。
# 注意：建表（store init）属有副作用操作，install 模式**不自动执行**——只做 CLI 同步、
# 目录骨架（纯文件系统）与探活；首次由人工执行 init，避免在新机器上意外建表。
step "store台账CLI"
STORE_SRC="$RES_ROOT/boot/store/store"
STORE_DIR="${STORE_DIR:-/opt/team/store}"
STORE_BIN="$STORE_DIR/store"
STORE_ROOT="${STORE_ROOT:-$WORKSPACE/shared}"
if [ ! -f "$STORE_SRC" ]; then
  warn "资源树无 boot/store/store（旧版本？），跳过"
else
  if [ -f "$STORE_BIN" ] && cmp -s "$STORE_SRC" "$STORE_BIN"; then :
  elif ! heal; then warn "CLI 缺失或与仓库漂移（install 模式自动同步）"
  else
    miss "同步CLI"
    mkdir -p "$STORE_DIR"
    [ -f "$STORE_BIN" ] && cp "$STORE_BIN" "$STORE_BIN.bak-provision-$(date +%Y%m%d-%H%M)"
    cp "$STORE_SRC" "$STORE_BIN" && chmod 755 "$STORE_BIN"
  fi
  # PATH 入口（任务 agent 直接敲 store）
  if [ -e /usr/local/bin/store ] || [ -L /usr/local/bin/store ]; then :
  elif heal; then ln -sfn "$STORE_BIN" /usr/local/bin/store; fi
  # 目录骨架 + index 配置（纯文件系统，幂等，无 DB 副作用）
  [ -f "$STORE_BIN" ] && "$VENV/bin/python" "$STORE_BIN" --root "$STORE_ROOT" skeleton >/dev/null 2>&1
  # 台账探活：DB 可连且 5 张表在（缺表时 ping 退出码 1）
  if [ ! -f "$STORE_BIN" ]; then bad "store CLI 安装失败"
  elif "$VENV/bin/python" "$STORE_BIN" --root "$STORE_ROOT" ping >/dev/null 2>&1; then
    ok "CLI+骨架+台账就绪（root=$STORE_ROOT）"
  else
    warn "台账未初始化或 DB 不可达；首次需人工执行: $STORE_BIN init（会建表，故不自动跑）"
  fi
fi

# ---------- 22 workspace opencode.json（内网 MCP，SOFT） ----------
step "workspace MCP配置"
if [ -f "$WORKSPACE/opencode.json" ]; then ok
else warn "缺失：含内网 MCP 地址不进 git，需要时从旧机拷贝 $WORKSPACE/opencode.json"; fi

# ---------- 22b 共享任务 venv（任务级 pip 落点；relay.service 沙箱下系统/服务 venv 只读，SOFT） ----------
step "共享任务venv"
SHARED_VENV="$WORKSPACE/shared/venv"
if [ -x "$SHARED_VENV/bin/pip" ]; then ok
elif heal; then
  miss "创建 $SHARED_VENV"
  if python3 -m venv "$SHARED_VENV" >/dev/null 2>&1 \
     && "$SHARED_VENV/bin/pip" install -q --upgrade pip -i "$PIP_MIRROR" >/dev/null 2>&1; then
    ok "已创建（任务 PATH 前置，裸 pip install 即用）"
  else warn "创建失败（任务 pip 需 --user 或自建 venv，不影响其余功能）"; fi
else warn "缺失（install 模式自动创建）"; fi

# ---------- 23 claude-proxy（可选组件，SOFT） ----------
step "claude-proxy"
if [ -f "$PROXY_DIR/proxy.mjs" ] && [ -f "$PROXY_DIR/config.json" ] && [ -f "$UNIT_DIR/claude-proxy.service" ] && systemctl is-active --quiet claude-proxy; then
  grep -q 'sk-xxxx' "$PROXY_DIR/config.json" && warn "运行中但 config.json 仍是示例密钥" || ok
elif heal; then
  miss "安装"
  cp -n "$RES_ROOT/claude-code-openai-proxy/proxy.mjs" "$PROXY_DIR/proxy.mjs" 2>/dev/null
  if [ ! -f "$PROXY_DIR/config.json" ]; then
    cp "$RES_ROOT/claude-code-openai-proxy/config.example.json" "$PROXY_DIR/config.json" && chmod 600 "$PROXY_DIR/config.json"
  fi
  if [ ! -f "$UNIT_DIR/claude-proxy.service" ]; then
    sed -e "s#__DIR__#$PROXY_DIR#g" -e "s#__NODE__#$(command -v node)#g" \
      "$RES_ROOT/claude-code-openai-proxy/claude-proxy.service.example" > "$UNIT_DIR/claude-proxy.service"
    systemctl daemon-reload && systemctl enable --now claude-proxy >/dev/null 2>&1
  fi
  systemctl is-active --quiet claude-proxy && warn "已安装运行；config.json 需填 baseURL/apiKey/model" || warn "已安装但未运行（检查 config.json）"
else warn "缺失（可选组件，install 模式自动安装）"; fi

# ---------- 24 relay-tls-proxy（仅 HTTPS 主机：回环明文健康检查转发 127.0.0.1:8787→8788，SOFT；内容级同步，角色变更随仓库模板自动迁移） ----------
step "relay-tls-proxy"
TLS_DIR="$WORKSPACE/shared/secrets/relay-tls"
if [ ! -f "$TLS_DIR/fullchain.pem" ] || [ ! -f "$TLS_DIR/server.key" ]; then ok "非 HTTPS 主机（无证书），跳过"
else
  want="$(cat "$RES_ROOT/relay-service/relay-tls-proxy.service" 2>/dev/null)"
  have="$(cat "$UNIT_DIR/relay-tls-proxy.service" 2>/dev/null)"
  if [ -z "$want" ]; then warn "仓库缺 relay-tls-proxy.service 模板"
  elif [ "$want" != "$have" ]; then
    if ! heal; then warn "与仓库模板漂移（install 模式自动同步）"
    else
      miss "安装/更新单元"
      command -v socat >/dev/null || pkg_install socat
      [ -n "$have" ] && cp "$UNIT_DIR/relay-tls-proxy.service" "$UNIT_DIR/relay-tls-proxy.service.provision-bak"
      printf '%s\n' "$want" > "$UNIT_DIR/relay-tls-proxy.service" && systemctl daemon-reload \
        && systemctl enable relay-tls-proxy >/dev/null 2>&1
      systemctl restart relay-tls-proxy
      systemctl is-active --quiet relay-tls-proxy && ok "已同步+运行" || warn "更新后未运行"
    fi
  elif systemctl is-active --quiet relay-tls-proxy; then ok
  elif heal; then
    miss "启动"
    command -v socat >/dev/null || pkg_install socat
    systemctl enable --now relay-tls-proxy >/dev/null 2>&1
    systemctl is-active --quiet relay-tls-proxy && ok "已启动" || warn "启动失败"
  else warn "单元存在但未运行（install 模式自动启动）"; fi
fi

# ---------- 25 首装引导（无现役 release 时执行 boot.sh boot 完成首次部署） ----------
step "首装引导"
if [ -L "$CURRENT_LINK" ]; then ok "已是 release 布局"
elif heal; then
  miss "无现役 release，执行 boot.sh boot（clone+测试门禁+上线，约 1-3 分钟）"
  if bash "$BOOT_DIR/boot.sh" boot && [ -L "$CURRENT_LINK" ]; then ok "引导完成"
  else abort "boot.sh boot 失败（详见 /opt/team/relay-deploy/boot.log）"; fi
else bad "无现役 release"; fi

# ---------- 26 relay 服务 ----------
step "relay服务"
if heal; then
  systemctl is-enabled --quiet "$UNIT" 2>/dev/null || systemctl enable "$UNIT" >/dev/null 2>&1
  systemctl is-active --quiet "$UNIT" || systemctl start "$UNIT" >/dev/null 2>&1
fi
if systemctl is-active --quiet "$UNIT" && wait_healthy 30; then ok "active+healthy version=$(health_version)"
elif heal; then
  systemctl restart "$UNIT" >/dev/null 2>&1
  wait_healthy 30 && ok "重启后健康" || abort "relay 不健康（journalctl -u $UNIT 排查）"
else bad "未运行或不健康"; fi

# ---------- 27 定时任务 ----------
step "定时任务(timer)"
if heal; then
  for t in relay-boot.timer relay-backup.timer; do
    systemctl is-enabled --quiet "$t" 2>/dev/null || systemctl enable "$t" >/dev/null 2>&1
    # failed 态（如触发目标单元曾被移除）不会再触发，必须 restart 拉起
    systemctl is-active --quiet "$t" || systemctl restart "$t" >/dev/null 2>&1
  done
fi
if systemctl is-enabled --quiet relay-boot.timer 2>/dev/null && systemctl is-active --quiet relay-boot.timer \
   && systemctl is-enabled --quiet relay-backup.timer 2>/dev/null && systemctl is-active --quiet relay-backup.timer; then
  ok "boot(01:00)+backup(03:30) enabled+active"
elif heal; then abort "timer 无法启用/激活"
else bad "timer 未启用或 failed（install 模式自动拉起）"; fi

# ---------- 27b GitHub 访问就绪（机队默认 SSH deploy key；HTTPS+PAT 备用） ----------
step "GitHub访问"
case "$REPO_URL" in
git@*|ssh://*)
  SSH_NOTE=""
  if [ -f /root/.ssh/config ] && grep -q "accept-new" /root/.ssh/config; then :
  elif heal; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    printf '\nHost github.com\n    User git\n    IdentityFile ~/.ssh/id_ed25519\n    StrictHostKeyChecking accept-new\n    ServerAliveInterval 30\n    ServerAliveCountMax 4\n' >> /root/.ssh/config
    chmod 600 /root/.ssh/config && SSH_NOTE="config已固化; "
  else SSH_NOTE="config缺accept-new段(install自动固化); "; fi
  if [ -f /root/.ssh/id_ed25519 ] || [ -f /root/.ssh/id_rsa ]; then :
  elif heal; then
    ssh-keygen -t ed25519 -C "relay-$(hostname)" -f /root/.ssh/id_ed25519 -N "" -q && chmod 600 /root/.ssh/id_ed25519 && SSH_NOTE="${SSH_NOTE}已生成新密钥(待登记); "
  else SSH_NOTE="${SSH_NOTE}无密钥(install自动生成); "; fi
  SSH_OUT="$(timeout 20 ssh -o BatchMode=yes -T git@github.com 2>&1)"  # 认证成功 github 也 exit 1，不能用管道+pipefail 判定
  case "$SSH_OUT" in
  *"successfully authenticated"*) ok "SSH ${SSH_NOTE}认证 OK" ;;
  *) warn "SSH ${SSH_NOTE}认证失败——把公钥登记到仓库 Settings→Deploy keys（勾选 Allow write access）：$(cat /root/.ssh/id_ed25519.pub 2>/dev/null || echo '(无公钥)')" ;;
  esac
  ;;
*)
  [ -s /root/.git-credentials ] && ok "HTTPS+PAT（备用模式）" || warn "HTTPS 模式但 /root/.git-credentials 缺失（拉取/推送会失败）"
  ;;
esac

# ---------- 28 GitHub 远端（SOFT） ----------
step "GitHub远端"
SHA="$(timeout 30 git ls-remote "$REPO_URL" "refs/heads/$BRANCH" 2>/dev/null | cut -f1)"
CUR=""
[ -L "$CURRENT_LINK" ] && CUR="$(basename "$(dirname "$(readlink -f "$CURRENT_LINK")")")"
if [ -n "$SHA" ] && [ "$SHA" = "$CUR" ]; then ok "remote=current=${SHA:0:7}"
elif [ -n "$SHA" ]; then warn "remote=${SHA:0:7} current=${CUR:0:7}（下一轮 boot 自动部署）"
else warn "远端不可达（网络异常？boot.sh 每日轮次会另行钉钉预警）"; fi

# ---------- 29 dws 登录态验证（SOFT） ----------
step "dws登录态验证"
if [ -d "$DWS_SEED" ] && command -v dws >/dev/null; then
  WORK="$(mktemp -d /tmp/provision-dws-XXXXXX)"
  mkdir -p "$WORK/.local/share" && ln -s "$DWS_SEED" "$WORK/.local/share/dws-cli"
  if HOME="$WORK" timeout 30 dws contact user me --format json >/dev/null 2>&1; then ok "种子登录态可用"
  else warn "种子在但查询失败（登录态过期？跑 dws-renew）"; fi
  rm -rf "$WORK"
else warn "跳过（无种子或无 dws）"; fi

# ---------- 29b IM Bridge（SOFT，默认不启用，marker 开关随时启动） ----------
step "IM Bridge"
IMB_SRC="$RES_ROOT/im-bridge"
IMB_DIR=/opt/team/relay-im-bridge
IMB_STATE="$WORKSPACE/im-bridge"
IMB_UNIT=relay-im-bridge.service
IMB_MARKER=/etc/relay-im-bridge.enabled
if [ -d "$IMB_SRC" ]; then
  if heal; then
    mkdir -p "$IMB_DIR" "$IMB_STATE/rounds" "$IMB_STATE/media"
    cp -f "$IMB_SRC/bridge.py" "$IMB_SRC/bridge-cli" "$IMB_DIR/"
    chmod 755 "$IMB_DIR/bridge.py" "$IMB_DIR/bridge-cli"
    [ -f "$IMB_STATE/config.json" ] || cp "$IMB_SRC/config.example.json" "$IMB_STATE/config.json"
    sed -e "s|@VENV_PY@|$VENV/bin/python|" -e "s|@BRIDGE_DIR@|$IMB_DIR|" -e "s|@STATE_DIR@|$IMB_STATE|" \
        "$IMB_SRC/relay-im-bridge.service.template" > "$UNIT_DIR/$IMB_UNIT"
    systemctl daemon-reload
  fi
  if [ -f "$IMB_MARKER" ]; then
    if heal; then systemctl enable --now "$IMB_UNIT" 2>/dev/null; fi
    if systemctl is-active --quiet "$IMB_UNIT"; then ok "enabled+active（名单/参数 $IMB_STATE/config.json）"
    elif heal; then bad "marker 在但服务拉不起来（journalctl -u $IMB_UNIT）"
    else bad "marker 在但服务未运行"; fi
  else
    if heal; then systemctl disable --now "$IMB_UNIT" 2>/dev/null; fi
    if [ -f "$UNIT_DIR/$IMB_UNIT" ]; then ok "已安装未启用（启用: touch $IMB_MARKER && systemctl enable --now $IMB_UNIT）"
    elif heal; then bad "unit 安装失败"
    else warn "未安装（install 模式自动安装）"; fi
  fi
  [ -f "$IMB_STATE/bridge.env" ] || warn "bridge.env 缺失（启用前需建 relay 用户+设备激活，见 im-bridge/README.md）"
else warn "资源树无 im-bridge 目录（旧版本？）"; fi

# ---------- 30 本机私有附加检查（SOFT，可选 hook） ----------
step "本机附加检查"
if [ -f "$LOCAL_HOOK" ]; then
  chmod +x "$LOCAL_HOOK" 2>/dev/null
  if "$LOCAL_HOOK" "$MODE"; then ok "hook 通过"
  else warn "hook 报错（详见其输出）"; fi
else ok "无 hook（内网 MCP 等本机检查可写 $LOCAL_HOOK）"; fi

summary_exit
