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

// 回归（2026-10-25）：占位符后跟随超过保留窗口的正文时，旧实现的切点会落进
// 占位符内部，头尾两段各自 restoreText 都匹配不上——原始占位符直接漏进工具
// 参数与交付文本（10-23 起两天实测 50 处：tool_use 37 ＋ text 13）。
test('stream processor never cuts inside a placeholder', () => {
  const r = makeRedactor();
  const state = createState();
  const redacted = r.redactText('示例集团控股有限公司', state);   // [[CO-TEST-CO-01]]
  const proc = createStreamProcessor(r, state.map);
  const tail1 = '尾部十三字符补足窗口测试用';
  const tail2 = '第二段继续补充一些内容确保越过保留窗口后再收尾。';
  let out = proc.push(`回执：${redacted}${tail1}`);
  out += proc.push(tail2);
  out += proc.flush();
  assert.equal(out, `回执：示例集团控股有限公司${tail1}${tail2}`);
  assert.ok(!out.includes('[['), `原始占位符漏出: ${out.slice(0, 60)}`);
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

test('surrogate-pair emoji do not misalign entity replacement (regression: relay task#1326)', () => {
  // 🔒/🤖 为代理对（1 码点 2 code unit），… 经 NFKC 1→3 展开。
  // 旧实现 starts/ends 按码点建、AC search 按 code unit 定位：emoji 出现在别名
  // 之前时 norm→orig 映射错位，替换打偏、别名残留 → assertNoEntities fail-closed，
  // 整个请求 503（claude 重试耗尽后任务失败）。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'redact-'));
  const p = path.join(dir, 'entities.json');
  fs.writeFileSync(p, JSON.stringify({
    placeholderSalt: 'E1',
    entities: [{ id: '01', type: 'CO', canonical: '示例集团控股有限公司', aliases: ['示例集团', '示例'] }],
  }));
  const r = makeRedactor(p);
  const state = createState();
  const text = '🔒锁…前文提到示例集团控股有限公司，中间还有🤖表情与省略号…之后示例集团再次出现';
  const out = r.redactText(text, state);
  assert.ok(!out.includes('示例'));
  assert.equal(out, '🔒锁…前文提到[[CO-E1-01]]，中间还有🤖表情与省略号…之后[[CO-E1-01]]再次出现');
  assert.equal(state.hits.CO, 2);
});

test('astral-plane aliases match (code-unit trie)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'redact-'));
  const p = path.join(dir, 'entities.json');
  fs.writeFileSync(p, JSON.stringify({
    placeholderSalt: 'E2',
    entities: [{ id: '01', type: 'CO', canonical: '🔒保密单位' }],
  }));
  const r = makeRedactor(p);
  const state = createState();
  const out = r.redactText('联系🔒保密单位处理', state);
  assert.equal(out, '联系[[CO-E2-01]]处理');
});
