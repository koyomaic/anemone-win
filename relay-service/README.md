# 团队中转服务（Relay/Gateway）

按《基于ClaudeCode的经营智能体蜂群设计方案》实现的中转服务 MVP：
本地千问通过 `team-agent` CLI 向本服务提交任务，服务排队调度、
以无头方式驱动服务器上的 agent（默认 `opencode run`，可切 `claude -p`）
在每任务独立工作目录中执行，完成后扫描共享区变更、检测冲突并留备份。

## 目录布局

```
relay-service/
├── app/                 # FastAPI 服务（config/db/targets/events/audit/workspace/executor/scheduler/main/entry/serve）
├── tests/               # 61 个测试（单测 + 真实 opencode 端到端）
├── team_agent.py        # CLI 客户端
└── requirements.txt
# systemd unit 真源在仓库 boot/relay.service.template（provision.sh 渲染 __CURRENT_LINK__/__WORKSPACE__/__VENV__ 占位符后落到 /etc/systemd/system/relay.service），本目录不再存放副本

运行时数据（非系统盘）：
/mnt/vol-eltaah12/workspace/
├── shared/              # 团队共享区：knowledge/ 知识库 + skills/ mcp/ memory/（共享知识，上传即全员可用）
├── tasks/<task_id>/     # 每任务独立工作目录（agent 的 cwd + 独立 XDG 数据目录）
├── users/               # 成员个人持久区
├── relay.db             # SQLite（用户 token + 任务全量日志）
├── relay.log            # 审计日志（submit/start/stop/confirm/error）
├── relay.env            # 服务环境变量（含 admin token，600 权限）
```

## 配置（两层：relay.defaults.env + relay.env）

systemd 按序加载，**后者覆盖前者**：

1. **`relay.defaults.env`**（进 git，随版本发布）：全机队统一的非涉密默认值——
   并发、超时、引擎、模型、路径、拦截策略等。改一次 push，全机队生效，无需逐台配置。
2. **`relay.env`**（每台机器各自维护，永不进 git，600 权限）：只放密钥
   （`RELAY_ADMIN_TOKEN`、`RELAY_DASHBOARD_PASSWORD`）与个别本机覆盖项。
   模板见 `relay.env.example`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| RELAY_WORKSPACE | /mnt/vol-eltaah12/workspace | 工作区根目录 |
| RELAY_DB / RELAY_SHARED / RELAY_TASK_ROOT / RELAY_USERS | 根目录下对应子目录 | 可分别覆盖 |
| RELAY_ADMIN_TOKEN | 必填 | 管理端 token（建/删成员） |
| RELAY_MAX_CONCURRENT | 3 | 并行 agent 进程上限 |
| RELAY_TASK_TIMEOUT | 1800 | 单任务超时（秒） |
| RELAY_ENGINE | opencode | opencode \| claude |
| RELAY_AGENT_BIN | 按引擎 | agent CLI 路径覆盖 |
| RELAY_MODEL | 空 | 覆盖 agent 模型（opencode -m） |
| RELAY_XDG_CONFIG_HOME | ~/.config | 共享 agent 配置目录（provider/技能全员共用） |
| RELAY_DASHBOARD_PASSWORD | 空 | 监控页面口令（空=禁用） |
| RELAY_STALE_TIMEOUT_HOURS | 24 | review/待审批 停留超时 → 自动 done 并标记 |
| RELAY_SWEEP_INTERVAL_SEC | 300 | 超时清扫周期（秒） |
| RELAY_BLOCKED_PATTERNS | 核心清单(见下) | 高危命令拦截正则（逗号分隔，覆盖默认） |
| RELAY_RM_SAFE_PREFIXES | /tmp,/var/tmp | 递归 rm 在这些前缀下放行，其余拦截 |

### 高危命令拦截策略（建设初期：开放优先，只守核心）

- **不拦**（放行）：`systemctl`（含 start/stop/restart 等全部）、`iptables`、`ufw`、`crontab -r`、
  `useradd`/`userdel`、`curl|sh`、`wget|sh`、非递归单文件 `rm`；
- **递归 rm 按路径**：目标在 `RELAY_RM_SAFE_PREFIXES`（默认 /tmp、/var/tmp）下放行，
  其余（含相对路径/`~`/变量，无法验证）拦截；
- **核心拦截**（不可逆/会炸服务器）：`mkfs`、写裸盘（`of=/dev/…`、`> /dev/…` 磁盘设备）、
  `shutdown`/`reboot`/`poweroff`/`halt`、`kill -9 1`（init）、`chmod -R 777 /` 或 `~`；
- **dws 钉钉外发二次确认**：对 dws 发送类命令按目标分级——
  - **1:1 单聊放行**（`--user`/`--open-dingtalk-id`/`+dm --to`，或 `--chat-id` 解析为单聊会话）；
  - **群聊/多接收人(≥2)/命中敏感对象** → 拦。群聊含：群名（`--chat-query`/`--group 名`）、
    多群（`--groups`）、webhook（`--webhook-token`）；`--chat-id`/`--conversation-id` 传 cid 时
    经 `dws chat +conversation-info` 判会话类型（单聊 `singleChat=true` 放行，群聊 `false` 拦，
    解析失败按群聊拦=保守）。敏感对象（`RELAY_CONFIRM_RECIPIENTS`，默认含马韵升 userId=1/其
    openDingTalkId/姓名）命中**一律拦**，不随单聊放宽。
- 命中 → 任务转 `pending_approval`，人工 approve（放行该条命令续跑）/ deny（置 failed）。
  放行入口：监控页「待审批」行的 批准/拒绝 按钮，或客户端 `team-agent approve <id> --decision approve|deny`。
  审批后 resume 带豁免，按**发送目标签名**匹配（agent 重排格式也不漏判）。

### 共享知识（取消隔离：个人上传即团队全员可用，立即生效）

`shared/` 下三类共享知识，**任何成员上传后下一个任务自动加载，无需重启**：

| 目录 | 内容 | 加载方式 |
| --- | --- | --- |
| `shared/skills/<name>/` | 团队技能（SKILL.md + 资源文件） | 每次任务运行时注入 opencode `skills.paths`，进入全员技能列表 |
| `shared/mcp/<name>.json` | 团队 MCP 服务器（每文件一个条目：`{"type":"local","command":[...]}` 或 `{"type":"remote","url":...}`） | 运行时合并进 opencode 配置 `mcp`，全员 agent 可直接调用其工具 |
| `shared/memory/<name>.md` | 团队记忆（Markdown） | 运行时拼接注入每个任务的 AGENTS.md |

机制：`app/shared_knowledge.py::prepare_run` 在每次任务运行前为 workdir 生成隔离的
`XDG_CONFIG_HOME`（`tasks/<id>/.xdg/`），= 全局基线配置 + 上述三类共享知识；
全局配置的 `node_modules` 等以符号链接复用，不重装依赖。

上传/管理入口：监控页"共享知识"页签（skill 文件 / MCP JSON / 记忆 Markdown，
含查看与删除）；API：`GET /dashboard/shared`、`POST/DELETE /dashboard/shared/{skills,mcp,memory}/...`
（监控口令会话）。单文件 ≤1MB；名称限字母数字与 `._-`。

## 启动

```bash
# systemd（生产）：unit 由 provision.sh 从 boot/relay.service.template 渲染同步，无需手工拷贝
#   首次装机 / 重新生成 unit：bash <release>/boot/provision.sh install
#   之后 systemctl enable --now relay && systemctl status relay

# 手动（调试）：app.serve 按 RELAY_TLS_DIR 证书存在性自听 TLS:8788 或明文:8787
set -a && . /mnt/vol-eltaah12/workspace/relay.env && set +a
python -m app.serve
```

## 使用（CLI）

```bash
export TEAM_AGENT_SERVER=http://<服务器>:8787

# 管理员：发 token
export TEAM_AGENT_TOKEN=<RELAY_ADMIN_TOKEN>
team_agent.py users create 张三        # 输出成员 token
team_agent.py users list / delete <名>

# 成员：提交任务（异步，拿到 task_id 即可走人）
export TEAM_AGENT_TOKEN=ta_xxx
team_agent.py run "写一份9月经营月报" --targets shared/report/市场分析/2026-09 --wait
team_agent.py run "补充结论" --resume 42          # 追任务，续接原会话

# 查询 / 实时流 / 取消 / 变更 / 确认
team_agent.py tasks --status running
team_agent.py task 42
team_agent.py stream 42                    # SSE：工具调用/文本/状态实时直播
team_agent.py cancel 42
team_agent.py diff 42                      # 变更文件清单（冲突判断依据）
team_agent.py confirm 42                   # review 完成后人工确认 -> done
team_agent.py approve 42 --decision approve   # pending_approval（高危拦截）放行续跑 / --decision deny 拒绝
```

## API（全部 Bearer token 认证）

| 接口 | 说明 |
| --- | --- |
| POST /users, GET/DELETE /users | 成员 token 管理（admin） |
| POST /tasks | 提交任务；targets 与在途任务重叠时 409（force=true 强制） |
| GET /tasks, GET /tasks/{id} | 列表 / 详情 |
| GET /tasks/{id}/stream | SSE 实时流（含历史回放） |
| POST /tasks/{id}/cancel | 取消（排队中直接取消；运行中杀整棵进程树） |
| GET /tasks/{id}/diff | 变更文件 + 冲突信息 |
| POST /tasks/{id}/confirm | review/conflict → done（人工核查确认） |
| POST /files | 上传文件到共享区（JSON：path + content_b64；单文件 ≤10MB，超限 413；防路径穿越） |
| GET /files?path= | 下载共享区文件（≤10MB；二进制直传） |
| GET /health | 健康检查（免认证） |

任务生命周期：`queued → running → review → done`，分支 `failed / conflict / cancelled`。

**两类自动 done（减少人工待办）**：
- **只读自动 done**：执行完且**无任何共享文件改动** → 直接 `done`（备注"自动done（只读，无文件改动）"），不落 review；
- **超时自动 done**：`review`/`pending_approval` 停留超过 `RELAY_STALE_TIMEOUT_HOURS`（默认 24h）→ 后台清扫置 `done` 并备注"超时自动done"。

## 并行不打架（无 git 版）

1. **事前防碰**：提交时声明 `targets`，与在途任务目录重叠 → 409 预警，人决定是否 force；
2. **物理隔离**：每任务独立工作目录 + 独立 XDG 数据目录（会话记录互不污染，支撑 --resume）；
3. **事后兜底**：完成时扫描共享区基线差异；同一文件被两个任务改动 → 后完成者标记
   `conflict`，先完成者版本自动备份为 `<文件>.bak-<用户>-HHMM`，绝不静默覆盖。

## 测试

```bash
cd relay-service
/mnt/vol-eltaah12/finaagent/.venv/bin/python -m pytest -v          # 32 项（含真实 opencode 集成），约 3-5 分钟
/mnt/vol-eltaah12/finaagent/.venv/bin/python -m pytest -m "not integration"  # 快速层
```

集成测试使用真实 opencode（非 mock），覆盖：认证与吊销、全生命周期、
urgent 插队、运行中取消（进程树杀灭）、超时、targets 重叠预警、
双任务同文件冲突备份、同内容重写不误报、会话续接（resume）、
SSE 实时流与历史回放、审计日志。

## 已知边界 / 后续

- 高危命令审批流、审计报表、监控、备份均已实现（Phase 4 完成）；
- dws 薄 skill 已落地（/opt/team/skills/dws）并挂载到服务器 opencode；
- 剩余：knowledge 目录每周整理（可定时 Claude 任务）、更多共享 skill 沉淀。

### 仪表盘（监控页面）

- 地址：`https://<服务器>:8788/`（内网 `https://10.189.51.32:8788/`），口令登录
  （`RELAY_DASHBOARD_PASSWORD`，现值在 relay.env）
- **调用情况**页签：运行/排队、今日/7日完成率、各状态分布、按用户统计、最近 50 任务（含"备注"列，展示只读/超时自动done、失败原因等标记），10s 自动刷新
- **凭证管理**页签：成员 token 增删改查（创建/重置/删除；列表展示完整 token + 近 7 天任务统计；重置后旧 token 立即失效）
- 会话：内存态 12 小时（重启需重新登录），HttpOnly + SameSite=Strict cookie
- 未设置口令时页面禁用（404）

### 备份（Phase 4，已完成）

- **脚本**：`scripts/backup.sh`（stdout 输出单行 JSON `{"ok":true,"file":...,"size_bytes":N,"kept":M}`）。
  备份目录 `/mnt/vol-eltaah12/backup/`，文件名 `relay-backup-YYYYmmdd-HHMM.tar.gz`
  （root 属主、chmod 600，因含 admin token）。
- **内容**：relay.db（sqlite3 CLI 可用时 `.backup` 一致性快照，不可用时 `cp` 并在输出中告警）、
  shared/ 整目录、relay.log、relay.env；打包后 `tar -tzf` 校验，失败则改名 `.failed` 并退出非 0。
- **定时**：`relay-backup.timer` 每日 03:30（`Persistent=true`，漏跑补跑）触发
  `relay-backup.service`（Type=oneshot，Nice=10）。
- **保留策略**：只留最近 3 份（按文件名排序删旧，`.failed` 也计入清理）。
- **手动触发**：`systemctl start relay-backup`（或直接 `bash scripts/backup.sh`）。
- **恢复方法**：`tar -xzf relay-backup-<时间戳>.tar.gz -C <临时目录>` 解压后，
  将 relay.db / relay.log / relay.env 及 shared/ 覆盖回 `/mnt/vol-eltaah12/workspace/`
  对应位置，然后 `systemctl restart relay`。
