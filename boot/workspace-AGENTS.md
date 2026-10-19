# 工作区约定（服务器任务 agent 必读）

## 动手前（共享知识检索顺序）

1. **优先权威源：jingboWiki**——用 `jingbowiki-api` 技能检索（快速检索 `POST /knowledge-search`；凭据 `shared/secrets/jingbowiki.env`，健康检查 `http://10.200.3.235:18082/health`）。团队知识以 jingboWiki 为准。
2. 本地兜底：`/mnt/vol-eltaah12/workspace/shared/knowledge/` 按 tag 域分目录（`口径方法论/` `组织制度/` `工具运维/` `外部资料/`）；jingboWiki 不可达或未命中时再查。找具体文件用 `store q`，不要递归 `find`。
3. 两处内容冲突时，以 jingboWiki 为准。有现成经验就用，不要重复摸索。

## 工具调用并行规约（硬性：直接决定任务耗时）

同一轮内可并行的工具调用**必须一次发出**，禁止逐个串行。

- **判定标准**：下一个调用的参数是否依赖上一个调用的返回值。**不依赖 → 必须并行**（同一轮发出多个 tool_use）；依赖 → 才允许分轮。
- **典型可并行**：多个独立文件读取／数据查询、多个 `store add` 登记、多个收件人的消息发送、多个会话的送达校验、对同一份产物的多项独立校验（grep／字段核对／体积检查）。
- **典型不可并行**：读结果 → 据结果决定下一步；写文件 → 校验刚写的这个文件。
- **批量取数用「一条命令取全」**，不要「多条命令各取一项」：反例是 4 轮各跑一次 `python3 -c "json.load(f)['a']"`，正例是 1 轮跑一次 `python3` 一次性输出全部所需字段。
- **同一文件的多处修改**，若能一次确定全部内容 → 用 1 次 `Write` 全量写，不要 N 次 `Edit` 逐处填。

> 为什么单列这条：实测每轮模型往返开销基本恒定（1 个工具 39.9s／2 个工具 39.2s／3+ 个工具 51.2s），**多塞一个工具几乎不额外花钱**；而历史任务里 **79% 的轮次只调 1 个工具**。合并独立调用可省约 20% 墙钟；长文报告类任务曾因此撞死 1800s 超时上限而整份作废。

## 可用 CLI

- `store`：业务存储台账（查文件/登记/血缘/对账/jingboWiki 同步），见下文「共享区约定」
- `dws`：钉钉工作套件（发消息/查通讯录/日程/表格/文档/审批/待办），用法与坑位见 dws skill
- `python3`
- 常规 shell 工具（bash、curl 等）

## GitHub 项目（"检查GitHub项目更新"即指它）

- 仓库 `koyomaic/business_advisor`（main）＝中转服务 relay 唯一真源；本地 git 工作区 `/mnt/vol-eltaah12/finaagent/relay-dev`
- 检查更新：`cd /mnt/vol-eltaah12/finaagent/relay-dev && git fetch origin && git log --oneline HEAD..origin/main`（无输出＝已最新；有输出＝汇报新提交后等指示）
- 任务里默认只检查/汇报；pull、部署、push、重启服务需任务明确要求（部署走 `/opt/team/relay-boot/boot.sh`，机队文档 `relay-dev/boot/INSTALL.md`）

## 共享区约定（shared/ ＝ store 存储根）

`shared/` 既是团队共享区，也是业务存储根（store）。三类内容分开放：

| 目录 | 放什么 | 分层 |
| --- | --- | --- |
| `shared/data/` | 待加工的经营数据（入站原件） | `<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<senderId>/` |
| `shared/report/` | 大脑生成的报告（出站成品） | `<域>/<YYYY-MM 或 YYYY-Www>/` |
| `shared/knowledge/` `templates/` `tools/` `skills/` `mcp/` `memory/` `secrets/` | 非经营共享 | `knowledge/` 按 tag 域分：`口径方法论/` `组织制度/` `工具运维/` `外部资料/` |

另有 `shared/index/`（台账，CLI 维护，**不要手写**）、`shared/work/`（任务中间态，7 天清）、`shared/.archive/`（备份/去重/残留，**不要读也不要写**）。

### 命名约定（让路径可推导，不用先搜索再决定放哪）

```
data   : <YYYYMMDD>_<senderId>_<taskId>_<topic>_v<N>.<ext>
report : <YYYYMMDD>_<taskId>_<topic>[-<variant>]_v<N>.<ext>
例：20260921_20102_T0_能源局座谈会-录音2分片01_v1.m4a
    20260921_T0_能源局座谈会-录音2逐字稿-funasr_v1.md
```

- 日期一律 `YYYYMMDD`（目录 `YYYY-MM-DD` / `YYYY-MM` / `YYYY-Www`），**禁** `2026.9.20` 这类
- `taskId` 用 relay 任务号 `T57`，无关联写 `T0`；`senderId` 是钉钉 userId 纯数字，姓名不进路径
- `variant` 区分同一产出的不同实现（如 ASR 引擎 `funasr`/`whisper`），与 `vN`（迭代版次）正交
- `topic` 可用中文，禁空格（用 `-`）与 `# % &`；路径段一律 ASCII
- **禁同名覆盖**，改版就 `vN+1`

### 硬规则

- **不要静默覆盖他人文件**；改动已有共享文件前先保留原版本
- 备份/中间产物写 `shared/work/` 或 `.archive/`，**禁止**在正式目录留 `.bak-*`/`~`/`.tmp`/`旧版备份`
- store 防御：正式目录的 `.bak`/`.old`/`.tmp` 即使被 `store scan/add` 登记，也会强制标为 `archive/backup/skip`；`store sync` 会再次跳过备份/临时件，绝不上传 jingboWiki
- **禁止**同一内容出中英双份目录或双份文件（历史上 `20260921能源局座谈会/` 与 `20260921_energy_meeting/`、`jingbo-data-api-能力清单.md` 与 `-capability-list.md` 都是这么来的）；中文为准，需要英文名时在台账记 alias
- 任务产生的可复用经验写入 `shared/knowledge/<tag域>/` 对应主题目录
- **知识归档双写（硬性）**：凡归档进 `shared/knowledge/` 的知识文件（报告/文档/摘要/经验，无论任务产出还是用户上传），**必须在同一任务内经 `jingbowiki-api` 同步上传 jingboWiki**（权威源，全员可检索）。双写是归档动作的一部分，任务描述里"只改动 shared/knowledge/ 下文件""不要修改其他文件"之类范围限定**不豁免**本条；仅当任务明确写"不要上传 jingboWiki"时才跳过
- 上传**受理即成功**（不阻塞等解析，解析后台异步），在任务结果里报告 knowledge_id；需确认解析完成时用技能的 wait 子命令后查，不要长轮询
- 报告升格为知识的判定：**进 wiki**＝口径/方法论/能力清单/组织架构/纪要结论/策略矩阵；**不进**＝一次性数值报表、日报周报原始数据、`report/经营汇报/`（敏感）、自检与进度类过程文档。大文件原件（m4a/xlsx/docx）永不进 wiki 正文，只存结论并在台账用 refs 指回原件

### 台账 CLI（`store`）

文件真身在磁盘，权威索引在 PostgreSQL（relay 同库 `public` schema、`store_` 前缀）。**找文件先查台账，不要递归 `find`**：

```bash
store today                                   # 今天该往哪写（不用自己算日期）
store q --kind report --domain 会议纪要 --since 2026-09-01
store q --task 67                             # 一个任务的全部输入输出（JOIN relay tasks）
store q --path 调价                            # 存量文件 topic 为空，按路径子串查
store add <path> --kind data --sender 20102 --task 67 --topic 门店日销
store lineage <id>                            # 血缘：原始数据 → 报告 → 知识
store doctor                                  # 磁盘↔台账对账、命名违规、重复、缺口
store sync scan|push|verify|backfill          # jingboWiki 同步
```

落盘新文件后**必须** `store add`（或 `store scan`）登记一行，否则台账与磁盘漂移。

任务内 `store` 同样可用：它会自动降级到 `index/store-task.env` 里的**最小权限连接串**（PG 角色本机私有、形如 `store_task_<机器标识>`，只授 `store_*` 表的 SELECT/INSERT/UPDATE 与 `tasks` 的 `id`/`status` 两列；`users`/`devices`/`tasks.description` 一律拒绝，也不能 DELETE/CREATE/DROP）。所以**不需要也不允许**为了连库去翻 `relay.env` 或 `/proc/<pid>/environ`。若 `store ping` 报 DB 不可达，如实报告即可——那属需要人工介入的故障，不要自行绕过。

### 凭据边界（硬性）

`shared/secrets/`、`relay.env`、以及**任何进程的环境变量（`/proc/<pid>/environ`）**都不得读取、复制、外发或回显。任务需要数据库/接口能力时走已封装的 CLI（`store`、`dws`）或 relay 接口，**不要**为了拿连接串去翻服务进程环境。缺凭据就在结果里说明缺什么，由人处理。

> 为什么单列这条：任务 agent 与 relay 服务同为 root，`relay.env` 的 `600` 与 `/proc/<pid>/environ` 的 `400` 对 agent 并无实际约束力，只能靠规则约束。

## 定时任务／计划（硬性：出标记由分身代办，不要自找凭据）

- **任务环境里没有 relay 凭据**（`RELAY_*` 已被剔除）。翻 `env`、翻 `/opt/team/`、grep relay 源码、探测 relay 路由（`/plans`、`/openapi.json`、`/dashboard/*`）**都拿不到**，只会白烧时限（任务超时上限 1800 秒）。缺凭据不是"做不了"，而是"该出标记让分身代办"。
- **本机只有一层定时任务**：relay 侧的业务计划（`/plans`，由 root 侧 `relay-plan.timer` 每分钟扫库触发）。**没有 local cron 层**——本会话工具集只有 `Read`/`Glob`/`Grep`/`LS`/`Task`/`Bash`，**没有任何"列定时任务"的工具**。不要凭空调用想象中的工具名，也不要因为"调不到"就去翻凭据。
- **查**「现在有几个定时任务／我的计划」→ 唯一入口是在结果正文出 ```plan``` 块 `{"op": "list"}`，由分身带凭据读取后回传清单。这是只读操作，零副作用。
- **建／删／停／启**计划 → 同样在**结果正文**出 ```plan``` 围栏块，由分身带凭据执行：

  ```plan
  {"op": "create", "name": "每日经营日报", "on_calendar": "*-*-* 07:35:00", "description": "到点要执行的任务描述，写实义内容"}
  ```

  `op` ∈ `create`/`delete`/`disable`/`enable`/`list`；`delete`/`disable`/`enable` 只需 `name`（**删除/停用必须给完整计划名**，子串不生效）。`on_calendar` 是 systemd OnCalendar 表达式（每日 07:35 即 `*-*-* 07:35:00`，可用 `systemd-analyze calendar "<表达式>"` 自检）。可选 `priority`(`normal`|`urgent`)、`read_only`(bool)。
- **`name` 与 `description` 必须是实义值**，不要把上面示例里的 `<计划名>`／`<到点要执行的任务描述>` 原样回显（整段就是占位符会被拒建）。但描述里**嵌日期/期次模板变量是允许且常见的**，如 `<YYYYMMDD>`、`<YYYY-MM>`、`<当日期次>`——生产在跑的计划都这么写，不会被拦。
- **投递目标由分身自动绑定**：用户在某个群/单聊里创建计划时，分身会把当前会话写入计划的 `action.delivery`；agent 不要在 ```plan``` 标记里自填 `delivery`/`to`/收件人。计划任务到点完成后，分身会按绑定目标代投结果正文与 ```send_file``` 文件。
- 计划创建受权限门禁（仅团队成员）与数量上限（每人 20 个）约束；核心业务计划（清单里带 🔒）仅超管可删改。被拒时分身会把原因原样告知用户，**不要换写法重试绕过**。
- 为什么单列这一节：2026-09-28 任务 #278 为「现在有几个定时任务？」这一句话烧掉 5.5 分钟 / 32 次 Bash 去刨 relay 路由、源码与凭据；#281 的「建每日 7:35 定时任务」因不知道有代建通道，最终回报「当前凭据下我建不了，需人工执行」——而发起人权限本来就够，机制也早就部署了。次日 #288 又照旧文档**幻觉调用**了一个并不存在的"列定时任务"工具并声称"实据＝无任何定时任务"（结论碰巧对、依据是编的），故本节明确写死"只有一层、唯一入口是 plan 标记"。

## 共享知识（取消隔离，个人上传即全员可用）

- `shared/skills/`：团队技能，已自动加载，按技能名直接用。其中**基础技能**（入库到 git `skills/` 的，安装位由技能内 `.dest` 指定，缺省 shared/skills）以 git 为真源：改动须提交回仓库，否则夜间 provision 以仓库版覆盖本机改动（旧版备份 `*.bak-provision-*`）；未入库的本地/实验技能不受限
- `shared/mcp/`：团队 MCP 工具服务器，已自动接入，把它们的工具当普通工具调用
- `shared/memory/`：团队记忆，已自动并入本 AGENTS.md
- 三类内容任何人上传后，下一个任务即全员生效，无需重启
- dws 凭证种子（`shared/secrets/dws-cli-seed/`）也**自动同步**进每个任务的 home，
  直接用 dws 即可，不用手动复制；报"登录态过期"时才走设备码流程

## 任务工作目录

你的工作目录 `tasks/<id>` 是一次性任务区，临时文件放这里即可，任务结束无需清理。

## 中转服务数据库

- 存储后端以 relay.env 的 `RELAY_DB` 为准：postgresql:// URL → PostgreSQL；缺省 → SQLite（workspace/relay.db）。
- 实时查证：`curl http://127.0.0.1:8787/health` 的 `db` 字段返回真实后端（postgresql/sqlite）。
- 若本机已切 PG，旧 `relay.db` 只是历史快照（存档、不再读写）——**不要**凭它存在就回答现役库是 SQLite。

## 临时文件清理（无需确认）

- **自己**在本次会话中创建的临时产物——如 `/tmp` 下的打包 zip、解压目录、中间文件、测试残留——直接删除即可，**不要向用户逐个征求确认**（用户要求：减少此类打扰）。
- 边界：仅限"本次会话自己生成的临时物"。用户的真实数据（workspace 业务数据、shared/ 共享区、他人文件、任务产出）不在免确认范围内，删除前必须说明并确认。

## 命令拦截边界（了解即可，正常干活别撞）

- `/tmp`、`/var/tmp` 下的清理（含 `rm -rf`）直接放行；systemctl、防火墙、cron、用户管理等均不拦。
- 会被拦的核心项：`mkfs`、写裸盘（`of=/dev/磁盘`、`> /dev/磁盘`）、`shutdown/reboot/poweroff/halt`、`kill -9 1`、`chmod -R 777 /` 或 `~`、**非 /tmp 路径的递归 rm**。
- 若任务确需执行会被拦的命令，如实说明并让用户在监控页审批，不要变着写法绕。

## 软件安装（硬性：沙箱内系统包安装必然失败）

- 任务沙箱由 systemd `ProtectSystem=strict` 托管，**`/usr`、`/etc`、`/boot` 对你只读**。`apt-get`／`apt`／`dnf`／`yum`／`rpm` 等系统包安装**一定失败**（典型报错：写 `/var/cache/apt/archives/partial/` 时 `Permission denied`）。**不要尝试、不要重试、不要 `sudo`**——宿主可写不代表沙箱可写。
- **明确禁止安装 LibreOffice／`soffice`**：PPT→PDF 的视觉质检这条路走不通。需要质检时用**程序化质检**（如 python-pptx 逐形状边界／越界／文本溢出估算 ＋ 内容断言），并在交付件里如实写明"视觉质检不可用及其原因"。
- Python 依赖只用 **`shared/venv`** 里已装好的（`python-pptx`、`pandas` 等）。缺库先查 `shared/venv/lib/python3.12/site-packages/` 确认，不要凭印象判定"未安装"。
- **任何安装类命令（含 `pip install`）超过 1 分钟仍未完成，立即取消**，改走降级方案并如实记录，不要干等。
- 为什么单列这一节：任务有超时上限（默认 1800 秒），安装类弯路会直接吃掉时限，导致**任务超时失败、已建成的交付件作废**。2026-09-24 任务 #203 即在 PPT 已构建并通过质检后，因反复尝试安装 LibreOffice 而在最后一步超时。
