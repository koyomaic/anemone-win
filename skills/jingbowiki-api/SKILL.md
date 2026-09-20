---
name: jingbowiki-api
description: 查询、调用和维护 jingboWiki 18082 端口的 HTTP 接口，支持知识上传、知识库查询、快速检索及其他接口操作。适用于 jingboWiki 系统集成、接口联调和接口文档更新。
---

# jingboWiki 接口

## 服务与参考

- API 基础地址：`http://10.200.3.235:18082/api/v1`。
- 健康检查：`GET http://10.200.3.235:18082/health`。
- [接口文档](references/api.md)：用户确认的 297 个接口条目，包含参数和请求响应；按需检索对应章节，不必一次加载全文。

## 纯 Wiki 上传与问答

用户要求纯 Wiki 或提供文件进行问答时，先读 [纯 Wiki 操作流程](references/pure-wiki.md)。按其中的配置、就绪检查、SSE 恢复和清理步骤执行，优先使用 [操作脚本](scripts/wiki_flow.py)。普通用户只需提供文件与问题，由助手完成必要的接口编排。不要把通用 RAG 检索接口用于纯 Wiki 流程。

## 调用要点

先从接口文档找到方法、路径和参数，再按本次用户任务调用。使用已配置的地址；用户明确指定其他地址时以本次指定为准。历史验证成功不代表当前仍可连接。

系统集成通过 `X-API-Key` 认证，登录态调用通过 `Authorization: Bearer <token>`。实际服务的 `POST /auth/login` 返回顶层 `token`，不要只查找 `access_token`；令牌仅用于当前调用，不输出或写入文档。本技能不存储密码和密钥，从当前会话或用户已配置的凭据获取。

- 上传：`POST /knowledge-bases/{id}/knowledge/file`，multipart 字段 `file`；标签为 `tag_ids`（逗号分隔），多模态字段拼写为 `enable_multimodel`。不要手动拼 multipart boundary。**HTTP 200 且知识记录创建即算"上传成功"，立即向用户报告；解析在后台异步进行，不要阻塞等待解析完成**（wiki_flow.py 的 upload 默认受理即返回，确需等待才显式传 `--wait-seconds N`）。
- 解析状态：`GET /knowledge/{id}`；区分两级报告——"上传成功"（受理即报，默认只报这一级并注明解析后台进行中）与"解析完成"（仅 `parse_status=completed` 且 `enable_status=enabled` 时才可报告"处理完成并已启用"）。仅在用户明确要求确认解析结果、或后续问答依赖该文件时才查解析：单次查 `GET /knowledge/{id}`，持续跟踪用 wait 子命令；不要默认长轮询。失败读取 `error_message`；等待超时不等于解析失败。
- 查库：`GET /knowledge-bases`、`GET /knowledge-bases/{id}`；查文件清单：`GET /knowledge-bases/{id}/knowledge`。
- 快速检索：`POST /knowledge-search` 使用 `query`，指定知识库 ID 或知识 ID 范围；`knowledge_base_id` 和 `knowledge_base_ids` 会合并去重。不要往此接口添加未声明的 `top_k` 或 `match_count`。
- 混合检索：`POST /knowledge-bases/{id}/hybrid-search` 使用 `query_text` 与 `match_count` 等参数；不要与快速检索的参数名混用。
- 权限：上传需要 `ingest`、读取与检索需要 `retrieve`、建库需要 `manage_kbs`、问答需要 `chat`，受限 API Key 还受知识库范围限制。

连接检查采用健康检查及任务相关的读取接口；用户仅要求检查连接时，不通过上传、建库或删除来测试。用户已要求的写操作按其范围执行。

## 建库默认

- **默认创建纯 Wiki 知识库**：`indexing_strategy` 仅 `wiki_enabled=true`（vector/keyword/graph 全关），其余配置按 [纯 Wiki 操作流程](references/pure-wiki.md) 的建库请求体；用户明确要 RAG/向量/关键词类时才按对应类型建。
- **默认模型取本地环境默认模型**：用户未指定模型时，读本地 opencode 配置 `~/.config/opencode/opencode.jsonc`（或 `.json`）：顶层 `model` 字段（形如 `provider/模型名`）+ 对应 `provider.<provider>.options.baseURL` / `apiKey`（OpenAI 兼容接口）；模型名取 `/` 后的部分（端点上的 model id）。
- 绑定前先 `GET /models` 查同名**且 type=`KnowledgeQA`** 的模型；未注册则先 `POST /models`（type=`KnowledgeQA`，source=`remote`，provider=`generic`，parameters: `base_url`/`api_key`），再用 `POST /initialization/remote/check` 验证 jingboWiki 侧出网连通。
- **同名不同 type 是不同槽位，不能混用**：把 type=`VLLM` 的同名模型 id 传给 `PUT /initialization/config/{kb_id}` 的 `llmModelId`，会落到 `multimodal.vlm` 且 `llm` 配置整块消失（实测 `Qwen3.8-27B-FP8-Z`）。此时另注册一个 `KnowledgeQA` 型同名模型（允许重名）再绑；改绑正确 id 后 `multimodal` 自动恢复 `enabled:false`。智能体问答模型走 `PUT /agents/{id}` 的 `config.model_id`，同样只填 `KnowledgeQA` 型 id。
- 换绑后必须复核三处：`GET /initialization/config/{kb_id}` 的 `llm.modelName`、`GET /knowledge-bases/{kb_id}` 的 `summary_model_id`、`GET /agents/{id}` 的 `config.model_id`；并实问一轮确认 SSE 正常出答案，不能只看接口返回 success。
- apiKey 仅用于注册与调用，不输出到对话、不写入文档。
- 用户明确指定（模型 / 知识库类型 / 空间）时以指定为准，默认只兜底。

## 文档维护

输出以 jingboWiki 命名，只写接口路径、必要的认证、参数和请求响应。保留前三项顺序：上传知识库文件、查询知识库、快速检索；其他接口放在之后。不要加入 MCP、上游品牌、来源追溯或过程性说明。

接口参考是已整理的文档快照，并非全部接口已在服务上联调通过。遇到实际响应差异，按当前服务证据修正对应接口；不要把内部字段或真实路由为了品牌展示而随意改名。

接口文档随技能包保存在 `references/api.md`，直接维护此文件。本技能不依赖外部项目目录；所有资源路径均相对于技能根目录解析。

## 服务器凭据

服务器端登录凭据（`JINGBOWIKI_EMAIL`、`JINGBOWIKI_PASSWORD`）位于 `shared/secrets/jingbowiki.env`（权限 600）。使用前先 `source shared/secrets/jingbowiki.env` 加载环境变量，再按变量取值调用接口。绝不把该文件内容复制到其他位置、写入日志或输出到对话中；令牌与密码仅用于当前调用。
