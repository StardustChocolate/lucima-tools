"""Real mobile/desktop event-shop UI with local fixtures and no game traffic."""
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend import activity_store, runtime_data, tasks
from tools.test_activity_store import CURRENT, ITEM, NOW, OLD, catalog, state

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://event-shop-test.invalid'


class ActivityStoreUITests(unittest.TestCase):
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
        self.enterContext(patch.object(activity_store, 'catalog', side_effect=catalog))
        self.states = {email: activity_store.status(state(), now_ms=NOW) for email in ('first', 'second')}
        self.params, self.requests, self.errors = {'first': {}, 'second': {}}, [], []
        self.unexpected = []
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 900}, has_touch=True)
        self.addCleanup(self.context.close)
        self.context.route('**/*', self.serve)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(5000)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.page.evaluate("""async () => {
            for (const account of ['first', 'second']) rememberAccount({account, status:{name:account}});
            activeAccount='first'; enterApp(); await loadTasks(); showSection('auto');
        }""")
        self.root = self.page.locator('#auto-activity_store')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        body = route.request.post_data_json if route.request.method == 'POST' else None
        self.requests.append((path, body))
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path.startswith('/api/'):
            email = (body or {}).get('account') or parse_qs(url.query).get('account', ['first'])[0]
            if path == '/api/toggles':
                self.params[email].update(body.get('params') or {})
            responses = {
                '/api/accounts': {'accounts': []}, '/api/preferences': {'settings': {}},
                '/api/config': {'platform': 'windows', 'version': 'test'},
                '/api/tasks': {'toggle': [t for t in tasks.toggle_tasks() if t['id'] == 'activity_store'], 'manual': []},
                '/api/update/status': {'testServer': True, 'available': False},
                '/api/update/check': {'testServer': True, 'available': False},
                '/api/resources/status': {'phase': 'ready'},
                '/api/logs/auto': {'entries': [], 'logSeq': 0},
                '/api/toggles': {'ok': True, 'params': self.params[email]},
                '/api/activity-store/options': self.states[email],
                '/api/status': {'status': {}, 'nextClaims': {'activity_store': self.states[email]}},
                '/api/tasks/auto': {'results': []},
            }
            if path not in responses:
                self.unexpected.append(path)
            route.fulfill(content_type='application/json', body=json.dumps(responses.get(path, {})))
            return
        file = ROOT / 'frontend/index.html' if path == '/' else (
            ROOT / 'frontend' / path.removeprefix('/static/') if path.startswith('/static/') else ROOT / path.lstrip('/'))
        if not file.resolve().is_relative_to(ROOT):
            self.unexpected.append(path)
            route.abort()
            return
        if not file.is_file():
            if not path.startswith('/assets/items/'):
                self.unexpected.append(path)
            route.fulfill(status=404, body='')
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def open_options(self):
        self.root.locator('.activity-store-toggle').click()
        self.page.wait_for_function("!!accountOptionCache('activityStore')")
        self.assertTrue(self.root.locator('.activity-store-wrap').is_visible())

    def test_shops_switch_and_limited_targets_save_without_starting_when_disabled(self):
        self.open_options()
        self.assertEqual(self.root.locator('[data-event-store]').count(), 2)
        self.assertEqual(self.root.locator('.guild-store-good').count(), 2)
        self.assertEqual(self.root.locator('[data-event-good$="_28"]').count(), 0)
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '478/ 0')
        self.root.locator(f'[data-event-good="{ITEM}"]').click()
        self.page.wait_for_function("ACCOUNTS.first.params.activity_store?.items?.length === 1")
        self.assertEqual(self.params['first']['activity_store']['items'], [ITEM])
        self.assertTrue(self.root.locator(f'[data-event-good="{ITEM}"] input').is_checked())
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '478/ 450')
        self.root.locator(f'[data-event-good="{CURRENT}_1"]').click()
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '478/ 1,650')
        self.root.locator(f'[data-event-good="{CURRENT}_1"]').click()
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '478/ 450')
        self.root.locator(f'[data-event-store="{OLD}"]').click()
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '180/ 0')
        self.assertFalse(any(path == '/api/tasks/auto' for path, _ in self.requests))

    def test_product_titles_show_bundle_amount_only_once(self):
        self.open_options()
        cases = [
            ('2個上級火元素', 2, '2個上級火元素'),
            ('3个中级水元素', 3, '3个中级水元素'),
            ('蜜拉贝儿招募契约X5', 5, '蜜拉贝儿招募契约X5'),
            ('专属契约 ×5', 5, '专属契约 ×5'),
            ('专属契约 x 5', 5, '专属契约 x 5'),
            ('专属契约', 5, '专属契约 ×5'),
            ('2★团员券', 5, '2★团员券 ×5'),
            ('5★羁绊券', 1, '5★羁绊券'),
        ]
        snapshot = deepcopy(self.states['first'])
        template = snapshot['stores'][0]['goods'][0]
        snapshot['stores'][0]['goods'] = [
            {**template, 'id': f'{CURRENT}_title_{index}', 'name': name, 'amount': amount}
            for index, (name, amount, _) in enumerate(cases)
        ]
        for width in (390, 1024):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.page.evaluate("value => renderActivityStoreProgress('activity_store', value)", snapshot)
            self.assertEqual(self.root.locator('.guild-store-name').all_text_contents(),
                             [expected for _, _, expected in cases])
            self.assertTrue(self.root.locator('.guild-store-grid').evaluate(
                'el => el.scrollWidth <= el.clientWidth + 1'))
        self.assertFalse(any(path == '/api/tasks/auto' for path, _ in self.requests))

    def test_equipment_uses_equipment_images_and_chips_keep_item_images(self):
        self.open_options()
        snapshot = deepcopy(self.states['first'])
        template = snapshot['stores'][0]['goods'][0]
        snapshot['stores'][0]['goods'] = [
            {**template, 'id': f'{CURRENT}_icon_{sid}', 'item': sid}
            for sid in ('E1161', 'EC11')
        ]
        self.page.evaluate("value => renderActivityStoreProgress('activity_store', value)", snapshot)
        for sid, category in (('E1161', 'equip'), ('EC11', 'items')):
            with self.subTest(sid=sid):
                image = self.root.locator(f'[data-event-good="{CURRENT}_icon_{sid}"] .guild-store-icon')
                expected = f'/assets/{category}/{sid}.png'
                self.assertEqual(expected, image.get_attribute('src'))
                self.page.wait_for_function("src => [...document.images].some(img => img.getAttribute('src') === src && img.naturalWidth > 0)", arg=expected)
                self.assertGreater(image.evaluate('img => img.naturalWidth'), 0)
                self.assertEqual(expected, self.page.evaluate("id => {const tile = document.createElement('div'); tile.innerHTML = itemTileHTML({id, count:1}); return tile.querySelector('img').getAttribute('src')}", sid))

    def test_switching_accounts_restores_panel_tab_and_targets(self):
        self.open_options()
        self.root.locator(f'[data-event-good="{ITEM}"]').click()
        self.root.locator(f'[data-event-store="{OLD}"]').click()
        self.page.evaluate("activeAccount='second'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertFalse(self.root.locator('.auto-body').is_visible())
        self.page.evaluate("activeAccount='first'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertTrue(self.root.locator('.auto-body').is_visible())
        self.assertTrue(self.root.locator(f'[data-event-store="{OLD}"]').evaluate("el => el.classList.contains('active')"))
        self.root.locator(f'[data-event-store="{CURRENT}"]').click()
        self.assertTrue(self.root.locator(f'[data-event-good="{ITEM}"] input').is_checked())
        self.assertEqual(self.params['second'], {})

    def test_live_purchase_updates_stock_and_balance_without_resetting_selection(self):
        self.open_options()
        self.root.locator(f'[data-event-good="{ITEM}"]').click()
        snapshot = deepcopy(self.states['first'])
        snapshot['stores'][0]['wallet']['AH806'] = 298
        snapshot['stores'][0]['goods'][0].update(bought=2, remaining=3, buyable=3)
        snapshot['serverNowMs'] += 1000
        self.page.evaluate("value => {NEXT_CLAIMS.activity_store=value;renderNextClaims()}", snapshot)
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '298/ 270')
        snapshot['stores'][0]['wallet']['AH806'] = 28
        snapshot['stores'][0]['goods'][0].update(bought=5, remaining=0, buyable=0)
        snapshot['serverNowMs'] += 1000
        self.page.evaluate("value => {NEXT_CLAIMS.activity_store=value;renderNextClaims()}", snapshot)
        self.assertIn('已售罄', self.root.locator(f'[data-event-good="{ITEM}"]').inner_text())
        self.assertEqual(self.root.locator('.guild-store-balances').text_content(), '28/ 0')
        self.assertTrue(self.root.locator(f'[data-event-good="{ITEM}"] input').is_checked())

    def test_mobile_and_desktop_have_no_horizontal_overflow_and_images_fall_back(self):
        self.open_options()
        for width in (390, 1024):
            self.page.set_viewport_size({'width': width, 'height': 900})
            self.assertTrue(self.root.locator('.guild-store-grid').evaluate(
                'el => el.scrollWidth <= el.clientWidth + 1'))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.wait_for_function("[...document.querySelectorAll('.activity-store-wrap img')].every(img => img.hidden || img.complete && img.naturalWidth > 0)")


if __name__ == '__main__':
    unittest.main()
