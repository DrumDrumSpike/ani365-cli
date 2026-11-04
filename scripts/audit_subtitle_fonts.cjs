// Usage: node scripts/audit_subtitle_fonts.cjs episode.ass [DATA_DIR/subtitle-fonts]
// Requires playwright with Chromium. No video download; checks actual libass fontselect.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const script = fs.readFileSync(process.argv[2], 'utf8');
  const directory = process.argv[3] || 'data/subtitle-fonts';
  const manifest = JSON.parse(fs.readFileSync(path.join(directory,'fonts.json')));
  const filenames = [...new Set(Object.values(manifest).flat())];
  const server = http.createServer((req,res) => {
    if (req.url === '/') {
      res.setHeader('Content-Type','text/html');
      res.end('<canvas width="640" height="360"></canvas><script src="/assets/subtitles-octopus.js"></script>');
      return;
    }
    const name=path.basename(req.url);
    const file = req.url.startsWith('/fonts/') && filenames.includes(name)
      ? path.join(directory,name) : path.join('ani365_bot/web_static',name);
    if (!fs.existsSync(file)) {res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.wasm')?'application/wasm':'application/octet-stream');
    const bytes=fs.readFileSync(file);
    // Diagnostic-only hook: expose libass verbose logging without modifying the
    // vendored worker or production renderer. Successful fontselect is level 6.
    res.end(name === 'subtitles-octopus-worker.js'
      ? Buffer.concat([bytes,Buffer.from('\nModule.onCustomMessage=function(){self.octObj.setLogLevel(6);};')]) : bytes);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true});
    for (const installed of [false,true]) {
      const page=await browser.newPage();
      const selections = new Set(), errors=[];
      page.on('console',msg=>{if(msg.text().includes('fontselect:')) selections.add(msg.text());});
      page.on('pageerror',e=>errors.push(e.message));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.evaluate(({script,fonts}) => {
        window.renderer=new SubtitlesOctopus({canvas:document.querySelector('canvas'),subContent:script,
          workerUrl:'/assets/subtitles-octopus-worker.js',fallbackFont:'/assets/LiberationSans-Regular.ttf',
          fonts,debug:true,onReady:()=>window.ready=true,onError:e=>window.rendererError=String(e)});
      },{script,fonts:installed?filenames.map(name=>'/fonts/'+name):[]});
      await page.waitForFunction(()=>window.ready || window.rendererError);
      assert.equal(await page.evaluate(()=>window.rendererError),undefined);
      const events=await page.evaluate(()=>new Promise((resolve,reject)=>window.renderer.getEvents(resolve,reject)));
      const styles=await page.evaluate(()=>new Promise((resolve,reject)=>window.renderer.getStyles(resolve,reject)));
      if (installed) {
        assert.deepEqual(styles,global.fallbackStyles);
        assert.deepEqual(events,global.fallbackEvents);
      } else {
        global.fallbackStyles=styles;
        global.fallbackEvents=events;
      }
      await page.evaluate(()=>window.renderer.worker.postMessage({target:'custom'}));
      // Pick a frame for every distinct style/override/bold/italic combination.
      const samples = new Map();
      for (const event of events) {
        const overrides=[...event.Text.matchAll(/\\(?:fn[^\\}]+|b\d+|i\d+)/g)].map(m=>m[0]).join('');
        if (/\\p[1-9]/.test(event.Text) || event.Duration<100) continue;
        const key=event.Style+overrides;
        if (!samples.has(key)) samples.set(key,(event.Start+event.Duration/2)/1000);
      }
      console.log((installed?'Installed':'Fallback')+': '+samples.size+' frames');
      const hashes=[];
      for(const time of samples.values()) {
        const pixels=await page.evaluate(async time=>{
          window.renderer.setCurrentTime(time);
          // Worker API response is a FIFO barrier after the render request.
          await new Promise((resolve,reject)=>window.renderer.getStyles(resolve,reject));
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          const c=document.querySelector('canvas');
          const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
          let hash=2166136261;
          for (const value of data) hash=Math.imul(hash ^ value,16777619);
          return hash >>> 0;
        },time);
        hashes.push(pixels);
      }
      assert.deepEqual(errors,[]);
      console.log('\n'+(installed?'INSTALLED FONTS':'FALLBACK ONLY')+` (${samples.size} sampled frames)`);
      console.log([...selections].join('\n'));
      if (installed) {
        for (const selection of selections) {
          const match=/fontselect: \(([^,]+),/.exec(selection);
          const expected=match && manifest[match[1].toLowerCase()];
          if (expected) assert.ok(expected.some(name=>selection.includes(name)), selection);
        }
        const changed=hashes.filter((hash,i)=>hash!==global.fallbackHashes[i]).length;
        console.log(`Changed frames: ${changed}/${hashes.length}`);
      } else global.fallbackHashes=hashes;
      await page.close();
    }
  } finally {
    await browser?.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
