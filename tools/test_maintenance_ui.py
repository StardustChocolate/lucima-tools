"""Exercise maintenance display and manual-loop cancellation in an offline browser."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'frontend/app.js').read_text(encoding='utf-8')


def function_source(name):
    start = SOURCE.index(f'function {name}(')
    return SOURCE[start:SOURCE.index('\n}', start) + 2]


class MaintenanceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        self.context.route('**/*', lambda route: route.abort())
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content('''
          <div id="connPill" class="conn-pill"><span class="status-dot"></span>
            <span id="connStatus"></span><button id="refreshBtn">↻</button></div>
          <button id="reconnectBtn"></button>
          <div id="accSwitcher">
            <button id="accCurrentBtn"><span class="acc-connection"></span></button>
            <div class="acc-item" data-email="alpha"><span class="acc-connection"></span></div>
            <div class="acc-item" data-email="beta"><span class="acc-connection"></span></div>
          </div>''')
        self.page.add_style_tag(path=str(ROOT / 'frontend/style.css'))
        self.page.add_script_tag(content='''
          let activeAccount = 'alpha';
          const $ = id => document.getElementById(id);
          const ACCOUNTS = {alpha:{connection:{connected:true}}, beta:{connection:{connected:true}}};
          const shopRunning = {alpha:true, beta:true}, shopStopRequested = {};
          const starSourceRunning = {alpha:true, beta:true}, starSourceStopRequested = {};
          const customizedEquipRunning = {alpha:true, beta:true}, customizedEquipStopRequested = {};
          const sweepRunning = {alpha:true, beta:true}, sweepStopRequested = {};
          const actRunning = {alpha:true, beta:true}, actStopRequested = {};
          const DEV_LOOP_STATES = new Map([['alpha', {running:true, wake:() => wakes++}]]);
          const decisions = [], logs = [], toasts = [];
          let wakes = 0;
          const setSecretShopStartState = () => {};
          const setStarSourceStartState = () => {};
          const logTo = (...args) => logs.push(args);
          const showMessageToast = (...args) => toasts.push(args);
          ACCOUNTS.alpha.secretShopPending = {id:'shop', resolve:decision => decisions.push(decision)};
          ACCOUNTS.alpha.starSourcePending = {id:'star', resolve:decision => decisions.push(decision)};
        ''' + '\n'.join(function_source(name) for name in (
            'connectionBlockedMessage', 'stopManualTasksForMaintenance',
            'resolveSecretShopPending', 'resolveStarSourcePending',
            'renderConnectionState', 'applyConnectionState', 'updateAccountConnectionIndicator')))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def maintenance(self):
        self.page.evaluate("applyConnectionState('alpha', {connected:false, reason:'maintenance', maintenance:true, maintenanceEpoch:1})")

    def test_maintenance_stops_loops_and_pending_choices_without_affecting_other_accounts(self):
        self.maintenance()
        self.assertEqual(self.page.evaluate('''[shopStopRequested.alpha, starSourceStopRequested.alpha,
          customizedEquipStopRequested.alpha, sweepStopRequested.alpha, actStopRequested.alpha,
          DEV_LOOP_STATES.get('alpha').stopRequested]'''), [True] * 6)
        self.assertFalse(self.page.evaluate('!!shopStopRequested.beta || !!sweepStopRequested.beta'))
        self.assertEqual(self.page.evaluate('decisions.map(item => item.action)'), ['stop', 'stop'])
        self.assertEqual(self.page.evaluate('wakes'), 1)
        self.assertEqual(self.page.locator('#connStatus').inner_text(), '维护中')
        self.assertFalse(self.page.locator('#refreshBtn').is_enabled())
        self.assertIn('维护', self.page.locator('[data-email="alpha"] .acc-connection').get_attribute('title'))

    def test_recovery_keeps_manual_tasks_stopped_and_updates_the_status(self):
        self.maintenance()
        self.page.evaluate("applyConnectionState('alpha', {connected:false, reason:'maintenance', maintenance:false, maintenanceEpoch:1})")
        self.assertEqual(self.page.locator('#connStatus').inner_text(), '等待重登')
        self.assertTrue(self.page.locator('#refreshBtn').is_enabled())
        self.page.evaluate("applyConnectionState('alpha', {connected:true, maintenanceEpoch:1})")
        self.assertEqual(self.page.locator('#connStatus').inner_text(), '已连接')
        self.assertTrue(self.page.evaluate('shopStopRequested.alpha && sweepStopRequested.alpha && actStopRequested.alpha'))
        self.assertEqual(self.page.evaluate('decisions.length'), 2)

    def test_browser_resuming_after_a_whole_maintenance_cycle_still_stops_old_loops(self):
        self.page.evaluate("applyConnectionState('alpha', {connected:true, maintenanceEpoch:1})")
        self.assertTrue(self.page.evaluate('shopStopRequested.alpha && starSourceStopRequested.alpha'))
        self.assertEqual(self.page.locator('#connStatus').inner_text(), '已连接')
        self.assertEqual(self.page.evaluate('logs.length'), 0)


if __name__ == '__main__':
    unittest.main()
