"""Equipment footer/log geometry and the requested compact record surfaces."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import unittest

from tools import test_account_switch_ui as base
from tools import test_release_1341_ui as previous

ROOT=Path(__file__).resolve().parents[1]
REVIEW=ROOT/'build/release-1342-review'


class EquipmentFooter(unittest.TestCase):
    setUpClass=classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass=classmethod(base.AccountSwitchUI.tearDownClass.__func__)
    tearDown=base.AccountSwitchUI.tearDown

    def serve(self,route):
        if urlsplit(route.request.url).path=='/api/equipment/action':
            if route.request.post_data_json['action'] in ('overview','reload'):
                return route.fulfill(content_type='application/json',body=json.dumps(self.overview))
        base.AccountSwitchUI.serve(self,route)

    def setUp(self):
        self.overview=json.loads((ROOT/'equipment/tests/fixtures/usability_overview.json').read_text())['overview']
        base.AccountSwitchUI.setUp(self)
        self.page.evaluate("showSection('equipment')")
        self.page.wait_for_function("document.querySelector('#eqpStatus').textContent.startsWith('已读取')")
        self.page.get_by_role('button',name='添加第一个配装角色',exact=True).click()
        search=self.page.get_by_label('搜索角色名或别称',exact=True)
        search.fill(self.overview['roles'][0]['name']);search.press('Enter')

    def test_footer_stays_at_scrollport_bottom_with_each_log_state_and_buttons_are_clear(self):
        report=[]
        for width in (320,390,820,1280):
            self.page.set_viewport_size({'width':width,'height':900})
            self.page.evaluate('setDrawer(false)')
            for log in ('disabled','collapsed','expanded'):
                self.page.evaluate("mode=>{setBottomLogEnabled(mode!=='disabled',false);setBottomLogExpanded(mode==='expanded')}",log)
                for fraction in (0,.5,1):
                    self.page.locator('.content').evaluate('(n,f)=>n.scrollTop=(n.scrollHeight-n.clientHeight)*f',fraction)
                    result=self.page.locator('.eqp-footer').evaluate("""n=>{
                      const r=n.getBoundingClientRect(),c=n.closest('.content'),b=c.getBoundingClientRect();
                      return {gap:b.bottom-r.bottom,reserved:parseFloat(getComputedStyle(c).paddingBottom),
                        buttons:[...n.querySelectorAll('button')].map(button=>{const a=button.getBoundingClientRect();
                          return {visible:a.top>=b.top&&a.bottom<=b.bottom,
                            clickable:document.elementFromPoint(a.x+a.width/2,a.y+a.height/2)===button}})};
                    }""")
                    self.assertGreaterEqual(result['gap'],result['reserved']-1,(width,log,fraction,result))
                    self.assertLessEqual(result['gap'],result['reserved']+24,(width,log,fraction,result))
                    self.assertTrue(all(b['visible']and b['clickable']for b in result['buttons']),(width,log,fraction,result))
                    report.append({'width':width,'log':log,'fraction':fraction,**result})
                if width==390:
                    self.page.screenshot(path=str(REVIEW/f'footer-{log}.png'))
        (REVIEW/'footer-layout.json').write_text(json.dumps(report,indent=2))


class CompactStarSource(unittest.TestCase):
    setUpClass=classmethod(previous.RefreshAndBalanceUI.setUpClass.__func__)
    tearDownClass=classmethod(previous.RefreshAndBalanceUI.tearDownClass.__func__)
    setUp=previous.RefreshAndBalanceUI.setUp
    tearDown=previous.RefreshAndBalanceUI.tearDown
    serve=previous.RefreshAndBalanceUI.serve
    finish_refresh=previous.RefreshAndBalanceUI.finish_refresh
    seed_buy_cards=previous.RefreshAndBalanceUI.seed_buy_cards

    def test_balance_contains_only_the_image_and_number_and_updates_after_buy(self):
        self.seed_buy_cards()
        balance=self.page.locator('.star-source-balance')
        self.assertEqual(balance.inner_text(),'13')
        self.assertEqual(balance.locator('img').count(),1)
        self.assertEqual(balance.locator('small').count(),0)
        self.assertEqual(balance.locator('img').get_attribute('alt'),'星源')
        self.page.locator('[data-shop-index="0"]').click()
        self.page.wait_for_function("document.querySelector('.star-source-balance').textContent==='8'")
        for width in (320,390,820):
            self.page.set_viewport_size({'width':width,'height':900})
            values=self.page.locator('#starshop-star_source_shop .shop-left').evaluate("root=>[...root.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().width&&n.scrollWidth>n.clientWidth+2).map(n=>n.className)")
            self.assertEqual(values,[],(width,values))
            if width==390:self.page.locator('#starshop-star_source_shop .shop-left').screenshot(path=str(REVIEW/'star-compact.png'))


class RecordPartColors(unittest.TestCase):
    setUpClass=classmethod(previous.DailyAndEquipmentUI.setUpClass.__func__)
    tearDownClass=classmethod(previous.DailyAndEquipmentUI.tearDownClass.__func__)
    setUp=previous.DailyAndEquipmentUI.setUp
    tearDown=previous.DailyAndEquipmentUI.tearDown
    serve=previous.DailyAndEquipmentUI.serve
    finish=previous.DailyAndEquipmentUI.finish
    enter=previous.DailyAndEquipmentUI.enter

    def test_equipment_part_text_is_white_in_dark_mode_and_readable_in_light_mode(self):
        for tab in ('shop','star'):
            self.page.locator(f'[data-tab={tab}]').click()
            for mode in ('dark','light'):
                self.page.evaluate('mode=>setTheme({mode})',mode)
                colors=self.page.locator('.ra-equipment-row>span').evaluate_all('nodes=>nodes.map(n=>getComputedStyle(n).color)')
                self.assertTrue(colors)
                if mode=='dark':self.assertEqual(set(colors),{'rgb(255, 255, 255)'})
                else:self.assertTrue(all(c!='rgb(255, 255, 255)'for c in colors))


if __name__=='__main__':unittest.main()
