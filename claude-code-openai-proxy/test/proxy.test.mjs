import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { createProxy, prefixTag } from '../proxy.mjs';

const dictionary = path.join(import.meta.dirname, '..', 'redaction.entities.example.json');

async function startServer(handler) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    server,
    port: server.address().port,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

async function startProxy(config, deps) {
  const proxy = createProxy(config, deps);
  await new Promise((resolve) => proxy.server.listen(0, '127.0.0.1', resolve));
  return {
    ...proxy,
    port: proxy.server.address().port,
    close: () => new Promise((resolve) => proxy.server.close(resolve)),
  };
}

function sseChunk(obj) {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

async function readText(res) {
  const chunks = [];
  for await (const c of res.body) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

test('openai non-stream redacts upstream and restores response', async (t) => {
  let upstreamBody = null;
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      upstreamBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({
        id: 'resp1',
        choices: [{
          finish_reason: 'stop',
          message: { role: 'assistant', content: '已处理 [[CO-TEST-CO-01]]' },
        }],
      }));
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key',
    model: 'external-model',
    routes: {},
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'external-model', messages: [{ role: 'user', content: '请分析示例集团控股有限公司' }] }),
  });
  const json = await res.json();
  const serialized = JSON.stringify(upstreamBody);
  assert.ok(serialized.includes('[[CO-TEST-CO-01]]'));
  assert.ok(!serialized.includes('示例集团'));
  assert.equal(json.choices[0].message.content, '已处理 示例集团控股有限公司');
});

test('openai stream restores placeholder split across chunks', async (t) => {
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ id: 's1', model: 'm', choices: [{ index: 0, delta: { content: '结果 [[CO-TEST-' } }] }));
      res.write(sseChunk({ id: 's1', model: 'm', choices: [{ index: 0, delta: { content: 'CO-01]] 完成' } }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key',
    model: 'm',
    routes: {},
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', stream: true, messages: [{ role: 'user', content: 'hi' }] }),
  });
  const text = await readText(res);
  assert.ok(text.includes('结果 示例集团控股有限公司 完成'));
  assert.ok(!text.includes('CO-TEST-CO-01'));
});

test('anthropic route restores tool call arguments', async (t) => {
  let upstreamBody = null;
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      upstreamBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const args = upstreamBody.messages.find((m) => m.role === 'user').content;
      const toolPath = args.replace(/^写入\s*/, '');
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({
        id: 'resp2',
        choices: [{
          finish_reason: 'tool_calls',
          message: {
            role: 'assistant',
            content: null,
            tool_calls: [{
              id: 'call1',
              type: 'function',
              function: { name: 'write', arguments: JSON.stringify({ path: toolPath }) },
            }],
          },
        }],
      }));
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key',
    model: 'm',
    routes: {},
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'm',
      max_tokens: 128,
      messages: [{ role: 'user', content: '写入 /mnt/example/workspace/out.txt' }],
    }),
  });
  const json = await res.json();
  const serialized = JSON.stringify(upstreamBody);
  assert.ok(serialized.includes('[[PATH-TEST-'));
  assert.ok(!serialized.includes('/mnt/example/workspace'));
  assert.equal(json.content[0].type, 'tool_use');
  assert.equal(json.content[0].input.path, '/mnt/example/workspace/out.txt');
});

test('healthz reports strict readiness', async (t) => {
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: 'http://127.0.0.1:1/v1',
    apiKey: 'k',
    model: 'm',
    routes: { 'm2': { model: 'm2' } },
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => proxy.close());
  const res = await fetch(`http://127.0.0.1:${proxy.port}/healthz`);
  const json = await res.json();
  assert.equal(res.status, 200);
  assert.equal(json.ok, true);
  assert.equal(json.redaction.mode, 'strict');
  assert.ok(json.routes.includes('m2'));
});

test('fail closed when dictionary missing and does not call upstream', async (t) => {
  let called = false;
  const upstream = await startServer(() => { called = true; });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'k',
    model: 'm',
    routes: {},
    redaction: { mode: 'strict', dictionary: '/nonexistent/entities.json' },
  });
  t.after(() => { upstream.close(); proxy.close(); });
  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', messages: [{ role: 'user', content: 'hi' }] }),
  });
  assert.equal(res.status, 503);
  assert.equal(called, false);
  const health = await fetch(`http://127.0.0.1:${proxy.port}/healthz`);
  assert.equal(health.status, 503);
});

test('routes select upstream and model', async (t) => {
  let seenUrl = '';
  let seenBody = null;
  const upstream = await startServer((req, res) => {
    seenUrl = req.url;
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seenBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ choices: [{ message: { content: 'ok' }, finish_reason: 'stop' }] }));
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/default/v1`,
    apiKey: 'k',
    model: 'default-model',
    routes: { routed: { baseURL: `http://127.0.0.1:${upstream.port}/routed/v1`, model: 'upstream-routed' } },
  });
  t.after(() => { upstream.close(); proxy.close(); });
  await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'routed', messages: [{ role: 'user', content: 'hi' }] }),
  });
  assert.equal(seenUrl, '/routed/v1/chat/completions');
  assert.equal(seenBody.model, 'upstream-routed');
});

// 流式 usage 必须把 input_tokens 一并回传（2026-10-23）。
// 此前 message_delta 只带 output_tokens，claude-code 记到的 input_tokens 恒为 0
// （实测一条 resume 链 106 个回合全 0），relay 的 tokens 退化成纯输出，avatar 的
// resume 上下文预算闸门对图片载荷完全瞎：转写已 12.66MB，chain_tokens 才 48387。
test('anthropic stream reports input_tokens and asks upstream for usage', async (t) => {
  let upstreamBody = null;
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      upstreamBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({
        choices: [{ delta: { role: 'assistant', content: '好了' } }],
      }));
      // OpenAI 兼容网关的 usage 尾包（choices 为空）
      res.write(sseChunk({
        choices: [],
        usage: { prompt_tokens: 123456, completion_tokens: 789, total_tokens: 124245 },
      }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key',
    model: 'm',
    routes: {},
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'm',
      max_tokens: 128,
      stream: true,
      messages: [{ role: 'user', content: 'hi' }],
    }),
  });
  const text = await readText(res);

  // 显式索要 usage 尾包，否则上游根本不回 prompt_tokens
  assert.deepEqual(upstreamBody.stream_options, { include_usage: true });

  const delta = text
    .split('\n')
    .filter((l) => l.startsWith('data: '))
    .map((l) => JSON.parse(l.slice(6)))
    .find((e) => e.type === 'message_delta');
  assert.ok(delta, `未收到 message_delta：${text.slice(0, 400)}`);
  assert.equal(delta.usage.input_tokens, 123456);
  assert.equal(delta.usage.output_tokens, 789);
});

// stream_options 只在 anthropic 路由注入（那条路上 claude-code 说 Anthropic 协议，
// 永远不会自己带 stream_options）。/v1/chat/completions 是 OpenAI 兼容直通，
// 客户端自己负责 usage，代理不得改写——否则会把调用方显式关掉的 usage 又打开。
test('openai passthrough keeps client stream_options untouched', async (t) => {
  let upstreamBody = null;
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      upstreamBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ delta: { content: 'ok' } }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1',
    port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key',
    model: 'm',
    routes: {},
    redaction: { mode: 'strict', dictionary },
  });
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'm',
      stream: true,
      stream_options: { include_usage: false },
      messages: [{ role: 'user', content: 'hi' }],
    }),
  });
  await readText(res);
  assert.deepEqual(upstreamBody.stream_options, { include_usage: false });
});

// ── 前缀稳定性诊断（PROXY_PREFIX_LOG）──
//
// 后端（vLLM 经 APISIX）做自动前缀缓存，命中与否决定每轮 prefill 是 ~1s 还是 ~3s。
// 实测同一 12235-token 前缀连发 8 次耗时 3.33/3.01/0.89/1.02/1.09/2.09/0.96/2.96s，
// 明显双峰、4 快 4 慢——网关轮询到多副本、只有部分副本缓存了这段前缀。OpenAI 兼容
// 协议没有 cache_control，Anthropic 那套显式标记无处可打，所以只能把前缀做小做稳，
// 再量出真实命中率去跟网关侧谈会话亲和。prefixTag 就是量它的尺子。

const SYS = [{ role: 'system', content: '你是财务智脑。' }];
const TOOLS = [{ type: 'function', function: { name: 'Bash', description: 'run' } }];

test('prefixTag ignores the trailing user message', () => {
  const a = prefixTag({ messages: [...SYS, { role: 'user', content: '任务A' }], tools: TOOLS });
  const b = prefixTag({ messages: [...SYS, { role: 'user', content: '完全不同的任务B，长得多' }], tools: TOOLS });
  assert.equal(a.tag, b.tag, '任务描述在尾部 user 消息里，不该影响前缀 tag');
  assert.equal(a.sysChars, '你是财务智脑。'.length);
  assert.equal(a.tools, 1);
  assert.ok(a.tailChars < b.tailChars, 'tailChars 要能反映尾部可变体量');
});

test('prefixTag changes when system prompt or tools change', () => {
  const base = prefixTag({ messages: SYS, tools: TOOLS });
  const sys2 = prefixTag({ messages: [{ role: 'system', content: '你是别的助手。' }], tools: TOOLS });
  const tool2 = prefixTag({ messages: SYS, tools: [...TOOLS, { type: 'function', function: { name: 'Read' } }] });
  assert.notEqual(base.tag, sys2.tag);
  assert.notEqual(base.tag, tool2.tag);
  assert.equal(tool2.tools, 2);
});

test('prefixTag tolerates missing fields', () => {
  const t = prefixTag({});
  assert.equal(typeof t.tag, 'string');
  assert.equal(t.tag.length, 12);
  assert.equal(t.sysChars, 0);
  assert.equal(t.tools, 0);
  assert.equal(t.tailChars, 0);
});

test('PROXY_PREFIX_LOG emits one prefix line per streamed request', async (t) => {
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ delta: { role: 'assistant', content: '好' } }] }));
      res.write(sseChunk({ choices: [], usage: { prompt_tokens: 12235, completion_tokens: 8 } }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1', port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key', model: 'm', routes: {},
    redaction: { mode: 'strict', dictionary },
  }, { env: { PROXY_PREFIX_LOG: '1' } });
  t.after(() => { upstream.close(); proxy.close(); });

  const lines = [];
  const orig = console.log;
  console.log = (...a) => lines.push(a.join(' '));
  try {
    const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        model: 'm', max_tokens: 8, stream: true,
        system: '你是财务智脑。',
        messages: [{ role: 'user', content: '只回一个字：好' }],
      }),
    });
    await readText(res);
  } finally { console.log = orig; }

  const hit = lines.find((l) => l.includes(' prefix '));
  assert.ok(hit, `未见 prefix 日志：${lines.join('|').slice(0, 300)}`);
  const rec = JSON.parse(hit.slice(hit.indexOf('{')));
  assert.equal(rec.tag.length, 12);
  assert.equal(rec.inTok, 12235);
  assert.equal(rec.outTok, 8);
  assert.ok(typeof rec.ms === 'number' && rec.ms >= 0);
  assert.ok(rec.sysChars > 0);
});

test('prefix logging is off by default (zero overhead)', async (t) => {
  const upstream = await startServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ delta: { content: '好' } }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy({
    host: '127.0.0.1', port: 0,
    baseURL: `http://127.0.0.1:${upstream.port}/v1`,
    apiKey: 'test-key', model: 'm', routes: {},
    redaction: { mode: 'strict', dictionary },
  }, { env: {} });
  t.after(() => { upstream.close(); proxy.close(); });

  const lines = [];
  const orig = console.log;
  console.log = (...a) => lines.push(a.join(' '));
  try {
    const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: 'm', max_tokens: 8, stream: true, messages: [{ role: 'user', content: 'hi' }] }),
    });
    await readText(res);
  } finally { console.log = orig; }
  assert.ok(!lines.some((l) => l.includes(' prefix ')), '默认不该打 prefix 日志');
});

// ── 2026-10-23「切碎事故」回归 ──
// 脱敏 StreamProcessor 对每段文本/工具参数保留末尾 hold 字符等待跨块匹配，旧实现
// 只在流结束的 finally 里统一 flush；彼时内容块早已关闭，sendText/sendToolArgs 会
// **重开已关闭块**（同 id 重复 content_block_start），claude-code 视为新 tool_use：
// 每个逻辑调用被切成「头部（截断 JSON）＋尾巴（定长 hold 碎片）」，双双
// InputValidationError（__unparsedToolInput {"raw":…,"len":27}）。实测 10-23/24
// 两天 3,381 次逻辑调用被拒 1,898 次（56%）：并发调用 97-100% 被切、单调用带
// 前置文本 99.8% 被切、纯工具单调用 2.6% 幸存；两天烧掉 ~2 亿 input token。
// 修复：关块前先把该块保留区刷进块自身；已关闭块永不重开（文本另开新 index，
// 工具参数记 warn 丢弃）；流末兜底从未开块的短文本。

function parseAnthropicEvents(text) {
  const events = [];
  for (const line of text.split('\n')) {
    if (line.startsWith('data: ')) events.push(JSON.parse(line.slice(6)));
  }
  return events;
}

function blockText(events, index, deltaType, field) {
  return events
    .filter((e) => e.type === 'content_block_delta' && e.index === index
      && e.delta.type === deltaType)
    .map((e) => e.delta[field])
    .join('');
}

const STRICT = (port) => ({
  host: '127.0.0.1',
  port: 0,
  baseURL: `http://127.0.0.1:${port}/v1`,
  apiKey: 'test-key',
  model: 'm',
  routes: {},
  redaction: { mode: 'strict', dictionary },
});

test('anthropic stream: parallel tool calls stay intact, no block reopened', async (t) => {
  const args = [
    JSON.stringify({ command: "find /data/reports -name '*.csv' | head -20 && echo ALPHA-OK" }),
    JSON.stringify({ command: "find /data/reports -name '*.pdf' | head -20 && echo BETA-OK" }),
    JSON.stringify({ command: "find /data/reports -name '*.xlsx' | head -20 && echo GAMMA-OK" }),
  ];
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ index: 0, delta: { role: 'assistant', content: '我来依次检查三个目录并把结果汇总给你' } }] }));
      for (let i = 0; i < 3; i++) {
        res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, id: `call_${i}`, type: 'function', function: { name: 'Bash', arguments: '' } }] } }] }));
        res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, function: { arguments: args[i].slice(0, 30) } }] } }] }));
        res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, function: { arguments: args[i].slice(30) } }] } }] }));
      }
      res.write(sseChunk({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }));
      res.write(sseChunk({ choices: [], usage: { prompt_tokens: 100, completion_tokens: 60 } }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy(STRICT(upstream.port));
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', max_tokens: 256, stream: true, messages: [{ role: 'user', content: 'hi' }] }),
  });
  const events = parseAnthropicEvents(await readText(res));

  const starts = events.filter((e) => e.type === 'content_block_start');
  const idxs = starts.map((e) => e.index);
  assert.equal(new Set(idxs).size, idxs.length, `重复 content_block_start：${JSON.stringify(idxs)}`);

  const toolStarts = starts.filter((e) => e.content_block.type === 'tool_use');
  assert.equal(toolStarts.length, 3, '应恰有 3 个 tool_use 块');
  for (let i = 0; i < 3; i++) {
    const st = toolStarts.find((e) => e.content_block.id === `call_${i}`);
    assert.ok(st, `缺 call_${i} 的 tool_use 块`);
    const json = blockText(events, st.index, 'input_json_delta', 'partial_json');
    assert.deepEqual(JSON.parse(json), JSON.parse(args[i]), `call_${i} 参数不完整（被切碎）`);
  }

  const textStart = starts.find((e) => e.content_block.type === 'text');
  assert.ok(textStart, '缺文本块');
  assert.equal(blockText(events, textStart.index, 'text_delta', 'text'),
               '我来依次检查三个目录并把结果汇总给你');

  const stops = events.filter((e) => e.type === 'content_block_stop').map((e) => e.index);
  for (const i of idxs) assert.ok(stops.includes(i), `块 ${i} 未关闭`);
  assert.ok(events.some((e) => e.type === 'message_stop'));
});

test('anthropic stream: short tool args (< hold) fully delivered at close', async (t) => {
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_s', type: 'function', function: { name: 'Bash', arguments: '' } }] } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '{"command":"ls"}' } }] } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy(STRICT(upstream.port));
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', max_tokens: 64, stream: true, messages: [{ role: 'user', content: 'hi' }] }),
  });
  const events = parseAnthropicEvents(await readText(res));
  const starts = events.filter((e) => e.type === 'content_block_start');
  assert.equal(starts.length, 1);
  const json = blockText(events, starts[0].index, 'input_json_delta', 'partial_json');
  assert.deepEqual(JSON.parse(json), { command: 'ls' });
  assert.ok(events.some((e) => e.type === 'content_block_stop' && e.index === starts[0].index));
});

test('anthropic stream: short text-only reply (never opened block) not lost', async (t) => {
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ index: 0, delta: { content: '好的' } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy(STRICT(upstream.port));
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', max_tokens: 16, stream: true, messages: [{ role: 'user', content: 'hi' }] }),
  });
  const events = parseAnthropicEvents(await readText(res));
  const starts = events.filter((e) => e.type === 'content_block_start');
  assert.equal(starts.length, 1);
  assert.equal(starts[0].content_block.type, 'text');
  assert.equal(blockText(events, starts[0].index, 'text_delta', 'text'), '好的');
  assert.ok(events.some((e) => e.type === 'content_block_stop'));
  assert.ok(events.some((e) => e.type === 'message_stop'));
});

test('anthropic stream: placeholder split across tool-arg deltas still restored', async (t) => {
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_r', type: 'function', function: { name: 'Bash', arguments: '' } }] } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '{"command": "echo 报告 [[CO-TEST-' } }] } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: 'CO-01]] 已送达 done-marker" }' } }] } }] }));
      res.write(sseChunk({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy(STRICT(upstream.port));
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    // 请求里带实体 → 请求侧脱敏生成 map，响应侧才能还原上游回显的占位符
    body: JSON.stringify({ model: 'm', max_tokens: 64, stream: true, messages: [{ role: 'user', content: '请通知示例集团控股有限公司' }] }),
  });
  const events = parseAnthropicEvents(await readText(res));
  const starts = events.filter((e) => e.type === 'content_block_start');
  assert.equal(starts.length, 1);
  const json = blockText(events, starts[0].index, 'input_json_delta', 'partial_json');
  const parsed = JSON.parse(json);
  assert.equal(parsed.command, 'echo 报告 示例集团控股有限公司 已送达 done-marker');
});

test('openai stream: holdback tail flushed before finish_reason chunk', async (t) => {
  const full = '这是一段足够长的回复文本，用来确保超出脱敏保留窗口从而产生尾巴';
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(sseChunk({ id: 'x1', choices: [{ index: 0, delta: { content: full.slice(0, 20) } }] }));
      res.write(sseChunk({ id: 'x1', choices: [{ index: 0, delta: { content: full.slice(20) } }] }));
      res.write(sseChunk({ id: 'x1', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }));
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  const proxy = await startProxy(STRICT(upstream.port));
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'm', stream: true, messages: [{ role: 'user', content: 'hi' }] }),
  });
  const text = await readText(res);
  const chunks = text.split('\n').filter((l) => l.startsWith('data: ') && !l.includes('[DONE]'))
    .map((l) => JSON.parse(l.slice(6)));
  const finishPos = chunks.findIndex((c) => c.choices?.[0]?.finish_reason);
  const lastContentPos = chunks.reduce((acc, c, i) => (c.choices?.[0]?.delta?.content ? i : acc), -1);
  assert.ok(finishPos >= 0, '缺 finish_reason 块');
  assert.ok(lastContentPos < finishPos, `尾巴(${lastContentPos})必须在 finish_reason(${finishPos}) 之前`);
  const joined = chunks.map((c) => c.choices?.[0]?.delta?.content || '').join('');
  assert.equal(joined, full);
});

// ── Anthropic 直通模式（mode:'anthropic'，2026-10-25）──
// 上游原生说 Anthropic 协议（aigateway 实测：格式/流式/缓存/thinking 全支持），
// 直通转发只就地改 delta 文本，content_block_stop 即协议级刷新点——翻译层的
// 块重构/收尾猜测（两起切碎事故根源）整类消失，cache_control 得以保留（缓存
// 命中实测 cache_read=33732）。

function ptProxy(upstreamPort) {
  return startProxy({
    host: '127.0.0.1', port: 0,
    baseURL: 'http://127.0.0.1:1/v1',
    apiKey: 'k', model: 'm',
    routes: {
      'pt-model': {
        baseURL: `http://127.0.0.1:${upstreamPort}/v1`,
        apiKey: 'pt-key', model: 'upstream-model', mode: 'anthropic',
      },
    },
    redaction: { mode: 'strict', dictionary },
  });
}

function anthEvents(text) {
  return text.split('\n').filter((l) => l.startsWith('data: '))
    .map((l) => JSON.parse(l.slice(6)));
}

function writeAnthSse(res, events) {
  for (const e of events) res.write(`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`);
}

test('passthrough: request redacted, note first, cache_control kept, error verbatim', async (t) => {
  let upstreamBody = null; let upstreamUrl = ''; let upstreamKey = '';
  const upstream = await startServer((req, res) => {
    upstreamUrl = req.url; upstreamKey = req.headers['x-api-key'];
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      upstreamBody = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({
        id: 'msg_1', type: 'message', role: 'assistant', model: 'upstream-model',
        content: [{ type: 'text', text: '完成 [[CO-TEST-CO-01]]' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 33732 },
      }));
    });
  });
  const proxy = await ptProxy(upstream.port);
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'anthropic-version': '2023-06-01' },
    body: JSON.stringify({
      model: 'pt-model', max_tokens: 64,
      system: [{ type: 'text', text: '你是助手。敏感方：示例集团控股有限公司', cache_control: { type: 'ephemeral' } }],
      messages: [{ role: 'user', content: '查询示例集团控股有限公司的报价' }],
    }),
  });
  const json = await res.json();
  assert.equal(upstreamUrl, '/v1/messages');
  assert.equal(upstreamKey, 'pt-key');
  assert.equal(upstreamBody.model, 'upstream-model');
  const s = JSON.stringify(upstreamBody);
  assert.ok(s.includes('[[CO-TEST-CO-01]]'));
  assert.ok(!s.includes('示例集团'));
  assert.ok(upstreamBody.system[0].text.includes('脱敏占位符'), '系统注记须为第一块');
  assert.deepEqual(upstreamBody.system[1].cache_control, { type: 'ephemeral' }, 'cache_control 必须原位保留');
  assert.equal(json.content[0].text, '完成 示例集团控股有限公司');
  assert.equal(json.usage.cache_read_input_tokens, 33732, 'usage 缓存字段原生透传');
});

test('passthrough: upstream error status and body forwarded verbatim', async (t) => {
  const errBody = { type: 'error', error: { type: 'rate_limit_error', message: 'slow down' } };
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(429, { 'content-type': 'application/json' });
      res.end(JSON.stringify(errBody));
    });
  });
  const proxy = await ptProxy(upstream.port);
  t.after(() => { upstream.close(); proxy.close(); });
  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'pt-model', max_tokens: 8, messages: [{ role: 'user', content: 'hi' }] }),
  });
  assert.equal(res.status, 429);
  assert.deepEqual(await res.json(), errBody);
});

test('passthrough stream: restore across deltas, flush before stop, usage visible', async (t) => {
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      writeAnthSse(res, [
        { type: 'message_start', message: { id: 'msg_s', type: 'message', role: 'assistant', model: 'upstream-model', content: [], usage: { input_tokens: 18, output_tokens: 0, cache_read_input_tokens: 33732 } } },
        { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
        { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '结果 [[CO-TEST-' } },
        { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'CO-01]] 完成，尾巴还要足够长把切点推过占位符之后才行。' } },
        { type: 'content_block_stop', index: 0 },
        { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 30 } },
        { type: 'message_stop' },
      ]);
      res.end();
    });
  });
  const proxy = await ptProxy(upstream.port);
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'pt-model', max_tokens: 128, stream: true,
      messages: [{ role: 'user', content: '请分析示例集团控股有限公司' }],
    }),
  });
  const events = anthEvents(await readText(res));
  const textOut = events
    .filter((e) => e.type === 'content_block_delta' && e.delta.type === 'text_delta')
    .map((e) => e.delta.text).join('');
  assert.equal(textOut, '结果 示例集团控股有限公司 完成，尾巴还要足够长把切点推过占位符之后才行。');
  const types = events.map((e) => e.type);
  assert.ok(types.lastIndexOf('content_block_delta') < types.indexOf('content_block_stop'),
            '保留区尾巴必须在 content_block_stop 之前补发');
  const ms = events.find((e) => e.type === 'message_start');
  assert.equal(ms.message.usage.cache_read_input_tokens, 33732);
  assert.ok(types.includes('message_stop'));
});

test('passthrough stream: parallel tool blocks intact, thinking restored, signature kept', async (t) => {
  const args0 = JSON.stringify({ command: "ls /tmp/alpha && echo ALPHA-OK-123" });
  const args1 = JSON.stringify({ command: "ls /tmp/betaa && echo BETA-OK-456" });
  const split = (s, n) => [s.slice(0, n), s.slice(n, Math.floor((n + s.length) / 2)), s.slice(Math.floor((n + s.length) / 2))];
  const upstream = await startServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const ev = [
        { type: 'message_start', message: { id: 'msg_t', type: 'message', role: 'assistant', model: 'upstream-model', content: [], usage: { input_tokens: 5, output_tokens: 0 } } },
        { type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } },
        { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: '先查 [[CO-TEST-' } },
        { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: 'CO-01]] 的两个目录再汇总' } },
        { type: 'content_block_delta', index: 0, delta: { type: 'signature_delta', signature: 'sig-abc' } },
        { type: 'content_block_stop', index: 0 },
        { type: 'content_block_start', index: 1, content_block: { type: 'tool_use', id: 'call_a', name: 'Bash', input: {} } },
      ];
      for (const p of split(args0, 12)) ev.push({ type: 'content_block_delta', index: 1, delta: { type: 'input_json_delta', partial_json: p } });
      ev.push({ type: 'content_block_stop', index: 1 });
      ev.push({ type: 'content_block_start', index: 2, content_block: { type: 'tool_use', id: 'call_b', name: 'Bash', input: {} } });
      for (const p of split(args1, 9)) ev.push({ type: 'content_block_delta', index: 2, delta: { type: 'input_json_delta', partial_json: p } });
      ev.push({ type: 'content_block_stop', index: 2 });
      ev.push({ type: 'message_delta', delta: { stop_reason: 'tool_use' }, usage: { output_tokens: 40 } });
      ev.push({ type: 'message_stop' });
      writeAnthSse(res, ev);
      res.end();
    });
  });
  const proxy = await ptProxy(upstream.port);
  t.after(() => { upstream.close(); proxy.close(); });

  const res = await fetch(`http://127.0.0.1:${proxy.port}/v1/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'pt-model', max_tokens: 256, stream: true,
      messages: [{ role: 'user', content: '查示例集团控股有限公司的目录' }],
    }),
  });
  const events = anthEvents(await readText(res));
  const starts = events.filter((e) => e.type === 'content_block_start');
  assert.deepEqual(starts.map((e) => e.index), [0, 1, 2], '块序原样，无重复无重排');
  const think = events.filter((e) => e.type === 'content_block_delta' && e.index === 0)
    .map((e) => e.delta.thinking || '').join('');
  assert.equal(think, '先查 示例集团控股有限公司 的两个目录再汇总');
  assert.ok(events.some((e) => e.delta?.type === 'signature_delta' && e.delta.signature === 'sig-abc'),
            'signature_delta 原样透传');
  for (const [idx, id, args] of [[1, 'call_a', args0], [2, 'call_b', args1]]) {
    const st = starts.find((e) => e.index === idx);
    assert.equal(st.content_block.id, id);
    const json = events.filter((e) => e.type === 'content_block_delta' && e.index === idx)
      .map((e) => e.delta.partial_json || '').join('');
    assert.deepEqual(JSON.parse(json), JSON.parse(args), `${id} 参数必须完整可解析`);
  }
});
