const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
function setup({native = true, telegram = null} = {}) {
  const classes = new Set(), listeners = new Map(), timers = new Map();
  let nextTimer = 0, focused = false, hovered = false;
  const classList = {
    contains: key => classes.has(key), add: key => classes.add(key),
    remove: (...keys) => keys.forEach(key => classes.delete(key)),
    toggle: (key, enabled) => enabled ? classes.add(key) : classes.delete(key),
  };
  const target = name => ({
    addEventListener: (event, fn) => listeners.set(name + event, fn),
    removeEventListener: event => listeners.delete(name + event),
  });
  const exitButton = {matches: () => focused || hovered};
  const surface = {...target('surface'), classList,
    matches: () => focused, querySelector: () => exitButton,
    requestFullscreen: async () => {
      if (!native) throw Error('denied');
      context.document.fullscreenElement = surface;
      listeners.get('docfullscreenchange')();
    },
  };
  const video = {closest: () => surface};
  const button = {};
  const context = {
    document: {...target('doc'), body: {classList},
      exitFullscreen: async () => {
        context.document.fullscreenElement = null;
        listeners.get('docfullscreenchange')?.();
      },
    }, telegram,
    setTimeout: fn => {timers.set(++nextTimer, fn); return nextTimer;},
    clearTimeout: id => timers.delete(id),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  async function requestVideoFullscreen('),
    source.indexOf('  function loadPlayerSubtitles(')), context);
  const cleanup = context.watchPlayerFullscreen(video, button);
  return {context, classes, video, surface, button, exitButton, listeners, timers, cleanup,
    focus: value => focused = value, hover: value => hovered = value,
    toggle: () => context.playerFullscreen(video),
    idle: () => {const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());},
  };
}
test('native fullscreen includes ASS surface and toggles repeatedly without Telegram request', async () => {
  let requested = 0;
  const h = setup({telegram: {requestFullscreen: () => requested++}});
  for (let i = 0; i < 3; i++) {
    await h.toggle();
    assert.equal(h.context.document.fullscreenElement, h.surface);
    assert.ok(h.classes.has('player-expanded'));
    await h.toggle();
    assert.equal(h.context.document.fullscreenElement, null);
    assert.ok(!h.classes.has('player-expanded'));
  }
  assert.equal(requested, 0);
  h.cleanup();
});
test('denied native fullscreen uses cinema fallback and restores Telegram BackButton', async () => {
  let requested = 0, exited = 0;
  const back = {isVisible: true, hide() {this.isVisible = false;}, show() {this.isVisible = true;}};
  const telegram = {BackButton: back, requestFullscreen() {requested++; this.isFullscreen = true;},
    exitFullscreen() {exited++; this.isFullscreen = false;}};
  const h = setup({native: false, telegram});
  await h.toggle();
  assert.equal(requested, 1);
  assert.equal(back.isVisible, false);
  assert.ok(h.classes.has('player-expanded'));
  h.listeners.get('dockeydown')({key: 'Escape'});
  assert.equal(exited, 1);
  assert.equal(back.isVisible, true);
  assert.ok(!h.classes.has('player-expanded'));
  h.cleanup();
});
test('external native exit restores interface; idle button respects interaction; cleanup removes listeners and timers', async () => {
  const h = setup();
  await h.toggle();
  h.idle();
  assert.ok(h.classes.has('controls-idle'));
  h.listeners.get('surfacepointermove')();
  assert.ok(!h.classes.has('controls-idle'));
  h.focus(true); h.idle();
  assert.ok(!h.classes.has('controls-idle'));
  h.focus(false); h.listeners.get('surfacefocusout')(); h.idle();
  assert.ok(h.classes.has('controls-idle'));
  await h.context.document.exitFullscreen();
  assert.ok(!h.classes.has('player-expanded'));
  await h.toggle();
  h.cleanup();
  assert.equal(h.listeners.size, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.video.togglePlayerFullscreen, undefined);
  assert.ok(!h.classes.has('player-expanded'));
});
test('existing Telegram fullscreen is preserved on cinema exit', async () => {
  let exited = 0;
  const h = setup({native: false, telegram: {isFullscreen: true, exitFullscreen: () => exited++}});
  await h.toggle(); await h.toggle();
  assert.equal(exited, 0);
  h.cleanup();
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

test('late native fullscreen completion after player close exits and does not revive cinema', async () => {
  const h = setup();
  let complete;
  h.surface.requestFullscreen = () => new Promise(resolve => complete = () => {
    h.context.document.fullscreenElement = h.surface;
    resolve();
  });
  const entering = h.toggle();
  h.cleanup();
  complete();
  await entering;
  assert.equal(h.context.document.fullscreenElement, null);
  assert.ok(!h.classes.has('player-expanded'));
  assert.equal(h.timers.size, 0);
});

test('Telegram fullscreen failure keeps fallback; external exit and cleanup restore state', async () => {
  const callbacks = new Map();
  const telegram = {
    requestFullscreen() {}, isVersionAtLeast: () => true,
    onEvent: (name, fn) => callbacks.set(name, fn),
    offEvent: name => callbacks.delete(name),
  };
  const h = setup({native: false, telegram});
  await h.toggle();
  callbacks.get('fullscreenFailed')({error: 'UNSUPPORTED'});
  assert.ok(h.classes.has('player-expanded'));
  telegram.isFullscreen = true;
  callbacks.get('fullscreenChanged')();
  telegram.isFullscreen = false;
  callbacks.get('fullscreenChanged')();
  assert.ok(!h.classes.has('player-expanded'));
  h.cleanup();
  assert.equal(callbacks.size, 0);
});

test('libass repeated resize preserves paused subtitle pixels when geometry is unchanged', () => {
  const rendererSource = fs.readFileSync('ani365_bot/web_static/subtitles-octopus.js', 'utf8');
  let resets = 0, messages = 0, width = 0, height = 0;
  const canvas = {style: {},
    get width() {return width;}, set width(value) {width = value; resets++;},
    get height() {return height;}, set height(value) {height = value; resets++;},
  };
  const renderer = {video: {getBoundingClientRect: () => ({top: 0})}, canvas,
    canvasParent: {style: {}, getBoundingClientRect: () => ({top: 800})},
    getVideoPosition: () => ({width: 1280, height: 720, x: 0, y: 40}),
    pixelRatio: 1, worker: {postMessage: () => messages++},
  };
  const context = {self: renderer, _computeCanvasSize: (width, height) => ({width, height})};
  vm.createContext(context);
  vm.runInContext(rendererSource.slice(rendererSource.indexOf('    self.resize = function ('),
    rendererSource.indexOf('    self.resizeWithTimeout = function (')), context);
  renderer.resize();
  assert.equal(resets, 2);
  assert.equal(canvas.style.top, '-760px');
  renderer.resize();
  assert.equal(resets, 2, 'unchanged resize must not clear canvas');
  assert.equal(messages, 1);
  renderer.getVideoPosition = () => ({width: 1000, height: 562, x: 0, y: 169});
  renderer.resize();
  assert.equal(resets, 4);
  assert.equal(messages, 2);
});

function episodePlayer({last = false, missingTranslation = false, missingQuality = false} = {}) {
  const events = {}, calls = [], nodes = {};
  const video = {duration: 100, currentTime: 25, pause() {events.pause?.();},
    play: async () => {}, addEventListener(name, fn) {events[name] = fn;}};
  for (const key of ['#fullscreen', '#fallback', '#complete-episode', '#player-progress-status', '.back']) nodes[key] = {};
  const episodes = [{id: 10, number: '1'}, {id: 20, number: '2'}];
  const groups = id => [{kind: 'sub', language: 'ru', items: [{id, authorsSummary: 'Studio'}]}];
  const state = {selected: {item: {series_id: 1, title: 'Title'}, episodes: last ? episodes.slice(0, 1) : episodes},
    episode: episodes[0], translation: 100};
  const context = {state, Date, Number, JSON, String, Promise, esc: String,
    root: {querySelector: key => key === 'video' ? video : nodes[key]},
    api: async (path, options) => {
      calls.push([path, options?.body && JSON.parse(options.body)]);
      if (path === '/api/play') return {media_url: 'video', proxy_url: 'proxy'};
      if (path.includes('/10/translations')) return {groups: groups(100)};
      if (path.includes('/20/translations')) return {groups: missingTranslation ? [] : groups(200)};
      if (path.includes('/qualities')) return {items: missingQuality ? [480] : [720]};
      return {};
    },
    watchPlayerFullscreen: () => () => {}, loadPlayerSubtitles: () => () => {},
    isHlsSource: () => false, setVideoSource: async () => {}, destroyHls() {},
    playerFullscreen() {}, fail(error) {throw error;}, details() {},
    selectEpisode: async id => calls.push(['select', id]),
    qualities: async id => calls.push(['qualities', id]),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function matchingPlayerTranslation('), source.indexOf("  api('/api/me')")), context);
  return {context, state, calls, events, nodes};
}

test('next episode completes before play, preserves translation and quality, ignores stale video events', async () => {
  const h = episodePlayer();
  await h.context.player(720);
  h.events.timeupdate();
  const oldTimeupdate = h.events.timeupdate;
  await h.nodes['#complete-episode'].onclick();
  const plays = h.calls.filter(([path]) => path === '/api/play');
  assert.equal(plays.length, 2);
  assert.equal(plays[1][1].episode_id, 20);
  assert.equal(plays[1][1].translation_id, 200);
  assert.equal(plays[1][1].quality, 720);
  assert.ok(h.calls.findIndex(([path]) => path.endsWith('/watched')) < h.calls.lastIndexOf(plays[1]));
  const count = h.calls.length;
  oldTimeupdate();
  await Promise.resolve();
  assert.equal(h.calls.length, count);
  assert.equal(h.calls.find(([path]) => path === '/api/progress')[1].episode_id, 10);
});

test('last episode is marked without opening another player', async () => {
  const h = episodePlayer({last: true});
  await h.context.player(720);
  await h.nodes['#complete-episode'].onclick();
  assert.equal(h.nodes['#complete-episode'].textContent, '✓ Просмотрено');
  assert.equal(h.calls.filter(([path]) => path === '/api/play').length, 1);
});

for (const [option, destination, id] of [['missingTranslation', 'select', 20], ['missingQuality', 'qualities', 200]]) {
  test(`${option} opens selection`, async () => {
    const h = episodePlayer({[option]: true});
    await h.context.player(720);
    await h.nodes['#complete-episode'].onclick();
    assert.ok(h.calls.some(([path, value]) => path === destination && value === id));
  });
}

test('failed completion keeps current episode and allows retry', async () => {
  const h = episodePlayer();
  const api = h.context.api;
  h.context.api = async (path, options) => {
    if (path.endsWith('/watched')) throw Error('Сеть недоступна');
    return api(path, options);
  };
  await h.context.player(720);
  await h.nodes['#complete-episode'].onclick();
  assert.equal(h.state.episode.id, 10);
  assert.equal(h.nodes['#complete-episode'].disabled, false);
  assert.equal(h.nodes['#player-progress-status'].textContent, 'Сеть недоступна');
  h.context.api = api;
  await h.nodes['#complete-episode'].onclick();
  assert.equal(h.state.episode.id, 20);
});
