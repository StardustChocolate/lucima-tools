"""Two-column event goods, active account buffs and shorter role notes."""
from pathlib import Path
import unittest

from tools import test_management_store_ui as base

REVIEW=Path(__file__).resolve().parents[1]/'build/followup-1348-review'


class FollowupUI(unittest.TestCase):
    setUpClass=classmethod(base.ManagementStoreUI.setUpClass.__func__)
    tearDownClass=classmethod(base.ManagementStoreUI.tearDownClass.__func__)
    setUp=base.ManagementStoreUI.setUp
    tearDown=base.ManagementStoreUI.tearDown
    serve=base.ManagementStoreUI.serve

    def test_store_uses_two_columns_when_the_space_fits_without_horizontal_overflow(self):
        self.page.locator('.activity-store-toggle').click()
        self.page.wait_for_selector('.activity-store-good')
        measurements=[]
        for width in (320,360,390,820,1280):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.page.evaluate('setDrawer(false)')
            layout=self.page.locator('.activity-store-goods').evaluate('''root=>{
              const cards=[...root.children].map(n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width};});
              return {width:root.clientWidth,cards,overflow:root.scrollWidth-root.clientWidth};
            }''')
            self.assertLessEqual(layout['overflow'],1,(width,layout))
            columns=2 if layout['width']>=280 else 1
            self.assertEqual(layout['cards'][0]['y']==layout['cards'][1]['y'],columns==2,(width,layout))
            self.assertGreater(layout['cards'][2]['y'],layout['cards'][0]['y'],(width,layout))
            measurements.append({'viewport':width,'columns':columns,**layout})
            if width==390:
                self.page.locator('#activity-store').screenshot(path=str(REVIEW/'event-store-two-columns.png'))
        self.page.locator('[data-quantity="u"]').fill('3')
        self.page.locator('[data-store-item="u"]').locator('..').click()
        self.page.wait_for_function('!activityStorePending.alpha')
        self.assertEqual(self.options['alpha']['ActivityH806']['quantities']['u'],3)
        import json
        (REVIEW/'event-store-layout.json').write_text(json.dumps(measurements,indent=2))

    def test_buff_display_keeps_guild_and_monthly_card_but_hides_cached_vip_entries(self):
        self.page.evaluate('''() => {
          showSection('info');
          applyStatus('alpha',{name:'alpha',buffs:{available:true,items:[
            {id:'vip',staticId:'VIPHeroExp10',source:'会员',name:'VIP经验增益'},
            {id:'vip2',staticId:'VIPMoneyBonus10',source:'账号',name:'VIP金币增益'},
            {id:'guild',staticId:'GuildMoneyBonus',source:'佣兵团',name:'金币获取提升',description:'金币获取提升 10%',expiresAtMs:Date.now()+3600000},
            {id:'month',staticId:'TIME07',source:'月卡',name:'月卡经验增益',description:'团员经验获取提升 10%',expiresAtMs:Date.now()+86400000}
          ]}});
        }''')
        root=self.page.locator('#accountBuffs')
        self.assertEqual(root.locator('.account-buff').count(),2)
        self.assertEqual(root.locator('.account-buffs-heading b').inner_text(),'2')
        root.locator('.account-buffs-heading').click()
        text=root.inner_text()
        self.assertIn('佣兵团',text);self.assertIn('月卡',text);self.assertNotIn('VIP',text);self.assertNotIn('会员',text)
        root.screenshot(path=str(REVIEW/'account-active-buffs.png'))

    def test_role_note_omits_insufficient_material_sentence_without_changing_bond_note(self):
        self.page.locator('#manage-roles .management-toggle').click()
        text=self.page.locator('#manage-roles .management-note').inner_text()
        self.assertNotIn('素材不足',text)
        self.assertIn('初始三星、四星',text);self.assertIn('请先在游戏内加锁',text)
        self.assertIn('素材不足先保留',self.page.locator('#manage-bonds .management-note').text_content())


if __name__=='__main__':unittest.main()
