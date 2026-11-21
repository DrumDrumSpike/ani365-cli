const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const app = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
const navigation = app.slice(app.indexOf('  const navItems ='), app.indexOf('  function updateAppFullscreen'));
function nativeButton() {
  const handlers = new Set();
  return {
    visible: false, handlers,
    onClick(fn) { handlers.add(fn); },
    offClick(fn) { handlers.delete(fn); },
    setText() {}, show() { this.visible = true; }, hide() { this.visible = false; },
    click() { for (const fn of handlers) fn(); },
  };
}

function setup() {
  const nodes = new Map();
  const storage = new Map();
  const state = {view: 'home'};
  const root = {innerHTML: '', querySelector: key => nodes.get(key), addEventListener() {}};
  const main = nativeButton(), back = nativeButton();
  const context = {
    root, state, home() {}, initData: 'signed',
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    document: {documentElement: {classList: {toggle() {}}, dataset: {}}},
    telegram: {MainButton: main, BackButton: back, isVersionAtLeast: () => true},
  };
  vm.createContext(context);
  vm.runInContext(navigation, context);
  return {context, state, root, nodes, main, back};
}

test('more has an account-scoped adult catalog opt-in and five primary tabs', () => {
  const {context, nodes, root} = setup();
  nodes.set('.back', {});
  assert.deepEqual(Array.from(vm.runInContext('navItems.map(item => item[0])', context)),
    ['home', 'search', 'library', 'downloads', 'more']);
  context.loadAdultPreference(21);
  context.more();
  assert.doesNotMatch(root.innerHTML, /data-nav="hentai"/);
  context.setAdultPreference(true);
  context.more();
  assert.match(root.innerHTML, /data-nav="hentai"/);
  context.loadAdultPreference(22);
  context.more();
  assert.doesNotMatch(root.innerHTML, /data-nav="hentai"/);
  context.loadAdultPreference(21);
  context.more();
  assert.match(root.innerHTML, /data-nav="hentai"/);
});

test('home resume and contextual back use distinct native callbacks and release them for the player', () => {
  const {context, state, nodes, main, back} = setup();
  let resumed = 0, returned = 0;
  nodes.set('#continue-watching', {click: () => resumed++});
  context.syncNavigationButtons();
  context.syncNavigationButtons();
  main.click();
  assert.ok(main.visible);
  assert.equal(resumed, 1);
  state.view = 'library';
  nodes.set('.back', {click: () => returned++});
  context.syncNavigationButtons();
  assert.equal(main.visible, false);
  assert.ok(back.visible);
  back.click();
  assert.equal(returned, 1);
  context.releaseNavigationButtons();
  assert.equal(back.handlers.size, 0);
  assert.equal(main.handlers.size, 0);
});
