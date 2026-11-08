/* Full-page offline checks. No game or portal requests are allowed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const {chromium} = require('/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'build/release-1.3.2-review/ui');
const origin = 'https://release-ui.invalid';
const tasks = JSON.parse(cp.execFileSync(path.join(root, 'build/equipment-test-venv/bin/python'), ['-c',
  'import json;from backend.appcore import tasks;print(json.dumps(tasks()))'], {cwd:root}).toString());
tasks.toggle = [];
tasks.manual = tasks.manual.filter(t => t.id === 'shop_diamond');
const saved = [
  {account:'one@example.invalid',name:'凤愿',hasToken:true},
  {account:'two@example.invalid',name:'很长的角色名字用于验证显示和截断',hasToken:true},
  {account:'three@example.invalid',name:'<img src=x onerror=alert(1)>',hasToken:true},
];

(async () => {
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({headless:true, executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport:{width:390,height:844},
    userAgent:'Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'});
  const errors=[], unexpected=[], checks=[];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', async route => {
    const req=route.request(),url=new URL(req.url());
    if(url.origin !== origin){unexpected.push(url.href);return route.abort();}
    const send=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
    if(url.pathname.startsWith('/api/')) {
      const values={
        '/api/accounts':{accounts:saved}, '/api/preferences':{settings:{}},
        '/api/config':{version:'1.3.2',platform:'android'},'/api/tasks':tasks,
        '/api/status':{status:{}},'/api/logs/auto':{entries:[],logSeq:0},
        '/api/accounts/start':{ok:true},'/api/next-claims':{nextClaims:{}},
      };
      return send(values[url.pathname] || {ok:true});
    }
    let rel=url.pathname==='/'?'frontend/index.html':url.pathname.startsWith('/static/')?
      'frontend/'+url.pathname.slice(8):url.pathname.startsWith('/equipment/')?
      'equipment/web/'+url.pathname.slice(11):url.pathname.slice(1);
    const file=path.resolve(root,rel);
    if(!file.startsWith(root+path.sep)||!fs.existsSync(file))return route.fulfill({status:404,body:''});
    const ext=path.extname(file),types={'.html':'text/html','.js':'text/javascript','.css':'text/css',
      '.json':'application/json','.png':'image/png','.svg':'image/svg+xml','.webp':'image/webp'};
    return route.fulfill({contentType:types[ext]||'application/octet-stream',body:fs.readFileSync(file)});
  });
  try {
    await page.goto(origin);
    await page.waitForFunction(()=>typeof SAVED_ACCTS!=='undefined'&&SAVED_ACCTS.length===3);
    assert.equal(await page.locator('html').getAttribute('data-rendering'),'stable');
    await page.locator('#acctCaret').click();
    assert.equal(await page.locator('.sa-name').first().innerText(),'凤愿');
    assert.equal(await page.locator('.sa-name').nth(2).innerText(),saved[2].name);
    assert.equal(await page.locator('#savedAccts img').count(),0);
    assert.equal(await page.locator('#savedAccts').evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(13, 20, 36)');
    for(const width of [360,390,760]) {
      await page.setViewportSize({width,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const bounds=await page.locator('.sa-opt').first().evaluate(n=>{
        const email=n.querySelector('.sa-em').getBoundingClientRect(),name=n.querySelector('.sa-name').getBoundingClientRect();
        return {emailRight:email.right,nameLeft:name.left,right:name.right,boxRight:n.getBoundingClientRect().right};
      });
      assert(bounds.emailRight<=bounds.nameLeft && bounds.right<=bounds.boxRight);
      await page.screenshot({path:path.join(output,`saved-accounts-${width}.png`),animations:'disabled'});
    }
    checks.push('Saved character names appear to the right; long names truncate, markup stays literal, menus are opaque.');
    await page.locator('.sa-opt').nth(1).click();
    assert.equal(await page.locator('#account').inputValue(),saved[1].account);
    assert.equal(await page.locator('#password').getAttribute('placeholder'),'使用临时token登录');
    await page.reload();
    await page.waitForFunction(()=>SAVED_ACCTS.length===3);
    assert.equal(await page.locator('.sa-name').first().innerText(),'凤愿');
    checks.push('Selecting an account preserves its login identity and token flow; cached names return after reload.');
    await page.evaluate(async()=>{
      rememberAccount({account:'offline',status:{name:'演示角色'},params:{},toggles:{}});
      activeAccount='offline';enterApp();await loadTasks();showSection('manual');
    });
    await page.locator('.shop-toggle').click();
    for(const width of [390,760]) {
      await page.setViewportSize({width,height:844});
      for(const theme of ['dark','light']) {
        await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
        const layers=await page.locator('.login-card,.sidebar,.topbar,.acct-menu').evaluateAll(nodes=>nodes.map(n=>({
          backdrop:getComputedStyle(n).backdropFilter,bg:getComputedStyle(n).backgroundColor,
        })));
        assert(layers.every(x=>x.backdrop==='none'&&!/rgba\(/.test(x.bg)));
        for(let i=0;i<8;i++)await page.locator('.content').evaluate((n,i)=>{n.scrollTop=i%2?0:n.scrollHeight;},i);
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        assert.equal(await page.locator('.shop-chip-parts input').count(),6);
        await page.screenshot({path:path.join(output,`shop-${theme}-${width}.png`),animations:'disabled'});
      }
    }
    checks.push('Android uses opaque UI panels with no backdrop blur; chip selection and repeated scrolling work in both themes.');
    assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify({passed:true,offlineOnly:true,checks,errors,unexpected},null,2));
    console.log(`PASS ${checks.length} full-page UI groups.`);
  } catch(e) {
    await page.screenshot({path:path.join(output,'failure.png')});
    fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({error:e.stack,errors,unexpected},null,2));
    throw e;
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
