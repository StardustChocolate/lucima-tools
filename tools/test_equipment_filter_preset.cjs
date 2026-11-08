/* 工具箱默认方案迁移与 JSON 导入导出离线检查，不连接游戏。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let runtime;
for (const name of ['playwright', process.env.PLAYWRIGHT_MODULE, '/Users/sk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'].filter(Boolean)) {
  try { runtime = require(name); break; } catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
}
if (!runtime) throw Error('需要 Playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'build/equipment-followup-review');
const source = JSON.parse(fs.readFileSync(path.join(root, 'frontend/presets/equipment.json'), 'utf8'));
const storageKey = 'ark_equipment_filter_schemes_v2';
const revisionKey = 'ark_equipment_filter_preset_revision';
const origin = 'https://equipment-preset.invalid';
(async () => {
  fs.mkdirSync(output, {recursive: true});
  const launch = {headless: true};
  const chrome = process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if (fs.existsSync(chrome)) launch.executablePath = chrome;
  const browser = await runtime.chromium.launch(launch);
  const checks = [], errors = [], unexpected = [];
  async function scenario(name, initialSettings, run) {
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    let settings = structuredClone(initialSettings), writes = 0;
    page.on('pageerror', error => errors.push(`${name}: ${error.message}`));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      const json = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
      if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
      if (url.pathname === '/api/preferences') {
        if (request.method() === 'POST') { writes++; settings = request.postDataJSON().settings; }
        return json({settings});
      }
      if (url.pathname.startsWith('/api/')) {
        const responses = {
          '/api/config': {platform: 'android', version: '1.3.1-beta'},
          '/api/accounts': {accounts: []}, '/api/logs/auto': {entries: [], logSeq: 0},
          '/api/tasks': {toggle: [], manual: [], buyTargets: {}}, '/api/status': {status: {}},
        };
        if (!(url.pathname in responses)) unexpected.push(url.pathname);
        return json(responses[url.pathname] || {});
      }
      const relative = url.pathname === '/' ? 'frontend/index.html'
        : url.pathname.startsWith('/static/') ? 'frontend/' + decodeURIComponent(url.pathname.slice(8))
        : url.pathname.startsWith('/equipment/') ? 'equipment/web/' + url.pathname.slice(11)
        : decodeURIComponent(url.pathname.slice(1));
      const file = path.resolve(root, relative);
      if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { unexpected.push(url.pathname); return route.abort(); }
      const mime = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2'};
      return route.fulfill({body: fs.readFileSync(file), contentType: mime[path.extname(file)] || 'application/octet-stream'});
    });
    async function ready() {
      await page.goto(origin);
      await page.waitForFunction(() => document.querySelector('#toolboxHome')?.dataset.bound === '1');
      await page.evaluate(() => { enterApp(); showSection('toolbox'); showToolboxPage('equipment-filter'); setDrawer(false); });
      await page.locator('#activeSchemeName').waitFor();
    }
    try {
      await ready();
      await run({page, ready, settings: () => settings, writes: () => writes});
      checks.push(name);
    } finally { await context.close(); }
  }
  try {
    let revision;
    await scenario('首次启动仅有装备方案，全部规则原样载入且可导入导出', {}, async ({page, settings, writes}) => {
      assert.deepEqual(await page.evaluate(() => EQUIP_FILTER_SCHEMES_DATA), [source.scheme]);
      assert.equal(await page.locator('#schemeList .scheme-item').count(), 1);
      assert.equal(await page.locator('#activeSchemeName').innerText(), source.scheme.name);
      assert.equal(await page.locator('#schemeCount').innerText(), '1 个方案');
      assert((await page.locator('#schemeList').innerText()).includes(`${Object.values(source.scheme.slots).reduce((n,rs)=>n+rs.length,0)} 条细则`));
      const converted = await page.evaluate(raw => {
        const parsed = parseEquipmentFilterImport(raw);
        return {normalized: normalizeEquipmentFilterScheme(parsed), exported: equipmentFilterExportPayload(activeFilterScheme()), fallback: defaultEquipmentFilterSchemes()};
      }, JSON.stringify(source));
      assert.deepEqual(converted.normalized, source.scheme, '每条主副属性、混合条件、评分和套装均不被归一化改变');
      assert.deepEqual(converted.exported, source);
      assert.deepEqual(converted.fallback, [source.scheme]);
      assert.deepEqual(JSON.parse(settings()[storageKey]), [source.scheme]);
      assert.equal(writes(), 1);
      revision = settings()[revisionKey];
      assert(revision.startsWith('equipment-doc-'));
      for (const [slot, rules] of Object.entries(source.scheme.slots)) {
        await page.locator(`#equipSlotTabs [data-slot=${slot}]`).click();
        assert.equal(await page.locator('#detailRuleCircles [data-detail-rule]').count(), rules.length, slot);
      }
      await page.locator('#equipSlotTabs [data-slot=weapon]').click();
      await page.screenshot({path: path.join(output, 'toolbox-equipment-preset-390.png'), animations: 'disabled'});
      assert.deepEqual(await page.evaluate(() => getCustomizedEquipCfg().schemeId), source.scheme.id);
    });
    const oldSettings = {
      [revisionKey]: 'equipment-doc-1026-ecaa2cbc24aae009',
      [storageKey]: JSON.stringify([{id: 'plan-old-a', name: '旧方案A', slots: {}}, {id: 'plan-old-b', name: '旧方案B', slots: {}}]),
      ark_equipment_role_exclusions_v1: JSON.stringify({account: {role: ['other-role']}}),
      ark_theme: JSON.stringify({mode: 'light', accent: '#ff7fac', accent2: '#a266ff'}),
    };
    await scenario('已有安装只替换旧方案列表，保留主题和角色排除；再次启动不重复迁移', oldSettings, async ({page, settings, writes, ready}) => {
      assert.deepEqual(await page.evaluate(() => EQUIP_FILTER_SCHEMES_DATA), [source.scheme]);
      assert.deepEqual(JSON.parse(settings()[storageKey]), [source.scheme]);
      assert.equal(settings().ark_equipment_role_exclusions_v1, oldSettings.ark_equipment_role_exclusions_v1);
      assert.equal(settings().ark_theme, oldSettings.ark_theme);
      assert.equal(settings()[revisionKey], revision);
      assert.equal(writes(), 1);
      await ready();
      assert.equal(writes(), 1);
      assert.deepEqual(await page.evaluate(() => EQUIP_FILTER_SCHEMES_DATA), [source.scheme]);
    });
    const edited = structuredClone(source.scheme);
    edited.name = '装备（已调整）'; edited.slots.weapon[0].minScore = 42;
    await scenario('迁移后的用户编辑和新建方案重启仍保留', {
      [revisionKey]: revision,
      [storageKey]: JSON.stringify([edited, {id: 'plan-new', name: '之后新建', slots: {}}]),
    }, async ({page, ready, writes}) => {
      assert.deepEqual(await page.evaluate(() => EQUIP_FILTER_SCHEMES_DATA), [edited, {id: 'plan-new', name: '之后新建', slots: {}}]);
      assert.equal(writes(), 0);
      await page.evaluate(() => { EQUIP_FILTER_SCHEMES_DATA[0].slots.weapon[0].minScore = 43; saveEquipmentFilterSchemes(); });
      await page.evaluate(async () => { await flushFrontendSettings(); });
      await ready();
      assert.equal(await page.evaluate(() => EQUIP_FILTER_SCHEMES_DATA[0].slots.weapon[0].minScore), 43);
      assert.equal(await page.locator('#schemeList .scheme-item').count(), 2);
    });
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    fs.writeFileSync(path.join(output, 'preset-verification.json'), JSON.stringify({passed: true, offlineOnly: true, checks, errors, unexpected}, null, 2));
    console.log(`PASS: ${checks.length} equipment preset migration and JSON checks.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
