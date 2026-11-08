"""Offline response holds against real login, task, guild and equipment controls."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from tools import test_account_switch_ui as base
from tools.test_guild_ui import summary
from tools.test_equipment_library import ui_fixture

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'build/button-wait-review'


class ActionWaitUI(unittest.TestCase):
    setUpClass = classmethod(base.AccountSwitchUI.setUpClass.__func__)
    tearDownClass = classmethod(base.AccountSwitchUI.tearDownClass.__func__)

    def setUp(self):
        self.hold_path = None
        self.held = []
        self.foreground = []
        self.equipment = ui_fixture()
        self.equipment['roles'] = [{'id': 'hero', 'staticID': 'H001', 'name': '测试角色', 'level': 60, 'star': 6, 'worn': 1}]
        self.equipment['equips'][0]['role'] = 'hero'
        base.AccountSwitchUI.setUp(self)

    def tearDown(self):
        for route in self.held:
            route.abort()
        base.AccountSwitchUI.tearDown(self)

    def serve(self, route):
        path = urlsplit(route.request.url).path
        body = route.request.post_data_json if route.request.method == 'POST' else {}
        if path == self.hold_path:
            self.foreground.append((path, deepcopy(body)))
            self.held.append(route)
            return
        if path == '/api/equipment/action':
            route.fulfill(content_type='application/json', body=json.dumps(self.equipment))
            return
        base.AccountSwitchUI.serve(self, route)

    def hold(self, path):
        self.hold_path = path

    def finish(self, value, status=200, index=0):
        self.held.pop(index).fulfill(status=status, content_type='application/json', body=json.dumps(value))

    def waiting(self, selector):
        self.page.wait_for_function("s => document.querySelector(s)?.hasAttribute('data-action-waiting')", arg=selector)
        self.assertTrue(self.page.locator(selector).is_disabled())
        self.assertEqual(self.page.locator(selector).get_attribute('aria-busy'), 'true')
        self.assertTrue(self.page.locator('#actionWaitStatus').is_visible())

    def settled(self):
        self.page.wait_for_function("!document.querySelector('[data-action-waiting]') && (!document.getElementById('actionWaitStatus') || document.getElementById('actionWaitStatus').hidden)")

    def enter_guild(self):
        self.page.evaluate("guild => { ACCOUNTS.alpha.guild=guild; showSection('guild'); }", summary())

    def test_score_entry_hidden_after_remount_and_normal_weekly_rewards_remain(self):
        self.enter_guild()
        for width in (320, 390, 1280):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.page.evaluate("showSection('info');showSection('guild')")
            self.assertEqual(self.page.locator('#guildScoreStart').count(), 1)
            self.assertTrue(self.page.locator('#guildScoreRow').is_hidden())
            self.assertTrue(self.page.locator('#guildWeeklyRow').is_visible())
        self.assertIn('async function runScore()', (ROOT / 'frontend/guild.js').read_text())

    def test_task_response_spins_blocks_repeat_and_restores_on_success_and_failure(self):
        self.hold('/api/tasks/run')
        button = self.page.locator('.task-run').first
        button.click()
        selector = f'.task-run[data-id="{button.get_attribute("data-id")}"]'
        self.waiting(selector)
        self.page.locator(selector).evaluate("b => {b.disabled=false; b.click(); b.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
        self.assertEqual(len(self.foreground), 1)
        self.assertNotEqual(self.page.locator(selector).evaluate("b => getComputedStyle(b,'::after').animationName"), 'none')
        for theme in ('dark', 'light'):
            self.page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
            self.page.screenshot(path=str(REVIEW / f'waiting-task-390-{theme}.png'))
        self.finish({'results': [{'ok': True, 'detail': '离线完成'}]})
        self.settled()
        self.assertTrue(self.page.locator(selector).is_enabled())
        self.page.locator(selector).click()
        self.waiting(selector)
        self.finish({'detail': '离线失败'}, status=500)
        self.settled()
        self.assertTrue(self.page.locator(selector).is_enabled())

    def test_keyboard_login_uses_same_lock_and_failure_allows_retry(self):
        self.page.evaluate("() => {activeAccount=null; window.dispatchEvent(new Event('lucima:account-change')); $('appView').classList.add('hidden'); $('loginView').classList.remove('hidden');}")
        self.page.locator('#account').fill('offline')
        self.page.locator('#password').fill('offline-test')
        self.hold('/api/login')
        self.page.locator('#password').press('Enter')
        self.waiting('#loginBtn')
        self.page.locator('#password').press('Enter')
        self.assertEqual(len(self.foreground), 1)
        self.finish({'detail': '离线登录失败'}, status=500)
        self.settled()
        self.assertTrue(self.page.locator('#loginBtn').is_enabled())

    def test_guild_refresh_preserves_disabled_state_when_connection_is_lost(self):
        self.enter_guild()
        self.hold('/api/guild/overview')
        self.page.locator('#guildRefresh').click()
        self.waiting('#guildRefresh')
        self.finish({'guild': summary(revision=5), 'connection': {'connected': False, 'reason': 'duplicate_login'}})
        self.settled()
        self.assertTrue(self.page.locator('#guildRefresh').is_disabled())

    def test_guild_switch_waits_without_moving_its_row(self):
        self.enter_guild()
        height = self.page.locator('.guild-signin-row').bounding_box()['height']
        self.hold('/api/toggles')
        self.page.locator('#guildAutoSignin').locator('..').click()
        self.waiting('#guildAutoSignin')
        self.assertAlmostEqual(self.page.locator('.guild-signin-row').bounding_box()['height'], height, delta=.5)
        self.finish({'ok': True})
        self.settled()
        self.assertTrue(self.page.locator('#guildAutoSignin').is_enabled())

    def test_guild_purchase_keeps_replacement_button_locked(self):
        self.enter_guild()
        self.page.locator('#guildManualStoreOptions').click()
        self.hold('/api/guild/store/buy')
        selector = '#guildManualStoreGoods [data-buy="GuildB003"]'
        if not self.page.locator(selector).count():
            product = self.page.locator('#guildManualStoreGoods [data-buy]').first.get_attribute('data-buy')
            selector = f'#guildManualStoreGoods [data-buy="{product}"]'
        self.page.locator(selector).click()
        self.waiting(selector)
        self.page.evaluate("window.dispatchEvent(new CustomEvent('lucima:guild-change',{detail:{account:'alpha'}}))")
        self.waiting(selector)
        self.page.locator(selector).evaluate("b => b.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        self.assertEqual(len(self.foreground), 1)
        self.finish({'guild': summary(revision=9)})
        self.settled()
        self.assertTrue(self.page.locator(selector).is_enabled())

    def test_equipment_lock_button_waits_for_response(self):
        self.page.evaluate("showSection('equipment')")
        self.page.wait_for_function("$('eqpStatus').textContent.includes('已读取')")
        self.page.locator('#eqpViewTabs [data-view="library"]').click()
        button = self.page.locator('[data-equip-lock]').first
        selector = f'[data-equip-lock="{button.get_attribute("data-equip-lock")}"]'
        for width, rendering, theme in ((390,'stable','dark'),(390,'stable','light'),(1280,'desktop','dark'),(1280,'desktop','light')):
            self.page.set_viewport_size({'width':width,'height':900})
            self.page.evaluate("v => {document.documentElement.dataset.rendering=v.rendering;document.documentElement.dataset.theme=v.theme}", {'rendering':rendering,'theme':theme})
            button=self.page.locator(selector)
            button.scroll_into_view_if_needed()
            def placement():
                return self.page.locator(selector).evaluate("b => {const r=b.getBoundingClientRect(), art=b.closest('.eqp-library-art').getBoundingClientRect(); return {x:r.x-art.x,y:r.y-art.y,width:r.width,height:r.height}}")
            before=placement()
            self.assertEqual(button.evaluate("b => getComputedStyle(b).position"),'absolute')
            self.hold('/api/equipment/action')
            button.click()
            self.waiting(selector)
            after=placement()
            for coordinate in ('x','y','width','height'):
                self.assertAlmostEqual(before[coordinate],after[coordinate],delta=.5)
            self.assertEqual(self.page.locator(selector).evaluate("b => getComputedStyle(b).position"),'absolute')
            self.assertNotEqual(self.page.locator(selector).evaluate("b => getComputedStyle(b,'::after').animationName"),'none')
            self.page.locator('.eqp-library-card').first.screenshot(path=str(REVIEW/f'lock-in-place-{width}-{theme}.png'))
            self.assertEqual(self.foreground[-1][1]['action'], 'setLock')
            self.finish({'overview': self.equipment, 'verified': True, 'detail': '已加锁'})
            self.settled()
            self.assertTrue(self.page.locator(selector).is_enabled())

    def test_confirmation_remains_usable_while_origin_button_waits(self):
        self.page.evaluate("showSection('equipment')")
        self.page.wait_for_function("$('eqpStatus').textContent.includes('已读取')")
        self.page.locator('#eqpTakeOffAll').click()
        self.waiting('#eqpTakeOffAll')
        self.assertTrue(self.page.locator('#eqpDialogCancel').is_enabled())
        self.page.locator('#eqpDialogCancel').click()
        self.settled()
        self.assertTrue(self.page.locator('#eqpTakeOffAll').is_enabled())

    def test_batch_stop_stays_usable_while_long_request_is_pending(self):
        self.page.evaluate("requestDelayRuntime={enabled:false}; setShopCfg({count:2,legOn:false})")
        self.page.locator('#shop-shop_diamond .shop-toggle').click()
        self.hold('/api/shop/reset')
        self.page.locator('.shop-start').click()
        self.page.wait_for_function("$('actionWaitStatus') && !$('actionWaitStatus').hidden")
        self.page.wait_for_function("document.querySelector('.shop-start').textContent === '停止'")
        self.assertTrue(self.page.locator('.shop-start').is_enabled())
        self.page.locator('.shop-start').click()
        self.page.wait_for_function("document.querySelector('.shop-start').disabled")
        self.page.wait_for_function("shopStopRequested.alpha === true")
        self.finish({'ok': True, 'bought': {}, 'legendaryMatches': []})
        self.settled()
        self.assertEqual(len(self.foreground), 1)
        self.assertTrue(self.page.locator('.shop-start').is_enabled())

    def test_concurrent_actions_clear_only_after_both_responses(self):
        self.page.evaluate("renderManualPanel([...TASKS,{id:'offline-second',name:'离线任务',desc:'并发响应检查'}])")
        self.hold('/api/tasks/run')
        buttons = self.page.locator('.task-run')
        buttons.nth(0).click()
        buttons.nth(1).click()
        self.page.wait_for_function("document.querySelectorAll('[data-action-waiting]').length === 2")
        self.finish({'results': [{'ok': True}]})
        self.page.wait_for_function("document.querySelectorAll('[data-action-waiting]').length === 1")
        self.assertTrue(self.page.locator('#actionWaitStatus').is_visible())
        self.finish({'results': [{'ok': True}]})
        self.settled()

    def test_polling_is_quiet_and_account_change_releases_old_locks(self):
        self.hold('/api/status')
        self.page.evaluate("void api('/api/status')")
        self.page.wait_for_function("true")
        self.assertEqual(self.page.locator('[data-action-waiting]').count(), 0)
        self.assertFalse(self.page.locator('#actionWaitStatus').count() and self.page.locator('#actionWaitStatus').is_visible())
        self.finish({'status': {}})
        self.hold('/api/tasks/run')
        self.page.locator('.task-run').first.click()
        self.page.wait_for_function("document.querySelector('[data-action-waiting]')")
        self.page.evaluate("switchAccount('beta')")
        self.settled()
        self.finish({'results': [{'ok': True}]})


if __name__ == '__main__':
    unittest.main()
