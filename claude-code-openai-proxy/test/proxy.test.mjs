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
