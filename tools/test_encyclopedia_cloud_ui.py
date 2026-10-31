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


if __name__ == '__main__':
    unittest.main()
