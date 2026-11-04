const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
function setup(overrides = {}) {
  const classes = new Set();
  const context = {
    document: {body: {classList: {
      contains: key => classes.has(key), add: key => classes.add(key), remove: key => classes.delete(key),
    }}},
    telegram: null, ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  async function requestVideoFullscreen('),
    source.indexOf('  function watchPlayerFullscreen(')), context);
  return {context, classes};
}
test('native video fullscreen does not also request Telegram fullscreen', async () => {
  let native = 0, telegram = 0;
  const {context, classes} = setup({telegram: {requestFullscreen: () => telegram++}});
  await context.playerFullscreen({requestFullscreen: async () => native++}, {});
  assert.equal(native, 1);
  assert.equal(telegram, 0);
  assert.equal(classes.size, 0);
});
test('rejected native fullscreen falls back to full window and can exit', async () => {
  let requested = 0, exited = 0;
  const telegram = {requestFullscreen: () => requested++, exitFullscreen: () => exited++};
  const {context, classes} = setup({telegram});
  const video = {requestFullscreen: async () => {throw Error('denied');}};
  const button = {};
  await context.playerFullscreen(video, button);
  assert.equal(requested, 1);
  assert.ok(classes.has('player-expanded'));
  telegram.isFullscreen = true;
  await context.playerFullscreen(video, button);
  assert.equal(exited, 1);
  assert.equal(classes.size, 0);
});
test('subtitles use authenticated fetch, enable native track, and release blob', async () => {
  let request, revoked, appended;
  const track = {track: {}, addEventListener() {}, remove() {}};
  const context = {
    document: {createElement: () => track}, initData: 'signed-init-data', AbortController, Blob,
    URL: {createObjectURL: () => 'blob:captions', revokeObjectURL: url => revoked = url},
    fetch: async (url, options) => {
      request = {url, options};
      return {ok: true, text: async () => 'WEBVTT\n\n00:00.000 --> 00:01.000\nПривет'};
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function loadPlayerSubtitles('),
    source.indexOf('  async function player(quality)')), context);
  const cleanup = context.loadPlayerSubtitles({appendChild: value => appended = value}, '/api/subtitles/ticket', {});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(request.options.headers['X-Telegram-Init-Data'], 'signed-init-data');
  assert.equal(appended.src, 'blob:captions');
  assert.equal(track.track.mode, 'showing');
  cleanup();
  assert.equal(revoked, 'blob:captions');
  assert.ok(request.options.signal.aborted);
});

test('ASS is passed intact to libass without creating a native text track', async () => {
  const body = fs.readFileSync('tests/fixtures/rendering.ass', 'utf8');
  let options, disposed = false, freed = false, restored;
  const button = {remove() {}};
  const status = {after() {}};
  const context = {
    document: {createElement: tag => tag === 'button' ? button : {addEventListener() {}, remove() {}}},
    initData: '', AbortController,
    SubtitlesOctopus: function(value) {
      options = value; this.worker = {}; this.dispose = () => disposed = true;
      this.freeTrack = () => freed = true; this.setTrack = value => restored = value;
    },
    fetch: async () => ({ok: true, text: async () => body}),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function loadPlayerSubtitles('), source.indexOf('  async function player(quality)')), context);
  const video = {setAttribute() {}};
  const cleanup = context.loadPlayerSubtitles(video, '/subtitle', status);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(options.subContent, body);
  assert.equal(options.video, video);
  assert.equal(options.workerUrl, '/assets/subtitles-octopus-worker.js');
  button.onclick(); assert.ok(freed);
  button.onclick(); assert.equal(restored, body);
  cleanup(); assert.ok(disposed);
});

test('fullscreen requests the container that includes the ASS overlay', async () => {
  let fullscreen = false;
  const container = {requestFullscreen: async () => fullscreen = true};
  const {context} = setup();
  await context.playerFullscreen({closest: () => container}, {});
  assert.ok(fullscreen);
});

test('ASS font loading respects Format, inline overrides and all available faces', async () => {
  const body = '[Script Info]\n[V4+ Styles]\nFormat: Fontsize, Name, Fontname\nStyle: 22,Main,@Tahoma\n[Events]\nDialogue: 0,0:00:01,0:00:02,Main,,0,0,0,,{\\fnImpact\\b1}Title{\\fnGeorgia}Other';
  let options;
  const context = {
    document: {createElement: () => ({addEventListener() {}, remove() {}})},
    initData: '', AbortController,
    SubtitlesOctopus: function(value) {options = value;},
    fetch: async () => ({ok: true, text: async () => body}),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function loadPlayerSubtitles('), source.indexOf('  async function player(quality)')), context);
  context.loadPlayerSubtitles({setAttribute() {}}, '/subtitle', {after() {}}, {
    tahoma: ['/tahoma.ttf','/tahomabd.ttf'], impact: ['/impact.ttf'],
    georgia: ['/georgia.ttf','/georgiab.ttf'], unused: ['/unused.ttf'],
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(options.subContent, body);
  assert.deepEqual(Array.from(options.fonts), ['/tahoma.ttf','/tahomabd.ttf','/impact.ttf','/georgia.ttf','/georgiab.ttf']);
});
