# 数据源与口径（L1 只读）

四路数据源，任一路失败自动降级并写入 `errors`，产物顶部如实标注。全部为**读操作**。

## 1. 本地目录树

| 维度 | 路径 | 取法 |
|---|---|---|
| 知识 | `shared/knowledge/` | `os.scandir` 递归 3 层，累计文件数与字节 |
| 技能 | `shared/skills/` | 逐项读 `SKILL.md` 的 `description:` 与「触发词：」行；`os.walk` 数文件 |
| 语料 | `shared/memory/*.md` | 扁平列举（name/size/mtime）；目录不存在 → `exists=false` |
| MCP | `shared/mcp/*.json` | 扁平列举 |

- 跳过：`__pycache__ .git node_modules .venv venv tmp .archive` 与所有 `.` 开头条目。
- 技能「来源」判定：目录本身是符号链接且 `realpath` 不在 `shared/skills` 下 → **系统级共享**
  （provision 21b2 按仓库 `.share` 标记建链，指向 `/root/.agents/skills` 或 `/opt/team/skills`），
  否则为**本地共享区**。
- 根目录直属文件数 = 根 `files` 合计 − 各子目录 `files` 合计（`_rollup` 后计算）。

## 2. store 台账（PG 权威索引）

```bash
store q --format json --limit 5000      # 主数据源
store dup                               # --deep：sha256 重复检测
store doctor                            # --deep：DB↔磁盘对账 + 命名违规 + 缺口
```

- 可执行文件解析：`shutil.which("store")` → 回退 `/usr/local/bin/store`；不存在则整维度跳过。
- 记录字段：`id fdate kind domain topic status wiki_status variant sensitivity authority
  sender_id sender_name path size version task_id task_status wiki_page_id note`。
- 聚合维度：`kind / domain / status / wiki_status / authority / sensitivity`。
- **缺口定义**：`wiki_status ∈ {none, skip, failed}`（`accepted` 为已受理，不算缺口）。
- **同步范围（每机私有，不可硬编码机队口径）**：缺口再按本机 wiki 同步范围分流为
  「范围内＝真缺口」与「范围外＝策略性不同步」。`resolve_sync_scope()` 判定优先级：
  1. `store doctor` 输出的 `wiki_sync_scope{scope, prefixes}`（`dc05a37` 起提供，**权威**）
  2. `store sync --help` 含 `config` 选项（`dc05a37`+）→ 读本机
     `STORE_SYNC_SCOPE` / `jingbowiki.env:JINGBOWIKI_SYNC_SCOPE`（`fourlib` 别名或 `all`），
     以及 `JINGBOWIKI_SYNC_PATHS`（逗号分隔自定义前缀）；均未设 → `all`
  3. `--help` 只有 `fourlib/all`（`941aa53` 口径，已被 `dc05a37` 撤销）→ `fourlib`
  4. 无 `--scope`（早于 `941aa53`）→ `all`
  `fourlib` 前缀 = `knowledge/口径方法论/{知识库,模型库,案例库,规则库}/`，仅中鲁本机别名。
  路径匹配同时接受 `store:` 前缀形式（doctor 的 orphan 样例用该写法）。
- `doctor` 关键指标：`db_rows disk_files orphan_disk_count orphan_db_count problem_count ok`、
  `violations{bak_in_production_dirs, bad_chars, bad_name_total, bad_name_bytes, bad_name_by_top, count}`、
  `domains{域名: 条数}`（值为 0 即「已定义但空」的业务域）、`by_kind{kind: {files, bytes}}`、
  `wiki_sync_scope{scope, prefixes}`（`dc05a37`+）。
- `doctor`/`dup` 输出为 JSON 时解析后**瘦身**入库（长列表截断到 20 条并另存 `*__total` 真值），
  非 JSON 时原文截断 20000 字符以 `<pre>` 呈现。
- 产物 JSON 格式**剥离 `records` 明细**，只留 `record_count` 与聚合，避免大体量外流。

## 3. jingboWiki（纯 Wiki 库）

凭据：`shared/secrets/jingbowiki.env`（600）→ `JINGBOWIKI_BASE / JINGBOWIKI_EMAIL /
JINGBOWIKI_PASSWORD / JINGBOWIKI_KB_ID / JINGBOWIKI_KB_NAME`；
库 ID 解析顺序：环境变量 `STORE_KB_ID` → `JINGBOWIKI_KB_ID`。
登录 `POST {BASE}/auth/login` 取**顶层 `token`**（不是 `access_token`），后续 `Authorization: Bearer`。
token 仅用于当次调用，不写产物、不写日志、不进缓存。

| 端点 | 用途 | 实测口径 |
|---|---|---|
| `GET knowledgebase/{kb}/wiki/stats` | 总量 | `total_pages total_links orphan_count pending_tasks pages_by_type recent_updates` |
| `GET knowledgebase/{kb}/wiki/pages?page=N&page_size=500` | 层级与清单 | `page_size=500` 实测可用（单页约 4.4MB / 0.4s）；返回 `pages total total_pages page page_size` |
| `GET knowledgebase/{kb}/wiki/graph` | 图谱 | 返回 `nodes edges meta`；**nodes 上限 500**（服务端截断），edges 为全量 |
| `GET knowledgebase/{kb}/wiki/index` | 索引页 | 备用，当前未渲染 |

- **无 `/wiki/tree`、`/wiki/folders`、`/wiki/links`、`/wiki/orphans`、`/wiki/recent` 端点**（实测 404）；
  层级由页面自带元数据构建。
- 页面可用字段：`slug title page_type status summary aliases category_path wiki_path depth
  in_links out_links folder_id source_refs chunk_refs version created_at updated_at`。
- **层级真源 = `category_path`**（数组，如 `["数字化与科技","核心组件"]`）；
  `wiki_path` 是它的字符串形式（`entity/数字化与科技/核心组件/文泉驿正黑`），`depth` 为层深。
  空 `category_path` 归入 `(未分类)`。字符串型 `category_path` 按 `[/>|]` 切分兜底。
- 树节点结构：`{分类名: {"__pages": 本层页数, 子分类…}}`；`__total`（含子层合计）与
  `__subdirs` 由 `_tree_counts` 递归计算，`__` 前缀键不参与渲染。
- **`content` 字段一律丢弃**（页面正文不入产物，见 SKILL.md 铁律 6）；`summary` 仅计数不渲染。
- 枚举上限 `WIKI_MAX_PAGES=20000`，超出即停并在 `errors` 说明；拉取中断时保留已取部分。
- 排序产物：`top_linked`（按 `len(in_links)` 降序 Top20）、`recent`（按 `updated_at` 降序 15 条）。

## 4. 服务健康

- `GET http://127.0.0.1:8787/health` → relay：`version`（部署 sha）、`db`、`active`、`queued`
- `GET http://127.0.0.1:8791/health` → avatar：`ok`
- 8s 超时；不可达只降级标注，不影响其余维度。

## 缓存

- 路径 `shared/index/asset-map.cache.json`，权限 600，TTL 600 秒（`--ttl` 可调，`--no-cache` 绕过）。
- 命中时产物头部标「缓存数据（Ns 前）」；`quick` 缓存不会被 full 请求复用（避免层级树为空）。
- 缓存**剥离** `_secret_values`；`store.records` 明细保留在缓存内（本机 600 文件，不外发）。

## 安全自检

渲染完成后对产物全文做凭据扫描（Wiki 密码、登录邮箱、当次 token，长度 ≥6 才参与比对），
命中即 `stderr` 报告并返回退出码 3、**不落盘不输出**。所有文本经 `html.escape` 转义。

## 性能实测（本机 i-5i8tes9z，1278 页 / 168 文件 / 91 条台账）

| 模式 | 本地扫描 | store | Wiki | 产物 |
|---|---|---|---|---|
| `--quick` | 0.01s | 0.22s | 0.57s | — |
| full | 0.01s | 0.22s | ~2.3s | HTML 约 53KB |
| `--deep` | 0.01s | 0.65s | 2.34s | HTML 约 55KB |
| 缓存命中 | — | — | — | 约 0.13s |
