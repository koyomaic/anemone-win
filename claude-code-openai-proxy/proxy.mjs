// Anthropic Messages API -> OpenAI Chat Completions translation proxy.
// Lets Claude Code talk to any OpenAI-compatible backend (e.g. opencode's default model).
// Optional strict redaction mode adds a reversible pseudonymization egress boundary.
// Zero dependencies. Node >= 18 (uses global fetch).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRedactor } from './redact.mjs';
import { createStreamProcessor } from './redact-stream.mjs';

const log = (...a) => console.log(new Date().toISOString(), ...a);

const STOP_MAP = {
  stop: 'end_turn',
  length: 'max_tokens',
  tool_calls: 'tool_use',
  function_call: 'tool_use',
  content_filter: 'end_turn',
};

export function resolveConfigPath(env = process.env, baseUrl = import.meta.url) {
  if (env.PROXY_CONFIG) return path.resolve(env.PROXY_CONFIG);
  return fileURLToPath(new URL('./config.json', baseUrl));
}

export function loadConfig(env = process.env, baseUrl = import.meta.url) {
  const configPath = resolveConfigPath(env, baseUrl);
  const fileConfig = fs.existsSync(configPath)
    ? JSON.parse(fs.readFileSync(configPath, 'utf8'))
    : {};
  const host = env.PROXY_HOST || fileConfig.host || '127.0.0.1';
  const port = Number(env.PROXY_PORT || fileConfig.port || 8790);
  const baseURL = env.UPSTREAM_BASE_URL || fileConfig.baseURL;
  const apiKey = env.UPSTREAM_API_KEY || fileConfig.apiKey;
  const model = env.UPSTREAM_MODEL || fileConfig.model;
  const routes = fileConfig.routes && typeof fileConfig.routes === 'object' ? fileConfig.routes : {};
  const redaction = fileConfig.redaction || null;
  if (!baseURL || !apiKey || !model) {
    throw new Error(
      'Missing config. Provide config.json (see config.example.json) or set ' +
        'UPSTREAM_BASE_URL, UPSTREAM_API_KEY, UPSTREAM_MODEL env vars.'
    );
  }
  return { configPath, host, port, baseURL, apiKey, model, routes, redaction, env };
}

// ---------- 前缀稳定性诊断（PROXY_PREFIX_LOG=1 时启用） ----------
//
// 为什么需要：后端（vLLM 经 APISIX）做**自动前缀缓存**，命中与否直接决定每轮
// prefill 是 ~1s 还是 ~3s。实测同一 12235-token 前缀连发 8 次，耗时
// 3.33/3.01/0.89/1.02/1.09/2.09/0.96/2.96s——**明显双峰，4 快 4 慢**，符合
// 「网关轮询到多个副本、只有部分副本的 KV 缓存里有这段前缀」。
// OpenAI 兼容协议没有 cache_control 字段，Anthropic 那套显式缓存标记在这里
// **无处可打**（这也是 cache_read_input_tokens 恒为 0 的原因：后端不按
// Anthropic 口径回报缓存账）。所以能做的只有两件事：把前缀做小、把前缀做稳，
// 然后**量出真实命中率**去跟网关侧谈会话亲和。这个 tag 就是量它的尺子。
//
// tag = sha256(system 消息 + tools 定义)——即跨轮跨任务应当字节不变的那一段。
// 任务描述在末尾的 user 消息里，不参与 tag，故同一台机器上所有任务的前缀 tag
// 应当相同；若日志里 tag 频繁变化，说明有可变内容混进了前缀，缓存必然打不中。
export function prefixTag(body) {
  const msgs = Array.isArray(body?.messages) ? body.messages : [];
  const sys = msgs.filter((m) => m && m.role === 'system');
  const tools = Array.isArray(body?.tools) ? body.tools : [];
  const sysText = sys.map((m) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content))).join('\n');
  const rest = msgs.filter((m) => m && m.role !== 'system');
  const h = crypto.createHash('sha256');
  h.update(sysText);
  h.update('\u0000');
  h.update(JSON.stringify(tools));
  return {
    tag: h.digest('hex').slice(0, 12),
    sysChars: sysText.length,
    tools: tools.length,
    // 尾部可变部分体量：前缀命中后，真正要现算的就是这一段
    tailChars: rest.reduce((n, m) => n + (typeof m.content === 'string' ? m.content.length : JSON.stringify(m.content || '').length), 0),
  };
}

// ---------- Anthropic request -> OpenAI request ----------
export function toOpenAI(body, routeModel) {
  const out = { model: routeModel, stream: !!body.stream };
  // 流式必须显式索要 usage 尾包，否则上游不回 prompt_tokens/completion_tokens，
  // 下游（claude-code）拿到的 input_tokens 恒为 0，上下文预算闸门全瞎。
  if (out.stream) out.stream_options = { include_usage: true };
  out.max_tokens = body.max_tokens ?? 1024;
  if (body.temperature != null) out.temperature = body.temperature;
  if (body.top_p != null) out.top_p = body.top_p;
  if (Array.isArray(body.stop_sequences) && body.stop_sequences.length)
    out.stop = body.stop_sequences;

  const messages = [];
  const systemTexts = [];

  const blocksToText = (content) => {
    if (typeof content === 'string') return content;
    if (Array.isArray(content))
      return content
        .map((b) => (b && typeof b.text === 'string' ? b.text : ''))
        .join('\n');
    return content == null ? '' : JSON.stringify(content);
  };

  if (body.system) {
    const text = blocksToText(body.system);
    if (text.trim()) systemTexts.push(text);
  }

  for (const m of body.messages || []) {
    const role = m.role;

    // Claude Code's SDK injects extra system messages mid-conversation, but the
    // upstream requires all system content at the beginning -> merge it up front.
    if (role === 'system') {
      const text = blocksToText(m.content);
      if (text.trim()) systemTexts.push(text);
      continue;
    }

    if (typeof m.content === 'string') {
      messages.push({ role, content: m.content });
      continue;
    }

    const blocks = Array.isArray(m.content) ? m.content : [];

    if (role === 'assistant') {
      let text = '';
      const tool_calls = [];
      for (const b of blocks) {
        if (b.type === 'text') text += b.text ?? '';
        else if (b.type === 'tool_use')
          tool_calls.push({
            id: b.id,
            type: 'function',
            function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
          });
      }
      const msg = { role: 'assistant', content: text || null };
      if (tool_calls.length) msg.tool_calls = tool_calls;
      messages.push(msg);
    } else if (role === 'user') {
      let text = '';
      const parts = [];
      for (const b of blocks) {
        if (b.type === 'text') {
          text += b.text ?? '';
        } else if (b.type === 'tool_result') {
          let c = b.content;
          if (Array.isArray(c))
            c = c
              .map((x) =>
                x.type === 'text' ? x.text : x.type === 'image' ? '[image]' : ''
              )
              .join('');
          messages.push({
            role: 'tool',
            tool_call_id: b.tool_use_id,
            content: typeof c === 'string' ? c : JSON.stringify(c ?? ''),
          });
        } else if (b.type === 'image') {
          const src = b.source || {};
          if (src.type === 'base64')
            parts.push({
              type: 'image_url',
              image_url: { url: `data:${src.media_type};base64,${src.data}` },
            });
          else if (src.type === 'url')
            parts.push({ type: 'image_url', image_url: { url: src.url } });
        }
      }
      if (parts.length) {
        if (text) parts.unshift({ type: 'text', text });
        messages.push({ role: 'user', content: parts });
      } else if (text) {
        messages.push({ role: 'user', content: text });
      }
    } else {
      messages.push({
        role,
        content:
          typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
      });
    }
  }

  if (systemTexts.length)
    messages.unshift({ role: 'system', content: systemTexts.join('\n\n') });

  out.messages = messages;

  if (Array.isArray(body.tools) && body.tools.length) {
    out.tools = body.tools.map((t) => ({
      type: 'function',
      function: {
        name: t.name,
        description: t.description || '',
        parameters: t.input_schema || { type: 'object', properties: {} },
      },
    }));
    const tc = body.tool_choice;
    if (tc) {
      if (tc.type === 'auto') out.tool_choice = 'auto';
      else if (tc.type === 'any') out.tool_choice = 'required';
      else if (tc.type === 'none') out.tool_choice = 'none';
      else if (tc.type === 'tool')
        out.tool_choice = { type: 'function', function: { name: tc.name } };
    }
  }

  return out;
}

// ---------- OpenAI response -> Anthropic response (non-streaming) ----------
export function fromOpenAI(json, reqModel, fallbackModel) {
  const choice = json.choices?.[0] || {};
  const msg = choice.message || {};
  const content = [];
  if (msg.content) content.push({ type: 'text', text: msg.content });
  for (const tc of msg.tool_calls || []) {
    let input = {};
    try {
      input = JSON.parse(tc.function?.arguments || '{}');
    } catch {
      // 兜底信封不能静默：__raw 派发出去必然吃 InputValidationError，日志里
      // 必须留下"上游给了非法参数 JSON"的真相（长度+finish_reason 供区分
      // 截断还是乱码），否则又变成"看起来像模型错"的哑巴故障。
      input = { __raw: tc.function?.arguments ?? '' };
      log('warn: unparseable tool arguments wrapped as __raw',
          JSON.stringify({ id: tc.id, name: tc.function?.name,
                           bytes: (tc.function?.arguments || '').length,
                           finish: choice.finish_reason }));
    }
    content.push({
      type: 'tool_use',
      id: tc.id,
      name: tc.function?.name ?? '',
      input,
    });
  }
  if (!content.length) content.push({ type: 'text', text: '' });
  return {
    id: 'msg_' + (json.id || Math.random().toString(36).slice(2)),
    type: 'message',
    role: 'assistant',
    model: reqModel || fallbackModel,
    content,
    stop_reason: STOP_MAP[choice.finish_reason] || 'end_turn',
    stop_sequence: null,
    usage: {
      input_tokens: json.usage?.prompt_tokens ?? 0,
      output_tokens: json.usage?.completion_tokens ?? 0,
    },
  };
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

function sendJson(res, status, obj) {
  if (!res.headersSent) res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(obj));
}

// 数组形态 routes（历史漂移：[{match:[...], baseURL, apiKey, mode}]）曾被**静默
// 忽略**——2026-09-22 写入的 aigateway 直通路由因此死了一个多月（healthz 的
// routes 显示 "0" 即症状）。现归一化激活：按 match 列表展开成 object-map（后
// 出现的条目覆盖同名模型）；每机私有 key 留在每机 config 里，代码不携带钥匙。
// object-map（README 正典格式）原样返回。
export function normalizeRoutes(routes) {
  if (!routes || typeof routes !== 'object') return {};
  if (!Array.isArray(routes)) return routes;
  const out = {};
  for (const entry of routes) {
    if (!entry || typeof entry !== 'object') continue;
    const { match, ...rest } = entry;
    for (const m of Array.isArray(match) ? match : []) {
      if (typeof m === 'string' && m) out[m] = { ...rest };
    }
  }
  log('routes normalized from array format →', Object.keys(out).join(',') || '(empty)');
  return out;
}

export function createProxy(config, deps = {}) {
  const env = deps.env || process.env;
  const fetchImpl = deps.fetch || fetch;
  config = { ...config, routes: normalizeRoutes(config.routes) };
  const redactor = config.redaction ? createRedactor(config.redaction) : null;
  if (redactor && !redactor.ready) {
    log('redaction enabled but dictionary unavailable; proxy will fail closed:', redactor.error);
  }

  function resolveRoute(requested) {
    const key = requested && Object.prototype.hasOwnProperty.call(config.routes, requested)
      ? requested
      : '';
    const r = key ? config.routes[key] || {} : {};
    const base = r.baseURL || config.baseURL;
    // mode:'anthropic' ＝上游原生说 Anthropic Messages 协议 → 直通转发（只做
    // 就地脱敏/还原，不拆包重装）。2026-10-25 实测 aigateway.chambroad.com 原生
    // Anthropic＋提示缓存可用（cache_read=33732 命中）；翻译层是当天两起切碎
    // 事故的所在，直通后该 bug 类结构性消失。默认仍走 openai 翻译（30131 的
    // /v1/messages 是半成品：Bearer 打过去回 OpenAI 格式，不可作直通目标）。
    const mode = r.mode === 'anthropic' ? 'anthropic' : 'openai';
    return {
      name: key || 'default',
      url: String(base).replace(/\/$/, '')
        + (mode === 'anthropic' ? '/messages' : '/chat/completions'),
      key: r.apiKey || config.apiKey,
      model: r.model || requested || config.model,
      mode,
    };
  }

  function debugDump(body) {
    if (!env.PROXY_DEBUG) return;
    const file = `/tmp/claude_proxy_last_${config.port}.json`;
    fs.writeFileSync(file, JSON.stringify(body, null, 2));
    log('DEBUG roles:', (body.messages || []).map((m) => m.role).join(','));
  }

  function logRedaction(route, state, started) {
    if (!redactor || !state) return;
    log('redact', JSON.stringify({
      route: route.name,
      model: route.model,
      ms: Number((Date.now() - started).toFixed(2)),
      hits: state.hits,
      credentials: state.credentials,
    }));
  }

  function logDlp(state) {
    if (!redactor || !state || !state.dlp) return;
    log('response_dlp', JSON.stringify({ dlp: state.dlp, hits: state.hits }));
  }

  function failClosed(res, kind, message) {
    log('fail-closed:', message);
    if (kind === 'openai') {
      return sendJson(res, 503, {
        error: { message: 'redaction proxy unavailable', type: 'service_unavailable', code: 'redaction_fail_closed' },
      });
    }
    return sendJson(res, 503, {
      type: 'error',
      error: { type: 'api_error', message: 'redaction proxy unavailable' },
    });
  }

  function ensureRedaction(res, kind) {
    if (!redactor) return true;
    if (redactor.ready) return true;
    failClosed(res, kind, redactor.error || 'dictionary unavailable');
    return false;
  }

  function redactRequest(body, kind, res) {
    if (!redactor) return { body, map: null, state: null };
    const started = Date.now();
    try {
      const out = redactor.redactOpenAiBody(body);
      logRedaction({ name: kind, model: body.model }, out.state, started);
      return out;
    } catch (e) {
      failClosed(res, kind, e.message);
      return null;
    }
  }

  async function upstreamFetch(route, payload) {
    return fetchImpl(route.url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: 'Bearer ' + route.key,
      },
      body: JSON.stringify(payload),
    });
  }

  function processNonStreamResponse(json, map) {
    if (!redactor) return json;
    const out = redactor.processOpenAiResponse(json, map);
    logDlp(out.state);
    return out.json;
  }

  async function streamTranslate(upstream, res, reqModel, route, map, ptag) {
    const send = (event, data) =>
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

    const msgId = 'msg_' + Math.random().toString(36).slice(2);
    // 本次流式往返的起点，仅供 prefix 诊断日志算 ms（外层 redactRequest 里那个
    // 同名 started 是它自己的局部变量，这里取不到）。
    const streamStartedAt = Date.now();
    let nextIndex = 0;
    let openIndex = -1;
    let textIndex = -1;
    let inputTokens = 0;
    let outputTokens = 0;
    let finish = null;
    const tools = new Map();
    const textProc = redactor ? createStreamProcessor(redactor, map) : null;
    const toolProcs = new Map();
    // 已关闭的块索引。Anthropic 协议禁止对同一 index 二次 content_block_start，
    // 重开会被 claude-code 当成**新的 tool_use（同 id）**——2026-10-23 事故根因：
    // 脱敏 StreamProcessor 对每段文本/工具参数保留末尾 hold（当时 27）字符等待
    // 跨块匹配，而旧实现只在流结束的 finally 里统一 flush；flush 时块早已关闭，
    // sendText/sendToolArgs 便重开已关闭块补发尾巴 → 每个逻辑调用被切成
    // 「头部（截断 JSON）＋尾巴（27B 碎片）」两个 tool_use，双双 InputValidationError。
    // 10-23/24 两天 3,381 次逻辑调用被拒 1,898 次（56%），并发调用 97-100% 被切、
    // 单调用带前置文本 99.8% 被切，两天烧掉 ~2 亿 input token（缓存命中恒 0）。
    const closedBlocks = new Set();

    const openBlock = (index, block) => {
      send('content_block_start', {
        type: 'content_block_start',
        index,
        content_block: block,
      });
      openIndex = index;
    };
    // 把当前打开块的脱敏保留区刷进**该块自身**（关块前必须做，否则尾巴丢给
    // 流末尾，见上）。flush 后 buf 清空，重复调用安全。
    const flushOpenBlock = () => {
      if (openIndex < 0) return;
      if (openIndex === textIndex && textProc) {
        const t = textProc.flush();
        if (t) send('content_block_delta', {
          type: 'content_block_delta',
          index: textIndex,
          delta: { type: 'text_delta', text: t },
        });
        return;
      }
      for (const [oi, proc] of toolProcs) {
        const st = tools.get(oi);
        if (st && st.ai === openIndex) {
          const t = proc.flush();
          if (t) send('content_block_delta', {
            type: 'content_block_delta',
            index: st.ai,
            delta: { type: 'input_json_delta', partial_json: t },
          });
          return;
        }
      }
    };
    const closeBlock = () => {
      if (openIndex >= 0) {
        flushOpenBlock();
        send('content_block_stop', { type: 'content_block_stop', index: openIndex });
        closedBlocks.add(openIndex);
        openIndex = -1;
      }
    };
    const sendText = (text) => {
      if (!text) return;
      if (textIndex < 0) textIndex = nextIndex++;
      if (openIndex !== textIndex) {
        // 文本块已关闭后又来文本（上游交错，罕见）：另开**新 index** 的文本块续写。
        // 文本块可多段拼接，新开合法；重开旧 index 非法。
        if (closedBlocks.has(textIndex)) textIndex = nextIndex++;
        closeBlock();
        openBlock(textIndex, { type: 'text', text: '' });
      }
      send('content_block_delta', {
        type: 'content_block_delta',
        index: textIndex,
        delta: { type: 'text_delta', text },
      });
    };
    const sendToolArgs = (st, args) => {
      if (!args) return;
      if (openIndex !== st.ai) {
        if (closedBlocks.has(st.ai)) {
          // 工具块关闭后又收到同 index 的参数＝上游真交错（现有网关从未出现）。
          // 无处合法续写：记真相并丢弃，绝不重开块（重开＝复制 10-23 切碎事故）。
          log(`warn: tool args after block close dropped (id=${st.id} ${args.length}B)`);
          return;
        }
        closeBlock();
        openBlock(st.ai, { type: 'tool_use', id: st.id, name: st.name, input: {} });
      }
      send('content_block_delta', {
        type: 'content_block_delta',
        index: st.ai,
        delta: { type: 'input_json_delta', partial_json: args },
      });
    };

    send('message_start', {
      type: 'message_start',
      message: {
        id: msgId,
        type: 'message',
        role: 'assistant',
        model: reqModel || route.model,
        content: [],
        stop_reason: null,
        stop_sequence: null,
        usage: { input_tokens: 0, output_tokens: 0 },
      },
    });

    function processChunk(json) {
      if (json.usage) {
        inputTokens = json.usage.prompt_tokens ?? inputTokens;
        outputTokens = json.usage.completion_tokens ?? outputTokens;
      }
      const choice = json.choices?.[0];
      if (!choice) return;
      const delta = choice.delta || {};

      if (typeof delta.content === 'string' && delta.content.length) {
        sendText(textProc ? textProc.push(delta.content) : delta.content);
      }

      for (const tcd of delta.tool_calls || []) {
        const oi = tcd.index ?? 0;
        let st = tools.get(oi);
        if (!st) {
          closeBlock();
          st = {
            ai: nextIndex++,
            id: tcd.id || 'toolu_' + Math.random().toString(36).slice(2),
            name: tcd.function?.name || '',
          };
          tools.set(oi, st);
          openBlock(st.ai, { type: 'tool_use', id: st.id, name: st.name, input: {} });
        }
        // 既有 index 的块切换交给 sendToolArgs（带 closedBlocks 守卫）；此处不得
        // 重开块——旧实现的 else-if 重开正是切碎事故的帮凶之一。
        if (tcd.id && !st.id) st.id = tcd.id;
        if (tcd.function?.name && !st.name) st.name = tcd.function.name;
        const args = tcd.function?.arguments;
        if (args) {
          let proc = toolProcs.get(oi);
          if (!proc && redactor) {
            proc = createStreamProcessor(redactor, map);
            toolProcs.set(oi, proc);
          }
          sendToolArgs(st, proc ? proc.push(args) : args);
        }
      }

      if (choice.finish_reason) finish = STOP_MAP[choice.finish_reason] || finish;
    }

    try {
      const reader = upstream.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith('data:')) continue;
          const data = line.slice(5).trim();
          if (!data || data === '[DONE]') continue;
          let json;
          try {
            json = JSON.parse(data);
          } catch {
            continue;
          }
          processChunk(json);
        }
      }
    } catch (e) {
      log('stream error:', e.message);
    } finally {
      // closeBlock 已内联 flushOpenBlock：当前打开块的脱敏保留区先刷进该块再关。
      // 其余块在切换关闭时已各自刷过——不存在「流末统一 flush 时块已关闭、只能
      // 重开补发」的旧路径（那正是 10-23 切碎事故的根因）。
      closeBlock();
      // 兜底：整段文本从未超出保留窗口（≤hold 字符的短文本）时文本块从未打开，
      // 保留区里的全文必须新开一块发出，否则静默丢字。
      if (textProc) {
        const t = textProc.flush();
        if (t) {
          if (textIndex < 0 || closedBlocks.has(textIndex)) textIndex = nextIndex++;
          send('content_block_start', {
            type: 'content_block_start',
            index: textIndex,
            content_block: { type: 'text', text: '' },
          });
          send('content_block_delta', {
            type: 'content_block_delta',
            index: textIndex,
            delta: { type: 'text_delta', text: t },
          });
          send('content_block_stop', { type: 'content_block_stop', index: textIndex });
          closedBlocks.add(textIndex);
        }
      }
      // 兜底：工具块必先于其 proc 打开，正常不会有残留；有残留说明走了未知路径，
      // 记日志留痕（真相回传优先于静默吞掉）。
      for (const [oi, proc] of toolProcs) {
        const rem = proc.flush();
        if (rem) log(`warn: orphan tool holdback dropped (oi=${oi} ${rem.length}B)`);
      }
      if (textProc) logDlp(textProc.state);
      for (const proc of toolProcs.values()) logDlp(proc.state);
      // input_tokens 必须在这里回传：message_start 发出时上游 usage 尾包还没到，
      // 只能先填 0；真实值随最后一个 chunk 到达，故在 message_delta 补齐。
      // 少了它 claude-code 记到的 input_tokens 恒为 0（实测 106 个回合全 0），
      // relay 的 tokens 字段退化成纯输出，avatar 的 resume 上下文预算闸门失效。
      send('message_delta', {
        type: 'message_delta',
        delta: { stop_reason: finish || 'end_turn', stop_sequence: null },
        usage: { input_tokens: inputTokens, output_tokens: outputTokens },
      });
      send('message_stop', { type: 'message_stop' });
      // 前缀稳定性/缓存命中诊断（PROXY_PREFIX_LOG=1 才打，默认零开销）。
      // 同一 tag 下 ms 呈双峰＝后端多副本、只有部分副本缓存了这段前缀；
      // tag 频繁变化＝有可变内容混进了前缀，缓存必然打不中。
      if (env.PROXY_PREFIX_LOG && ptag) {
        log('prefix', JSON.stringify({
          tag: ptag.tag, sysChars: ptag.sysChars, tools: ptag.tools,
          tailChars: ptag.tailChars, inTok: inputTokens, outTok: outputTokens,
          ms: Number((Date.now() - streamStartedAt).toFixed(1)),
        }));
      }
      res.end();
    }
  }

  // ── Anthropic 直通（mode:'anthropic' 路由）──
  // 事件原样转发，只对 text_delta / input_json_delta / thinking_delta 的文本做
  // 就地脱敏还原；每个内容块一个 StreamProcessor，**content_block_stop 就是协议
  // 规定的刷新点**——不再需要翻译层那套"猜块何时结束"的收尾逻辑（2026-10-23
  // 切碎事故的根源即在于此）。usage/cache 字段原生透传，claude-code 与 relay
  // 记账直接可见缓存命中。
  const PASSTHROUGH_DELTA_FIELDS = {
    text_delta: 'text',
    input_json_delta: 'partial_json',
    thinking_delta: 'thinking',
  };
  const PASSTHROUGH_KIND_DELTA = {
    text: 'text_delta',
    tool_use: 'input_json_delta',
    thinking: 'thinking_delta',
  };

  async function streamPassthrough(upstream, res, route, map) {
    const startedAt = Date.now();
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    const forward = (data) =>
      res.write(`event: ${data.type}\ndata: ${JSON.stringify(data)}\n\n`);
    const blocks = new Map();          // index -> { kind, proc }
    const usage = { input_tokens: 0, output_tokens: 0,
                    cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };
    let stopped = false;

    const flushBlock = (index) => {
      const b = blocks.get(index);
      if (!b) return;
      if (b.proc) {
        const rem = b.proc.flush();
        const dt = PASSTHROUGH_KIND_DELTA[b.kind] || 'text_delta';
        if (rem) {
          forward({ type: 'content_block_delta', index,
                    delta: { type: dt, [PASSTHROUGH_DELTA_FIELDS[dt]]: rem } });
        }
        logDlp(b.proc.state);
      }
      blocks.delete(index);
    };

    function handleEvent(d) {
      switch (d.type) {
        case 'message_start': {
          const u = d.message?.usage || {};
          for (const k of Object.keys(usage)) {
            if (typeof u[k] === 'number') usage[k] = u[k];
          }
          forward(d);
          return;
        }
        case 'content_block_start':
          blocks.set(d.index, {
            kind: d.content_block?.type || 'text',
            proc: redactor ? createStreamProcessor(redactor, map) : null,
          });
          forward(d);
          return;
        case 'content_block_delta': {
          const b = blocks.get(d.index);
          const dt = d.delta?.type;
          const field = PASSTHROUGH_DELTA_FIELDS[dt];
          if (b && b.proc && field && typeof d.delta[field] === 'string'
              && d.delta[field].length) {
            const out = b.proc.push(d.delta[field]);
            if (out) { d.delta[field] = out; forward(d); }
            // 全被保留区扣下：本事件不转发，尾巴在 content_block_stop 前补发
          } else {
            forward(d);           // signature_delta 等原样过
          }
          return;
        }
        case 'content_block_stop':
          flushBlock(d.index);    // 协议级刷新点：先补尾巴，再转发 stop
          forward(d);
          return;
        case 'message_delta': {
          const u = d.usage || {};
          for (const k of Object.keys(usage)) {
            if (typeof u[k] === 'number') usage[k] = u[k];
          }
          forward(d);
          return;
        }
        case 'message_stop':
          stopped = true;
          forward(d);
          return;
        case 'error':
          log('passthrough upstream error event:', JSON.stringify(d.error || {}).slice(0, 300));
          forward(d);
          return;
        default:
          forward(d);             // ping 及未来新事件类型一律原样过
      }
    }

    try {
      const reader = upstream.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith('data:')) continue;   // event: 行丢弃，转发时按 data.type 重建
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let d;
          try {
            d = JSON.parse(payload);
          } catch {
            continue;
          }
          handleEvent(d);
        }
      }
    } catch (e) {
      log('passthrough stream error:', e.message);
    } finally {
      // 上游断流未发 message_stop：补齐收尾事件，避免 claude-code 挂等
      // （与翻译路径 finally 的兜底语义一致）。
      for (const index of [...blocks.keys()]) {
        flushBlock(index);
        forward({ type: 'content_block_stop', index });
      }
      if (!stopped) {
        forward({ type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null },
                  usage: { output_tokens: usage.output_tokens } });
        forward({ type: 'message_stop' });
      }
      log('passthrough', JSON.stringify({
        route: route.name, model: route.model,
        ms: Number((Date.now() - startedAt).toFixed(1)),
        inTok: usage.input_tokens, outTok: usage.output_tokens,
        cacheRead: usage.cache_read_input_tokens,
        cacheCreate: usage.cache_creation_input_tokens,
      }));
      res.end();
    }
  }

  async function streamOpenAi(upstream, res, map) {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    const procs = new Map();
    let last = null;
    let doneWritten = false;

    const getProc = (key) => {
      if (!redactor) return null;
      if (!procs.has(key)) procs.set(key, createStreamProcessor(redactor, map));
      return procs.get(key);
    };
    const write = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
    const chunkFor = (index, delta) => ({
      id: last?.id || `chatcmpl-${Math.random().toString(36).slice(2)}`,
      object: 'chat.completion.chunk',
      created: last?.created || Math.floor(Date.now() / 1000),
      model: last?.model || config.model,
      choices: [{ index: Number(index), delta, finish_reason: null }],
    });
    const flushAll = () => {
      if (!redactor) return;
      for (const [key, proc] of procs) {
        const rem = proc.flush();
        if (!rem) continue;
        const parts = key.split(':');
        if (parts[0] === 'tool') {
          write(chunkFor(parts[1], { tool_calls: [{ index: Number(parts[2]), function: { arguments: rem } }] }));
        } else {
          write(chunkFor(parts[1], { [parts[0]]: rem }));
        }
        logDlp(proc.state);
      }
    };
    // 单个 choice 的保留区尾巴。必须在转发 finish_reason 块**之前**刷：OpenAI
    // 客户端见到 finish_reason 即停止累积，等到 [DONE] 再 flushAll 就晚了，
    // 尾巴会被静默丢弃（参数截断成非法 JSON）。flush 幂等，flushAll 兜底不重复。
    const flushChoice = (ci) => {
      if (!redactor) return;
      for (const [key, proc] of procs) {
        const parts = key.split(':');
        if (parts[1] !== String(ci)) continue;
        const rem = proc.flush();
        if (!rem) continue;
        if (parts[0] === 'tool') {
          write(chunkFor(ci, { tool_calls: [{ index: Number(parts[2]), function: { arguments: rem } }] }));
        } else {
          write(chunkFor(ci, { [parts[0]]: rem }));
        }
        logDlp(proc.state);
      }
    };

    try {
      const reader = upstream.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          if (!line.startsWith('data:')) {
            res.write(line + '\n');
            continue;
          }
          const data = line.slice(5).trim();
          if (!data) continue;
          if (data === '[DONE]') {
            flushAll();
            res.write('data: [DONE]\n\n');
            doneWritten = true;
            continue;
          }
          if (!redactor) {
            res.write(`data: ${data}\n\n`);
            continue;
          }
          let json;
          try {
            json = JSON.parse(data);
          } catch {
            res.write(`data: ${data}\n\n`);
            continue;
          }
          last = json;
          for (const [ci, choice] of (json.choices || []).entries()) {
            const delta = choice?.delta || {};
            for (const field of ['content', 'reasoning', 'reasoning_content']) {
              if (typeof delta[field] === 'string' && delta[field].length) {
                const proc = getProc(`${field}:${ci}`);
                const out = proc.push(delta[field]);
                if (out) delta[field] = out;
                else delete delta[field];
              }
            }
            for (const [ti, tcd] of (delta.tool_calls || []).entries()) {
              const args = tcd?.function?.arguments;
              if (typeof args === 'string' && args) {
                const idx = tcd.index ?? ti;
                const proc = getProc(`tool:${ci}:${idx}`);
                const out = proc.push(args);
                if (out) tcd.function.arguments = out;
                else delete tcd.function.arguments;
              }
            }
            if (choice?.finish_reason) flushChoice(ci);
          }
          write(json);
        }
      }
      if (!doneWritten) flushAll();
    } catch (e) {
      log('openai stream error:', e.message);
      if (!doneWritten) flushAll();
    } finally {
      if (!doneWritten) res.write('data: [DONE]\n\n');
      res.end();
    }
  }

  async function handleMessages(req, res) {
    const raw = await readBody(req);
    let body;
    try {
      body = JSON.parse(raw || '{}');
    } catch {
      return sendJson(res, 400, {
        type: 'error',
        error: { type: 'invalid_request_error', message: 'invalid JSON' },
      });
    }
    if (!ensureRedaction(res, 'anthropic')) return;
    const route = resolveRoute(body.model);

    // ── 直通分支：上游原生 Anthropic 协议，不翻译 ──
    if (route.mode === 'anthropic') {
      let redactedA;
      const startedA = Date.now();
      try {
        redactedA = redactor
          ? redactor.redactAnthropicBody(body)
          : { body, map: null, state: null };
        if (redactedA.state) {
          logRedaction({ name: 'anthropic-passthrough', model: route.model },
                       redactedA.state, startedA);
        }
      } catch (e) {
        return failClosed(res, 'anthropic', e.message);
      }
      const ptBody = redactedA.body;
      ptBody.model = route.model;
      if (env.PROXY_DEBUG) {
        fs.writeFileSync(`/tmp/claude_proxy_last_${config.port}.json`,
                         JSON.stringify(ptBody, null, 2));
      }
      let upA;
      try {
        upA = await fetchImpl(route.url, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            'x-api-key': route.key,
            authorization: 'Bearer ' + route.key,
            'anthropic-version': req.headers['anthropic-version'] || '2023-06-01',
          },
          body: JSON.stringify(ptBody),
        });
      } catch (e) {
        return sendJson(res, 502, {
          type: 'error',
          error: { type: 'api_error', message: 'upstream fetch failed: ' + e.message },
        });
      }
      if (!upA.ok) {
        const t = await upA.text();
        log('upstream(anthropic)', upA.status, t.slice(0, 300));
        // 错误体本就是 Anthropic 格式，原样透传（保真优先，不包装不掩盖）
        res.writeHead(upA.status, { 'content-type': 'application/json' });
        return res.end(t);
      }
      if (ptBody.stream) {
        return await streamPassthrough(upA, res, route, redactedA.map);
      }
      const jA = await upA.json();
      if (!redactor) return sendJson(res, 200, jA);
      const processedA = redactor.processAnthropicResponse(jA, redactedA.map);
      logDlp(processedA.state);
      return sendJson(res, 200, processedA.json);
    }

    let openaiBody = toOpenAI(body, route.model);
    const redacted = redactRequest(openaiBody, 'anthropic', res);
    if (!redacted) return;
    openaiBody = redacted.body;
    debugDump(openaiBody);
    // 在脱敏**之后**取 tag：量的是真正上送给后端的那一段前缀
    const ptag = env.PROXY_PREFIX_LOG ? prefixTag(openaiBody) : null;

    let up;
    try {
      up = await upstreamFetch(route, openaiBody);
    } catch (e) {
      return sendJson(res, 502, {
        type: 'error',
        error: { type: 'api_error', message: 'upstream fetch failed: ' + e.message },
      });
    }
    if (!up.ok) {
      const t = await up.text();
      log('upstream', up.status, t.slice(0, 300));
      return sendJson(res, up.status, {
        type: 'error',
        error: { type: 'api_error', message: t.slice(0, 2000) },
      });
    }
    if (body.stream) {
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
      });
      return await streamTranslate(up, res, body.model, route, redacted.map, ptag);
    }
    let j;
    try {
      j = await up.json();
    } catch {
      // 200 但空体/非 JSON：典型症状是路由指到了不说 OpenAI 协议的端点
      // （如 aigateway 的 /chat/completions 废端点，实测回 200 空体）。
      // 真相回传优先：明确 502，绝不包装成空回复让 claude-code 打哑谜。
      log('upstream returned 200 with empty/non-JSON body; route misconfigured?');
      return sendJson(res, 502, {
        type: 'error',
        error: { type: 'api_error', message: 'upstream returned empty or non-JSON body (route/protocol misconfigured?)' },
      });
    }
    const processed = processNonStreamResponse(j, redacted.map);
    const anth = fromOpenAI(processed, body.model, route.model);
    return sendJson(res, 200, anth);
  }

  async function handleChatCompletions(req, res) {
    const raw = await readBody(req);
    let body;
    try {
      body = JSON.parse(raw || '{}');
    } catch {
      return sendJson(res, 400, {
        error: { message: 'invalid JSON', type: 'invalid_request_error' },
      });
    }
    if (!ensureRedaction(res, 'openai')) return;
    const route = resolveRoute(body.model);
    body.model = route.model;
    const redacted = redactRequest(body, 'openai', res);
    if (!redacted) return;
    body = redacted.body;
    body.model = route.model;
    debugDump(body);

    let up;
    try {
      up = await upstreamFetch(route, body);
    } catch (e) {
      return sendJson(res, 502, {
        error: { message: 'upstream fetch failed: ' + e.message, type: 'api_error' },
      });
    }
    if (!up.ok) {
      const t = await up.text();
      log('upstream', up.status, t.slice(0, 300));
      return sendJson(res, up.status, {
        error: { message: t.slice(0, 2000), type: 'api_error' },
      });
    }
    if (body.stream) return await streamOpenAi(up, res, redacted.map);
    const j = await up.json();
    return sendJson(res, 200, processNonStreamResponse(j, redacted.map));
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://local');
    const p = url.pathname;

    try {
      if (p === '/healthz' || p.endsWith('/healthz')) {
        const ok = !redactor || redactor.ready;
        return sendJson(res, ok ? 200 : 503, {
          ok,
          service: 'claude-code-openai-proxy',
          port: config.port,
          model: config.model,
          routes: Object.keys(config.routes),
          redaction: redactor ? redactor.health() : { enabled: false, mode: 'none' },
        });
      }

      if (req.method === 'POST' && p.endsWith('/chat/completions')) {
        return await handleChatCompletions(req, res);
      }

      if (req.method === 'POST' && p.endsWith('/messages')) {
        return await handleMessages(req, res);
      }

      if (req.method === 'POST' && p.endsWith('/count_tokens')) {
        const raw = await readBody(req);
        let n = 0;
        try {
          const b = JSON.parse(raw || '{}');
          n = Math.ceil(JSON.stringify(b.messages ?? b).length / 4);
        } catch {}
        return sendJson(res, 200, { input_tokens: n });
      }

      if (p.endsWith('/models')) {
        const ids = [...new Set([config.model, ...Object.keys(config.routes)])];
        return sendJson(res, 200, {
          data: ids.map((id) => ({
            type: 'model',
            id,
            display_name: id,
            created_at: new Date().toISOString(),
          })),
        });
      }

      return sendJson(res, 404, {
        type: 'error',
        error: { type: 'not_found_error', message: 'no route: ' + p },
      });
    } catch (e) {
      log('handler error:', e.stack || e.message);
      if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
      try {
        res.end(
          JSON.stringify({
            type: 'error',
            error: { type: 'api_error', message: e.message },
          })
        );
      } catch {}
    }
  });

  return { server, redactor, resolveRoute, config };
}

function main() {
  let config;
  try {
    config = loadConfig();
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
  const { server } = createProxy(config);
  server.listen(config.port, config.host, () =>
    log(`anthropic->openai proxy on http://${config.host}:${config.port} (${config.model})`)
  );
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) main();
