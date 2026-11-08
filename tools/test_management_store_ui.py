"""Real mobile DOM for support persistence, management notes and store stop."""
from copy import deepcopy
import json
from urllib.parse import urlsplit,parse_qs
import unittest

from tools import test_account_switch_ui as base
from backend import activity_store
from tools.test_activity_store import Client,GOODS
from unittest.mock import patch


class ManagementStoreUI(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    tearDown=base.AccountSwitchUI.tearDown

    def setUp(self):
        self.options={};self.clients={key:Client(0) for key in ('alpha','beta')}
        self.management_result={'ok':True,'detail':'整理完成'}
        self.management_status={}
        self.patcher=patch.object(activity_store,'catalog',return_value={'shops':GOODS});self.patcher.start();self.addCleanup(self.patcher.stop)
        base.AccountSwitchUI.setUp(self)

    def serve(self,route):
        url=urlsplit(route.request.url);path=url.path
        body=route.request.post_data_json if route.request.method=='POST' else {}
        email=body.get('account')or parse_qs(url.query).get('account',['alpha'])[0]
        def send(data):route.fulfill(content_type='application/json',body=json.dumps(data))
        if path=='/api/status' and email in self.management_status:
            send({'status':self.management_status[email]});return
        if path=='/api/activity/store/options':
            send({'activityStore':activity_store.snapshot(self.clients[email],self.options.get(email,{}))});return
        if path=='/api/activity/store/config':
            self.options.setdefault(email,{})[body['store']]=deepcopy(body['options'])
            send({'activityStore':activity_store.snapshot(self.clients[email],self.options[email])});return
        if path=='/api/activity/store/buy':
            result=activity_store.process(self.clients[email],self.options.get(email,{}),body['store'])
            send({'ok':True,'detail':'商店检查完成','activityStore':activity_store.snapshot(self.clients[email],self.options.get(email,{}))});return
        if path=='/api/activity/options':
            send({'scenes':[{'id':'BH806_1_12','name':'活动困难'}], 'teams':[{'id':'T','name':'队伍1'}],
                  'supports':[{'cuid':'123','name':'好友一','hero':{'name':'角色一'}}], 'favorites':[], 'activities':[]});return
        if path=='/api/activity/run':
            self.calls.append((path,email))
            self.clients[email].account_state['ItemContainer']['Items'][0]['Count']=25
            result=activity_store.process(self.clients[email],self.options.get(email,{}),'ActivityH806')
            send({'ok':True,'detail':'活动掉落','storeStop':result['stop'],'storePurchases':result,
                  'activityStore':activity_store.snapshot(self.clients[email],self.options.get(email,{}))});return
        if path=='/api/inventory/manage':
            self.request_bodies.append((path,deepcopy(body)));send(self.management_result);return
        base.AccountSwitchUI.serve(self,route)

    def open_activity(self):
        self.page.locator('.act-toggle').click()
        self.page.wait_for_selector('.sup-card[data-cuid="123"]')

    def test_support_selection_survives_tap_poll_rebinding_and_navigation_in_both_themes(self):
        self.open_activity()
        for mode in ('dark','light'):
            self.page.evaluate('mode=>setTheme({mode})',mode)
            selected=self.page.locator('.sup-card[data-cuid="123"]');selected.tap()
            self.page.wait_for_timeout(250)
            self.assertEqual(selected.get_attribute('aria-pressed'),'true')
            self.assertEqual(self.page.evaluate('getActCfg().supportCuid'),'123')
            colors=selected.evaluate('n=>({border:getComputedStyle(n).borderColor,accent:getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),check:getComputedStyle(n,"::after").content})')
            self.assertIn('✓',colors['check'])
            self.page.evaluate('window.supportNode=document.querySelector(".sup-card.sel"); void loadTasks(); void loadTasks()')
            self.page.wait_for_timeout(70)
            self.assertTrue(self.page.evaluate('supportNode===document.querySelector(".sup-card.sel")'))
            self.page.locator('.sup-card[data-cuid="123"]').press('Enter')
            self.assertEqual(self.page.evaluate('getActCfg().supportCuid'),'123')
        self.page.evaluate("showSection('info');showSection('manual')")
        self.open_activity()
        self.assertEqual(self.page.locator('.sup-card.sel').get_attribute('data-cuid'),'123')
        self.page.screenshot(path=str(base.ROOT/'build/management-store-review/support-selected-mobile.png'))

    def test_inventory_widgets_are_last_and_notes_keep_initial_star_and_locks_clear(self):
        ids=self.page.locator('#taskList > div').evaluate_all('(nodes)=>nodes.slice(-3).map(n=>n.id)')
        self.assertEqual(ids,['manage-roles','manage-bonds','energy-management'])
        self.page.locator('#manage-roles .management-toggle').click()
        self.assertIn('初始三星、四星',self.page.locator('#manage-roles .management-note').inner_text())
        self.assertIn('西尔维纳',self.page.locator('#manage-roles .management-note').inner_text())
        self.page.locator('#manage-bonds .management-toggle').click()
        note=self.page.locator('#manage-bonds .management-note').inner_text()
        self.assertIn('满破＝等级上限 30',note)
        self.assertIn('贩卖多余羁绊',note)
        self.assertEqual(self.page.get_by_label('4星羁绊满破保留张数').input_value(),'2')
        self.page.get_by_label('4星羁绊满破保留张数').fill('3')
        self.page.locator('#manage-bonds .management-run').click()
        self.page.wait_for_function("document.querySelector('#manage-bonds .management-summary').textContent==='整理完成'")
        body=next(body for path,body in self.request_bodies if path=='/api/inventory/manage')
        self.assertEqual(body['options'],{'keep3':2,'keep4':3})
        self.page.screenshot(path=str(base.ROOT/'build/management-store-review/management-mobile.png'))

    def test_inventory_retrieval_options_persist_and_submit_for_both_management_types(self):
        for kind in ('roles','bonds'):
            with self.subTest(kind=kind):
                root=self.page.locator(f'#manage-{kind}')
                root.locator('.management-toggle').click()
                self.assertIn('先整理当前背包',root.locator('.management-note').inner_text())
                self.assertIn('背包满时自动再整理',root.locator('.management-note').inner_text())
                self.assertFalse(root.locator('.management-retrieve').is_checked())
                root.locator('.management-retrieve').locator('..').tap()
                self.page.evaluate("showSection('info');showSection('manual')")
                root=self.page.locator(f'#manage-{kind}')
                root.locator('.management-toggle').click()
                self.assertTrue(root.locator('.management-retrieve').is_checked())
                root.locator('.management-run').click()
                self.page.wait_for_function("kind=>document.querySelector('#manage-'+kind+' .management-summary').textContent==='整理完成'",arg=kind)
                request=[body for path,body in self.request_bodies if path=='/api/inventory/manage'][-1]
                self.assertEqual(request['kind'],kind)
                self.assertTrue(request['options']['retrieveLibrary'])
                if kind=='bonds':self.assertEqual(request['options'],{'keep3':2,'keep4':2,'retrieveLibrary':True})
                root.locator('.management-retrieve').locator('..').tap()
                root.locator('.management-run').click()
                self.page.wait_for_function("kind=>!managementRunning.alpha && document.querySelector('#manage-'+kind+' .management-summary').textContent==='整理完成'",arg=kind)
                request=[body for path,body in self.request_bodies if path=='/api/inventory/manage'][-1]
                self.assertNotIn('retrieveLibrary',request['options'])

    def test_full_bag_pause_shows_capacity_and_error_then_recovers_on_success(self):
        root=self.page.locator('#manage-bonds')
        root.locator('.management-toggle').click()
        for result,color in (
            ({'ok':False,'blocked':True,'detail':'羁绊整理暂停；背包占用 4/4，空位 0'},'var(--err)'),
            ({'ok':True,'detail':'羁绊整理完成；背包占用 4/8，空位 4'},'var(--ok)'),
        ):
            self.management_result=result
            root.locator('.management-run').click()
            self.page.wait_for_function("detail=>!managementRunning.alpha && document.querySelector('#manage-bonds .management-summary').textContent===detail",arg=result['detail'])
            summary=root.locator('.management-summary')
            self.assertEqual(summary.evaluate('node=>node.style.color'),color)
            self.assertIn('背包占用',summary.inner_text())
            self.assertFalse(root.locator('.management-run').is_disabled())

    def test_management_descriptions_show_live_account_specific_inventory_counts(self):
        self.management_status={
            'alpha':{'name':'alpha','inventoryManagement':{
                'roles':{'count':20,'capacity':100,'space':80,'library':3},
                'bonds':{'count':118,'capacity':145,'space':27,'library':0}}},
            'beta':{'name':'beta','inventoryManagement':{
                'roles':{'count':40,'capacity':50,'space':10,'library':9},
                'bonds':{'count':25,'capacity':70,'space':45,'library':11}}},
        }
        self.page.evaluate("refreshAccountStatus('alpha')")
        self.page.wait_for_function("document.querySelector('#manage-bonds .management-usage').textContent.includes('118/145')")
        self.assertIn('当前背包占用 118/145，空位 27，保管仓库 0',self.page.locator('#manage-bonds .tdesc').inner_text())
        self.assertIn('20/100，空位 80，保管仓库 3',self.page.locator('#manage-roles .tdesc').inner_text())
        self.page.evaluate("refreshAccountStatus('beta')")
        self.assertIn('118/145',self.page.locator('#manage-bonds .tdesc').inner_text())
        self.page.evaluate("switchAccount('beta')")
        self.page.wait_for_function("document.querySelector('#manage-bonds .management-usage').textContent.includes('25/70')")
        self.assertIn('40/50，空位 10，保管仓库 9',self.page.locator('#manage-roles .tdesc').inner_text())
        self.management_status['beta']['inventoryManagement']['bonds']={'count':30,'capacity':80,'space':50,'library':6}
        self.page.evaluate("refreshAccountStatus('beta')")
        self.page.wait_for_function("document.querySelector('#manage-bonds .management-usage').textContent.includes('30/80')")
        for width in (320,390,780):
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

    def test_store_is_under_activity_and_bought_targets_disable_only_sweep(self):
        self.open_activity()
        self.page.locator('.activity-store-toggle').click()
        self.page.locator('[data-store-item="p"]').locator('..').tap()
        self.page.locator('.activity-store-stop').locator('..').tap()
        self.page.wait_for_function('!activityStorePending.alpha')
        self.clients['alpha'].account_state['StoreRecordContainer']['Records']=[{'StaticID':'p','Store':'ActivityH806','BuyCount':2}]
        self.page.evaluate('loadActivityStore(true)')
        self.page.wait_for_function("document.querySelector('.act-start').disabled")
        self.assertTrue(self.page.locator('.act-store-complete').is_visible())
        self.assertTrue(self.page.locator('.act-scene-slot').evaluate('n=>n.inert'))
        self.page.locator('[data-act-mode="progress"]').click()
        self.assertFalse(self.page.locator('.act-start').is_disabled())
        self.page.locator('[data-act-mode="sweep"]').click()
        self.page.locator('.activity-store-stop').locator('..').tap()
        self.page.wait_for_function("!document.querySelector('.act-start').disabled")
        self.assertFalse(self.page.locator('.act-store-complete').is_visible())
        self.assertTrue(self.page.evaluate("document.querySelector('#act-activity_scene').nextElementSibling.id==='activity-store'"))

    def test_running_sweep_stops_after_purchase_logs_and_exchanges_coins(self):
        self.open_activity();self.page.locator('.activity-store-toggle').click()
        self.page.locator('[data-store-item="p"]').locator('..').tap();self.page.locator('.activity-store-stop').locator('..').tap()
        self.page.wait_for_function('!activityStorePending.alpha')
        self.page.evaluate("setActCfg({count:5,urgentEnabled:false,autoSell:false});document.querySelector('.act-start').click()")
        self.page.wait_for_function("!actRunning.alpha && document.querySelector('.act-start').disabled")
        self.assertEqual(sum(path=='/api/activity/run'for path,_ in self.calls),1)
        self.assertEqual(self.clients['alpha'].account_state['ItemContainer']['Items'][0]['Count'],0)
        self.assertIn('当前所需商品已购满',self.page.evaluate('activeAcc().logRows.map(r=>r.text || JSON.stringify(r)).join("\\n")'))
        self.page.screenshot(path=str(base.ROOT/'build/management-store-review/store-complete-mobile.png'))


if __name__=='__main__':unittest.main()
