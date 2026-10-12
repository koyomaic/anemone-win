# 真实数据源映射与已知缺口

> 2026-10-12 实测确立。改采集器前先读这份，否则会重踩「用 user 判渠道」「count 任务行当提问数」两个坑。
> 前置排查见 `shared/tmp/hermes-usage-discovery.md`（T760，无凭据环境视角）；
> 本文补上 T760 拿不到的 **PG 现役库**部分。

## 1. 权威源：relay 现役 PostgreSQL

连接串在 `/mnt/vol-eltaah12/workspace/relay.env` 的 `RELAY_DB`（**严禁回显/入库/外发**）。
必须用 `/mnt/vol-eltaah12/finaagent/.venv/bin/python`（系统 python3 无 psycopg2）。

规模实测（2026-10-12）：`tasks` 761 行，跨度 2026-09-15 ~ 2026-10-12；`plans` 9 行。

### tasks 表关键字段

| 字段 | 用途 | 坑 |
|---|---|---|
| `project` | **渠道判别唯一可靠依据** | 见下表 |
| `user` | token 主体名 | 分身代投恒为「鲁大师」，**无区分力** |
| `requester` | 真实发起人姓名 | **2026-10-10 才上线**，此前全空（48/761 有值） |
| `description` | 提问原文 | relay 侧带「--- 【投递铁律…」尾巴，去重前要剥 |
| `status` | done/failed/cancelled/review/conflict/queued/running | 影子对要取「更终态」的那个 |
| `engine` | local/claude/opencode | local＝本地执行，claude＝relay 执行 |
| `result` | 交付正文 | 卡片「交付」字段取前 400 字提炼 |
| `error` | 错误 | **完成态任务也可能有残留**（如「升级 relay 门禁」），渲染时分开标注 |
| `created_at/started_at/finished_at` | epoch float | 时区按 Asia/Shanghai 转 |
| `tokens/cost` | 算力 | 影子对要相加 |

### project → 渠道映射（实测全量交叉）

| project | engine | 行数 | 渠道 | 判定依据 |
|---|---|---|---|---|
| `avatar-local` | local | 326 | dingtalk | 钉钉人工，本地执行 |
| `relay-avatar` | claude | 126 | dingtalk | 钉钉人工，relay 执行 |
| `''` | claude | 261 | cron / api_server | user=鲁大师→cron；user=中鲁运营→api_server |
| `''` | (空) | 38 | cron / system | user=__admin__/canary→system |
| `''` | opencode | 9 | cli | 本人持 token 直连 |
| `avatar-replay` | claude | 1 | replay | 回放测试，不计入人工 |

`project=''` 的 user 分布（308 行）：中鲁运营 144、鲁大师 99、崔成东 21、王洪彬 18、
舒瀚-trae 15、__admin__ 5、白露 2、canary 2、靳光传 1、孔德光 1。
→ 其中**真人名的 58 行是 cli 渠道**（本人用自己 token 直连 API），不是 cron。

### plans 表

9 个计划，`owner_name` **全是「鲁大师」**，故 `user='鲁大师' AND project=''` → cron。

| id | name | on_calendar | enabled |
|---|---|---|---|
| 1 | 每日原油价格预测调取 | `*-*-* 07:50:00` | ✓ |
| 2 | 每日成品油价格预测调取 | `*-*-* 08:05:00` | ✓ |
| 3 | 每日市场行情研究报告 | `*-*-* 08:20:00` | ✓ |
| 4 | b0-refined-forecast-0735 | `*-*-* 07:35:00` | ✗ |
| 5 | b0-refined-forecast-0700 | `Mon..Fri *-*-* 07:00:00` | ✓ |
| 6 | d17-readiness-gate | `Mon..Fri *-*-* 07:15:00` | ✓ |
| 7 | 每日净头寸三层研判 | `*-*-* 07:55:00` | ✓ |
| 10 | 任务启用检查当日数据更新情况并将 | `*-*-* 18:00:00` | ✓ |
| 11 | D-02国内成品油价格表2130自动归档 | `*-*-* 21:30:00` | ✓ |

cron 构成按 `action::json->>'description'` 与 tasks.description **精确匹配**归并。
实测只有 92/308 能匹配上——**计划描述被改过就回溯不了**，归入「(计划已改名/删除，无法回溯)」。

## 2. 影子执行：提问数必须去重

一个人工提问落**两行**任务：

```
#701  project=avatar-local  engine=local   requester=毕倩  desc="按照记忆归档 ✅ 已收到《合同及订单-11.xlsx》…"
#702  project=relay-avatar  engine=claude  requester=毕倩  desc="按照记忆归档 ✅ 已收到《合同及订单-11.xlsx》… --- 【投递铁律｜由分身代投，禁止自行发送】…"
```

去重规则：剥掉 `\s*---\s*【投递铁律.*$` 尾巴 → 归一空白 → 取前 120 字 →
按 `(发起人, desc核心)` 归并。合并时：

- `status` 取更终态：done(4) > review/conflict(3) > failed(2) > cancelled(1)
- `end` 取更晚的 finished_at
- `result_head` / `error` 取非空的那个
- `tokens` / `cost` **相加**

实测 2026-10-11：36 行 → 22 条提问（1.6×）；10-05：30→16；10-09：29→19；10-12：29→15。

**直接 count 任务行会把提问数翻倍虚高**，这是最容易犯的错。

## 3. 归属回退链

| 级别 | 来源 | 覆盖 | 说明 |
|---|---|---|---|
| 1 | `tasks.requester` | 48/761 | 2026-10-10 上线（cf9281f），此后 100% |
| 2 | `audit.log` task_id→actor | +391 | 取每个 task_id **最早**的非空 actor |
| 3 | unknown | 13/452 | 归入 unknown，不占排名 |

对 452 个人工任务实测覆盖 **97%**（439/452）。

audit 里能归属的 action：`local_shadow`(325) + `task_submit`(113) + 零星。
**只用 `task_submit` 只能覆盖 33%**——必须把 `local_shadow` 也算进来。

`actor` 形态：userId（数字，如 `18377`）或 openId（`D…` 开头，如赵兴峰
`DJTDWXe5Omkca6fEkz0vxWVZZG4c4SxTc`，跨组织无本 corp userId）。
统一用 `key_users.toml` 的 `name_by_id` 反查姓名。

audit actor 分布（442 个 task_id）：18377 毕倩 183、18434 王洪彬 55、赵兴峰(openId) 49、
20102 崔成东 45、40125 孙瑞昱 36、12139 孔德光 27、23518 王思义 20、34718 闫路英 15、
19173 舒瀚 11、48332 邵磊 1。

## 4. 本机日志

| 路径 | 格式 | 关键字段 | 坑 |
|---|---|---|---|
| `avatar/events-<YYYYMMDD>.ndjson` | 单行 JSON | `type`(`…_o2o_all` 私聊 / `…_at` 群@)、`event_id`、`sender`、`content`、`create_time`(字符串) | **按 event_id 去重**；只有 2026-09-24 起的档 |
| `avatar/audit/audit.log` | 单行 JSON | `ts`(**字符串** `YYYY-MM-DD HH:MM:SS`)、`action`、`actor`、`detail`、`task_id`、`backend`、`ingress` | ts 是字符串不是 epoch |
| `avatar/rounds/<task_id>.jsonl` | 单行 JSON | `t`(**epoch float**)、`kind`(submit/done/failed/…)、`data` | t 是 float，`t[:10]` 会 TypeError |
| `avatar/run/contacts.json` | JSON | openDingTalkId → userId | `miss:` 前缀＝未解析 |
| `avatar/run/whitelist_dynamic.json` | JSON | userId → {name,tier,by,ts,expires} | grant_valid_days=90 会过期 |
| `avatar/policy.toml` | TOML | whitelist/blacklist/owner_userid | 注释里有 userId=姓名 对照 |

audit action 全量分布：local_exec 352、local_shadow 325、local_status 325、task_submit 126、
send_file 121、**store_add_failed 79**、guest_ignored 22、send_refused 22、plan_denied 14、
plan_create 11、gate_confirm 11、plan_op 7。

异常信号（渲染器会自动挑进第 5 节候选观察）：
`send_refused`（越权发送被拦）、`guest_ignored`（未授权用户被忽略）、
`store_add_failed`（产出未登记台账）、`plan_denied`、`plan_disable_bad_calendar`、`plan_bad_action`。

## 5. 静默追踪的 cutoff 陷阱

`last_seen` 必须**先按报告日 24:00 过滤，再取 max**。

反例（曾经的 bug）：先全局取 max 再过滤 → 赵兴峰最晚活动是 10-12，跑 10-11 日报时
被 `>= hi` 整条跳过，连他 09-28 的真实活动也丢了 → 误判「无记录」。

两个来源都要并：
- PG：`SELECT "user",requester,max(created_at) FROM tasks WHERE created_at < hi GROUP BY 1,2`
- audit：`load_audit_last_seen(cutoff=hi)`，actor 经 name_by_id 转姓名

只用 PG 会漏掉**跨组织无 userId 的人**（赵兴峰），因为他的活动只以 openId 形式存在于 audit。

## 6. 姓名/公司/部门反查

`employee.db` **全盘不存在**（`/` 与 `/mnt/vol-eltaah12` 全量 find 无果）。
relay 的 `users` 表只有 `token/name/created_at/consumed_at`，无公司部门。

实际途径：

```bash
/root/fenshen-ludashi/bin/dws contact user get --ids 18434,40714,40125,48332,20102,19173,12139,34718,23518,18377 \
  --profile ding3b5622356ccdf1e2:37505774 --format json
# → orgEmployeeModel: orgUserId/orgUserName/orgName/depts[].deptName/orgTitle/orgAuthEmail
```

实测 11 个 id 只返回 10 人：**靳光传(12223) 返回全 None**（离职/无权限/id 变更），
故名单里其岗位留空。赵兴峰跨组织无 userId，通讯录不可反查。

反查结果已固化进 `config/key_users.toml`（org 全是「京博控股集团」，dept 各异）。
**人员变动后需重跑反查更新名单**，不要手改。

## 7. 模板要求但本机无源（照实标 [缺档]）

| 模板项 | 实情 | 处置 |
|---|---|---|
| `employee.db` | 全盘不存在 | 用 `key_users.toml`（dws contact 反查生成） |
| `prediction_log` | 全盘不存在（无文件/表/目录） | 标 [缺档]，需业务方给路径 |
| `--source all` / `--source "dingtalk,api_server"` CLI | 不存在此聚合命令 | `usage_collect.py` 即替代实现 |
| `acp` / `飞书` 渠道 | 本机无独立日志 | 恒为 0，标 [缺档] |
| 重点用户「17 人」清单 | 全盘 grep `17人\|17 人\|重点用户` 无定义性命中 | 用可追溯出处重建的 12 人，活跃率按实际人数算 |
| 法定节假日日历 | 未接 | 交易日判定只按周末，调休日会误判，需手工纠正 |

D-17 成品油价格预测自 2026-09-23 断供，是**上游数据源问题**（P-01 孙瑞昱），非代码缺陷；
若报告里出现相关失败，归因要写清楚，不要算成系统故障。

## 8. 输出与台账

```
shared/report/自检报告/<YYYY-MM>/<YYYYMMDD>_T<任务号>_鲁大师用户日度使用分析_<报告日>_v1.md
```

落盘后必须 `store add <path> --kind report --task <任务号> --topic 用户日度使用分析`。

`shared/report/` 已有域：会议纪要、市场分析、调价预期、周报、能力清单、自检报告、
项目进度、工作总结、经营汇报。「使用分析」归 **自检报告**（系统自身运行度量）最贴合；
新建域须先登记台账。
