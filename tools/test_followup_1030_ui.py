"""Mobile DOM regressions with mocked game responses only."""
import json
from urllib.parse import parse_qs, urlsplit
import unittest

from tools import test_management_store_ui as base


class FollowupUI(unittest.TestCase):
    setUpClass = classmethod(base.ManagementStoreUI.setUpClass.__func__)
    tearDownClass = classmethod(base.ManagementStoreUI.tearDownClass.__func__)
    tearDown = base.ManagementStoreUI.tearDown
    open_activity = base.ManagementStoreUI.open_activity

    def setUp(self):
        self.held_search = None
        self.held_options = None
        self.hold_search = self.hold_options = False
        self.buy_count = 0
        self.energy_counts = [3, 90, 200]
        self.energy_used = 1
        self.energy_purchases = []
        base.ManagementStoreUI.setUp(self)

    def serve(self, route):
        url = urlsplit(route.request.url)
        if url.path == '/api/energy/options':
            route.fulfill(content_type='application/json', body=json.dumps({'energyOptions': self.energy_options()}))
            return
        if url.path == '/api/energy/buy':
            sid = route.request.post_data_json['commodityId']
            count = route.request.post_data_json['count']
            self.energy_purchases.append(route.request.post_data_json)
            index = ['Normal6', 'Normal11', 'HeartShop8'].index(sid)
            self.energy_counts[index] -= [1, 30, 100][index] * count
            if index == 2:
                self.energy_counts[0] += 10 * count
            if index == 1:
                self.energy_used += count
            route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'detail': '兑换完成', 'energyOptions': self.energy_options()}))
            return
        if url.path == '/api/activity/support/search':
            query = parse_qs(url.query).get('q', [''])[0]
            if self.hold_search and query == 'Biu':
                self.held_search = route
                return
            friend = {'cuid': '456', 'name': 'BiuBiu诺', 'hero': {'name': '随机助战'}}
            matches = query.casefold() in ('biu', 'biubiu', 'biubiu诺', '456')
            route.fulfill(content_type='application/json', body=json.dumps({'supports': [friend] if matches else [], 'favorites': []}))
            return
        if url.path == '/api/activity/options' and self.hold_options:
            self.held_options = route
            return
        if url.path == '/api/activity/store/buy':
            self.buy_count += 1
        base.ManagementStoreUI.serve(self, route)

    def energy_options(self):
        return [dict(id=sid, currencyName=name, current=count, price=price, gain=gain, resultName=target,
                     currency=currency, item=item, daily=sid == 'Normal11',
                     dailyLimit=33 if sid == 'Normal11' else None,
                     remaining=33-self.energy_used if sid == 'Normal11' else None,
                     maxBuy=min(count//price, 33-self.energy_used) if sid == 'Normal11' else count//price,
                     reason='今日可购买次数已用完' if sid == 'Normal11' and self.energy_used == 33 else '')
                for (sid, name, price, gain, target, currency, item), count in zip([
                    ('Normal6', '能源电池', 1, 100, '体力', '37', '3'),
                    ('Normal11', '钻石', 30, 60, '体力', '2', '3'),
                    ('HeartShop8', '心愿币', 100, 10, '能源电池', '83', '37')], self.energy_counts)]

    def test_save_buys_once_updates_balance_and_marks_sold_out_without_erasing_selection(self):
        self.clients['alpha'].account_state['ItemContainer']['Items'][0]['Count'] = 25
        self.page.locator('.activity-store-toggle').click()
        self.page.wait_for_selector('[data-store-item="p"]')
        self.assertIn('剩余 2/2 份', self.page.locator('.activity-store-good').first.inner_text())
        self.page.locator('[data-store-item="p"]').locator('..').click()
        self.page.get_by_role('button', name='保存并购买', exact=True).click()
        self.page.wait_for_function("document.querySelector('[data-store-item=p]').disabled")
        checkbox = self.page.locator('[data-store-item="p"]')
        self.assertTrue(checkbox.is_checked())
        self.assertIn('sold-out', checkbox.locator('../..').get_attribute('class'))
        self.assertIn('剩余 0/2 份', checkbox.locator('../..').inner_text())
        self.assertEqual(self.page.locator('.activity-store-currency strong').inner_text(), '5')
        self.assertEqual(self.buy_count, 1)
        self.page.locator('#activity-store').screenshot(path=str(base.base.ROOT/'build/management-store-review/store-remaining-sold-out.png'))
        self.assertEqual(self.options['alpha']['ActivityH806']['items'], ['p'])
        self.assertEqual(self.page.get_by_text('活动扫荡前后自动购买', exact=True).count(), 0)

    def test_continuous_mixed_name_search_and_late_response_preserve_selected_friend(self):
        self.open_activity()
        search = self.page.locator('.act-support-search')
        self.hold_search = True
        search.fill('Biu')
        self.page.wait_for_timeout(250)
        self.assertIsNotNone(self.held_search)
        search.fill('BiuBiu')
        self.page.wait_for_selector('.sup-card[data-cuid="456"]')
        self.held_search.fulfill(content_type='application/json', body='{"supports":[],"favorites":[]}')
        self.page.wait_for_timeout(70)
        self.page.locator('.sup-card[data-cuid="456"]').tap()
        for query in ['BiuBiu诺', 'biubiu', '456']:
            search.fill(query)
            self.page.wait_for_selector('.sup-card[data-cuid="456"]')
            self.page.wait_for_timeout(220)
            self.assertEqual(self.page.evaluate('getActCfg().supportCuid'), '456')
        search.fill('没有这个好友')
        self.page.wait_for_timeout(250)
        self.assertEqual(self.page.evaluate('getActCfg().supportCuid'), '456')
        search.fill('')
        self.page.wait_for_selector('.sup-card[data-cuid="123"]')
        self.assertEqual(self.page.evaluate('getActCfg().supportCuid'), '456')
        self.page.evaluate("loadActivityOptions('activity_scene',true)")
        self.page.wait_for_selector('.sup-card[data-cuid="456"][aria-pressed="true"]')

    def test_failed_refresh_keeps_existing_controls_and_selection(self):
        self.open_activity()
        self.page.locator('.sup-card[data-cuid="123"]').tap()
        self.page.evaluate('window.oldTeam=document.querySelector(".act-team-slot").firstElementChild')
        self.hold_options = True
        self.page.locator('#act-activity_scene .act-options-refresh').click()
        self.page.wait_for_timeout(80)
        self.assertTrue(self.page.evaluate('oldTeam===document.querySelector(".act-team-slot").firstElementChild'))
        self.assertEqual(self.page.locator('.act-team-slot .dd-loading').count(), 0)
        self.held_options.fulfill(status=503, content_type='application/json', body='{"detail":"暂时不可用"}')
        self.page.wait_for_function('!document.querySelector("#act-activity_scene .act-options-refresh").disabled')
        self.assertTrue(self.page.evaluate('oldTeam===document.querySelector(".act-team-slot").firstElementChild'))
        self.assertEqual(self.page.evaluate('getActCfg().supportCuid'), '123')

    def test_buff_menu_is_compact_top_aligned_and_does_not_move_content(self):
        self.page.evaluate("""() => {
          showSection('info'); applyStatus('alpha',{name:'alpha',buffs:{available:true,items:[
            {id:'guild',staticId:'GuildMoneyBonus',source:'佣兵团',name:'金币增益',description:'10%'}]}});
        }""")
        root = self.page.locator('#accountBuffs')
        layout = root.evaluate('''n=>{
          const heading=n.querySelector('.account-buffs-heading'),label=heading.children[0].getBoundingClientRect(),count=heading.children[1].getBoundingClientRect(),arrow=heading.children[2].getBoundingClientRect();
          return {width:n.offsetWidth,top:n.getBoundingClientRect().top,profile:document.querySelector('.account-profile').getBoundingClientRect().top,labelGap:count.left-label.right,arrowGap:arrow.left-count.right};
        }''')
        self.assertLess(layout['width'], 170)
        self.assertAlmostEqual(layout['top'], layout['profile'], delta=1)
        self.assertAlmostEqual(layout['labelGap'], 10, delta=1)
        self.assertAlmostEqual(layout['arrowGap'], 15, delta=1)
        self.page.locator('.info-top').screenshot(path=str(base.base.ROOT/'build/management-store-review/buff-compact.png'))
        before = self.page.locator('.dial-row').bounding_box()
        root.locator('.account-buffs-heading').click()
        self.assertEqual(root.locator('.account-buffs-chevron').inner_text(), '▴')
        self.assertEqual(self.page.locator('.dial-row').bounding_box(), before)
        self.assertTrue(root.locator('.account-buffs-details').is_visible())
        root.locator('.account-buffs-heading').click()
        self.assertEqual(root.locator('.account-buffs-chevron').inner_text(), '▾')

    def test_all_energy_currency_counts_refresh_after_each_conversion(self):
        self.page.locator('.energy-toggle').click()
        counts = self.page.locator('.energy-option small strong')
        self.assertEqual(counts.all_text_contents(), ['3', '90', '200'])
        for sid, expected in [('Normal6', ['2', '90', '200']), ('Normal11', ['2', '60', '200']),
                              ('HeartShop8', ['12', '60', '100'])]:
            self.page.locator(f'[data-energy-id="{sid}"]').click()
            self.page.wait_for_selector('#energyExchangeCount')
            self.page.locator('.energy-exchange-confirm').click()
            self.page.wait_for_function('(expected)=>[...document.querySelectorAll(".energy-option small strong")].map(n=>n.textContent).join()===expected.join()', arg=expected)
        self.assertNotIn('已购', self.page.locator('.energy-options').inner_text())


if __name__ == '__main__':
    unittest.main()
