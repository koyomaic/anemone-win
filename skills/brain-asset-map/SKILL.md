---
name: brain-asset-map
version: 1.0.0
description: 生成「大脑资产地图」——财务智脑当前知识资产与能力的一次性可视化盘点（单文件 HTML / 钉钉 Markdown 摘要 / 结构化 JSON）。当用户说"看看大脑现在有什么资产""资产地图""盘点知识库/语料库/技能""大脑能力清单""知识层级结构""资产总览""生成资产盘点页面""知识库有多少东西""有哪些技能能用""资产体检"时使用。只读盘点：汇总本地知识目录树、store 台账、jingboWiki 层级与图谱、技能清单、memory/MCP 现状，并给出双写缺口与命名违规体检。不做增删改（写操作走 relay 看板或 L2 版本）、不做单条知识检索（走 jingbowiki-api）、不做任务查询（走 relay 台账）。
---

# 大脑资产地图（L1 只读盘点）

把分散在四处的大脑资产**一次性汇总成可交付的页面**，让用户直观看见"大脑现在有什么、能干什么、哪里缺"。
按需生成、用完即走，不依赖任何常驻后台界面。

## 铁律（先读，违反即产物失真）

1. **只读**。本技能不写任何资产真源：不改知识文件、不动台账、不碰 Wiki、不增删技能。
   唯一落盘是产物 HTML/MD/JSON 与 `shared/index/asset-map.cache.json` 缓存。
   用户要求"增加/修改/删除"资产时，**不要用本技能硬做**，答：L1 只读，写操作走 relay 看板
   （`https://<本机IP>:8788` 的共享区页签，支持 skills/mcp/memory 上传删除）或等 L2 版本。
2. **数字只能来自脚本产物**，不许心算、不许目测、不许按印象补。要什么维度就加参数重跑。
3. **无数据的维度如实呈现，绝不编造**。`memory` 当前 0 条、`MCP` 当前 0 个是**事实**，
   照实显示"未启用/未配置"，不要写成"暂无数据"含糊过去，更不要虚构条目。
4. **取数降级必须显式标注**。任一路数据源不可达时脚本会把原因写进 `errors`，
   产物顶部有「取数降级提示」区块；转述给用户时**必须带上**，不要把残缺产物当完整结论汇报。
5. **凭据零外泄**。Wiki 凭据只从 `shared/secrets/jingbowiki.env`（600）读取、只用于当次调用；
   脚本渲染后做泄漏自检，命中即拒绝输出（退出码 3）。**不要**把 env 内容、token、密码
   写进对话、产物或日志；也不要把看板口令发给用户。
6. **产物不含知识正文**。台账 `sensitivity=internal`，页面只出标题、路径、分类、计数与元数据。
   用户要正文时单条取（走 jingbowiki-api 或直接读文件），不要批量导出。
7. **默认不登记台账**。资产地图是高频重跑的快照，每次登记会污染 `store` 台账；
   仅当用户明确要求"归档这份盘点"时才 `store add --kind report`。

## 用法

脚本：`scripts/asset_map.py`（Python 3，仅标准库 + `store` CLI + Wiki HTTP 接口）。
下文 `$S` 指本技能目录。

```bash
P=/mnt/vol-eltaah12/finaagent/.venv/bin/python

# 1) 完整版 HTML（默认，约 3–6 秒；含 1278 页级别的全量层级树）
$P $S/scripts/asset_map.py

# 2) 钉钉 Markdown 摘要（可直接发群/发人，约 40 行）
$P $S/scripts/asset_map.py --format md --out -

# 3) 结构化 JSON（供二次加工/对账，records 已剥离只留计数）
$P $S/scripts/asset_map.py --format json --out -

# 4) 秒级快答（只要总量与顶层分类，不枚举全部 Wiki 页面）
$P $S/scripts/asset_map.py --quick

# 5) 深度体检（追加 store dup 重复检测 + store doctor 对账，较慢）
$P $S/scripts/asset_map.py --deep

# 6) 忽略缓存强制重取（默认缓存 600 秒）
$P $S/scripts/asset_map.py --no-cache --ttl 0
```

参数：`--format html|md|json`、`--out PATH`（`-` 为 stdout，缺省落
`shared/report/asset-map/asset-map-<时间戳>.<ext>` 并同步覆盖 `latest.<ext>`）、
`--quick`、`--deep`、`--no-cache`、`--ttl N`。
成功时 stdout 是一行 JSON：`{ok, format, out, latest, bytes, generated_at, cached, errors}`。

## 产物六区块

1. **资产总览**：Wiki 页数/链接/孤儿、本地知识文件数与体积、台账条数、技能数、memory、MCP、待解析
2. **知识层级结构**：Wiki 分类树（由页面 `category_path` 实时构建，可折叠到 4 层）+ 本地目录树（3 层）
3. **能力矩阵**：技能 × 能力说明 × 触发词 × 文件数 × 来源（系统级共享 / 本地共享区）
4. **三视图对齐**：分类名在「本地目录 / store 台账 / Wiki 分类」三处的有无对照
5. **语料库 memory 与 MCP**：逐条清单，或如实标注未启用
6. **体检区**：双写状态分布、缺口清单（`wiki_status=none/skip/failed`）、页面类型、
   被引最多页面 Top20、最近更新；`--deep` 追加重复检测与 DB↔磁盘对账

## 转述给用户的口径

- 先给**总量四件套**：Wiki 页数、本地知识文件数/体积、台账条数、技能数。
- 再给**层级 Top**：Wiki 顶层分类前 5（带页数）+ 本地分类前 5（带文件数）。
- 最后给**缺口与体检**：真缺口条数（同步范围内）、`--deep` 时的磁盘孤儿/命名违规/空业务域。
- 结尾附产物路径；用户要页面就用 `dingtalk-send` 把 HTML 发过去，要摘要就直接贴 `--format md` 结果。

## 常见误判（实测踩过）

- **`wiki_status=none/skip` 不一定是缺口**：wiki 同步范围是**每机私有配置**
  （`dc05a37` 起 store `--scope` 默认 `config`，按本机 `STORE_SYNC_SCOPE` /
  `JINGBOWIKI_SYNC_SCOPE` 解析，缺省 `all`；`fourlib` 只是中鲁本机的四库路径别名，
  **不代表机队统一范围**，也可用 `JINGBOWIKI_SYNC_PATHS` 自定义前缀）。
  脚本按本机实际范围分流成「同步范围内（需处理）」与「范围外（策略性不同步）」两张清单，
  判定优先级：`store doctor` 的 `wiki_sync_scope`（权威）→ 本机 env 配置 → store 版本口径。
  **转述时只把范围内那批报成缺口**，否则会把正常策略报成故障；也**不要**把本机的四库前缀
  当成其他机队成员的口径。
- **「三处齐备 0 个」不是数据丢失**：本地目录用业务口径命名（财务制度、税务政策…），
  Wiki 分类由服务端自动聚类生成（财税与金融、组织与机构…），两套命名体系尚未建映射。
  这是**分类口径未统一**的治理缺口，照实说明，不要报成"知识库没同步"。
- **`wiki/graph` 节点数上限 500**：图谱节点被服务端截断，边数（5000+）才是全量；
  不要用节点数当"知识页总数"，总数看 `wiki/stats.total_pages`。
- **`wiki/stats` 与实时枚举会有小差异**：stats 是服务端计数，枚举期间可能有并发写入；
  产物页脚已注明，差异几十页内属正常，不要当成取数失败。
- **`store q` 与 `store doctor` 的 wiki_status 计数可能不同**：doctor 排除归档/未注记条目，
  两者都对，引用时说明来源。
- **quick 模式分类树为空**是预期行为（未枚举页面），要层级就去掉 `--quick`。
- **缓存**：600 秒内重跑直接吃缓存（约 0.1 秒），产物头部会标「缓存数据（Ns 前）」；
  用户说"刚更新过知识，重新盘一遍"时务必加 `--no-cache`。

## 边界

- 数据源、端点、字段与已知上限见 [references/data-sources.md](references/data-sources.md)。
- 单条知识检索/上传/解析状态 → `jingbowiki-api`；台账增改与血缘 → `store` CLI；
  技能与 memory 的增删 → relay 看板共享区；发文件给人 → `dingtalk-send`。
- 本技能不做定时推送。需要每日快照时由 `relay-plan` 建计划调用本脚本，再用 `dingtalk-send` 投递。
