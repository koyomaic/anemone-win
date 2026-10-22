import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { createProxy } from '../proxy.mjs';

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

async function startProxy(config) {
  const proxy = createProxy(config);
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
