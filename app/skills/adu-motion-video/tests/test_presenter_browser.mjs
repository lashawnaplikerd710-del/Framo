// Real pack layouts and macro playback; media here are explicit SVG fixtures.
import fs from 'node:fs';import path from 'node:path';import os from 'node:os';import assert from 'node:assert/strict';import {fileURLToPath,pathToFileURL} from 'node:url';import {spawnSync} from 'node:child_process';
import {chromium} from 'playwright-core';import {chrome} from '../scripts/chrome.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=process.argv[2],fixtures=output||fs.mkdtempSync(path.join(os.tmpdir(),'adu-presenter-'));
let browser;const report=[];
try {
 const versions=output?JSON.parse(fs.readFileSync(path.join(output,'versions.json'))):JSON.parse(fs.readFileSync(root+'/references/presenter-motion-versions.json'));
 if(!output) {
  const made=spawnSync('python3',['-c',`import sys,json;sys.path.insert(0,${JSON.stringify(root+'/tests')});from portrait_fixture import create;create(${JSON.stringify(fixtures+'/after')},[(r['family'],r['to']) for r in json.loads(${JSON.stringify(JSON.stringify(versions))})])`],{encoding:'utf8'});if(made.status)throw Error(made.stderr);
 }
 browser=await chromium.launch({executablePath:chrome(),headless:true,args:['--allow-file-access-from-files','--disable-accelerated-2d-canvas']});
 for(const v of versions) {
  const page=await browser.newPage({viewport:{width:1080,height:1920}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const svg=fs.readFileSync(fixtures+'/after/'+v.family+'/assets/demo-presenter.svg');
  await page.route('**/*',r=>/\.(jpg|jpeg|png|svg|webp)(\?|$)/.test(r.request().url())?r.fulfill({body:svg,contentType:'image/svg+xml'}):r.continue());
  await page.goto(pathToFileURL(fixtures+'/after/'+v.family+'/index.html').href);await page.evaluate(()=>window.READY);
  const plan=JSON.parse(fs.readFileSync(fixtures+'/after/'+v.family+'/plan.json'));const audio=await page.evaluate(()=>JSON.stringify(SFX));
  const metrics=[],seams=[];
  for(let i=0;i<plan.length;i++) {
   const p=plan[i],times=new Set([p.output_start_frame,(p.output_start_frame+p.output_end_frame)>>1,p.output_end_frame-1]);
   for(let f=p.output_start_frame;f<Math.min(p.output_start_frame+100,p.output_end_frame);f+=2)times.add(f);
   for(const f of [...times].sort((a,b)=>a-b)) {
    const state=await page.evaluate(async f=>{await renderAt(f/60);await imgWait();return{frame:f,rects:[...document.querySelectorAll('.cam,.tkc')].filter(e=>e.querySelector('img')&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})).map(e=>{const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})};},f);
    metrics.push(state);
   }
   const first=p.output_start_frame+Math.min(30,p.output_end_frame-p.output_start_frame-1);
   const a=await page.evaluate(async f=>{await renderAt(f/60);await imgWait();return [...document.querySelectorAll('.cam,.tkc')].map(e=>[e.style.cssText,e.parentElement.style.cssText]);},first);
   await page.evaluate(async f=>{await renderAt(f/60);await imgWait();},p.output_end_frame-1);
   const b=await page.evaluate(async f=>{await renderAt(f/60);await imgWait();return [...document.querySelectorAll('.cam,.tkc')].map(e=>[e.style.cssText,e.parentElement.style.cssText]);},first);
   assert.deepEqual(b,a,v.family+' reverse pose at '+p.sceneId);
  }
  if(['vibrant-color-performance','dark-3d-showcase'].includes(v.family))for(let i=1;i<plan.length;i++) {
   const frame=plan[i].output_start_frame;
   const primary=async f=>page.evaluate(async f=>{await renderAt(f/60);await imgWait();const e=[...document.querySelectorAll('.cam,.tkc')].filter(e=>e.querySelector('img')&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})).sort((a,b)=>b.getBoundingClientRect().width*b.getBoundingClientRect().height-a.getBoundingClientRect().width*a.getBoundingClientRect().height)[0];if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};},f);
   const before=await primary(frame-1),after=await primary(frame);
   if(before&&after){const distance=Math.hypot(after.x-before.x,after.y-before.y),ratio=Math.max(after.w/before.w,before.w/after.w,after.h/before.h,before.h/after.h);assert.ok(distance<2&&ratio<1.02,`${v.family} ${plan[i].sceneId} seam jump: ${distance}/${ratio}`);seams.push({scene:plan[i].sceneId,distance,ratio});}
  }
  assert.deepEqual(errors,[],v.family+' runtime errors');assert.equal(await page.evaluate(()=>JSON.stringify(SFX)),audio,v.family+' SFX changed');
  if(!['vibrant-color-performance','dark-3d-showcase'].includes(v.family)) {
   const cx=v.family==='vivid-sticker-performance'?880:900,cy=v.family==='vivid-sticker-performance'?250:240,size=v.family==='vivid-sticker-performance'?240:220;
   for(const state of metrics)for(const r of state.rects) {
    assert.ok(Math.abs(r.x-cx)<1.5&&Math.abs(r.y-cy)<1.5,`${v.family} f${state.frame}: unstable resting anchor ${JSON.stringify(r)}`);
    assert.ok(Math.abs(r.w-size)<1.5&&Math.abs(r.h-size)<1.5,`${v.family} f${state.frame}: unstable size ${JSON.stringify(r)}`);
   }
  }
  report.push({family:v.family,groups:plan.length,sampledFrames:metrics.length,visiblePoses:metrics.reduce((n,s)=>n+s.rects.length,0),errors,seams,status:'geometry-and-seek-pass'});await page.close();
 }
 const result={passed:true,styles:report.length,groups:report.reduce((n,r)=>n+r.groups,0),checks:['fixed resting anchor and size','reverse/repeated seeks','unchanged SFX'],limits:'Synthetic media geometry; real face/occlusion and continuous sound listening remain separate',report};
 if(output)fs.writeFileSync(output+'/browser-report.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
}finally{if(browser)await browser.close();if(!output)fs.rmSync(fixtures,{recursive:true,force:true});}
