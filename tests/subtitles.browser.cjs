// Run with Playwright installed: node --test tests/subtitles.browser.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');

test('real libass renders vectors, layers and movement; follows seeks, size and fullscreen', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ani365-render-'));
  execFileSync('ffmpeg', ['-v','error','-f','lavfi','-i','color=black:s=640x360:d=8',
    '-c:v','libvpx','-an',path.join(dir, 'video.webm')]);
  const source = fs.readFileSync('ani365_bot/web_static/app.js', 'utf8');
  const functions = source.slice(source.indexOf('  function loadPlayerSubtitles('), source.indexOf('  async function player(quality)'));
  const fullscreen = source.slice(source.indexOf('  async function requestVideoFullscreen('), source.indexOf('  function loadPlayerSubtitles('));
  const fixture = fs.readFileSync('tests/fixtures/rendering.ass');
  const server = http.createServer((req, res) => {
    if (req.url === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(`<link rel="stylesheet" href="/assets/app.css"><style>body{margin:0}.player-surface{width:640px}</style>
        <div class="player-page"><div class="player-surface"><video controls src="/video.webm"></video><button class="player-exit">Выйти</button></div></div><p id="status"></p>
        <button id="full">Fullscreen</button><script src="/assets/subtitles-octopus.js"></script>
        <script>const initData=''; const telegram=null; ${functions} ${fullscreen}
        window.cleanup=loadPlayerSubtitles(document.querySelector('video'),'/subtitle',document.querySelector('#status'));
        watchPlayerFullscreen(document.querySelector('video'),document.querySelector('#full'));
        document.querySelector('#full').onclick=()=>playerFullscreen(document.querySelector('video'),document.querySelector('#full'));</script>`);
    } else if (req.url === '/subtitle') { res.end(fixture); }
    else {
      const file = req.url === '/video.webm' ? path.join(dir, 'video.webm') : path.join('ani365_bot/web_static', path.basename(req.url));
      if (!fs.existsSync(file)) {res.writeHead(404); res.end(); return;}
      const types = {'.js':'text/javascript','.wasm':'application/wasm','.css':'text/css','.webm':'video/webm'};
      res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
      const data = fs.readFileSync(file);
      res.setHeader('Accept-Ranges', 'bytes');
      if (req.headers.range) {
        const [, start, end] = /bytes=(\d+)-(\d*)/.exec(req.headers.range);
        const from = Number(start), to = end ? Number(end) : data.length-1;
        res.writeHead(206, {'Content-Range': `bytes ${from}-${to}/${data.length}`, 'Content-Length':to-from+1});
        res.end(data.subarray(from,to+1));
      } else res.end(data);
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true});
    const page = await browser.newPage({viewport:{width:1000,height:800}});
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('Русские субтитры'));
    await page.waitForFunction(() => document.querySelector('video').readyState >= 2);
    const seek = async time => {
      await page.evaluate(time => {document.querySelector('video').currentTime=time;}, time);
      await page.waitForFunction(time => Math.abs(document.querySelector('video').currentTime-time)<.01 && !document.querySelector('video').seeking, time);
    };
    const pixels = () => page.evaluate(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      const result={red:[],green:[],blue:[]};
      for(let i=0;i<data.length;i+=4) {
        if(data[i+3]<100) continue;
        const point=[i/4%c.width,Math.floor(i/4/c.width)];
        if(data[i]>200 && data[i+1]<40 && data[i+2]<40) result.red.push(point);
        if(data[i+1]>200 && data[i]<40 && data[i+2]<40) result.green.push(point);
        if(data[i+2]>200 && data[i]<40 && data[i+1]<40) result.blue.push(point);
      }
      return Object.fromEntries(Object.entries(result).map(([key, points])=>[key, points.length ? {
        count:points.length,x:Math.min(...points.map(p=>p[0])),y:Math.min(...points.map(p=>p[1])),
        width:Math.max(...points.map(p=>p[0]))-Math.min(...points.map(p=>p[0]))+1,
        height:Math.max(...points.map(p=>p[1]))-Math.min(...points.map(p=>p[1]))+1,
      }:null]));
    });
    await seek(2);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      return c.getContext('2d').getImageData(85,62,1,1).data[0]>200;
    });
    const first=await pixels();
    assert.equal(first.red.x,80); assert.equal(first.red.y,60);
    assert.equal(first.red.width,75); assert.equal(first.red.height,6);
    assert.ok(first.green.count>100); assert.ok(first.blue.count>100);
    assert.equal(await page.locator('video track').count(),0);
    await seek(4);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      return c.getContext('2d').getImageData(255,185,1,1).data[2]>200;
    });
    assert.ok((await pixels()).blue.x>first.blue.x+80);
    await page.evaluate(() => {document.querySelector('.player-surface').style.width='320px';});
    await page.waitForFunction(() => document.querySelector('canvas.libassjs-canvas').width===320);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      return c.getContext('2d').getImageData(42,31,1,1).data[0]>200;
    });
    assert.equal((await pixels()).red.x,40);
    await page.click('#full');
    await page.waitForFunction(() => document.fullscreenElement?.classList.contains('player-surface'));
    assert.ok(await page.evaluate(() => document.fullscreenElement.contains(document.querySelector('canvas.libassjs-canvas'))));
    await seek(6);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      return !d.some((v,i)=>i%4===3 && v>0);
    });
    await seek(2);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      return c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3 && v>100);
    });
    await page.evaluate(async () => {
      const video=document.querySelector('video');
      video.playbackRate=2;
      await video.play();
    });
    await page.waitForFunction(() => document.querySelector('video').currentTime>5.5);
    await page.waitForFunction(() => {
      const c=document.querySelector('canvas.libassjs-canvas');
      return !c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3 && v>0);
    });
    await page.evaluate(()=>document.querySelector('video').pause());
    await page.evaluate(()=>window.cleanup());
    assert.equal(await page.locator('canvas.libassjs-canvas').count(),0);
    assert.deepEqual(errors,[]);
  } finally {
    await browser?.close();
    await new Promise(resolve=>server.close(resolve));
    fs.rmSync(dir,{recursive:true,force:true});
  }
});
