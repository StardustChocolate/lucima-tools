"""Global bottom-log preference survives module disposal, accounts and relogin."""
from copy import deepcopy
import json
import unittest
from urllib.parse import urlsplit

from tools import test_account_switch_ui as fixtures


class BottomLogPreference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixtures.AccountSwitchUI.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixtures.AccountSwitchUI.tearDownClass.__func__(cls)
    def setUp(self):
        self.preferences={};self.hold_preferences=False;self.pending_preferences=[]
        fixtures.AccountSwitchUI.setUp(self)
    def tearDown(self):
        for route,_ in self.pending_preferences:route.abort()
        fixtures.AccountSwitchUI.tearDown(self)
    def serve(self,route):
        if urlsplit(route.request.url).path=='/api/preferences':
            if route.request.method=='POST':
                data=route.request.post_data_json['settings']
                if self.hold_preferences:
                    self.pending_preferences.append((route,deepcopy(data)));return
                self.preferences=deepcopy(data)
            route.fulfill(content_type='application/json',body=json.dumps({'settings':self.preferences}));return
        fixtures.AccountSwitchUI.serve(self,route)
    def save(self):
        self.page.evaluate('async()=>{await flushFrontendSettings();}')
        self.page.wait_for_function('!frontendSettingsSaveInFlight && !frontendSettingsSavePending')
    def reload_and_enter(self):
        self.page.reload()
        self.page.wait_for_function("typeof enterApp==='function' && equipmentNotificationReady")
        self.page.evaluate("""async()=>{
          for(const account of ['alpha','beta'])rememberAccount({account,status:{name:account}});
          activeAccount='alpha';enterApp();await loadTasks();showSection('manual');
        }""")
    def enable(self):
        self.page.evaluate("showSection('logs')")
        self.page.locator('.log-dock-toggle').click()
        self.assertTrue(self.page.locator('#bottomLogEnabled').is_checked())
    def test_enabled_and_expanded_stay_after_remount_switch_logout_and_reload(self):
        self.enable()
        self.page.locator('#bottomLogExpand').click()
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.page.evaluate("showSection('manual')")
        self.assertEqual(self.page.locator('#bottomLogEnabled').count(),0)
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.page.evaluate("showSection('info');showSection('logs');switchAccount('beta')")
        self.assertTrue(self.page.locator('#bottomLogEnabled').is_checked())
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.page.evaluate("logTo('beta','新账号日志');showSection('manual')")
        self.page.wait_for_function("$('bottomLog').textContent.includes('新账号日志')")
        self.assertIn('新账号日志',self.page.locator('#bottomLog').inner_text())
        self.page.evaluate("""async()=>{
          await accLogout('alpha');await accLogout('beta');
          rememberAccount({account:'alpha',status:{name:'alpha'}});activeAccount='alpha';enterApp();showSection('logs');
        }""")
        self.assertTrue(self.page.locator('#bottomLogEnabled').is_checked())
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.save();self.reload_and_enter()
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.page.evaluate("showSection('logs')")
        self.assertTrue(self.page.locator('#bottomLogEnabled').is_checked())
        self.assertEqual(self.preferences['ark_bottom_log_enabled'],'1')
        self.assertEqual(self.preferences['ark_bottom_log_expanded'],'1')
    def test_disabled_stays_disabled_after_switch_remount_and_restart(self):
        self.enable();self.page.locator('.log-dock-toggle').click();self.save()
        for section in ['manual','logs','info','logs']:
            self.page.evaluate('id=>showSection(id)',section)
            self.assertFalse(self.page.locator('#bottomLogDock').is_visible())
        self.page.evaluate("switchAccount('beta')")
        self.assertFalse(self.page.locator('#bottomLogEnabled').is_checked())
        self.reload_and_enter();self.page.evaluate("showSection('logs')")
        self.assertFalse(self.page.locator('#bottomLogEnabled').is_checked())
        self.assertFalse(self.page.locator('#bottomLogDock').is_visible())
        self.assertEqual(self.preferences['ark_bottom_log_enabled'],'0')
    def test_expander_works_with_logs_module_unmounted_and_collapse_is_remembered(self):
        self.enable();self.page.evaluate("showSection('manual')")
        self.page.locator('#bottomLogExpand').click()
        self.assertTrue(self.page.evaluate('isBottomLogExpanded()'))
        self.page.locator('#bottomLogCollapse').click()
        self.page.evaluate("showSection('logs');showSection('manual')")
        self.assertFalse(self.page.evaluate('isBottomLogExpanded()'))
        self.assertTrue(self.page.locator('#bottomLogExpand').is_visible())
        self.save();self.reload_and_enter()
        self.assertFalse(self.page.evaluate('isBottomLogExpanded()'))
        self.assertTrue(self.page.locator('#bottomLogExpand').is_visible())
    def test_newest_toggle_is_saved_when_previous_save_is_still_inflight(self):
        self.page.evaluate("showSection('logs')");self.hold_preferences=True
        self.page.locator('.log-dock-toggle').click()
        self.page.evaluate('void flushFrontendSettings()')
        self.page.wait_for_function('frontendSettingsSaveInFlight')
        self.page.locator('.log-dock-toggle').click()
        self.page.wait_for_timeout(450)  # New debounce expires while previous request is still held.
        self.assertTrue(self.pending_preferences)
        self.assertTrue(self.page.evaluate('frontendSettingsSavePending'))
        self.hold_preferences=False
        route,body=self.pending_preferences.pop(0);self.preferences=body
        route.fulfill(content_type='application/json',body=json.dumps({'settings':body}))
        self.page.wait_for_function('!frontendSettingsSaveInFlight && !frontendSettingsSavePending')
        self.assertEqual(self.preferences['ark_bottom_log_enabled'],'0')
        self.reload_and_enter();self.assertFalse(self.page.locator('#bottomLogDock').is_visible())


if __name__=='__main__':unittest.main()
