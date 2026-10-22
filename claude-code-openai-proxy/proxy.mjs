// Anthropic Messages API -> OpenAI Chat Completions translation proxy.
// Lets Claude Code talk to any OpenAI-compatible backend (e.g. opencode's default model).
// Optional strict redaction mode adds a reversible pseudonymization egress boundary.
// Zero dependencies. Node >= 18 (uses global fetch).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
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

// ---------- Anthropic request -> OpenAI request ----------
export function toOpenAI(body, routeModel) {
  const out = { model: routeModel, stream: !!body.stream };
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
      input = { __raw: tc.function?.arguments ?? '' };
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

export function createProxy(config, deps = {}) {
  const env = deps.env || process.env;
  const fetchImpl = deps.fetch || fetch;
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
    return {
      name: key || 'default',
      url: String(base).replace(/\/$/, '') + '/chat/completions',
      key: r.apiKey || config.apiKey,
      model: r.model || requested || config.model,
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

  async function streamTranslate(upstream, res, reqModel, route, map) {
    const send = (event, data) =>
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

    const msgId = 'msg_' + Math.random().toString(36).slice(2);
    let nextIndex = 0;
    let openIndex = -1;
    let textIndex = -1;
    let inputTokens = 0;
    let outputTokens = 0;
    let finish = null;
    const tools = new Map();
    const textProc = redactor ? createStreamProcessor(redactor, map) : null;
    const toolProcs = new Map();

    const openBlock = (index, block) => {
      send('content_block_start', {
        type: 'content_block_start',
        index,
        content_block: block,
      });
      openIndex = index;
    };
    const closeBlock = () => {
      if (openIndex >= 0) {
        send('content_block_stop', { type: 'content_block_stop', index: openIndex });
        openIndex = -1;
      }
    };
    const sendText = (text) => {
      if (!text) return;
      if (textIndex < 0) textIndex = nextIndex++;
      if (openIndex !== textIndex) {
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
        } else if (openIndex !== st.ai) {
          closeBlock();
          openBlock(st.ai, { type: 'tool_use', id: st.id, name: st.name, input: {} });
        }
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
      if (textProc) sendText(textProc.flush());
      for (const [oi, proc] of toolProcs) {
        const st = tools.get(oi);
        if (st) sendToolArgs(st, proc.flush());
      }
      if (textProc) logDlp(textProc.state);
      for (const proc of toolProcs.values()) logDlp(proc.state);
      closeBlock();
      send('message_delta', {
        type: 'message_delta',
        delta: { stop_reason: finish || 'end_turn', stop_sequence: null },
        usage: { output_tokens: outputTokens },
      });
      send('message_stop', { type: 'message_stop' });
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
            const delta = choice?.delta;
            if (!delta) continue;
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
    let openaiBody = toOpenAI(body, route.model);
    const redacted = redactRequest(openaiBody, 'anthropic', res);
    if (!redacted) return;
    openaiBody = redacted.body;
    debugDump(openaiBody);

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
      return await streamTranslate(up, res, body.model, route, redacted.map);
    }
    const j = await up.json();
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
