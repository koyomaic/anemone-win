---
name: ludashi-usage-daily-report
version: 1.0.0
description: 生成《鲁大师用户日度使用分析报告》。当用户说"出昨天的使用分析报告""盘点今天谁用了鲁大师""日度使用分析""用户活跃日报""重点用户活跃情况""谁在用分身""使用量统计""生成 usage 日报"时使用。负责按固定模板盘点全渠道会话/人工提问/重点用户活跃/交付成功率/静默追踪，数据取自 relay 现役 PG 与本机 audit·events·rounds 日志。不做周报（走周报流程）、不做单任务查询（走 relay 台账）。
---

# 鲁大师用户日度使用分析报告

按固定模板盘点**某一天**鲁大师的使用情况，产出可交给领导的 Markdown 报告。

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
7. **推送一律「摘要＋文件」**：摘要用 `render_report.py --summary` 生成（聚焦用户使用量，
   ~430 字符），完整件作附件发。不要把整份报告塞进消息正文。

## 每日自动化：两段式（宿主机采集 → 沙箱渲染）

**为什么必须拆两段**：本地任务沙箱的 `local_sandbox_hide` 整体隐藏 `relay.env`
（policy.toml 明写「凭据在沙箱内**不可见**，不是可见但禁读」，是沙箱出网不限后的剩余防线）。
所以沙箱内的计划任务读不到 `RELAY_DB` → 采集器必然降级 `logs_only` → 日报只剩入站消息数，
任务级/用户级全空。**直接在计划里跑采集会每天静默产出废报告。**

| 段 | 触发 | 跑什么 | 凭据 | 产物 |
|---|---|---|---|---|
| ① 采集 | 宿主机 systemd `ludashi-usage-collect.timer`，每日 **08:30** | `boot/usage-collect.sh` → `usage_collect.py` | 需要 `RELAY_DB`（宿主机可读） | `shared/tmp/usage/facts-<日>.json` |
| ② 渲染投递 | relay 计划 **#12「每日鲁大师用户日度使用分析报告」**，每日 **08:45** | `render_report.py` ＋ 补叙述 ＋ 落盘 ＋ 台账 ＋ DM | 不需要（纯标准库，系统 python3 即可） | `shared/report/自检报告/<YYYY-MM>/…md` |

衔接点 `shared/tmp/usage/` 的可达性已实测：

- **不在** `local_sandbox_hide` → 沙箱**可读**（宿主机写、沙箱读，只读正好）
- `shared/report`、`shared/skills` **在** `local_sandbox_rw` 16 项里 → 沙箱能读脚本、能写报告
- `render_report.py` 不 import psycopg2 → 沙箱内系统 `python3` 直接可跑（已实测）

08:30 这个点是刻意错开 08:00 的 `daily-report.timer`（同样查 `RELAY_DB`，避免并发争库）。

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
| 本技能全部文件 | `shared/skills/ludashi-usage-daily-report/` | 21b |

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
| `employee.db` 反查姓名/公司/部门 | 全盘不存在 | `config/key_users.toml`（由 `dws contact user get` 反查生成） |
| `prediction_log` 预测质量追溯 | 全盘不存在 | 无；需业务方给落盘路径 |
| `--source all` / `--source "dingtalk,api_server"` CLI | 不存在此命令 | `usage_collect.py` 即其替代实现 |
| `acp` / `飞书` 渠道 | 本机无独立日志 | 恒为 0，标 `[缺档]` |

## 重点用户名单：运行时从本机派生，不是固定 17 人

模板写「17 人」，但**全盘无该名单定义**（T760 排查已确认）。更关键的是——
**机队各机的名单本来就不同**：本机 `policy.toml` 是 `owner_userid=20102`＋白名单 5 人，
而机队模板 `policy.example.toml` 是 `owner_userid=18434`＋`whitelist=[]`。
把某一台的名单提交进 git，夜间 provision 会把它强加给其他 7 台 → 活跃率与静默追踪全错。

所以：**成员＝运行时从本机授权真源派生**，`config/key_users.toml` 只是派生结果的
**缓存**（存 dws 反查来的 org/dept/title，省得每次跑报告都打通讯录）。

```bash
$PY $S/build_roster.py --dry-run              # 先看派生结果
$PY $S/build_roster.py                        # 写入 config/key_users.toml
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
| `dynamic_grant` | `run/whitelist_dynamic.json` **且未过期** | 5（舒瀚、孔德光、闫路英、靳光传、王思义） |
| `historical_active` | audit.log 历史活跃发起人 | 需 `--include-historical` |

本机合计 **12 人**。活跃率按**实际人数**算（N/12），不写死 17；换台机器自动变成那台的数。
业务方若给出原始名单，往 `key_users.toml` 追加 `[[user]]` 块即可（重跑派生会保留人工 `note`）。

### 姓名/部门反查的三个坑

1. **通讯录返回顺序随机**：邹雄(40714) 有 **34 个部门**，连查两次 `depts[0]` 分别是
   「KG董事局成员」和「乐安家族管理委员会成员」。直接取 `depts[0]` 会让报告每天显示不同部门。
   → `pick_dept()` 确定性选择：**旧缓存的部门若仍在列表里就沿用**（跨次稳定），
   否则取 `deptPathName` 最短者、同级按名排序。
2. **多部门高管显示岗位而非部门**：`dept_count > 3` 时报告改显示 `orgTitle`
   （邹雄 → 「京博控股集团／总经理(主岗)（兼 34 部门，取岗位）」），比随机挑一个董事会名有意义。
3. **通讯录查不到的人**：靳光传(12223) 返回全空（离职/无权限/id 变更），
   赵兴峰跨组织无本 corp userId。姓名回退链必须含 `whitelist_dynamic.json`——
   靳光传的名字**只有那里有**，漏掉这一环就会退化成用 id 当姓名。
   查不到的 org/dept 标 `[缺档] 通讯录无返回`，**不要猜**。

`dws` 的 profile 各机不同，`build_roster.py` 从 `dws auth status` 动态取
（`corp_id:user_id`），不写死。企业邮箱对报告无用且属 PII，**不写入文件**。

## 你要补的 5 处叙述位

渲染器只填数字，判断类的留给你：

| 位置 | 要写什么 |
|---|---|
| 1 节 一句话结论 | 主体渠道＋提问数＋重点用户活跃＋最活跃的人＋有无异常，一句话 |
| 2.1 填写说明 | 点明当天主体（如「周日，人工提问为主体，22 条」） |
| 2.2 填写说明 | 1–2 句解释主要渠道变化 |
| 3.1 追问结构 | 与前日对比多轮会话增减 |
| 5 节 观察与建议 | 从渲染器给的「候选观察」里挑，补上建议动作/优先级/归属 |

第 5 节的候选观察是渲染器**从数据直接算出来的**（台账登记失败次数、算力放大倍数、
超 30 天静默人数、归属缺口等），可直接取用，但**建议动作要你自己写**。

3.3 卡片的「需求类型」由采集器自动归类（`usage_collect.TOPIC_RULES`），不再留待填；
归类明显不对就去改规则，别在报告里手改单条。

> 模板原文 3.3 要求每张卡片有「需求」和「交付」两个字段填用户原文/结论——
> 按铁律 6 已取消，改为「提问规模（字数）＋需求类型」。模板与实现的这处差异是**有意的**。

## 交付：落盘 → 台账 → 摘要＋文件推送

```bash
D=2026-10-11                                   # 报告日
PY=/mnt/vol-eltaah12/finaagent/.venv/bin/python
S=/mnt/vol-eltaah12/finaagent/relay-dev/skills/ludashi-usage-daily-report/scripts
OUT=/mnt/vol-eltaah12/workspace/shared/report/自检报告/$(date +%Y-%m)
F=$OUT/$(date +%Y%m%d)_T<任务号>_鲁大师用户日度使用分析_${D}_v1.md

# 1) 落盘（域用「自检报告」＝系统自身运行度量，最贴合）
mkdir -p $OUT && cp /tmp/report-$D.md "$F"

# 2) 登记台账（必须，否则算未完成）。注意是 --domain 不是 --topic；同 path 重复 add 是幂等 upsert
store add "$F" --kind report --domain 自检报告 --task <任务号> --note "报告日 $D"

# 3) 生成推送摘要（聚焦用户使用量，~430 字符，不含任务原文）
$PY $S/render_report.py --facts /tmp/facts-$D.json --summary --out /tmp/summary-$D.md
```

### 推送：一律「摘要＋文件」两条消息

```bash
DWS=/root/fenshen-ludashi/bin/dws
PROF=ding3b5622356ccdf1e2:37505774            # 各机不同，从 dws auth status 取 corp_id:user_id

# ① 摘要（Markdown DM）
$DWS chat +dm --to <收件人> --content "$(cat /tmp/summary-$D.md)" --profile $PROF --yes --format json

# ② 完整件（附件）。--file 只吃**工作目录内的相对路径**，绝对路径会被拒 → 先 cd
cd "$(dirname "$F")"
$DWS chat +messages-send --user-query <收件人> --file "$(basename "$F")" --profile $PROF --yes --format json
```

**发完必须回读核验**，不能只看 `success: true`：

```bash
# 拿 openTaskId 换真实 messageId
$DWS chat +messages-query-send-status --open-task-id '<openTaskId>' --profile $PROF --format json
# 再回读会话确认内容在（默认 desc＝最新在前，取 msgs[:n] 不是 msgs[-n:]）
$DWS chat +chat-messages --conversation-id '<cid>' --limit 5 --profile $PROF --format json
```

回读的 `text` 字段是**纯文本投影**，会剥掉 Markdown 表格线与反引号，所以字数比原文少
（实测 1923 字符发出、回读 950 字）——**这不是截断**，比对末尾内容是否一致即可判断完整性。

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

| 路径 | 作用 |
|---|---|
| `scripts/build_roster.py` | 从本机 policy.toml＋whitelist_dynamic＋events＋通讯录派生重点用户名单 |
| `scripts/usage_collect.py` | 采集器：PG＋audit＋events＋rounds → facts.json |
| `scripts/render_report.py` | 渲染器：facts.json → 报告 Markdown |
| `config/key_users.toml` | 名单**缓存**（派生结果＋org/dept/title），换机自动重派生 |
| `references/template.md` | 用户给的报告模板原文 |
| `references/data-sources.md` | 真实数据源映射、字段含义、已知缺口 |

**换机器 / 人员变动后先跑 `build_roster.py`**，否则名单是上一台的。
