"""Mobile voting UI uses the real app, local API fixtures and full candidates."""
import copy
import json
import mimetypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

from backend import runtime_data, tasks, voting
from tools.test_voting import FIXTURE, NOW

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://voting-test.invalid'


class VotingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(self.folder.name)))
        self.states = {}
        for email in ('first', 'second'):
            self.states[email] = voting.status(copy.deepcopy(FIXTURE['state']), now_ms=NOW * 1000, candidates=True)
        self.params = {'first': {}, 'second': {}}
        self.requests, self.errors, self.unexpected = [], [], []
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 900}, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.context.route('**/*', self.serve)
        self.page.goto(ORIGIN)
        self.page.wait_for_function("typeof enterApp === 'function' && updateStartupReady")
        self.page.evaluate("""async () => {
            for (const account of ['first', 'second']) rememberAccount({account, status:{name:account}});
            activeAccount='first'; enterApp(); await loadTasks(); showSection('auto');
        }""")
        self.root = self.page.locator('#auto-activity_vote')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.unexpected, [])

    def serve(self, route):
        url = urlsplit(route.request.url)
        path = unquote(url.path)
        body = route.request.post_data_json if route.request.method == 'POST' else None
        self.requests.append((path, body, route.request.method))
        if url.netloc != urlsplit(ORIGIN).netloc:
            self.unexpected.append(route.request.url)
            route.abort()
            return
        if path.startswith('/api/'):
            from urllib.parse import parse_qs
            email = (body or {}).get('account') or parse_qs(url.query).get('account', ['first'])[0]
            state = copy.deepcopy(self.states[email])
            for a in state['activities']:
                a['target'] = self.params[email].get('activity_vote', {}).get('targets', {}).get(a['id'], '')
                match = next((c for c in a['candidates'] if c['id'] == a['target']), {})
                a.update(targetName=match.get('name', ''), targetValid=bool(match))
            if path == '/api/toggles': self.params[email].update((body or {}).get('params', {}))
            responses = {
                '/api/accounts': {'accounts': []}, '/api/preferences': {'settings': {}},
                '/api/config': {'platform': 'windows', 'version': 'test'},
                '/api/tasks': {'toggle': [t for t in tasks.toggle_tasks() if t['id'] == 'activity_vote'], 'manual': []},
                '/api/update/status': {'testServer': True, 'available': False},
                '/api/update/check': {'testServer': True, 'available': False},
                '/api/resources/status': {'phase': 'ready'},
                '/api/logs/auto': {'entries': [], 'logSeq': 0},
                '/api/toggles': {'ok': True, 'params': self.params[email]},
                '/api/voting/options': state,
                '/api/status': {'status': {}, 'nextClaims': {'activity_vote': state}},
            }
            if path not in responses: self.unexpected.append(path)
            route.fulfill(content_type='application/json', body=json.dumps(responses.get(path, {})))
            return
        file = ROOT / 'frontend/index.html' if path == '/' else (
            ROOT / 'frontend' / path.removeprefix('/static/') if path.startswith('/static/') else ROOT / path.lstrip('/'))
        if not file.resolve().is_relative_to(ROOT) or not file.is_file():
            self.unexpected.append(path)
            route.abort()
            return
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def open_options(self):
        self.root.locator('.voting-toggle').click()
        self.page.wait_for_function("!!accountOptionCache('voting')")
        self.assertTrue(self.root.locator('.voting-target').is_visible())

    def test_filters_select_saves_target_and_reuses_existing_card_styles(self):
        self.open_options()
        self.assertFalse(self.root.locator('.switch-in').is_checked())
        self.root.locator('.voting-target').click()
        self.assertEqual(self.root.locator('[data-vote-role]').count(), 118)
        self.root.locator('[data-voting-filter="element"][data-value="Ice"]').click()
        self.root.locator('[data-voting-filter="job"][data-value="Defender"]').click()
        expected = {c['id'] for c in self.states['first']['activities'][0]['candidates']
                    if c['element'] == 'Ice' and c['job'] == 'Defender'}
        self.assertEqual(set(self.root.locator('[data-vote-role]').evaluate_all(
            'buttons => buttons.map(b => b.dataset.voteRole)')), {'', *expected})
        self.assertEqual(self.root.locator('[data-vote-role="H149"]').count(), 1)
        self.assertEqual(self.root.locator('[data-vote-role="H149"].encyclopedia-preview .encyclopedia-preview-avatar').count(), 1)
        self.root.locator('[data-vote-role="H149"]').click()
        self.page.wait_for_function("ACCOUNTS.first.params.activity_vote?.targets.Vote03 === 'H149'")
        self.assertFalse(self.root.locator('.voting-picker').is_visible())
        self.assertIn('璀璨誓约的露比', self.root.locator('.voting-target').inner_text())
        self.assertEqual(self.params['first']['activity_vote']['targets'], {'Vote03': 'H149'})
        self.assertTrue(all(method == 'GET' for path, _, method in self.requests if path == '/api/voting/options'))
        self.assertFalse(any(path == '/api/tasks/auto' for path, _, _ in self.requests))
        self.assertEqual(self.root.locator('.voting-search,.voting-note').count(), 0)

    def test_account_restores_expansion_and_selection_without_game_or_options_requery(self):
        self.open_options()
        self.root.locator('.voting-target').click()
        self.root.locator('[data-voting-filter="element"][data-value="Ice"]').click()
        self.root.locator('[data-vote-role="H149"]').click()
        self.page.wait_for_function("ACCOUNTS.first.params.activity_vote?.targets.Vote03 === 'H149'")
        count = sum(path == '/api/voting/options' for path, _, _ in self.requests)
        self.page.evaluate("activeAccount='second'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertFalse(self.root.locator('.auto-body').is_visible())
        self.page.evaluate("activeAccount='first'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertTrue(self.root.locator('.auto-body').is_visible())
        self.assertIn('璀璨誓约的露比', self.root.locator('.voting-target').inner_text())
        self.assertEqual(self.root.locator('[data-voting-filter="element"][data-value="Ice"]').get_attribute('aria-pressed'), 'true')
        self.assertEqual(sum(path == '/api/voting/options' for path, _, _ in self.requests), count)

    def test_mobile_layout_and_live_progress_keep_filters_stable(self):
        self.open_options()
        self.root.locator('.voting-target').click()
        selected = self.root.locator('[data-voting-filter="element"][data-value="Ice"]')
        selected.click()
        state = copy.deepcopy(self.states['first'])
        state['activities'][0]['tickets'] = 10
        state['activities'][0]['quests'][0]['claimed'] = True
        self.page.evaluate("state => { NEXT_CLAIMS.activity_vote=state; renderNextClaims(); }", state)
        self.assertEqual(selected.get_attribute('aria-pressed'), 'true')
        self.assertTrue(self.root.locator('.voting-picker').is_visible())
        self.assertEqual(self.root.locator('.voting-ticket').inner_text(), '×10')
        self.assertEqual(self.root.locator('.voting-ticket img').get_attribute('src'), '/assets/items/T03.png')
        self.assertIn('已领取', self.root.locator('.voting-quests').inner_text())
        self.assertEqual(self.root.locator('.guild-weekly-count').first.inner_text(), '1/1')
        self.assertEqual(self.root.locator('[role="progressbar"]').first.get_attribute('aria-valuenow'), '1')
        for width in (360, 390, 768):
            self.page.set_viewport_size({'width': width, 'height': 900})
            overflow = self.root.evaluate("root => [...root.querySelectorAll('.voting-wrap,.voting-target,.voting-picker,.voting-filters,.guild-weekly-task,.auto-head')].filter(e => e.scrollWidth > e.clientWidth+1).map(e=>e.className)")
            self.assertEqual(overflow, [])
        self.root.locator('[data-voting-filter="job"][data-value="Defender"]').click()
        self.page.set_viewport_size({'width': 390, 'height': 900})
        path = ROOT / 'build/voting-ui.png'
        self.root.screenshot(path=str(path))

    def test_filter_multiselect_all_and_abstain(self):
        self.open_options()
        self.root.locator('.voting-target').click()
        for element in ('Ice', 'Fire'):
            self.root.locator(f'[data-voting-filter="element"][data-value="{element}"]').click()
        expected = {c['id'] for c in self.states['first']['activities'][0]['candidates']
                    if c['element'] in ('Ice', 'Fire')}
        self.assertEqual(set(self.root.locator('[data-vote-role]').evaluate_all(
            'buttons => buttons.map(b => b.dataset.voteRole)')), {'', *expected})
        self.root.locator('[data-voting-filter="element"][data-value="ALL"]').click()
        self.assertEqual(self.root.locator('[data-vote-role]').count(), 118)
        self.assertEqual(self.root.locator('[data-voting-filter="element"].active').count(), 1)
        self.root.locator('[data-vote-role="H149"]').click()
        self.page.wait_for_function("ACCOUNTS.first.params.activity_vote?.targets.Vote03 === 'H149'")
        self.root.locator('.voting-target').click()
        abstain = self.root.locator('[data-vote-role=""]')
        self.assertEqual(abstain.inner_text(), '暂不投票')
        self.assertEqual(abstain.locator('svg circle').count(), 1)
        abstain.click()
        self.page.wait_for_function("ACCOUNTS.first.params.activity_vote?.targets.Vote03 === ''")
        self.assertIn('暂不投票', self.root.locator('.voting-target').inner_text())
        self.assertFalse(self.root.locator('.voting-picker').is_visible())

    def test_dropdown_restores_by_account_and_closes_on_escape_or_outside(self):
        self.open_options()
        self.root.locator('.voting-target').click()
        self.page.evaluate("activeAccount='second'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.page.evaluate("activeAccount='first'; NEXT_CLAIMS={}; renderAutoPanel(TOGGLE_TASKS)")
        self.assertTrue(self.root.locator('.voting-picker').is_visible())
        self.root.locator('.voting-target').press('Escape')
        self.assertFalse(self.root.locator('.voting-picker').is_visible())
        self.root.locator('.voting-target').click()
        self.root.locator('.voting-heading').click()
        self.assertFalse(self.root.locator('.voting-picker').is_visible())

    def test_new_activity_loads_candidates_when_previous_options_cache_was_empty(self):
        self.open_options()
        self.page.evaluate("""state => {
            NEXT_CLAIMS.activity_vote = state;
            setAccountOptionCache('voting', {dataVersion:'bundled', activities:[]});
            renderVotingProgress();
        }""", self.states['first'])
        self.root.locator('.voting-target').click()
        self.page.wait_for_function("accountOptionCache('voting')?.activities?.[0]?.candidates?.length === 117")
        self.assertEqual(self.root.locator('[data-vote-role]').count(), 118)


if __name__ == '__main__':
    unittest.main()
