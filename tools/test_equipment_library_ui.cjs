/* Real frontend, mocked account snapshot: no game requests or mutations. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const {chromium}=require('/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..'),out=path.join(root,'build/equipment-library-followup-1030-review'),origin='https://equipment-library.invalid';
const fixture=JSON.parse(cp.execFileSync(path.join(root,'build/equipment-test-venv/bin/python'),['-c','import json;from tools.test_equipment_library import ui_fixture;print(json.dumps(ui_fixture()))'],{cwd:root,encoding:'utf8'}));
fixture.roles=[{id:'owner',name:'测试穿戴角色',avatar:'/assets/avatars/H153.png'}];
for(const equip of fixture.equips)if(equip.id.endsWith('-0')){equip.role='owner';equip.roleName='测试穿戴角色';}
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],calls=[],unexpected=[];page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{window.savedLibraryImage='';window.LucimaFiles={saveEncyclopediaImageStart:()=>({ok:true,token:'library'}),saveEncyclopediaImageChunk:(token,chunk,final)=>{window.savedLibraryImage+=chunk;return {ok:true,path:'/offline/装备库.png'};},saveEncyclopediaImageCancel:()=>{}};});
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url()),name=url.pathname,json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
  if(url.origin!==origin){unexpected.push(url.href);return route.abort();}
  if(name==='/api/equipment/action'){const body=route.request().postDataJSON();calls.push(body);assert(['reload','overview'].includes(body.action),'装备库不应提交穿戴等操作');return json(fixture);}
  if(name.startsWith('/api/')){const values={'/api/preferences':{settings:{}},'/api/config':{platform:'android',version:'offline'},'/api/accounts':{accounts:[]},'/api/tasks':{toggle:[],manual:[],buyTargets:[]},'/api/logs/auto':{entries:[],logSeq:0},'/api/status':{status:{}},'/api/roster':{roles:[],items:[]},'/api/game-data/status':{phase:'ready',revision:0},'/api/encyclopedia/resources':{}};if(!(name in values))unexpected.push(name);return json(values[name]||{});}
  const relative=name==='/'?'frontend/index.html':name.startsWith('/static/')?'frontend/'+decodeURIComponent(name.slice(8)):name.startsWith('/equipment/')?'equipment/web/'+name.slice(11):decodeURIComponent(name.slice(1)),file=path.resolve(root,relative);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){unexpected.push(name);return route.abort();}
  const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2','.json':'application/json'};return route.fulfill({contentType:mime[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 async function pick(label,value){const trigger=page.getByRole('button',{name:label,exact:true});await trigger.click();const control=page.locator('.eqp-library-dropdown').filter({has:trigger});await control.locator(`.dd-opt[data-val="${value}"]`).click();assert.equal(await page.locator('.eqp-library-dropdown.open').count(),0);}
 async function screenshot(name,width){await page.setViewportSize({width,height:1040});await page.evaluate(()=>{setDrawer(false);document.querySelector('.content').scrollTop=0;});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'页面不横向溢出');assert(await page.locator('.eqp-library').evaluate(n=>n.scrollWidth<=n.clientWidth+1),'装备库不横向溢出');await page.screenshot({path:path.join(out,`${name}-${width}.png`),animations:'disabled'});}
 try{
  await page.goto(origin);await page.waitForFunction(()=>typeof enterApp==='function'&&updateStartupReady);
  await page.evaluate(()=>{enterApp();activeAccount='offline';ACCOUNTS.offline={status:{},params:{},toggles:{},nextClaims:{}};window.dispatchEvent(new CustomEvent('lucima:account-change'));showSection('equipment');});
  await page.waitForFunction(()=>document.querySelector('#eqpStatus').textContent.startsWith('已读取'));
  assert.equal(await page.locator('#section-equipment .loadout-safety-warning').count(),0);
  assert.equal(await page.getByRole('tab',{name:'装备评分'}).count(),0);
  await page.getByRole('tab',{name:'战力总览',exact:true}).click();
  assert.equal(await page.locator('.eqp-library-power').innerText(),fixture.library.power.toFixed(1));
  assert.equal(await page.locator('.eqp-library-three-surface canvas').count(),1);assert.equal(await page.locator('#eqpTakeOffAll').isVisible(),false);
  assert.equal(await page.locator('.eqp-library-total').innerText(),'（总分100分）');
  assert(await page.locator('.eqp-library-total').evaluate(n=>parseFloat(getComputedStyle(n).fontSize)<parseFloat(getComputedStyle(n.parentElement).fontSize)));
  await page.locator('.content').evaluate(n=>n.scrollTop=0);
  const fit=await page.locator('.eqp-library-overview').evaluate(n=>({top:n.getBoundingClientRect().top,bottom:n.getBoundingClientRect().bottom,height:n.getBoundingClientRect().height,viewport:innerHeight}));
  assert(fit.height<500&&fit.bottom<fit.viewport-30,'总览与统计在390x844首屏显示');await page.screenshot({path:path.join(out,'overview-phone-390x844.png')});
  const compact=[];for(const width of [320,390,1280]){await screenshot('overview-dark',width);compact.push({width,height:await page.locator('.eqp-library-overview').evaluate(n=>n.getBoundingClientRect().height)});}
  fs.writeFileSync(path.join(out,'compact-layout.json'),JSON.stringify({compact,fit},null,2));

  await page.getByRole('button',{name:'切换雷达与条形图'}).click();assert.equal(await page.locator('.eqp-library-bars progress').count(),6);await page.getByRole('button',{name:'切换雷达与条形图'}).click();
  await page.getByRole('button',{name:'切换前五套速度的套装范围'}).click();assert((await page.locator('.eqp-library-speed-head').innerText()).includes('速度套'));
  await page.getByRole('button',{name:'保存装备战力图片'}).click();await page.waitForFunction(()=>window.savedLibraryImage.startsWith('iVBOR')&&document.body.textContent.includes('装备库图片已保存'));
  const encoded=await page.evaluate(()=>window.savedLibraryImage);fs.writeFileSync(path.join(out,'exported-radar.png'),Buffer.from(encoded,'base64'));
  await page.getByRole('button',{name:'满配',exact:false}).filter({has:page.locator('span', {hasText:'满配'})}).click();
  assert.equal(await page.locator('.eqp-library-card').count(),fixture.equips.filter(e=>e.enhance>=15&&e.quality.grade==='满配').length);
  await page.getByRole('button',{name:'满配 ×'}).click();
  assert.equal(await page.locator('.eqp-library-card').count(),fixture.library.maxed);
  for(const width of [320,390,1280]){await screenshot('library-dark',width);const columns=await page.locator('.eqp-library-grid').first().evaluate(n=>getComputedStyle(n).gridTemplateColumns.split(' ').length);assert(width<700?columns===3:columns>=4);}
  await page.setViewportSize({width:390,height:1040});
  assert.equal(await page.locator('.eqp-library-filters select').count(),0);
  for(const label of ['装备库排序','强化筛选','套装筛选']){const trigger=page.getByRole('button',{name:label,exact:true});await trigger.click();assert.equal(await trigger.getAttribute('aria-expanded'),'true');const toolbar=await page.locator('.eqp-library-filters').boundingBox();await page.mouse.click(toolbar.x+4,toolbar.y+toolbar.height+6);assert.equal(await trigger.getAttribute('aria-expanded'),'false');}
  const sortTrigger=page.getByRole('button',{name:'装备库排序',exact:true});await sortTrigger.press('ArrowDown');await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');assert((await page.getByRole('button',{name:'装备库排序',exact:true}).innerText()).includes('副速度'));await pick('装备库排序','score');
  await page.getByRole('button',{name:'套装筛选',exact:true}).click();assert(await page.locator('.eqp-library-dropdown.open .eqp-library-option-icon').count()>0);await page.keyboard.press('Escape');assert.equal(await page.locator('.eqp-library-dropdown.open').count(),0);
  const avatar=page.locator('.eqp-library-owner-icon').first();const avatarSize=await avatar.evaluate(n=>({width:n.getBoundingClientRect().width,top:n.getBoundingClientRect().top,artBottom:n.closest('.eqp-library-card').querySelector('.eqp-library-art').getBoundingClientRect().bottom}));assert(avatarSize.width>=26&&avatarSize.top>=avatarSize.artBottom);assert.equal(await page.locator('.eqp-library-art .eqp-library-owner-icon').count(),0);assert.equal(await page.locator('.eqp-library-item-name').count(),0);assert.equal(await page.locator('.eqp-library-item-meta .eqp-library-set-icon').count(),fixture.library.maxed);
  await page.getByRole('button',{name:'武器',exact:true}).click();assert.equal(await page.locator('.eqp-library-card').count(),7);
  await pick('套装筛选','Speed');assert.equal(await page.locator('.eqp-library-card').count(),4);
  const search=page.getByLabel('搜索装备库',{exact:true});await search.fill('速度');assert.equal(await search.inputValue(),'速度');assert.equal(await search.evaluate(n=>n===document.activeElement),true);assert.equal(await page.locator('.eqp-library-card').count(),4);
  await search.fill('不存在的装备');assert.equal(await page.locator('.eqp-library-card').count(),0);await page.getByRole('button',{name:'重置筛选',exact:true}).click();assert.equal(await page.locator('.eqp-library-card').count(),fixture.equips.length);
  await pick('强化筛选','embryo');assert.equal(await page.locator('.eqp-library-card').count(),fixture.library.embryos);
  await pick('强化筛选','max');await pick('装备库排序','speed');
  assert.equal(await page.locator('.eqp-library-group').count(),1);await page.locator('.eqp-library-card').first().click();assert.equal(await page.getByRole('dialog',{name:'装备详情'}).isVisible(),true);assert((await page.locator('.eqp-library-detail').innerText()).includes('主属性'));await page.locator('.eqp-library-detail h3').click();assert.equal(await page.getByRole('dialog',{name:'装备详情'}).isVisible(),true);await page.screenshot({path:path.join(out,'equipment-detail.png')});await page.mouse.click(8,20);await page.getByRole('dialog',{name:'装备详情'}).waitFor({state:'detached'});await page.locator('.eqp-library-card').first().click();await page.getByRole('button',{name:'关闭',exact:true}).click();
  await pick('装备库排序','score');await page.evaluate(()=>document.documentElement.dataset.theme='light');await screenshot('library-light',390);
  await page.getByRole('tab',{name:'战力总览',exact:true}).click();await screenshot('overview-light',390);const reloads=calls.length;await page.getByRole('button',{name:'刷新装备库'}).click();await page.waitForFunction(()=>!document.querySelector('#eqpControls').disabled);assert.equal(calls.length,reloads+1);
  await page.getByRole('tab',{name:'装备清单',exact:true}).click();await page.getByRole('button',{name:'武器',exact:true}).click();
  await page.evaluate(()=>{ACCOUNTS.second={status:{},params:{},toggles:{},nextClaims:{}};activeAccount='second';window.dispatchEvent(new CustomEvent('lucima:account-change'));});await page.waitForFunction(()=>document.querySelector('.eqp-library-power'));
  await page.getByRole('tab',{name:'装备清单',exact:true}).click();assert.equal(await page.locator('.eqp-library-card').count(),fixture.library.maxed);
  await page.getByRole('tab',{name:'配装',exact:true}).click();assert.equal(await page.locator('#eqpTakeOffAll').isVisible(),true);assert.equal(await page.locator('#section-equipment .loadout-safety-warning').count(),0);
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);fs.writeFileSync(path.join(out,'ui-checks.json'),JSON.stringify({ok:true,calls,errors,unexpected,gear:fixture.equips.length,checks:['radar and bars','PNG export via Android bridge','grade drilldown','3 mobile columns','320/390/1280 dark and light','slot/enhance/set/search','search focus','empty reset','detail dialog outside dismissal','custom dropdown outside dismissal and keyboard','larger lower portraits','set icons','compact overview with 100-point description','refresh','account reset','no equipment mutation']},null,2));
  process.stdout.write('装备库 UI 回归通过，截图和导出图片：'+out+'\n');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
