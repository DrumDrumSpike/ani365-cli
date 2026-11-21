const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
function extract(start, end, context) {
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);
  return context;
}

test('opening an update selects its concrete episode after details load', async () => {
  const calls = [];
  const context = extract('  async function openNewEpisode(', '  function cards(', {
    state: {selected: null},
    details: async id => { calls.push(['details', id]); context.state.selected = {item: {series_id: id}, episodes: [{id: 405}]}; return true; },
    selectEpisode: async id => calls.push(['episode', id]),
  });
  await context.openNewEpisode(12, 405);
  assert.deepEqual(calls, [['details', 12], ['episode', 405]]);
});

test('failed details load never reads stale selection or opens its episode', async () => {
  let selected = 0;
  const context = extract('  async function openNewEpisode(', '  function cards(', {
    state: {selected: null}, details: async () => false,
    selectEpisode: async () => { selected++; },
  });
  await context.openNewEpisode(12, 405);
  assert.equal(selected, 0);
});

test('updates filter regular and hentai groups by IDs and show filtered heading/count', async () => {
  const buttons = [];
  const groups = {watching: [{series_id: 12, title: 'Updated'}, {series_id: 13, title: 'Unrelated'}]};
  const hentai = [{series_id: 900012, title: 'Other hentai'}];
  const root = {
    innerHTML: '',
    querySelector(selector) {
      if (selector === '.back') return {onclick: null};
      if (selector === '#library-search') return {value: '', addEventListener() {}};
      if (selector === '#library-groups') return {set innerHTML(value) { root.groupsHtml = value; }};
      return null;
    },
    querySelectorAll() { return buttons; },
  };
  const context = extract('  function formatTitleCount(', '  function recommendationCards(', {
    state: {}, root, home() {}, useBack() {}, api: async () => ({items: [...groups.watching, ...hentai], groups, hentai_items: hentai, new_episodes: [{series_id: 13}, {series_id: 13}]}),
    cards: items => `<cards>${items.map(item => item.title).join('|')}</cards>`, esc: value => String(value), fail: error => { throw error; },
  });
  await context.library(true);
  assert.match(root.innerHTML, /<h1>Обновления<\/h1>/);
  assert.match(root.innerHTML, /1 тайтл/);
  assert.match(root.groupsHtml, /Unrelated/);
  assert.doesNotMatch(root.groupsHtml, /Updated/);
  assert.doesNotMatch(root.groupsHtml, /Other hentai/);
});

test('ordinary home-library click does not pass its MouseEvent as a filter', async () => {
  const calls = [];
  const libraryButton = {};
  const context = extract('  async function home()', '  async function openNewEpisode(', {
    state: {library: []}, telegram: null, root: {innerHTML: '', querySelector: () => null, querySelectorAll: () => []},
    document: {getElementById: id => id === 'library' ? libraryButton : null},
    api: async () => ({items: [], new_episodes: []}), cards: () => '', library: (...args) => calls.push(args),
    esc: value => String(value), fail: error => { throw error; },
  });
  await context.home();
  libraryButton.onclick({type: 'click'});
  assert.deepEqual(calls, [[]]);
});

test('home updates link passes only updated series IDs to library', async () => {
  const calls = [];
  const updatesButton = {};
  const context = extract('  async function home()', '  async function openNewEpisode(', {
    state: {library: []}, telegram: null, root: {innerHTML: '', querySelector: () => null, querySelectorAll: () => []},
    document: {getElementById: id => id === 'library-updates' ? updatesButton : null},
    api: async () => ({items: [], new_episodes: [12, 14, 16, 18, 20].map(series_id => ({series_id}))}), cards: () => '', library: (...args) => calls.push(args),
    esc: value => String(value), fail: error => { throw error; },
  });
  await context.home();
  assert.equal((context.root.innerHTML.match(/data-new-series=/g) || []).length, 3);
  updatesButton.onclick({type: 'click'});
  assert.deepEqual(calls, [[true]]);
});
