/* Production-mode startup regression. Every API and asset request stays offline. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let playwright;
for (const name of ['playwright', process.env.PLAYWRIGHT_MODULE, '/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'].filter(Boolean)) {
  try { playwright = require(name); break; } catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
}
if (!playwright) throw Error('Playwright is required.');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'build/direct-entry-review');
const host = 'direct-entry.invalid';

async function main() {
  fs.mkdirSync(output, {recursive: true});
  const options = {headless: true};
  const chrome = process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if (fs.existsSync(chrome)) options.executablePath = chrome;
  const browser = await playwright.chromium.launch(options);
  const results = [];
  try {
    for (const mode of ['unconfigured', 'network-error', 'required-update', 'current']) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      const page = await context.newPage();
      const calls = [], errors = [], unexpected = [];
      const updateCalls = () => calls.filter(item => item.path.startsWith('/api/update/'));
      page.setDefaultTimeout(10000);
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.install();
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url()), name = url.pathname;
        if (url.hostname !== host) { unexpected.push(url.href); return route.abort(); }
        const json = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
        if (name.startsWith('/api/')) {
          const body = request.method() === 'POST' ? request.postDataJSON() : null;
          calls.push({path: name, body});
          if (name.startsWith('/api/update/')) {
            if (mode === 'network-error') return route.abort('failed');
            const state = {
              testServer: false, checked: mode !== 'unconfigured', checking: false,
              available: mode === 'required-update', forced: mode === 'required-update',
              currentVersion: '1.3.0', latestVersion: mode === 'required-update' ? '1.4.0' : '1.3.0',
              lastError: mode === 'unconfigured' ? '更新服务未配置' : '',
              artifactAvailable: true, installSupported: true, installing: false,
              notes: [], progress: {phase: 'idle'},
            };
            return json(state);
          }
          const responses = {
            '/api/game-data/status': {phase:'ready',version:'catalog_115942',revision:0},
            '/api/accounts': {accounts: []}, '/api/preferences': {settings: {}},
            '/api/config': {platform: 'android', version: '1.3.0'},
            '/api/login': {account: 'offline', status: {name: '离线测试账号'}, params: {}, toggles: {}},
            '/api/accounts/start': {ok: true}, '/api/accounts/logout': {ok: true},
            '/api/tasks': {toggle: [], manual: [], buyTargets: {}},
            '/api/roster': {roles: [], items: []}, '/api/status': {status: {}},
            '/api/logs/auto': {entries: [], logSeq: 0},
            '/api/intimacy/status': {intimacy:{running:false,progress:null}}, '/api/encyclopedia/resources': {},
          };
          if (!(name in responses)) unexpected.push(name);
          return json(responses[name] || {});
        }
        const relative = name === '/' ? 'frontend/index.html' : name.startsWith('/static/') ? `frontend/${name.slice(8)}` : name.startsWith('/equipment/') ? `equipment/web/${name.slice(11)}` : name.slice(1);
        const file = path.resolve(root, relative);
        if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { unexpected.push(name); return route.abort(); }
        const mime = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.woff2': 'font/woff2'};
        return route.fulfill({body: fs.readFileSync(file), contentType: mime[path.extname(file)] || 'application/octet-stream'});
      });
      try {
        await page.goto(`https://${host}`);
        await page.waitForFunction(() => typeof checkForUpdates === 'function' && updateStartupReady);
        await page.locator('#loginView').waitFor({state: 'visible'});
        assert.equal(await page.locator('#loginBtn').isEnabled(), true);
        assert.equal(await page.locator('#updateModal').isHidden(), true);
        assert.equal(updateCalls().length, 0, 'startup makes no update request');
        await page.clock.fastForward(19000);
        await page.evaluate(() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); });
        await page.clock.fastForward(3600001);
        assert.equal(updateCalls().length, 0, 'old startup timers, foreground and hourly triggers stay disabled');
        assert.equal(await page.locator('#loginBtn').isEnabled(), true);
        assert.equal(await page.locator('#updateModal').isHidden(), true);
        if (mode === 'unconfigured') await page.screenshot({path: path.join(output, 'direct-entry-390.png'), animations: 'disabled'});

        await page.locator('#account').fill('offline');
        await page.locator('#password').fill('offline-test-password');
        await page.locator('#loginBtn').click();
        await page.locator('#appView').waitFor({state: 'visible'});
        assert.equal(calls.filter(item => item.path === '/api/login').length, 1, 'real login handler reaches the mocked API');
        assert.equal(updateCalls().length, 0, 'entering the app never depends on update status');

        await page.evaluate(() => showSection('about'));
        const beforeManual = updateCalls().length;
        await page.locator('#aboutUpdateBtn').click();
        if (mode === 'current') {
          await page.getByText('当前已是最新版本。', {exact: true}).waitFor();
          assert.equal(await page.locator('#updateModal').isHidden(), true);
        } else {
          await page.locator('#updateModal').waitFor({state: 'visible'});
          assert.equal(await page.locator('#updateIgnoreBtn').isVisible(), true, 'manual update prompt can always be dismissed');
          if (mode === 'required-update') {
            assert.equal(await page.locator('#updateIgnoreBtn').innerText(), '暂不更新');
            await page.locator('#updateIgnoreBtn').click();
            assert.equal(calls.some(item => item.path === '/api/update/ignore'), false, 'dismiss a server-required update locally');
          } else {
            assert.equal(await page.locator('#updateTitle').innerText(), '暂时无法检查更新');
            assert((await page.locator('#updateSummary').innerText()).includes('可继续使用'));
            await page.locator('#updateIgnoreBtn').click();
          }
          await page.locator('#updateModal').waitFor({state: 'hidden'});
          await page.locator('#aboutUpdateBtn').click();
          await page.locator('#updateModal').waitFor({state: 'visible'});
          await page.keyboard.press('Escape');
          await page.locator('#updateModal').waitFor({state: 'hidden'});
        }
        assert(updateCalls().length > beforeManual, 'manual entry still checks updates');
        const afterManual = updateCalls().length;
        await page.evaluate(() => logout());
        await page.locator('#loginView').waitFor({state: 'visible'});
        await page.clock.fastForward(20000);
        assert.equal(await page.locator('#loginBtn').isEnabled(), true, 'logout returns to usable login');
        assert.equal(await page.locator('#updateModal').isHidden(), true);
        assert.equal(updateCalls().length, afterManual, 'logout does not restart update checks');
        assert.deepEqual(errors, []);
        assert.deepEqual(unexpected, []);
        results.push({mode, passed: true, manualUpdateRequests: afterManual, pageErrors: errors, unexpectedRequests: unexpected});
      } catch (error) {
        await page.screenshot({path: path.join(output, `failure-${mode}.png`), animations: 'disabled'}).catch(() => {});
        fs.writeFileSync(path.join(output, `failure-${mode}.json`), JSON.stringify({error: error.stack, calls, errors, unexpected}, null, 2));
        throw error;
      } finally { await context.close(); }
    }
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({passed: true, productionMode: true, offlineOnly: true, results}, null, 2));
    console.log('PASS: production startup, failed/unconfigured/required/current update states, direct login, manual checks, dismiss and logout.');
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
