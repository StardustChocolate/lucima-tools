"""Real mobile dialogs and connection controls, with local mock requests only."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from tools import test_followup_1030_ui as followup

REVIEW = Path(__file__).resolve().parents[1] / 'build/energy-exchange-review'


class EnergyExchangeUI(unittest.TestCase):
    setUpClass = classmethod(followup.FollowupUI.setUpClass.__func__)
    tearDownClass = classmethod(followup.FollowupUI.tearDownClass.__func__)
    tearDown = followup.FollowupUI.tearDown
    energy_options = followup.FollowupUI.energy_options

    def setUp(self):
        self.refresh_requests = []
        self.refresh_hold = False
        self.refresh_pending = []
        self.energy_hold = False
        self.energy_pending = []
        self.energy_fail = False
        followup.FollowupUI.setUp(self)
        REVIEW.mkdir(parents=True, exist_ok=True)
        self.page.locator('.energy-toggle').click()

    def serve(self, route):
        path = urlsplit(route.request.url).path
        if path == '/api/status/refresh':
            self.refresh_requests.append(route.request.post_data_json)
            if self.refresh_hold:
                self.refresh_pending.append(route)
            else:
                self.finish_refresh(route)
            return
        if path == '/api/energy/buy':
            if self.energy_hold:
                self.energy_pending.append(route)
                return
            if self.energy_fail:
                route.fulfill(status=400, content_type='application/json', body=json.dumps({'detail': '余额变化，请重新选择数量'}))
                return
        followup.FollowupUI.serve(self, route)

    def finish_refresh(self, route):
        route.fulfill(content_type='application/json', body=json.dumps({'status': {}, 'connection': {'connected': True}}))

    def open_exchange(self, sid):
        if not self.page.locator(f'[data-energy-id="{sid}"]').is_visible():
            self.page.locator('.energy-toggle').click()
        self.page.locator(f'[data-energy-id="{sid}"]').click()
        self.page.wait_for_selector('#energyExchangeCount')

    def test_maximum_counts_cancel_and_confirmed_quantity_receipt(self):
        self.energy_counts = [13, 25_377, 280]
        self.open_exchange('Normal11')
        self.assertEqual(self.energy_purchases, [])
        self.assertIn('每日可购买 33 次 · 剩余可购买 32 次', self.page.locator('.energy-exchange-limit').inner_text())
        count = self.page.locator('#energyExchangeCount')
        self.assertEqual(count.get_attribute('max'), '32')
        self.page.locator('[data-energy-max]').click()
        self.assertEqual(count.input_value(), '32')
        self.assertEqual(self.page.locator('.energy-exchange-cost').inner_text(), '960')
        self.assertEqual(self.page.locator('.energy-exchange-gain').inner_text(), '1,920')
        self.page.locator('.energy-exchange-dialog').screenshot(path=str(REVIEW / 'diamond-max-32.png'))
        self.page.locator('.energy-exchange-confirm').click()
        self.page.wait_for_selector('#energyExchangeModal.hidden', state='attached')
        self.assertEqual(self.energy_purchases, [{'account': 'alpha', 'commodityId': 'Normal11', 'count': 32}])
        self.assertEqual(self.energy_counts[1], 24_417)
        self.open_exchange('Normal11')
        self.assertIn('剩余可购买 0 次', self.page.locator('.energy-exchange-limit').inner_text())
        self.assertTrue(self.page.locator('.energy-exchange-confirm').is_disabled())
        self.page.locator('.scheme-import-cancel').last.click()
        self.open_exchange('HeartShop8')
        self.assertEqual(count.get_attribute('max'), '2')
        self.page.locator('[data-energy-max]').click()
        self.assertEqual(self.page.locator('.energy-exchange-cost').inner_text(), '200')
        self.assertEqual(self.page.locator('.energy-exchange-gain').inner_text(), '20')
        self.page.keyboard.press('Escape')
        self.assertEqual(len(self.energy_purchases), 1)
        self.open_exchange('Normal6')
        self.assertEqual(count.get_attribute('max'), '13')
        self.page.locator('[data-energy-max]').click()
        self.assertEqual(self.page.locator('.energy-exchange-gain').inner_text(), '1,300')

    def test_balance_bound_clamping_and_small_screen_themes(self):
        self.open_exchange('Normal11')
        count = self.page.locator('#energyExchangeCount')
        self.assertEqual(count.get_attribute('max'), '3')
        count.fill('9999')
        self.assertEqual(count.input_value(), '3')
        count.fill('0')
        self.assertEqual(count.input_value(), '1')
        self.page.locator('[data-energy-plus]').click()
        self.assertEqual(count.input_value(), '2')
        self.page.locator('[data-energy-minus]').click()
        self.assertEqual(count.input_value(), '1')
        self.page.locator('.energy-exchange-range').fill('3')
        self.assertEqual(self.page.locator('.energy-exchange-cost').inner_text(), '90')
        for theme in ('dark', 'light'):
            self.page.evaluate('mode=>setTheme({mode})', theme)
            for width in (320, 390, 1280):
                self.page.set_viewport_size({'width': width, 'height': 844})
                layout = self.page.locator('.energy-exchange-dialog').evaluate('''n=>{
                  const r=n.getBoundingClientRect();return {left:r.left,right:r.right,width:innerWidth,
                    overflow:n.scrollWidth-n.clientWidth,color:getComputedStyle(n).backgroundColor};}''')
                self.assertGreaterEqual(layout['left'], 0)
                self.assertLessEqual(layout['right'], layout['width'])
                self.assertLessEqual(layout['overflow'], 1)
                self.assertTrue(layout['color'].startswith('rgb('), layout)
                if width == 390:
                    self.page.locator('.energy-exchange-dialog').screenshot(path=str(REVIEW / f'dialog-{theme}.png'))
        self.page.locator('.energy-exchange-confirm').focus()
        self.page.keyboard.press('Tab')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'energyExchangeCount')
        self.page.keyboard.press('Escape')
        self.assertEqual(self.energy_purchases, [])

    def test_failed_purchase_cannot_resubmit_without_reopening_and_reloading(self):
        self.open_exchange('Normal6')
        self.energy_fail = True
        self.page.locator('.energy-exchange-confirm').click()
        self.page.wait_for_function('document.querySelector(".energy-exchange-message").textContent.includes("余额变化")')
        self.assertTrue(self.page.locator('.energy-exchange-confirm').is_disabled())
        self.page.keyboard.press('Escape')
        self.energy_fail = False
        self.open_exchange('Normal6')
        self.assertFalse(self.page.locator('.energy-exchange-body').evaluate('n=>n.inert'))
        self.assertFalse(self.page.locator('.energy-exchange-confirm').is_disabled())
        self.page.locator('.energy-exchange-confirm').click()
        self.page.wait_for_selector('#energyExchangeModal.hidden', state='attached')
        self.assertEqual(len(self.energy_purchases), 1)

    def test_pending_purchase_is_single_and_switch_closes_without_painting_other_account(self):
        self.open_exchange('Normal6')
        self.energy_hold = True
        self.page.locator('.energy-exchange-confirm').click()
        self.page.wait_for_function('energyBuying.alpha === true')
        self.page.evaluate('confirmEnergyExchange(); confirmEnergyExchange()')
        self.assertEqual(len(self.energy_pending), 1)
        self.page.evaluate("switchAccount('beta')")
        self.page.wait_for_selector('#energyExchangeModal.hidden', state='attached')
        self.page.wait_for_function('Array.isArray(energyOptionsData.beta)')
        before = self.page.evaluate('JSON.stringify(energyOptionsData.beta)')
        self.energy_hold = False
        followup.FollowupUI.serve(self, self.energy_pending.pop())
        self.page.wait_for_function('!energyBuying.alpha')
        self.assertEqual(self.page.evaluate('JSON.stringify(energyOptionsData.beta)'), before)
        self.assertEqual(self.energy_purchases[0]['account'], 'alpha')
        self.open_exchange('HeartShop8')
        self.assertFalse(self.page.locator('.energy-exchange-dialog .scheme-import-cancel').is_disabled())

    def test_entire_disconnected_pill_click_keyboard_and_icon_share_refresh_lock(self):
        self.page.evaluate("applyConnectionState('alpha',{connected:false})")
        pill = self.page.locator('#connPill')
        self.assertEqual(pill.get_attribute('role'), 'button')
        self.refresh_hold = True
        self.page.locator('#connStatus').click()
        self.page.wait_for_function('ACCOUNTS.alpha.refreshPending')
        pill.click(position={'x': 4, 'y': 12}, force=True)
        pill.press('Enter')
        self.assertEqual(self.refresh_requests, [{'account': 'alpha'}])
        self.finish_refresh(self.refresh_pending.pop())
        self.page.wait_for_function('!ACCOUNTS.alpha.refreshPending')
        self.refresh_hold = False
        for key in ('Enter', ' '):
            self.page.evaluate("applyConnectionState('alpha',{connected:false})")
            pill.press(key)
            self.page.wait_for_function('!ACCOUNTS.alpha.refreshPending && ACCOUNTS.alpha.connection.connected')
        self.page.evaluate("applyConnectionState('alpha',{connected:false})")
        self.page.locator('#reconnectBtn').click()
        self.page.wait_for_function('!ACCOUNTS.alpha.refreshPending && ACCOUNTS.alpha.connection.connected')
        self.assertEqual(len(self.refresh_requests), 4)

    def test_stop_description_is_smaller_and_dimmed(self):
        self.page.locator('.activity-store-toggle').click()
        self.page.wait_for_selector('.activity-store-stop-note')
        label = self.page.locator('.activity-store-stop').locator('..')
        self.assertEqual(label.inner_text(), '买完停止（买完后将自动停止扫荡）')
        style = label.evaluate('''n=>{
          const main=getComputedStyle(n.querySelector('span:last-child')),small=getComputedStyle(n.querySelector('small'));
          return {main:parseFloat(main.fontSize),small:parseFloat(small.fontSize),color:small.color,mainColor:main.color,display:small.display};}''')
        self.assertLess(style['small'], style['main'])
        self.assertNotEqual(style['color'], style['mainColor'])
        self.assertEqual(style['display'], 'inline')


if __name__ == '__main__':
    unittest.main()
