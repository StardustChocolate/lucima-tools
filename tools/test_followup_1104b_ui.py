"""Offline real DOM checks for multi-set selection and equipment lock controls."""
from copy import deepcopy
import json
from urllib.parse import urlsplit
import unittest
from tools import test_account_switch_ui as base
from tools.test_equipment_library import ui_fixture


class EquipmentUpdateUI(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    tearDown=base.AccountSwitchUI.tearDown

    def setUp(self):
        self.fixture=ui_fixture()
        for item in self.fixture['equips']:item['locked']=False
        self.fixture['roles']=[{'id':'hero','staticID':'H001','name':'测试角色','level':60,'star':6,'worn':0}]
        self.actions=[]
        base.AccountSwitchUI.setUp(self)
        self.page.evaluate("showSection('equipment')")
        self.page.wait_for_function("document.getElementById('eqpStatus').textContent.includes('已读取')")

    def serve(self,route):
        if urlsplit(route.request.url).path=='/api/equipment/action':
            body=route.request.post_data_json;action=body['action'];self.actions.append(deepcopy(body))
            if action in ('setLock','lockAllMax'):
                for item in self.fixture['equips']:
                    if (action=='setLock' and item['id']==body['equipId']) or (action=='lockAllMax' and item['enhance']==15):item['locked']=body['locked']
                value={'overview':self.fixture,'verified':True,'detail':'已加锁' if body['locked'] else '已解锁'}
            else:value=self.fixture
            route.fulfill(content_type='application/json',body=json.dumps(value));return
        base.AccountSwitchUI.serve(self,route)

    def test_multiple_four_piece_and_two_piece_choices_stay_enabled_and_explain_combinations(self):
        self.page.get_by_role('button',name='添加第一个配装角色').click()
        search=self.page.get_by_role('searchbox',name='搜索角色名或别称')
        search.fill('测试角色');self.page.locator('.eqp-search-results button').first.click()
        sets=self.page.locator('.eqp-sets')
        for name in ('攻击','暴伤','免疫','暴击'):sets.get_by_role('button',name=name,exact=True).click()
        self.assertEqual(sets.locator('.eqp-setrow [aria-pressed=true]').count(),4)
        rule=sets.locator('.eqp-set-combination-rule').inner_text()
        self.assertIn('攻击4件、暴伤4件中的任一套',rule)
        self.assertIn('免疫2件、暴击2件中的任一套',rule)
        for name in ('攻击','暴伤','暴击'):sets.get_by_role('button',name=name,exact=True).click()
        for name in ('生命','防御'):sets.get_by_role('button',name=name,exact=True).click()
        self.assertIn('免疫2件固定',sets.locator('.eqp-set-combination-rule').inner_text())
        self.assertIn('任二组合，可重复',sets.locator('.eqp-set-combination-rule').inner_text())
        sets.get_by_role('button',name='免疫',exact=True).click()
        self.assertIn('任三组合，可重复',sets.locator('.eqp-set-combination-rule').inner_text())
        self.page.locator('.eqp-sets').screenshot(path=str(base.ROOT/'build/followup-1104/set-options.png'))

    def test_lock_unlock_details_and_bulk_lock_preserve_equipment_card_click(self):
        self.page.get_by_role('tab',name='装备清单').click()
        card=self.page.locator('.eqp-library-card').first
        eid=card.get_attribute('data-equip-id')
        card.get_by_role('button',name='未加锁，点击加锁').click()
        self.page.wait_for_function("document.querySelector('.eqp-library-card button[data-equip-lock]').getAttribute('aria-pressed')==='true'")
        self.assertEqual(self.page.locator('dialog[open]').count(),0)
        self.assertIn('已加锁',self.page.locator('.message-toast-text').last.inner_text())
        self.page.locator(f'.eqp-library-card[data-equip-id="{eid}"]').click()
        self.assertIn('已加锁',self.page.locator('dialog[open]').inner_text())
        self.page.locator('dialog[open]').get_by_role('button',name='已加锁，点击解锁').click()
        self.page.wait_for_function("document.querySelector('.eqp-library-card button[data-equip-lock]').getAttribute('aria-pressed')==='false'")
        self.assertIn('已解锁',self.page.locator('.message-toast-text').last.inner_text())
        self.page.get_by_role('button',name='一键锁+15装备',exact=True).click()
        self.page.wait_for_function("[...document.querySelectorAll('.eqp-library-card button[data-equip-lock]')].every(n=>n.getAttribute('aria-pressed')==='true')")
        self.assertEqual([row['action'] for row in self.actions if row['action'] in ('setLock','lockAllMax')],['setLock','setLock','lockAllMax'])
        for width in (320,390,1280):
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        self.page.set_viewport_size({'width':390,'height':900})
        self.page.locator('.eqp-library-grid').first.screenshot(path=str(base.ROOT/'build/followup-1104/equipment-locks.png'))


if __name__=='__main__':unittest.main()
