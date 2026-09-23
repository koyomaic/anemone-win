---
name: chatx-mcp
version: 1.0.0
description: chatx ClickHouse 只读查询 MCP 网关（https://chatx.chambroad.com/mcp）的团队接入技能。当用户说"帮我装一下这个 MCP""接入 chatx""用 chatx 查经营数据/日利润"，或需要把 chatx 注册为团队共享 MCP 时使用。能力：一键安装 stdio↔HTTP 桥接脚本并注册 shared/mcp/chatx.json，使本机所有 relay 任务自动挂载 mcp__chatx__* 工具（whoami/query/list_databases/list_tables/list_columns/list_catalog/list_definitions/metadata）。
---

# chatx MCP 团队接入（方案1：local 桥接）

chatx 是外部 **ClickHouse 只读查询网关**（`mcp-clickhouse-gateway`），入口
`https://chatx.chambroad.com/mcp`（Streamable HTTP）与 `/mcp/http`（无状态纯 JSON-RPC），
鉴权 `Authorization: Bearer <token>`。

## 为什么走 local 桥接而不是 remote 注册

relay 的 `shared_knowledge._claude_mcp_entry` 对 `remote` 条目只转
`{"type":"http","url":...}`，**不透传 headers**；chatx 需要 Authorization，直接注册
remote 会 401。故用 `local` 条目拉起本技能自带的 stdio↔HTTP 桥接脚本，由脚本注入
Authorization 头转发到 `/mcp/http`。

## 安装（幂等）

```
bash <本skill目录>/scripts/install.sh
```

- 铺 `shared/tools/chatx-mcp-bridge.py`（桥接，无密钥）
- 生成 `shared/mcp/chatx.json`（local 条目，指向桥接绝对路径）
- **下一个 relay 任务即自动挂载 `mcp__chatx__*`，无需重启 relay**

## 凭据（严禁入库）

token 只存 `shared/secrets/chatx.env`（600，`CHATX_MCP_TOKEN=...`），桥接脚本运行时读取。
本技能仓库内**不含任何 token**。缺失该文件时 install 会提示；向服务方 chambroad 申请后
写入即可。失效或权限变更同样找 chambroad 重新签发。

## 验证

```
bash <本skill目录>/scripts/verify.sh
```

应看到 `serverInfo=mcp-clickhouse-gateway` 与 8 个工具。运行时也可在任务里直接
`tools/call whoami` / `list_databases`（当前 token 可见库：`company_dataset_xny`）。

## 已知坑

- `/mcp` 入口 `tools/list` 稳定缺 `list_catalog`；桥接固定走 `/mcp/http`（8 工具全）。
- 只读网关：`query` 仅接受 SELECT 类语句，写操作被拒。
- 查业务数据前先 `list_columns` + `list_definitions` 看列名与中文释义再写 SQL。

## 溯源

接入细节与权限范围见团队知识库 `shared/knowledge/mcp-chatx-接入说明.md`（jingboWiki
「项目大脑」同步）。
