"""Production UI: two main slots, shared picker and account-specific start payloads."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import unittest

from tools import test_account_switch_ui as fixtures

REVIEW=Path(__file__).resolve().parents[1]/'build/intimacy-main-choice-review'


class MainChoiceUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.AccountSwitchUI.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.AccountSwitchUI.tearDownClass.__func__(cls)
    def tearDown(self):fixtures.AccountSwitchUI.tearDown(self)

    def setUp(self):
        self.role_options=[{'roleId':rid,'staticId':sid,'name':name,'level':level,'avatar':'/assets/avatars/'+sid+'.png'}
            for rid,sid,name,level in [('r0','H002','菲莉斯',60),('r1','H006','璐茜娅',60),('r2','H026','卡洛琳',20)]]
        self.fail_roles=False
        fixtures.AccountSwitchUI.setUp(self)
        self.snapshots['alpha']={'running':False,'revision':2,'progress':None}
        self.page.evaluate("intimacyState.alpha={running:false};renderIntimacy('intimacy_auto')")
        REVIEW.mkdir(parents=True,exist_ok=True)

    def serve(self,route):
        if urlsplit(route.request.url).path=='/api/intimacy/options':
            route.fulfill(status=500 if self.fail_roles else 200,content_type='application/json',
                          body=json.dumps({'error':'角色暂时不可用'} if self.fail_roles else {'roles':self.role_options}))
            return
        fixtures.AccountSwitchUI.serve(self,route)

    def open_options(self):
        self.widget=self.page.locator('.intimacy-widget')
        self.widget.locator('.intimacy-toggle').click()
        self.widget.locator('.intimacy-refresh:not(:disabled)').wait_for()

    def test_safety_warning_is_only_shown_after_options_open(self):
        widget=self.page.locator('.intimacy-widget')
        self.assertNotIn('自动配队或借装',widget.inner_text())
        self.open_options()
        self.assertIn('自动配队或借装',widget.locator('.intimacy-config').inner_text())
        self.assertEqual(widget.locator('.loadout-safety-warning').count(),1)

    def choose_pair(self):
        self.widget.locator('.intimacy-main-slot[data-wave="upper"]').click()
        picker=self.page.locator('#abyssRolePicker')
        picker.locator('.abyss-role-option[data-role-id="r0"]').click()
        picker.locator('.abyss-role-option[data-role-id="r1"]').click()
        picker.get_by_role('button',name='完成',exact=True).click()

    def test_two_side_by_side_slots_choose_start_and_preserve_on_remount(self):
        self.open_options()
        for width in (320,390,768):
            self.page.set_viewport_size({'width':width,'height':900})
            slots=self.widget.locator('.intimacy-main-slot')
            self.assertEqual(slots.count(),2)
            upper,lower=[node.bounding_box()for node in slots.all()]
            self.assertLess(abs(upper['y']-lower['y']),1)
            self.assertLess(upper['x']+upper['width'],lower['x'])
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        self.page.set_viewport_size({'width':390,'height':1000})
        self.widget.locator('.intimacy-main-slot[data-wave="upper"]').click()
        picker=self.page.locator('#abyssRolePicker')
        self.assertEqual(picker.locator('[data-abyss-team-slot]').count(),2)
        slots=picker.locator('[data-abyss-team-slot]').all()
        for slot in slots: slot.wait_for(state='visible')
        cells=[node.bounding_box()for node in slots]
        self.assertTrue(all(cells),cells)
        self.assertLess(abs(cells[0]['y']-cells[1]['y']),1)
        picker.locator('.abyss-role-option[data-role-id="r0"]').click()
        picker.locator('.abyss-role-option[data-role-id="r1"]').click()
        self.page.screenshot(path=str(REVIEW/'main-picker-mobile.png'),full_page=True)
        picker.get_by_role('button',name='完成',exact=True).click()
        self.assertEqual(self.widget.locator('.intimacy-auto').count(),0)
        self.assertEqual(self.widget.locator('.intimacy-config-hint').inner_text(),'未配置主角色时自动配队。')
        self.page.evaluate("showSection('info');showSection('manual')")
        self.assertEqual(self.page.evaluate('getIntimacyCfg().mains'),{'upper':'r0','lower':'r1'})
        self.assertFalse(self.page.evaluate('getIntimacyCfg().autoConfigure'))
        self.widget.screenshot(path=str(REVIEW/'main-selection-mobile.png'))
        self.widget.locator('.intimacy-start').click()
        self.page.wait_for_function("intimacyState.alpha?.running===true")
        sent=[body for path,body in self.request_bodies if path=='/api/intimacy/start'][-1]
        self.assertEqual(sent,{'account':'alpha','mains':{'upper':'r0','lower':'r1'},'autoConfigure':False})
        self.assertTrue(self.widget.locator('.intimacy-main-slot').first.is_disabled())

    def test_empty_selection_automatically_configures_even_if_old_checkbox_was_off(self):
        self.open_options()
        self.page.evaluate("setIntimacyCfg({autoConfigure:false})")
        self.widget.locator('.intimacy-start').click()
        self.page.wait_for_function("intimacyState.alpha?.running===true")
        sent=[body for path,body in self.request_bodies if path=='/api/intimacy/start'][-1]
        self.assertEqual(sent['mains'],{'upper':None,'lower':None});self.assertTrue(sent['autoConfigure'])

    def test_one_main_is_preserved_and_only_missing_half_is_automatic(self):
        self.open_options()
        self.page.evaluate("setIntimacyCfg({mains:{upper:'r0',lower:null},autoConfigure:false});renderIntimacy('intimacy_auto')")
        self.widget.locator('.intimacy-start').click()
        self.page.wait_for_function("intimacyState.alpha?.running===true")
        sent=[body for path,body in self.request_bodies if path=='/api/intimacy/start'][-1]
        self.assertEqual(sent['mains'],{'upper':'r0','lower':None})
        self.assertTrue(sent['autoConfigure'])

    def test_selection_is_separate_per_account_and_does_not_change_abyss_team(self):
        self.open_options();self.choose_pair()
        self.page.evaluate("setAbyssCfg({upper:['abyss-role',null,null,null]});activeAccount='beta';renderIntimacy('intimacy_auto')")
        self.assertEqual(self.page.evaluate('getIntimacyCfg().mains'),{'upper':None,'lower':None})
        self.page.evaluate("activeAccount='alpha';renderIntimacy('intimacy_auto')")
        self.assertEqual(self.page.evaluate('getIntimacyCfg().mains'),{'upper':'r0','lower':'r1'})
        self.assertEqual(self.page.evaluate('getAbyssCfg().upper[0]'),'abyss-role')
        self.widget.locator('.intimacy-main-clear[data-wave="upper"]').click()
        self.assertEqual(self.page.evaluate('getIntimacyCfg().mains'),{'upper':None,'lower':'r1'})

    def test_role_load_failure_can_retry_and_navigation_releases_picker_nodes(self):
        self.fail_roles=True;self.open_options()
        self.assertIn('重试',self.widget.locator('.intimacy-config .intimacy-error').inner_text())
        self.fail_roles=False;self.widget.locator('.intimacy-refresh').click()
        self.widget.locator('.intimacy-refresh:not(:disabled)').wait_for()
        self.widget.locator('.intimacy-main-slot').first.click()
        self.page.locator('#abyssRolePicker .abyss-role-option').first.wait_for()
        self.page.evaluate("showSection('info')")
        self.assertFalse(self.page.locator('#abyssRolePicker').is_visible())
        self.assertEqual(self.page.locator('#abyssRolePicker .abyss-picker-list *').count(),0)
        self.assertEqual(self.page.locator('#section-manual *').count(),0)


if __name__=='__main__':unittest.main()
