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
