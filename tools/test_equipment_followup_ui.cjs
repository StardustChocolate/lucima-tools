/* Offline surface QA: compare each leader with its rendered 3D marker. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const {chromium}=require('/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'build/equipment-rotation-1102-review'),origin='https://equipment-library.invalid';
const fixture=JSON.parse(cp.execFileSync(path.join(root,'build/equipment-test-venv/bin/python'),['-c',`
import json
from tools.test_equipment_library import native_equipment
from tools.test_equipment_rankings import gear
from equipment import library,plans
from equipment.api import _meta
from backend import tasks
items=native_equipment()
for kind in ('Hit','Resist','Health','Defense'):
 for n in range(6):
  for slot in plans.SLOTS:
   e=gear(slot,n,kind,SpeedValue=16-n,HPRate=.20-n*.02,DefenceRate=.15-n*.01,EffectHitRate=.14-n*.01,ResistanceRate=.12-n*.01)
   e['_id']={'$oid':f'{kind}-{slot}-{n}'}
   items.append(e)
views=[{**plans._equip_view(e),'quality':library.quality(e),'roleName':''} for e in items]
s=library.overview(items)
s.update(qualified=100,small=90,large=60,embryos=0)
for i,x in enumerate(s['speedBands']):x['count']=[10,30,60,0][i]
print(json.dumps({**_meta(),'roles':[],'equips':views,'library':s,'manual':tasks.manual_tasks()}))
`],{cwd:root,encoding:'utf8'}));
fixture.roles=[{id:'owner',name:'测试角色',avatar:'/assets/avatars/H153.png'}];for(const e of fixture.equips)e.level=85;for(const e of fixture.equips)if(e.id.endsWith('-0')){e.role='owner';e.roleName='测试角色';}
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:390,height:1100},deviceScaleFactor:2}),errors=[],unexpected=[],checks=[],taskRuns=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url()),name=url.pathname,json=v=>route.fulfill({contentType:'application/json',body:JSON.stringify(v)});
  if(url.origin!==origin){unexpected.push(url.href);return route.abort();}
  if(name==='/api/equipment/action'){assert(['reload','overview'].includes(route.request().postDataJSON().action));return json(fixture);}
  if(name==='/api/tasks/run'){taskRuns.push(route.request().postDataJSON());return json({results:[{id:'claim_mail_all',name:'领取邮件',ok:true,detail:'共领取2封邮件（离线验证）'}]});}
  if(name.startsWith('/api/')){const values={'/api/preferences':{settings:{}},'/api/config':{platform:'android',version:'offline'},'/api/accounts':{accounts:[]},'/api/tasks':{toggle:[],manual:[fixture.manual[0]],buyTargets:[]},'/api/logs/auto':{entries:[],logSeq:0},'/api/status':{status:{}},'/api/roster':{roles:[],items:[]},'/api/game-data/status':{phase:'ready',revision:0},'/api/encyclopedia/resources':{}};if(!(name in values))unexpected.push(name);return json(values[name]||{});}
  const relative=name==='/'?'frontend/index.html':name.startsWith('/static/')?'frontend/'+decodeURIComponent(name.slice(8)):name.startsWith('/equipment/')?'equipment/web/'+name.slice(11):decodeURIComponent(name.slice(1)),file=path.resolve(root,relative);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){unexpected.push(name);return route.abort();}
  const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.json':'application/json'};
  return route.fulfill({contentType:mime[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 try{
  await page.goto(origin);await page.waitForFunction(()=>typeof enterApp==='function'&&updateStartupReady);
  await page.evaluate(()=>{
   const Renderer=THREE.WebGLRenderer;
   THREE.WebGLRenderer=class extends Renderer{constructor(options){super(options);const render=this.render;this.render=(scene,camera)=>{render.call(this,scene,camera);if(scene.children.filter(n=>n.geometry?.type==='SphereGeometry').length===6){window.surfaceScene=scene;window.surfaceCamera=camera;}if(scene.children.some(n=>n.geometry?.type==='ExtrudeGeometry')){window.pieScenes=window.pieScenes||[];if(!window.pieScenes.some(p=>p.scene===scene))window.pieScenes.push({scene,camera});}};}};
   enterApp();activeAccount='offline';ACCOUNTS.offline={status:{},params:{},toggles:{},nextClaims:{}};window.dispatchEvent(new CustomEvent('lucima:account-change'));showSection('equipment');
  });
  await page.waitForFunction(()=>document.querySelector('#eqpStatus').textContent.startsWith('已读取'));
  await page.getByRole('tab',{name:'战力总览',exact:true}).click();
  assert.deepEqual(fixture.library.rankings.map(c=>c.builds.length),[3,3,3,6]);
  await page.waitForFunction(()=>window.pieScenes?.length===2&&document.querySelectorAll('.eqp-library-ranking-value').length===15);
  const pies=await page.evaluate(()=>pieScenes.map(({scene,camera})=>({camera:camera.type,sectors:scene.children.filter(n=>n.geometry?.type==='ExtrudeGeometry').map(n=>{n.geometry.computeBoundingBox();return {...n.userData,spanY:n.geometry.boundingBox.max.y-n.geometry.boundingBox.min.y};})})));
  for(const pie of pies){assert.equal(pie.camera,'OrthographicCamera');assert.equal(pie.sectors.length,3);for(const s of pie.sectors)assert(Math.abs(s.spanY-s.height)<1e-6,'extrusion must point upwards');assert(Math.abs(pie.sectors.find(s=>s.value===30).height/pie.sectors.find(s=>s.value===10).height-8/3)<1e-6);}
  await page.evaluate(data=>{const pane=document.querySelector('#eqpContent');pane.querySelectorAll('.eqp-library-three-surface,.eqp-library-three-pie').forEach(n=>n.dispose?.());pane.replaceChildren();window.pieScenes=[];Object.assign(data.library,{qualified:478,small:153,large:19,embryos:127});data.library.speedBands.forEach((b,i)=>b.count=[96,48,20,6][i]);window.EquipmentLibrary.render({pane,data,account:'offline'});},fixture);
  await page.waitForFunction(()=>window.pieScenes.length===2&&document.querySelectorAll('.eqp-library-pie-leader').length===8);
  assert.equal(await page.locator('.eqp-library-metrics,.eqp-library-speeds,.eqp-library-speed-head,.eqp-library-open').count(),0);
  assert.equal(await page.locator('.eqp-library-ranking-chart,.eqp-library-ranking-legend,.eqp-library-ranking-note').count(),0);assert.equal(await page.locator('.eqp-library-ranking-help').innerText(),'点击可查看对应装备详情');assert.equal(await page.locator('.eqp-library-ranking-help').evaluate(n=>getComputedStyle(n).color),'rgb(54, 224, 164)');assert.equal(await page.locator('.eqp-library-build-detail').isVisible(),false);
  assert.equal(await page.locator('.eqp-library-ranking-value').count(),15);
  assert((await page.locator('.eqp-library-ranking-rows').innerText()).includes('速度≥200'));
  assert((await page.locator('.eqp-library-ranking-rows').innerText()).includes('速度≥150'));
  for(const key of ['speed','hit','resist','survival']){
   await page.locator(`.eqp-library-ranking-value[data-key="${key}"]`).first().click();
   assert.equal(await page.locator('.eqp-library-build-gear button').count(),6);assert.equal(await page.locator('.eqp-library-build-detail').evaluate(n=>n.parentElement.dataset.key),key);assert.equal(await page.locator('.eqp-library-ranking-row .eqp-library-build-detail').count(),1);assert.equal(await page.locator('.eqp-library-ranking-value[aria-expanded=true]').count(),1);
  }
  await page.locator('.eqp-library-ranking-value[data-key=hit]').first().click();
  await page.locator('.eqp-library-build-gear button').first().click();assert(await page.getByRole('dialog',{name:'装备详情'}).isVisible());await page.getByRole('button',{name:'关闭',exact:true}).click();
  for(const width of [320,390,768,1280]){
   await page.setViewportSize({width,height:1100});await page.evaluate(()=>setDrawer(false));await page.waitForFunction(()=>{const p=document.querySelector('.eqp-library-three-pie');return p.querySelector('svg').getAttribute('width')==p.clientWidth;});
   const geometry=await page.evaluate(()=>[...document.querySelectorAll('.eqp-library-three-pie')].map(p=>{const leaders=[...p.querySelectorAll('svg polyline')].map(n=>({index:n.dataset.index,points:[...n.points].map(p=>({x:p.x,y:p.y}))})),dots=[...p.querySelectorAll('svg circle')].map(n=>({index:n.dataset.index,x:+n.getAttribute('cx'),y:+n.getAttribute('cy')}));const labels=[...p.querySelectorAll('svg text')].map(n=>{const b=n.getBBox();return {x:b.x,y:b.y,w:b.width,h:b.height,text:n.textContent};});return {w:p.clientWidth,h:p.clientHeight,labels,leaders,dots};}));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   for(const pie of geometry){for(const l of pie.leaders){const dot=pie.dots.find(d=>d.index===l.index),[a,b,c]=l.points;assert(Math.hypot(a.x-dot.x,a.y-dot.y)<.01,'pie line must start at marker');assert(Math.abs(a.x-b.x)>=6&&Math.abs(a.y-b.y)>=10,'pie line must have a clear diagonal');assert.equal(b.y,c.y);assert(Math.abs(b.x-c.x)>=33.9,'pie line must have a horizontal label segment');}for(const b of pie.labels)assert(b.x>=0&&b.x+b.w<=pie.w&&b.y>=0&&b.y+b.h<=pie.h,`${width} pie caption cropped`);}
   if([390,1280].includes(width)){
    await page.locator('.eqp-library-pies').screenshot({path:path.join(out,`pies-${width}.png`),scale:'css',animations:'disabled',style:'#eqpViewTabs,.sidebar,.sidebar-backdrop{visibility:hidden!important}'});
    await page.locator('.eqp-library-rankings').screenshot({path:path.join(out,`rankings-${width}.png`),scale:'css',animations:'disabled',style:'#eqpViewTabs,.sidebar,.sidebar-backdrop{visibility:hidden!important}'});
   }
   checks.push({section:'pie and rankings',width});
  }
  await page.getByRole('tab',{name:'装备清单',exact:true}).click();
  for(const width of [320,360,390,1280]){
   await page.setViewportSize({width,height:1100});await page.evaluate(()=>setDrawer(false));
   const cards=await page.locator('.eqp-library-card').evaluateAll(cards=>cards.slice(0,12).map(n=>{const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height};};return {card:rect(n),art:rect(n.querySelector('.eqp-library-art')),score:rect(n.querySelector('.eqp-library-card-score')),slot:rect(n.querySelector('.eqp-library-item-slot')),badges:[...n.querySelectorAll('.eqp-library-level,.eqp-library-enhance')].map(rect),radius:parseFloat(getComputedStyle(n.querySelector('.eqp-library-card-score')).borderRadius),overflow:getComputedStyle(n.querySelector('.eqp-library-art')).overflow,slotOverflow:n.querySelector('.eqp-library-item-slot').scrollWidth>n.querySelector('.eqp-library-item-slot').clientWidth+1,scoreOverflow:n.querySelector('.eqp-library-card-score').scrollWidth>n.querySelector('.eqp-library-card-score').clientWidth};}));
   for(const c of cards){assert(c.score.x>=c.card.x&&c.score.right<=c.card.right,'score must remain inside card');assert(Math.abs(c.score.y+c.score.h/2-c.slot.y-c.slot.h/2)<1,'score must align with slot name');assert(c.radius>=6);assert(!c.scoreOverflow);assert(!c.slotOverflow,`${width}: slot name must remain readable`);assert.equal(c.overflow,'visible');for(const b of c.badges){assert(b.x>=c.card.x&&b.right<=c.card.right&&b.y>=c.card.y,'badge must not be clipped by card');assert(b.y<c.art.y,'badge must be raised above artwork');}}
   if(width===390){await page.locator('.content').evaluate(n=>n.scrollTop=0);const b=await page.locator('.eqp-library-group').first().boundingBox();await page.screenshot({path:path.join(out,'equipment-list-390.png'),clip:{x:b.x,y:b.y,width:b.width,height:Math.min(500,b.height)},scale:'css',animations:'disabled',style:'#eqpViewTabs,.sidebar,.sidebar-backdrop{visibility:hidden!important}'});}
   checks.push({section:'card score',width});
  }
  assert.equal(fixture.manual[0].id,'claim_mail_all');
  await page.evaluate(first=>{TASKS=[first];renderManualPanel(TASKS);showSection('manual');},fixture.manual[0]);
  const first=page.locator('#taskList [data-id]').first();assert.equal(await first.getAttribute('data-id'),'claim_mail_all');
  await first.click();
  await page.waitForFunction(()=>document.querySelector('#ts-claim_mail_all').textContent.includes('完成'));
  assert.equal(taskRuns.length,1);assert.deepEqual(taskRuns[0].taskIds,['claim_mail_all']);assert.deepEqual(taskRuns[0].params,{claim_mail_all:{}});
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  fs.writeFileSync(path.join(out,'followup-ui-checks.json'),JSON.stringify({ok:true,checks,pies,taskRuns,errors,unexpected},null,2));
  console.log(`Equipment followup UI passed: flat sector geometry, share heights, 4 rankings/15 builds, category-local details, aligned score/badges and manual-mail dispatch. ${out}`);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
