const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const sourcePath = 'ani365_bot/web_static/app.js';
const source = fs.readFileSync(sourcePath, 'utf8');

test('app.js parses', () => {
  const check = spawnSync(process.execPath, ['--check', sourcePath], {encoding: 'utf8'});
  assert.equal(check.status, 0, check.stderr);
});

test('only one recommendations function exists', () => {
  const declarations = source.match(/async function recommendations\s*\(/g) || [];
  assert.deepEqual(declarations, ['async function recommendations(']);
});

test('no recommendations function reads more before its own const more', () => {
  const start = source.indexOf('async function recommendations');
  const next = source.indexOf('\n  async function ', start + 10);
  const body = source.slice(start, next);
  const declaration = body.search(/\b(?:const|let)\s+more\b/);
  if (declaration === -1) return;
  const before = body.slice(0, declaration);
  assert.equal(/\bmore\b/.test(before.replace(/async function recommendations[\s\S]*?\{/, '')), false);
});

function parseAttrs(raw) {
  const attrs = {};
  const re = /([:@\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  let match;
  while ((match = re.exec(raw))) {
    if (match[1] === '/') continue;
    attrs[match[1]] = match[2] ?? match[3] ?? match[4] ?? '';
  }
  return attrs;
}

class MiniElement {
  constructor(tag, doc) {
    this.doc = doc;
    this.tagName = String(tag || 'div').toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.onclick = null;
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this.value = '';
    this._text = '';
  }

  get id() { return this.attrs.id || ''; }
  set id(value) { this.attrs.id = value; }
  get className() { return this.attrs.class || ''; }
  set className(value) { this.attrs.class = value; }
  get classList() {
    const read = () => (this.attrs.class || '').split(/\s+/).filter(Boolean);
    const write = names => { this.attrs.class = names.join(' '); };
    return {
      contains: name => read().includes(name),
      add: (...names) => write([...new Set([...read(), ...names])]),
      remove: (...names) => write(read().filter(name => !names.includes(name))),
      toggle: (name, force) => {
        const has = read().includes(name);
        const next = force === undefined ? !has : force;
        if (next) this.classList.add(name);
        else this.classList.remove(name);
        return next;
      },
    };
  }
  get dataset() {
    const data = {};
    for (const [key, value] of Object.entries(this.attrs)) {
      if (key.startsWith('data-')) data[key.slice(5)] = value;
    }
    return data;
  }
  get textContent() {
    return this._text + this.children.map(child => child.textContent || child.text || '').join('');
  }
  set textContent(value) {
    this.children = [];
    this._text = String(value ?? '');
  }
  get innerHTML() { return this.serialize(); }
  set innerHTML(value) {
    this._text = '';
    this.children = parseHTML(String(value ?? ''), this.doc);
    this.children.forEach(child => { if (child.parent !== undefined) child.parent = this; });
    this.doc.notify();
  }
  serialize() {
    const inner = this._text + this.children.map(child => child.serialize ? child.serialize() : child.text || '').join('');
    const attrs = Object.entries(this.attrs).map(([key, value]) => value === '' ? key : `${key}="${value}"`).join(' ');
    const open = attrs ? `<${this.tagName.toLowerCase()} ${attrs}>` : `<${this.tagName.toLowerCase()}>`;
    return open + inner + `</${this.tagName.toLowerCase()}>`;
  }
  matches(selector) {
    if (selector.includes(':checked')) {
      const [base] = selector.split(':');
      return this.matches(base) && (this.checked || this.attrs.checked !== undefined);
    }
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    if (selector.startsWith('[') && selector.endsWith(']')) {
      const body = selector.slice(1, -1);
      const equal = body.indexOf('=');
      if (equal === -1) return Object.prototype.hasOwnProperty.call(this.attrs, body);
      const key = body.slice(0, equal);
      const expected = body.slice(equal + 1).replace(/^["']|["']$/g, '');
      return this.attrs[key] === expected;
    }
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = node => {
      for (const child of node.children || []) {
        if (!child.matches) continue;
        if (child.matches(selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get previousElementSibling() {
    const siblings = (this.parent?.children || []).filter(child => child.matches);
    const index = siblings.indexOf(this);
    return index > 0 ? siblings[index - 1] : null;
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
  }
  contains(node) {
    if (node === this) return true;
    return (this.children || []).some(child => child.contains && child.contains(node));
  }
  insertAdjacentHTML(position, html) {
    const nodes = parseHTML(html, this.doc);
    if (!nodes.length) throw new Error('parse failed: ' + html.slice(0, 180));
    nodes.forEach(node => { node.parent = this; });
    if (position === 'afterbegin') this.children.unshift(...nodes);
    else this.children.push(...nodes);
    this.doc.notify();
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  click() {
    const event = {target: this, isTrusted: false, preventDefault() {}};
    if (typeof this.onclick === 'function') this.onclick(event);
    for (const fn of this.listeners.click || []) fn(event);
  }
}

function parseHTML(html, doc) {
  const roots = [];
  const stack = [{children: roots}];
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w:-]*)([^>]*?)(\/?)>|([^<]+)/g;
  let match;
  while ((match = re.exec(html))) {
    if (match[0].startsWith('<!--')) continue;
    if (match[5] !== undefined) {
      if (match[5]) stack.at(-1).children.push({text: match[5], textContent: match[5]});
      continue;
    }
    const closing = match[1] === '/';
    const tag = match[2].toLowerCase();
    if (closing) {
      while (stack.length > 1 && stack.at(-1).tagName !== tag.toUpperCase()) stack.pop();
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new MiniElement(tag, doc);
    el.attrs = parseAttrs(match[3] || '');
    if (el.attrs.checked !== undefined) el.checked = true;
    el.parent = stack.at(-1).tagName ? stack.at(-1) : null;
    stack.at(-1).children.push(el);
    const voidTag = match[4] === '/' || ['img', 'br', 'input', 'hr', 'meta', 'link'].includes(tag);
    if (!voidTag) stack.push(el);
  }
  return roots;
}

function boot(payloads) {
  const doc = {
    observers: [],
    notify() {
      if (this.scheduled) return;
      this.scheduled = true;
      queueMicrotask(() => {
        this.scheduled = false;
        for (const observer of this.observers) observer();
      });
    },
  };
  const root = new MiniElement('div', doc);
  root.attrs.id = 'app';
  const documentElement = new MiniElement('html', doc);
  const body = new MiniElement('body', doc);
  const document = {
    documentElement,
    body,
    fullscreenElement: null,
    getElementById: id => (root.id === id ? root : root.querySelector('#' + id)),
    addEventListener() {},
  };
  const fetchCalls = [];
  const context = {
    window: {scrollY: 0},
    document,
    localStorage: {getItem: () => null, setItem() {}},
    location: {search: '', href: ''},
    MutationObserver: class {
      constructor(callback) { this.callback = callback; doc.observers.push(() => callback([])); }
      observe() {}
      disconnect() {}
    },
    fetch: async input => {
      const url = String(input);
      fetchCalls.push(url);
      const body = url.startsWith('/api/me') ? {user_id: 42}
        : url.startsWith('/api/library') ? {items: [], new_episodes: []}
        : url.startsWith('/api/recommendations/history') ? payloads.history
        : url.startsWith('/api/recommendations') ? payloads.current
        : null;
      if (!body) return {ok: false, status: 404, json: async () => ({})};
      return {ok: true, status: 200, json: async () => body};
    },
    setTimeout,
    clearTimeout,
    queueMicrotask,
    URLSearchParams,
    console,
    root,
    fetchCalls,
  };
  context.window = context;
  context.scrollY = 0;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return {root, fetchCalls, context};
}

const emptySources = {
  openai: {id: 'openai', label: 'ChatGPT', configured: true, daily_limit: 5, daily_used: 0, job: null, pick: null},
  xai: {id: 'xai', label: 'Grok', configured: true, daily_limit: 5, daily_used: 0, job: null, pick: null},
};

function recommendationBody(overrides = {}) {
  return {
    shikimori_connected: true,
    shikimori_configured: true,
    continuations: [],
    continuations_refreshing: false,
    sources: {
      openai: {...emptySources.openai, ...overrides},
      xai: emptySources.xai,
    },
  };
}

async function flush() {
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
}

async function openRecommendations(root) {
  const nav = root.querySelector('[data-nav="more"]');
  if (!nav) throw new Error('missing nav\n' + root.innerHTML.slice(0, 2000));
  nav.click();
  await flush();
  root.querySelector('[data-nav="recommendations"]').click();
  await flush();
}

test('recommendations view renders every owner-visible state without a TDZ error', async () => {
  const payloads = {
    current: recommendationBody(),
    history: {items: []},
  };
  const {root} = boot(payloads);
  await flush();
  assert.doesNotMatch(root.innerHTML, /before initialization/);

  payloads.current = {
    shikimori_connected: false,
    shikimori_configured: true,
    continuations: [],
    sources: emptySources,
  };
  await openRecommendations(root);
  assert.doesNotMatch(root.innerHTML, /before initialization/);
  assert.match(root.innerHTML, /Подключить Shikimori/);
  assert.doesNotMatch(root.innerHTML, /Подобрать аниме/);

  payloads.current = recommendationBody();
  await openRecommendations(root);
  assert.match(root.innerHTML, /Подобрать аниме/);
  assert.match(root.innerHTML, /Нажмите «Подобрать аниме»/);

  payloads.current = recommendationBody({
    job: {id: 1, state: 'running', error_message: null},
    pick: {id: 7, created_at: 1, pair_rating: null, pair_skipped: false, items: [
      {shikimori_anime_id: '10', anime365_series_id: 55, title: 'Прошлая драма', year: 2016, kind: 'tv', reason: 'Похоже на любимое.', interested: false},
    ]},
  });
  await openRecommendations(root);
  assert.match(root.innerHTML, /Обновить статус/);
  assert.match(root.innerHTML, /Подбираем аниме/);
  assert.match(root.innerHTML, /Прошлая драма/);

  payloads.current = recommendationBody({
    job: {id: 2, state: 'ready', error_message: null},
    pick: {id: 8, created_at: 1, pair_rating: null, pair_skipped: false, items: [
      {shikimori_anime_id: '11', anime365_series_id: 56, title: 'Готовая пара', year: 2020, kind: 'tv', reason: 'Как и «Адский рай», который ты оценил на 9, эта история держит тот же тон без спойлеров и досматривается до конца.', interested: false},
    ]},
  });
  await openRecommendations(root);
  assert.match(root.innerHTML, /Готовая пара/);
  assert.match(root.innerHTML, /досматривается до конца/);
  assert.match(root.innerHTML, /Подобрать аниме/);
  const toggle = root.querySelector('[data-reason-toggle]');
  assert.equal(toggle.textContent, 'Подробнее');
  toggle.click();
  assert.match(root.innerHTML, /recommendation-reason expanded/);
  assert.equal(toggle.textContent, 'Свернуть');

  payloads.current = recommendationBody({
    job: {id: 3, state: 'failed', error_message: 'У ChatGPT закончился лимит или баланс. Попробуйте позже.'},
    pick: {id: 8, created_at: 1, pair_rating: null, pair_skipped: false, items: [
      {shikimori_anime_id: '11', anime365_series_id: 56, title: 'Готовая пара', year: 2020, kind: 'tv', reason: 'Как и «Адский рай», который ты оценил на 9, эта история держит тот же тон без спойлеров и досматривается до конца.', interested: false},
    ]},
  });
  await openRecommendations(root);
  assert.match(root.innerHTML, /закончился лимит/);
  assert.match(root.innerHTML, /Готовая пара/);

  payloads.history = {items: [{
    id: 8, source: 'openai', created_at: 1_700_000_000, pair_rating: 2, pair_skipped: false,
    items: [{shikimori_anime_id: '11', anime365_series_id: 56, title: 'Готовая пара', year: 2020, kind: 'tv', reason: 'Как и «Адский рай», который ты оценил на 9, эта история держит тот же тон без спойлеров и досматривается до конца.', interested: true}],
  }]};
  root.querySelector('#recommendation-history').click();
  await flush();
  assert.match(root.innerHTML, /История подборок/);
  assert.match(root.innerHTML, /Готовая пара/);
  assert.match(root.innerHTML, /досматривается до конца/);
  assert.match(root.innerHTML, /Оценка пары: 2/);
  const historyToggle = root.querySelector('[data-reason-toggle]');
  historyToggle.click();
  assert.match(root.innerHTML, /recommendation-reason expanded/);
  assert.doesNotMatch(root.innerHTML, /before initialization/);
});
