import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  Redactor,
  createState,
  loadDictionary,
  restoreText,
  dlpText,
} from '../redact.mjs';
import { createStreamProcessor } from '../redact-stream.mjs';

const example = path.join(import.meta.dirname, '..', 'redaction.entities.example.json');

function makeRedactor(dictPath = example) {
  return new Redactor({ mode: 'strict', dictionary: dictPath });
}

test('dictionary loads aliases and patterns', () => {
  const dict = loadDictionary(example);
  assert.equal(dict.entities.length, 5);
  assert.ok(dict.aliasCount >= 10);
  assert.ok(dict.patterns.length >= 4);
  assert.ok(dict.credentials.length >= 5);
});

test('strict mode rejects other modes', () => {
  assert.throws(() => new Redactor({ mode: 'redact-only', dictionary: example }), /only strict/i);
});

test('fail closed when dictionary missing', () => {
  const r = new Redactor({ mode: 'strict', dictionary: '/nonexistent/entities.json' });
  assert.equal(r.ready, false);
  assert.throws(() => r.redactText('示例集团', createState()), /dictionary unavailable/i);
});

test('longest company alias wins', () => {
  const r = makeRedactor();
  const state = createState();
  const out = r.redactText('示例集团控股有限公司与示例子公司签约', state);
  assert.ok(out.includes('[[CO-TEST-CO-01]]'));
  assert.ok(out.includes('[[CO-TEST-CO-02]]'));
  assert.ok(!out.includes('示例集团'));
  assert.ok(!out.includes('示例子公司'));
});

test('english aliases respect word boundaries and case/full-width variants', () => {
  const r = makeRedactor();
  const state = createState();
  const out = r.redactText('Example Group and ＥＸＡＭＰＬＥ ＧＲＯＵＰ but not example-groupx', state);
  assert.equal(out.match(/\[\[CO-TEST-CO-01\]\]/g)?.length, 2);
  assert.ok(out.includes('example-groupx'));
});

test('patterns redact phone/email/host/path and remain restorable', () => {
  const r = makeRedactor();
  const state = createState();
  const src = '联系 13800138000 或 zhang@example.com，服务器 10.1.2.3，路径 /mnt/example/data/a.json';
  const out = r.redactText(src, state);
  assert.ok(!out.includes('13800138000'));
  assert.ok(!out.includes('zhang@example.com'));
  assert.ok(!out.includes('10.1.2.3'));
  assert.ok(!out.includes('/mnt/example/data/a.json'));
  assert.equal(restoreText(out, state.map), src);
});

test('credentials are irreversibly masked', () => {
  const r = makeRedactor();
  const state = createState();
  const out = r.redactText('Authorization: Bearer abcdefghijklmnop apiKey=sk-abcdefghijklmnop', state);
  assert.ok(!out.includes('abcdefghijklmnop'));
  assert.ok(out.includes('[CREDENTIAL_REDACTED]'));
});

test('openai body redaction covers messages, tool args, tool descriptions and blocks attachments', () => {
  const r = makeRedactor();
  const body = {
    model: 'external',
    messages: [
      { role: 'system', content: '你是示例集团的助手' },
      { role: 'user', content: [
        { type: 'text', text: '分析 /mnt/example/workspace 下张三的数据' },
        { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
      ] },
      { role: 'assistant', content: null, tool_calls: [
        { id: 't1', type: 'function', function: { name: 'read', arguments: '{"path":"/mnt/example/workspace/a.txt","owner":"张三"}' } },
      ] },
      { role: 'tool', tool_call_id: 't1', content: '示例集团控股有限公司内部文件' },
    ],
    tools: [{ type: 'function', function: { name: 'read', description: '读取示例集团文件', parameters: { type: 'object', properties: { path: { type: 'string', description: '示例集团路径' } } } } }],
  };
  const { body: out, map, state } = r.redactOpenAiBody(body);
  const serialized = JSON.stringify(out);
  assert.ok(!serialized.includes('示例集团'));
  assert.ok(!serialized.includes('张三'));
  assert.ok(!serialized.includes('/mnt/example/workspace'));
  assert.ok(!serialized.includes('AAAA'));
  assert.ok(serialized.includes('[attachment blocked by strict redaction proxy]'));
  assert.ok(out.messages[0].content.includes('[[CO-TEST-CO-01]]'));
  assert.ok(out.messages.some((m) => m.role === 'system' && m.content.includes('脱敏占位符')));
  const args = out.messages.find((m) => m.role === 'assistant').tool_calls[0].function.arguments;
  assert.ok(args.includes('[[PATH-TEST-'));
  assert.ok(restoreText(args, map).includes('/mnt/example/workspace/a.txt'));
  assert.ok(state.hits.CO >= 2);
});

test('response DLP masks guessed real entities before restore', () => {
  const r = makeRedactor();
  const state = createState();
  const redacted = r.redactText('示例集团控股有限公司', state);
  const map = state.map;
  const leaked = `答案是示例集团控股有限公司，占位符 ${redacted}`;
  const out = r.processText(leaked, map, createState());
  assert.ok(out.includes('[DLP-BLOCKED:CO]'));
  assert.ok(out.includes('示例集团控股有限公司')); // restored placeholder remains legitimate locally
  assert.equal(out.match(/示例集团控股有限公司/g).length, 1);
});

test('stream processor restores placeholders split across chunks', () => {
  const r = makeRedactor();
  const state = createState();
  const redacted = r.redactText('示例集团控股有限公司', state);
  const proc = createStreamProcessor(r, state.map);
  const mid = Math.floor(redacted.length / 2);
  let out = proc.push(redacted.slice(0, mid));
  out += proc.push(redacted.slice(mid));
  out += proc.flush();
  assert.equal(out, '示例集团控股有限公司');
});

test('stream processor masks DLP across chunks', () => {
  const r = makeRedactor();
  const proc = createStreamProcessor(r, new Map());
  let out = proc.push('示例集');
  out += proc.push('团控股有限公司');
  out += proc.flush();
  assert.ok(out.includes('[DLP-BLOCKED:CO]'));
  assert.ok(!out.includes('示例集团'));
});

test('dictionary can disable default patterns', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'redact-'));
  const p = path.join(dir, 'entities.json');
  fs.writeFileSync(p, JSON.stringify({
    placeholderSalt: 'X',
    useDefaultPatterns: false,
    useDefaultCredentialPatterns: false,
    entities: [{ id: '01', type: 'CO', canonical: '甲公司' }],
  }));
  const r = makeRedactor(p);
  const state = createState();
  const out = r.redactText('甲公司 13800138000', state);
  assert.ok(out.includes('[[CO-X-01]]'));
  assert.ok(out.includes('13800138000'));
});
