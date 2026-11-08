/* Offline surface QA: compare each leader with its rendered 3D marker. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const {chromium}=require('/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'build/equipment-rotation-1102-review'),origin='https://equipment-library.invalid';
const fixture=JSON.parse(cp.execFileSync(path.join(root,'build/equipment-test-venv/bin/python'),['-c','import json;from tools.test_equipment_library import ui_fixture;print(json.dumps(ui_fixture()))'],{cwd:root,encoding:'utf8'}));
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:390,height:1100},deviceScaleFactor:2}),errors=[],unexpected=[],checks=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url()),name=url.pathname,json=v=>route.fulfill({contentType:'application/json',body:JSON.stringify(v)});
  if(url.origin!==origin){unexpected.push(url.href);return route.abort();}
  if(name==='/api/equipment/action'){assert(['reload','overview'].includes(route.request().postDataJSON().action));return json(fixture);}
  if(name.startsWith('/api/')){const values={'/api/preferences':{settings:{}},'/api/config':{platform:'android',version:'offline'},'/api/accounts':{accounts:[]},'/api/tasks':{toggle:[],manual:[],buyTargets:[]},'/api/logs/auto':{entries:[],logSeq:0},'/api/status':{status:{}},'/api/roster':{roles:[],items:[]},'/api/game-data/status':{phase:'ready',revision:0},'/api/encyclopedia/resources':{}};if(!(name in values))unexpected.push(name);return json(values[name]||{});}
  const relative=name==='/'?'frontend/index.html':name.startsWith('/static/')?'frontend/'+decodeURIComponent(name.slice(8)):name.startsWith('/equipment/')?'equipment/web/'+name.slice(11):decodeURIComponent(name.slice(1)),file=path.resolve(root,relative);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){unexpected.push(name);return route.abort();}
  const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.json':'application/json'};
  return route.fulfill({contentType:mime[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 try{
  await page.goto(origin);await page.waitForFunction(()=>typeof enterApp==='function'&&updateStartupReady);
  await page.evaluate(()=>{
   const Renderer=THREE.WebGLRenderer;
   THREE.WebGLRenderer=class extends Renderer{constructor(options){super(options);const render=this.render;this.render=(scene,camera)=>{render.call(this,scene,camera);if(scene.children.filter(n=>n.geometry?.type==='SphereGeometry').length===6){window.surfaceScene=scene;window.surfaceCamera=camera;}};}};
   enterApp();activeAccount='offline';ACCOUNTS.offline={status:{},params:{},toggles:{},nextClaims:{}};window.dispatchEvent(new CustomEvent('lucima:account-change'));showSection('equipment');
  });
  await page.waitForFunction(()=>document.querySelector('#eqpStatus').textContent.startsWith('已读取'));
  await page.getByRole('tab',{name:'战力总览',exact:true}).click();
  const samples={reported:[18,3,28,17,9,8],zero:[0,0,0,0,0,0],full:[30,20,80,80,40,80],alternating:[0,20,0,80,0,80]};
  await page.evaluate(data=>{window.surfaceFixture=data;},fixture);
  for(const [sample,values] of Object.entries(samples)){
   await page.evaluate(values=>{const pane=document.querySelector('#eqpContent');pane.querySelectorAll('.eqp-library-three-surface,.eqp-library-three-pie').forEach(n=>n.dispose?.());pane.replaceChildren();surfaceFixture.library.axes.forEach((a,i)=>a.value=values[i]);surfaceFixture.library.power=Math.round(surfaceFixture.library.axes.reduce((sum,a)=>sum+Math.min(1,a.value/a.target),0)/6*1000)/10;window.EquipmentLibrary.render({pane,data:surfaceFixture,account:'offline'});},values);
   for(const width of [320,360,390,768,1280]){
    await page.setViewportSize({width,height:1100});
    await page.waitForFunction(()=>{const p=document.querySelector('.eqp-library-surface-plot'),s=p.querySelector('svg');return s&&+s.getAttribute('width')===p.clientWidth&&s.querySelectorAll('polyline').length===6;});
    for(const turn of [0,90,180,270]){
    await page.evaluate(turn=>{const p=document.querySelector('.eqp-library-surface-plot');p.dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}));for(let i=0;i<turn/10;i++)p.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));},turn);
    await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
    const geometry=await page.evaluate(()=>{
     const plot=document.querySelector('.eqp-library-surface-plot'),canvas=plot.querySelector('canvas'),svg=plot.querySelector('svg'),box=plot.getBoundingClientRect();
     const markers=surfaceScene.children.filter(n=>n.geometry?.type==='SphereGeometry').map(n=>{const p=n.position.clone().project(surfaceCamera);return {x:(p.x*.5+.5)*box.width,y:(-p.y*.5+.5)*box.height,radius:n.geometry.parameters.radius,height:n.position.y,score:n.userData.score};});
     const surface=surfaceScene.children.find(n=>n.userData.kind==='scoreSurface'),position=surface.geometry.getAttribute('position'),color=surface.geometry.getAttribute('color');let lo=0,hi=0;for(let i=1;i<position.count;i++){if(position.getY(i)<position.getY(lo))lo=i;if(position.getY(i)>position.getY(hi))hi=i;}const hex=i=>new THREE.Color(color.getX(i),color.getY(i),color.getZ(i)).convertLinearToSRGB().getHexString(),palette={low:hex(lo),high:hex(hi),range:surface.userData.colorRange};
     const labels=[...svg.querySelectorAll('text')].map(n=>{const b=n.getBBox();return {text:n.textContent,x:b.x,y:b.y,w:b.width,h:b.height};});
     const leaders=[...svg.querySelectorAll('polyline')].map(n=>({label:n.dataset.axis,points:[...n.points].map(p=>({x:p.x,y:p.y}))}));
     const legend=document.querySelector('.eqp-library-surface-legend').getBoundingClientRect(),hint=document.querySelector('.eqp-library-surface-hint').getBoundingClientRect(),pies=document.querySelector('.eqp-library-pies').getBoundingClientRect();
     return {palette,w:box.width,h:box.height,canvas:{w:canvas.clientWidth,h:canvas.clientHeight},markers,labels,leaders,legendGap:hint.top-legend.bottom,piesGap:pies.top-hint.bottom,overflow:document.documentElement.scrollWidth>innerWidth,tools:document.querySelectorAll('.eqp-library-tools button').length};
    });
    assert.equal(geometry.palette.low,'3fe0d0');assert.equal(geometry.palette.high,sample==='zero'?'3fe0d0':'ff7a8a');assert.equal(geometry.tools,0);assert(!geometry.overflow,`${sample}/${width}: page overflow`);
    assert.equal(geometry.canvas.w,geometry.w);assert.equal(geometry.canvas.h,geometry.h);
    assert(geometry.legendGap>=0&&geometry.piesGap>=12,`${sample}/${width}: legend overlaps following content`);
    for(let i=0;i<6;i++){
     const leader=geometry.leaders[i],label=geometry.labels[i],axis=fixture.library.axes.findIndex(a=>a.label===leader.label),marker=geometry.markers[axis],[start,knee,end]=leader.points;
     assert(Math.hypot(start.x-marker.x,start.y-marker.y)<.01,`${sample}/${width}/${leader.label}: detached from marker`);
     assert(Math.abs(start.x-knee.x)>=12&&start.y-knee.y>=18,`${sample}/${width}/${leader.label}: first segment must be diagonal`);
     assert(Math.abs(knee.y-end.y)<.01);assert.equal(marker.radius,.032);assert(Math.abs(marker.height-(.047+marker.score/100*1.15))<1e-6,'peak height must exactly match the axis score');
     assert(label.x>=-1&&label.x+label.w<=geometry.w+1&&label.y>=0&&label.y+label.h<=geometry.h,`${sample}/${width}/${leader.label}: clipped text ${JSON.stringify(label)}`);
     assert(label.y+label.h<knee.y-2,`${sample}/${width}/${leader.label}: value below horizontal line`);
     assert(label.x>=Math.min(knee.x,end.x)-1&&label.x+label.w<=Math.max(knee.x,end.x)+1,`${sample}/${width}/${leader.label}: line too short for text`);
     for(let j=0;j<i;j++){const other=geometry.labels[j];assert(label.x+label.w<=other.x||other.x+other.w<=label.x||label.y+label.h<=other.y||other.y+other.h<=label.y,`${sample}/${width}: overlapping labels`);}
    }
    checks.push({sample,width,turn,plot:{width:geometry.w,height:geometry.h}});
    if(sample==='reported'&&turn===0&&[320,390,1280].includes(width)){
     await page.locator('.content').evaluate(n=>n.scrollTop=0);
     const clip=await page.locator('.eqp-library-overview').evaluate(n=>{const b=n.getBoundingClientRect(),p=n.querySelector('.eqp-library-pies').getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:p.y-b.y};});
     await page.screenshot({path:path.join(out,`surface-${width}.png`),clip,scale:'css'});
    }
    }
   }
  }
  await page.setViewportSize({width:390,height:1100});await page.evaluate(()=>{setDrawer(false);document.querySelector('.content').scrollTop=0;});
  const plot=page.locator('.eqp-library-surface-plot');await plot.scrollIntoViewIfNeeded();await plot.press('Home');
  assert.equal(await page.locator('.eqp-library-rotate-hint').innerText(),'左右拖动可水平旋转查看');
  const box=await plot.boundingBox(),before=await page.evaluate(()=>({x:surfaceCamera.position.x,z:surfaceCamera.position.z}));
  await page.mouse.move(box.x+box.width*.4,box.y+box.height*.55);await page.mouse.down();await page.mouse.move(box.x+box.width*.7,box.y+box.height*.55,{steps:8});await page.mouse.up();
  await page.waitForFunction(()=>+document.querySelector('.eqp-library-surface-plot').dataset.rotation>.1);
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(r)));
  const after=await page.evaluate(()=>({x:surfaceCamera.position.x,z:surfaceCamera.position.z}));assert(Math.hypot(after.x-before.x,after.z-before.z)>.1,'mouse drag must move the camera');
  await plot.press('Home');await page.waitForFunction(()=>document.querySelector('.eqp-library-surface-plot').dataset.rotation==='0');
  const cdp=await page.context().newCDPSession(page);await cdp.send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:1});
  const x=box.x+box.width*.4,y=box.y+box.height*.55;await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:1}]});
  for(let i=1;i<=6;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x+i*10,y,id:1}]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await page.waitForFunction(()=>+document.querySelector('.eqp-library-surface-plot').dataset.rotation>.1);
  assert.equal(await plot.evaluate(n=>getComputedStyle(n).touchAction),'pan-y');
  const rotation=await plot.getAttribute('data-rotation'),scrollBefore=await page.locator('.content').evaluate(n=>n.scrollTop);
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:2}]});
  for(let i=1;i<=8;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x+1,y:y-i*18,id:2}]});
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await page.waitForFunction(before=>document.querySelector('.content').scrollTop>before,scrollBefore);assert.equal(await plot.getAttribute('data-rotation'),rotation,'vertical swipe must scroll without rotating');
  await cdp.detach();
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  fs.writeFileSync(path.join(out,'ui-checks.json'),JSON.stringify({ok:true,checks,errors,unexpected},null,2));
  console.log(`Surface QA passed: ${checks.length} layouts, markers/leaders/text/legend checked. ${out}`);
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
