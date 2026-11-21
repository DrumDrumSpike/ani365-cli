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
  assert.match(context.root.innerHTML, /Пока нет известных обновлений/);
  libraryButton.onclick({type: 'click'});
  assert.deepEqual(calls, [[]]);
});

test('home shows released episodes above the queue and opens the updates library', async () => {
  const calls = [];
  const updatesButton = {};
  const context = extract('  async function home()', '  async function openNewEpisode(', {
    state: {library: []}, telegram: null, root: {innerHTML: '', querySelector: () => null, querySelectorAll: () => []},
    document: {getElementById: id => id === 'library-updates' ? updatesButton : null},
    api: async () => ({items: [], new_episodes: [12, 14, 16, 18, 20].map(series_id => ({series_id, title: 'Тайтл', last_available_episode_number: '7 серия', last_available_episode_id: 701}))}), cards: () => '', library: (...args) => calls.push(args),
    esc: value => String(value), fail: error => { throw error; },
  });
  await context.home();
  assert.equal((context.root.innerHTML.match(/data-new-series=/g) || []).length, 3);
  assert.match(context.root.innerHTML, /data-new-episode="701".*Тайтл.*7 серия/);
  assert.ok(context.root.innerHTML.indexOf('Новые серии') < context.root.innerHTML.indexOf('Смотрю сейчас'));
  updatesButton.onclick({type: 'click'});
  assert.deepEqual(calls, [[true]]);
});

test('home cards expose full titles, real progress, new episode state and poster skeletons', () => {
  const context = extract('  function episodeNumberValue(', '  async function openHomeSeries(', {esc: value => String(value)});
  const html = context.homeCards([{series_id: 4, poster_url: '/poster/4', title: 'A very long anime title that must remain readable', last_watched_episode_number: '4 серия', last_available_episode_number: '8 серия'}], [{series_id: 4}]);
  assert.match(html, /home-rail/);
  assert.match(html, /A very long anime title that must remain readable/);
  assert.match(html, /aria-valuenow="50"/);
  assert.match(html, /4 из 8/);
  assert.match(html, /Новая серия/);
  assert.match(html, /poster-skeleton/);
  assert.match(html, /data-home-watch="4"/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  const trailer = context.homeCards([{series_id: 5, title: 'Title', last_watched_episode_number: 'Трейлер'}], []);
  assert.doesNotMatch(trailer, /progressbar|home-card-meta|poster-skeleton|\?/);
});

test('home watch CTA resumes saved playback or selects the next real unwatched episode', async () => {
  const calls = [];
  const state = {selected: null};
  let withPlayback = true, resumeWatched = false;
  const context = extract('  async function openHomeSeries(', '  function bindHomePullToRefresh(', {
    state, details: async id => { state.selected = {item: {series_id: id}, playback: withPlayback ? {episode_id: 201} : null, episodes: [{id: 201, watched: resumeWatched}, {id: 202, watched: false}]}; return true; },
    resumeDirect: async id => calls.push(['resume', id]), selectEpisode: async id => calls.push(['select', id]),
  });
  await context.openHomeSeries(10);
  assert.deepEqual(calls, [['resume', 201]]);
  resumeWatched = true;
  await context.openHomeSeries(10);
  assert.deepEqual(calls, [['resume', 201], ['select', 202]]);
  withPlayback = false;
  await context.openHomeSeries(10);
  assert.deepEqual(calls, [['resume', 201], ['select', 202], ['select', 202]]);
});

test('pull to refresh reloads home after a downward gesture at scroll top', async () => {
  const listeners = {};
  let refreshed = 0;
  const indicator = {hidden: true, textContent: ''};
  const context = extract('  function bindHomePullToRefresh(', '  async function openNewEpisode(', {
    root: {addEventListener: (name, fn) => listeners[name] = fn, removeEventListener() {}, querySelector: () => indicator},
    state: {view: 'home'}, window: {scrollY: 0}, home: async () => { refreshed++; },
  });
  context.bindHomePullToRefresh();
  listeners.touchstart({touches: [{clientY: 5, clientX: 8}]});
  listeners.touchmove({touches: [{clientY: 100, clientX: 10}]});
  assert.match(indicator.textContent, /Отпустите/);
  await listeners.touchend({changedTouches: [{clientY: 100}]});
  assert.equal(refreshed, 1);
});


test('horizontal rail drag does not trigger pull-to-refresh', async () => {
  const listeners = {};
  let refreshed = 0;
  const indicator = {hidden: true, textContent: ''};
  const context = extract('  function bindHomePullToRefresh(', '  async function openNewEpisode(', {
    root: {addEventListener: (name, fn) => listeners[name] = fn, removeEventListener() {}, querySelector: () => indicator},
    state: {view: 'home'}, window: {scrollY: 0}, home: async () => { refreshed++; },
  });
  context.bindHomePullToRefresh();
  listeners.touchstart({touches: [{clientY: 10, clientX: 100}]});
  listeners.touchmove({touches: [{clientY: 20, clientX: 170}]});
  await listeners.touchend({changedTouches: [{clientY: 110, clientX: 170}]});
  assert.equal(refreshed, 0);
});
