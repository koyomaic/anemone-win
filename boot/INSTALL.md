# relay 机队安装清单（provision.sh）

> 可执行的顺序清单：**检测 → PASS → 继续；检测 → 缺失 → 安装 → 验证 → PASS → 继续**。
> 新服务器安装、每日 git 更新守护轮次，跑的都是同一份清单（本目录 `provision.sh`）。

## 组件全景

| 组件 | 单元 | 说明 |
|---|---|---|
| 中转服务 | `relay.service` | 有 TLS 证书时 `app.serve` TLS 直听 :8788（真实客户端 IP 不失真），否则明文 :8787；代码在 `CURRENT_LINK`（release 软链）；unit 真源 `boot/relay.service.template`，provision.sh 渲染同步 |
| git 同步部署 | `relay-boot.timer` → `boot.sh` | 每日 01:00 + 开机 2min：拉新→测试门禁→上线/回滚→**跑 provision 清单**→失败钉钉预警 |
| 工作区备份 | `relay-backup.timer` → `scripts/backup.sh` | 每日 03:30：数据库快照（SQLite `.backup` 或 PG `pg_dump -Fc`）+ shared/ + relay.env + relay.log → `/mnt/vol-eltaah12/backup/` |
| dws 登录态小时级刷新 | `dws-refresh.timer` → `dws-refresh.sh` | 每小时整点（±60s 抖动）：软链共享种子进临时 HOME 跑 `auth status`，token 刷新原地发生在种子上（与 boot 预警同一份登录态）；日志 `/var/log/dws-refresh.log`；登录态失效时清单 WARN，需人工设备码重授权 |
| 计划触发器 | `relay-plan.timer` → `plan-tick.sh` | 每分钟（±20s 抖动，`Persistent=true` 漏跑补跑）：扫本机 relay 的 due 计划→`POST /plans/{id}/fire` 原子认领并提交任务；纯 HTTP 打 admin 接口，不碰 systemd 单元/不写 /etc；日志 `/var/log/relay-plan.log`。计划由大脑/成员经 `POST /plans` 或 dashboard「计划」页创建，存本机各自的库 |
| Claude 代理 | `claude-proxy.service` | 可选：Anthropic→OpenAI 翻译代理 :8790 |
| TLS 代理 | `relay-tls-proxy.service` | 可选：仅 HTTPS 主机（有 relay-tls 证书才装），socat 回环明文 :8787→TLS :8788（仅本机健康检查兼容） |
| store 台账 | —（CLI，`/usr/local/bin/store`） | 业务存储统一索引：磁盘为文件真身、PostgreSQL（relay 同库 `public` schema、`store_` 前缀）为权威索引与血缘、`index/manifest.jsonl` 为降级快照。存储根＝relay 的 `shared_dir`（同址是刻意的，见 `boot.env.example` 的 `STORE_ROOT` 说明）；约定见 workspace `AGENTS.md`「共享区约定」 |
| 分身网关 | `relay-avatar.service` | 双入口统一分身网关（im-bridge 演进替代）：钉钉(dws)+relay API → 同一策略核 `policy.toml`（权限分层/审批流/限流/注入清洗/审计）→ relay 后端（默认，门禁透传）/local claude 后端（仅 self/L3，默认关）。代码 `/opt/team/relay-avatar`、状态 `workspace/avatar/`、看板 `:8791`（X-Token）、凭据 `shared/secrets/relay-avatar.env`（600）。**默认不启用**：`touch /etc/relay-avatar.enabled` 后 install 轮自动拉起；上传文件自动落 `shared/data/` store 台账（DB 不可达先暂存补登记）。详见 `relay-avatar/README.md` |

## 新服务器一键安装

前置：root、systemd、能出网到 github.com 与模型网关。其余全部由清单自动安装。

```bash
curl -fsSL https://raw.githubusercontent.com/koyomaic/business_advisor/main/boot/provision.sh \
  -o /tmp/provision.sh && bash /tmp/provision.sh install
```

**GitHub 访问方式**：机队默认 **SSH**（deploy key，每机一把）。新机 provision 自动补齐：生成 ed25519 密钥、
固化 `/root/.ssh/config`（github.com `accept-new`，systemd 非交互轮次必需）、认证实测；首装按其 WARN 提示把
本机公钥登记到仓库 Settings → Deploy keys（**勾选 Allow write access**，部署需 push），title 建议 `relay-<hostname>`。
**HTTPS + PAT 为备用**（凭据 `/root/.git-credentials`）：SSH 故障时把 `boot.env` 的 `REPO_URL` 改为
`https://github.com/koyomaic/business_advisor.git`（`relay-dev` 同步 `git remote set-url`），恢复后改回。
已配 HTTPS 的存量机器不受影响（boot.env 每机一份，provision 不覆盖）。

**旧机迁移（推荐先做）**：把旧机 `/mnt/vol-eltaah12/backup/relay-backup-*.tar.gz` 最新一份
放到新机同路径，再跑上面命令 —— 清单会自动用备份补齐 workspace 缺失文件
（relay.db 成员 token、relay.env 密钥、shared/ 技能与 dws 种子；`--keep-old-files` 绝不覆盖已有）。

安装后必须人工补的（清单会 WARN 提示，密钥不进 git）：

1. `/root/.config/opencode/opencode.jsonc` — 填两处 `apiKey`（占位符 `sk-REPLACE_ME`），然后 `systemctl restart relay`
2. 无备份包时：`relay.env` 是新生成的随机密钥，成员 token 需重新发放；`relay.db` 为空库
3. 可选：`/root/.claude-proxy/config.json` 填 baseURL/apiKey/model
4. 可选：workspace `opencode.json`（内网 MCP 地址）从旧机拷贝
5. 可选：本机私有检查写 `/opt/team/relay-boot/provision-local.sh`（可执行，参数=模式；
   适合放内网 MCP 连通性等不进 git 的检查，失败只 WARN；PG 连通性已是原生清单项）

## 清单项目（顺序执行；项数随组件与主机角色浮动，以运行输出的 `[NN]` 编号与末尾 `PASS=/WARN=/FAIL=` 汇总为准）

| # | 项目 | 缺失时动作 | 级别 |
|---|---|---|---|
| 01 | root+systemd | 中止 | 硬 |
| 02 | 目录布局（/mnt/vol-eltaah12/*、/opt/team/*） | mkdir -p | 硬 |
| 03-05 | git / curl·tar·openssl / sqlite3 | apt/dnf 安装 | 硬 |
| 06-07 | python3≥3.10+venv / node≥20+npm | apt/dnf（node 走 NodeSource 22.x） | 硬 |
| 08-09 | opencode-ai≥1.18.29（sort -V 比较，新版不降级）/ dingtalk-workspace-cli@1.0.61 | npm -g 安装；opencode 装后验证 --version，npm 跳过 postinstall 时手动补跑 | 硬 |
| 10 | 资源树（release/现役树，否则临时 clone） | git clone | 硬 |
| 11 | venv+依赖（fastapi/uvicorn/httpx/pytest/psycopg2 可导入） | venv + pip -r requirements.txt（阿里云镜像） | 硬 |
| 12-13 | boot.env / boot.sh | 生成标准布局 / 从资源树同步(700) | 硬 |
| 14-18 | relay.service、relay-backup.service/timer、relay-boot.service/timer | 模板渲染同步；relay.service 变更后重启+健康检查，不健康自动回退旧单元 | 硬 |
| 19 | relay.env | 备份恢复 → 随机生成 | 软(WARN) |
| 20 | 数据库：SQLite relay.db 或 PG 连通实测（按 relay.env RELAY_DB 判定） | SQLite：备份恢复→提示首启自建；PG：不可达=FAIL 预警 | SQLite 软 / PG 硬 |
| 20b | pg_dump 客户端（仅 PG 模式，备份必需） | apt/dnf 装 postgresql-client | 硬（FAIL 预警） |
| 21 | dws 登录态种子 | 提示拷贝/登录 | 软 |
| 22 | opencode.jsonc | 落模板 → 提示填 apiKey | 软 |
| 23-24 | workspace AGENTS.md / opencode.json | 仓库模板安装 / 提示拷贝 | 软 |
| 24b | 基础技能：仅仓库 `skills/` 内人为标记入库的（机队共享，git 为真源），逐文件比对安装位同名技能（安装位由技能内 `.dest` 指定：钉钉套件→`/root/.agents/skills`、dws→`/opt/team/skills`，缺省 `shared/skills/`） | 缺失/漂移 → 本地旧版备份 `*.bak-provision-*` 后同步仓库版；未入库的本地/实验技能不碰 | 软 |
| 24c | opencode 全局语境 `/root/.config/opencode/AGENTS.md`（模板 `boot/opencode-global-AGENTS.md`，git 为真源；所有会话默认加载 GitHub 项目指向等语境） | 缺失/漂移 → 备份后同步模板 | 软 |
| 24d | team-agent-relay 安装位 `ca.pem`（公开 CA 证书，不入 git；统一从 `RELAY_CA_SRC`（缺省 `127.0.0.1:8788`，即所在服务器本机 TLS 链）TLS 链提取，自签校验后写入） | 缺失/漂移 → 备份后提取写入；源不可达且已有现存 → 沿用 | 软 |
| 24e | store 台账 CLI（`boot/store/store`，业务存储统一索引）：装 `/opt/team/store/store` + 软链 `/usr/local/bin/store`；建目录骨架与 `index/` 配置（纯文件系统、幂等）；`store ping` 探活 DB 与 5 张 `store_*` 表。**建表（`store init`）属有副作用操作，install 不自动执行**，首次需人工跑 | CLI 同步 / 骨架自建 / 探活：缺表 WARN、**DB 不可达 FAIL（触发钉钉预警）** | 软（DB 不可达时硬） |
| 25-26 | claude-proxy / relay-tls-proxy | 缺则装（tls 仅证书存在时） | 软 |
| 27 | 首装引导（无现役 release → boot.sh boot 完整部署） | 自动 | 硬 |
| 28 | relay active+healthy | enable/start/restart | 硬 |
| 29 | 两个 timer enabled | enable --now | 硬 |
| 29b | GitHub 访问就绪：SSH（默认）→ config `accept-new` 固化、无密钥自动生成、认证实测；HTTPS（备用）→ 查 `/root/.git-credentials` | config/密钥自愈；认证失败 WARN 附公钥与 Deploy keys 登记指引 | 软 |
| 30 | GitHub 远端可达且 remote==current | 仅报告 | 软 |
| 31 | dws 种子登录态实测（contact user me） | 仅报告 | 软 |
| 31b | relay-avatar（`relay-avatar/`，双入口统一分身网关，im-bridge 演进替代）：代码装 `/opt/team/relay-avatar`、状态在 `workspace/avatar/`、unit 渲染安装；首装自动迁移 im-bridge 状态（`app/migrate.py` 幂等：bridge.env→`shared/secrets/relay-avatar.env` 补随机看板 token、config.json 名单→policy.toml、state/rounds/events 拷贝、integrity 基线）；**默认不启用**，`touch /etc/relay-avatar.enabled` 后 install 轮自动 enable --now；marker 在但拉不起/心跳不新鲜=FAIL，看板 :8791 不可达=WARN | 安装/迁移/按 marker 启停 | 软 |
| 31c | im-bridge 退役：旧 unit/marker 回收（代码 `/opt/team/relay-im-bridge` 与状态 `workspace/im-bridge/` 留痕不删） | disable+rm unit/marker | 软 |
| 32 | provision-local.sh 本机附加检查 | 执行 hook | 软 |

硬项失败：install 模式立即中止（后续项依赖它），退出码 1；软项失败只 WARN，退出码不受影响。
20/20b 的 PG 失败记 FAIL（退出码 1 → 钉钉预警）但不中止后续项。

## 数据库后端（SQLite / PostgreSQL）

缺省 SQLite（`$RELAY_WORKSPACE/relay.db`）。切换 PostgreSQL：本机 `relay.env`（不进 git）设

```bash
RELAY_DB=postgresql://USER:PASSWORD@HOST:5432/DBNAME   # 密码 URL 编码：@ → %40，+ → %2B
```

- `app/db.py` 按 URL 前缀自动识别后端，表结构首启自建，断线自动重连重试
- 存量数据一次性迁移（切换前跑，行数自动核对）：
  `.venv/bin/python relay-service/scripts/migrate_sqlite_to_pg.py <sqlite路径> <pg_url> [--force]`
- 切换后 `systemctl restart relay`；清单第 20 项实测 PG 连通性，20b 自动装 pg_dump
- 备份自动切 `pg_dump -Fc`（包内 `relay.pg.dump`，历史 relay.db 若存在一并归档）；
  灾难恢复：`pg_restore -d <pg_url> --clean relay.pg.dump`
- PG 后端测试：`RELAY_TEST_PG_URL=<pg_url> pytest -m pg`（服务器上建临时库，测完即删；
  部署门禁 `-m "not integration and not pg"` 不依赖外部库）
- 注意：多台机器指向同一 PG 库会共享成员与任务队列（调度器会互相抢任务）；多机部署请分库

## store 台账（业务存储统一索引）

三层架构：**磁盘**为文件真身、**PostgreSQL**（relay 同库 `public` schema、`store_` 前缀）为权威索引与血缘、`index/manifest.jsonl` 为 PG 导出的降级快照。CLI 真源 `boot/store/store`，provision 24e 装到 `/opt/team/store/store` 并软链 `/usr/local/bin/store`。

**首次初始化（人工，provision 不自动做）**：

```bash
store init      # 建 pg_trgm 扩展 + 5 张 store_* 表 + 目录骨架 + 域/发送人种子
store ping      # 探活：DB 可连且 5 张表在（缺表退出码 1）
```

`init` 会建表，属有副作用操作，所以 install 模式只同步 CLI、建目录骨架、跑 `ping` 探活；表缺失时 WARN 并提示人工执行。需要 `RELAY_DB` 指向的库有建表权限（`pg_trgm` 需 superuser 或对该库有 CREATE）。

**任务侧连库（最小权限角色）**：任务环境拿不到 `RELAY_DB`（`executor._scrubbed_environ` 滤掉 `RELAY_*` 前缀），也读不到 `relay.env`（`relay.service` 的 `InaccessiblePaths` 物理屏蔽），所以 `store init` 会另建 PG 角色并把连接串写进 `index/store-task.env`（644，任务可读）。CLI 的解析顺序是 `RELAY_DB` env → `relay.env` → `STORE_DB` env → `index/store-task.env`，宿主自动走全权限、任务自动降到最小权限，同一条命令两边都能用。

角色名**每机私有**：`store_task_<machine-id 前 8 位>`（`STORE_TASK_ROLE` env 可覆盖）。为什么不能全机队共用一个 `store_task`：库是共享的（8 台机器同连 `10.189.21.235`），而 `index/store-task.env` 是每机本地文件（`/mnt/vol-eltaah12` 为本地 ext4，非网络挂载）——共享角色 + 每机密码文件 + 自动轮换，会让任何一台的轮换立即打断其余 7 台，各自 `provision` 检测到 `ping` rc=3 再轮换，夜夜互相失效且每轮都报 PASS（2026-09-25 轮换风暴）。轮换共享秘密必然使其他持有者失效，自愈只有在凭据本机私有时才安全。旧的共享 `store_task` 角色保留不删，供尚未升级的机器继续使用。

授权范围（`GRANT_TMPL` 常量，`{r}` 为角色名占位）：

| 对象 | 权限 |
|---|---|
| `store_files` / `store_refs` / `store_wiki_sync` | SELECT, INSERT, UPDATE |
| `store_senders` / `store_domains` | SELECT |
| `tasks` | **仅 `id`, `status` 两列**（列级授权，供 `store q --task` 的 JOIN） |
| `users` / `devices` | **REVOKE ALL**（成员 token 与设备密钥不可见） |
| DDL / DELETE / TRUNCATE | 无 |

另设 `statement_timeout=60s` 防跑飞。密码默认不轮换（避免打断在途任务），需要时 `store init --rotate-task-role`。台账允许任务写，但 `store doctor` 以磁盘为准对账，篡改可被发现。

**探活与预警**：`store ping` 退出码 `0`=正常、`1`=连上了但缺表（首次未 init，provision 记 WARN）、`2`=**DB 不可达**（relay 依赖同一个库，通常意味着服务已瘫，provision 记 FAIL → `boot.sh` 走 `notify_failure` 钉钉预警 `ALERT_TO`）。

**日常用法**（约定详见 workspace `AGENTS.md`「共享区约定」）：

```bash
store today                                  # 今天该往哪写
store scan [--src DIR] [--kind K] [--prefix P] [--root-name store|workspace]
store add <path> --kind data --sender 20102 --task 67 --topic 门店日销
store q --kind report --domain 会议纪要 --since 2026-09-01
store q --task 67                            # JOIN relay tasks，一个任务的输入+输出
store lineage <id> / store link <src> <dst>  # 血缘
store dup / store doctor / store export      # 去重 / 对账体检 / 导出快照
store sync tags|scan|push|verify|backfill    # jingboWiki 同步
```

- **存储根＝relay 的 `shared_dir`**（`STORE_ROOT`，缺省 `$WORKSPACE/shared`）。同址是刻意的：relay 的 `_snap` 用 `os.walk` 默认不跟随符号链接，若把 store 放别处再用软链做兼容，`knowledge`/`report` 会整个从基线快照消失、静默破坏冲突检测。改 `STORE_ROOT` 必须同步改 `RELAY_SHARED`。
- 扫描按相对路径前缀排除非台账区（`secrets`/`skills`/`mcp`/`memory`/`tmp`/`venv*`/`models`/`index`/`work`/`tools/dingtalk-skills`），否则 `secrets/` 下数千文件全成 orphan。
- `scan` 的人工标注粘滞：`stub`/`junk`/`archived`/`done` 等判定与 `domain`/`topic`/`sender_id` 等人工字段不会被重扫清空，只有 `legacy`/`indexed`/`raw` 三个自动态会刷新。
- 多机部署若分库（见上节），store 台账也随之分库；跨机汇总需另建视图，`manifest.jsonl` 可作为交换格式。

## 基础技能（仓库 skills/，机队共享）

技能分两类：

- **基础技能**：经人特别标记入库到仓库 `skills/` 目录的（入库即标记），机队共享、git 为真源。
  provision 清单 24b 逐文件比对并自愈：本机缺失/漂移 → 本地旧版备份 `*.bak-provision-*` 后同步仓库版。
- **本地/实验技能**：未入库的，git 与 provision 均不碰，各机自由增改。

**安装位（.dest）**：基础技能同步到哪个父目录，由技能目录内 `.dest` 文件（单行绝对路径）决定；
无 `.dest` → 默认 `$WORKSPACE/shared/skills`。`.dest` 是元数据，不下发到目标、不参与比对。

- 钉钉套件 `dingtalk-aisearch/aitable/calendar/chat/contact/doc/drive/event/mail/minutes/misc/shared/todo/wiki` → `/root/.agents/skills`
- `dws` → `/opt/team/skills`
- `jingbowiki-api`、`dingtalk-chat-digest`、`dingtalk-group-remark`、`dingtalk-send`、`dingtalk-toolbar` → 默认 shared/skills

标记新基础技能（管理员操作）：把技能目录拷入仓库 `skills/`（剔除 `*.bak*`、zip、`__pycache__`、运行时状态文件；
**严禁入库任何密钥**，凭据一律放 `shared/secrets/`）→ 非默认安装位则加 `.dest` 文件 → commit + push；各机夜间轮次自动同步。
修改基础技能：改仓库（或改本机后提交回仓库）；只改本机不提交，夜间自愈会以仓库版覆盖（有备份）。

## opencode 全局语境（~/.config/opencode/AGENTS.md）

机队每台机的 `/root/.config/opencode/AGENTS.md` 由模板 `boot/opencode-global-AGENTS.md` 安装（provision 清单 24c，git 为真源），
所有 opencode 会话（任意工作目录）启动即默认加载：GitHub 项目指向（"检查GitHub项目更新"＝business_advisor/relay-dev）、
部署/门禁/健康检查入口、关键路径、知识源优先级、铁律。改语境＝改模板提交，夜间轮次自动同步（旧版备份 `*.bak-provision-*`）。
已运行的会话需重启 opencode 才生效。

## 两种模式

```bash
provision.sh install   # 默认。幂等自愈，可反复跑；新装 + 每日守护轮次用
provision.sh check     # 只读体检，不做任何改动；缺失硬项=FAIL、配置漂移=WARN
```

## 每日自动执行（已集成 boot.sh）

`boot.sh boot/update`（timer 每日 01:00 + 开机 2min）在部署/守护完成后自动执行
`provision.sh install`：健康机上全部 PASS（约 10-20 秒纯检测）；发现缺失自动补装；
清单 FAIL → 钉钉直聊预警 `ALERT_TO`（boot.env 配置，默认王洪彬）。

## 验收（新装完成后）

```bash
curl -s 127.0.0.1:8787/health          # ok:true, version=远端 sha
/opt/team/relay-boot/boot.sh status    # remote=current=health 三者一致, unit active
/opt/team/relay-boot/boot.sh update    # 守护 OK + 安装检测清单 PASS
systemctl list-timers relay-boot.timer relay-backup.timer --no-pager
systemctl start relay-backup           # /mnt/vol-eltaah12/backup/ 出新包
bash /opt/team/relay-deploy/current/../boot/provision.sh check   # 全 PASS（current 指向 relay-service，boot 在上一层）
store ping                               # ok:true，5 张 store_* 表在（缺表则先 store init）
store doctor                             # ok:true，problems=0（台账↔磁盘零漂移、零命名违规、零重复）
```

## 切流与旧机收尾

1. 成员改指向新机 `IP:8787`（HTTPS 主机另有 :8788）
2. 旧机 `systemctl disable --now relay relay-boot.timer relay-backup.timer claude-proxy`，数据保留 ≥1 周
3. 旧机 bootloader 备份目录（`/opt/team/relay-boot.bak-*`）含历史 PAT，迁移后销毁
