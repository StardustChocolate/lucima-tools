"""Offline regressions for real friend cards, initial stars and patrol timing."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from backend import appcore, tasks
from backend.guild import GuildService
from tools import test_auto_rewards_1030 as rewards, test_guild_checkin_progress as checkin


class FriendAndRosterTests(unittest.TestCase):
    def setUp(self):
        self.card = {'IsFriend': True, 'PlayerInfo': {'CUID': 456, 'Name': 'BiuBiu诺'},
                     'RoleDataList': [{'SupportJob': 'Sniper', 'Role': {'StaticID': 'H019', 'Star': 6, 'LV': 60}}]}
        def call(route, data):
            if route == 'FriendHandler.QueryFriendContainer':
                self.assertEqual(data, {'Type': 'FriendList'})
                return {'FriendInfos': [{'CUID': 456, 'Name': 'BiuBiu诺'}, {'CUID': 789, 'Name': '其他好友'}]}
            if route == 'AccountHandler.QueryPlayerCardData':
                self.assertEqual(data, {'CUID': 456})
                return {'BattleSupportData': deepcopy(self.card)}
            raise AssertionError(route)
        self.client = SimpleNamespace(call=Mock(side_effect=call), _auth_data=lambda x: x)
        self.acc = SimpleNamespace(email='offline', lock=threading.Lock(), client=self.client, support_cache={})
        self.enterContext(patch.object(appcore, '_require', return_value=self.acc))
        self.enterContext(patch.object(appcore.config, 'load_support_favs', return_value={}))

    def test_substring_search_finds_friend_outside_random_list_without_captain(self):
        for query in ['Biu', 'BiuBiu', 'BiuBiu诺', 'biubiu', '456']:
            with self.subTest(query=query):
                result = appcore.activity_support_search('offline', query)
                self.assertEqual([(r['cuid'], r['name']) for r in result['supports']], [(456, 'BiuBiu诺')])
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(tasks._build_support(self.acc.support_cache['456'])['Job'], 'Sniper')

    def test_last_friend_is_resolved_after_restarting_without_a_random_match(self):
        entry = appcore._activity_support_entry(self.acc, '456')
        self.assertEqual(entry['PlayerInfo']['Name'], 'BiuBiu诺')
        self.assertIs(appcore._activity_support_entry(self.acc, '456'), entry)
        self.assertEqual(self.client.call.call_count, 2)

    def test_roster_uses_official_initial_star_and_light_dark_first_within_star(self):
        table = appcore.battle_metadata()['tables']['Role']
        ids = ['H019', 'H023', 'H001']
        roster = appcore._roster({'RoleDataContainer': {'Roles': [
            {'StaticID': sid, 'Star': 6, 'LV': 60} for sid in ids]}})['heroes']
        by_id = {row['id']: row for row in roster}
        for sid in ids:
            self.assertEqual(by_id[sid]['initialStar'], int(table[sid]['InitStar']))
        self.assertEqual(by_id['H019']['initialStar'], 5)
        keys = [(-row['initialStar'], 0 if row['element'] in ('Light', 'Dark') else 1) for row in roster]
        self.assertEqual(keys, sorted(keys))


class PatrolTimingTests(unittest.TestCase):
    setUp = rewards.AutoRewardTests.setUp
    shelf = rewards.AutoRewardTests.shelf
    enable_shop = rewards.AutoRewardTests.enable_shop

    def test_midway_inspection_does_not_delay_actual_hourly_refresh(self):
        self.enable_shop()
        original = self.now
        self.now += 30 * 60_000
        self.assertTrue(self.acc.run_due()[0]['ok'])
        deadline = original + tasks.SHOP_FREE_COOLDOWN_MS
        self.assertEqual(self.acc.next_claims()['shop_free']['nextMs'], deadline)
        self.now = deadline - 1
        self.assertEqual(self.acc.run_due(), [])
        self.now = deadline
        self.assertIn('shop_free', self.acc._due_ids())

    def test_inventory_updates_do_not_requeue_signin_shop_or_guild_login(self):
        self.enable_shop(); self.acc.run_due()
        self.acc.toggles['month_signin'] = True
        revision = self.acc.login_revision
        self.acc.task_runtime['month_signin'] = {'checkedRevision': revision}
        self.acc.guild_runtime.update(enteredRevision=revision, custom='keep')
        for _ in range(5):
            self.acc.note_inventory_changed()
        self.assertEqual(self.acc.login_revision, revision)
        self.assertEqual(self.acc.guild_runtime['custom'], 'keep')
        self.assertNotIn('shop_free', self.acc._due_ids())
        self.assertNotIn('month_signin', self.acc._due_ids())


class AttendanceSyncTests(unittest.TestCase):
    def test_checkin_refreshes_today_count_without_changing_yesterday(self):
        c = checkin.CheckInProgress().setup_client()
        original = c.call
        def call(route, data=None):
            result = original(route, data)
            if route in ('GuildHandler.QueryGuildDataForSync', 'GuildHandler.QueryFullGuildData'):
                result['GuildData']['Record'].update(NowCheckInCount=1, LastCheckInCount=0)
            return result
        c.call = call
        service = GuildService(c, {})
        self.assertTrue(service.checkin_if_due())
        record = c.account_state['GuildData']['Record']
        self.assertEqual((record['NowCheckInCount'], record['LastCheckInCount']), (1, 0))
        member = c.account_state['GuildData']['MemberList'][0]['PlayerGuildInfo']
        self.assertEqual(member['LastCheckInTime'], c.account_state['PlayerGuildInfo']['LastCheckInTime'])
        self.assertFalse(service.checkin_if_due())
        self.assertEqual(len(c.routes('QueryGuildDataForSync')), 1)


if __name__ == '__main__':
    unittest.main()
