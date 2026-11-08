"""Refresh after remount, real-response star balance and monthly equipment icons."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import unittest

from tools import test_account_switch_ui as base
from tools import test_recruitment_ui as archive_base
from tools.test_star_source_records_ui import star_data

REVIEW=Path(__file__).resolve().parents[1]/'build/release-1341-review'


class RefreshAndBalanceUI(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    tearDown=base.AccountSwitchUI.tearDown

    def setUp(self):
        self.refresh_requests=[];self.refresh_hold=False;self.refresh_pending=[]
        self.buy_failed=False;self.balance=13
        base.AccountSwitchUI.setUp(self)
        self.page.evaluate("applyStatus('alpha',{name:'alpha',currencies:{starSource:13}});applyStatus('beta',{name:'beta',currencies:{starSource:99}})")

    def serve(self,route):
        path=urlsplit(route.request.url).path
        if path=='/api/status/refresh':
            self.refresh_requests.append(route.request.post_data_json)
            if self.refresh_hold:self.refresh_pending.append(route)
            else:self.finish_refresh(route)
            return
        if path=='/api/star-source/buy':
            if not self.buy_failed:self.balance-=5
            route.fulfill(content_type='application/json',body=json.dumps({'ok':not self.buy_failed,'detail':'NotEnoughItem' if self.buy_failed else '购买成功',
                'status':{'name':'alpha','currencies':{'starSource':self.balance}}}))
            return
        base.AccountSwitchUI.serve(self,route)

    def finish_refresh(self,route):
        route.fulfill(content_type='application/json',body=json.dumps({'status':{'name':'重新连接成功','currencies':{'starSource':self.balance}},'connection':{'connected':True}}))

    def test_info_refresh_survives_repeated_remount_and_shares_the_reconnect_lock(self):
        self.page.evaluate("for(let i=0;i<3;i++){showSection('info');showSection('manual')}showSection('info');applyConnectionState('alpha',{connected:false})")
        self.refresh_hold=True
        self.page.locator('#refreshBtn').click()
        self.page.wait_for_function("document.getElementById('refreshBtn').disabled")
        self.assertTrue(self.page.locator('#reconnectBtn').is_disabled())
        self.assertEqual(self.refresh_requests,[{'account':'alpha'}])
        self.page.evaluate("showSection('manual');showSection('info')")
        self.assertTrue(self.page.locator('#refreshBtn').is_disabled())
        self.refresh_hold=False
        for route in self.refresh_pending:self.finish_refresh(route)
        self.refresh_pending.clear()
        self.page.wait_for_function("!document.getElementById('refreshBtn').disabled")
        self.assertEqual(self.page.locator('#connStatus').inner_text(),'已连接')
        self.page.locator('#refreshBtn').click()
        self.page.wait_for_function("!document.getElementById('refreshBtn').disabled")
        self.assertEqual(len(self.refresh_requests),2)

    def seed_buy_cards(self):
        card={'equipmentKey':'a'*64,'staticID':'E0011','name':'测试武器','level':85,'classLV':4,'tierName':'传说',
              'enhance':0,'set':'Critical','setCN':'暴击','slot':'weapon','slotCN':'武器','auxScore':33,
              'mainProp':{'label':'攻击','text':'100'},'subProps':[]}
        cards=[{**card,'shopIndex':i}for i in range(2)]
        self.page.locator('#starshop-star_source_shop .shop-toggle').click()
        self.page.evaluate("cards=>renderStarSourceState('star_source_shop',{kind:'matched',notification:true,cards},'alpha')",cards)

    def test_star_balance_updates_only_from_successful_purchase_responses(self):
        self.seed_buy_cards()
        balance=self.page.locator('.star-source-balance b')
        self.assertEqual(balance.inner_text(),'13')
        self.page.locator('[data-shop-index="0"]').click()
        self.page.wait_for_function("document.querySelector('.star-source-balance b').textContent==='8'")
        self.buy_failed=True
        self.page.locator('[data-shop-index="1"]').click()
        self.page.wait_for_function("document.querySelector('.star-source-card-error')?.textContent.includes('NotEnoughItem')")
        self.assertEqual(balance.inner_text(),'8')
        self.buy_failed=False
        self.page.locator('[data-shop-index="1"]').click()
        self.page.wait_for_function("document.querySelector('.star-source-balance b').textContent==='3'")
        self.page.evaluate("switchAccount('beta')")
        self.assertEqual(balance.inner_text(),'99')

    def test_star_balance_and_refresh_controls_fit_phone_widths(self):
        self.seed_buy_cards()
        for width in (320,390,820):
            self.page.set_viewport_size({'width':width,'height':900})
            bad=self.page.locator('#starshop-star_source_shop .shop-left').evaluate("root=>[...root.querySelectorAll('*')].filter(n=>{const b=n.getBoundingClientRect();return b.width>0&&n.scrollWidth>n.clientWidth+2}).map(n=>n.className)")
            self.assertEqual(bad,[],(width,bad))
            if width==390:
                REVIEW.mkdir(exist_ok=True,parents=True)
                self.page.locator('#starshop-star_source_shop').screenshot(path=str(REVIEW/'star-balance-390.png'))
        REVIEW.mkdir(exist_ok=True,parents=True)
        self.page.locator('#starshop-star_source_shop').screenshot(path=str(REVIEW/'star-balance.png'))


class DailyAndEquipmentUI(unittest.TestCase):
    setUpClass=classmethod(archive_base.RecruitmentUI.setUpClass.__func__)
    tearDownClass=classmethod(archive_base.RecruitmentUI.tearDownClass.__func__)
    tearDown=archive_base.RecruitmentUI.tearDown
    serve=archive_base.RecruitmentUI.serve
    finish=archive_base.RecruitmentUI.finish
    enter=archive_base.RecruitmentUI.enter

    def setUp(self):
        archive_base.RecruitmentUI.setUp(self)
        self.responses['first']=star_data()
        day=self.responses['first']['shop']['months'][0]['days'][0]
        day.update(paid=50,free=0,diamonds=150)
        self.enter()

    def test_daily_counts_have_colons_and_show_actual_paid_and_free_refreshes(self):
        self.page.locator('[data-tab=shop]').click()
        self.page.locator('[data-shop-days="2026-10"]').click()
        day=self.page.locator('[data-day="2026-10-29"]')
        self.assertIn('招募契约：5',day.locator('.ra-day-gains').inner_text())
        self.assertIn('钻石：3×50次=150',day.locator('.ra-day-costs').inner_text())
        self.assertIn('免费刷新次数：0',day.locator('.ra-day-costs').inner_text())
        self.assertNotIn('×',day.locator('.ra-day-gains').inner_text())

    def test_both_shops_show_real_slot_and_set_images_after_the_labels(self):
        for tab in ('shop','star'):
            self.page.locator(f'[data-tab={tab}]').click()
            rows=self.page.locator('.ra-equipment-row')
            self.assertEqual(rows.count(),2)
            self.assertEqual(rows.locator('img').count(),4)
            for img in rows.locator('img').all():
                img.evaluate("n=>n.loading='eager'")
                img.wait_for(state='visible')
            self.page.wait_for_function("[...document.querySelectorAll('.ra-equipment-row img')].every(i=>i.complete&&i.naturalWidth)")
            self.assertEqual(rows.first.locator('span img').get_attribute('src'),'/assets/slots/weapon.png')
            self.assertEqual(rows.first.locator('strong img').get_attribute('src'),'/assets/sets/Speed.png')


if __name__=='__main__':unittest.main()
