"""Real mobile DOM checks for detached shops and shared equipment search."""
from copy import deepcopy
import json
from urllib.parse import urlsplit
import unittest

from tools import test_account_switch_ui as base
from tools.test_equipment_library import ui_fixture


class UserFixesUI(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    tearDown=base.AccountSwitchUI.tearDown

    def setUp(self):
        self.store_hold=False;self.store_route=None
        self.fixture=ui_fixture()
        self.fixture['roles']=[{'id':'hero','staticID':'H151','name':'兔女郎爱莉卡','aliases':['兔女','兔子'],
                               'level':60,'star':6,'worn':1}]
        self.fixture['equips'][0].update(role='hero',roleName='兔女郎爱莉卡')
        base.AccountSwitchUI.setUp(self)

    def serve(self,route):
        path=urlsplit(route.request.url).path
        if path=='/api/equipment/action':
            route.fulfill(content_type='application/json',body=json.dumps(self.fixture));return
        if path=='/api/store/options':
            if self.store_hold:self.store_route=route;return
            route.fulfill(content_type='application/json',body='{"stores":[],"wallet":{}}');return
        if path=='/api/store/buy':
            self.calls.append((path,route.request.post_data_json['account']))
            route.fulfill(content_type='application/json',body=json.dumps({
                'ok':True,'detail':'购买成功','lines':['购买 7 档：获得 160能源'],'status':{}}));return
        base.AccountSwitchUI.serve(self,route)

    def test_successful_purchase_with_unmounted_widget_does_not_report_purchase_error(self):
        self.page.evaluate("""async () => {
          showSection('info'); setStoreCfg({picks:{'7':1}});
          await runStoreBuy('store_buy'); showSection('logs');
        }""")
        text=self.page.locator('#log').inner_text()
        self.assertIn('购买 7 档',text)
        self.assertNotIn('商店购买出错',text)
        self.assertEqual(self.calls.count(('/api/store/buy','alpha')),1)

    def test_late_shelf_reply_cannot_fill_the_new_accounts_widget(self):
        self.store_hold=True
        self.page.evaluate("""() => {
          const root=document.createElement('div');root.id='store-store_buy';
          root.innerHTML='<div class="store-panes"></div>';document.body.append(root);
          void loadStoreOptions('store_buy');
        }""")
        self.page.wait_for_timeout(70);self.assertIsNotNone(self.store_route)
        self.page.evaluate("document.getElementById('store-store_buy').remove(); activeAccount='beta'")
        self.store_route.fulfill(content_type='application/json',body='{"stores":[],"wallet":{}}')
        self.page.wait_for_timeout(70)
        self.assertTrue(self.page.evaluate("Boolean(STORE_DATA.alpha) && !STORE_DATA.beta"))

    def library(self,panel):
        self.page.evaluate("showSection('equipment')")
        self.page.wait_for_function("document.getElementById('eqpStatus').textContent.includes('已读取')")
        self.page.evaluate("""({data,panel}) => {
          const pane=document.getElementById('eqpContent');pane.replaceChildren();
          window.EquipmentLibrary.render({pane,data,account:'ui-regression',panel});
        }""",{'data':self.fixture,'panel':panel})

    def test_equipment_owner_search_matches_loadout_alias_pinyin_and_homophone(self):
        self.library('list')
        search=self.page.get_by_role('searchbox',name='搜索装备库')
        for query in ('兔子','tunv','兔女郎爱丽卡','爱丽卡','tnlalk'):
            with self.subTest(query=query):
                search.fill(query)
                self.assertEqual(self.page.locator('.eqp-library-card').count(),1)
                self.assertEqual(self.page.locator('.eqp-library-card').get_attribute('data-equip-id'),self.fixture['equips'][0]['id'])

    def test_rankings_display_two_decimal_places_in_cards_and_details(self):
        self.fixture['library']['rankings']=[{'key':'speed','label':'速度','reference':'参考角色','limit':3,'unit':'速',
            'builds':[{'value':261.22,'speed':261.22,'panel':{'Speed':261.22,'EffectHitRate':266.66,'ResistanceRate':242.66},
                       'equipIds':[],'sets':[]}]}]
        self.library('overview')
        self.assertIn('261.22',self.page.locator('.eqp-library-ranking-value').inner_text())
        self.page.locator('.eqp-library-ranking-value').click()
        self.assertIn('命中 266.66%',self.page.locator('.eqp-library-build-detail').inner_text())
        self.assertIn('抗性 242.66%',self.page.locator('.eqp-library-build-detail').inner_text())
        output=base.ROOT/'build/user-fixes-20261104/equipment-decimals.png'
        self.page.locator('.eqp-library-rankings').screenshot(path=str(output))


if __name__=='__main__':unittest.main()
