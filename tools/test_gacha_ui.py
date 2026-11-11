"""Actual desktop/mobile UI with local responses; no external network access."""
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from backend import gacha, runtime_data
from tools.test_gacha import NOW, POOL, ReplayClient

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://gacha-test.invalid'


class GachaUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(folder.name)))
        self.enterContext(patch.object(gacha.time, 'time', return_value=NOW))
        self.pools = [pool for pool in gacha.options(ReplayClient().account_state) if pool['id'] == POOL]
        self.account_pools = {email: deepcopy(self.pools) for email in ['first', 'second']}
        self.configs = {email: {} for email in ['first', 'second']}
        self.jobs = {email: {'state': 'idle', 'running': False, 'completed': 0, 'requested': 0,
                             'message': '等待开始', 'results': [], 'blocked': False}
                     for email in ['first', 'second']}
        self.requests, self.errors, self.unexpected = [], [], []
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 900}, has_touch=True)
        self.addCleanup(self.context.close)
        self.context.route('**/*', self.serve)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.open_app()

    def open_app(self):
        self.page.evaluate("""async () => {
            for (const account of ['first', 'second']) rememberAccount({account, status:{name:account}});
            activeAccount='first'; enterApp(); await loadTasks(); showSection('toolbox'); showToolboxPage('gacha');
        }""")
        self.page.wait_for_selector('#gachaStart')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = url.path
        body = route.request.post_data_json if route.request.method == 'POST' else None
        self.requests.append((path, body))
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path.startswith('/api/'):
            email = (body or {}).get('account') or parse_qs(url.query).get('account', ['first'])[0]
            if path == '/api/toggles':
                self.configs[email] = deepcopy(body['params']['gacha'])
            if path == '/api/gacha/start':
                self.configs[email] = deepcopy(body['params'])
                self.jobs[email] = {'id': body['requestId'], 'sequence': 1, 'revision': 1,
                                    'state': 'running', 'running': True, 'completed': 0,
                                    'requested': body['params']['count'], 'params': body['params'],
                                    'guaranteedCount': next(pool['guaranteedCount'] for pool in self.account_pools[email]
                                                            if pool['id'] == body['params']['poolId']),
                                    'message': '正在抽取第 1 次', 'results': [], 'blocked': False}
            elif path == '/api/gacha/cancel':
                self.jobs[email].update(state='stopped', running=False, message='已停止抽卡', revision=5)
            responses = {
                '/api/accounts': {'accounts': []}, '/api/preferences': {'settings': {}},
                '/api/config': {'platform': 'windows', 'version': 'test'},
                '/api/tasks': {'toggle': [], 'manual': []},
                '/api/update/status': {'testServer': True, 'available': False},
                '/api/update/check': {'testServer': True, 'available': False},
                '/api/resources/status': {'phase': 'ready'},
                '/api/logs/auto': {'entries': [], 'logSeq': 0, 'gacha': self.jobs[email]},
                '/api/gacha/options': {'pools': self.account_pools[email], 'config': self.configs[email], 'job': self.jobs[email], 'dataVersion': 'test'},
                '/api/gacha/start': {'job': self.jobs[email]}, '/api/gacha/cancel': {'job': self.jobs[email]},
                '/api/status': {'status': {}, 'nextClaims': {}},
                '/api/roster': {'heroes': [], 'items': []}, '/api/inventory': {'items': [], 'full': True},
                '/api/accounts/start': {'ok': True},
                '/api/toggles': {'ok': True, 'params': {'gacha': self.configs[email]}},
            }
            if path not in responses:
                self.unexpected.append(path)
            route.fulfill(content_type='application/json', body=json.dumps(responses.get(path, {})))
            return
        file = ROOT / 'frontend/index.html' if path == '/' else (
            ROOT / 'frontend' / path.removeprefix('/static/') if path.startswith('/static/') else ROOT / path.lstrip('/'))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file():
            self.unexpected.append(path)
            route.abort()
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def test_pool_targets_and_common_controls_render_without_auto_start(self):
        self.assertIn('蜜拉贝儿', self.page.locator('.gacha-pool .dd-cur').inner_text())
        self.assertIn('甜蜜的顾问', self.page.locator('.gacha-targets').inner_text())
        self.assertEqual(self.page.locator('.gacha-target-media img').count(), 2)
        self.assertEqual(self.page.locator('.gacha-currency').count(), 0)
        self.assertEqual(self.page.locator('.gacha-contract img').count(), 2)
        self.assertEqual(self.page.locator('[data-gacha-balance="TH806"]').inner_text(), '0')
        self.assertEqual(self.page.locator('[data-gacha-balance="5"]').inner_text(), '50')
        self.assertTrue(self.page.locator('#gachaStopRole').is_checked())
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertEqual(self.page.locator('#gachaPage .card').count(), 1)
        self.assertEqual(self.page.locator('#gachaStop, #gachaRecent, #gachaMessage, #gachaProgressRow').count(), 0)
        self.assertTrue(self.page.locator('#gachaPityField').is_visible())
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '1 / 120')
        self.assertTrue(self.page.locator('#gachaPityField').evaluate("""field => {
            const pool = document.querySelector('.gacha-pool').getBoundingClientRect();
            const targets = document.querySelector('.gacha-targets').getBoundingClientRect();
            const rect = field.getBoundingClientRect();
            return pool.bottom <= rect.top && rect.bottom <= targets.top;
        }"""))
        self.assertFalse(any(path == '/api/gacha/start' for path, _ in self.requests))
        self.page.locator('#gachaDrawCount').fill('237')
        self.page.evaluate("applyGachaJob('first', {state:'idle',running:false,message:'等待开始',results:[]})")
        self.assertEqual(self.page.locator('#gachaDrawCount').input_value(), '237')

    def test_current_queen_avatar_and_bond_image_load_on_mobile_and_desktop(self):
        self.account_pools['first'][0].update(
            name='【庆典女王艾乐莱思】招募机率UP',
            roles=[{'id': 'H195', 'name': '庆典女王艾乐莱思', 'image': '/assets/avatars/H195.png'}],
            artifacts=[{'id': 'A0443', 'name': '专属的乐趣', 'image': '/assets/gacha/artifacts/A0443.webp'}],
        )
        self.page.evaluate("loadGachaOptions('first')")
        self.page.wait_for_function("gachaState().data.pools[0].roles[0].id === 'H195'")
        for width in (390, 1024):
            self.page.set_viewport_size({'width': width, 'height': 900})
            images = self.page.locator('.gacha-target-media img')
            self.assertEqual(2, images.count())
            self.page.wait_for_function("[...document.querySelectorAll('.gacha-target-media img')].every(img => !img.hidden && img.complete && img.naturalWidth > 0)")
            self.assertEqual(['/assets/avatars/H195.png', '/assets/gacha/artifacts/A0443.webp'],
                             images.evaluate_all("images => images.map(img => img.getAttribute('src'))"))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertFalse(any(path == '/api/gacha/start' for path, _ in self.requests))

    def test_start_settings_and_stop_use_captured_account_and_single_job(self):
        self.page.locator('#gachaDrawCount').fill('25')
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaStopArtifact')).click()
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running")
        starts = [body for path, body in self.requests if path == '/api/gacha/start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['params'], {'poolId': POOL, 'count': 25,
                                               'stopRole': True, 'stopArtifact': True, 'preferExclusive': True})
        self.assertTrue(self.page.locator('#gachaDrawCount').is_disabled())
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_disabled())
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '停止')
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '1 / 120')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("!gachaState().job.running")
        self.assertFalse(self.page.locator('#gachaDrawCount').is_disabled())
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_enabled())
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '▶ 开始抽卡')
        self.assertTrue(self.page.locator('#gachaStart').is_enabled())
        self.assertEqual(sum(path == '/api/gacha/start' for path, _ in self.requests), 1)
        self.assertEqual(sum(path == '/api/gacha/cancel' for path, _ in self.requests), 1)

    def test_disabled_specific_priority_is_sent_and_restored_from_saved_settings(self):
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaPreferExclusive')).click()
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running && !gachaState().starting")
        starts = [body for path, body in self.requests if path == '/api/gacha/start']
        self.assertFalse(starts[0]['params']['preferExclusive'])
        self.assertFalse(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_disabled())
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("!gachaState().job.running")
        self.page.evaluate('async () => { gachaState().draft = null; await loadGachaOptions(); }')
        self.assertFalse(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_enabled())

    def test_replacing_expired_pool_and_editing_settings_survive_reload_without_starting(self):
        self.configs['first'] = {'poolId': 'ExpiredPool', 'count': 41, 'stopRole': True,
                                 'stopArtifact': False, 'preferExclusive': True}
        self.page.evaluate('async () => { gachaState().draft = null; await loadGachaOptions(); }')
        self.assertEqual('原卡池已结束，请重新选择', self.page.locator('.gacha-pool .dd-cur').inner_text())
        self.page.locator('.gacha-pool .dd-btn').click()
        self.page.locator(f'.gacha-pool .dd-opt[data-val="{POOL}"]').click()
        self.page.locator('#gachaDrawCount').fill('57')
        self.page.locator('#gachaDrawCount').press('Tab')
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaPreferExclusive')).click()
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaStopArtifact')).click()
        self.page.evaluate('async () => { await gachaState().saving; }')
        expected = {'poolId': POOL, 'count': 57, 'stopRole': True,
                    'stopArtifact': True, 'preferExclusive': False}
        self.assertEqual(expected, self.configs['first'])
        self.assertEqual(expected, self.page.evaluate('ACCOUNTS.first.params.gacha'))
        self.assertEqual({}, self.configs['second'])
        self.page.reload()
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.open_app()
        self.assertIn('蜜拉贝儿', self.page.locator('.gacha-pool .dd-cur').inner_text())
        self.assertEqual(0, self.page.locator('.gacha-pool .dd-opt[data-val="ExpiredPool"]').count())
        self.assertEqual('57', self.page.locator('#gachaDrawCount').input_value())
        self.assertFalse(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertTrue(self.page.locator('#gachaStopArtifact').is_checked())
        self.assertFalse(any(path == '/api/gacha/start' for path, _ in self.requests))
        saves = [body for path, body in self.requests if path == '/api/toggles']
        self.assertTrue(saves)
        self.assertTrue(all(body['account'] == 'first' and body['toggles'] is None
                            and set(body['params']) == {'gacha'} for body in saves))

    def test_start_waits_for_pending_setting_saves_and_keeps_latest_choices(self):
        pending = []

        def hold_first_save(route):
            if not pending:
                pending.append(route)
            else:
                self.serve(route)

        self.context.route('**/api/toggles', hold_first_save)
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaPreferExclusive')).click()
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaStopArtifact')).click()
        self.page.locator('#gachaStart').click()
        self.assertEqual(1, len(pending))
        self.assertFalse(any(path == '/api/gacha/start' for path, _ in self.requests))
        self.serve(pending[0])
        self.page.wait_for_function('gachaState().job?.running && !gachaState().starting')
        self.assertEqual(False, self.configs['first']['preferExclusive'])
        self.assertEqual(True, self.configs['first']['stopArtifact'])
        self.assertEqual(self.configs['first'], self.jobs['first']['params'])
        writes = [path for path, _ in self.requests if path in ('/api/toggles', '/api/gacha/start')]
        self.assertEqual('/api/gacha/start', writes[-1])
        self.assertGreaterEqual(writes.count('/api/toggles'), 2)
        self.assertEqual(1, writes.count('/api/gacha/start'))

    def test_stop_waits_for_in_flight_draw_without_allowing_a_second_start(self):
        self.page.locator('#gachaDrawCount').fill('25')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running && !gachaState().starting")
        pending = []
        self.context.route('**/api/gacha/cancel', lambda route: pending.append(route))
        self.page.locator('#gachaStart').click()
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '停止中…')
        self.assertTrue(self.page.locator('#gachaStart').is_disabled())
        self.assertTrue(self.page.locator('#gachaDrawCount').is_disabled())
        self.assertEqual(len(pending), 1)
        self.jobs['first'].update(state='stopping', revision=2, completed=2, guaranteedCount=3)
        pending[0].fulfill(content_type='application/json', body=json.dumps({'job': self.jobs['first']}))
        self.page.wait_for_function("!gachaState().stopping")
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '停止中…')
        self.assertTrue(self.page.locator('#gachaStart').is_disabled())
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '3 / 120')
        self.account_pools['first'][0]['guaranteedCount'] = 4
        self.jobs['first'].update(state='stopped', running=False, revision=3, completed=3, guaranteedCount=4)
        self.page.evaluate("job => applyGachaJob('first', job)", self.jobs['first'])
        self.page.wait_for_function("$('gachaStart').textContent === '▶ 开始抽卡'")
        self.assertTrue(self.page.locator('#gachaStart').is_enabled())
        self.assertFalse(self.page.locator('#gachaDrawCount').is_disabled())
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '4 / 120')
        self.assertEqual(sum(path == '/api/gacha/start' for path, _ in self.requests), 1)

    def test_server_resets_pity_and_new_run_keeps_current_count(self):
        self.account_pools['first'][0]['guaranteedCount'] = 119
        self.page.evaluate('loadGachaOptions()')
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '119 / 120')
        self.page.locator('#gachaDrawCount').fill('25')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running && !gachaState().starting")
        self.account_pools['first'][0]['guaranteedCount'] = 0
        self.jobs['first'].update(state='done', running=False, completed=1, revision=3, guaranteedCount=0)
        self.page.evaluate("job => applyGachaJob('first', job)", self.jobs['first'])
        self.page.wait_for_function("$('gachaStart').textContent === '▶ 开始抽卡'")
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '0 / 120')
        self.assertEqual(self.page.locator('#gachaPityProgress').get_attribute('aria-valuenow'), '0')
        self.assertFalse(self.page.locator('#gachaStart').evaluate("el => el.classList.contains('task-stop-button')"))
        self.page.locator('#gachaDrawCount').fill('10')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running && !gachaState().starting")
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '0 / 120')
        self.assertEqual(self.page.locator('#gachaPityProgress').get_attribute('aria-valuenow'), '0')
        self.assertEqual(sum(path == '/api/gacha/start' for path, _ in self.requests), 2)

    def test_contract_balances_update_while_running_without_rebuilding_controls(self):
        self.page.locator('#gachaDrawCount').fill('25')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running")
        self.page.evaluate("window.gachaCountControl = $('gachaDrawCount')")
        self.jobs['first'].update(revision=2, completed=1, balances={'TH806': 0, '5': 45},
                                  freeRemaining=0, guaranteedCount=2)
        self.page.evaluate("job => applyGachaJob('first', job)", self.jobs['first'])
        self.assertEqual(self.page.locator('[data-gacha-balance="TH806"]').inner_text(), '0')
        self.assertEqual(self.page.locator('[data-gacha-balance="5"]').inner_text(), '45')
        self.assertTrue(self.page.evaluate("window.gachaCountControl === $('gachaDrawCount')"))
        self.assertEqual(self.page.locator('#gachaDrawCount').input_value(), '25')
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '2 / 120')
        self.assertEqual(self.page.locator('#gachaPityProgress').get_attribute('aria-valuenow'), '2')
        self.assertEqual(self.page.locator('#gachaPityProgress').get_attribute('aria-valuemax'), '120')

    def test_navigation_and_account_switch_keep_running_job_and_own_drafts(self):
        self.account_pools['second'][0]['guaranteedCount'] = 34
        self.page.locator('label.gacha-stop-row').filter(has=self.page.locator('#gachaPreferExclusive')).click()
        self.page.locator('#gachaDrawCount').fill('42')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running")
        self.jobs['first'].update(revision=2, completed=5, guaranteedCount=6)
        self.account_pools['first'][0]['guaranteedCount'] = 6
        self.page.evaluate("job => applyGachaJob('first', job)", self.jobs['first'])
        self.page.locator('#gachaBack').click()
        self.page.locator('[data-tool="gacha"]').click()
        self.page.wait_for_function("gachaState().job?.running")
        self.page.evaluate("switchAccount('second')")
        self.page.wait_for_function("!!gachaState().data")
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '▶ 开始抽卡')
        self.assertTrue(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '34 / 120')
        self.page.locator('#gachaDrawCount').fill('13')
        self.page.evaluate("switchAccount('first')")
        self.page.wait_for_function("activeAccount === 'first' && $('gachaDrawCount').value === '42'")
        self.assertTrue(self.page.locator('#gachaStart').is_enabled())
        self.assertEqual(self.page.locator('#gachaStart').inner_text(), '停止')
        self.assertFalse(self.page.locator('#gachaPreferExclusive').is_checked())
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '6 / 120')
        self.page.evaluate("switchAccount('second')")
        self.page.wait_for_function("activeAccount === 'second' && $('gachaDrawCount').value === '13'")
        self.assertEqual(sum(path == '/api/gacha/start' for path, _ in self.requests), 1)
        self.assertFalse(any(path == '/api/gacha/cancel' for path, _ in self.requests))

    def test_selected_pool_has_its_own_pity_count_and_limit(self):
        normal = next(pool for pool in gacha.options(ReplayClient().account_state) if pool['id'] == 'NormalSummon')
        normal['guaranteedCount'] = 57
        self.account_pools['first'].append(normal)
        self.page.evaluate('loadGachaOptions()')
        self.page.locator('.gacha-pool .dd-btn').click()
        self.page.locator('.gacha-pool .dd-opt[data-val="NormalSummon"]').click()
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '57 / 180')
        self.assertEqual(self.page.locator('#gachaPityProgress').get_attribute('aria-valuemax'), '180')
        self.page.locator('.gacha-pool .dd-btn').click()
        self.page.locator(f'.gacha-pool .dd-opt[data-val="{POOL}"]').click()
        self.assertEqual(self.page.locator('#gachaPityCount').inner_text(), '1 / 120')
        self.assertFalse(any(path == '/api/gacha/start' for path, _ in self.requests))

    def test_mobile_and_desktop_have_no_horizontal_overflow(self):
        self.page.locator('#gachaDrawCount').fill('9999')
        self.page.locator('#gachaStart').click()
        self.page.wait_for_function("gachaState().job?.running && !gachaState().starting")
        self.jobs['first'].update(revision=2, completed=1234, guaranteedCount=119)
        self.page.evaluate("job => applyGachaJob('first', job)", self.jobs['first'])
        for width in [360, 390, 1280]:
            self.page.set_viewport_size({'width': width, 'height': 900})
            for theme in ['light', 'dark']:
                self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                self.assertTrue(self.page.locator('#gachaPage').evaluate(
                    "root => root.scrollWidth <= root.clientWidth + 1"), (width, theme))
                self.assertTrue(self.page.locator('#gachaPityProgress').evaluate("""bar => {
                    const track = bar.getBoundingClientRect(), count = document.getElementById('gachaPityCount').getBoundingClientRect();
                    return track.width > 0 && track.right < count.left && Math.abs(track.top + track.height/2 - count.top - count.height/2) < 1;
                }"""), (width, theme))


if __name__ == '__main__':
    unittest.main()
