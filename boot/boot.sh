#!/usr/bin/env bash
# relay-boot: 团队中转服务 bootloader（git 更新部署 / 存活守护 / 回滚）
# 用法: boot.sh [boot|update|status|rollback]
#   boot/update  检测 git 更新并部署；无更新则做服务存活守护（幂等，可重复执行）
#   status       显示远端/本地版本与健康状态
#   rollback     切回上一 release 并重启
# 自更新: 部署成功/守护轮次会把 release 内 boot/（本仓库真源）同步回本机
#         bootloader（boot.sh 原子替换、systemd unit 拷贝+daemon-reload）；
#         boot.env 为每机私有配置，永不覆盖。
set -euo pipefail
export GIT_TERMINAL_PROMPT=0  # 公开仓库匿名拉取，绝不交互要凭证（防挂起）

BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$BOOT_DIR/boot.env"
mkdir -p "$RELEASE_ROOT"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" >&2; }

notify_failure() { # $1=原因；更新失败 → 钉钉直聊预警（dws 软链共享种子，token 轮换直接发生在种子上，无复制无回写竞态）
  local who="${ALERT_TO:-王洪彬}" ver msg seed work rc=1
  ver="$(current_sha 2>/dev/null || true)"
  msg="【relay更新预警·$(hostname)】$1；当前版本 ${ver:-?}；$(date '+%F %T')；日志 $LOG"
  seed="${DWS_SEED:-/mnt/vol-eltaah12/workspace/shared/secrets/dws-cli-seed/dws-cli}"
  if [ -d "$seed" ]; then # 种子登录态：临时 HOME 里软链种子目录，dws 直接读写种子（与 dws-renew 同一份登录态）
    work="$(mktemp -d /tmp/relay-alert-XXXXXX)"
    mkdir -p "$work/.local/share"
    ln -s "$seed" "$work/.local/share/dws-cli"
    HOME="$work" "${DWS_BIN:-/usr/bin/dws}" chat +dm --to "$who" --content "$msg" --yes --format json >>"$LOG" 2>&1 && rc=0
    rm -rf "$work" # 只删软链本身，种子不受影响
  else # 无种子时退回本机 root 登录态直发
    HOME=/root "${DWS_BIN:-/usr/bin/dws}" chat +dm --to "$who" --content "$msg" --yes --format json >>"$LOG" 2>&1 && rc=0
  fi
  if [ "$rc" -eq 0 ]; then
    log "预警已发送 → $who"
  else
    log "预警发送失败 → $who（原因：$1）"
  fi
}

remote_sha() { timeout 30 git ls-remote "$REPO_URL" "refs/heads/$BRANCH" | cut -f1; }

current_sha() {
  [ -L "$CURRENT_LINK" ] || { echo ""; return; }
  basename "$(dirname "$(readlink -f "$CURRENT_LINK")")"
}

health_version() { # 双探：TLS 直听主机走 https:8788（自签 -k），否则回落 HEALTH_URL（明文/回环转发）
  local u v
  for u in "https://127.0.0.1:8788/health" "${HEALTH_URL:-http://127.0.0.1:8787/health}"; do
    v="$(curl -sfk -m 3 "$u" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin).get("version",""))' 2>/dev/null)" || true
    [ -n "$v" ] && { echo "$v"; return 0; }
  done
  echo ""
}

wait_healthy() { # $1=期望 version（空=只要健康即可）
  local i v
  for i in $(seq 1 30); do
    v="$(health_version)"
    if [ -n "$v" ] && { [ -z "$1" ] || [ "$v" = "$1" ]; }; then
      return 0
    fi
    sleep 1
  done
  return 1
}

migrate_if_needed() { # 首次：真实目录 → releases 布局（保留为回滚目标）
  if [ -d "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
    local mig="$RELEASE_ROOT/pre-git-$(date +%Y%m%d-%H%M%S)"
    log "首次迁移：真实目录 → release 布局（$mig）"
    mkdir -p "$mig"
    mv "$CURRENT_LINK" "$mig/relay-service"
    ln -sfn "$mig/relay-service" "$CURRENT_LINK"
  fi
}

switch_to() { # $1=release 目录名
  ln -sfn "$RELEASE_ROOT/$1/relay-service" "$CURRENT_LINK.new"
  mv -Tf "$CURRENT_LINK.new" "$CURRENT_LINK"
}

self_update_bootloader() { # $1=sha；从 release 的 boot/（仓库真源）同步本机 bootloader；boot.env 每机私有，永不覆盖
  local sha="$1" src dst f reloaded=0
  [ -n "$sha" ] || return 0
  src="$RELEASE_ROOT/$sha/boot"
  [ -d "$src" ] || return 0 # 老 release 无 boot/ 目录，跳过
  if [ -f "$src/boot.sh" ] && ! cmp -s "$src/boot.sh" "$BOOT_DIR/boot.sh"; then
    if cp "$src/boot.sh" "$BOOT_DIR/.boot.sh.new" && chmod +x "$BOOT_DIR/.boot.sh.new" \
       && mv -f "$BOOT_DIR/.boot.sh.new" "$BOOT_DIR/boot.sh"; then # 原子替换，运行中实例持旧 inode 不受影响
      log "bootloader 自更新：boot.sh ← release $sha"
    else
      rm -f "$BOOT_DIR/.boot.sh.new"
      log "bootloader 自更新失败：boot.sh（沿用旧版）"
    fi
  fi
  for f in relay-boot.service relay-boot.timer; do
    dst="/etc/systemd/system/$f"
    if [ -f "$src/$f" ] && { [ ! -f "$dst" ] || ! cmp -s "$src/$f" "$dst"; }; then
      if cp "$src/$f" "$dst"; then
        log "bootloader 自更新：$f → $dst"
        reloaded=1
      else
        log "bootloader 自更新失败：$f（沿用旧版）"
      fi
    fi
  done
  if [ "$reloaded" -eq 1 ]; then
    systemctl daemon-reload && log "systemd daemon-reload 完成" || log "systemd daemon-reload 失败，需人工执行"
  fi
}

run_provision() { # $1=sha；部署/守护轮次后执行安装检测清单（幂等自愈：检测→缺失→安装→验证）；老 release 无 provision.sh 则跳过
  local sha="$1" p rc=0
  [ -n "$sha" ] || return 0
  p="$RELEASE_ROOT/$sha/boot/provision.sh"
  [ -f "$p" ] || return 0
  log "执行安装检测清单（provision.sh install）"
  bash "$p" install >>"$LOG" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    log "安装检测清单 PASS"
  else
    log "安装检测清单 FAIL（rc=$rc），详见 $LOG"
  fi
  return "$rc"
}

sync_dev_workdir() { # 本地开发工作区同步远端 main（仅 fast-forward）；脏工作区/非目标分支/不可 ff 一律跳过并记日志，永不阻塞 boot 流程
  local d="${DEV_DIR:-/mnt/vol-eltaah12/finaagent/relay-dev}" before after
  [ -d "$d/.git" ] || return 0
  [ "$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || true)" = "$BRANCH" ] || { log "dev 工作区不在 $BRANCH 分支，跳过同步"; return 0; }
  before="$(git -C "$d" rev-parse HEAD 2>/dev/null || true)"
  if ! timeout 120 git -C "$d" fetch -q origin >>"$LOG" 2>&1; then
    log "dev 工作区 fetch 失败（网络异常？），本轮跳过同步"
    return 0
  fi
  if [ -n "$(git -C "$d" status --porcelain 2>/dev/null)" ]; then
    log "dev 工作区有未提交变更，跳过同步（需人工处理）"
    return 0
  fi
  if git -C "$d" merge --ff-only -q "origin/$BRANCH" 2>>"$LOG"; then
    after="$(git -C "$d" rev-parse HEAD)"
    [ "$after" != "$before" ] && log "dev 工作区同步：${before:0:7} → ${after:0:7}"
  else
    log "dev 工作区领先远端或不可 fast-forward，跳过同步（需人工处理）"
  fi
  return 0
}

deploy() { # $1=sha
  local sha="$1" rel="$RELEASE_ROOT/$1" prev got tmp
  if [ -d "$rel" ]; then
    log "release 已存在，直接切换：$sha"
  else
    tmp="$RELEASE_ROOT/.tmp-$sha"
    rm -rf "$tmp"
    timeout 600 git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$tmp"
    got="$(cd "$tmp" && git rev-parse HEAD)"
    if [ "$got" != "$sha" ]; then
      log "clone 期间远端移动（$got != $sha），本轮放弃"
      DEPLOY_FAIL_REASON="clone 失败或期间远端移动（目标 $sha）"
      rm -rf "$tmp"; return 1
    fi
    "$VENV/bin/pip" install -q -r "$tmp/relay-service/requirements.txt" >>"$LOG" 2>&1 || {
      log "依赖安装失败，中止部署（现役版本不动）"; DEPLOY_FAIL_REASON="依赖安装失败（目标 $sha）"; rm -rf "$tmp"; return 1; }
    (cd "$tmp/relay-service" && "$VENV/bin/python" -m pytest -q -m "not integration and not pg" -x) >>"$LOG" 2>&1 || {
      log "快速测试门禁未过，中止部署（现役版本不动）"; DEPLOY_FAIL_REASON="快速测试门禁未过（目标 $sha）"; rm -rf "$tmp"; return 1; }
    if [ -d "$tmp/relay-avatar/tests" ]; then # avatar 门禁（旧版本树无此目录则跳过）
      (cd "$tmp/relay-avatar" && "$VENV/bin/python" -m pytest -q -m "not integration and not pg" -x) >>"$LOG" 2>&1 || {
        log "relay-avatar 测试门禁未过，中止部署（现役版本不动）"; DEPLOY_FAIL_REASON="relay-avatar 门禁未过（目标 $sha）"; rm -rf "$tmp"; return 1; }
    fi
    echo "$sha" > "$tmp/relay-service/VERSION"
    rm -rf "$tmp/.git"
    mv "$tmp" "$rel"
  fi
  prev="$(current_sha)"
  switch_to "$sha"
  systemctl restart "$UNIT"
  if wait_healthy "$sha"; then
    log "部署成功：${prev:-none} → $sha"
    self_update_bootloader "$sha"
    prune
    return 0
  fi
  log "健康检查失败（$sha），回滚 → ${prev:-none}"
  if [ -n "$prev" ] && [ -d "$RELEASE_ROOT/$prev/relay-service" ]; then
    switch_to "$prev"
    systemctl restart "$UNIT"
    if wait_healthy ""; then
      log "回滚成功（$prev）"
      DEPLOY_FAIL_REASON="新版本 $sha 健康检查失败，已回滚 $prev"
    else
      log "回滚后仍不健康！需人工介入"
      DEPLOY_FAIL_REASON="新版本 $sha 健康检查失败且回滚后仍不健康，需人工介入！"
    fi
  else
    log "无可回滚版本！需人工介入"
    DEPLOY_FAIL_REASON="新版本 $sha 健康检查失败且无可回滚版本，需人工介入！"
  fi
  rm -rf "$rel"
  return 1
}

prune() { # 保留现役 + 最近 KEEP-1 份
  local cur d
  cur="$(current_sha)"
  ls -1t "$RELEASE_ROOT" 2>/dev/null | grep -v '^\.' | grep -vx "$cur" \
    | tail -n +$KEEP | while read -r d; do
      [ -n "$d" ] && rm -rf "${RELEASE_ROOT:?}/$d"
    done
}

cmd_boot() {
  local sha prc=0
  sha="$(remote_sha || true)"
  if [ -n "$sha" ] && [ "$sha" != "$(current_sha)" ]; then
    migrate_if_needed
    deploy "$sha" || { notify_failure "更新部署失败：${DEPLOY_FAIL_REASON:-未知原因，详见日志}"; exit 1; }
    run_provision "$sha" || prc=$?
  else
    [ -n "$sha" ] || notify_failure "更新检查失败：远端不可达或空仓库（github 网络异常？），本轮未检查更新"
    self_update_bootloader "${sha:-$(current_sha)}" # release 已存在（无更新轮次）也自愈 bootloader
    if ! systemctl is-active --quiet "$UNIT"; then
      log "服务未运行，启动"
      systemctl start "$UNIT" || true
    fi
    if wait_healthy ""; then
      log "守护 OK：version=$(health_version) remote=${sha:-远端不可达或空仓库}"
    else
      log "服务不健康，尝试重启"
      systemctl restart "$UNIT"
      wait_healthy "" && log "重启后恢复" || log "重启后仍不健康！需人工介入"
    fi
    run_provision "${sha:-$(current_sha)}" || prc=$?
  fi
  sync_dev_workdir
  if [ "$prc" -ne 0 ]; then
    notify_failure "安装检测清单（provision）未通过 rc=$prc，详见 $LOG"
    exit 1
  fi
}

cmd_status() {
  local r c
  r="$(remote_sha 2>/dev/null || echo unreachable)"; [ -n "$r" ] || r="(空仓库/无 $BRANCH 分支)"
  c="$(current_sha)"; [ -n "$c" ] || c="(真实目录，未迁移)"
  echo "repo:    $REPO_URL ($BRANCH)"
  echo "remote:  $r"
  echo "current: $c"
  echo "health:  $(health_version || true)"
  systemctl is-active --quiet "$UNIT" && echo "unit:    active" || echo "unit:    NOT ACTIVE"
}

cmd_rollback() {
  local cur prev
  cur="$(current_sha)"
  prev="$(ls -1t "$RELEASE_ROOT" | grep -v '^\.' | grep -vx "$cur" | head -1 || true)"
  [ -n "$prev" ] || { log "无可回滚版本"; exit 1; }
  switch_to "$prev"
  systemctl restart "$UNIT"
  wait_healthy "" && log "回滚成功：${cur:-?} → $prev" || { log "回滚后服务不健康！"; exit 1; }
}

case "${1:-boot}" in
  boot|update) cmd_boot ;;
  status)      cmd_status ;;
  rollback)    cmd_rollback ;;
  *) echo "usage: boot.sh [boot|update|status|rollback]" >&2; exit 2 ;;
esac
