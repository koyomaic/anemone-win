---
name: ludashi-usage-daily-report
version: 1.0.0
description: 生成《鲁大师用户日度使用分析报告》。当用户说"出昨天的使用分析报告""盘点今天谁用了鲁大师""日度使用分析""用户活跃日报""重点用户活跃情况""谁在用分身""使用量统计""生成 usage 日报"时使用。负责按固定模板盘点全渠道会话/人工提问/重点用户活跃/交付成功率/静默追踪，数据取自 relay 现役 PG 与本机 audit·events·rounds 日志。不做周报（走周报流程）、不做单任务查询（走 relay 台账）。
---

# 鲁大师用户日度使用分析报告

按固定模板盘点**某一天**鲁大师的使用情况，产出可交给领导的 Markdown 报告。

> **报告抬头按机取分身名（2026-10-15）**：本技能机队共享，但报告**标题/摘要/文件名**里的分身名
> **不写死鲁大师**——由 `scripts/self_name.py` 按机解析：本机（bot 自称鲁大师）显示「鲁大师」，
> 其他机显示各自 `policy.toml` 的 `avatar_name`（如智微办）。宿主机 `usage_collect.py` 把解析结果写进
> `facts.display_name`，渲染端只读它（沙箱读不到 identity/policy 也不会错）。下文其余「鲁大师」多指
> **relay 全机队共用 bot token 的主体名**（PG 里 `tasks.user`/`plans.owner_name` 的字面值），属事实口径，不随机器变。

## 铁律（先读，违反即报告失真）

1. **数字只能来自 `usage_collect.py` 的 facts.json**，不许自己心算、不许从日志里"目测"。
2. **无数据的维度打 `[缺档]` 标签，绝不编造**。模板里有 4 项本机根本没有数据源（见下），照实标。
3. **`{{待填：…}}` 标记必须由你补齐**后才能交付；补不齐就保留标记并说明原因，不要用套话填。
4. **第 5 节每条「发现」必须带当天数据支撑**，不许写"建议加强用户运营"这类空话。
5. 报告落盘后**必须**登记台账（`store add`），否则算未完成。
6. **不留存用户任务内容**（2026-10-12 用户指示）。报告要落 `shared/report/` 并进台账
   （机队可见、长期留存），而提问原文与交付正文里含预算数字、期货策略、头寸限额等
   经营敏感信息，不该随使用度量一起持久化。采集器已把原文压成**需求类型类别**，
   facts.json 与报告文件里都不出现原文——**不要想办法把原文捞回来**
   （`--keep-content` 只供排障，开了就别外发 facts）。
7. **推送一律「摘要＋文件」**：摘要用 `render_report.py --summary` 生成（人员使用置顶，
    ~400 字符），完整件作附件发。不要把整份报告塞进消息正文。
8. **收件人不许写死**（2026-10-12 用户指示：机队各机推送对象不同，并不都是王洪彬和崔成东）。
   一律用 `recipients.py` 从本机配置解析，并**逐个收件人各发一遍**：

   ```bash
   python3 $S/recipients.py --kind report    # 日报收件人（每行一个姓名）
   python3 $S/recipients.py --kind alert     # 失败预警收件人
   ```

   它读**本机配置区**的 `avatar/usage-report-recipients.json`（与 `policy.toml` 同级），
   查找顺序：`$USAGE_RECIPIENTS` → `avatar/usage-report-recipients.json` →
   `<技能目录>/config/recipients.local.json`（开发/兼容）。

   > ⚠ 主位置刻意**不放技能目录内**：技能目录是 provision 21b 的同步目标，一次
   > 「rm -rf 安装位 + 重拷」就会抹掉本机配置 → 解析器静默回退成 policy 派生值，
   > 收件人从「王洪彬、崔成东」变成「崔成东、毕倩」，**报告发错人且无人察觉**（实测踩过）。
   > `avatar/` 永不被同步，故安全。已验证全量重同步后收件人不变。

   配置缺失时自动从本机 `policy.toml` 的 `owner_userid`＋`superadmin` 派生并写出该文件，
   所以新机器零配置也能跑，且推给的是**本机**负责人。

   | 机器 | policy 派生结果 | 本机实际配置 |
   |---|---|---|
    | 本机 i-ll733rsj（owner=20102） | 崔成东、毕倩 | **王洪彬、崔成东、邹雄、舒瀚**（用户指定） |
   | 机队模板（owner=18434） | 王洪彬 | 各机自定 |

   > 反例：`dingtalk-chat-digest/local.config.json` 把本机路径提交进了 git，
   > 会被 provision 推给其他机器。本技能的收件人配置刻意不入库就是为了避开这个坑。

    `boot/usage-collect.sh` 的失败预警同样走 `recipients.py --kind alert`，
    不再写死人名；`USAGE_ALERT_USER` 环境变量可临时覆盖（排障时定向发给自己）。

9. **完整件按 2026-10-16 精简版口径渲染**：用户明确要求「把人的使用情况放到最上面；
   一些汇总可以简单点；两页长度能讲清楚，就不需要更多了」。`render_report.py` 已固化为：
   ① 人员使用情况（活跃人员表＋当日无活动重点用户表）置顶；② 核心汇总用 5 行要点；
   ③ 异常与建议最多 5 条数据发现＋1 处待填建议；④ 口径与缺口压缩为 2–3 行。
   **不再输出逐会话卡片、渠道长表、cron 明细表和附录长表**；如需排障，查 facts.json
   或 relay 台账，不要把完整件重新拉长。

## 每日自动化：两段式（宿主机采集 → 沙箱渲染）

**为什么必须拆两段**：本地任务沙箱的 `local_sandbox_hide` 整体隐藏 `relay.env`
（policy.toml 明写「凭据在沙箱内**不可见**，不是可见但禁读」，是沙箱出网不限后的剩余防线）。
所以沙箱内的计划任务读不到 `RELAY_DB` → 采集器必然降级 `logs_only` → 日报只剩入站消息数，
任务级/用户级全空。**直接在计划里跑采集会每天静默产出废报告。**

**参考编排（新能源机实例，2026-10-16 起生效）**——三段各自按机开关，其他机未启用即无此链路，
技能本体（采集器/渲染器）仍可手工单跑：

| 段 | 触发 | 跑什么 | 凭据 | 产物 |
|---|---|---|---|---|
| ① 采集 | 宿主机 systemd `ludashi-usage-collect.timer`，每日 **07:45**（drop-in 覆盖，含 60s 抖动）；**机队默认停用**，仅 `boot.env` 设 `USAGE_REPORT_TIMER=1` 的机器启用 | `boot/usage-collect.sh` → `usage_collect.py` | 需要 `RELAY_DB`（宿主机可读） | `shared/tmp/usage/facts-<日>.json`（含 `display_name`） |
| ② 渲染落盘 | 本机 relay 计划（新能源机为 **#6「ludashi-usage-daily-report-group」**，每日 **08:00**；计划号/时刻按机自定） | `render_report.py` ＋ 补叙述 ＋ 落盘 ＋ 台账（**不做任何 dws 发送**） | 不需要（纯标准库，系统 python3 即可） | `shared/report/自检报告/<YYYY-MM>/…md` |
| ③ 群推送 | 宿主机 systemd `usage-report-send.timer`，每日 **08:30:00** 准点（**本机私有单元，不在仓库 `boot/`**，其他机无） | `usage-report-send.sh`：轮询落盘报告 → 生成摘要 → 发钉钉群 | 宿主机 dws 凭据（不经沙箱门禁） | 群消息（摘要＋报告文件） |

> ②③ 拆分原因：沙箱内**所有** dws 发送命令被门禁拦成 `pending_approval`（群发必拦），
> 故渲染留在沙箱、发送移到宿主机。计划 action 必须明令禁止 agent 自行发送。
>
> 报告抬头与文件名的 `<分身名>` 一律取 `facts.display_name`（`--print-name`）；无 facts 时回落
> `self_name.py` 按机解析（bot 自称鲁大师的机器→鲁大师，其余→各自 `policy.toml` 的 `avatar_name`）。
> **严禁在抬头或文件名写死任何分身名。**

衔接点 `shared/tmp/usage/` 的可达性已实测：

- **不在** `local_sandbox_hide` → 沙箱**可读**（宿主机写、沙箱读，只读正好）
- `shared/report`、`shared/skills` **在** `local_sandbox_rw` 16 项里 → 沙箱能读脚本、能写报告
- `render_report.py` 不 import psycopg2 → 沙箱内系统 `python3` 直接可跑（已实测）

08:30 这个点是刻意错开 08:00 的 `daily-report.timer`（同样查 `RELAY_DB`，避免并发争库）。

### 机队默认**不开启**（2026-10-12 用户指示）

其他机队成员默认先不做定时。provision 19d 照样安装两个单元（版本受控、便于一键开启），
但只有本机 `boot.env` 显式设 `USAGE_REPORT_TIMER=1` 才 `enable`；设回 0（或不设）时
下一轮 provision 会 `disable`，使机队收敛到未开启。

| flag | 当前状态 | install 模式动作 |
|---|---|---|
| `1` | 已启用 | 无动作 |
| `1` | 未启用 | `enable --now` |
| `0`/未设 | 已启用 | `disable --now`（收敛到关） |
| `0`/未设 | 未启用 | 无动作 |

八种组合（含 check 模式）已用 mock systemctl 逐一验证。本机 `i-ll733rsj` 是首发机，
`boot.env` 里设了 `USAGE_REPORT_TIMER=1`。

**别的机器要开启，除了设 flag 还需两件事**（否则日报出不来或发错人）：

1. **自建 relay 渲染计划**——各机 relay 库是**独立实例**，计划不共享，本机建的计划 #12
   在其他机器上不存在。description 可直接抄计划 #12（`SELECT action FROM plans WHERE id=12`）。
2. **配本机收件人** `<workspace>/avatar/usage-report-recipients.json`。不配则
   `recipients.py` 从该机 `policy.toml` 的 `owner_userid`＋`superadmin` 派生，
   推给的是**那台机器的业务负责人**——他未必想要这份日报，这也是默认不开的原因之一。

### 计划 #12 的失败处置

计划描述里写死了：**facts 缺失或 `source != pg_authoritative` 时，不许自己跑采集补救**，
改为 DM 王洪彬一句「预采集缺失，当日日报不可用，查 `/var/log/ludashi-usage-collect.log`」后结束。
理由是沙箱内跑采集只会产出降级件，反而把「预采集坏了」这个真故障盖住。

宿主机侧 `usage-collect.sh` 自己也会预警：采集退出非 0、产物为空、或 `source` 不是
`pg_authoritative` → 钉钉 DM 王洪彬（沿用 `daily-report.sh` 的惯例）。

### 相关文件（均在 git，provision 同步）

| 路径 | 安装位 | provision 步骤 |
|---|---|---|
| `boot/usage-collect.sh` | `/opt/team/relay-boot/`（chmod 700） | 19d |
| `boot/ludashi-usage-collect.service.template` | `/etc/systemd/system/…service`（render `__WORKSPACE__`/`__VENV__`） | 19d |
| `boot/ludashi-usage-collect.timer` | `/etc/systemd/system/…timer` | 19d |
| `boot.env` 的 `USAGE_REPORT_TIMER` | 每机一份、不入库、存在即不覆盖 | 12（生成）／19d（消费） |
| 本技能全部文件 | `shared/skills/ludashi-usage-daily-report/` | 21b |
| `avatar/usage-report-recipients.json` | 本机收件人，**不在技能目录内**（见铁律 8） | 不同步 |

`ludashi-usage-collect.service` 刻意**不加** `InaccessiblePaths`——它的全部意义就是能读
`relay.env`，与沙箱内任务单元的边界正好相反。凭据只存在于进程环境，不落产物、不打日志。
产物 `facts-*.json` 会落在沙箱可读路径，故已扫描确认**不含连接串/令牌/密码/邮箱**
（openId 会出现，但 policy.toml 本就对沙箱可见，非新增暴露）。

### 与既有 daily-report.sh 的关系（不重复）

本机 08:00 已有 `daily-report.sh` + `daily-report.timer`，发钉钉群「G·鲁大师训练营」，
内容是**任务战果/练习明细/活跃成员/新增共享资产**。本技能是**用户日度使用分析**
（渠道分布/重点用户活跃/静默追踪/交付质量/逐会话卡片），受众与口径都不同，互补不替代。

它的日志可用作**独立交叉验证**——`/var/log/relay-daily-report.log` 的「昨日任务=N」
应与本技能 facts 的 `task_rows` 完全相等。实测 10-09/10-10/10-11 三天分别为
29/53/36，两边逐日吻合。对不上就说明有一边口径漂了。

> ⚠ `daily-report.sh` **不在 git 里**（provision 完全不管它），只存在于本机
> `/opt/team/relay-boot/`，属未纳管漂移：不会分发到其他机器，重装机器会丢。
> 本技能的三段（脚本＋两个单元）都已入库并由 provision 19d 同步。

## 跑法

```bash
PY=/mnt/vol-eltaah12/finaagent/.venv/bin/python     # 必须用这个 venv，系统 python 无 psycopg2
S=/mnt/vol-eltaah12/finaagent/relay-dev/skills/ludashi-usage-daily-report/scripts
D=2026-10-11                                        # 报告日；不传默认昨天

# ⓪ 换机器或人员变动后才需要：重派生本机名单（平时跳过）
$PY $S/build_roster.py --dry-run

# ① 采集 → ② 渲染
$PY $S/usage_collect.py --date $D --out /tmp/facts-$D.json
$PY $S/render_report.py --facts /tmp/facts-$D.json --out /tmp/report-$D.md
```

`render_report.py` 会在 stderr 报「N 处待填叙述位」。N=0 才算渲染完整。

然后你做三件事：**补待填 → 落盘 → 登记台账**（见「交付」节）。

## 口径：三个反直觉的点

这三条是实测确立的，凭直觉改必错：

### ① 渠道判别看 `project`，不是 `user`

`tasks.user` 对分身代投的任务**恒为「鲁大师」**（bot 自己的 token 名），毫无区分力。真实判别：

| project | 渠道 | 含义 |
|---|---|---|
| `avatar-local` | dingtalk | 钉钉人工提问，本地执行 |
| `relay-avatar` | dingtalk | 钉钉人工提问，relay 执行 |
| `''` + user=鲁大师 | cron | 计划触发（`plans.owner_name` 全是鲁大师） |
| `''` + user=中鲁运营 | api_server | token 账号跑云端运维，非人工提问 |
| `''` + user=真人名 | cli | 本人持自己 token 直连 API |
| `''` + user=__admin__/canary | system | 管理接口/健康探针 |
| `avatar-replay` | replay | 回放测试，不计入人工 |

### ② 影子执行：任务行数 ≈ 提问数 × 2

一个人工提问会落**两行**任务（`avatar-local` + `relay-avatar`）。relay 侧描述还会追加
`--- 【投递铁律｜由分身代投，禁止自行发送】…` 尾巴。

所以「提问数」必须剥尾巴后按 `(发起人, desc 前 120 字)` 去重。**直接 count 任务行会翻倍虚高。**
实测 2026-10-11：36 行 → 22 条真实提问。

### ③ 归属三级回退

`tasks.requester` 列 **2026-10-10 才上线**，此前全空。回退链：

```
requester（PG，权威）→ audit.log 的 task_id→actor（覆盖历史，实测 97%）→ unknown
```

`actor` 可能是 userId（数字）或 openId（`D…` 开头，如赵兴峰跨组织无 userId），
统一用 `config/key_users.toml` 的 `name_by_id` 反查姓名。

## 模板要求但本机无数据源的 4 项

照实打 `[缺档]`，**不要编**：

| 模板项 | 实情 | 替代 |
|---|---|---|
| `employee.db` 反查姓名/公司/部门 | 原模板假设的本地人员库，从未部署 | 已由 `shared/tmp/usage/key-users.toml`（`dws contact user get` 动态派生）替代；日报不再输出 employee.db 缺档提示 |
| `prediction_log` 预测质量追溯 | 全盘不存在 | 无；需业务方给落盘路径 |
| `--source all` / `--source "dingtalk,api_server"` CLI | 不存在此命令 | `usage_collect.py` 即其替代实现 |
| `acp` / `飞书` 渠道 | 本机无独立日志 | 恒为 0，标 `[缺档]` |

## 重点用户名单：运行时从本机派生，不是固定 17 人

模板写「17 人」，但**全盘无该名单定义**（T760 排查已确认）。更关键的是——
**机队各机的名单本来就不同**：本机 `policy.toml` 是 `owner_userid=20102`＋白名单 5 人，
而机队模板 `policy.example.toml` 是 `owner_userid=18434`＋`whitelist=[]`。
把某一台的名单提交进 git，夜间 provision 会把它强加给其他 7 台 → 活跃率与静默追踪全错。

所以：**成员＝运行时从本机授权真源派生**，运行时缓存写在
`shared/tmp/usage/key-users.toml`（存 dws 反查来的 org/dept/title，省得每次跑报告都打通讯录）。
该缓存**不在技能目录内**，provision 不会用仓库版覆盖；旧 `config/key_users.toml`
仅作首次迁移/兼容兜底。

`boot/usage-collect.sh` 每日预采集前会 best-effort 跑一次 `build_roster.py`：
动态授权新增/撤销/过期后，次日日报名单自动跟随；重派生失败时沿用现有缓存，不阻断日报。
`build_roster.py` 拒绝写空名单，避免 policy/动态授权读取异常时误清缓存。

```bash
$PY $S/build_roster.py --dry-run              # 先看派生结果
$PY $S/build_roster.py                        # 写入 shared/tmp/usage/key-users.toml
$PY $S/build_roster.py --include-historical   # 把 audit 历史活跃发起人也纳入
$PY $S/build_roster.py --no-dws               # 离线：跳过通讯录，沿用缓存里的 org/dept
```

派生来源（每人标 `source`，可追溯）：

| source | 来源 | 本机人数 |
|---|---|---|
| `policy_owner` | policy.toml `owner_userid` | 1（崔成东） |
| `superadmin` | policy.toml `superadmin` | 1（毕倩） |
| `static_whitelist` | policy.toml `whitelist` | 5（王洪彬、邹雄、孙瑞昱、邵磊、赵兴峰） |
| `file_uploader` | policy.toml `file_uploaders` | 0 |
| `dynamic_grant` | `run/whitelist_dynamic.json` **且未过期** | 6（舒瀚、孔德光、闫路英、王思义、顾芳芳、张帅） |
| `historical_active` | audit.log 历史活跃发起人 | 需 `--include-historical` |

本机合计 **13 人**（2026-10-16 重派生：撤销靳光传动态授权，纳入顾芳芳、张帅）。活跃率按**实际人数**算（N/13），不写死 17；换台机器自动变成那台的数。
业务方若另有原始名单，应把它落到本机授权真源（`policy.toml` 或动态授权），不要手改运行时缓存——每日重派生会按真源覆盖成员，只保留人工 `note`。

### 姓名/部门反查的三个坑

1. **通讯录返回顺序随机**：邹雄(40714) 有 **34 个部门**，连查两次 `depts[0]` 分别是
   「KG董事局成员」和「乐安家族管理委员会成员」。直接取 `depts[0]` 会让报告每天显示不同部门。
   → `pick_dept()` 确定性选择：**旧缓存的部门若仍在列表里就沿用**（跨次稳定），
   否则取 `deptPathName` 最短者、同级按名排序。
2. **多部门高管显示岗位而非部门**：`dept_count > 3` 时报告改显示 `orgTitle`
   （邹雄 → 「京博控股集团／总经理(主岗)（兼 34 部门，取岗位）」），比随机挑一个董事会名有意义。
3. **通讯录查不到的人**：历史案例靳光传(12223) 返回全空（离职/无权限/id 变更），
    赵兴峰跨组织无本 corp userId。姓名回退链必须含 `whitelist_dynamic.json`——
    动态授权表可能是唯一姓名源，漏掉这一环就会退化成用 id 当姓名。
    靳光传已于 2026-10-16 撤销动态授权并从重派生名单移除。
    查不到的 org/dept 标 `[缺档] 通讯录无返回`，**不要猜**。

`dws` 的 profile 各机不同，`build_roster.py` 从 `dws auth status` 动态取
（`corp_id:user_id`），不写死。企业邮箱对报告无用且属 PII，**不写入文件**。

## 你要补的 1 处叙述位

精简版渲染器已把数字、人员表、异常发现全部填好，只留一个判断位：

| 位置 | 要写什么 |
|---|---|
| 3 节 建议 | 针对渲染器列出的异常，给不超过 3 条可执行建议；每条写清动作、优先级、责任/归属；无异常填「无」 |

渲染器 stderr 会报「1 处待填叙述位」；补齐后必须为 0。不要把旧版的 5 处叙述位或逐会话卡片加回来。

需求类型由采集器自动归类（`usage_collect.TOPIC_RULES`），渲染器只展示类别；归类明显不对就去改规则，别在报告里手改单条。

> 原模板 3.3 要求逐会话卡片与「需求/交付」原文——按铁律 6（不留存原文）和铁律 9（两页精简）已取消。

## 交付：落盘 → 台账 → 摘要＋文件推送

```bash
D=2026-10-11                                   # 报告日
PY=/mnt/vol-eltaah12/finaagent/.venv/bin/python
S=/mnt/vol-eltaah12/finaagent/relay-dev/skills/ludashi-usage-daily-report/scripts
OUT=/mnt/vol-eltaah12/workspace/shared/report/自检报告/$(date +%Y-%m)
# 报告抬头/文件名用**本机分身名**，不写死鲁大师：本机=鲁大师，其他机=各自 avatar_name。
# display_name 由宿主机 usage_collect.py 写进 facts；--print-name 取出，与报告标题同源一致。
NAME=$($PY $S/render_report.py --facts /tmp/facts-$D.json --print-name)
F=$OUT/$(date +%Y%m%d)_T<任务号>_${NAME}用户日度使用分析_${D}_v1.md

# 1) 落盘（域用「自检报告」＝系统自身运行度量，最贴合）
mkdir -p $OUT && cp /tmp/report-$D.md "$F"

# 2) 登记台账（必须，否则算未完成）。注意是 --domain 不是 --topic；同 path 重复 add 是幂等 upsert
store add "$F" --kind report --domain 自检报告 --task <任务号> --note "报告日 $D"

# 3) 生成推送摘要（人员使用置顶，~400 字符，不含任务原文）
$PY $S/render_report.py --facts /tmp/facts-$D.json --summary --out /tmp/summary-$D.md
```

### 推送：一律「摘要＋文件」两条消息，逐个收件人各发一遍

```bash
DWS=/root/fenshen-ludashi/bin/dws
PROF=ding3b5622356ccdf1e2:37505774            # 各机不同，从 dws auth status 取 corp_id:user_id
SK=/mnt/vol-eltaah12/workspace/shared/skills/ludashi-usage-daily-report/scripts

# 收件人从本机配置解析，不许写死人名
while IFS= read -r R; do
  [ -n "$R" ] || continue
  # ① 摘要（Markdown DM）
  $DWS chat +dm --to "$R" --content "$(cat /tmp/summary-$D.md)" --profile $PROF --yes --format json
  # ② 完整件（附件）。--file 只吃**工作目录内的相对路径**，绝对路径会被拒 → 先 cd
  ( cd "$(dirname "$F")" && $DWS chat +messages-send --user-query "$R" --file "$(basename "$F")" \
      --profile $PROF --yes --format json )
done < <(python3 $SK/recipients.py --kind report)
```

**发完必须逐个收件人回读核验**，不能只看 `success: true`；部分成功要如实报告：

```bash
# 拿 openTaskId 换真实 messageId
$DWS chat +messages-query-send-status --open-task-id '<openTaskId>' --profile $PROF --format json
# 再回读会话确认内容在（默认 desc＝最新在前，取 msgs[:n] 不是 msgs[-n:]）
$DWS chat +chat-messages --conversation-id '<cid>' --limit 5 --profile $PROF --format json
```

回读的 `text` 字段是**纯文本投影**，会剥掉 Markdown 表格线与反引号，所以字数比原文少
（实测 1923 字符发出、回读 950 字）——**这不是截断**，比对末尾内容是否一致即可判断完整性。

发错了要撤回：`$DWS chat +messages-recall --msg-id '<messageId>' --conversation-id '<cid>' --yes`
（`+messages-query-send-status` 的 `nextActions` 里会直接给出可用参数）。

## 常见坑

- **必须用 `/mnt/vol-eltaah12/finaagent/.venv/bin/python`**，系统 python3 无 psycopg2，会静默降级成 logs_only。
- 跑出来 `source` 字段应是 `pg_authoritative`。若是 `logs_only`，说明 PG 没连上，
  报告首屏会有 `[降级]` 横幅——**这种报告不能交给领导**，先查 relay.env / PG 连通性。
- 跑**历史**日报时静默天数依赖 audit 的 cutoff 过滤；若某人显示「无记录」但他明明活跃过，
  检查 `load_audit_last_seen(cutoff=…)` 是否被改成了"先取 max 再过滤"（那是 bug，会丢数据）。
- `events-*.ndjson` 只有 2026-09-24 起的档；更早的日期入站消息会是 0 并报 gap，属正常。
- 周末/节假日要保留 `[周末基线]` 标签，环比不与工作日直接同比。
- 交易日判定**只按周末**，未接法定节假日日历——调休上班的周六会被误判成非交易日，需在 2.1 手工纠正。

## 文件

| 路径 | 作用 | 入库 |
|---|---|---|
| `scripts/build_roster.py` | 从本机 policy.toml＋whitelist_dynamic＋events＋通讯录派生重点用户名单，写 `shared/tmp/usage/key-users.toml` | ✓ |
| `scripts/usage_collect.py` | 采集器：PG＋audit＋events＋rounds → facts.json（宿主机跑，需凭据；优先读运行时名单缓存） | ✓ |
| `scripts/render_report.py` | 渲染器：facts.json → 精简版报告 MD（人员使用置顶、两页内）；`--summary` 出推送摘要；`--print-name` 出本机展示名（纯标准库） | ✓ |
| `scripts/self_name.py` | 解析**本机**分身展示名：bot 自称鲁大师→鲁大师，否则 policy.toml `avatar_name`（标题/摘要/文件名用） | ✓ |
| `scripts/recipients.py` | 解析**本机**收件人（report / alert 两类） | ✓ |
| `shared/tmp/usage/key-users.toml` | 名单**运行时缓存**（派生结果＋org/dept/title），各机不同、不入库、provision 不同步 | ✗ |
| `config/key_users.toml` | 旧名单缓存/首次迁移兜底；运行时缓存存在后不再作为主源 | ✓ |
| `config/recipients.example.json` | 收件人配置模板与说明 | ✓ |

本机收件人配置**不在技能目录里**，在 `avatar/usage-report-recipients.json`（各机不同、
不入库、provision 不同步）；`.gitignore` 另有 `skills/*/config/*.local.json` 兜住开发期
放在技能目录内的副本。
| `references/template.md` | 用户给的报告模板原文 | ✓ |
| `references/data-sources.md` | 真实数据源映射、字段含义、已知缺口 | ✓ |

**换机器 / 人员变动后先跑 `build_roster.py`**；本机每日 08:30 预采集前也会自动 best-effort 重派生。
**换机器后检查 `avatar/usage-report-recipients.json`**，否则会按本机 policy 派生（可能不是你想发的人）。
