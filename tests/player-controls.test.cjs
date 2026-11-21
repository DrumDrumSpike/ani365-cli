const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
const helpers = source.slice(source.indexOf('  function playerExtrasMarkup('), source.indexOf('  async function requestVideoFullscreen('));

test('player controls seek within the episode, change speed and navigate by episode ID', () => {
  const nodes = {'.video-skip': {}, '.video-speed select': {}, '.caption-size select': {}};
  const list = {querySelector: () => null};
  const context = {root: {querySelector: () => list}, esc: String};
  vm.runInNewContext(helpers, context);
  let tap, switched;
  const video = {currentTime: 95, duration: 100, dataset: {},
    closest: () => ({querySelector: selector => nodes[selector]}),
    getBoundingClientRect: () => ({left: 0, width: 320, bottom: 200}),
    addEventListener: (_, fn) => {tap = fn;}, removeEventListener: () => {tap = null;},
  };
  const episodes = [{id: 4, number: '1', watched: true}, {id: 8, number: '2'}];
  const cleanup = context.bindPlayerExtras(video, episodes, episodes[1], id => {switched = id;});
  nodes['.video-skip'].onclick();
  assert.equal(video.currentTime, 100);
  for (const t of [1000, 1250]) tap({pointerType: 'touch', isPrimary: true, timeStamp: t, clientX: 24, clientY: 60, preventDefault() {}});
  assert.equal(video.currentTime, 90);
  nodes['.video-speed select'].onchange({target: {value: '1.5'}});
  assert.equal(video.playbackRate, 1.5);
  list.onclick({target: {closest: () => ({dataset: {playerEpisode: '4'}})}});
  assert.equal(switched, 4);
  assert.match(context.playerEpisodeList(episodes, episodes[1]), /aria-current="true" disabled/);
  cleanup();
  assert.equal(tap, null);
});
