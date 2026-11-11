"""Client regressions for refreshing a character and deferring an open card."""
import json
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class CloudUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.route('http://test.local/', lambda route: route.fulfill(body='<div id="encyclopediaStatusModal" class="hidden"></div>'))
        self.page.goto('http://test.local/')

    def test_details_are_cached_by_content_and_failed_download_can_be_retried(self):
        self.calls = 0
        def respond(route):
            self.calls += 1
            route.fulfill(status=503 if self.calls == 1 else 200,
                          json={'detail': 'offline'} if self.calls == 1 else {'version': 'v2', 'character': {'ID': 'H001'}})
        self.page.route('**/api/encyclopedia/detail?*', respond)
        self.page.add_script_tag(path=str(ROOT / 'frontend/encyclopedia-data.js'))
        result = self.page.evaluate('''async () => {
          const first = {id: "H001", detail: {sha256: "old"}};
          const second = {id: "H001", detail: {sha256: "new"}};
          await LucimaEncyclopediaData.detail("v1", first).catch(() => {});
          await Promise.all([LucimaEncyclopediaData.detail("v1", first), LucimaEncyclopediaData.detail("v1", first)]);
          await LucimaEncyclopediaData.detail("v2", second);
          return true;
        }''')
        self.assertTrue(result)
        self.assertEqual(self.calls, 3)

    def test_update_and_rollback_while_card_open_leave_card_pinned_to_its_version(self):
        app = (ROOT / 'frontend/app.js').read_text(encoding='utf-8')
        snippet = app[app.index('const ENCYCLOPEDIA_STATE ='):app.index('function characterMatchesFilter(')]
        self.page.add_script_tag(content='''
          const $ = id => document.getElementById(id);
          function encyclopediaStatusOptions() { return {buff: [], debuff: []}; }
          function updateEncyclopediaStatusFilterButton() {}
          function renderEncyclopediaGrid() {}
          function showMessageToast() {}
          let answer;
          window.LucimaEncyclopediaData = {check: async () => answer};
        ''' + snippet)
        result = self.page.evaluate('''async () => {
          const make = version => ({available: true, version, characters: [{id: "H001"}], ui: {}});
          applyEncyclopediaCatalog(make("v1"));
          ENCYCLOPEDIA_STATE.activeId = "H001";
          answer = make("v2"); await checkEncyclopediaUpdate(true);
          const pinned = ENCYCLOPEDIA_STATE.version === "v1" && ENCYCLOPEDIA_STATE.pendingCatalog.version === "v2";
          answer = make("v1"); await checkEncyclopediaUpdate(true);
          ENCYCLOPEDIA_STATE.activeId = null;
          applyEncyclopediaCatalog(ENCYCLOPEDIA_STATE.pendingCatalog);
          return {pinned, afterRollback: ENCYCLOPEDIA_STATE.version};
        }''')
        self.assertEqual(result, {'pinned': True, 'afterRollback': 'v1'})

    def load_catalog_logic(self):
        app = (ROOT / 'frontend/app.js').read_text(encoding='utf-8')
        snippet = app[app.index('const ENCYCLOPEDIA_STATE ='):app.index('function characterMatchesFilter(')]
        self.page.add_script_tag(content='''
          const $ = id => document.getElementById(id);
          function encyclopediaStatusOptions() { return {buff: [], debuff: []}; }
          function updateEncyclopediaStatusFilterButton() {}
          function renderEncyclopediaGrid() {}
          const toasts = [];
          function showMessageToast(...args) { toasts.push(args); }
          const makeCatalog = version => ({available: true, version, characters: [{id: "H001"}], ui: {}, checkAfter: 3600});
        ''' + snippet)

    def test_entering_shows_cache_before_silent_update_and_coalesces_checks(self):
        self.load_catalog_logic()
        result = self.page.evaluate('''async () => {
          const calls = [];
          let finishCheck;
          window.LucimaEncyclopediaData = {
            catalog: async () => makeCatalog("v1"),
            check: force => { calls.push(force); return new Promise(resolve => { finishCheck = resolve; }); },
          };
          ENCYCLOPEDIA_STATE.searchQuery = "夏妮";
          ENCYCLOPEDIA_STATE.filters.element = ["Fire"];
          await loadEncyclopediaIndex({force: true});
          const cachedVersion = ENCYCLOPEDIA_STATE.version;
          await loadEncyclopediaIndex({force: true});
          const mergedCalls = [...calls];
          finishCheck(makeCatalog("v2"));
          await encyclopediaCheckPending;
          LucimaEncyclopediaData.check = async force => { calls.push(force); return makeCatalog("v2"); };
          await checkEncyclopediaUpdate();
          return {cachedVersion, mergedCalls, calls, version: ENCYCLOPEDIA_STATE.version,
            query: ENCYCLOPEDIA_STATE.searchQuery, elements: ENCYCLOPEDIA_STATE.filters.element, toasts};
        }''')
        self.assertEqual(result, {
            'cachedVersion': 'v1', 'mergedCalls': [True], 'calls': [True, False],
            'version': 'v2', 'query': '夏妮', 'elements': ['Fire'], 'toasts': [],
        })

    def test_failed_entry_check_keeps_cache_and_only_shows_error_without_cache(self):
        self.load_catalog_logic()
        self.page.evaluate('''() => {
          document.body.insertAdjacentHTML("beforeend", '<div id="encyclopediaEmpty" class="hidden"></div>');
          window.LucimaEncyclopediaData = {
            catalog: async () => ({available: false}),
            check: async () => { throw new Error("离线测试"); },
          };
          applyEncyclopediaCatalog(makeCatalog("v1"));
        }''')
        cached = self.page.evaluate('''async () => {
          await loadEncyclopediaIndex({force: true});
          await encyclopediaCheckPending?.catch(() => {});
          return {version: ENCYCLOPEDIA_STATE.version, hidden: $("encyclopediaEmpty").classList.contains("hidden"), toasts};
        }''')
        self.assertEqual(cached, {'version': 'v1', 'hidden': True, 'toasts': []})
        empty = self.page.evaluate('''async () => {
          ENCYCLOPEDIA_STATE.index = null; ENCYCLOPEDIA_STATE.ui = null;
          await loadEncyclopediaIndex({force: true}).catch(() => {});
          return {error: $("encyclopediaEmpty").textContent, hidden: $("encyclopediaEmpty").classList.contains("hidden"), toasts};
        }''')
        self.assertEqual(empty, {'error': '离线测试', 'hidden': False, 'toasts': []})


if __name__ == '__main__':
    unittest.main()
