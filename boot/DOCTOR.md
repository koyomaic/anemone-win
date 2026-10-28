# relay-doctor —— 机队每日体检（06:00 日报）

## 定位

每日一份「体检日报 + 趋势 + 依赖漂移探测」，Markdown DM 推 `DOCTOR_ALERT_TO`
（默认王洪彬）。与既有告警的分工：

| 通道 | 时机 | 覆盖 |
|---|---|---|
| boot.sh notify_failure | 即时 | 部署门禁失败 |
| avatar monitor.py | 实时（状态跳变） | 进程/入站/定时器存活 |
| avatar 计划投递告警 | 即时 | 定时计划 🔴放弃 / 🟠体检 |
| **relay-doctor** | **每日 06:00** | **趋势外推、依赖漂移、24h 回顾、死人开关** |

doctor 不做即时告警（避免与上面三条重复）；**日报本身即死人开关**——某天 06:00
没收到，就是 doctor 或这台机挂了。全绿也发一行。

## 组成（全部在 git 真源 `boot/`，夜间 provision 同步到各机 `/opt/team/relay-boot/`）

- `doctor_checks.py` —— 检查套件：**采集器**（薄、带副作用、异常兜底成 warn）与
  **判级器**（纯函数）分离；判级器被 `relay-service/tests/test_doctor.py` 密封单测
  覆盖，进夜间门禁。
- `doctor.sh` —— 编排：flock 防重入 → 跑套件 → dws 种子登录态 DM（与 boot.sh
  notify_failure 同一份种子，token 轮换直接发生在种子上）→ 日志 `/var/log/relay-doctor.log`。
- `relay-doctor.service` —— Type=oneshot；Nice=10 / IOSchedulingClass=idle /
  CPUSchedulingPolicy=batch / MemoryMax=512M / TasksMax=64 / TimeoutStartSec=1200。
- `relay-doctor.timer` —— `OnCalendar=*-*-* 06:00:00`，`Persistent=true`（关机错过
  开机补跑），`RandomizedDelaySec=90`（机队错峰）。

## 检查目录（26 项，五类）

| 类 | id | 判级 |
|---|---|---|
| 业务 | relay_health | /health 不可达 → CRIT（组内其余 skip） |
| 业务 | task_failed_24h | ≥1 WARN，≥3 CRIT；错误聚类前 2 类 |
| 业务 | queue_backlog | 排队>5 或最老>10min → WARN |
| 业务 | approval_stuck | 有滞留审批 → WARN（60min 窗自动 failed，只提示） |
| 业务 | plan_overdue | enabled 计划 next_run 逾期>1h 或为空 → WARN |
| 数据 | debt_tracker | auth_legacy=on / max_concurrent≠机队期望 → WARN（可 ack） |
| 数据 | pg_health | PG 直连 SELECT 1 失败 → CRIT；无 RELAY_DB/无驱动 → info |
| 资源 | disk_space | 任一挂载点余量 <15% WARN / <8% CRIT；inode<20% 提示、<10% CRIT |
| 资源 | disk_forecast | history 7 天最小二乘外推到 95%：<14d WARN / <5d CRIT（样本<3 天只积累） |
| 资源 | dir_growth | tasks/avatar/releases/varlog 日增 >500MB → WARN |
| 资源 | service_recap | relay/avatar/tls-proxy/claude-proxy 非全 active → CRIT；NRestarts 增量>0 → WARN |
| 资源 | journal_errors | 24h err 级日志 >0 WARN / >10 CRIT（附末 2 条） |
| 元 | timer_stuck | 关键 timer（boot/backup/plan/doctor）装了但未激活 → WARN |
| 依赖 | engine_smoke | **真跑** opencode+claude 最小往返（全日唯一 2 次模型调用预算）；失败 CRIT |
| 依赖 | version_drift | opencode/claude/node/python/dws 版本指纹与昨日 diff → WARN |
| 依赖 | model_gateway | 各 provider GET /models（零推理 token）：401/403 key 失效、网络断 → CRIT |
| 依赖 | dws_login | dws 登录态失效 → CRIT（所有钉钉通道都依赖它） |
| 依赖 | git_remote | GitHub SSH 不可达 → WARN（今晚部署将失败）；顺带报今晚是否换版 |
| 依赖 | install_drift | /opt/team/relay-avatar/app 与 release 树 diff → WARN |
| 业务 | plan_fire_fail | relay-plan.log 24h 内 `FAIL fire plan=` → CRIT |
| 业务 | plan_delivery | avatar 审计 24h 内 gave_up → CRIT / health_alert → WARN |
| 数据 | backup_fresh | timer 未激活 WARN；>26h 无新备份 CRIT；大小偏离中位 0.5~3x 外 WARN |
| 数据 | cert_expiry | 8788 证书 <14d WARN / <3d CRIT |
| 数据 | secrets_perms | relay.env 等凭据权限非 600/700 → WARN |
| 元 | provision_recap | 最近 provision FAIL>0 → CRIT；WARN 数高于历史基线 → WARN |
| 元 | doctor_self | 上次体检距今 >26h（漏跑）→ WARN |

## 状态与历史（`$WORKSPACE/doctor/`，权限 600）

- `state.json` —— 版本指纹、服务重启计数基线、provision WARN 基线、`acks` 静默表
- `history.ndjson` —— 每天一行（overall / 各检查状态 / 磁盘占用 / 目录大小 / 备份大小），保留 90 天
- `reports/YYYY-MM-DD.md` —— 完整日报，保留 30 天；DM 里只发 ≤2200 字符 digest

## ack 静默（已知债不重复刷屏）

```bash
/opt/team/relay-boot/doctor_checks.py --ack debt_tracker --days 30 --reason "等马韵升切并发"
```
生效期内该检查的 warn/crit 降级为 info（日报保留原文并标 🔕），到期自动恢复。

## 每机配置（boot.env，全部可选）

```
DOCTOR_ENABLED=1        # 0=本机彻底静默（provision 会停用 timer）
DOCTOR_ALERT_TO=王洪彬   # 日报收件人（默认沿用 ALERT_TO）
DOCTOR_SMOKE=1          # 0=关引擎真跑（省 2 次模型调用/日）
DOCTOR_HOST_CN=xx机      # 日报标题里的机名
DOCTOR_PYTHON=...       # 解释器（默认 finaagent venv，回退 /usr/bin/python3）
```
阈值类（DOCTOR_DISK_WARN_PCT / DOCTOR_FORECAST_*_DAYS / DOCTOR_FLEET_MAX_CONCURRENT /
DOCTOR_DIR_GROW_WARN_MB / DOCTOR_HISTORY_DAYS / DOCTOR_REPORT_DAYS）见 doctor_checks.py 头部。

## 性能红线

- 常态一轮（含 smoke）实测 <2 分钟；无 smoke <10 秒。
- 全日模型调用预算 ≤2 次（engine_smoke）；网关探测走 /models 零 token。
- 所有外部调用带紧超时；套件内任何采集器异常只降级为该项 warn，不中断整轮。
- doctor 自己也在 timer_stuck 名单里——看门狗也要被看。

## 失败语义

- 套件崩溃 → doctor.sh 兜底发「体检套件崩溃」DM 并 exit 1（systemd 记 failed）。
- DM 发送失败 → 只记日志（死人开关兜底：连续两天没日报即异常）。
- 正常完成一律 exit 0：crit 是「发现」，不是「脚本故障」。
