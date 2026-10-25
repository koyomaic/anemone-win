# claude-code-openai-proxy

让 **Claude Code** 使用任意 **OpenAI 兼容** 的大模型（例如 opencode 的默认模型）。

Claude Code 只会说 Anthropic Messages API，而很多自建 / 第三方模型端点（vLLM、new-api、one-api 等）只提供 OpenAI Chat Completions API。本项目是一个**零依赖**的本地转换代理，在两种协议之间做双向翻译，让 `claude` 命令直接用上这些模型。

> 一个 Node 文件，无需 `npm install`。支持流式、工具调用（function calling）、多轮 `tool_result` 回传。

---

## 工作原理

```
┌────────────┐   Anthropic Messages API    ┌───────────────┐   OpenAI Chat Completions   ┌──────────────┐
│ Claude Code │ ──────────────────────────> │  本代理 (:8790) │ ──────────────────────────> │  模型端点      │
│  (claude)   │ <────────────────────────── │  协议双向翻译    │ <────────────────────────── │ vLLM/new-api │
└────────────┘        Anthropic 格式         └───────────────┘        OpenAI 格式           └──────────────┘
```

代理负责：

| 方向 | 转换内容 |
| --- | --- |
| 请求 Anthropic → OpenAI | `system`（含 SDK 注入的多条 system，统一合并到最前）、`messages`、`tools` / `input_schema` → `functions`、`tool_use` → `tool_calls`、`tool_result` → `role:tool`、`stop_sequences`、`temperature` 等 |
| 响应 OpenAI → Anthropic | `choices[].message` → `content[]`（`text` / `tool_use`）、`finish_reason` → `stop_reason`、`usage` 字段映射 |
| 流式 SSE | OpenAI `chat.completion.chunk` → Anthropic `message_start` / `content_block_start` / `content_block_delta`（`text_delta`、`input_json_delta`）/ `message_delta` / `message_stop` |

---

## 环境要求

- Node.js ≥ 18（使用内置 `fetch`，无需任何第三方包）
- 一个 OpenAI 兼容的模型端点（`/v1/chat/completions`），且支持 function calling
- Linux + systemd（用于开机自启；也可手动前台运行）

---

## 快速开始

### 1. 配置

```bash
cp config.example.json config.json
chmod 600 config.json      # 里面有密钥，务必收紧权限
```

编辑 `config.json`：

```json
{
  "host": "127.0.0.1",
  "port": 8790,
  "baseURL": "http://your-endpoint/v1",
  "apiKey": "sk-xxxxxxxx",
  "model": "your-model-name"
}
```

也可以用环境变量覆盖（优先级：**环境变量 > config.json > 默认值**）：

| 环境变量 | 对应字段 | 默认 |
| --- | --- | --- |
| `PROXY_CONFIG` | 配置文件路径 | `./config.json` |
| `PROXY_HOST` | `host` | `127.0.0.1` |
| `PROXY_PORT` | `port` | `8790` |
| `UPSTREAM_BASE_URL` | `baseURL` | —（必填） |
| `UPSTREAM_API_KEY` | `apiKey` | —（必填） |
| `UPSTREAM_MODEL` | `model` | —（必填） |
| `PROXY_DEBUG` | 打印每次请求的 role 序列并 dump **脱敏后** body 到 `/tmp/claude_proxy_last_<port>.json` | 关闭 |
| `PROXY_PREFIX_LOG` | 每次流式请求打一行 `prefix {...}`：前缀 tag（`sha256(system+tools)` 前 12 位）、`sysChars`/`tools`/`tailChars`、`inTok`/`outTok`、`ms`。用于量后端**自动前缀缓存**的真实命中率——同一 tag 下 `ms` 呈双峰即网关轮询到多副本、只有部分副本缓存了该前缀；tag 频繁变化则有可变内容混进了前缀。默认关闭（关闭时连 tag 都不计算，零开销） | 关闭 |

`config.json` 还支持按请求模型名分流：

```json
{
  "routes": {
    "qwen3.8-flash": {
      "baseURL": "http://127.0.0.1:8792/v1",
      "apiKey": "redaction-proxy-local",
      "model": "qwen3.8-flash"
    }
  }
}
```

路由条目可加 `"mode": "anthropic"`：上游**原生说 Anthropic Messages 协议**时走
直通转发（`{baseURL}/messages`，`x-api-key` 认证），代理只对
`text_delta`/`input_json_delta`/`thinking_delta` 做就地脱敏还原，事件与
`usage`（含 `cache_read_input_tokens`）原样透传，`content_block_stop` 即协议级
刷新点。相比默认的 OpenAI 翻译路径：①`cache_control` 断点得以保留，**提示缓存
可命中**（脱敏是确定性的，前缀逐字节稳定）；②不做块重构，翻译层的切碎类缺陷
（2026-10-23 两起事故）结构性消失；③thinking 流不再被丢弃。默认（不写 mode）
仍走 OpenAI 翻译，兼容 vLLM/new-api 等只说 OpenAI 协议的上游。

```json
{
  "routes": {
    "qwen3.8-flash": {
      "baseURL": "https://your-anthropic-native-gateway/v1",
      "apiKey": "sk-...",
      "model": "qwen3.8-flash",
      "mode": "anthropic"
    }
  }
}
```

> 注意：`routes` 只认 **object-map** 格式；数组形式（`[{"match":[...]}]`）是
> 无效配置，会被静默忽略、全部走默认上游（healthz 的 `routes` 字段显示 `"0"`
> 即此症状）。

### 2. 运行

前台试跑：

```bash
node proxy.mjs
```

或用脚本装成 systemd 服务（开机自启、崩溃自动重启）：

```bash
sudo bash install.sh
```

### 3. 让 Claude Code 指向代理

编辑 `~/.claude/settings.json`，加入 `env`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8790",
    "ANTHROPIC_AUTH_TOKEN": "dummy-proxy-ignores-it",
    "ANTHROPIC_MODEL": "your-model-name",
    "ANTHROPIC_SMALL_FAST_MODEL": "your-model-name",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144"
  }
}
```

说明：
- 代理**忽略**客户端传来的鉴权头，统一用 `config.json` 里的 `apiKey` 访问上游，所以 `ANTHROPIC_AUTH_TOKEN` 填任意非空值即可（用于避免 Claude Code 走 OAuth 登录）。
- 代理会把请求里的模型名**强制改写**为 `config.json` 的 `model`，因此 `ANTHROPIC_MODEL` 主要影响界面显示与 token 统计。
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` 设为模型真实上下文长度，可消除 “unrecognized model / 200k 假设” 的告警并让自动压缩更准确。

### 4. 验证

```bash
claude -p "Reply with exactly one word: pong"
```

---

## 手动 systemd（不用 install.sh）

```bash
sudo sed -e "s#__DIR__#$(pwd)#g" -e "s#__NODE__#$(command -v node)#g" \
  claude-proxy.service.example > /etc/systemd/system/claude-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now claude-proxy
```

管理命令：

```bash
systemctl status  claude-proxy      # 状态
systemctl restart claude-proxy      # 重启
journalctl -u claude-proxy -f       # 实时日志（或 tail -f proxy.log）
```

---

## 直接测试代理（不经过 Claude Code）

非流式：

```bash
curl -s http://127.0.0.1:8790/v1/messages \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"x","max_tokens":64,"messages":[{"role":"user","content":"say pong"}]}'
```

流式：

```bash
curl -sN http://127.0.0.1:8790/v1/messages \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"x","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"count to 3"}]}'
```

工具调用：

```bash
curl -s http://127.0.0.1:8790/v1/messages \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"x","max_tokens":256,
       "tools":[{"name":"calculator","description":"multiply","input_schema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}}],
       "messages":[{"role":"user","content":"Compute 23*7 with the calculator tool."}]}'
```

代理还实现了 `POST /v1/messages/count_tokens`（按字符数粗估）和 `GET /v1/models`（返回配置的模型），以满足 Claude Code 的辅助调用。

---

## Strict 脱敏出口（外部先进模型）

外部先进模型必须走独立的 strict 脱敏代理（本机约定端口 `8792`），本地模型不走脱敏。

```text
Claude CLI / opencode
  → 127.0.0.1:8790（本地模型/路由）
    → 外部模型路由改指 127.0.0.1:8792
      → 出站：公司/人员/联系方式/内部基础设施/凭据 → 占位符或不可逆掩码
      → 外部先进模型
      ← 入站：占位符 → 真实值
      ← 响应 DLP：模型猜出词典真实实体时拦截/掩码
```

- **只支持 `strict` 模式**：出站脱敏、响应还原、tool args 还原、响应 DLP、附件不出网、fail-closed 全部强制开启。
- `redaction.dictionary` 指向本机受控词典（建议 `shared/secrets/redaction/entities.json`，权限 `600`），词典**不进 Git**。
- 词典缺失、加载失败或脱敏异常时返回 `503`，绝不调用外部模型。
- `PROXY_DEBUG` 只能 dump 脱敏后的上游请求。
- 日志只记录命中类型/数量、模型、耗时，不记录明文实体。
- 健康检查：`GET /healthz`，strict 代理要求 `ok=true` 且 `redaction.mode="strict"`、`redaction.ready=true`。

外部代理配置示例见 `external.config.example.json`；systemd 模板见 `claude-proxy-external.service.example`。

OpenAI 兼容入口：

```bash
curl -s http://127.0.0.1:8792/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"external-advanced-model","messages":[{"role":"user","content":"..."}]}'
```

测试：

```bash
node --test test/*.test.mjs
```

---

## 已知限制

- **思维链（reasoning）**：部分模型（如 Qwen 系）会把推理放在独立的 `reasoning` 字段。代理只转发正式回答 `content`，丢弃 `reasoning`，以避免与 Claude Code 的 extended-thinking（需要签名）冲突。
- **`unrecognized_model` 提示**：因为模型名不在 Claude Code 内置目录里，启动时会打印一行 `[claude-code:unrecognized_model]` 提示，属正常现象、不影响使用。
- **system 合并**：上游要求 system 只能在最前，故 SDK 在对话中途注入的 system 内容会被统一合并到开头的单条 system 消息。
- **图像**：`image` 内容块按 OpenAI `image_url`（base64 data URL 或 http URL）转发，具体是否生效取决于上游模型是否支持多模态。

---

## 安全

- `config.json`、`external.config.json` 含明文密钥，已在 `.gitignore` 中排除，**切勿提交**。
- 脱敏词典含敏感实体，只能放本机受控目录（如 `shared/secrets/redaction/entities.json`），权限 `600`，**切勿提交**。
- 建议 `chmod 600 config.json external.config.json`，并让代理只监听 `127.0.0.1`。
- 外部模型真钥匙只应存在于 `8792` 脱敏代理；Claude/opencode 任务环境使用本地占位钥匙。

---

## License

MIT
