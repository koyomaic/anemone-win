# relay-avatar —— 双入口统一分身网关（im-bridge × fenshen-next 能力合并）

钉钉（dws）与 relay API 两条消息接收路径并存，收敛到同一策略核：存储规则、
调用约束、权限、审计只有一份实现、一份 `policy.toml`。设计方案定稿 v2.1
（2026-09-23，王洪彬确认 6 项决策）见 `workspace/work/relay-avatar-设计方案-20260923.md`。

## 架构

```
入口层(两条都保留)              策略核(唯一)                    执行层(双后端)
A. 钉钉/dws: event consume      policy/  权限分层+审批流+限流     relay 后端(默认)
   +self轮询+SELF_MARK          storage/ 统一存储规则+store台账    /tasks+门禁+追问
   +媒体下载+表情ACK             session/ 状态机+debounce聚合      设备鉴权(PG落库)
B. relay API 客户端              monitor/ 心跳+入站健康+probe      local 后端(受限,P2)
   (web/CLI/他机/avatar-cli)     security/ [security]配置节        claude CLI,仅self/L3
                                        ↓                         硬隔离,升级不降级
                        统一 round/audit 记录 ←─ task_id 关联 relay PG
```

- **权限分层**：guest / L3（文件投递）/ L2（白名单）/ L1（管理员）/ super（超管）。
  动态白名单钉钉内闭环：「申请授权」→ super「同意/拒绝/同意传 <工号|姓名>」→
  热生效，默认 90 天过期。
- **存储**：业务文件一律进 store 台账（`shared/data|report|knowledge`，09-22 规范）；
  `$WORKSPACE/avatar/` 只放运行态（state/rounds/audit/events/run）。store 连接走
  `index/store-task.env` 最小权限角色，禁读 relay.env 与 /proc/*/environ。
  DB 不可达时上传文件先落 `run/staging` 暂存，恢复后自动补登记。
- **监控**：heartbeat 30s + inbound_health 三态（ok/bad_credentials/stalled）+
  probe 快照 + 看板 `:8791`（人：口令登录页+会话 cookie 12h，防爆破同 IP 15 分钟
  失败 5 次锁定；机：X-Token 走 `AVATAR_DASHBOARD_TOKEN`，probe POST /report 用）
  + critical 钉钉告警（状态跳变才推，6h 重提）。
- **安全默认收紧**（修 fenshen 审计 P0/P1）：L2 不再 bypassPermissions、
  注入清洗不可关、动态授权强制过期、bot 永久拉黑、profile 缺失留痕、
  凭据只进 env 文件（600）、integrity 漂移检测+告警+固化快照。
- **群监控**：`[group_watch] enabled=false` 默认关；开启需显式改 policy.toml 并评审（P4）。

## 部署（机队标准，provision 29b）

- 代码安装位 `/opt/team/relay-avatar/`，服务 `relay-avatar.service`（SIGTERM 优雅停）。
- 启用语义沿用 31b：默认 disabled；`touch /etc/relay-avatar.enabled &&
  systemctl enable --now relay-avatar` 随时启动；marker 在但服务不活 = FAIL 预警。
- 首装自动从 `im-bridge/` 迁移状态（`app/migrate.py`，幂等）：bridge.env →
  `shared/secrets/relay-avatar.env`（补随机看板 token）、config.json 名单 →
  policy.toml、state/rounds/events 拷贝；旧 unit/marker 由 provision 回收。
- 依赖：store CLI（provision 21e）+ `store ping` 可达；dws 登录态。
- **机队各机用自己的 dws 认证（每服务器独立登录，非共享账号）**，启用前按本机补两处：
  1. `policy.toml [identity] self_open_id`＝本机账号 openId（`dws contact user me`）
  2. relay 面板注册本机设备 → 填 `relay-avatar.env` 的 `RELAY_DEVICE_ID/SECRET`
- 文字表情模板（马上办/办妥啦/出错了）**机队共用**，example 已带 ID，各机直接用。
- superadmin/alert_to 保持王洪彬（18434）：审批通知与告警跨账号按 userId 直发，同组织可达。

## 文件

| 文件 | 说明 |
|---|---|
| `policy.example.toml` | 策略模板（权限/约束/安全唯一配置源；首装拷入状态目录） |
| `avatar.env.example` | 凭据模板（部署位 `shared/secrets/relay-avatar.env`，600） |
| `app/main.py` | 守护入口（双 ingress 循环 + 策略路由 + 门禁透传） |
| `app/config.py` / `policy.py` | policy.toml 加载 / tier 约束表+审批流+限流+清洗 |
| `app/session.py` / `storage.py` | 状态机+debounce / 统一存储规则+store 台账 |
| `app/audit.py` | append-only 审计 |
| `app/ingress_dingtalk.py` | event consume+看门狗+入站健康+SELF_MARK+轮询+媒体 |
| `app/ingress_relay.py` | relay API 客户端（设备鉴权，avatar-cli 同源） |
| `app/exec_relay.py` / `exec_local.py` | relay 后端（默认）/ local claude 后端（P2，默认关） |
| `app/file_inbox.py` | 上传文件收件：shared/data 落盘+store add/link+doc→md |
| `app/knowledge.py` | jingboWiki 权威+shared/knowledge 兜底（P2） |
| `app/group_watch.py` | 群监控登记/采集/提炼（默认关，P4 全链路） |
| `app/monitor.py` / `dashboard.py` | 心跳/probe/告警 / 看板 :8791 |
| `app/integrity.py` | 代码漂移检测+固化快照+告警（P3 基础版） |
| `app/migrate.py` | im-bridge 状态迁移（幂等） |
| `app/recall.py` | 出站回执落档（run/outbox.jsonl）+ 撤回（openTaskId→query-send-status→recall） |
| `avatar-cli` | 本机直提 + `--status` 运维快照 + `--recall [--n N] [--task TID]` 撤回出站消息 |
| `tests/` | pytest（沿用 relay 门禁标记约定：not integration/pg） |

## 自测

```bash
# 单测（门禁同款）
/mnt/vol-eltaah12/finaagent/.venv/bin/python -m pytest relay-avatar/tests -q
# 注入自测（不真发钉钉）
python3 app/main.py --state /tmp/avatar-test --inject events.ndjson --dry-send
```
