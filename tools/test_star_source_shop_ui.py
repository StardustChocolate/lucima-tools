"""Exercise star-source stop/skip races with controlled offline responses."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "frontend/app.js").read_text(encoding="utf-8")


def function_source(name):
    start = SOURCE.index(f"function {name}(")
    return SOURCE[start:SOURCE.index("\n}", start) + 2]


class StarSourceShopUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.set_content('<base href="https://star-source.invalid"><main id="widgets"></main>')
        self.page.add_style_tag(path=str(ROOT / "frontend/style.css"))
        shared = "\n".join(function_source(name) for name in (
            "filterEscape", "dropdownHTML", "bindDropdown"))
        shop = SOURCE[SOURCE.index("const STAR_SOURCE_SHOP_LS ="):
                      SOURCE.index("// ---------- 装备活动刷新 ----------")]
        self.page.add_script_tag(content="""
          let activeAccount = 'alpha', holdQuery = false;
          const ACCOUNTS = {alpha:{}, beta:{}};
          const $ = id => document.getElementById(id);
          const activeAcc = () => ACCOUNTS[activeAccount];
          const values = {}, calls = [], pending = [], logs = [];
          const frontendStorageGet = key => values[key];
          const frontendStorageSet = (key, value) => {values[key] = value;};
          const EQUIP_FILTER_SCHEMES_DATA = [{id:'scheme', name:'test', slots:{weapon:[{sets:['Speed']}]}}];
          const EQUIP_FILTER_SLOTS = [{id:'weapon'}], ELEMENT_COLOR = {};
          const clampCount = value => Math.min(999, Math.max(1, Number(value) || 1));
          const taskIconHTML = () => '';
          const equipCardHTML = card => '<div class="eq-card">Equipment ' + card.shopIndex + '</div>';
          const logTo = (...args) => logs.push(args);
          const applyStatus = () => {};
          const refreshAccountStatus = () => {};
          const waitRequestDelayBetweenIterations = () => Promise.resolve();
          const api = (path, body) => {
            calls.push({path, body});
            if (path.endsWith('/query') && !holdQuery) return Promise.resolve({ok:true});
            return new Promise((resolve, reject) => pending.push({path, body, resolve, reject}));
          };
          function reply(action, result, error=false, account='alpha') {
            const index = pending.findIndex(request => request.path.endsWith('/' + action)
              && request.body.account === account);
            if (index < 0) throw new Error('No pending ' + action + ' for ' + account);
            const request = pending.splice(index, 1)[0];
            if (error) request.reject(new Error(result)); else request.resolve(result);
          }
          function matches(count=1) {
            return {ok:true, matches:Array.from({length:count}, (_, shopIndex) => ({shopIndex}))};
          }
        """ + shared + shop + """
          function mount() {
            $('widgets').innerHTML = renderStarSourceShopWidget({id:'star_source_shop', name:'test', desc:''});
            bindStarSourceShopWidget('star_source_shop');
            document.querySelector('.shop-body').classList.remove('hidden');
          }
          setStarSourceShopCfg({count:3});
          mount();
        """)

    def tearDown(self):
        self.context.close()

    def start_refresh(self):
        self.page.click('.star-source-start')
        self.page.wait_for_function("pending.some(request => request.path.endsWith('/refresh'))")

    def wait_stopped(self):
        self.page.wait_for_function("!starSourceRunning.alpha")
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '▶ 开始刷新')
        self.assertTrue(self.page.locator('.star-source-start').is_enabled())
        self.assertFalse(self.page.evaluate('!!ACCOUNTS.alpha.starSourcePending'))
        self.assertEqual(self.errors, [])

    def refresh_count(self):
        return self.page.evaluate("calls.filter(request => request.path.endsWith('/refresh')).length")

    def test_stop_in_flight_preserves_last_match_and_skip_clears_it_without_refreshing(self):
        self.start_refresh()
        self.page.click('.star-source-start')
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '停止中…')
        self.page.evaluate("reply('refresh', matches())")
        self.wait_stopped()
        self.assertEqual(self.page.locator('.star-source-card-item').count(), 1)

        self.page.click('.star-source-skip')

        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)
        self.assertIn('已停止刷新', self.page.locator('.star-source-card-area').inner_text())
        self.assertEqual(self.refresh_count(), 1)
        self.assertFalse(self.page.evaluate('!!ACCOUNTS.alpha.starSourceResult.cards?.length'))
        self.page.evaluate("activeAccount='beta'; mount(); activeAccount='alpha'; mount()")
        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)
        self.wait_stopped()

    def test_stop_while_choosing_then_skip_does_not_restart_loop(self):
        self.start_refresh()
        self.page.evaluate("reply('refresh', matches())")
        self.page.wait_for_function('!!ACCOUNTS.alpha.starSourcePending')
        self.page.click('.star-source-start')
        self.wait_stopped()

        self.page.click('.star-source-skip')

        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)
        self.assertEqual(self.refresh_count(), 1)
        self.wait_stopped()

    def test_normal_skip_continues_until_refresh_count_is_complete(self):
        self.start_refresh()
        for round_number in (1, 2):
            self.page.evaluate("reply('refresh', matches())")
            self.page.click('.star-source-skip')
            self.page.wait_for_function(
                "count => calls.filter(request => request.path.endsWith('/refresh')).length === count",
                arg=round_number + 1)
        self.page.evaluate("reply('refresh', {ok:true, matches:[]})")
        self.wait_stopped()
        self.assertEqual(self.refresh_count(), 3)
        self.assertIn('已完成 3 次刷新', self.page.locator('.star-source-card-area').inner_text())

    def test_stop_waits_for_query_response_then_exits_without_refresh(self):
        self.page.evaluate('holdQuery=true')
        self.page.click('.star-source-start')
        self.page.click('.star-source-start')
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '停止中…')

        self.page.evaluate("reply('query', {ok:true})")

        self.wait_stopped()
        self.assertEqual(self.refresh_count(), 0)
        self.assertIn('已停止刷新', self.page.locator('.star-source-card-area').inner_text())

    def test_stop_leaves_stopping_when_refresh_returns_empty_or_raises(self):
        for error in (False, True):
            with self.subTest(error=error):
                self.start_refresh()
                self.page.click('.star-source-start')
                if error:
                    self.page.evaluate("reply('refresh', 'read timeout', true)")
                else:
                    self.page.evaluate("reply('refresh', {ok:true, matches:[]})")
                self.wait_stopped()

    def test_stop_waits_for_every_purchase_even_when_one_fails(self):
        self.start_refresh()
        self.page.evaluate("reply('refresh', matches(2))")
        self.page.locator('.star-source-buy').nth(0).click()
        self.page.locator('.star-source-buy').nth(1).click()
        self.page.click('.star-source-start')
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '停止中…')
        self.assertTrue(self.page.locator('.star-source-skip').is_disabled())

        self.page.evaluate("reply('buy', {ok:true})")

        self.assertTrue(self.page.evaluate('starSourceRunning.alpha'))
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '停止中…')
        self.assertTrue(self.page.evaluate('!!ACCOUNTS.alpha.starSourcePending'))
        self.page.evaluate("reply('buy', 'read timeout', true)")
        self.wait_stopped()
        self.assertEqual(self.refresh_count(), 1)
        self.assertIn('已购买', self.page.locator('.star-source-buy').nth(0).inner_text())
        self.assertIn('read timeout', self.page.locator('.star-source-card-error').inner_text())
        self.page.click('.star-source-skip')
        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)

    def test_purchase_after_stop_blocks_restart_until_request_settles(self):
        self.start_refresh()
        self.page.click('.star-source-start')
        self.page.evaluate("reply('refresh', matches())")
        self.wait_stopped()

        self.page.click('.star-source-buy')

        self.assertTrue(self.page.locator('.star-source-start').is_disabled())
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '购买中…')
        self.page.evaluate("void runStarSourceShop('star_source_shop')")
        self.assertEqual(self.refresh_count(), 1)
        self.page.evaluate("reply('buy', {ok:true})")
        self.wait_stopped()

    def test_stopping_in_background_cannot_change_other_accounts_button(self):
        self.start_refresh()
        self.page.click('.star-source-start')
        self.page.evaluate("activeAccount='beta'; mount()")
        self.assertEqual(self.page.locator('.star-source-start').inner_text(), '▶ 开始刷新')

        self.page.evaluate("reply('refresh', matches())")
        self.page.wait_for_function('!starSourceRunning.alpha')

        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)
        self.page.evaluate("activeAccount='alpha'; mount()")
        self.page.click('.star-source-skip')
        self.assertEqual(self.page.locator('.star-source-card-item').count(), 0)
        self.assertTrue(self.page.evaluate('calls.every(request => request.body.account === "alpha")'))
        self.wait_stopped()


if __name__ == '__main__':
    unittest.main()
