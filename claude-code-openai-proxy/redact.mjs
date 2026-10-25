import crypto from 'node:crypto';
import fs from 'node:fs';

export const REDACTION_MODE = 'strict';
export const REDACTION_SYSTEM_NOTE =
  '[[CO-*]]、[[PER-*]]、[[HOST-*]]、[[PATH-*]] 等是脱敏占位符。' +
  '请原样保留，不要猜测、翻译或改写其真实名称。';
export const ATTACHMENT_BLOCKED_NOTE = '[attachment blocked by strict redaction proxy]';
export const CREDENTIAL_MASK = '[CREDENTIAL_REDACTED]';

const DEFAULT_CREDENTIAL_PATTERNS = [
  {
    type: 'CRED',
    pattern: '\\b(?:authorization|proxy-authorization|x-api-key)\\b\\s*[:=]\\s*(?:bearer\\s+)?[^\\s,;"}\\]]+',
    flags: 'gi',
  },
  { type: 'CRED', pattern: '\\bbearer\\s+[A-Za-z0-9._~+/=-]{8,}', flags: 'gi' },
  {
    type: 'CRED',
    pattern:
      '\\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|passwd|pwd)\\b\\s*[:=]\\s*["\']?[^\\s"\',;}{\\]]+',
    flags: 'gi',
  },
  { type: 'CRED', pattern: '\\bsk-[A-Za-z0-9_-]{12,}', flags: 'g' },
  {
    type: 'CRED',
    pattern: '-----BEGIN [A-Z ]*PRIVATE KEY-----[\\s\\S]*?-----END [A-Z ]*PRIVATE KEY-----',
    flags: 'g',
  },
];

const DEFAULT_PATTERNS = [
  { type: 'PHONE', pattern: '(?<!\\d)1[3-9]\\d{9}(?!\\d)', flags: 'g' },
  {
    type: 'EMAIL',
    pattern: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}',
    flags: 'g',
  },
  {
    type: 'HOST',
    pattern:
      '(?<!\\d)(?:10|127|172\\.(?:1[6-9]|2\\d|3[01])|192\\.168)(?:\\.\\d{1,3}){1,3}(?!\\d)',
    flags: 'g',
  },
];

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function shortHash(value) {
  return crypto.createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 8).toUpperCase();
}

function compilePattern(entry, fallbackType) {
  const type = String(entry.type || fallbackType || 'ENT').toUpperCase();
  const source = entry.pattern || entry.regex;
  if (!source) throw new Error('pattern missing source');
  let flags = String(entry.flags || 'g');
  if (!flags.includes('g')) flags += 'g';
  return { type, re: new RegExp(source, flags), replacement: entry.replacement || '' };
}

export function normalizeWithMap(input) {
  const chars = [];
  const starts = [];
  const ends = [];
  for (let i = 0; i < input.length; ) {
    const cp = input.codePointAt(i);
    const ch = String.fromCodePoint(cp);
    const norm = ch.normalize('NFKC').toLowerCase();
    for (const nc of norm) {
      chars.push(nc);
      // starts/ends 必须按 norm.text 的 code unit 索引：AC search 以 code unit 定位，
      // 而代理对（emoji 等 astral 字符）1 码点占 2 code unit，只 push 一次会让
      // 其后所有匹配的 norm→orig 映射错位，替换打偏 → 别名残留 → fail-closed。
      for (let u = 0; u < nc.length; u += 1) {
        starts.push(i);
        ends.push(i + ch.length);
      }
    }
    i += ch.length;
  }
  return { text: chars.join(''), starts, ends };
}

function normalizeSimple(input) {
  return String(input || '').normalize('NFKC').toLowerCase();
}

function isWordChar(ch) {
  return !!ch && /[a-z0-9_]/i.test(ch);
}

class AcNode {
  constructor() {
    this.next = new Map();
    this.fail = null;
    this.out = [];
  }
}

export class AcAutomaton {
  constructor() {
    this.root = new AcNode();
    this.size = 0;
  }

  add(word, meta) {
    const norm = normalizeSimple(word);
    if (!norm) return;
    let node = this.root;
    // 按 code unit 建 trie，与 search() 的 code unit 扫描一致（含代理对的别名才能命中）
    for (let i = 0; i < norm.length; i += 1) {
      const ch = norm[i];
      if (!node.next.has(ch)) node.next.set(ch, new AcNode());
      node = node.next.get(ch);
    }
    node.out.push({ ...meta, length: norm.length, norm });
    this.size += 1;
  }

  build() {
    const queue = [];
    for (const child of this.root.next.values()) {
      child.fail = this.root;
      queue.push(child);
    }
    while (queue.length) {
      const node = queue.shift();
      for (const [ch, child] of node.next) {
        let fail = node.fail;
        while (fail && !fail.next.has(ch)) fail = fail.fail;
        child.fail = fail ? fail.next.get(ch) : this.root;
        if (child.fail === child) child.fail = this.root;
        if (child.fail.out.length) child.out.push(...child.fail.out);
        queue.push(child);
      }
    }
    return this;
  }

  search(text) {
    const matches = [];
    let node = this.root;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      while (node && !node.next.has(ch)) node = node.fail;
      node = node ? node.next.get(ch) : this.root;
      if (!node) node = this.root;
      for (const meta of node.out) {
        matches.push({ start: i - meta.length + 1, end: i + 1, meta });
      }
    }
    return matches;
  }
}

function boundaryOk(text, match) {
  if (!match.meta.boundary) return true;
  const before = match.start > 0 ? text[match.start - 1] : '';
  const after = match.end < text.length ? text[match.end] : '';
  return !isWordChar(before) && !isWordChar(after);
}

function selectLongest(matches, textLength) {
  if (!matches.length) return [];
  const ordered = [...matches].sort(
    (a, b) => b.end - b.start - (a.end - a.start) || a.start - b.start
  );
  const occupied = new Uint8Array(textLength);
  const chosen = [];
  for (const m of ordered) {
    let free = true;
    for (let i = m.start; i < m.end; i += 1) {
      if (occupied[i]) {
        free = false;
        break;
      }
    }
    if (!free) continue;
    for (let i = m.start; i < m.end; i += 1) occupied[i] = 1;
    chosen.push(m);
  }
  return chosen.sort((a, b) => a.start - b.start);
}

export function loadDictionary(dictPath) {
  const raw = JSON.parse(fs.readFileSync(dictPath, 'utf8'));
  if (!raw || typeof raw !== 'object') throw new Error('dictionary must be an object');
  const salt = String(raw.placeholderSalt || 'A7F3').toUpperCase();
  const entities = [];
  const ac = new AcAutomaton();
  let maxAliasLength = 0;

  for (const [idx, entry] of (raw.entities || []).entries()) {
    const type = String(entry.type || 'ENT').toUpperCase();
    const id = String(entry.id || `${type}-${String(idx + 1).padStart(2, '0')}`);
    const aliases = [...new Set([entry.canonical, ...(entry.aliases || [])].filter(Boolean).map(String))];
    const canonical = aliases[0];
    if (!canonical) throw new Error(`entity ${id} missing canonical alias`);
    const placeholder = entry.placeholder || `[[${type}-${salt}-${id}]]`;
    const core = placeholder.replace(/^\[\[/, '').replace(/\]\]$/, '');
    const entity = { type, id, canonical, placeholder, core, aliases };
    entities.push(entity);
    for (const alias of aliases) {
      const norm = normalizeSimple(alias);
      maxAliasLength = Math.max(maxAliasLength, alias.length, norm.length);
      ac.add(alias, {
        kind: 'entity',
        entityIndex: entities.length - 1,
        boundary: /^[a-z0-9._-]+$/i.test(norm),
      });
    }
  }

  const patternSource = Array.isArray(raw.patterns) && raw.patterns.length
    ? raw.patterns
    : (raw.useDefaultPatterns === false ? [] : DEFAULT_PATTERNS);
  const patterns = patternSource.map((p) => compilePattern(p, 'ENT'));
  const credentialSource = Array.isArray(raw.credentialPatterns) && raw.credentialPatterns.length
    ? raw.credentialPatterns
    : (raw.useDefaultCredentialPatterns === false ? [] : DEFAULT_CREDENTIAL_PATTERNS);
  const credentials = credentialSource.map((p) => compilePattern(p, 'CRED'));

  ac.build();
  return {
    path: dictPath,
    salt,
    entities,
    patterns,
    credentials,
    ac,
    maxAliasLength,
    entityCount: entities.length,
    aliasCount: ac.size,
    loadedAt: Date.now(),
  };
}

export function createState() {
  return { map: new Map(), hits: {}, dlp: 0, credentials: 0 };
}

function bump(state, key, n = 1) {
  state.hits[key] = (state.hits[key] || 0) + n;
}

function addMapping(state, core, value) {
  if (!state.map.has(core)) state.map.set(core, value);
}

function maskCredentials(text, dict, state) {
  let out = text;
  for (const cred of dict.credentials) {
    out = out.replace(cred.re, (match) => {
      state.credentials += 1;
      bump(state, cred.type);
      return cred.replacement || CREDENTIAL_MASK;
    });
  }
  return out;
}

function scanEntities(text, dict) {
  const norm = normalizeWithMap(text);
  const matches = dict.ac.search(norm.text).filter((m) => boundaryOk(norm.text, m));
  return { norm, matches: selectLongest(matches, norm.text.length) };
}

function redactEntities(text, dict, state) {
  const { norm, matches } = scanEntities(text, dict);
  if (!matches.length) return text;
  let out = '';
  let last = 0;
  for (const m of matches) {
    const start = norm.starts[m.start];
    const end = norm.ends[m.end - 1];
    const entity = dict.entities[m.meta.entityIndex];
    out += text.slice(last, start) + entity.placeholder;
    addMapping(state, entity.core, entity.canonical);
    bump(state, entity.type);
    last = end;
  }
  return out + text.slice(last);
}

function redactPatterns(text, dict, state) {
  let out = text;
  for (const p of dict.patterns) {
    out = out.replace(p.re, (match) => {
      const core = `${p.type}-${dict.salt}-${shortHash(match)}`;
      const placeholder = `[[${core}]]`;
      addMapping(state, core, match);
      bump(state, p.type);
      return placeholder;
    });
  }
  return out;
}

export function assertNoEntities(text, dict) {
  const { matches } = scanEntities(text, dict);
  if (matches.length) {
    const types = [...new Set(matches.map((m) => dict.entities[m.meta.entityIndex].type))];
    throw new Error(`residual sensitive entities after redaction: ${types.join(',')}`);
  }
}

export function restoreRegex(map) {
  const cores = [...map.keys()].sort((a, b) => b.length - a.length).map(escapeRegex);
  if (!cores.length) return null;
  return new RegExp(`\\[{0,2}\\s*(${cores.join('|')})\\s*\\]{0,2}`, 'gi');
}

export function restoreText(text, map) {
  if (!map || !map.size || typeof text !== 'string' || !text) return text;
  const re = restoreRegex(map);
  if (!re) return text;
  return text.replace(re, (match, core) => {
    const value = map.get(String(core).toUpperCase());
    return value == null ? match : value;
  });
}

export function dlpText(text, dict, state) {
  if (!dict || typeof text !== 'string' || !text) return text;
  let out = maskCredentials(text, dict, state);
  const { norm, matches } = scanEntities(out, dict);
  if (!matches.length) return out;
  let result = '';
  let last = 0;
  for (const m of matches) {
    const start = norm.starts[m.start];
    const end = norm.ends[m.end - 1];
    const entity = dict.entities[m.meta.entityIndex];
    result += out.slice(last, start) + `[DLP-BLOCKED:${entity.type}]`;
    state.dlp += 1;
    bump(state, `dlp_${entity.type}`);
    last = end;
  }
  return result + out.slice(last);
}

function redactUnknownValue(value, redactor, state, depth = 0) {
  if (typeof value === 'string') return redactor.redactText(value, state);
  if (Array.isArray(value)) return value.map((v) => redactUnknownValue(v, redactor, state, depth + 1));
  if (value && typeof value === 'object' && depth < 12) {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = redactUnknownValue(v, redactor, state, depth + 1);
    return out;
  }
  return value;
}

function redactContentParts(parts, redactor, state) {
  const out = [];
  for (const part of parts || []) {
    if (!part || typeof part !== 'object') {
      out.push(redactUnknownValue(part, redactor, state));
      continue;
    }
    if (part.type === 'text' && typeof part.text === 'string') {
      out.push({ ...part, text: redactor.redactText(part.text, state) });
      continue;
    }
    if (part.type === 'image_url' || part.type === 'input_audio' || part.type === 'file' || part.type === 'image') {
      out.push({ type: 'text', text: ATTACHMENT_BLOCKED_NOTE });
      continue;
    }
    out.push(redactUnknownValue(part, redactor, state));
  }
  return out;
}

function redactSchema(node, redactor, state, depth = 0) {
  if (!node || typeof node !== 'object' || depth > 12) return node;
  if (Array.isArray(node)) {
    for (const item of node) redactSchema(item, redactor, state, depth + 1);
    return node;
  }
  for (const [key, value] of Object.entries(node)) {
    if (key === 'description' && typeof value === 'string') {
      node[key] = redactor.redactText(value, state);
    } else if (value && typeof value === 'object') {
      redactSchema(value, redactor, state, depth + 1);
    }
  }
  return node;
}

export class Redactor {
  constructor(config = {}) {
    const mode = String(config.mode || REDACTION_MODE).toLowerCase();
    if (mode !== REDACTION_MODE) {
      throw new Error(`unsupported redaction mode: ${mode}; only strict is allowed`);
    }
    this.config = {
      mode: REDACTION_MODE,
      dictionary: config.dictionary || '',
      restoreResponses: true,
      restoreToolArgs: true,
      failClosed: true,
      responseDlp: true,
      blockAttachments: true,
    };
    this.dict = null;
    this.error = '';
    this.entityMap = new Map();
    this._mapCache = new WeakMap();
    this.reload();
  }

  reload() {
    try {
      if (!this.config.dictionary) throw new Error('dictionary path is required');
      this.dict = loadDictionary(this.config.dictionary);
      this.entityMap = new Map(this.dict.entities.map((e) => [e.core, e.canonical]));
      this.error = '';
    } catch (e) {
      this.dict = null;
      this.entityMap = new Map();
      this.error = e.message;
    }
    return this.ready;
  }

  get ready() {
    return !!this.dict;
  }

  health() {
    return {
      enabled: true,
      mode: REDACTION_MODE,
      ready: this.ready,
      error: this.error || undefined,
      dictionary: this.config.dictionary,
      entities: this.dict?.entityCount || 0,
      aliases: this.dict?.aliasCount || 0,
      patterns: this.dict?.patterns.length || 0,
      credentialPatterns: this.dict?.credentials.length || 0,
    };
  }

  ensureReady() {
    if (!this.ready) {
      throw new Error(`redaction dictionary unavailable: ${this.error || 'unknown'}`);
    }
  }

  redactText(text, state) {
    this.ensureReady();
    if (typeof text !== 'string' || !text) return text;
    let out = maskCredentials(text, this.dict, state);
    out = redactEntities(out, this.dict, state);
    out = redactPatterns(out, this.dict, state);
    assertNoEntities(out, this.dict);
    return out;
  }

  combineMap(map) {
    if (!this.entityMap.size) return map || new Map();
    if (!map) return this.entityMap;
    if (this._mapCache.has(map)) return this._mapCache.get(map);
    const combined = new Map([...this.entityMap, ...map]);
    this._mapCache.set(map, combined);
    return combined;
  }

  processText(text, map, state) {
    if (typeof text !== 'string' || !text) return text;
    let out = text;
    if (this.config.responseDlp) {
      this.ensureReady();
      out = dlpText(out, this.dict, state);
    }
    return restoreText(out, this.combineMap(map));
  }

  redactOpenAiBody(inputBody) {
    this.ensureReady();
    const state = createState();
    const body = structuredClone(inputBody);
    if (!Array.isArray(body.messages)) body.messages = [];

    for (const msg of body.messages) {
      if (!msg || typeof msg !== 'object') continue;
      if (typeof msg.content === 'string') {
        msg.content = this.redactText(msg.content, state);
      } else if (Array.isArray(msg.content)) {
        msg.content = redactContentParts(msg.content, this, state);
      } else if (msg.content && typeof msg.content === 'object') {
        msg.content = redactUnknownValue(msg.content, this, state);
      }
      if (typeof msg.reasoning === 'string') msg.reasoning = this.redactText(msg.reasoning, state);
      if (typeof msg.reasoning_content === 'string') {
        msg.reasoning_content = this.redactText(msg.reasoning_content, state);
      }
      for (const tc of msg.tool_calls || []) {
        const args = tc?.function?.arguments;
        if (typeof args === 'string') tc.function.arguments = this.redactText(args, state);
      }
    }

    for (const tool of body.tools || []) {
      const fn = tool?.function;
      if (!fn) continue;
      if (typeof fn.description === 'string') fn.description = this.redactText(fn.description, state);
      if (fn.parameters) redactSchema(fn.parameters, this, state);
    }

    const sys = body.messages.find((m) => m && m.role === 'system' && typeof m.content === 'string');
    if (sys) {
      if (!sys.content.includes(REDACTION_SYSTEM_NOTE)) {
        sys.content = `${REDACTION_SYSTEM_NOTE}\n\n${sys.content}`;
      }
    } else {
      body.messages.unshift({ role: 'system', content: REDACTION_SYSTEM_NOTE });
    }

    return { body, map: state.map, state };
  }

  processOpenAiResponse(json, map) {
    const state = createState();
    const out = structuredClone(json);
    for (const choice of out.choices || []) {
      const msg = choice?.message || choice?.delta || {};
      for (const key of ['content', 'reasoning', 'reasoning_content']) {
        if (typeof msg[key] === 'string') msg[key] = this.processText(msg[key], map, state);
      }
      for (const tc of msg.tool_calls || []) {
        const args = tc?.function?.arguments;
        if (typeof args === 'string') tc.function.arguments = this.processText(args, map, state);
      }
      if (typeof choice?.text === 'string') choice.text = this.processText(choice.text, map, state);
    }
    return { json: out, state };
  }

  createStreamProcessor(map) {
    return new StreamProcessor(this, this.combineMap(map));
  }
}

export class StreamProcessor {
  constructor(redactor, map) {
    this.redactor = redactor;
    this.map = map || new Map();
    this.state = createState();
    this.buf = '';
    const maxCore = [...this.map.keys()].reduce((m, c) => Math.max(m, c.length), 0);
    const maxAlias = redactor?.dict?.maxAliasLength || 0;
    this.hold = Math.max(maxCore + 8, maxAlias + 4, 24);
  }

  push(chunk) {
    if (typeof chunk !== 'string' || !chunk) return '';
    this.buf += chunk;
    return this._process(false);
  }

  flush() {
    return this._process(true);
  }

  _process(final) {
    if (!this.buf) return '';
    let cut = final ? this.buf.length : Math.max(0, this.buf.length - this.hold);
    // 切点不得落在占位符 [[…]] 内部：落在里面就回退到占位符起点，让它整体留在
    // 保留区，等 ']]' 到齐后下一轮完整还原。否则头尾两段分别过 restoreText 都
    // 匹配不上——原始占位符直接漏进工具参数与交付文本（2026-10-23 起实测两天
    // 50 处：tool_use 37 ＋ text 13，如 task#1405 交付正文带 [[ORG-32F6-01]]）。
    // 字面 '[[' 迟迟不闭合也无害：切点回退只压住 '[[' 起的尾巴，之前的正文照常
    // 流出，final flush 兜底放行。
    if (!final && cut > 0) {
      const head = this.buf.slice(0, cut);
      const open = head.lastIndexOf('[[');
      if (open >= 0 && head.indexOf(']]', open) < 0) cut = open;
    }
    if (cut <= 0) return '';
    let chunk = this.buf.slice(0, cut);
    this.buf = this.buf.slice(cut);
    chunk = this.redactor.processText(chunk, this.map, this.state);
    return chunk;
  }
}

export function createRedactor(config) {
  if (!config) return null;
  return new Redactor(config);
}
