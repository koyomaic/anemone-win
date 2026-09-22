# jingboWiki 接口文档

API 基础地址：`http://10.200.3.235:18082/api/v1`  
接口前缀：`/api/v1`  
认证请求头：`X-API-Key: <API_KEY>`；登录态接口使用 `Authorization: Bearer <TOKEN>`，登录等公开接口无需认证。
路径中的 `{id}` 等占位符须替换为实际 ID。

## 1. 上传知识库文件

**POST `/api/v1/knowledge-bases/{id}/knowledge/file`**

路径参数：`id` 为知识库 ID。请求类型：`multipart/form-data`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | 上传文件 |
| `fileName` | string | 否 | 自定义文件名 |
| `tag_ids` | string | 否 | 标签 ID，多个以逗号分隔 |
| `enable_multimodel` | string | 否 | 是否启用多模态解析：`true` / `false` |
| `metadata` | string | 否 | JSON 字符串，键值均为字符串 |
| `channel` | string | 否 | 来源渠道，如 `api` |

请求示例：

```bash
curl 'http://10.200.3.235:18082/api/v1/knowledge-bases/<知识库ID>/knowledge/file' \
  -H 'X-API-Key: <API_KEY>' \
  -F 'file=@/data/采购管理办法.pdf' \
  -F 'channel=api'
```

响应示例（HTTP 200，主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "<知识ID>",
    "knowledge_base_id": "<知识库ID>",
    "file_name": "采购管理办法.pdf",
    "parse_status": "processing",
    "enable_status": "disabled"
  }
}
```

上传后异步解析，通过接口 2.4 查询状态。`parse_status=completed` 且 `enable_status=enabled` 表示处理完成并已启用。

## 2. 查询知识库

### 2.1 获取知识库列表

**GET `/api/v1/knowledge-bases`**

无需请求参数。

```bash
curl 'http://10.200.3.235:18082/api/v1/knowledge-bases' \
  -H 'X-API-Key: <API_KEY>'
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "<知识库ID>",
      "name": "采购制度",
      "description": "采购管理制度与流程",
      "type": "document",
      "knowledge_count": 12
    }
  ]
}
```

### 2.2 获取知识库详情

**GET `/api/v1/knowledge-bases/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | path | string | 是 | 知识库 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "<知识库ID>",
    "name": "采购制度",
    "description": "采购管理制度与流程",
    "type": "document",
    "knowledge_count": 12,
    "chunk_count": 156,
    "processing_count": 0
  }
}
```

### 2.3 获取知识库内文件列表

**GET `/api/v1/knowledge-bases/{id}/knowledge`**

路径参数：`id` 为知识库 ID。

| Query 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | 否 | 页码，默认 1 |
| `page_size` | integer | 否 | 每页条数，默认 20 |
| `keyword` | string | 否 | 查询关键词 |
| `file_type` | string | 否 | 文件类型，如 `pdf`、`docx` |
| `parse_status` | string | 否 | 解析状态，如 `completed` |

```bash
curl --get 'http://10.200.3.235:18082/api/v1/knowledge-bases/<知识库ID>/knowledge' \
  -H 'X-API-Key: <API_KEY>' \
  --data-urlencode 'page=1' \
  --data-urlencode 'page_size=20' \
  --data-urlencode 'keyword=采购'
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "<知识ID>",
      "knowledge_base_id": "<知识库ID>",
      "file_name": "采购管理办法.pdf",
      "parse_status": "completed",
      "enable_status": "enabled"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### 2.4 获取文件详情及解析状态

**GET `/api/v1/knowledge/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | path | string | 是 | 上传接口返回的知识 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "<知识ID>",
    "knowledge_base_id": "<知识库ID>",
    "file_name": "采购管理办法.pdf",
    "parse_status": "completed",
    "enable_status": "enabled",
    "error_message": ""
  }
}
```

`parse_status`：`pending` 等待、`processing` 处理中、`finalizing` 后处理、`completed` 完成、`failed` 失败、`cancelled` 已取消。失败原因见 `error_message`。

## 3. 快速检索

### 3.1 搜索知识内容

**POST `/api/v1/knowledge-search`**

请求类型：`application/json`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 查询文本 |
| `knowledge_base_ids` | string[] | 条件必填 | 检索的知识库 ID 列表 |
| `knowledge_ids` | string[] | 条件必填 | 检索的文件知识 ID 列表 |
| `knowledge_base_id` | string | 否 | 单个知识库 ID，与 `knowledge_base_ids` 合并去重 |

知识库 ID 或知识 ID 至少指定一种。

```bash
curl 'http://10.200.3.235:18082/api/v1/knowledge-search' \
  -H 'X-API-Key: <API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "采购合同的审批流程是什么？",
    "knowledge_base_ids": ["<知识库ID>"]
  }'
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "<分块ID>",
      "content": "采购合同按审批权限履行审批程序……",
      "knowledge_id": "<知识ID>",
      "knowledge_title": "采购管理办法",
      "knowledge_filename": "采购管理办法.pdf",
      "score": 0.86
    }
  ]
}
```

返回命中的正文片段，不生成问答总结。`score` 为检索排序得分。

### 3.2 混合检索

**POST `/api/v1/knowledge-bases/{id}/hybrid-search`**

路径参数：`id` 为知识库 ID。请求类型：`application/json`。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `query_text` | string | 查询文本 |
| `match_count` | integer | 期望召回数量 |
| `vector_threshold` | number | 向量检索阈值 |
| `keyword_threshold` | number | 关键词检索阈值 |
| `disable_keywords_match` | boolean | 是否禁用关键词检索 |
| `disable_vector_match` | boolean | 是否禁用向量检索 |
| `knowledge_ids` | string[] | 可选，限定文件范围 |

请求示例：

```bash
curl 'http://10.200.3.235:18082/api/v1/knowledge-bases/<知识库ID>/hybrid-search' \
  -H 'X-API-Key: <API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "query_text": "采购审批流程",
    "match_count": 5,
    "vector_threshold": 0.5,
    "keyword_threshold": 0.3,
    "disable_keywords_match": false,
    "disable_vector_match": false
  }'
```

响应为 `{"success":true,"data":[...]}`，`data` 包含命中的分块内容、知识 ID 和检索得分。

## 4. 知识库管理

### 4.1 创建知识库

**POST `/api/v1/knowledge-bases`**

| 字段                          | 类型    | 必填 | 说明                                                            |
| ----------------------------- | ------- | ---- | --------------------------------------------------------------- |
| name                          | string  | 是   | 知识库名称                                                      |
| description                   | string  | 否   | 知识库描述                                                      |
| type                          | string  | 否   | 知识库类型：`document`（默认）或 `faq`                          |
| is_temporary                  | boolean | 否   | 是否为临时知识库（默认 `false`，临时库通常不在 UI 列表中显示）  |
| chunking_config               | object  | 否   | 分块配置（见下方示例）                                          |
| image_processing_config       | object  | 否   | 图片处理配置                                                    |
| embedding_model_id            | string  | 否   | Embedding 模型 ID                                               |
| summary_model_id              | string  | 否   | 摘要模型 ID                                                     |
| vlm_config                    | object  | 否   | VLM（视觉模型）配置                                             |
| asr_config                    | object  | 否   | ASR（语音识别）配置                                             |
| storage_provider_config       | object  | 否   | 存储提供者选择，如 `{"provider": "local"}`                      |
| storage_config                | object  | 否   | 旧版 COS 存储凭证（兼容字段，新集成留空即可）                   |
| extract_config                | object  | 否   | 图谱抽取配置；`enabled=true` 时需提供 `text`/`tags`/`nodes`/`relations` |
| faq_config                    | object  | 否   | FAQ 配置（仅 FAQ 类型知识库需要）                               |
| question_generation_config    | object  | 否   | 问题生成配置                                                    |
| vector_store_id               | string  | 否   | 绑定的向量存储 ID。不传或为空字符串等同于 `null`（使用环境变量默认存储）。指定时必须是调用者所在空间拥有的向量存储 UUID；创建后不可修改。无效 UUID / 跨空间 / 未注册到引擎的 ID 会返回 `400` |

请求示例：

```json
{
  "name": "jingboWiki",
  "description": "jingboWiki description",
  "type": "document",
  "is_temporary": false,
  "chunking_config": {
    "chunk_size": 1000,
    "chunk_overlap": 200,
    "separators": [
      "."
    ],
    "enable_multimodal": true,
    "parser_engine_rules": [
      {
        "file_types": [
          ".pdf"
        ],
        "engine": "builtin"
      }
    ],
    "enable_parent_child": false,
    "parent_chunk_size": 4096,
    "child_chunk_size": 384
  },
  "image_processing_config": {
    "model_id": "f2083ad7-63e3-486d-a610-e6c56e58d72e"
  },
  "embedding_model_id": "dff7bc94-7885-4dd1-bfd5-bd96e4df2fc3",
  "summary_model_id": "8aea788c-bb30-4898-809e-e40c14ffb48c",
  "vlm_config": {
    "enabled": true,
    "model_id": "f2083ad7-63e3-486d-a610-e6c56e58d72e"
  },
  "asr_config": {
    "enabled": false,
    "model_id": "",
    "language": ""
  }
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "b5829e4a-3845-4624-a7fb-ea3b35e843b0",
    "name": "jingboWiki",
    "description": "jingboWiki description",
    "type": "document",
    "is_temporary": false,
    "tenant_id": 1,
    "chunking_config": {
      "chunk_size": 1000,
      "chunk_overlap": 200,
      "separators": [
        "."
      ],
      "enable_multimodal": true,
      "parser_engine_rules": [
        {
          "file_types": [
            ".pdf"
          ],
          "engine": "builtin"
        }
      ],
      "enable_parent_child": false,
      "parent_chunk_size": 4096,
      "child_chunk_size": 384
    },
    "image_processing_config": {
      "model_id": "f2083ad7-63e3-486d-a610-e6c56e58d72e"
    }
  },
  "success": true
}
```

### 4.2 更新知识库

**PUT `/api/v1/knowledge-bases/{id}`**

| 字段 | 类型   | 说明      |
| ---- | ------ | --------- |
| id   | string | 知识库 ID |

| 字段        | 类型   | 必填 | 说明                                                          |
| ----------- | ------ | ---- | ------------------------------------------------------------- |
| name        | string | 是   | 知识库名称                                                    |
| description | string | 否   | 知识库描述                                                    |
| config      | object | 否   | 更新配置；包含 `chunking_config` / `image_processing_config` / `faq_config` / `wiki_config` / `indexing_strategy` |

请求示例：

```json
{
  "name": "jingboWiki new",
  "description": "jingboWiki description new",
  "config": {
    "chunking_config": {
      "chunk_size": 1000,
      "chunk_overlap": 200,
      "separators": [
        "\n\n"
      ],
      "enable_multimodal": true,
      "parser_engine_rules": [
        {
          "file_types": [
            ".md"
          ],
          "engine": "builtin"
        }
      ],
      "enable_parent_child": true,
      "parent_chunk_size": 4096,
      "child_chunk_size": 384
    },
    "image_processing_config": {
      "model_id": ""
    }
  }
}
```

响应：200：更新后的知识库。

### 4.3 删除知识库

**DELETE `/api/v1/knowledge-bases/{id}`**

| 字段 | 类型   | 说明      |
| ---- | ------ | --------- |
| id   | string | 知识库 ID |

响应示例（主要字段）：

```json
{
  "message": "Knowledge base deleted successfully",
  "success": true
}
```

### 4.4 置顶/取消置顶知识库

**PUT `/api/v1/knowledge-bases/{id}/pin`**

| 字段 | 类型   | 说明      |
| ---- | ------ | --------- |
| id   | string | 知识库 ID |

响应：200：更新后的知识库。

### 4.5 拷贝知识库

**POST `/api/v1/knowledge-bases/copy`**

| 字段       | 类型   | 必填 | 说明                                                          |
| ---------- | ------ | ---- | ------------------------------------------------------------- |
| source_id  | string | 是   | 源知识库 ID（必须属于当前空间）                               |
| target_id  | string | 否   | 目标知识库 ID（若复用已存在知识库；同样必须属于当前空间）     |
| task_id    | string | 否   | 自定义任务 ID；不传则由服务端生成（基于空间、源 ID、时间戳）  |

请求示例：

```json
{
  "source_id": "kb-00000001"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "task_id": "kb_clone_1_kb-00000001_1736582400",
    "source_id": "kb-00000001",
    "target_id": "",
    "message": "Knowledge base copy task started"
  },
  "success": true
}
```

### 4.6 获取拷贝进度

**GET `/api/v1/knowledge-bases/copy/progress/{task_id}`**

| 字段    | 类型   | 说明                                              |
| ------- | ------ | ------------------------------------------------- |
| task_id | string | 由 `POST /knowledge-bases/copy` 返回的任务 ID     |

响应示例（主要字段）：

```json
{
  "data": {
    "task_id": "kb_clone_1_kb-00000001_1736582400",
    "source_id": "kb-00000001",
    "target_id": "kb-00000002",
    "status": "completed",
    "progress": 100,
    "total": 10,
    "processed": 10,
    "message": "Task completed successfully"
  },
  "success": true
}
```

### 4.7 创建知识库副本

**POST `/api/v1/knowledge-bases/{id}/duplicate`**

| 字段 | 类型   | 说明        |
| ---- | ------ | ----------- |
| id   | string | 源知识库 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "source_id": "kb-00000001",
    "target_id": "kb-00000002",
    "message": "Knowledge base duplicate created",
    "knowledge_base": {
      "id": "kb-00000002",
      "name": "产品文档 副本",
      "type": "document",
      "description": "…",
      "embedding_model_id": "embed-1",
      "chunking_config": {},
      "knowledge_count": 0,
      "chunk_count": 0
    }
  }
}
```

### 4.8 获取可迁移目标知识库列表

**GET `/api/v1/knowledge-bases/{id}/move-targets`**

| 字段 | 类型   | 说明          |
| ---- | ------ | ------------- |
| id   | string | 源知识库 ID   |

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "kb-00000002",
      "name": "技术文档知识库",
      "description": "技术文档相关知识",
      "type": "document",
      "is_temporary": false,
      "tenant_id": 1,
      "chunking_config": {
        "chunk_size": 1000,
        "chunk_overlap": 200,
        "separators": [
          "\n\n"
        ],
        "enable_multimodal": true,
        "parser_engine_rules": [],
        "enable_parent_child": false,
        "parent_chunk_size": 4096,
        "child_chunk_size": 384
      },
      "image_processing_config": {
        "model_id": ""
      }
    }
  ],
  "success": true
}
```

### 4.9 混合搜索

**GET `/api/v1/knowledge-bases/{id}/hybrid-search`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 知识库ID |
| disable_keywords_match | body | boolean | 否 |  |
| disable_vector_match | body | boolean | 否 |  |
| keyword_threshold | body | number | 否 |  |
| knowledge_base_ids | body | array | 否 | KnowledgeBaseIDs overrides the single KB ID passed to HybridSearch, |
| knowledge_ids | body | array | 否 |  |
| match_count | body | integer | 否 |  |
| only_recommended | body | boolean | 否 |  |
| query_embedding | body | array | 否 |  |
| query_text | body | string | 否 |  |
| scope_tag_ids | body | array | 否 |  |
| skip_context_enrichment | body | boolean | 否 | SkipContextEnrichment skips fetching parent, nearby, and relation chunks |
| tag_ids | body | array | 否 | Tag IDs for filtering (used for FAQ priority filtering) |
| vector_threshold | body | number | 否 |  |

响应：200：搜索结果。


## 5. 知识文件管理

### 5.1 从 URL 创建知识

**POST `/api/v1/knowledge-bases/{id}/knowledge/url`**

| 字段                | 类型    | 必填 | 说明                                              |
| ------------------- | ------- | ---- | ------------------------------------------------- |
| `url`               | string  | 是   | 目标 URL                                          |
| `file_name`         | string  | 否   | 显式指定文件名，强制走文件下载模式                |
| `file_type`         | string  | 否   | 显式指定文件类型（如 `pdf`、`docx`）              |
| `enable_multimodel` | boolean | 否   | 是否启用多模态解析                                |
| `title`             | string  | 否   | 自定义标题                                        |
| `tag_id`            | string  | 否   | 标签 ID                                           |
| `channel`           | string  | 否   | 来源渠道标识                                      |

请求示例：

```json
{
  "url": "https://example.com",
  "enable_multimodel": true
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "9c8af585-ae15-44ce-8f73-45ad18394651",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "type": "url",
    "title": "",
    "description": "",
    "source": "https://example.com",
    "channel": "web"
  },
  "success": true
}
```

### 5.2 创建手工 Markdown 知识

**POST `/api/v1/knowledge-bases/{id}/knowledge/manual`**

| 字段      | 类型   | 必填 | 说明                                                |
| --------- | ------ | ---- | --------------------------------------------------- |
| `title`   | string | 是   | 标题                                                |
| `content` | string | 是   | Markdown 正文                                       |
| `status`  | string | 否   | 草稿/发布等业务状态（草稿不会触发解析）             |
| `tag_id`  | string | 否   | 标签 ID                                             |
| `channel` | string | 否   | 来源渠道标识                                        |

请求示例：

```json
{
  "title": "产品使用指南",
  "content": "# 产品使用指南\n\n## 快速入门\n\n这是一份产品使用指南...",
  "status": "published",
  "tag_id": "tag-00000001"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "5a3b2c1d-0e9f-4a8b-7c6d-5e4f3a2b1c0d",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "type": "manual",
    "title": "产品使用指南",
    "description": "",
    "source": "",
    "channel": "web"
  },
  "success": true
}
```

### 5.3 清空知识库下的所有知识

**DELETE `/api/v1/knowledge-bases/{id}/knowledge`**

参数：`id`（path，必填）：知识库ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Knowledge base contents clear task submitted",
  "data": {
    "deleted_count": 42
  }
}
```

### 5.4 批量获取知识

**GET `/api/v1/knowledge/batch`**

| 字段        | 类型     | 必填 | 说明                                                                  |
| ----------- | -------- | ---- | --------------------------------------------------------------------- |
| `ids`       | string[] | 是   | 知识 ID，重复 `ids=...` 传多个                                        |
| `kb_id`     | string   | 否   | 限定知识库范围；共享知识库场景下用于按 KB 校验权限并解析有效空间       |
| `agent_id`  | string   | 否   | 共享 Agent ID；按 Agent 所属空间拉取，常用于共享场景刷新后的文件回填   |

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "9c8af585-ae15-44ce-8f73-45ad18394651",
      "tenant_id": 1,
      "knowledge_base_id": "kb-00000001",
      "type": "url",
      "title": "",
      "source": "https://example.com",
      "parse_status": "pending",
      "enable_status": "disabled"
    }
  ],
  "success": true
}
```

### 5.5 更新知识

**PUT `/api/v1/knowledge/{id}`**

请求示例：

```json
{
  "title": "彗星 - 天文百科",
  "description": "彗星条目，已校对",
  "tag_id": "tag-00000001",
  "enable_status": "enabled"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Knowledge chunk updated successfully"
}
```

### 5.6 删除单条知识

**DELETE `/api/v1/knowledge/{id}`**

参数：`id`（path，必填）：知识ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Deleted successfully"
}
```

### 5.7 更新手工 Markdown 知识

**PUT `/api/v1/knowledge/manual/{id}`**

请求示例：

```json
{
  "title": "产品使用指南 V2",
  "content": "# 产品使用指南 V2\n\n## 更新内容\n\n..."
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "5a3b2c1d-0e9f-4a8b-7c6d-5e4f3a2b1c0d",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "type": "manual",
    "title": "产品使用指南 V2",
    "parse_status": "processing",
    "enable_status": "enabled",
    "created_at": "2025-08-12T12:00:00.000000+08:00"
  },
  "success": true
}
```

### 5.8 重新解析知识

**POST `/api/v1/knowledge/{id}/reparse`**

参数：`id`（path，必填）：知识ID；`body`（body，可选）：可选的处理配置覆盖：{\。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Knowledge reparse task submitted",
  "data": {
    "id": "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "type": "file",
    "title": "彗星.txt",
    "parse_status": "pending",
    "enable_status": "enabled",
    "created_at": "2025-08-12T11:52:36.168632+08:00"
  }
}
```

### 5.9 取消解析

**POST `/api/v1/knowledge/{id}/cancel-parse`**

参数：`id`（path，必填）：知识ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Knowledge parse cancelled",
  "data": {
    "id": "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "type": "file",
    "title": "彗星.txt",
    "parse_status": "cancelled",
    "error_message": "用户已取消解析",
    "enable_status": "disabled"
  }
}
```

### 5.10 下载原始文件

**GET `/api/v1/knowledge/{id}/download`**

参数：`id`（path，必填）：知识ID。

响应：200：文件内容。

### 5.11 内联预览文件

**GET `/api/v1/knowledge/{id}/preview`**

参数：`id`（path，必填）：知识ID。

响应：200：文件内容。

### 5.12 更新分块图像信息

**PUT `/api/v1/knowledge/image/{id}/{chunk_id}`**

| 字段       | 类型   | 说明     |
| ---------- | ------ | -------- |
| `id`       | string | 知识 ID  |
| `chunk_id` | string | 分块 ID  |

| 字段         | 类型   | 必填 | 说明                                 |
| ------------ | ------ | ---- | ------------------------------------ |
| `image_info` | string | 是   | 图像信息（业务侧 JSON 字符串）       |

请求示例：

```json
{
  "image_info": "{\"description\":\"产品架构图\",\"alt_text\":\"jingboWiki 系统架构\"}"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Knowledge chunk image updated successfully"
}
```

### 5.13 批量更新知识标签

**PUT `/api/v1/knowledge/tags`**

| 字段      | 类型                        | 必填 | 说明                                                                       |
| --------- | --------------------------- | ---- | -------------------------------------------------------------------------- |
| `updates` | object<string, []string\|null> | 是   | 知识 ID → 标签 ID **数组**的映射；值为 `null`/`[]` 表示清除该条知识的标签   |
| `kb_id`   | string                      | 否   | 限定知识库范围；指定时按该 KB 校验编辑权限（共享 KB 场景必填）             |

> ⚠️ **实测坑（2026-09-22，服务端 jingboWiki）**：`updates` 的值必须是**字符串数组**，
> 不能是裸字符串。传 `{"<知识ID>": "tag-xxx"}` 会返回
> `400 {"error":{"code":1000,"details":"json: cannot unmarshal string into Go struct field knowledgeTagBatchRequest.updates of type []string"}}`。
> 本表早期版本按 `object<string, string|null>` 记录，是错的。
>
> 另：`GET /knowledge-bases/{id}/knowledge` 返回的条目里，标签字段是 **`tags`（对象数组，
> 含 `id`/`name`/`color`/`sort_order`）**，不是 `tag_id`；按 `tag_id` 取值会恒为 `None`，
> 容易误判成"没打上标签"。`PUT /knowledge/{id}` 的单条接口才用 `tag_id` 单值。

请求示例：

```json
{
  "kb_id": "kb-00000001",
  "updates": {
    "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5": ["tag-00000001"],
    "9c8af585-ae15-44ce-8f73-45ad18394651": null
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 5.14 跨知识库搜索/过滤知识

**GET `/api/v1/knowledge/search`**

| 字段         | 类型    | 默认 | 说明                                                                  |
| ------------ | ------- | ---- | --------------------------------------------------------------------- |
| `keyword`    | string  | -    | 关键词（可选）                                                       |
| `offset`     | integer | 0    | 偏移量                                                                |
| `limit`      | integer | 20   | 返回条数                                                              |
| `file_types` | string  | -    | 逗号分隔的扩展名列表，例如 `txt,pdf,docx`                            |
| `agent_id`   | string  | -    | 共享 Agent ID；按该 Agent 的 KB 选择模式（`all`/`selected`/`none`）限定范围 |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5",
      "tenant_id": 1,
      "knowledge_base_id": "kb-00000001",
      "type": "file",
      "title": "彗星.txt",
      "description": "彗星是由冰和尘埃构成的太阳系小天体...",
      "file_name": "彗星.txt",
      "file_type": "txt"
    }
  ],
  "has_more": false
}
```

### 5.15 同一知识库内批量删除

**POST `/api/v1/knowledge/batch-delete`**

| 字段    | 类型     | 必填 | 说明                              |
| ------- | -------- | ---- | --------------------------------- |
| `kb_id` | string   | 是   | 目标知识库 ID                     |
| `ids`   | string[] | 是   | 待删除的知识 ID 列表（≤ 200）     |

请求示例：

```json
{
  "kb_id": "kb-00000001",
  "ids": [
    "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5"
  ]
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Batch delete task submitted",
  "data": {
    "task_id": "kg_delete_1_kb-00000001_xxxx",
    "deleted_count": 2
  }
}
```

### 5.16 迁移知识到另一知识库

**POST `/api/v1/knowledge/move`**

| 字段            | 类型     | 必填 | 说明                                                                            |
| --------------- | -------- | ---- | ------------------------------------------------------------------------------- |
| `knowledge_ids` | string[] | 是   | 待迁移的知识 ID 列表（至少 1 个）                                              |
| `source_kb_id`  | string   | 是   | 源知识库 ID                                                                     |
| `target_kb_id`  | string   | 是   | 目标知识库 ID                                                                   |
| `mode`          | string   | 是   | 迁移模式：`reuse_vectors`（复用向量数据，零成本） / `reparse`（在目标库重新解析） |

请求示例：

```json
{
  "knowledge_ids": [
    "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5"
  ],
  "source_kb_id": "kb-00000001",
  "target_kb_id": "kb-00000002",
  "mode": "reuse_vectors"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "task_id": "kg_move_1_kb-00000001_xxxx",
    "source_kb_id": "kb-00000001",
    "target_kb_id": "kb-00000002",
    "knowledge_count": 1,
    "message": "Knowledge move task started"
  }
}
```

### 5.17 查询迁移进度

**GET `/api/v1/knowledge/move/progress/{task_id}`**

参数：`task_id`（path，必填）：移动任务 ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "task_id": "kg_move_1_kb-00000001_xxxx",
    "source_kb_id": "kb-00000001",
    "target_kb_id": "kb-00000002",
    "status": "completed",
    "progress": 100,
    "total": 1,
    "processed": 1,
    "failed": 0
  }
}
```

### 5.18 获取知识文档解析的 Span 树（含历史尝试）

**GET `/api/v1/knowledge/{id}/spans`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 知识ID |
| attempt | query | integer | 否 | 指定尝试号；省略=最新 |

响应：200：OK。

### 5.19 批量重新解析知识

**POST `/api/v1/knowledge/batch-reparse`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| ids | body | array | 是 |  |
| kb_id | body | string | 是 |  |
| process_config | body | object | 否 |  |

响应：200：任务已提交。


## 6. 分块管理

### 6.1 获取知识的分块列表

**GET `/api/v1/chunks/{knowledge_id}`**

| 字段          | 类型   | 说明        |
| ------------- | ------ | ----------- |
| knowledge_id  | string | 知识 ID     |

| 字段       | 类型 | 默认 | 说明       |
| ---------- | ---- | ---- | ---------- |
| page       | int  | 1    | 页码       |
| page_size  | int  | 20   | 每页条数   |

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "df10b37d-cd05-4b14-ba8a-e1bd0eb3bbd7",
      "tenant_id": 1,
      "knowledge_id": "4c4e7c1a-09cf-485b-a7b5-24b8cdc5acf5",
      "knowledge_base_id": "kb-00000001",
      "tag_id": "",
      "content": "彗星xxxx",
      "chunk_index": 0,
      "is_enabled": true
    }
  ],
  "page": 1,
  "page_size": 1,
  "success": true,
  "total": 5
}
```

### 6.2 更新分块

**PUT `/api/v1/chunks/{knowledge_id}/{id}`**

| 字段          | 类型   | 说明        |
| ------------- | ------ | ----------- |
| knowledge_id  | string | 知识 ID     |
| id            | string | 分块 ID     |

| 字段         | 类型    | 必填 | 说明                  |
| ------------ | ------- | ---- | --------------------- |
| content      | string  | 否   | 分块内容               |
| chunk_index  | int     | 否   | 分块在知识中的序号     |
| is_enabled   | boolean | 否   | 是否启用               |
| start_at     | int     | 否   | 起始位置（字符偏移）   |
| end_at       | int     | 否   | 结束位置（字符偏移）   |
| image_info   | string  | 否   | 图像分块的元信息（JSON 字符串） |

请求示例：

```json
{
  "content": "更新后的分块内容",
  "is_enabled": true
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "df10b37d-cd05-4b14-ba8a-e1bd0eb3bbd7",
    "content": "更新后的分块内容",
    "is_enabled": true,
    "...": "其他字段同 GET 响应"
  },
  "success": true
}
```

### 6.3 删除单个分块

**DELETE `/api/v1/chunks/{knowledge_id}/{id}`**

参数：`knowledge_id`（path，必填）：知识ID；`id`（path，必填）：分块ID。

响应示例（主要字段）：

```json
{
  "message": "Chunk deleted",
  "success": true
}
```

### 6.4 删除知识下的所有分块

**DELETE `/api/v1/chunks/{knowledge_id}`**

| 字段          | 类型   | 说明        |
| ------------- | ------ | ----------- |
| knowledge_id  | string | 知识 ID     |

响应示例（主要字段）：

```json
{
  "message": "All chunks under knowledge deleted",
  "success": true
}
```

### 6.5 根据 ID 直接获取分块

**GET `/api/v1/chunks/by-id/{id}`**

| 字段 | 类型   | 说明    |
| ---- | ------ | ------- |
| id   | string | 分块 ID |

响应：200：分块详情。

### 6.6 删除分块下的某个生成问题

**DELETE `/api/v1/chunks/by-id/{id}/questions`**

| 字段 | 类型   | 说明    |
| ---- | ------ | ------- |
| id   | string | 分块 ID |

| 字段        | 类型   | 必填 | 说明        |
| ----------- | ------ | ---- | ----------- |
| question_id | string | 是   | 问题 ID     |

请求示例：

```json
{
  "question_id": "q-00000001"
}
```

响应示例（主要字段）：

```json
{
  "message": "Question deleted successfully",
  "success": true
}
```

### 6.7 预览分块结果

**POST `/api/v1/chunker/preview`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| chunking_config | body | object | 否 |  |
| text | body | string | 否 |  |

响应：200：分块结果。


## 7. 标签管理

### 7.1 获取知识库标签列表

**GET `/api/v1/knowledge-bases/{id}/tags`**

参数：`id`（path，必填）：知识库ID；`page`（query，可选）：页码；`page_size`（query，可选）：每页数量；`keyword`（query，可选）：关键词搜索。

响应示例（主要字段）：

```json
{
  "data": {
    "total": 2,
    "page": 1,
    "page_size": 10,
    "data": [
      {
        "id": "tag-00000001",
        "tenant_id": 1,
        "knowledge_base_id": "kb-00000001",
        "name": "技术文档",
        "color": "#1890ff",
        "sort_order": 1,
        "created_at": "2025-08-12T10:00:00+08:00",
        "updated_at": "2025-08-12T10:00:00+08:00"
      }
    ]
  },
  "success": true
}
```

### 7.2 创建标签

**POST `/api/v1/knowledge-bases/{id}/tags`**

| 字段 | 类型   | 说明        |
| ---- | ------ | ----------- |
| id   | string | 知识库 ID    |

| 字段       | 类型   | 必填 | 说明                     |
| ---------- | ------ | ---- | ------------------------ |
| name       | string | 是   | 标签名（同库内唯一）      |
| color      | string | 否   | 标签颜色（CSS 颜色字符串） |
| sort_order | int    | 否   | 排序值（数值越小越靠前）   |

请求示例：

```json
{
  "name": "产品手册",
  "color": "#faad14",
  "sort_order": 3
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "tag-00000003",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "name": "产品手册",
    "color": "#faad14",
    "sort_order": 3,
    "created_at": "2025-08-12T11:00:00+08:00",
    "updated_at": "2025-08-12T11:00:00+08:00"
  },
  "success": true
}
```

### 7.3 更新标签

**PUT `/api/v1/knowledge-bases/{id}/tags/{tag_id}`**

| 字段   | 类型   | 说明        |
| ------ | ------ | ----------- |
| id     | string | 知识库 ID    |
| tag_id | string | 标签 ID      |

请求示例：

```json
{
  "name": "产品手册更新",
  "color": "#ff4d4f"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "tag-00000003",
    "tenant_id": 1,
    "knowledge_base_id": "kb-00000001",
    "name": "产品手册更新",
    "color": "#ff4d4f",
    "sort_order": 3,
    "created_at": "2025-08-12T11:00:00+08:00",
    "updated_at": "2025-08-12T11:30:00+08:00"
  },
  "success": true
}
```

### 7.4 删除标签

**DELETE `/api/v1/knowledge-bases/{id}/tags/{tag_id}`**

| 字段   | 类型   | 说明     |
| ------ | ------ | -------- |
| id     | string | 知识库 ID |
| tag_id | string | 标签 ID   |

| 字段  | 类型    | 默认  | 说明                                          |
| ----- | ------- | ----- | --------------------------------------------- |
| force | boolean | false | 设置为 `true` 时强制删除（即使标签被引用）     |

响应示例（主要字段）：

```json
{
  "success": true
}
```


## 8. FAQ 管理

### 8.1 获取 FAQ 条目列表

**GET `/api/v1/knowledge-bases/{id}/faq/entries`**

| 参数         | 类型   | 必填 | 说明                                                                                          |
| ------------ | ------ | ---- | --------------------------------------------------------------------------------------------- |
| page         | int    | 否   | 页码，默认 1                                                                                  |
| page_size    | int    | 否   | 每页数量，默认 20                                                                             |
| tag_id       | int    | 否   | 按标签 `seq_id` 过滤                                                                          |
| keyword      | string | 否   | 关键字搜索                                                                                    |
| search_field | string | 否   | 搜索字段：`standard_question` / `similar_questions` / `answers`，留空则全字段搜索              |
| sort_order   | string | 否   | 排序方式，`asc` 表示按更新时间正序，默认按更新时间倒序                                          |

响应示例（主要字段）：

```json
{
  "data": {
    "total": 100,
    "page": 1,
    "page_size": 10,
    "data": [
      {
        "id": 1,
        "chunk_id": "chunk-00000001",
        "knowledge_id": "knowledge-00000001",
        "knowledge_base_id": "kb-00000001",
        "tag_id": 12,
        "tag_name": "账户",
        "is_enabled": true,
        "is_recommended": false
      }
    ]
  },
  "success": true
}
```

### 8.2 导出 FAQ 条目

**GET `/api/v1/knowledge-bases/{id}/faq/entries/export`**

参数：`id`（path，必填）：知识库ID。

响应：200：CSV文件。

### 8.3 获取单个 FAQ 条目

**GET `/api/v1/knowledge-bases/{id}/faq/entries/{entry_id}`**

参数：`id`（path，必填）：知识库ID；`entry_id`（path，必填）：FAQ条目ID(seq_id)。

响应示例（主要字段）：

```json
{
  "data": {
    "id": 1,
    "chunk_id": "chunk-00000001",
    "knowledge_id": "knowledge-00000001",
    "knowledge_base_id": "kb-00000001",
    "tag_id": 12,
    "tag_name": "账户",
    "is_enabled": true,
    "is_recommended": false
  },
  "success": true
}
```

### 8.4 批量 Upsert FAQ 条目（异步）

**POST `/api/v1/knowledge-bases/{id}/faq/entries`**

| 字段         | 类型                       | 必填 | 说明                                                                                |
| ------------ | -------------------------- | ---- | ----------------------------------------------------------------------------------- |
| entries      | `[]FAQEntryPayload`        | 是   | FAQ 条目数组                                                                        |
| mode         | string                     | 是   | `append` 或 `replace`（替换会清空已有条目）                                          |
| knowledge_id | string                     | 否   | 关联的 FAQ Knowledge ID（不传则使用知识库默认 FAQ knowledge）                         |
| task_id      | string                     | 否   | 任务 ID，不传则自动生成 UUID                                                        |
| dry_run      | boolean                    | 否   | 仅验证不导入                                                                        |

| 字段                | 类型      | 必填 | 说明                                                          |
| ------------------- | --------- | ---- | ------------------------------------------------------------- |
| id                  | int64     | 否   | 指定 `seq_id`（数据迁移场景，需小于自增起始值 100000000）       |
| standard_question   | string    | 是   | 标准问                                                        |
| similar_questions   | string[]  | 否   | 相似问列表                                                    |
| negative_questions  | string[]  | 否   | 反例问题列表                                                  |
| answers             | string[]  | 否   | 答案列表                                                      |
| answer_strategy     | string    | 否   | 答案返回策略：`all` 或 `random`                                |
| tag_id              | int64     | 否   | 标签 `seq_id`                                                 |
| tag_name            | string    | 否   | 标签名（用于按名匹配标签）                                    |
| is_enabled          | boolean   | 否   | 是否启用                                                      |
| is_recommended      | boolean   | 否   | 是否推荐                                                      |

请求示例：

```json
{
  "mode": "append",
  "entries": [
    {
      "standard_question": "如何联系客服？",
      "similar_questions": [
        "客服电话"
      ],
      "answers": [
        "您可以通过拨打400-xxx-xxxx联系我们的客服。"
      ],
      "tag_id": 1
    }
  ]
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "task_id": "task-00000001"
  },
  "success": true
}
```

### 8.5 同步创建单个 FAQ 条目

**POST `/api/v1/knowledge-bases/{id}/faq/entry`**

请求示例：

```json
{
  "standard_question": "如何联系客服？",
  "similar_questions": [
    "客服电话"
  ],
  "answers": [
    "您可以通过拨打400-xxx-xxxx联系我们的客服。"
  ],
  "tag_id": 1,
  "is_enabled": true
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": 1,
    "chunk_id": "chunk-00000001",
    "knowledge_id": "knowledge-00000001",
    "knowledge_base_id": "kb-00000001",
    "tag_id": 1,
    "tag_name": "客服",
    "is_enabled": true,
    "is_recommended": false
  },
  "success": true
}
```

### 8.6 更新单个 FAQ 条目

**PUT `/api/v1/knowledge-bases/{id}/faq/entries/{entry_id}`**

请求示例：

```json
{
  "standard_question": "如何重置账户密码？",
  "similar_questions": [
    "忘记密码怎么办"
  ],
  "answers": [
    "您可以通过以下步骤重置密码：1. 点击登录页面的\"忘记密码\" 2. 输入注册邮箱 3. 查收重置邮件"
  ],
  "is_enabled": true
}
```

响应：200：更新成功。

### 8.7 追加相似问

**POST `/api/v1/knowledge-bases/{id}/faq/entries/{entry_id}/similar-questions`**

| 字段              | 类型     | 必填 | 说明                  |
| ----------------- | -------- | ---- | --------------------- |
| similar_questions | string[] | 是   | 要追加的相似问数组    |

请求示例：

```json
{
  "similar_questions": [
    "怎样修改密码"
  ]
}
```

响应：200：更新后的FAQ条目。

### 8.8 批量更新字段

**PUT `/api/v1/knowledge-bases/{id}/faq/entries/fields`**

| 字段        | 类型                              | 必填 | 说明                                       |
| ----------- | --------------------------------- | ---- | ------------------------------------------ |
| by_id       | `map[int64]FAQEntryFieldsUpdate`  | 否   | 按条目 `seq_id` 更新                       |
| by_tag      | `map[int64]FAQEntryFieldsUpdate`  | 否   | 按标签 `seq_id` 对该标签下所有条目更新     |
| exclude_ids | `int64[]`                         | 否   | 与 `by_tag` 配合使用，排除指定条目 `seq_id` |

| 字段           | 类型    | 说明           |
| -------------- | ------- | -------------- |
| is_enabled     | boolean | 是否启用       |
| is_recommended | boolean | 是否推荐       |
| tag_id         | int64   | 标签 `seq_id`  |

请求示例：

```json
{
  "by_id": {
    "1": {
      "is_enabled": true,
      "is_recommended": false
    },
    "2": {
      "is_enabled": false
    }
  },
  "by_tag": {
    "100": {
      "is_recommended": true
    }
  },
  "exclude_ids": [
    3
  ]
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 8.9 批量更新标签

**PUT `/api/v1/knowledge-bases/{id}/faq/entries/tags`**

| 字段    | 类型                  | 必填 | 说明                                         |
| ------- | --------------------- | ---- | -------------------------------------------- |
| updates | `map[int64]int64?`    | 是   | 键：条目 `seq_id`；值：标签 `seq_id` 或 `null` |

请求示例：

```json
{
  "updates": {
    "1": 10,
    "2": 11,
    "3": null
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 8.10 批量删除

**DELETE `/api/v1/knowledge-bases/{id}/faq/entries`**

| 字段 | 类型      | 必填 | 说明                              |
| ---- | --------- | ---- | --------------------------------- |
| ids  | `int64[]` | 是   | 要删除的 FAQ 条目 `seq_id` 列表    |

请求示例：

```json
{
  "ids": [
    1
  ]
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 8.11 FAQ 混合搜索

**POST `/api/v1/knowledge-bases/{id}/faq/search`**

| 字段                    | 类型      | 必填 | 说明                                                                                |
| ----------------------- | --------- | ---- | ----------------------------------------------------------------------------------- |
| query_text              | string    | 是   | 搜索文本                                                                            |
| vector_threshold        | float     | 否   | 向量相似度阈值（0–1）                                                               |
| match_count             | int       | 否   | 返回数量，默认 10，最大 200                                                         |
| first_priority_tag_ids  | `int64[]` | 否   | 第一优先级标签 `seq_id` 列表（最高优先召回范围）                                     |
| second_priority_tag_ids | `int64[]` | 否   | 第二优先级标签 `seq_id` 列表                                                        |
| only_recommended        | boolean   | 否   | 是否仅返回 `is_recommended=true` 的条目                                              |

请求示例：

```json
{
  "query_text": "如何重置密码",
  "vector_threshold": 0.5,
  "match_count": 10,
  "first_priority_tag_ids": [
    12
  ],
  "only_recommended": false
}
```

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": 1,
      "chunk_id": "chunk-00000001",
      "knowledge_id": "knowledge-00000001",
      "knowledge_base_id": "kb-00000001",
      "tag_id": 12,
      "tag_name": "账户",
      "is_enabled": true,
      "is_recommended": false
    }
  ],
  "success": true
}
```

### 8.12 更新上次导入结果显示状态

**PUT `/api/v1/knowledge-bases/{id}/faq/import/last-result/display`**

| 字段           | 类型   | 必填 | 说明                  |
| -------------- | ------ | ---- | --------------------- |
| display_status | string | 是   | `open` 或 `close`     |

请求示例：

```json
{
  "display_status": "close"
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 8.13 查询 FAQ 导入进度

**GET `/api/v1/faq/import/progress/{task_id}`**

| 参数    | 类型   | 说明           |
| ------- | ------ | -------------- |
| task_id | string | 导入任务的 ID  |

响应示例（主要字段）：

```json
{
  "data": {
    "task_id": "task-00000001",
    "kb_id": "kb-00000001",
    "knowledge_id": "knowledge-00000001",
    "status": "completed",
    "progress": 100,
    "total": 100,
    "processed": 100,
    "success_count": 95
  },
  "success": true
}
```


## 9. 智能体管理

### 9.1 创建智能体

**POST `/api/v1/agents`**

| 参数          | 类型   | 必填 | 说明                                              |
| ------------- | ------ | ---- | ------------------------------------------------- |
| `name`        | string | 是   | 智能体名称                                        |
| `description` | string | 否   | 智能体描述                                        |
| `avatar`      | string | 否   | 头像（emoji 或图标名称）                          |
| `config`      | object | 否   | 智能体配置，详见 配置参数            |

请求示例：

```json
{
  "name": "我的智能体",
  "description": "自定义智能体描述",
  "avatar": "🤖",
  "config": {
    "agent_mode": "smart-reasoning",
    "system_prompt": "你是一个专业的助手...",
    "temperature": 0.7,
    "max_iterations": 10,
    "kb_selection_mode": "all",
    "web_search_enabled": true,
    "multi_turn_enabled": true,
    "history_turns": 5
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "我的智能体",
    "description": "自定义智能体描述",
    "avatar": "🤖",
    "is_builtin": false,
    "tenant_id": 1,
    "created_by": "user-123",
    "config": {
      "agent_mode": "smart-reasoning",
      "system_prompt": "你是一个专业的助手...",
      "temperature": 0.7,
      "max_iterations": 10
    }
  }
}
```

### 9.2 获取智能体列表

**GET `/api/v1/agents`**

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "builtin-quick-answer",
      "name": "快速问答",
      "description": "基于知识库的 RAG 问答，快速准确地回答问题",
      "avatar": "💬",
      "is_builtin": true,
      "tenant_id": 10000,
      "created_by": "",
      "config": {
        "agent_mode": "quick-answer",
        "temperature": 0.3,
        "max_completion_tokens": 2048,
        "kb_selection_mode": "all",
        "web_search_enabled": false,
        "multi_turn_enabled": true,
        "history_turns": 5
      }
    }
  ],
  "disabled_own_agent_ids": []
}
```

### 9.3 获取智能体详情

**GET `/api/v1/agents/{id}`**

| 参数 | 类型   | 说明     |
| ---- | ------ | -------- |
| `id` | string | 智能体 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "builtin-quick-answer",
    "name": "快速问答",
    "description": "基于知识库的 RAG 问答，快速准确地回答问题",
    "is_builtin": true,
    "tenant_id": 1,
    "config": {
      "agent_mode": "quick-answer",
      "system_prompt": "",
      "context_template": "请根据以下参考资料回答用户问题...",
      "temperature": 0.7,
      "max_completion_tokens": 2048,
      "kb_selection_mode": "all",
      "web_search_enabled": true,
      "multi_turn_enabled": true
    },
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
}
```

### 9.4 更新智能体

**PUT `/api/v1/agents/{id}`**

| 参数 | 类型   | 说明     |
| ---- | ------ | -------- |
| `id` | string | 智能体 ID |

| 参数          | 类型   | 必填 | 说明         |
| ------------- | ------ | ---- | ------------ |
| `name`        | string | 否   | 智能体名称   |
| `description` | string | 否   | 智能体描述   |
| `avatar`      | string | 否   | 智能体头像   |
| `config`      | object | 否   | 智能体配置   |

请求示例：

```json
{
  "name": "更新后的智能体",
  "description": "更新后的描述",
  "config": {
    "agent_mode": "smart-reasoning",
    "temperature": 0.8,
    "max_iterations": 20
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "更新后的智能体",
    "description": "更新后的描述",
    "config": {
      "agent_mode": "smart-reasoning",
      "temperature": 0.8,
      "max_iterations": 20
    },
    "updated_at": "2025-01-19T11:00:00Z"
  }
}
```

### 9.5 删除智能体

**DELETE `/api/v1/agents/{id}`**

| 参数 | 类型   | 说明     |
| ---- | ------ | -------- |
| `id` | string | 智能体 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Agent deleted successfully"
}
```

### 9.6 复制智能体

**POST `/api/v1/agents/{id}/copy`**

| 参数 | 类型   | 说明           |
| ---- | ------ | -------------- |
| `id` | string | 源智能体 ID    |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "660e8400-e29b-41d4-a716-446655440001",
    "name": "智能推理 (副本)",
    "description": "ReAct 推理框架，支持多步思考和工具调用",
    "is_builtin": false,
    "config": {
      "agent_mode": "smart-reasoning",
      "max_iterations": 50
    },
    "created_at": "2025-01-19T12:00:00Z",
    "updated_at": "2025-01-19T12:00:00Z"
  }
}
```

### 9.7 获取占位符定义

**GET `/api/v1/agents/placeholders`**

响应：200：占位符定义。

### 9.8 获取智能体类型预设列表

**GET `/api/v1/agents/type-presets`**

响应：200：预设列表。

### 9.9 获取推荐问题

**GET `/api/v1/agents/{id}/suggested-questions`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 智能体ID |
| knowledge_base_ids | query | string | 否 | 知识库ID列表（逗号分隔），覆盖智能体默认配置 |
| knowledge_ids | query | string | 否 | 知识ID列表（逗号分隔），限定到具体文档 |
| tag_scopes | query | string | 否 | 带知识库归属的标签范围（JSON） |
| limit | query | integer | 否 | 返回数量上限（默认6） |

响应：200：推荐问题列表。


## 10. 会话管理

### 10.1 创建会话

**POST `/api/v1/sessions`**

| 字段          | 类型   | 必填 | 描述     |
| ------------- | ------ | ---- | -------- |
| `title`       | string | 否   | 会话标题 |
| `description` | string | 否   | 会话描述 |

请求示例：

```json
{
  "title": "我的新对话",
  "description": "关于 AI 的讨论"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "411d6b70-9a85-4d03-bb74-aab0fd8bd12f",
    "title": "我的新对话",
    "description": "关于 AI 的讨论",
    "tenant_id": 1,
    "user_id": "u-001",
    "is_pinned": false,
    "created_at": "2026-03-27T12:26:19.611616+08:00",
    "updated_at": "2026-03-27T12:26:19.611616+08:00"
  }
}
```

### 10.2 批量删除会话

**DELETE `/api/v1/sessions/batch`**

| 字段         | 类型     | 必填 | 描述                                                       |
| ------------ | -------- | ---- | ---------------------------------------------------------- |
| `ids`        | string[] | 否   | 要删除的会话 ID 列表（`delete_all` 为 `false` 时必填）     |
| `delete_all` | bool     | 否   | 设为 `true` 时删除当前空间的所有会话，忽略 `ids` 字段      |

请求示例：

```json
{
  "ids": [
    "411d6b70-9a85-4d03-bb74-aab0fd8bd12f"
  ]
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Sessions deleted successfully"
}
```

### 10.3 获取会话详情

**GET `/api/v1/sessions/{id}`**

| 字段 | 类型   | 必填 | 描述    |
| ---- | ------ | ---- | ------- |
| `id` | string | 是   | 会话 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "ceb9babb-1e30-41d7-817d-fd584954304b",
    "title": "模型优化策略",
    "description": "",
    "tenant_id": 1,
    "user_id": "u-001",
    "is_pinned": true,
    "pinned_at": "2026-04-01T09:12:33.123456+08:00",
    "created_at": "2026-03-27T10:24:38.308596+08:00"
  }
}
```

### 10.4 获取当前空间的会话列表

**GET `/api/v1/sessions`**

| 字段        | 类型   | 必填 | 描述                                                              |
| ----------- | ------ | ---- | ----------------------------------------------------------------- |
| `page`      | int    | 否   | 页码（默认 1）                                                    |
| `page_size` | int    | 否   | 每页数量（默认 10）                                               |
| `keyword`   | string | 否   | 按标题模糊匹配（ILIKE `%keyword%`）                               |
| `source`    | string | 否   | 来源过滤：`web`（无 IM 映射）或 IM 平台名，如 `feishu`、`wechat`、`slack` |
| `agent_id`  | string | 否   | 按 Agent 过滤（仅对 IM 会话生效）                                 |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "411d6b70-9a85-4d03-bb74-aab0fd8bd12f",
      "title": "我的新对话",
      "description": "",
      "tenant_id": 1,
      "user_id": "u-001",
      "is_pinned": true,
      "pinned_at": "2026-04-01T09:12:33.123456+08:00",
      "created_at": "2026-03-27T12:26:19.611616+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 10
}
```

### 10.5 更新会话

**PUT `/api/v1/sessions/{id}`**

| 字段 | 类型   | 必填 | 描述    |
| ---- | ------ | ---- | ------- |
| `id` | string | 是   | 会话 ID |

| 字段          | 类型   | 必填 | 描述     |
| ------------- | ------ | ---- | -------- |
| `title`       | string | 否   | 会话标题 |
| `description` | string | 否   | 会话描述 |

请求示例：

```json
{
  "title": "jingboWiki 技术讨论",
  "description": "关于 jingboWiki 架构的讨论"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "411d6b70-9a85-4d03-bb74-aab0fd8bd12f",
    "title": "jingboWiki 技术讨论",
    "description": "关于 jingboWiki 架构的讨论",
    "tenant_id": 1,
    "user_id": "u-001",
    "is_pinned": false,
    "created_at": "2026-03-27T12:26:19.611616+08:00",
    "updated_at": "2026-03-27T14:20:56.738424+08:00"
  }
}
```

### 10.6 删除会话

**DELETE `/api/v1/sessions/{id}`**

| 字段 | 类型   | 必填 | 描述    |
| ---- | ------ | ---- | ------- |
| `id` | string | 是   | 会话 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Session deleted successfully"
}
```

### 10.7 清空会话消息

**DELETE `/api/v1/sessions/{id}/messages`**

| 字段 | 类型   | 必填 | 描述    |
| ---- | ------ | ---- | ------- |
| `id` | string | 是   | 会话 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Session messages cleared successfully"
}
```

### 10.8 生成会话标题

**POST `/api/v1/sessions/{session_id}/generate_title`**

| 字段         | 类型   | 必填 | 描述    |
| ------------ | ------ | ---- | ------- |
| `session_id` | string | 是   | 会话 ID |

| 字段       | 类型      | 必填 | 描述                         |
| ---------- | --------- | ---- | ---------------------------- |
| `messages` | Message[] | 是   | 用作标题生成上下文的消息列表 |

请求示例：

```json
{
  "messages": [
    {
      "role": "user",
      "content": "你好，我想了解关于人工智能的知识"
    }
  ]
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": "人工智能基础知识"
}
```

### 10.9 停止生成

**POST `/api/v1/sessions/{session_id}/stop`**

| 字段         | 类型   | 必填 | 描述    |
| ------------ | ------ | ---- | ------- |
| `session_id` | string | 是   | 会话 ID |

| 字段         | 类型   | 必填 | 描述                    |
| ------------ | ------ | ---- | ----------------------- |
| `message_id` | string | 是   | 要停止生成的助手消息 ID |

请求示例：

```json
{
  "message_id": "ebbf7e53-dfe6-44d5-882f-36a4104910b5"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Generation stopped"
}
```

### 10.10 置顶会话

**POST `/api/v1/sessions/{session_id}/pin`**

| 字段         | 类型   | 必填 | 描述    |
| ------------ | ------ | ---- | ------- |
| `session_id` | string | 是   | 会话 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "is_pinned": true
}
```

### 10.11 取消置顶会话

**DELETE `/api/v1/sessions/{id}/pin`**

| 字段 | 类型   | 必填 | 描述    |
| ---- | ------ | ---- | ------- |
| `id` | string | 是   | 会话 ID |

响应示例（主要字段）：

```json
{
  "success": true,
  "is_pinned": false
}
```

### 10.12 继续未完成的流式响应

**GET `/api/v1/sessions/continue-stream/{session_id}`**

参数：`session_id`（path，必填）：会话ID；`message_id`（query，必填）：消息ID。

响应：200：流式响应。

### 10.13 获取回答后推荐问题

**GET `/api/v1/sessions/{session_id}/messages/{message_id}/suggestions`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话 ID |
| message_id | path | string | 是 | 助手消息 ID |

响应：200：OK。

### 10.14 确保生成回答后推荐问题

**POST `/api/v1/sessions/{session_id}/messages/{message_id}/suggestions`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话 ID |
| message_id | path | string | 是 | 助手消息 ID |
| regenerate | body | boolean | 否 |  |

响应：200：OK；202：Accepted。

### 10.15 上报推荐问题事件

**POST `/api/v1/sessions/{session_id}/suggestion-events`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话 ID |
| event_type | body | string | 是 |  |
| question_id | body | string | 否 |  |
| suggestion_set_id | body | string | 是 |  |

响应：204：No Content。

### 10.16 生成会话标题

**POST `/api/v1/sessions/{session_id}/title`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话ID |
| messages | body | array | 是 | Messages to use as context for title generation |

响应：200：生成的标题。


## 11. 问答

### 11.1 基于知识库的问答

**POST `/api/v1/knowledge-chat/{session_id}`**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 查询文本 |
| `knowledge_base_ids` | string[] | 否 | 知识库 ID 列表 |
| `knowledge_ids` | string[] | 否 | 知识文件 ID 列表，指定具体文件进行检索 |
| `agent_id` | string | 否 | 自定义 Agent ID，指定使用的智能体 |
| `summary_model_id` | string | 否 | 覆盖默认的摘要模型 ID |
| `mentioned_items` | object[] | 否 | @提及的知识库和文件列表 |
| `disable_title` | bool | 否 | 是否禁用自动标题生成（默认 false） |
| `images` | object[] | 否 | 附带的图片（base64 格式），需要 Agent 启用图片上传 |
| `channel` | string | 否 | 来源渠道标识：`web`、`api`、`im`、`browser_extension` |
| `suggestion_attribution` | object | 否 | 用户从推荐问题发起本轮时传入 `{suggestion_set_id, question_id}`；服务端会校验归属 |

请求示例：

```json
{
  "query": "彗尾的形状",
  "knowledge_base_ids": [
    "kb-00000001"
  ],
  "agent_id": "builtin-quick-answer"
}
```

响应：`text/event-stream`（SSE 流式响应）。

### 11.2 基于 Agent 的智能问答

**POST `/api/v1/agent-chat/{session_id}`**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 查询文本 |
| `knowledge_base_ids` | string[] | 否 | 知识库 ID 列表，可动态指定本次查询使用的知识库 |
| `knowledge_ids` | string[] | 否 | 知识文件 ID 列表，可动态指定本次查询使用的具体文件 |
| `agent_enabled` | bool | 否 | 是否启用 Agent 模式（默认 false，优先使用 Agent 配置） |
| `agent_id` | string | 否 | 自定义 Agent ID，指定使用的智能体（支持共享 Agent） |
| `web_search_enabled` | bool | 否 | 是否启用网络搜索（默认 false） |
| `summary_model_id` | string | 否 | 覆盖默认的摘要模型 ID |
| `mentioned_items` | object[] | 否 | @提及的知识库和文件列表 |
| `disable_title` | bool | 否 | 是否禁用自动标题生成（默认 false） |
| `images` | object[] | 否 | 附带的图片（base64 格式），需要 Agent 启用图片上传 |
| `channel` | string | 否 | 来源渠道标识：`web`、`api`、`im`、`browser_extension` |
| `suggestion_attribution` | object | 否 | 用户从推荐问题发起本轮时传入 `{suggestion_set_id, question_id}`；服务端会校验归属 |

响应：`text/event-stream`（SSE 流式响应）。

### 11.3 知识搜索

**POST `/api/v1/sessions/search`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| knowledge_base_id | body | string | 否 | Single knowledge base ID (for backward compatibility) |
| knowledge_base_ids | body | array | 否 | IDs of knowledge bases to search (multi-KB support) |
| knowledge_ids | body | array | 否 | IDs of specific knowledge (files) to search |
| mentioned_items | body | array | 否 | Optional scoped tag mentions |
| query | body | string | 是 | Query text to search for |
| tag_ids | body | array | 否 | Tag IDs for filtering within a single KB |

响应：200：搜索结果。

### 11.4 Agent问答

**POST `/api/v1/sessions/{session_id}/agent-qa`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话ID |
| agent_enabled | body | boolean | 否 | Whether agent mode is enabled for this request |
| agent_id | body | string | 否 | Selected custom agent ID (backend resolves shared agent and its workspace from share relation) |
| attachment_ids | body | array | 否 | Pre-uploaded session-scoped document IDs |
| attachment_uploads | body | array | 否 | Attached files (documents, audio, etc.) |
| channel | body | string | 否 | Source channel: "web", "api", "im", etc. |
| disable_title | body | boolean | 否 | Whether to disable auto title generation |
| images | body | array | 否 | Attached images for multimodal chat |
| knowledge_base_ids | body | array | 否 | Selected knowledge base ID for this request |
| knowledge_ids | body | array | 否 | Selected knowledge ID for this request |
| mentioned_items | body | array | 否 | @mentioned knowledge bases and files |
| query | body | string | 是 | Query text for knowledge base search |
| response_format | body | object | 否 | Optional strict JSON-object response configuration |
| skill_names | body | array | 否 | Per-request Skills selected via @mention |
| suggestion_attribution | body | object | 否 |  |
| summary_model_id | body | string | 否 | Optional summary model ID for this request (overrides session default) |
| tag_ids | body | array | 否 | @mentioned tag IDs (display/debug; scoped via MentionedItems) |
| web_search_enabled | body | boolean | 否 | Whether web search is enabled for this request |

响应：200：问答结果（SSE流）。

### 11.5 知识问答

**POST `/api/v1/sessions/{session_id}/knowledge-qa`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| session_id | path | string | 是 | 会话ID |
| agent_enabled | body | boolean | 否 | Whether agent mode is enabled for this request |
| agent_id | body | string | 否 | Selected custom agent ID (backend resolves shared agent and its workspace from share relation) |
| attachment_ids | body | array | 否 | Pre-uploaded session-scoped document IDs |
| attachment_uploads | body | array | 否 | Attached files (documents, audio, etc.) |
| channel | body | string | 否 | Source channel: "web", "api", "im", etc. |
| disable_title | body | boolean | 否 | Whether to disable auto title generation |
| images | body | array | 否 | Attached images for multimodal chat |
| knowledge_base_ids | body | array | 否 | Selected knowledge base ID for this request |
| knowledge_ids | body | array | 否 | Selected knowledge ID for this request |
| mentioned_items | body | array | 否 | @mentioned knowledge bases and files |
| query | body | string | 是 | Query text for knowledge base search |
| response_format | body | object | 否 | Optional strict JSON-object response configuration |
| skill_names | body | array | 否 | Per-request Skills selected via @mention |
| suggestion_attribution | body | object | 否 |  |
| summary_model_id | body | string | 否 | Optional summary model ID for this request (overrides session default) |
| tag_ids | body | array | 否 | @mentioned tag IDs (display/debug; scoped via MentionedItems) |
| web_search_enabled | body | boolean | 否 | Whether web search is enabled for this request |

响应：200：问答结果（SSE流）。


## 12. 消息管理

### 12.1 获取最近的会话消息列表

**GET `/api/v1/messages/{session_id}/load`**

请求示例：

```json
{
  "query": "彗尾的形状"
}
```

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "b8b90eeb-7dd5-4cf9-81c6-5ebcbd759451",
      "session_id": "ceb9babb-1e30-41d7-817d-fd584954304b",
      "request_id": "hCA8SDjxcAvv",
      "content": "<think>\n好的",
      "role": "assistant",
      "knowledge_references": [
        {
          "id": "c8347bef-127f-4a22-b962-edf5a75386ec",
          "content": "彗星xxx",
          "knowledge_id": "a6790b93-4700-4676-bd48-0d4804e1456b",
          "chunk_index": 0,
          "knowledge_title": "彗星.txt",
          "start_at": 0,
          "end_at": 2760,
          "seq": 0
        }
      ],
      "agent_steps": [],
      "is_completed": true
    }
  ],
  "success": true
}
```

### 12.2 删除消息

**DELETE `/api/v1/messages/{session_id}/{id}`**

参数：`session_id`（path，必填）：会话ID；`id`（path，必填）：消息ID。

响应示例（主要字段）：

```json
{
  "message": "Message deleted successfully",
  "success": true
}
```

### 12.3 搜索历史对话

**POST `/api/v1/messages/search`**

请求示例：

```json
{
  "query": "彗星的结构",
  "mode": "hybrid",
  "limit": 20,
  "session_ids": []
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "items": [
      {
        "request_id": "3475c004-0ada-4306-9d30-d7f5efce50d2",
        "session_id": "ceb9babb-1e30-41d7-817d-fd584954304b",
        "session_title": "彗星知识问答",
        "query_content": "彗尾的形状",
        "answer_content": "彗尾的形状主要取决于...",
        "score": 0.85,
        "match_type": "hybrid",
        "created_at": "2025-08-12T14:30:39.732246+08:00"
      }
    ],
    "total": 1
  },
  "success": true
}
```

### 12.4 获取聊天历史知识库统计

**GET `/api/v1/messages/chat-history-stats`**

响应示例（主要字段）：

```json
{
  "data": {
    "enabled": true,
    "embedding_model_id": "dff7bc94-7885-4dd1-bfd5-bd96e4df2fc3",
    "knowledge_base_id": "kb-chat-00000001",
    "knowledge_base_name": "聊天历史知识库",
    "indexed_message_count": 1024,
    "has_indexed_messages": true
  },
  "success": true
}
```


## 13. 认证管理

### 13.1 用户注册

**POST `/api/v1/auth/register`**

| 字段     | 类型   | 必填 | 校验                       | 说明      |
| -------- | ------ | ---- | -------------------------- | --------- |
| username | string | 是   | 2-50 个字符；仅 Unicode 字母、数字、`_`、`.`、`-` | 用户名 |
| email    | string | 是   | 邮箱格式                   | 邮箱      |
| password | string | 是   | 8-32 个字符；至少一个英文字母和一个数字；不得使用常见弱密码 | 密码 |

请求示例：

```json
{
  "username": "alice",
  "email": "alice@example.com",
  "password": "SecurePass2026"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Registration successful",
  "user": {
    "id": "usr-...",
    "username": "alice",
    "email": "alice@example.com",
    "tenant_id": 1,
    "is_active": true,
    "created_at": "2026-05-11T10:00:00+08:00",
    "updated_at": "2026-05-11T10:00:00+08:00"
  },
  "tenant": {
    "id": 1,
    "name": "alice's workspace",
    "api_key": "sk-..."
  }
}
```

### 13.2 用户登录

**POST `/api/v1/auth/login`**

| 字段     | 类型   | 必填 | 说明          |
| -------- | ------ | ---- | ------------- |
| email    | string | 是   | 注册邮箱      |
| password | string | 是   | 密码          |

请求示例：

```json
{
  "email": "alice@example.com",
  "password": "SecurePass2026"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Login successful",
  "user": {
    "id": "usr-...",
    "username": "alice",
    "email": "alice@example.com"
  },
  "tenant": {
    "id": 1,
    "name": "alice's workspace",
    "api_key": "sk-..."
  },
  "token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi..."
}
```

### 13.3 获取 OIDC 配置元数据

**GET `/api/v1/auth/oidc/config`**

响应示例（主要字段）：

```json
{
  "success": true,
  "enabled": true,
  "provider_display_name": "jingboWiki SSO"
}
```

### 13.4 获取 OIDC 授权链接

**GET `/api/v1/auth/oidc/url`**

| 字段       | 类型   | 必填 | 说明                                                    |
| ---------- | ------ | ---- | ------------------------------------------------------- |
| redirect   | string | 否   | 登录成功后前端期望落地的路径（如 `/dashboard`），透传到 state |

响应示例（主要字段）：

```json
{
  "success": true,
  "provider_display_name": "jingboWiki SSO",
  "authorization_url": "https://idp.example.com/oauth/authorize?client_id=...&state=...",
  "state": "abcdef..."
}
```

### 13.5 OIDC 授权回调

**GET `/api/v1/auth/oidc/callback`**

| 字段              | 类型   | 必填 | 说明                          |
| ----------------- | ------ | ---- | ----------------------------- |
| code              | string | 是   | IdP 颁发的 authorization code |
| state             | string | 是   | 与 `/auth/oidc/url` 返回值一致 |
| error             | string | 否   | IdP 返回的错误标识            |
| error_description | string | 否   | IdP 返回的错误详情            |

### 13.6 刷新令牌

**POST `/api/v1/auth/refresh`**

| 字段          | 类型   | 必填 | 说明              |
| ------------- | ------ | ---- | ----------------- |
| refreshToken  | string | 是   | 登录时颁发的 refresh_token |

请求示例：

```json
{
  "refreshToken": "eyJhbGciOi..."
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Token refreshed successfully",
  "access_token": "eyJhbGciOi...",
  "refresh_token": "eyJhbGciOi..."
}
```

### 13.7 验证 JWT

**GET `/api/v1/auth/validate`**

响应示例（主要字段）：

```json
{
  "success": true,
  "valid": true,
  "user_id": "usr-...",
  "tenant_id": 1
}
```

### 13.8 退出登录

**POST `/api/v1/auth/logout`**

响应：200：登出成功。

### 13.9 获取当前用户信息

**GET `/api/v1/auth/me`**

响应示例（主要字段）：

```json
{
  "success": true,
  "user": {
    "id": "usr-...",
    "username": "alice",
    "email": "alice@example.com",
    "avatar": "",
    "tenant_id": 1,
    "is_active": true,
    "can_access_all_tenants": false,
    "created_at": "2026-05-11T10:00:00+08:00"
  }
}
```

### 13.10 修改密码

**POST `/api/v1/auth/change-password`**

| 字段          | 类型   | 必填 | 校验    | 说明      |
| ------------- | ------ | ---- | ------- | --------- |
| old_password  | string | 是   |          | 旧密码    |
| new_password  | string | 是   | 8-32 个字符；至少一个英文字母和一个数字；不得使用常见弱密码 | 新密码 |

请求示例：

```json
{
  "old_password": "SecurePass2026",
  "new_password": "NewSecurePass2027"
}
```

响应：200：修改成功。

### 13.11 自动初始化（Lite 桌面版）

**POST `/api/v1/auth/auto-setup`**

响应：200：OK。

### 13.12 获取认证配置

**GET `/api/v1/auth/config`**

响应：200：认证配置。

### 13.13 解析共享邀请链接 token

**POST `/api/v1/auth/invitations/lookup`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| token | body | string | 是 |  |

响应：200：OK。

### 13.14 更新当前用户的个性化设置

**PUT `/api/v1/auth/me/preferences`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| last_active_tenant_id | body | integer | 否 | LastActiveTenantID lets the SPA persist "after a fresh login, |

响应：200：更新后的偏好。

### 13.15 使用共享链接注册

**POST `/api/v1/auth/register-by-invite`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| email | body | string | 是 |  |
| password | body | string | 是 |  |
| token | body | string | 是 |  |
| username | body | string | 是 |  |

响应：201：Created。

### 13.16 切换激活空间

**POST `/api/v1/auth/switch-tenant`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| refresh_token | body | string | 否 |  |
| tenant_id | body | integer | 否 |  |

响应：200：OK。


## 14. 空间管理

### 14.1 获取所有空间列表

**GET `/api/v1/tenants/all`**

响应示例（主要字段）：

```json
{
  "data": {
    "items": [
      {
        "id": 10001,
        "name": "jingboWiki-1",
        "description": "jingboWiki workspaces 1",
        "status": "active",
        "business": "wechat",
        "created_at": "2025-08-11T20:37:28.39698+08:00",
        "updated_at": "2025-08-11T20:37:28.405693+08:00"
      }
    ]
  },
  "success": true
}
```

### 14.2 搜索空间

**GET `/api/v1/tenants/search`**

参数：`keyword`（query，可选）：搜索关键词；`tenant_id`（query，可选）：空间ID筛选；`page`（query，可选）：页码；`page_size`（query，可选）：每页数量。

响应示例（主要字段）：

```json
{
  "data": {
    "items": [
      {
        "id": 10002,
        "name": "jingboWiki",
        "description": "jingboWiki workspaces",
        "status": "active",
        "business": "wechat",
        "created_at": "2025-08-11T20:52:58.05679+08:00",
        "updated_at": "2025-08-11T20:52:58.060495+08:00"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 10
  },
  "success": true
}
```

### 14.3 创建新空间

**POST `/api/v1/tenants`**

| 字段              | 类型   | 必填 | 说明                                                   |
| ----------------- | ------ | ---- | ------------------------------------------------------ |
| name              | string | 是   | 空间名称                                               |
| description       | string | 否   | 空间描述                                               |
| business          | string | 否   | 业务标识（如 `wechat`）                                |
| retriever_engines | object | 否   | 检索引擎组合配置（`engines` 数组：每项含 `retriever_type` 与 `retriever_engine_type`） |
| storage_quota     | int    | 否   | 存储配额（字节）                                       |

请求示例：

```json
{
  "name": "jingboWiki",
  "description": "jingboWiki workspaces",
  "business": "wechat",
  "retriever_engines": {
    "engines": [
      {
        "retriever_type": "keywords",
        "retriever_engine_type": "postgres"
      }
    ]
  }
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": 10000,
    "name": "jingboWiki",
    "description": "jingboWiki workspaces",
    "status": "active",
    "retriever_engines": {
      "engines": [
        {
          "retriever_engine_type": "postgres",
          "retriever_type": "keywords"
        }
      ]
    },
    "business": "wechat",
    "storage_quota": 10737418240,
    "storage_used": 0
  },
  "success": true
}
```

### 14.4 获取指定空间信息

**GET `/api/v1/tenants/{id}`**

| 字段 | 类型 | 说明    |
| ---- | ---- | ------- |
| id   | int  | 空间 ID |

响应示例（主要字段）：

```json
{
  "data": {
    "id": 10000,
    "name": "jingboWiki",
    "description": "jingboWiki workspaces",
    "api_key": "sk-aaLRAgvCRJcmtiL2vLMeB1FB5UV0Q-qB7DlTE1pJ9KA93XZG",
    "status": "active",
    "retriever_engines": {
      "engines": [
        {
          "retriever_engine_type": "postgres",
          "retriever_type": "keywords"
        }
      ]
    },
    "business": "wechat",
    "storage_quota": 10737418240
  },
  "success": true
}
```

### 14.5 更新空间信息

**PUT `/api/v1/tenants/{id}`**

| 字段 | 类型 | 说明    |
| ---- | ---- | ------- |
| id   | int  | 空间 ID |

请求示例：

```json
{
  "name": "jingboWiki new",
  "description": "jingboWiki workspaces new",
  "status": "active",
  "retriever_engines": {
    "engines": [
      {
        "retriever_engine_type": "postgres",
        "retriever_type": "keywords"
      }
    ]
  },
  "business": "wechat",
  "storage_quota": 10737418240
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": 10000,
    "name": "jingboWiki new",
    "description": "jingboWiki workspaces new",
    "api_key": "sk-aaLRAgvCRJcmtiL2vLMeB1FB5UV0Q-qB7DlTE1pJ9KA93XZG",
    "status": "active",
    "retriever_engines": {
      "engines": [
        {
          "retriever_engine_type": "postgres",
          "retriever_type": "keywords"
        }
      ]
    },
    "business": "wechat",
    "storage_quota": 10737418240
  },
  "success": true
}
```

### 14.6 删除空间

**DELETE `/api/v1/tenants/{id}`**

| 字段 | 类型 | 说明    |
| ---- | ---- | ------- |
| id   | int  | 空间 ID |

响应示例（主要字段）：

```json
{
  "message": "Workspace deleted successfully",
  "success": true
}
```

### 14.7 获取 API Key 用户身份配置

**GET `/api/v1/tenants/{id}/api-principal-config`**

参数：`id`（path，必填）：空间ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "mode": "signed_token",
    "direct_header_name": "X-External-User-ID",
    "signed_token_header_name": "X-External-User-Token",
    "require_direct_header": false,
    "has_hmac_secret": true
  }
}
```

### 14.8 更新 API Key 用户身份配置

**PUT `/api/v1/tenants/{id}/api-principal-config`**

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| mode | string | 必填，`tenant` / `direct_header` / `signed_token` |
| direct_header_name | string | 可选 |
| signed_token_header_name | string | 可选 |
| require_direct_header | bool | 可选，`direct_header` 模式下缺 header 是否 401 |
| hmac_secret | string | 可选，`signed_token` 模式 HMAC 密钥；省略则保留现有值 |

请求示例：

```json
{
  "mode": "direct_header",
  "direct_header_name": "X-External-User-ID",
  "require_direct_header": true
}
```

响应：200：更新后的配置。

### 14.9 获取空间列表

**GET `/api/v1/tenants`**

响应示例（主要字段）：

```json
{
  "data": {
    "items": [
      {
        "id": 10002,
        "name": "jingboWiki",
        "description": "jingboWiki workspaces",
        "api_key": "sk-An7_t_izCKFIJ4iht9Xjcjnj_MC48ILvwezEDki9ScfIa7KA",
        "status": "active",
        "retriever_engines": {
          "engines": [
            {
              "retriever_engine_type": "postgres",
              "retriever_type": "keywords"
            }
          ]
        },
        "business": "wechat",
        "storage_quota": 10737418240
      }
    ]
  },
  "success": true
}
```

### 14.10 获取空间 KV 配置

**GET `/api/v1/tenants/kv/{key}`**

| 字段 | 类型   | 说明                                           |
| ---- | ------ | ---------------------------------------------- |
| key  | string | 配置键名（见下方支持的 key 列表，不支持的键返回 400） |

响应示例（主要字段）：

```json
{
  "data": {
    "max_iterations": 10,
    "allowed_tools": [
      "knowledge_search"
    ],
    "temperature": 0.3,
    "system_prompt": "...",
    "use_custom_system_prompt": false,
    "available_tools": [
      {
        "name": "knowledge_search",
        "label": "知识库检索",
        "description": "..."
      }
    ],
    "available_placeholders": [
      {
        "name": "web_search_status",
        "label": "联网搜索状态",
        "description": "..."
      }
    ]
  },
  "success": true
}
```

### 14.11 更新空间 KV 配置

**PUT `/api/v1/tenants/kv/{key}`**

| 字段 | 类型   | 说明                          |
| ---- | ------ | ----------------------------- |
| key  | string | 配置键名（见 GET 接口的支持列表，`prompt-templates` 除外） |

请求示例：

```json
{
  "max_iterations": 20,
  "temperature": 0.3,
  "system_prompt": ""
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "max_iterations": 20,
    "allowed_tools": [
      "knowledge_search"
    ],
    "temperature": 0.3,
    "system_prompt": "",
    "use_custom_system_prompt": false
  },
  "message": "Agent configuration updated successfully",
  "success": true
}
```

### 14.12 获取提示词模板

**GET `/api/v1/tenants/kv/prompt-templates`**

响应：200：提示词模板配置。

### 14.13 获取空间网络搜索配置

**GET `/api/v1/tenants/kv/web-search-config`**

响应：200：网络搜索配置。

### 14.14 生成 API Playground 测试 JWT

**POST `/api/v1/tenants/{id}/api-principal-test-token`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | integer | 是 | 空间ID |
| expires_in_seconds | body | integer | 否 |  |
| external_user_id | body | string | 否 |  |

响应：200：短期 JWT。


## 15. 组织管理

### 15.1 创建组织

**POST `/api/v1/organizations`**

| 字段                        | 类型    | 必填 | 说明                                              |
| --------------------------- | ------- | ---- | ------------------------------------------------- |
| name                        | string  | 是   | 组织名称（1-255 字符）                            |
| description                 | string  | 否   | 组织描述（最多 1000 字符）                        |
| avatar                      | string  | 否   | 头像 URL（最多 512 字符）                         |
| invite_code_validity_days   | int     | 否   | 邀请码有效天数：`0`=永久，`1` / `7` / `30`，默认 7 |
| member_limit                | int     | 否   | 成员上限，`0`=不限，默认 50                       |

请求示例：

```json
{
  "name": "AI 技术团队",
  "description": "专注于 AI 技术研究与知识管理",
  "invite_code_validity_days": 7,
  "member_limit": 50
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "org-00000001",
    "name": "AI 技术团队",
    "description": "专注于 AI 技术研究与知识管理",
    "avatar": "",
    "owner_id": "user-00000001",
    "invite_code": "",
    "invite_code_validity_days": 7,
    "require_approval": false
  },
  "success": true
}
```

### 15.2 获取我的组织列表

**GET `/api/v1/organizations`**

响应示例（主要字段）：

```json
{
  "data": {
    "organizations": [
      {
        "id": "org-00000001",
        "name": "AI 技术团队",
        "description": "专注于 AI 技术研究与知识管理",
        "owner_id": "user-00000001",
        "invite_code": "ABC123XY",
        "invite_code_expires_at": "2025-08-19T10:00:00+08:00",
        "invite_code_validity_days": 7,
        "require_approval": false
      }
    ],
    "total": 1,
    "resource_counts": {
      "knowledge_bases": {
        "by_organization": {
          "org-00000001": 5
        }
      },
      "agents": {
        "by_organization": {
          "org-00000001": 2
        }
      }
    }
  },
  "success": true
}
```

### 15.3 通过邀请码预览组织

**GET `/api/v1/organizations/preview/{code}`**

| 字段 | 类型   | 说明   |
| ---- | ------ | ------ |
| code | string | 邀请码 |

响应示例（主要字段）：

```json
{
  "data": {
    "id": "org-00000001",
    "name": "AI 技术团队",
    "description": "专注于 AI 技术研究与知识管理",
    "avatar": "",
    "member_count": 3,
    "share_count": 2,
    "agent_share_count": 1,
    "is_already_member": false
  },
  "success": true
}
```

### 15.4 通过邀请码加入组织

**POST `/api/v1/organizations/join`**

| 字段        | 类型   | 必填 | 说明                  |
| ----------- | ------ | ---- | --------------------- |
| invite_code | string | 是   | 邀请码（8-32 字符）   |

请求示例：

```json
{
  "invite_code": "ABC123XY"
}
```

响应：200：OK。

### 15.5 提交加入申请

**POST `/api/v1/organizations/join-request`**

| 字段        | 类型   | 必填 | 说明                                        |
| ----------- | ------ | ---- | ------------------------------------------- |
| invite_code | string | 是   | 邀请码（8-32 字符）                         |
| message     | string | 否   | 申请留言（最多 500 字符）                   |
| role        | string | 否   | 期望角色：`viewer` / `editor` / `admin`     |

请求示例：

```json
{
  "invite_code": "ABC123XY",
  "message": "希望加入团队参与知识库建设",
  "role": "editor"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "jr-00000001",
    "user_id": "user-00000002",
    "request_type": "join",
    "requested_role": "editor",
    "status": "pending",
    "created_at": "2025-08-14T10:00:00+08:00"
  },
  "success": true
}
```

### 15.6 搜索可加入的组织

**GET `/api/v1/organizations/search`**

| 字段  | 类型   | 必填 | 说明                              |
| ----- | ------ | ---- | --------------------------------- |
| q     | string | 否   | 搜索关键词（名称或描述模糊匹配）   |
| limit | int    | 否   | 返回数量（1-100，默认 20）         |

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "org-00000001",
      "name": "AI 技术团队",
      "description": "专注于 AI 技术研究与知识管理",
      "avatar": "",
      "member_count": 3,
      "member_limit": 50,
      "share_count": 2,
      "agent_share_count": 1
    }
  ],
  "total": 1,
  "success": true
}
```

### 15.7 通过组织 ID 加入

**POST `/api/v1/organizations/join-by-id`**

| 字段             | 类型   | 必填 | 说明                                       |
| ---------------- | ------ | ---- | ------------------------------------------ |
| organization_id  | string | 是   | 目标组织 ID                                |
| message          | string | 否   | 申请留言（最多 500 字符）                  |
| role             | string | 否   | 期望角色：`viewer` / `editor` / `admin`    |

请求示例：

```json
{
  "organization_id": "org-00000001",
  "message": "希望加入贵团队",
  "role": "viewer"
}
```

响应：200：OK。

### 15.8 获取组织详情

**GET `/api/v1/organizations/{id}`**

| 字段 | 类型   | 说明    |
| ---- | ------ | ------- |
| id   | string | 组织 ID |

响应示例（主要字段）：

```json
{
  "data": {
    "id": "org-00000001",
    "name": "AI 技术团队",
    "description": "专注于 AI 技术研究与知识管理",
    "avatar": "",
    "owner_id": "user-00000001",
    "invite_code": "ABC123XY",
    "invite_code_expires_at": "2025-08-19T10:00:00+08:00",
    "invite_code_validity_days": 7
  },
  "success": true
}
```

### 15.9 更新组织

**PUT `/api/v1/organizations/{id}`**

| 字段                        | 类型    | 说明                                          |
| --------------------------- | ------- | --------------------------------------------- |
| name                        | string  | 组织名称（1-255 字符）                        |
| description                 | string  | 组织描述（最多 1000 字符）                    |
| avatar                      | string  | 头像 URL（最多 512 字符）                     |
| require_approval            | bool    | 加入是否需要审核                              |
| searchable                  | bool    | 是否在 `/organizations/search` 中可被发现     |
| invite_code_validity_days   | int     | 邀请码有效天数（0=永久，1/7/30）              |
| member_limit                | int     | 成员上限（0=不限）                            |

请求示例：

```json
{
  "description": "专注于 AI 技术研究与知识管理（更新）",
  "require_approval": true,
  "searchable": true
}
```

响应：200：OK。

### 15.10 删除组织

**DELETE `/api/v1/organizations/{id}`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 15.11 离开组织

**POST `/api/v1/organizations/{id}/leave`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Left organization successfully"
}
```

### 15.12 申请角色升级

**POST `/api/v1/organizations/{id}/request-upgrade`**

| 字段           | 类型   | 必填 | 说明                                                 |
| -------------- | ------ | ---- | ---------------------------------------------------- |
| requested_role | string | 是   | 期望角色：`viewer` / `editor` / `admin`              |
| message        | string | 否   | 申请理由（最多 500 字符）                            |

请求示例：

```json
{
  "requested_role": "admin",
  "message": "需要管理员权限来管理知识库共享"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "jr-00000002",
    "request_type": "upgrade",
    "prev_role": "editor",
    "requested_role": "admin",
    "status": "pending",
    "created_at": "2025-08-14T11:00:00+08:00"
  },
  "success": true
}
```

### 15.13 重新生成邀请码

**POST `/api/v1/organizations/{id}/invite-code`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "data": {
    "invite_code": "NEW1CODE"
  },
  "success": true
}
```

### 15.14 搜索可邀请的用户

**GET `/api/v1/organizations/{id}/search-users`**

| 字段  | 类型   | 必填 | 说明                          |
| ----- | ------ | ---- | ----------------------------- |
| q     | string | 是   | 关键词（用户名或邮箱）         |
| limit | int    | 否   | 返回数量上限，默认 10          |

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "user-00000002",
      "username": "zhangsan",
      "email": "zhangsan@example.com",
      "avatar": ""
    }
  ],
  "success": true
}
```

### 15.15 直接邀请用户

**POST `/api/v1/organizations/{id}/invite`**

| 字段    | 类型   | 必填 | 说明                                        |
| ------- | ------ | ---- | ------------------------------------------- |
| user_id | string | 是   | 被邀请用户的 ID                             |
| role    | string | 是   | 角色：`viewer` / `editor` / `admin`         |

请求示例：

```json
{
  "user_id": "user-00000002",
  "role": "editor"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Member added successfully"
}
```

### 15.16 获取成员列表

**GET `/api/v1/organizations/{id}/members`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "data": {
    "members": [
      {
        "id": "mem-00000001",
        "user_id": "user-00000001",
        "username": "admin",
        "email": "admin@example.com",
        "avatar": "",
        "role": "owner",
        "tenant_id": 1,
        "joined_at": "2025-08-12T10:00:00+08:00"
      }
    ],
    "total": 2
  },
  "success": true
}
```

### 15.17 更新成员角色

**PUT `/api/v1/organizations/{id}/members/{user_id}`**

| 字段     | 类型   | 说明           |
| -------- | ------ | -------------- |
| id       | string | 组织 ID        |
| user_id  | string | 目标成员的用户 ID |

| 字段 | 类型   | 必填 | 说明                                |
| ---- | ------ | ---- | ----------------------------------- |
| role | string | 是   | `viewer` / `editor` / `admin`       |

请求示例：

```json
{
  "role": "admin"
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 15.18 移除成员

**DELETE `/api/v1/organizations/{id}/members/{user_id}`**

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 15.19 获取待审核申请列表

**GET `/api/v1/organizations/{id}/join-requests`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "data": {
    "requests": [
      {
        "id": "jr-00000001",
        "user_id": "user-00000003",
        "username": "zhangwei",
        "email": "zhangwei@example.com",
        "message": "希望加入团队参与知识库建设",
        "request_type": "join",
        "prev_role": "",
        "requested_role": "editor"
      }
    ],
    "total": 2
  },
  "success": true
}
```

### 15.20 审核加入申请

**PUT `/api/v1/organizations/{id}/join-requests/{request_id}/review`**

| 字段       | 类型   | 说明     |
| ---------- | ------ | -------- |
| id         | string | 组织 ID  |
| request_id | string | 申请 ID  |

| 字段     | 类型   | 必填 | 说明                                                                  |
| -------- | ------ | ---- | --------------------------------------------------------------------- |
| approved | bool   | 是   | 是否通过                                                              |
| message  | string | 否   | 审核留言（最多 500 字符）                                             |
| role     | string | 否   | 通过时强制分配的角色（缺省按申请者请求的角色），仅 `viewer/editor/admin` |

请求示例：

```json
{
  "approved": true,
  "message": "欢迎加入",
  "role": "editor"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Review completed"
}
```

### 15.21 共享知识库到组织

**POST `/api/v1/knowledge-bases/{id}/shares`**

| 字段             | 类型   | 必填 | 说明                            |
| ---------------- | ------ | ---- | ------------------------------- |
| organization_id  | string | 是   | 目标组织 ID                     |
| permission       | string | 是   | 共享权限：`viewer` / `editor`   |

请求示例：

```json
{
  "organization_id": "org-00000001",
  "permission": "viewer"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "kbs-00000001",
    "knowledge_base_id": "kb-00000001",
    "organization_id": "org-00000001",
    "shared_by_user_id": "user-00000001",
    "source_tenant_id": 1,
    "permission": "viewer",
    "created_at": "2025-08-15T10:00:00+08:00"
  },
  "success": true
}
```

### 15.22 获取知识库共享列表

**GET `/api/v1/knowledge-bases/{id}/shares`**

参数：`id`（path，必填）：知识库ID。

响应示例（主要字段）：

```json
{
  "data": {
    "shares": [
      {
        "id": "kbs-00000001",
        "knowledge_base_id": "kb-00000001",
        "knowledge_base_name": "技术文档库",
        "knowledge_base_type": "document",
        "knowledge_count": 12,
        "chunk_count": 0,
        "organization_id": "org-00000001",
        "organization_name": "AI 技术团队"
      }
    ],
    "total": 1
  },
  "success": true
}
```

### 15.23 更新共享权限

**PUT `/api/v1/knowledge-bases/{id}/shares/{share_id}`**

| 字段     | 类型   | 说明           |
| -------- | ------ | -------------- |
| id       | string | 知识库 ID      |
| share_id | string | 共享记录 ID    |

| 字段       | 类型   | 必填 | 说明                          |
| ---------- | ------ | ---- | ----------------------------- |
| permission | string | 是   | 新权限：`viewer` / `editor`   |

请求示例：

```json
{
  "permission": "editor"
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 15.24 取消知识库共享

**DELETE `/api/v1/knowledge-bases/{id}/shares/{share_id}`**

参数：`id`（path，必填）：知识库ID；`share_id`（path，必填）：共享记录ID。

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 15.25 获取组织下被共享进来的知识库

**GET `/api/v1/organizations/{id}/shares`**

参数：`id`（path，必填）：组织ID。

响应：200：OK。

### 15.26 组织内知识库空间视图

**GET `/api/v1/organizations/{id}/shared-knowledge-bases`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "data": [
    {
      "knowledge_base": {
        "id": "kb-00000001",
        "name": "技术文档库",
        "type": "document"
      },
      "share_id": "kbs-00000001",
      "organization_id": "org-00000001",
      "org_name": "AI 技术团队",
      "permission": "viewer",
      "source_tenant_id": 1,
      "shared_at": "2025-08-15T10:00:00+08:00",
      "is_mine": false
    }
  ],
  "total": 1,
  "success": true
}
```

### 15.27 共享智能体到组织

**POST `/api/v1/agents/{id}/shares`**

| 字段             | 类型   | 必填 | 说明                            |
| ---------------- | ------ | ---- | ------------------------------- |
| organization_id  | string | 是   | 目标组织 ID                     |
| permission       | string | 是   | 共享权限：`viewer` / `editor`   |

请求示例：

```json
{
  "organization_id": "org-00000001",
  "permission": "viewer"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "as-00000001",
    "agent_id": "agent-00000001",
    "organization_id": "org-00000001",
    "shared_by_user_id": "user-00000001",
    "source_tenant_id": 1,
    "permission": "viewer",
    "created_at": "2025-08-15T11:00:00+08:00"
  },
  "success": true
}
```

### 15.28 获取智能体共享列表

**GET `/api/v1/agents/{id}/shares`**

响应示例（主要字段）：

```json
{
  "data": {
    "shares": [
      {
        "id": "as-00000001",
        "agent_id": "agent-00000001",
        "organization_id": "org-00000001",
        "organization_name": "AI 技术团队",
        "shared_by_user_id": "user-00000001",
        "source_tenant_id": 1,
        "permission": "viewer",
        "created_at": "2025-08-15T11:00:00+08:00"
      }
    ],
    "total": 1
  },
  "success": true
}
```

### 15.29 取消智能体共享

**DELETE `/api/v1/agents/{id}/shares/{share_id}`**

参数：`id`（path，必填）：智能体 ID；`share_id`（path，必填）：共享记录 ID。

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Share removed successfully"
}
```

### 15.30 获取组织下被共享进来的智能体

**GET `/api/v1/organizations/{id}/agent-shares`**

参数：`id`（path，必填）：组织 ID。

响应示例（主要字段）：

```json
{
  "data": {
    "shares": [
      {
        "id": "as-00000001",
        "agent_id": "agent-00000001",
        "agent_name": "智能客服助手",
        "agent_avatar": "🤖",
        "organization_id": "org-00000001",
        "organization_name": "AI 技术团队",
        "shared_by_user_id": "user-00000001",
        "shared_by_username": "admin"
      }
    ],
    "total": 1
  },
  "success": true
}
```

### 15.31 组织内智能体空间视图

**GET `/api/v1/organizations/{id}/shared-agents`**

参数：`id`（path，必填）：组织ID。

响应示例（主要字段）：

```json
{
  "data": [
    {
      "agent": {
        "id": "agent-00000001",
        "name": "智能客服助手"
      },
      "share_id": "as-00000001",
      "organization_id": "org-00000001",
      "org_name": "AI 技术团队",
      "permission": "viewer",
      "source_tenant_id": 1,
      "shared_at": "2025-08-15T11:00:00+08:00",
      "shared_by_user_id": "user-00000001"
    }
  ],
  "total": 1,
  "success": true
}
```

### 15.32 获取共享给我的知识库（跨组织）

**GET `/api/v1/shared-knowledge-bases`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "knowledge_base": {
        "id": "kb-00000001",
        "name": "技术文档库"
      },
      "share_id": "kbs-00000001",
      "organization_id": "org-00000001",
      "org_name": "AI 技术团队",
      "permission": "viewer",
      "source_tenant_id": 1,
      "shared_at": "2025-08-15T10:00:00+08:00"
    }
  ],
  "total": 1,
  "success": true
}
```

### 15.33 获取共享给我的智能体（跨组织）

**GET `/api/v1/shared-agents`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "agent": {
        "id": "agent-00000001",
        "name": "智能客服助手"
      },
      "share_id": "as-00000001",
      "organization_id": "org-00000001",
      "org_name": "AI 技术团队",
      "permission": "viewer",
      "source_tenant_id": 1,
      "shared_at": "2025-08-15T11:00:00+08:00",
      "shared_by_user_id": "user-00000001"
    }
  ],
  "total": 1,
  "success": true
}
```

### 15.34 更新成员角色

**PUT `/api/v1/organizations/{id}/members/{tenant_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 组织ID |
| tenant_id | path | string | 是 | 成员空间ID |
| role | body | object | 是 |  |

响应：200：OK。

### 15.35 移除成员

**DELETE `/api/v1/organizations/{id}/members/{tenant_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 组织ID |
| tenant_id | path | string | 是 | 成员空间ID |

响应：200：OK。

### 15.36 搜索可邀请的空间

**GET `/api/v1/organizations/{id}/search-tenants`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 组织ID |
| q | query | string | 是 | 搜索关键词（空间名、用户名或邮箱） |
| limit | query | integer | 否 | 返回数量限制 |

响应：200：OK。


## 16. 模型管理

### 16.1 获取模型服务商列表

**GET `/api/v1/models/providers`**

| 字段       | 类型   | 必填 | 说明                                                |
| ---------- | ------ | ---- | --------------------------------------------------- |
| model_type | string | 否   | 模型类型，可选值：`chat` / `embedding` / `rerank` / `vllm` / `asr`；省略则返回全部 |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "value": "aliyun",
      "label": "阿里云 DashScope",
      "description": "qwen-plus, tongyi-embedding-vision-plus, qwen3-rerank, etc.",
      "defaultUrls": {
        "chat": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "rerank": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
      },
      "modelTypes": [
        "chat"
      ]
    }
  ]
}
```

### 16.2 创建模型

**POST `/api/v1/models`**

| 字段        | 类型   | 必填 | 说明                                                            |
| ----------- | ------ | ---- | --------------------------------------------------------------- |
| name        | string | 是   | 模型名称（远程模型对应服务商的 model id，本地模型为 Ollama tag）|
| type        | string | 是   | 模型类型，可选值：`KnowledgeQA` / `Embedding` / `Rerank` / `VLLM` / `ASR` |
| source      | string | 是   | 模型来源，可选值：`local` / `remote`                            |
| description | string | 否   | 模型描述                                                        |
| parameters  | object | 是   | 模型参数，见下方远程模型参数              |

远程模型参数（`source=remote`，OpenAI 兼容接口）：

| 字段     | 类型   | 必填 | 说明                                    |
| -------- | ------ | ---- | --------------------------------------- |
| base_url | string | 是   | 服务地址（OpenAI 兼容，含 `/v1` 路径）  |
| api_key  | string | 是   | 服务密钥                                |
| provider | string | 否   | 服务商标识，通用/自建网关用 `generic`   |

请求示例（远程模型）：

```json
{
  "name": "Qwen3.8-27B-FP8-E",
  "type": "KnowledgeQA",
  "source": "remote",
  "description": "内网模型",
  "parameters": {
    "base_url": "http://model.example.com:30131/v1",
    "api_key": "sk-xxxxx",
    "provider": "generic"
  }
}
```

响应：201：创建的模型。

### 16.3 获取模型列表

**GET `/api/v1/models`**

响应：200：模型列表。

### 16.4 获取模型详情

**GET `/api/v1/models/{id}`**

| 字段 | 类型   | 必填 | 说明     |
| ---- | ------ | ---- | -------- |
| id   | string | 是   | 模型 ID  |

响应：200：模型详情。

### 16.5 更新模型

**PUT `/api/v1/models/{id}`**

| 字段 | 类型   | 必填 | 说明     |
| ---- | ------ | ---- | -------- |
| id   | string | 是   | 模型 ID  |

| 字段        | 类型   | 必填 | 说明                                                            |
| ----------- | ------ | ---- | --------------------------------------------------------------- |
| name        | string | 否   | 模型名称（为空字符串时保留原值）                                |
| description | string | 否   | 模型描述（始终覆盖，传空字符串会清空）                          |
| type        | string | 否   | 模型类型，取值同创建接口                                        |
| source      | string | 否   | 模型来源，取值同创建接口                                        |
| parameters  | object | 否   | 模型参数；`parameter_size` 由后端管理，请求中无需提供；`extra_config` 为空时会沿用旧值 |

请求示例：

```json
{
  "name": "gte-rerank-v2",
  "type": "Rerank",
  "source": "remote",
  "description": "阿里云 GTE Rerank 模型 V2",
  "parameters": {
    "base_url": "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
    "api_key": "sk-your-new-api-key",
    "provider": "aliyun"
  }
}
```

响应：200：更新后的模型。

### 16.6 删除模型

**DELETE `/api/v1/models/{id}`**

| 字段 | 类型   | 必填 | 说明     |
| ---- | ------ | ---- | -------- |
| id   | string | 是   | 模型 ID  |

响应示例（主要字段）：

```json
{
  "success": true,
  "message": "Model deleted"
}
```


## 17. 初始化管理

### 17.1 获取知识库初始化配置

**GET `/api/v1/initialization/config/{kb_id}`**

响应示例（主要字段）：

```json
{
  "data": {
    "hasFiles": false,
    "documentSplitting": {
      "chunkSize": 1500,
      "chunkOverlap": 150,
      "separators": null
    },
    "llm": {
      "baseUrl": "http://model.example.com:30131/v1",
      "credentials": {
        "apiKey": true
      },
      "modelName": "Qwen3.8-27B-FP8-E",
      "source": "remote"
    },
    "multimodal": {
      "enabled": false
    },
    "nodeExtract": {
      "enabled": false
    },
    "questionGeneration": {
      "enabled": false,
      "questionCount": 0,
      "customInstructions": ""
    },
    "rerank": {
      "enabled": false,
      "baseUrl": "",
      "modelName": "",
      "credentials": {
        "apiKey": false
      }
    }
  },
  "success": true
}
```

### 17.2 初始化知识库模型配置

**POST `/api/v1/initialization/initialize/{kb_id}`**

请求示例：

```json
{
  "chat_model_id": "model-00000001",
  "embedding_model_id": "model-00000002",
  "rerank_model_id": "model-00000003",
  "multimodal_id": "model-00000004"
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 17.3 更新知识库模型配置

**PUT `/api/v1/initialization/config/{kb_id}`**

| 字段         | 类型   | 必填 | 说明                              |
| ------------ | ------ | ---- | --------------------------------- |
| llmModelId   | string | 是   | 问答模型 ID（来自 GET `/api/v1/models`） |

请求示例：

```json
{
  "llmModelId": "11111111-2222-3333-4444-555555555555"
}
```

响应示例：

```json
{
  "message": "配置更新成功",
  "success": true
}
```

### 17.4 检查 Ollama 状态

**GET `/api/v1/initialization/ollama/status`**

响应示例（主要字段）：

```json
{
  "data": {
    "available": true
  },
  "success": true
}
```

### 17.5 获取本地 Ollama 模型列表

**GET `/api/v1/initialization/ollama/models`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "name": "llama3:8b",
      "size": 4661211648,
      "modified_at": "2025-08-10T15:30:00+08:00"
    }
  ],
  "success": true
}
```

### 17.6 检查 Ollama 模型是否可用

**POST `/api/v1/initialization/ollama/models/check`**

请求示例：

```json
{
  "models": [
    "llama3:8b"
  ]
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "llama3:8b": true,
    "nomic-embed-text:latest": true,
    "mistral:7b": false
  },
  "success": true
}
```

### 17.7 下载 Ollama 模型

**POST `/api/v1/initialization/ollama/models/download`**

请求示例：

```json
{
  "model": "mistral:7b"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "task-00000001",
    "modelName": "mistral:7b",
    "status": "downloading",
    "progress": 0,
    "message": "开始下载",
    "startTime": "2025-08-12T10:00:00+08:00"
  },
  "success": true
}
```

### 17.8 获取下载进度

**GET `/api/v1/initialization/ollama/download/progress/{task_id}`**

响应示例（主要字段）：

```json
{
  "data": {
    "id": "task-00000001",
    "modelName": "mistral:7b",
    "status": "downloading",
    "progress": 45.6,
    "message": "正在下载 2.1GB / 4.6GB",
    "startTime": "2025-08-12T10:00:00+08:00"
  },
  "success": true
}
```

### 17.9 获取所有下载任务

**GET `/api/v1/initialization/ollama/download/tasks`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "task-00000001",
      "modelName": "mistral:7b",
      "status": "completed",
      "progress": 100,
      "message": "下载完成",
      "startTime": "2025-08-12T10:00:00+08:00",
      "endTime": "2025-08-12T10:15:00+08:00"
    }
  ],
  "success": true
}
```

### 17.10 检查远程模型 API

**POST `/api/v1/initialization/remote/check`**

请求示例：

```json
{
  "baseUrl": "https://api.openai.com/v1",
  "apiKey": "sk-xxxxx",
  "modelName": "gpt-4o"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "available": true,
    "message": "连接正常，模型可用"
  },
  "success": true
}
```

### 17.11 测试嵌入模型

**POST `/api/v1/initialization/embedding/test`**

请求示例：

```json
{
  "api_url": "https://api.openai.com/v1",
  "api_key": "sk-xxxxx",
  "model": "text-embedding-3-small"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "success": true,
    "message": "嵌入模型测试通过"
  },
  "success": true
}
```

### 17.12 检查重排序模型

**POST `/api/v1/initialization/rerank/check`**

请求示例：

```json
{
  "api_url": "https://api.cohere.ai/v1",
  "api_key": "sk-xxxxx",
  "model": "rerank-english-v3.0"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "success": true,
    "message": "重排序模型可用"
  },
  "success": true
}
```

### 17.13 测试多模态模型

**POST `/api/v1/initialization/multimodal/test`**

请求示例：

```json
{
  "api_url": "https://api.openai.com/v1",
  "api_key": "sk-xxxxx",
  "model": "gpt-4o"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "success": true,
    "message": "多模态模型测试通过"
  },
  "success": true
}
```

### 17.14 提取文本关系

**POST `/api/v1/initialization/extract/text-relation`**

请求示例：

```json
{
  "text": "jingboWiki 是一个知识管理平台，支持多种文档格式的解析和检索。",
  "model_id": "model-00000001"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "entities": [
      {
        "name": "jingboWiki",
        "type": "Product"
      }
    ],
    "relations": [
      {
        "source": "jingboWiki",
        "target": "知识管理平台",
        "relation": "is_a"
      }
    ]
  },
  "success": true
}
```

### 17.15 检查ASR模型

**POST `/api/v1/initialization/asr/check`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| apiKey | body | string | 否 |  |
| appSecret | body | string | 否 | AppSecret 用于 LKEAP / Volcengine Rerank 等需要第二段密钥的场景（对应模型 Parameters.AppSecret）。 |
| baseUrl | body | string | 否 |  |
| customHeaders | body | object | 否 |  |
| dimension | body | integer | 否 |  |
| extraConfig | body | object | 否 |  |
| interfaceType | body | string | 否 |  |
| modelId | body | string | 否 | ModelID, when set, instructs the handler to substitute any missing |
| modelName | body | string | 是 |  |
| provider | body | string | 否 |  |
| source | body | string | 否 | 为空时按需默认为 "remote" |
| supportsDimensionOverride | body | boolean | 否 |  |

响应：200：检查结果。

### 17.16 获取知识库配置

**GET `/api/v1/initialization/config/{kbId}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kbId | path | string | 是 | 知识库ID |

响应：200：配置信息。

### 17.17 更新知识库配置

**PUT `/api/v1/initialization/config/{kbId}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kbId | path | string | 是 | 知识库ID |
| asr_config | body | object | 否 |  |
| documentSplitting | body | object | 否 | 文档分块配置 |
| embeddingModelId | body | string | 否 | optional when RAG indexing is disabled |
| llmModelId | body | string | 是 |  |
| multimodal | body | object | 否 | 多模态配置（仅模型相关；存储引擎在 storageProvider 中配置） |
| nodeExtract | body | object | 否 | 知识图谱配置 |
| questionGeneration | body | object | 否 | 问题生成配置 |
| storageBackendId | body | string | 否 |  |
| storageProvider | body | string | 否 | 存储引擎选择（"local" / "minio" / "cos"），影响文档上传与文档内图片存储，参数从全局设置读取 |
| vlm_config | body | object | 否 |  |

响应：200：更新成功。

### 17.18 生成随机标签

**POST `/api/v1/initialization/extract/fabri-tag`**

响应：200：生成的标签。

### 17.19 生成示例文本

**POST `/api/v1/initialization/extract/fabri-text`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| model_id | body | string | 是 |  |
| tags | body | array | 否 |  |

响应：200：生成的文本。

### 17.20 初始化知识库配置

**POST `/api/v1/initialization/initialize/{kbId}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kbId | path | string | 是 | 知识库ID |
| documentSplitting | body | object | 是 |  |
| embedding | body | object | 是 |  |
| llm | body | object | 是 |  |
| multimodal | body | object | 否 |  |
| nodeExtract | body | object | 否 |  |
| questionGeneration | body | object | 否 |  |
| rerank | body | object | 否 |  |

响应：200：初始化成功。

### 17.21 获取下载进度

**GET `/api/v1/initialization/ollama/download/progress/{taskId}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| taskId | path | string | 是 | 任务ID |

响应：200：下载进度。


## 18. 系统管理

### 18.1 获取系统信息

**GET `/api/v1/system/info`**

响应示例（主要字段）：

```json
{
  "data": {
    "version": "1.2.0",
    "edition": "community",
    "commit_id": "a1b2c3d",
    "build_time": "2025-08-12T08:00:00Z",
    "go_version": "go1.21.5",
    "keyword_index_engine": "bleve",
    "vector_store_engine": "milvus",
    "graph_database_engine": "neo4j"
  },
  "success": true
}
```

### 18.2 获取解析引擎列表

**GET `/api/v1/system/parser-engines`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "name": "docreader",
      "label": "DocReader",
      "description": "高精度文档解析引擎",
      "available": true
    }
  ],
  "connected": true,
  "success": true
}
```

### 18.3 检查解析引擎可用性

**POST `/api/v1/system/parser-engines/check`**

请求示例：

```json
{
  "addr": "http://docreader:8000"
}
```

响应示例（主要字段）：

```json
{
  "data": [
    {
      "name": "docreader",
      "label": "DocReader",
      "description": "高精度文档解析引擎",
      "available": true
    }
  ],
  "success": true
}
```

### 18.4 重连文档解析服务

**POST `/api/v1/system/docreader/reconnect`**

请求示例：

```json
{
  "addr": "http://docreader:8000"
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 18.5 获取存储引擎状态

**GET `/api/v1/system/storage-engine-status`**

响应示例（主要字段）：

```json
{
  "data": {
    "engines": [
      {
        "name": "minio",
        "available": true,
        "description": "MinIO 对象存储"
      }
    ],
    "minio_env_available": true
  },
  "success": true
}
```

### 18.6 检查存储引擎连通性

**POST `/api/v1/system/storage-engine-check`**

请求示例：

```json
{
  "provider": "minio",
  "minio": {
    "endpoint": "localhost:9000",
    "access_key": "minioadmin",
    "secret_key": "minioadmin",
    "bucket": "jingboWiki",
    "use_ssl": false
  }
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "ok": true,
    "message": "连接成功",
    "bucket_created": false
  },
  "success": true
}
```

### 18.7 List platform API keys

**GET `/api/v1/system/admin/api-keys`**

响应：200：OK。

### 18.8 Create a platform API key

**POST `/api/v1/system/admin/api-keys`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| capabilities | body | array | 否 |  |
| expires_at_unix | body | integer | 否 |  |
| name | body | string | 否 |  |

响应：201：Created。

### 18.9 Revoke a platform API key

**DELETE `/api/v1/system/admin/api-keys/{key_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| key_id | path | integer | 是 | API key ID |

响应：200：OK。

### 18.10 List all system administrators

**GET `/api/v1/system/admin/list`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| offset | query | integer | 否 | Page offset |
| limit | query | integer | 否 | Page size (max 200) |

响应：200：System admins retrieved successfully。

### 18.11 Promote a user to system administrator

**POST `/api/v1/system/admin/promote`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| email | body | string | 否 |  |
| user_id | body | string | 否 |  |

响应：200：User promoted successfully。

### 18.12 Revoke system administrator privileges from a user

**POST `/api/v1/system/admin/revoke`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| user_id | body | string | 是 |  |

响应：200：Privileges revoked successfully。

### 18.13 List runtime queue tasks by state

**GET `/api/v1/system/admin/runtime/queues/{queue}/tasks`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| queue | path | string | 是 | Queue name |
| state | query | string | 是 | Task state |
| cursor | query | string | 否 | Opaque continuation cursor |
| page_size | query | integer | 否 | Page size |

响应：200：OK。

### 18.14 Run a safe runtime task action

**POST `/api/v1/system/admin/runtime/queues/{queue}/tasks/{task_id}/actions/{action}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| queue | path | string | 是 | Queue name |
| task_id | path | string | 是 | Task ID |
| action | path | string | 是 | Action |

响应：200：OK。

### 18.15 Get a single system setting by key

**GET `/api/v1/system/admin/settings/{key}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| key | path | string | 是 | Setting key (e.g. file.max_size_mb) |

响应：200：the setting row。

### 18.16 Update a system setting value

**PUT `/api/v1/system/admin/settings/{key}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| key | path | string | 是 | Setting key |
| value | body | object | 否 | Value is intentionally `any` (decoded as float64 / string / bool / |

响应：200：the updated row。

### 18.17 Reset a system setting to ENV / built-in default

**DELETE `/api/v1/system/admin/settings/{key}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| key | path | string | 是 | Setting key |

响应：200：Reset acknowledged。

### 18.18 Apply the default storage quota to every existing workspace

**POST `/api/v1/system/admin/tenants/apply-default-storage-quota`**

响应：200：{ affected: int64, quota_bytes: int64 }。

### 18.19 Reset another user's password

**POST `/api/v1/system/admin/users/reset-password`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| email | body | string | 是 |  |
| new_password | body | string | 是 |  |

响应：200：Password reset successfully。

### 18.20 获取解析任务队列运行时状态

**GET `/api/v1/system/admin/runtime/queues`**

响应：200：OK。


## 19. 评估管理

### 19.1 获取评估任务结果

**GET `/api/v1/evaluation`**

| 字段     | 类型   | 必填 | 说明                                                |
| -------- | ------ | ---- | --------------------------------------------------- |
| task_id  | string | 是   | 从 `POST /evaluation` 返回的任务 ID                  |

响应示例（主要字段）：

```json
{
  "data": {
    "task": {
      "id": "c34563ad-b09f-4858-b72e-e92beb80becb",
      "tenant_id": 1,
      "dataset_id": "default",
      "start_time": "2025-08-12T14:54:26.221804768+08:00",
      "status": 2,
      "total": 1,
      "finished": 1
    },
    "params": {
      "session_id": "",
      "knowledge_base_id": "2ef57434-8c8d-4442-b967-2f7fc578a2fc",
      "vector_threshold": 0.5,
      "keyword_threshold": 0.3,
      "embedding_top_k": 10,
      "vector_database": "",
      "rerank_model_id": "b30171a1-787b-426e-a293-735cd5ac16c0",
      "rerank_top_k": 5
    },
    "metric": {
      "retrieval_metrics": {
        "precision": 0,
        "recall": 0,
        "ndcg3": 0,
        "ndcg10": 0,
        "mrr": 0,
        "map": 0
      },
      "generation_metrics": {
        "bleu1": 0.037656734016532384,
        "bleu2": 0.04067392145167686,
        "bleu4": 0.048963321289052536,
        "rouge1": 0,
        "rouge2": 0,
        "rougel": 0
      }
    }
  },
  "success": true
}
```

### 19.2 创建评估任务

**POST `/api/v1/evaluation`**

| 字段              | 类型   | 必填 | 说明                                            |
| ----------------- | ------ | ---- | ----------------------------------------------- |
| dataset_id        | string | 是   | 评估数据集，目前仅支持 `default`（官方测试集）   |
| knowledge_base_id | string | 是   | 评估使用的知识库 ID                              |
| chat_id           | string | 是   | 评估使用的对话模型 ID                            |
| rerank_id         | string | 是   | 评估使用的重排序模型 ID                          |

请求示例：

```json
{
  "dataset_id": "default",
  "knowledge_base_id": "kb-00000001",
  "chat_id": "8aea788c-bb30-4898-809e-e40c14ffb48c",
  "rerank_id": "b30171a1-787b-426e-a293-735cd5ac16c0"
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "task": {
      "id": "c34563ad-b09f-4858-b72e-e92beb80becb",
      "tenant_id": 1,
      "dataset_id": "default",
      "start_time": "2025-08-12T14:54:26.221804768+08:00",
      "status": 1
    },
    "params": {
      "session_id": "",
      "knowledge_base_id": "2ef57434-8c8d-4442-b967-2f7fc578a2fc",
      "vector_threshold": 0.5,
      "keyword_threshold": 0.3,
      "embedding_top_k": 10,
      "vector_database": "",
      "rerank_model_id": "b30171a1-787b-426e-a293-735cd5ac16c0",
      "rerank_top_k": 5
    }
  },
  "success": true
}
```

### 19.3 获取评估结果

**GET `/api/v1/evaluation/`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| task_id | query | string | 是 | 评估任务ID |

响应：200：评估结果。

### 19.4 执行评估

**POST `/api/v1/evaluation/`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| chat_id | body | string | 否 | ID of chat model to use |
| dataset_id | body | string | 否 | ID of dataset to evaluate |
| knowledge_base_id | body | string | 否 | ID of knowledge base to use |
| rerank_id | body | string | 否 | ID of rerank model to use |

响应：200：评估任务。


## 20. 技能管理

### 20.1 获取预装 Skills 列表

**GET `/api/v1/skills`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "name": "web_search",
      "description": "搜索互联网获取最新信息"
    }
  ],
  "skills_available": true,
  "success": true
}
```


## 21. 网络搜索

### 21.1 获取网络搜索服务商类型列表

**GET `/api/v1/web-search/providers`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "name": "google",
      "label": "Google Search",
      "description": "通过 Google 自定义搜索 API 进行网络搜索",
      "enabled": true
    }
  ],
  "success": true
}
```

### 21.2 获取 Provider 类型元数据

**GET `/api/v1/web-search-providers/types`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "provider": "google",
      "label": "Google Search",
      "description": "...",
      "parameter_schema": [
        {
          "name": "api_key",
          "label": "API Key",
          "type": "string",
          "required": true
        }
      ]
    }
  ],
  "success": true
}
```

### 21.3 使用原始凭证测试连通性

**POST `/api/v1/web-search-providers/test`**

| 字段       | 类型   | 必填 | 说明                              |
| ---------- | ------ | ---- | --------------------------------- |
| provider   | string | 是   | provider 类型（如 `google`、`bing`） |
| parameters | object | 是   | 该 provider 所需凭证与参数（与 `/types` 中 `parameter_schema` 对应） |

请求示例：

```json
{
  "provider": "google",
  "parameters": {
    "api_key": "AIza...",
    "cx": "0123456789:abcdefg"
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 21.4 创建 Provider

**POST `/api/v1/web-search-providers`**

| 字段        | 类型    | 必填 | 说明                                       |
| ----------- | ------- | ---- | ------------------------------------------ |
| name        | string  | 是   | Provider 显示名（在空间内唯一友好名）       |
| provider    | string  | 是   | Provider 类型（来自 `/web-search-providers/types`） |
| description | string  | 否   | 备注                                       |
| parameters  | object  | 否   | 凭证与参数                                 |
| is_default  | boolean | 否   | 是否设为当前空间默认 Provider              |

请求示例：

```json
{
  "name": "公司 Google CSE",
  "provider": "google",
  "description": "用于内网搜索",
  "parameters": {
    "api_key": "AIza...",
    "cx": "0123456789:abcdefg"
  },
  "is_default": true
}
```

响应示例（主要字段）：

```json
{
  "data": {
    "id": "wsp-...",
    "tenant_id": 1,
    "name": "公司 Google CSE",
    "provider": "google",
    "is_default": true,
    "parameters": {
      "api_key": "***",
      "cx": "0123456789:abcdefg"
    }
  },
  "success": true
}
```

### 21.5 获取 Provider 列表

**GET `/api/v1/web-search-providers`**

响应示例（主要字段）：

```json
{
  "data": [
    {
      "id": "wsp-001",
      "name": "公司 Google CSE",
      "provider": "google",
      "is_default": true
    }
  ],
  "success": true
}
```

### 21.6 获取 Provider 详情

**GET `/api/v1/web-search-providers/{id}`**

| 字段 | 类型   | 说明        |
| ---- | ------ | ----------- |
| id   | string | Provider ID |

响应：200：Provider 详情。

### 21.7 更新 Provider

**PUT `/api/v1/web-search-providers/{id}`**

请求示例：

```json
{
  "name": "公司 Google CSE (v2)",
  "parameters": {
    "api_key": "NEW...",
    "cx": "0123456789:abcdefg"
  },
  "is_default": false
}
```

响应：200：更新后的 Provider。

### 21.8 删除 Provider

**DELETE `/api/v1/web-search-providers/{id}`**

参数：`id`（path，必填）：Provider ID。

响应：200：success: true。

### 21.9 测试已保存的 Provider

**POST `/api/v1/web-search-providers/{id}/test`**

参数：`id`（path，必填）：Provider ID。

响应：200：测试结果。


## 22. 向量存储

### 22.1 获取支持的引擎类型

**GET `/api/v1/vector-stores/types`**

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "type": "elasticsearch",
      "display_name": "Elasticsearch (Keywords + Vector)",
      "connection_fields": [
        {
          "name": "addr",
          "type": "string",
          "required": true,
          "description": "Elasticsearch URL (e.g., http://localhost:9200)"
        }
      ],
      "index_fields": [
        {
          "name": "index_name",
          "type": "string",
          "required": false,
          "default": "xwrag_default"
        }
      ]
    }
  ]
}
```

### 22.2 使用原始凭据测试连接

**POST `/api/v1/vector-stores/test`**

| 字段              | 类型   | 必填 | 说明                                                          |
| ----------------- | ------ | ---- | ------------------------------------------------------------- |
| engine_type       | string | 是   | 引擎类型，取自 `/vector-stores/types` 的 `type`                |
| connection_config | object | 是   | 该引擎对应的连接配置字段（与 `connection_fields` 对应）         |

请求示例：

```json
{
  "engine_type": "elasticsearch",
  "connection_config": {
    "addr": "http://es:9200",
    "username": "elastic",
    "password": "changeme"
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "version": "7.10.1"
}
```

### 22.3 创建向量存储

**POST `/api/v1/vector-stores`**

| 字段              | 类型   | 必填 | 说明                                                            |
| ----------------- | ------ | ---- | --------------------------------------------------------------- |
| name              | string | 是   | 存储显示名（空间内友好名）                                       |
| engine_type       | string | 是   | 引擎类型，取自 `/vector-stores/types`                            |
| connection_config | object | 是   | 连接配置（与所选引擎的 `connection_fields` 对应）                |
| index_config      | object | 否   | 索引配置（与所选引擎的 `index_fields` 对应）                     |

请求示例：

```json
{
  "name": "elasticsearch-hot",
  "engine_type": "elasticsearch",
  "connection_config": {
    "addr": "http://es-hot:9200",
    "username": "elastic",
    "password": "changeme"
  },
  "index_config": {
    "index_name": "my_index"
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "elasticsearch-hot",
    "engine_type": "elasticsearch",
    "connection_config": {
      "addr": "http://es-hot:9200",
      "username": "elastic",
      "password": "***"
    },
    "index_config": {
      "index_name": "my_index"
    },
    "source": "user",
    "readonly": false,
    "created_at": "2026-04-07T10:00:00Z"
  }
}
```

### 22.4 获取向量存储列表

**GET `/api/v1/vector-stores`**

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "__env_postgres__",
      "name": "postgres (env)",
      "engine_type": "postgres",
      "connection_config": {
        "use_default_connection": true
      },
      "source": "env",
      "readonly": true
    }
  ]
}
```

### 22.5 获取向量存储详情

**GET `/api/v1/vector-stores/{id}`**

| 字段 | 类型   | 必填 | 说明                                                |
| ---- | ------ | ---- | --------------------------------------------------- |
| id   | string | 是   | 向量存储 ID（DB UUID 或 `__env_{driver}__`）          |

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "elasticsearch-hot",
    "engine_type": "elasticsearch",
    "connection_config": {
      "addr": "http://es-hot:9200",
      "username": "elastic",
      "password": "***",
      "version": "7.10.1"
    },
    "index_config": {
      "index_name": "my_index"
    },
    "source": "user",
    "readonly": false,
    "created_at": "2026-04-07T10:00:00Z"
  }
}
```

### 22.6 更新向量存储

**PUT `/api/v1/vector-stores/{id}`**

| 字段 | 类型   | 必填 | 说明           |
| ---- | ------ | ---- | -------------- |
| id   | string | 是   | 向量存储 ID    |

| 字段 | 类型   | 必填 | 说明              |
| ---- | ------ | ---- | ----------------- |
| name | string | 是   | 新的存储显示名     |

请求示例：

```json
{
  "name": "elasticsearch-hot-renamed"
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "elasticsearch-hot-renamed",
    "engine_type": "elasticsearch",
    "connection_config": {
      "addr": "http://es-hot:9200",
      "username": "elastic",
      "password": "***"
    },
    "index_config": {
      "index_name": "my_index"
    },
    "source": "user",
    "readonly": false,
    "created_at": "2026-04-07T10:00:00Z"
  }
}
```

### 22.7 删除向量存储

**DELETE `/api/v1/vector-stores/{id}`**

| 字段 | 类型   | 必填 | 说明           |
| ---- | ------ | ---- | -------------- |
| id   | string | 是   | 向量存储 ID    |

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 22.8 测试已保存或环境变量存储的连接

**POST `/api/v1/vector-stores/{id}/test`**

| 字段 | 类型   | 必填 | 说明                                                |
| ---- | ------ | ---- | --------------------------------------------------- |
| id   | string | 是   | 向量存储 ID（DB UUID 或 `__env_{driver}__`）          |

响应示例（主要字段）：

```json
{
  "success": true,
  "version": "7.10.1"
}
```


## 23. 存储后端

### 23.1 获取允许的存储类型

**GET `/api/v1/storage-backends/types`**

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    "local"
  ]
}
```

### 23.2 使用原始配置测试连通性

**POST `/api/v1/storage-backends/test`**

| 字段     | 类型   | 必填 | 说明                                        |
| -------- | ------ | ---- | ------------------------------------------- |
| name     | string | 是   | 实例显示名                                  |
| provider | string | 是   | 存储类型，取自 `/storage-backends/types`    |
| config   | object | 否   | 该 provider 对应的存储配置字段              |

请求示例：

```json
{
  "name": "s3-hot",
  "provider": "s3",
  "config": {
    "endpoint": "https://s3.example.com",
    "region": "ap-test-1",
    "access_key_id": "AKID",
    "secret_access_key": "SECRET",
    "bucket_name": "jingboWiki"
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 23.3 创建存储实例

**POST `/api/v1/storage-backends`**

| 字段     | 类型   | 必填 | 说明                                        |
| -------- | ------ | ---- | ------------------------------------------- |
| name     | string | 是   | 实例显示名（空间内唯一）                     |
| provider | string | 是   | 存储类型，取自 `/storage-backends/types`    |
| config   | object | 否   | 该 provider 对应的存储配置字段              |
| status   | string | 否   | `active`（默认）或 `disabled`               |

请求示例：

```json
{
  "name": "s3-hot",
  "provider": "s3",
  "config": {
    "endpoint": "https://s3.example.com",
    "region": "ap-test-1",
    "access_key_id": "AKID",
    "secret_access_key": "SECRET",
    "bucket_name": "jingboWiki",
    "path_prefix": "prod"
  }
}
```

响应示例（主要字段）：

```json
{
  "success": true,
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "s3-hot",
    "provider": "s3",
    "config": {
      "endpoint": "https://s3.example.com",
      "region": "ap-test-1",
      "access_key_id": "***",
      "secret_access_key": "***",
      "bucket_name": "jingboWiki",
      "path_prefix": "prod"
    },
    "source": "user",
    "status": "active",
    "legacy_alias": false,
    "created_at": "2026-07-15T10:00:00Z"
  }
}
```

### 23.4 获取存储实例列表

**GET `/api/v1/storage-backends`**

响应示例（主要字段）：

```json
{
  "success": true,
  "data": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "s3-hot",
      "provider": "s3",
      "config": {
        "endpoint": "https://s3.example.com",
        "access_key_id": "***",
        "secret_access_key": "***",
        "bucket_name": "jingboWiki"
      },
      "source": "user",
      "status": "active",
      "legacy_alias": false
    }
  ],
  "default_storage_backend_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 23.5 获取存储实例详情

**GET `/api/v1/storage-backends/{id}`**

| 字段 | 类型   | 必填 | 说明          |
| ---- | ------ | ---- | ------------- |
| id   | string | 是   | 存储实例 ID   |

响应：200：Storage backend details。

### 23.6 更新存储实例

**PUT `/api/v1/storage-backends/{id}`**

| 字段 | 类型   | 必填 | 说明          |
| ---- | ------ | ---- | ------------- |
| id   | string | 是   | 存储实例 ID   |

请求示例：

```json
{
  "name": "s3-hot-renamed",
  "provider": "s3",
  "config": {
    "access_key_id": "***",
    "secret_access_key": "NEW_SECRET"
  }
}
```

响应：200：Updated storage backend。

### 23.7 删除存储实例

**DELETE `/api/v1/storage-backends/{id}`**

| 字段 | 类型   | 必填 | 说明          |
| ---- | ------ | ---- | ------------- |
| id   | string | 是   | 存储实例 ID   |

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 23.8 测试已保存实例的连通性

**POST `/api/v1/storage-backends/{id}/test`**

| 字段 | 类型   | 必填 | 说明          |
| ---- | ------ | ---- | ------------- |
| id   | string | 是   | 存储实例 ID   |

响应示例（主要字段）：

```json
{
  "success": true
}
```

### 23.9 设为空间默认实例

**PUT `/api/v1/storage-backends/{id}/default`**

| 字段 | 类型   | 必填 | 说明          |
| ---- | ------ | ---- | ------------- |
| id   | string | 是   | 存储实例 ID   |

响应示例（主要字段）：

```json
{
  "success": true
}
```


## 24. 数据源管理

### 24.1 List data sources for a knowledge base

**GET `/api/v1/datasource`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | query | string | 是 | Knowledge base ID |

响应：200：OK。

### 24.2 Create a new data source

**POST `/api/v1/datasource`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| config | body | array | 否 | Encrypted configuration (API credentials, tokens, etc.) |
| conflict_strategy | body | string | 否 | Conflict resolution strategy: overwrite or skip |
| created_at | body | string | 否 | Creation timestamp |
| deleted_at | body | object | 否 | Soft delete timestamp |
| error_message | body | string | 否 | Error message if status is "error" |
| id | body | string | 否 | Unique identifier |
| knowledge_base_id | body | string | 否 | Target knowledge base ID |
| last_sync_at | body | string | 否 | Last successful sync timestamp |
| last_sync_cursor | body | array | 否 | Cursor or state for incremental sync (connector-specific) |
| last_sync_result | body | array | 否 | Summary of last sync result |
| latest_sync_log | body | object | 否 | Latest sync log (not stored in DB, populated on query) |
| name | body | string | 否 | User-friendly name |
| status | body | string | 否 | Current status: active, paused, error |
| sync_deletions | body | boolean | 否 | Whether to sync deletions from source |
| sync_log_retention_days | body | integer | 否 | Number of days to keep sync logs (default: 30) |
| sync_mode | body | string | 否 | Sync mode: "incremental" (recommended) or "full" |
| sync_schedule | body | string | 否 | Cron expression for scheduled syncs (e.g., "0 */6 * * *" = every 6 hours) |
| tenant_id | body | integer | 否 | Workspace ID for multi-workspace isolation |
| total_items_synced | body | integer | 否 | Total items synced (not stored in DB, calculated on query) |
| type | body | string | 否 | Connector type (feishu, notion, confluence, etc.) |
| updated_at | body | string | 否 | Last update timestamp |

响应：201：Created。

### 24.3 Get specific sync log

**GET `/api/v1/datasource/logs/{log_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| log_id | path | string | 是 | Sync log ID |

响应：200：OK。

### 24.4 Get available connectors

**GET `/api/v1/datasource/types`**

响应：200：OK。

### 24.5 Test connection with raw credentials (no persistence)

**POST `/api/v1/datasource/validate-credentials`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| request | body | object | 是 | type and credentials |

响应：200：OK。

### 24.6 Get a data source by ID

**GET `/api/v1/datasource/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：200：OK。

### 24.7 Update a data source

**PUT `/api/v1/datasource/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |
| config | body | array | 否 | Encrypted configuration (API credentials, tokens, etc.) |
| conflict_strategy | body | string | 否 | Conflict resolution strategy: overwrite or skip |
| created_at | body | string | 否 | Creation timestamp |
| deleted_at | body | object | 否 | Soft delete timestamp |
| error_message | body | string | 否 | Error message if status is "error" |
| id | body | string | 否 | Unique identifier |
| knowledge_base_id | body | string | 否 | Target knowledge base ID |
| last_sync_at | body | string | 否 | Last successful sync timestamp |
| last_sync_cursor | body | array | 否 | Cursor or state for incremental sync (connector-specific) |
| last_sync_result | body | array | 否 | Summary of last sync result |
| latest_sync_log | body | object | 否 | Latest sync log (not stored in DB, populated on query) |
| name | body | string | 否 | User-friendly name |
| status | body | string | 否 | Current status: active, paused, error |
| sync_deletions | body | boolean | 否 | Whether to sync deletions from source |
| sync_log_retention_days | body | integer | 否 | Number of days to keep sync logs (default: 30) |
| sync_mode | body | string | 否 | Sync mode: "incremental" (recommended) or "full" |
| sync_schedule | body | string | 否 | Cron expression for scheduled syncs (e.g., "0 */6 * * *" = every 6 hours) |
| tenant_id | body | integer | 否 | Workspace ID for multi-workspace isolation |
| total_items_synced | body | integer | 否 | Total items synced (not stored in DB, calculated on query) |
| type | body | string | 否 | Connector type (feishu, notion, confluence, etc.) |
| updated_at | body | string | 否 | Last update timestamp |

响应：200：OK。

### 24.8 Delete a data source

**DELETE `/api/v1/datasource/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：204：No Content。

### 24.9 Get sync logs

**GET `/api/v1/datasource/{id}/logs`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |
| limit | query | integer | 否 | Limit (default: 10) |
| offset | query | integer | 否 | Offset (default: 0) |

响应：200：OK。

### 24.10 Pause data source

**POST `/api/v1/datasource/{id}/pause`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：200：OK。

### 24.11 Resolve resource ancestors

**POST `/api/v1/datasource/{id}/resource-ancestors`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |
| resource_ids | body | array | 否 |  |

响应：200：OK。

### 24.12 List available resources in data source

**GET `/api/v1/datasource/{id}/resources`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |
| parent_id | query | string | 否 | Parent resource ExternalID; empty lists the top level |

响应：200：OK。

### 24.13 Resume data source

**POST `/api/v1/datasource/{id}/resume`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：200：OK。

### 24.14 Trigger immediate sync

**POST `/api/v1/datasource/{id}/sync`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：200：OK。

### 24.15 Test data source connection

**POST `/api/v1/datasource/{id}/validate`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | Data source ID |

响应：200：OK。


## 25. IM 渠道

### 25.1 更新 IM 渠道

**PUT `/api/v1/im-channels/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 渠道 ID |
| request | body | object | 是 | 更新字段（name/mode/output_mode/knowledge_base_id/credentials/enabled） |

响应：200：更新后的渠道。

### 25.2 删除 IM 渠道

**DELETE `/api/v1/im-channels/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 渠道 ID |

响应：200：success: true。

### 25.3 启用/停用 IM 渠道

**POST `/api/v1/im-channels/{id}/toggle`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 渠道 ID |

响应：200：更新后的渠道。

### 25.4 获取微信扫码登录二维码

**POST `/api/v1/wechat/qrcode`**

响应：200：二维码信息（qrcode_url + qrcode 标识）。

### 25.5 轮询微信二维码状态

**POST `/api/v1/wechat/qrcode/status`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| request | body | object | 是 | {qrcode: string} |

响应：200：扫码状态。


## 26. IM 回调

### 26.1 IM 平台回调

**GET `/api/v1/im/callback/{channel_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| channel_id | path | string | 是 | 渠道 ID |

响应：200：处理结果。

### 26.2 IM 平台回调

**POST `/api/v1/im/callback/{channel_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| channel_id | path | string | 是 | 渠道 ID |

响应：200：处理结果。


## 27. 知识 Wiki

### 27.1 Auto-fix wiki issues

**POST `/api/v1/knowledgebase/{kb_id}/wiki/auto-fix`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |

响应：200：OK。

### 27.2 List wiki folders

**GET `/api/v1/knowledgebase/{kb_id}/wiki/folders`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| parent_id | query | string | 否 | Parent folder id (empty = root) |

响应：200：OK。

### 27.3 Create a wiki folder

**POST `/api/v1/knowledgebase/{kb_id}/wiki/folders`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| name | body | string | 否 |  |
| parent_id | body | string | 否 |  |

响应：201：Created。

### 27.4 Rename or move a wiki folder

**PUT `/api/v1/knowledgebase/{kb_id}/wiki/folders/{folder_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| folder_id | path | string | 是 | Folder ID |
| move_parent | body | boolean | 否 |  |
| name | body | string | 否 |  |
| parent_id | body | string | 否 |  |

响应：200：OK。

### 27.5 Delete an empty wiki folder

**DELETE `/api/v1/knowledgebase/{kb_id}/wiki/folders/{folder_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| folder_id | path | string | 是 | Folder ID |

响应：204：No Content。

### 27.6 Get wiki link graph

**GET `/api/v1/knowledgebase/{kb_id}/wiki/graph`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| mode | query | string | 否 | overview (default) / ego |
| center | query | string | 否 | Center slug for ego mode |
| depth | query | integer | 否 | Ego BFS depth (1-3, default 1) |
| types | query | string | 否 | Comma-separated page_type allow-list |
| limit | query | integer | 否 | Max nodes to return (default 500, max 2000) |

响应：200：OK。

### 27.7 Get wiki index view

**GET `/api/v1/knowledgebase/{kb_id}/wiki/index`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| types | query | string | 否 | Comma-separated page types (default: all content types) |
| limit | query | integer | 否 | Per-group window size, 1-200 (default 50) |
| cursor | query | string | 否 | Opaque offset cursor from previous response |

响应：200：OK。

### 27.8 List wiki page issues

**GET `/api/v1/knowledgebase/{kb_id}/wiki/issues`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| slug | query | string | 否 | Filter by page slug |
| status | query | string | 否 | Filter by status (pending, ignored, resolved) |

响应：200：OK。

### 27.9 Update wiki page issue status

**PUT `/api/v1/knowledgebase/{kb_id}/wiki/issues/{issue_id}/status`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| issue_id | path | string | 是 | Issue ID |
| status | body | object | 是 | New status {'status': 'ignored'} |

响应：200：OK。

### 27.10 Run wiki lint

**GET `/api/v1/knowledgebase/{kb_id}/wiki/lint`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |

响应：200：OK。

### 27.11 Get wiki operation log

**GET `/api/v1/knowledgebase/{kb_id}/wiki/log`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| cursor | query | string | 否 | Opaque cursor from the previous page (empty = newest) |
| limit | query | integer | 否 | Page size, 1-200 (default 50) |

响应：200：OK。

### 27.12 Move a wiki page into a folder

**PUT `/api/v1/knowledgebase/{kb_id}/wiki/move-page`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| folder_id | body | string | 否 |  |
| slug | body | string | 是 |  |

响应：200：OK。

### 27.13 List wiki pages

**GET `/api/v1/knowledgebase/{kb_id}/wiki/pages`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| page_type | query | string | 否 | Filter by page type; comma-separated for multiple (e.g. entity,concept) |
| status | query | string | 否 | Filter by status |
| query | query | string | 否 | Full-text search |
| page | query | integer | 否 | Page number |
| page_size | query | integer | 否 | Page size |
| sort_by | query | string | 否 | Sort field |
| sort_order | query | string | 否 | Sort order (asc/desc) |

响应：200：OK。

### 27.14 Create a wiki page

**POST `/api/v1/knowledgebase/{kb_id}/wiki/pages`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| aliases | body | array | 否 | Alternate names, abbreviations, acronyms or translated names |
| category_path | body | array | 否 | CategoryPath is the directory breadcrumb that groups this page in the |
| chunk_refs | body | array | 否 | ChunkRefs records the specific source-document chunks this page was |
| content | body | string | 否 | Full markdown content |
| created_at | body | string | 否 | Creation time |
| deleted_at | body | object | 否 | Soft delete |
| depth | body | integer | 否 | Depth is len(CategoryPath), cached for filtering / display. |
| folder_id | body | string | 否 | FolderID is the single source of truth for where this page sits in the |
| id | body | string | 否 | Unique identifier (UUID) |
| in_links | body | array | 否 | Slugs of pages that link TO this page (backlinks) |
| knowledge_base_id | body | string | 否 | Knowledge base this page belongs to |
| out_links | body | array | 否 | Slugs of pages this page links to (outbound links) |
| page_metadata | body | array | 否 | Arbitrary metadata (tags, categories, dates, etc.) |
| page_type | body | string | 否 | Page type: summary, entity, concept, index, log, synthesis, comparison |
| parent_slug | body | string | 否 | ParentSlug optionally points at the wiki page that should act as this |
| slug | body | string | 否 | URL-friendly slug for addressing, e.g. "entity/acme-corp", "concept/rag" |
| sort_order | body | integer | 否 | SortOrder allows generated or manually edited pages to control sibling |
| source_refs | body | array | 否 | References to source knowledge IDs that contributed to this page. |
| status | body | string | 否 | Page status: draft, published, archived |
| summary | body | string | 否 | One-line summary for index listing |
| tenant_id | body | integer | 否 | Workspace ID for multi-workspace isolation |
| title | body | string | 否 | Human-readable title |
| updated_at | body | string | 否 | Last update time |
| version | body | integer | 否 | Version number. Incremented only when a user-visible content field |
| wiki_path | body | string | 否 | WikiPath is a normalized, sortable path derived from page_type, |

响应：201：Created。

### 27.15 Get a wiki page by slug

**GET `/api/v1/knowledgebase/{kb_id}/wiki/pages/{slug}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| slug | path | string | 是 | Page slug |

响应：200：OK。

### 27.16 Update a wiki page

**PUT `/api/v1/knowledgebase/{kb_id}/wiki/pages/{slug}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| slug | path | string | 是 | Page slug |
| aliases | body | array | 否 | Alternate names, abbreviations, acronyms or translated names |
| category_path | body | array | 否 | CategoryPath is the directory breadcrumb that groups this page in the |
| chunk_refs | body | array | 否 | ChunkRefs records the specific source-document chunks this page was |
| content | body | string | 否 | Full markdown content |
| created_at | body | string | 否 | Creation time |
| deleted_at | body | object | 否 | Soft delete |
| depth | body | integer | 否 | Depth is len(CategoryPath), cached for filtering / display. |
| folder_id | body | string | 否 | FolderID is the single source of truth for where this page sits in the |
| id | body | string | 否 | Unique identifier (UUID) |
| in_links | body | array | 否 | Slugs of pages that link TO this page (backlinks) |
| knowledge_base_id | body | string | 否 | Knowledge base this page belongs to |
| out_links | body | array | 否 | Slugs of pages this page links to (outbound links) |
| page_metadata | body | array | 否 | Arbitrary metadata (tags, categories, dates, etc.) |
| page_type | body | string | 否 | Page type: summary, entity, concept, index, log, synthesis, comparison |
| parent_slug | body | string | 否 | ParentSlug optionally points at the wiki page that should act as this |
| slug | body | string | 否 | URL-friendly slug for addressing, e.g. "entity/acme-corp", "concept/rag" |
| sort_order | body | integer | 否 | SortOrder allows generated or manually edited pages to control sibling |
| source_refs | body | array | 否 | References to source knowledge IDs that contributed to this page. |
| status | body | string | 否 | Page status: draft, published, archived |
| summary | body | string | 否 | One-line summary for index listing |
| tenant_id | body | integer | 否 | Workspace ID for multi-workspace isolation |
| title | body | string | 否 | Human-readable title |
| updated_at | body | string | 否 | Last update time |
| version | body | integer | 否 | Version number. Incremented only when a user-visible content field |
| wiki_path | body | string | 否 | WikiPath is a normalized, sortable path derived from page_type, |

响应：200：OK。

### 27.17 Delete a wiki page

**DELETE `/api/v1/knowledgebase/{kb_id}/wiki/pages/{slug}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| slug | path | string | 是 | Page slug |

响应：204：No Content。

### 27.18 Rebuild wiki links

**POST `/api/v1/knowledgebase/{kb_id}/wiki/rebuild-links`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |

响应：200：OK。

### 27.19 Search wiki pages

**GET `/api/v1/knowledgebase/{kb_id}/wiki/search`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |
| q | query | string | 是 | Search query |
| limit | query | integer | 否 | Max results (default 10) |

响应：200：OK。

### 27.20 Get wiki statistics

**GET `/api/v1/knowledgebase/{kb_id}/wiki/stats`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| kb_id | path | string | 是 | Knowledge base ID |

响应：200：OK。


## 28. 我的邀请

### 28.1 列出我的待接受邀请

**GET `/api/v1/me/invitations`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| include_terminal | query | boolean | 否 | 是否包含已处理 / 已过期等终止态行（默认 false） |

响应：200：OK。

### 28.2 获取我的待处理邀请数

**GET `/api/v1/me/invitations/pending-count`**

响应：200：OK。

### 28.3 接受邀请

**POST `/api/v1/me/invitations/{inv_id}/accept`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| inv_id | path | string | 是 | 邀请 ID |

响应：200：OK。

### 28.4 拒绝邀请

**POST `/api/v1/me/invitations/{inv_id}/decline`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| inv_id | path | string | 是 | 邀请 ID |

响应：200：OK。


## 29. 审计日志

### 29.1 获取平台审计日志

**GET `/api/v1/system/admin/audit-log`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| after_id | query | integer | 否 | 游标：返回 id 小于此值的记录（默认从最新开始） |
| limit | query | integer | 否 | 页大小，1-100，默认 50 |
| action | query | string | 否 | 按 action 精确过滤（如 system.setting_changed） |
| outcome | query | string | 否 | 按 outcome 精确过滤（success / denied） |
| actor | query | string | 否 | 按 actor_user_id 精确过滤 |

响应：200：OK。

### 29.2 获取空间审计日志

**GET `/api/v1/tenants/{id}/audit-log`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间ID |
| after_id | query | integer | 否 | 游标：返回 id 小于此值的记录（默认从最新开始） |
| limit | query | integer | 否 | 页大小，1-100，默认 50 |
| action | query | string | 否 | 按 action 精确过滤（如 rbac.member_added / rbac.access_denied） |
| outcome | query | string | 否 | 按 outcome 精确过滤（success / denied） |
| actor | query | string | 否 | 按 actor_user_id 精确过滤 |

响应：200：OK。


## 30. 空间邀请

### 30.1 列出空间邀请

**GET `/api/v1/tenants/{id}/invitations`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| include_terminal | query | boolean | 否 | 是否包含终止态行（默认 false） |
| page | query | integer | 否 | 页码（从 1 起） |
| page_size | query | integer | 否 | 每页数量 |

响应：200：OK。

### 30.2 发出空间邀请

**POST `/api/v1/tenants/{id}/invitations`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| email | body | string | 是 |  |
| message | body | string | 否 |  |
| role | body | object | 是 |  |

响应：201：Created。

### 30.3 撤销待接受邀请

**DELETE `/api/v1/tenants/{id}/invitations/{inv_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| inv_id | path | string | 是 | 邀请 ID |

响应：200：OK。

### 30.4 生成共享邀请链接

**POST `/api/v1/tenants/{id}/invite-links`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| message | body | string | 否 |  |
| role | body | object | 是 |  |

响应：201：Created。


## 31. 空间成员

### 31.1 退出当前空间

**POST `/api/v1/tenants/{id}/leave`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |

响应：200：OK。

### 31.2 列出空间成员

**GET `/api/v1/tenants/{id}/members`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| q | query | string | 否 | 按邮箱/用户名模糊筛选 |
| page | query | integer | 否 | 页码（从 1 起） |
| page_size | query | integer | 否 | 每页数量（最大 100） |

响应：200：OK。

### 31.3 直接添加空间成员（直加路径）

**POST `/api/v1/tenants/{id}/members`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| email | body | string | 是 |  |
| role | body | object | 是 |  |

响应：201：Created。

### 31.4 修改空间成员角色

**PUT `/api/v1/tenants/{id}/members/{user_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| user_id | path | string | 是 | 用户 ID |
| role | body | object | 是 |  |

响应：200：OK。

### 31.5 移除空间成员

**DELETE `/api/v1/tenants/{id}/members/{user_id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | path | string | 是 | 空间 ID |
| user_id | path | string | 是 | 用户 ID |

响应：200：OK。


## 32. 用户管理

### 32.1 List my favorites

**GET `/api/v1/user/favorites`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| type | query | string | 是 | Resource type (kb / agent) |

响应：200：OK。

### 32.2 Star a resource

**POST `/api/v1/user/favorites`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| id | body | string | 否 |  |
| type | body | string | 否 |  |

响应：200：OK。

### 32.3 Unstar a resource

**DELETE `/api/v1/user/favorites/{type}/{id}`**

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| type | path | string | 是 | Resource type |
| id | path | string | 是 | Resource id |

响应：200：OK。
