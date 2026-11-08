"""Real multi-account navigation and Android long-page rendering, offline only."""
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend.appcore import tasks


ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'build/account-switch-render-review'
ORIGIN = 'https://account-switch.invalid'
ANDROID_UA = ('Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 '
              'Chrome/120.0.0.0 Mobile Safari/537.36')


def snapshot(account, *, revision=1, exp=12):
    first = 26 if account == 'alpha' else 32
    members = [dict(roleId=f'{account}-{i}', staticId=f'H{first+i:03}',
                    name=f'{account}角色{i}', avatar=f'/assets/avatars/H{first+i:03}.png',
                    intimacyLevel=7, intimacyExp=exp+i, intimacyMax=250)
               for i in range(6)]
    return {'running': True, 'revision': revision,
            'progress': {'phase': 'battle', 'teams': {'upper': members[:3], 'lower': members[3:]}}}


class AccountSwitchUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        cls.browser = cls.playwright.chromium.launch(headless=True, **(
            {'executable_path': str(chrome)} if chrome.is_file() else {}))
        cls.catalog = tasks()
        REVIEW.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844},
                                                device_scale_factor=3, user_agent=ANDROID_UA,
                                                is_mobile=True, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(4000)
        self.errors, self.unexpected, self.calls = [], [], []
        self.snapshots = {account: snapshot(account) for account in ('alpha', 'beta')}
        self.hold_tasks, self.hold_log_account = False, None
        self.notification_result = None
        self.request_bodies = []
        self.pending_tasks, self.pending_logs = [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.context.route('**/*', self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && equipmentNotificationReady")
        self.page.evaluate("""async () => {
          for (const account of ['alpha', 'beta']) rememberAccount({account,status:{name:account}});
          activeAccount='alpha'; enterApp(); await loadTasks(); renderAccountSwitcher(); showSection('manual');
        }""")

    def tearDown(self):
        # Resolve deliberately held local requests before the context closes.
        for route in self.pending_tasks + self.pending_logs:
            route.abort()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        if f'{url.scheme}://{url.netloc}' != ORIGIN:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        path = unquote(url.path)
        body = route.request.post_data_json if route.request.method == 'POST' else {}
        account = body.get('account') or parse_qs(url.query).get('account', [''])[0]
        self.calls.append((path, account))
        self.request_bodies.append((path,deepcopy(body)))
        def send(value):
            route.fulfill(content_type='application/json', body=json.dumps(value))
        if path == '/api/tasks':
            if self.hold_tasks:
                self.pending_tasks.append(route)
            else:
                send(self.catalog)
            return
        if path == '/api/logs/auto':
            if account == self.hold_log_account:
                self.pending_logs.append(route)
            else:
                send({'entries': [], 'logSeq': 0, 'intimacy': self.snapshots[account]})
            return
        if path == '/api/intimacy/status':
            send({'intimacy': self.snapshots[account]})
            return
        if path.startswith('/api/intimacy/'):
            if path.endswith('/start') and not self.snapshots[account]['running']:
                self.snapshots[account] = snapshot('beta', revision=self.snapshots[account]['revision'] + 1, exp=40)
            send({'ok': True, 'intimacy': self.snapshots[account]})
            return
        if path == '/api/equipment-notifications/open' and self.notification_result:
            send(self.notification_result)
            return
        if path == '/api/login/saved':
            send({'account': account, 'status': {'name': account}})
            return
        if path.startswith('/api/'):
            responses = {
                '/api/config': {'version': '1.3.2', 'platform': 'android'},
                '/api/preferences': {'settings': {}},
                '/api/accounts': {'accounts': [{'account': name, 'name': name, 'hasToken': True}
                                               for name in ('alpha', 'beta', 'gamma')]},
                '/api/accounts/restore': {'accounts': []},
                '/api/roster': {'heroes': [], 'items': []},
                '/api/status': {'status': {}},
                '/api/equipment/action': {'roles': [], 'equips': [], 'artifacts': [], 'stats': [], 'sets': []},
            }
            send(responses.get(path, {'ok': True}))
            return
        rel = ('frontend/index.html' if path == '/' else 'frontend/' + path[8:]
               if path.startswith('/static/') else 'equipment/web/' + path[11:]
               if path.startswith('/equipment/') else path.lstrip('/'))
        file = (ROOT / rel).resolve()
        if ROOT not in file.parents or not file.is_file():
            route.fulfill(status=404, body='')
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    def seed_teams(self):
        self.page.evaluate("values => {for(const [email,value] of Object.entries(values)) applyIntimacy(email,value);}", self.snapshots)

    def test_login_prefills_the_last_successful_account(self):
        self.page.evaluate("frontendStorageSet('last_login_account','beta')")
        self.page.evaluate("initAccounts()")
        self.page.wait_for_function("document.getElementById('account').value==='beta'")
        self.assertEqual(self.page.locator('#password').get_attribute('placeholder'),'使用临时token登录')
        self.page.evaluate("frontendStorageSet('last_login_account','deleted@example.invalid')")
        self.page.evaluate("() => {$('account').value=''; fillLastLoginAccount();}")
        self.assertEqual(self.page.locator('#account').input_value(),'')

    def test_long_background_logs_are_bounded_and_render_only_when_opened(self):
        result=self.page.evaluate("""() => {
          showSection('manual'); const observer=new MutationObserver(()=>{});
          observer.observe($('section-logs'),{childList:true,subtree:true});
          for(let i=0;i<1200;i++)logTo(activeAccount,'长时间运行测试 '+i);
          const result={rows:activeAcc().logRows.length,mutations:observer.takeRecords().length};
          observer.disconnect(); return result;
        }""")
        self.assertEqual(result,{'rows':500,'mutations':0})
        self.page.evaluate("showSection('logs')")
        self.page.wait_for_function("document.getElementById('log').textContent.includes('长时间运行测试 1199')")
        self.assertNotIn('长时间运行测试 0\n',self.page.locator('#log').inner_text())
        self.page.locator('#clearLogBtn').click()
        self.page.wait_for_function("document.getElementById('log').textContent === ''")
        self.assertEqual(self.page.evaluate('activeAcc().logRows.length'),0)

    def test_identical_friendship_poll_preserves_existing_portrait_nodes(self):
        self.seed_teams()
        self.page.evaluate("window.originalPortrait=document.querySelector('.intimacy-member')")
        self.seed_teams()
        self.assertTrue(self.page.evaluate("originalPortrait===document.querySelector('.intimacy-member')"))

    def test_switch_repaints_cached_team_without_waiting_for_task_catalog(self):
        self.seed_teams()
        self.hold_tasks = True
        self.page.evaluate("switchAccount('beta')")
        # This is deliberately synchronous: no previous account portraits may
        # remain while a catalog/network request is outstanding.
        labels = self.page.locator('.intimacy-member').evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("aria-label"))')
        self.assertEqual(labels, [f'beta角色{i}' for i in range(6)])
        self.assertEqual(self.page.locator('.intimacy-start').inner_text(), '中止')
        self.page.evaluate("switchAccount('alpha')")
        self.assertEqual(self.page.locator('.intimacy-member').first.get_attribute('aria-label'), 'alpha角色0')
        self.assertEqual(self.page.evaluate('currentSection'), 'manual')

    def test_slow_account_does_not_block_other_account_progress_poll(self):
        self.seed_teams()
        self.hold_log_account = 'alpha'
        self.snapshots['beta'] = snapshot('beta', revision=2, exp=65)
        self.page.evaluate("activeAccount='beta'; renderManualPanel(TASKS); void pollAutoLogs();")
        self.page.wait_for_function("document.querySelector('.intimacy-exp')?.textContent === '65/250'")
        self.assertTrue(self.pending_logs)
        self.assertEqual(self.page.locator('.intimacy-member').first.get_attribute('aria-label'), 'beta角色0')
        self.snapshots['beta'] = snapshot('beta', revision=3, exp=90)
        self.page.evaluate("ACCOUNTS.beta.lastUIPollAt=0; void pollAutoLogs()")
        self.page.wait_for_function("document.querySelector('.intimacy-exp')?.textContent === '90/250'")

    def test_switch_reads_latest_worker_snapshot_without_periodic_poll(self):
        self.seed_teams()
        self.snapshots['beta'] = snapshot('beta', revision=2, exp=80)
        self.page.evaluate("switchAccount('beta')")
        self.page.wait_for_function("document.querySelector('.intimacy-exp')?.textContent === '80/250'")
        self.assertIn(('/api/intimacy/status', 'beta'), self.calls)
        self.assertFalse(self.page.evaluate('!!autoLogTimer'))

    def test_older_snapshot_cannot_replace_newer_progress(self):
        self.page.evaluate("v => {applyIntimacy('alpha', v.new); applyIntimacy('alpha', v.old);}", {
            'new': snapshot('alpha', revision=9, exp=90), 'old': snapshot('alpha', revision=8, exp=20)})
        self.assertEqual(self.page.locator('.intimacy-exp').first.inner_text(), '90/250')

    def test_add_account_flow_keeps_existing_workers_and_shows_its_own_team(self):
        self.seed_teams()
        # Follow the sidebar's actual add-account/login controls.
        self.page.evaluate('setDrawer(true)')
        self.page.locator('#accCurrentBtn').click()
        self.page.locator('#accAddBtn').click()
        self.page.locator('#account').fill('gamma')
        self.snapshots['gamma'] = {'running': False, 'revision': 0, 'progress': None}
        self.page.locator('#loginBtn').click()
        self.page.wait_for_function("activeAccount === 'gamma'")
        self.page.locator('.intimacy-start').click()
        self.page.wait_for_function("document.querySelector('.intimacy-exp')?.textContent === '40/250'")
        self.assertEqual(self.page.evaluate('Object.keys(ACCOUNTS)'), ['alpha', 'beta', 'gamma'])
        self.assertTrue(self.page.evaluate('intimacyState.alpha.running && intimacyState.beta.running'))
        self.assertIn(('/api/intimacy/start', 'gamma'), self.calls)
        self.assertFalse(any(path == '/api/intimacy/cancel' for path, _ in self.calls))

    def test_android_auto_page_expansion_and_navigation_keep_content_painted(self):
        self.page.evaluate("showSection('auto')")
        # Do not mask production animations with screenshot/test CSS.
        style = self.page.locator('#section-auto').evaluate('n=>{const s=getComputedStyle(n);return {animation:s.animationName,opacity:s.opacity,transform:s.transform};}')
        self.assertEqual(style, {'animation': 'none', 'opacity': '1', 'transform': 'none'})
        toggle = self.page.locator('.shop-free-toggle')
        toggle.click()
        targets = self.page.locator('#auto-shop_free').get_by_text('自动购买目标', exact=True)
        self.assertTrue(targets.is_visible())
        for theme in ('dark', 'light'):
            self.page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
            for index in range(8):
                self.page.evaluate("""i=>{
                  showSection(i%2?'manual':'info'); showSection('auto');
                  document.querySelector('.content').scrollTop=i%2?0:document.querySelector('.content').scrollHeight;
                }""", index)
                self.assertEqual(self.page.locator('#section-auto').evaluate('n=>getComputedStyle(n).opacity'), '1')
                self.assertEqual(self.page.locator('#section-auto').evaluate('n=>getComputedStyle(n).transform'), 'none')
                self.assertTrue(self.page.locator('.topbar').is_visible())
            self.page.locator('#auto-shop_free').scroll_into_view_if_needed()
            self.assertTrue(toggle.is_visible())
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
            self.page.screenshot(path=str(REVIEW / f'auto-expanded-{theme}.png'))
        self.assertFalse(targets.is_visible())

    def test_native_task_raster_mode_tracks_navigation_and_older_packages_keep_working(self):
        self.page.evaluate("window.renderModes=[];window.LucimaRendering={setTaskMode:value=>renderModes.push(value)}")
        sections=['info','auto','manual','equipment','toolbox','manual','settings']
        for section in sections:
            self.page.evaluate('section=>showSection(section)',section)
        self.assertEqual(self.page.evaluate('renderModes'),[False] * len(sections))
        self.page.evaluate("delete window.LucimaRendering;showSection('auto');showSection('manual')")
        self.assertTrue(self.page.locator('#section-manual').evaluate('n=>n.classList.contains("active")'))

    def test_automatic_shop_chip_parts_persist_per_account_and_all_excluded_stays_checked(self):
        self.page.evaluate("showSection('auto')")
        panel=self.page.locator('#auto-shop_free')
        panel.locator('.shop-free-toggle').click()
        for sid in ('EC11','EC51'):
            panel.locator(f'[data-free-chip-exclude="{sid}"]').locator('..').click()
        self.assertEqual(self.page.evaluate('ACCOUNTS.alpha.params.shop_free.wanted'),['5','6','EC21','EC31','EC41','EC61'])
        self.page.evaluate("switchAccount('beta')")
        self.assertEqual(self.page.locator('[data-free-chip-exclude]:checked').count(),0)
        self.page.evaluate("switchAccount('alpha')")
        panel.locator('.shop-free-toggle').click()
        self.assertEqual(panel.locator('[data-free-chip-exclude]:checked').count(),2)
        for box in panel.locator('[data-free-chip-exclude]').all():
            if not box.is_checked():box.locator('..').click()
        for key in ('recruit','mystery'):
            panel.locator(f'[data-free-wkey="{key}"]').locator('..').click()
        self.assertEqual(self.page.evaluate('ACCOUNTS.alpha.params.shop_free.wanted'),[])
        self.page.evaluate('renderAutoPanel(TOGGLE_TASKS)')
        self.assertTrue(panel.locator('[data-free-wkey="chip"]').is_checked())
        panel.locator('.shop-free-toggle').click()
        self.assertEqual(panel.locator('[data-free-chip-exclude]:checked').count(),6)
        for width in (320,390):
            self.page.set_viewport_size({'width':width,'height':844})
            boxes=[node.bounding_box() for node in panel.locator('.shop-chip-part').all()]
            self.assertEqual(len({round(box['y'])for box in boxes}),1)
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        self.page.screenshot(path=str(ROOT/'build/user-revision-1028/auto-chip-exclusions.png'))

    def test_saved_account_delete_cross_is_visible_without_hover_and_does_not_select_it(self):
        self.page.evaluate('openAddAccount()')
        self.page.locator('#acctCaret').click()
        cross=self.page.locator('.sa-del[data-del="gamma"]')
        self.assertTrue(cross.is_visible())
        self.assertGreater(float(cross.evaluate('n=>getComputedStyle(n).opacity')),.5)
        # Android WebViews without a WebChromeClient silently cancel confirm().
        self.page.evaluate('window.confirm = () => false')
        cross.click()
        self.page.wait_for_function("!SAVED_ACCTS.some(account=>account.account==='gamma')")
        self.assertEqual(self.page.locator('#account').input_value(),'')
        self.assertTrue(self.page.evaluate("!!ACCOUNTS.alpha&&!!ACCOUNTS.beta"))
        self.assertIn(('/api/accounts/delete','gamma'),self.calls)
        self.assertEqual(self.page.locator('.sa-del[data-del="gamma"]').count(),0)

    def test_sale_epic85_toggle_is_opt_in_scoped_and_keeps_the_rule_text_current(self):
        panel=self.page.locator('#sweep-hunt_sweep')
        panel.locator('.sweep-toggle').click()
        include=panel.locator('.sweep-sell-epic85')
        self.assertFalse(include.is_checked())
        self.assertIn('及85级史诗',panel.locator('.sweep-sale-hint').inner_text())
        include.locator('..').click()
        self.assertTrue(self.page.evaluate("getSweepCfg('hunt_sweep').includeEpic85"))
        self.assertIn('同时卖出85级史诗',panel.locator('.sweep-sale-hint').inner_text())
        self.page.evaluate("switchAccount('beta')")
        self.assertFalse(include.is_checked())
        self.page.evaluate("switchAccount('alpha')")
        panel.locator('.sweep-toggle').click()
        self.assertTrue(include.is_checked())
        for width in (320,390):
            self.page.set_viewport_size({'width':width,'height':844})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
            toggles=panel.locator('.sweep-ticket-sales .target-score-toggle')
            boxes=[label.bounding_box()for label in toggles.all()]
            self.assertEqual(len(boxes),3)
            self.assertLess(max(b['y']+b['height']/2 for b in boxes)-min(b['y']+b['height']/2 for b in boxes),2)
            self.assertEqual(panel.locator('.sweep-sale-separator').inner_text(),'—')
            right=panel.locator('.sweep-ticket-sales').bounding_box()
            self.assertLessEqual(abs(boxes[-1]['x']+boxes[-1]['width']-right['x']-right['width']),2)
            self.assertTrue(panel.locator('.sweep-ticket-sales input[type="checkbox"]').evaluate_all("""nodes=>nodes.every(node=>
              getComputedStyle(node).position === 'static' && node.offsetWidth >= 18
            )"""))
        self.page.screenshot(path=str(ROOT/'build/user-revision-1028/sweep-sale-options.png'))

    def test_notification_opens_each_correct_account_purchase_panel_and_never_auto_buys(self):
        card={'equipmentKey':'a'*64,'index':0,'shopIndex':0,'staticID':'S001','name':'通知靴子',
              'level':85,'classLV':4,'tierName':'传说','enhance':0,'set':'Speed','setCN':'速度',
              'slot':'boots','slotCN':'靴子','auxScore':32,'mainProp':{'label':'速度','text':'40'},
              'subProps':[{'label':'攻击','text':'8%'}]}
        for source,selector,buy_path in [('shop_free','#auto-shop_free','/api/shop/buy'),
                ('shop_diamond','#shop-shop_diamond','/api/shop/buy'),
                ('star_source_shop','#starshop-star_source_shop','/api/star-source/buy')]:
            self.notification_result={'ok':True,'account':'beta','source':source,'cards':[card],'stale':False}
            before=len([path for path,_ in self.calls if path in ('/api/shop/buy','/api/star-source/buy')])
            self.page.evaluate("openEquipmentNotification('"+'b'*32+"')")
            self.assertEqual(self.page.evaluate('activeAccount'),'beta')
            panel=self.page.locator(selector)
            buy=panel.locator('.shop-free-buy, .secret-shop-buy, .star-source-buy')
            self.assertTrue(buy.is_visible())
            self.assertEqual(len([path for path,_ in self.calls if path in ('/api/shop/buy','/api/star-source/buy')]),before)
            buy.click()
            self.page.wait_for_function("[...document.querySelectorAll('button')].some(n=>n.textContent==='已购买')")
            body=next(body for path,body in reversed(self.request_bodies) if path==buy_path)
            self.assertEqual(body['account'],'beta')
            self.assertEqual(body['expectedEquipmentKey'],'a'*64)

    def test_expanded_equipment_form_uses_direct_disabled_colors_without_alpha_layers(self):
        panel=self.page.locator('.customized-equip-widget')
        panel.locator('.customized-equip-toggle').click()
        self.page.wait_for_function("!document.querySelector('.customized-equip-widget')._customizedAction")
        self.page.evaluate("""()=>{
          const root=document.querySelector('.customized-equip-widget');
          updateCustomizedEquipWidget(root.id.slice('customizedequip-'.length),{stage:'ChooseSet'});
        }""")
        for theme in ('dark','light'):
            self.page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
            disabled=panel.locator('button:disabled')
            self.assertGreaterEqual(disabled.count(),5)
            styles=disabled.evaluate_all("""nodes=>nodes.map(n=>{const s=getComputedStyle(n);return {
              opacity:s.opacity,transition:s.transitionDuration,transform:s.transform,
              filter:s.filter,shadow:s.boxShadow,background:s.backgroundImage};})""")
            self.assertTrue(all(style=={'opacity':'1','transition':'0s','transform':'none',
                'filter':'none','shadow':'none','background':'none'}for style in styles),styles)
            enabled=panel.locator('.customized-set-button')
            self.assertTrue(enabled.is_enabled())
            enabled.click()
            self.assertTrue(panel.locator('.customized-set-popover').is_visible())
            enabled.click()
            self.assertFalse(panel.locator('.customized-set-popover').is_visible())
        panel.scroll_into_view_if_needed()
        self.page.screenshot(path=str(ROOT/'build/android-paint-tiles-review/equipment-form.png'))

    def test_chip_selection_in_both_shop_panels_uses_static_checkmarks(self):
        for section, root, toggle, chip in (
                ('auto', '#auto-shop_free', '.shop-free-toggle', 'input[value="chip"]'),
                ('manual', '#shop-shop_diamond', '.shop-toggle', 'input[data-wkey="chip"]')):
            self.page.evaluate('section => showSection(section)', section)
            panel = self.page.locator(root)
            panel.locator(toggle).click()
            label = panel.locator(chip).locator('..')
            for _ in range(4):
                was_checked = panel.locator(chip).is_checked()
                label.click()
                self.assertEqual(panel.locator(chip).is_checked(), not was_checked)
                styles = label.locator('.check-box, .check-box svg').evaluate_all('''nodes=>nodes.map(n=>{
                  const s=getComputedStyle(n);return {animation:s.animationName,transition:s.transitionDuration,transform:s.transform};})''')
                self.assertTrue(all(style == {'animation': 'none', 'transition': '0s', 'transform': 'none'} for style in styles))
                self.assertEqual(label.locator('svg').evaluate('n=>getComputedStyle(n).visibility'),
                                 'hidden' if was_checked else 'visible')
                self.assertEqual(self.page.locator(f'#section-{section}').evaluate('n=>getComputedStyle(n).opacity'), '1')
            if section == 'manual':
                exclusions = panel.locator('[data-chip-exclude]')
                for index in range(exclusions.count()):
                    exclusions.nth(index).locator('..').click()
                    self.assertTrue(exclusions.nth(index).is_checked())
                self.assertEqual(len(self.page.evaluate('getShopCfg().excludedChipIds')), 6)
            self.page.screenshot(path=str(REVIEW / f'chips-{section}.png'))

    def test_legendary_equipment_controls_expand_without_squeezing_the_task_page(self):
        for width in (320, 390, 600):
            self.page.set_viewport_size({'width': width, 'height': 844})
            for section, root, toggle, checkbox, controls in (
                    ('auto', '#auto-shop_free', '.shop-free-toggle', '.free-equip-on', '.shop-free-equip-controls'),
                    ('manual', '#shop-shop_diamond', '.shop-toggle', '.leg-on', '.shop-equip-scheme-wrap')):
                self.page.evaluate('section=>showSection(section)', section)
                panel = self.page.locator(root)
                if panel.locator('.shop-body' if section == 'manual' else '.auto-body').evaluate('n=>n.classList.contains("hidden")'):
                    panel.locator(toggle).click()
                for _ in range(3):
                    if panel.locator(checkbox).is_checked(): panel.locator(checkbox).locator('..').click()
                    panel.locator(checkbox).locator('..').click()
                    self.assertTrue(panel.locator(controls).is_visible())
                    self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    bounds=panel.locator(controls).bounding_box()
                    self.assertGreater(bounds['width'], 150)
                    self.assertLessEqual(bounds['x']+bounds['width'], width)
                if section == 'auto':
                    self.assertGreater(panel.locator('.auto-head .tinfo').bounding_box()['width'], 100)
                panel.locator('.shop-equip-scheme .dd-btn, .shop-free-equip-scheme .dd-btn').click()
                self.assertTrue(panel.locator('.dd-menu:not(.hidden)').is_visible())
                panel.locator('.dd-menu:not(.hidden) .dd-opt').first.click()
                self.page.screenshot(path=str(REVIEW / f'legendary-{section}-{width}.png'))
        self.page.evaluate("showSection('guild')")
        self.assertEqual(self.page.locator('.topbar').bounding_box()['y'],0)
        self.page.evaluate("showSection('toolbox')")
        self.assertEqual(self.page.locator('.topbar').bounding_box()['y'],0)

    def test_hidden_shop_controls_cannot_scroll_toolbar_outside_the_touch_scroller(self):
        for section, root, toggle, selectors in (
                ('auto', '#auto-shop_free', '.shop-free-toggle', ['input[value="chip"]', '.free-equip-on']),
                ('manual', '#shop-shop_diamond', '.shop-toggle', ['[data-wkey="chip"]', '[data-chip-exclude]', '.leg-on'])):
            self.page.evaluate('value=>showSection(value)', section)
            panel=self.page.locator(root)
            panel.locator(toggle).click()
            for selector in selectors:
                control=panel.locator(selector).first
                if selector == '[data-chip-exclude]' and not panel.locator('[data-wkey="chip"]').is_checked():
                    panel.locator('[data-wkey="chip"]').locator('..').tap()
                label=control.locator('..')
                label.scroll_into_view_if_needed()
                # Focusing is intentional: mouse clicks can skip native focus
                # scrolling, which is why the earlier visual tests missed this.
                control.evaluate('input=>input.focus()')
                label.tap()
                metrics=self.page.evaluate('''() => ({
                  main:document.querySelector('.main').scrollTop,
                  root:document.scrollingElement.scrollTop,body:document.body.scrollTop,
                  toolbar:document.querySelector('.topbar').getBoundingClientRect().top})''')
                self.assertEqual(metrics, {'main':0,'root':0,'body':0,'toolbar':0})
                input_box=control.bounding_box();label_box=label.bounding_box()
                self.assertGreaterEqual(input_box['y'],label_box['y'])
                self.assertLessEqual(input_box['y']+input_box['height'],label_box['y']+label_box['height'])
                self.page.locator('.content').evaluate('node=>node.scrollTop=0')
                self.assertEqual(self.page.locator('.topbar').bounding_box()['y'],0)
                self.assertGreaterEqual(self.page.locator(f'#section-{section}').bounding_box()['y'],
                                        self.page.locator('.topbar').bounding_box()['height'])


if __name__ == '__main__':
    unittest.main()
