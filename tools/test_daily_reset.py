"""Offline regression checks for the login prerequisite at the 08:00 game boundary."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import httpx

# Reuse the transport-free fixtures and their temporary application data directory.
from tools.test_guild_aid import AidClient, aid_record
from tools.test_guild_donation import DonationClient
from tools.test_guild_weekly import WeeklyClient, quest
from tools.test_guild_store import StoreClient
from backend import accounts, appcore, config, tasks
from backend.game_client import GameClient, GameError, TaskDeferred
from backend.guild import GUILD_ENTRY_ROUTES, GuildService
from backend.guild_aid import GuildAidService, aid_options
from backend.guild_quests import GuildWeeklyService
from backend.guild_store import GuildStoreService
from backend.updater import service as update_service


TZ = timezone(timedelta(hours=8))
DAY = accounts.DAY_MS


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


class DailyClient(DonationClient):
    def bootstrap(self):
        self.calls.append(('AccountHandler.Login', {}))
        self.account_state['Utc'] = str(self.now // 1000)
        self.account_state['PlayerGuildInfo'] = copy.deepcopy(self.own)
        self.connected = True
        return self.account_state

    def call(self, route, data=None):
        if route.startswith('Test.'):
            self.calls.append((route, data))
            return {}
        return super().call(route, data)


class DailyResetTests(unittest.TestCase):
    def setUp(self):
        self.now = stamp('2026-10-26 07:59:59')
        self.enterContext(patch('time.time', side_effect=lambda: self.now / 1000))
        self.enterContext(patch.object(config, 'load_accounts', return_value={}))
        self.enterContext(patch.object(config, 'save_account'))
        self.enterContext(patch.object(config, 'get_request_delay_config', return_value={'enabled': False, 'ms': 0, 'jitter': 0}))
        self.enterContext(patch.object(update_service, 'is_installing', return_value=False))
        self.enterContext(patch('httpx.Client.request', side_effect=AssertionError('No live requests')))
        self.enterContext(patch.dict(tasks.TASKS, {'daily_probe': {
            'kind': 'toggle', 'name': 'Probe', 'fn': self.probe}}))
        self.client, self.acc = self.make_account('first@example.invalid')

    @staticmethod
    def probe(client):
        client.call('Test.Automatic', client._auth_data())
        return {'ok': True, 'detail': 'done'}

    def make_account(self, email):
        client = DailyClient()
        client.now = self.now
        client.account_state['Utc'] = str(self.now // 1000)
        client.own['LastCheckInTime'] = {'$date': self.now}
        client.account_state['PlayerGuildInfo'] = copy.deepcopy(client.own)
        acc = accounts.Account(email, client, None)
        client.account = acc
        acc.toggles['daily_probe'] = True
        acc.params['guild_auto_donate'] = {'reserve': {'gold': 0, 'courage': 0}}
        return client, acc

    def move(self, value):
        self.now = stamp(value)
        self.client.now = self.now

    def login(self):
        self.client.bootstrap()
        self.acc.note_state_refreshed()

    def test_daily_random_window_ends_and_midnight(self):
        for offset, expected in [(0, '08:05:00'), (600, '08:15:00')]:
            with patch.object(accounts.random, 'randint', return_value=offset):
                for value, date in [('2026-10-26 07:59:59', '2026-10-26'),
                                    ('2026-10-26 08:00:00', '2026-10-27')]:
                    actual = accounts._next_daily_state_refresh(datetime.fromisoformat(value).replace(tzinfo=TZ))
                    self.assertEqual(actual * 1000, stamp(date + ' ' + expected))
        self.move('2026-10-26 00:00:00')
        client, acc = self.make_account('midnight@example.invalid')
        self.now += 1_000
        self.assertTrue(acc.daily_state_current())

    def test_all_toggles_are_blocked_on_monday_and_ordinary_day(self):
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.assertTrue(self.acc.run_toggles(['daily_probe'], 'test')[0]['ok'])
        for date in ['2026-10-26', '2026-10-27']:
            with self.subTest(date=date):
                self.move(date + ' 08:00:00')
                self.acc.toggles.update({tid: True for tid, meta in tasks.TASKS.items() if meta.get('kind') == 'toggle'})
                count = len(self.client.calls)
                self.assertEqual(self.acc._due_ids(), [])
                self.assertEqual(self.acc.run_toggles(list(self.acc.toggles), 'test'), [])
                self.acc._run_guild_if_due()
                self.assertEqual(len(self.client.calls), count)
                claims = self.acc.next_claims()
                self.assertTrue(all(not row['ready'] and row['waiting'] == '等待每日重登' for row in claims.values()))

    def test_failed_login_toggle_changes_and_guild_queries_do_not_release_gate(self):
        self.move('2026-10-26 08:06:00')
        self.acc._state_refresh_at = self.now / 1000
        with patch.object(self.client, 'bootstrap', side_effect=GameError('offline')) as login:
            self.assertFalse(self.acc._refresh_state_if_due())
            self.assertEqual(self.acc._state_refresh_at, self.now / 1000 + 300)
            self.acc.toggles['send_meal'] = True
            self.acc.notify_config_changed()
            self.acc._run_guild_if_due()
            with self.assertRaises(TaskDeferred):
                GuildService(self.client, self.acc.guild_runtime).query()
            self.assertEqual(self.acc.run_toggles(['send_meal', 'daily_probe'], 'test'), [])
            self.assertEqual(self.acc._next_wake_delay(), 300)
            self.assertEqual(login.call_count, 1)
        self.assertEqual(self.acc.automation_wait_reason(), '等待每日重登')
        self.assertEqual(self.client.calls, [])

    def test_successful_login_requires_guild_sync_and_accounts_are_isolated(self):
        _, second = self.make_account('second@example.invalid')
        self.move('2026-10-26 08:06:00')
        self.login()
        self.assertEqual(self.acc.automation_wait_reason(), '等待佣兵团同步')
        self.assertEqual(second.automation_wait_reason(), '等待每日重登')
        self.assertEqual(self.acc.run_toggles(['daily_probe'], 'test'), [])
        self.acc._run_guild_if_due()
        self.assertIsNone(self.acc.automation_wait_reason())
        self.assertTrue(self.acc.run_toggles(['daily_probe'], 'test')[0]['ok'])

    def test_scheduled_login_success_releases_daily_gate(self):
        self.move('2026-10-26 08:06:00')
        self.acc._state_refresh_at = self.now / 1000
        self.assertTrue(self.acc._refresh_state_if_due())
        self.assertTrue(self.acc.daily_state_current())
        self.assertEqual(self.acc.automation_wait_reason(), '等待佣兵团同步')

    def test_recovery_login_success_releases_daily_gate(self):
        self.move('2026-10-26 08:06:00')
        status = lambda: {'connected': self.client.connected, 'changedAt': self.now - 1_000}
        self.client.connected = False
        with patch.object(self.client, 'connection_status', side_effect=status), patch.object(
                config, 'get_auto_relogin_config', return_value={'enabled': True, 'delaySeconds': 0}):
            self.assertTrue(self.acc._auto_relogin_if_due())
        self.assertTrue(self.acc.daily_state_current())
        self.assertEqual(self.acc.automation_wait_reason(), '等待佣兵团同步')

    def test_failed_guild_entry_keeps_ordinary_tasks_waiting_but_allows_login_tasks(self):
        self.move('2026-10-26 08:06:00')
        self.login()
        original = self.client.call
        def call(route, data=None):
            if route == 'GuildHandler.QueryFullGuildData':
                raise GameError('query timeout')
            return original(route, data)
        with patch.object(self.client, 'call', side_effect=call):
            self.acc._run_guild_if_due()
        self.assertEqual(self.acc.automation_wait_reason(), '等待佣兵团同步')
        self.assertEqual(self.acc.run_toggles(['daily_probe'], 'test'), [])
        self.acc.toggles['month_signin'] = True
        with patch.dict(tasks.TASKS['month_signin'], {'fn': lambda c, p, r: self.probe(c)}):
            self.assertTrue(self.acc.run_toggles(['month_signin'], 'test')[0]['ok'])
        self.assertEqual(self.acc._next_wake_delay(), 300)
        self.now += 300_000
        self.client.now = self.now
        self.acc._run_guild_if_due()
        self.assertIsNone(self.acc.automation_wait_reason())

    def test_scheduler_order_login_tasks_queries_checkin_donation_then_ordinary(self):
        self.move('2026-10-26 08:06:00')
        self.acc._state_refresh_at = self.now / 1000
        self.acc.toggles['guild_auto_donate'] = True
        def login_phase(prefix, *, login_only):
            if login_only:
                with self.acc._request_scope(login_task=True):
                    self.client.call('Test.Signin', self.client._auth_data())
                    self.client.call('Test.Meal', self.client._auth_data())
            else:
                self.acc.run_toggles(['daily_probe'], prefix)
                self.acc._stop.set()
        with patch.object(self.acc, 'run_due', side_effect=login_phase), patch.object(
                self.acc, '_run_startup_tasks'), patch.object(accounts.redeem_codes, 'refresh_if_due'), patch.object(
                accounts.redeem_codes, 'revision', return_value=0):
            self.acc._run_loop()
        routes = [route for route, _ in self.client.calls]
        expected = ['AccountHandler.Login', 'Test.Signin', 'Test.Meal'] + [
            route for _, route, _ in GUILD_ENTRY_ROUTES] + ['GuildHandler.GuildMemberCheckIn',
            'GuildHandler.DonateGold', 'GuildHandler.DonateCourage', 'Test.Automatic']
        self.assertEqual(routes, expected)

    def test_crossing_day_between_requests_stops_batch_without_retry(self):
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        def batch(c):
            self.probe(c)
            self.move('2026-10-26 08:00:00')
            return self.probe(c)
        with patch.dict(tasks.TASKS['daily_probe'], {'fn': batch}):
            result = self.acc.run_toggles(['daily_probe'], 'test')[0]
        self.assertTrue(result['deferred'])
        self.assertNotIn('retryAtMs', self.acc.task_runtime['daily_probe'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.acc.logs_since(0), [])

    def test_waiting_for_account_lock_rechecks_day(self):
        class CrossDayLock:
            def __enter__(inner):
                self.move('2026-10-26 08:00:00')
            def __exit__(inner, *args):
                pass
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.acc.lock = CrossDayLock()
        self.assertEqual(self.acc.run_toggles(['daily_probe'], 'test'), [])
        self.assertEqual(self.client.calls, [])

    def test_manual_guild_and_compatibility_auto_routes_cannot_bypass_gate(self):
        self.move('2026-10-26 08:00:00')
        with patch.object(appcore, '_require', return_value=self.acc), patch.object(accounts, 'get', return_value=self.acc):
            with self.assertRaises(appcore.ApiError) as caught:
                appcore.guild_overview(self.acc.email)
            self.assertIn('等待每日重登', str(caught.exception))
            result = appcore.run(self.acc.email, ['daily_probe'], {})
            self.assertIn('等待每日重登', result['results'][0]['detail'])
        self.assertEqual(self.client.calls, [])

    def test_actual_transport_checks_day_before_sending_preauthenticated_payload(self):
        payload = self.client._auth_data()
        self.client._ensure_request_transport()
        self.move('2026-10-26 08:00:00')
        with self.acc._request_scope(), patch.object(self.client, '_call_serial') as send:
            with self.assertRaises(TaskDeferred):
                GameClient.call(self.client, 'Test.Automatic', payload)
        send.assert_not_called()

    def test_http_transport_rechecks_after_waiting_for_transport_lock(self):
        class CrossDayLock:
            def __enter__(inner):
                self.move('2026-10-26 08:00:00')
            def __exit__(inner, *args):
                pass
        self.client._ensure_request_transport()
        self.client._request_serial_lock = CrossDayLock()
        self.client._client = Mock()
        with self.acc._request_scope(), self.assertRaises(TaskDeferred):
            self.client._request_http('PUT', 'https://offline.invalid', game_route='Test.Automatic')
        self.client._client.request.assert_not_called()

    def test_proxy_retry_cannot_send_again_after_crossing_day(self):
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.client._sync_proxy = Mock()
        self.client._proxy = 'http://offline.invalid'
        self.client._client = Mock()
        direct = Mock()
        self.client._build_client = Mock(return_value=direct)
        def connect_failure(*args, **kwargs):
            self.move('2026-10-26 08:00:00')
            raise httpx.ConnectError('offline connection')
        self.client._client.request.side_effect = connect_failure
        with self.acc._request_scope(), self.assertRaises(TaskDeferred):
            GameClient.call(self.client, 'Test.Automatic', self.client._auth_data())
        direct.request.assert_not_called()

    def test_no_guild_account_resumes_after_login_without_guild_query(self):
        self.move('2026-10-26 08:06:00')
        self.client.info.pop('GuildSubInfo')
        self.login()
        self.assertIsNone(self.acc.automation_wait_reason())
        self.assertTrue(self.acc.run_toggles(['daily_probe'], 'test')[0]['ok'])

    def test_crossing_day_between_two_donations_keeps_confirmed_first_result(self):
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.acc.toggles['guild_auto_donate'] = True
        self.client.after_donate = lambda kind: self.move('2026-10-26 08:00:00')
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [('GuildHandler.DonateGold', 10)])
        self.assertNotIn('donationErrors', self.acc.guild_runtime)
        self.assertTrue(self.acc.logs_since(0)[0]['result']['ok'])

    def test_blocked_first_npc_request_does_not_create_uncertain_result(self):
        self.client.account_state['Teams'] = {'Settings': [
            {'ID': '0', 'TeamSetting': {'RolePosMap': {'role': 0}}}]}
        self.move('2026-10-26 08:00:00')
        with self.acc._request_scope(), patch.object(tasks, '_build_role_for_battle', return_value={}), patch.object(
                tasks, '_build_arena_enemy', return_value=({}, [])):
            with self.assertRaises(TaskDeferred):
                tasks.arena_sweep_once(self.client, 1, '0')
        self.assertEqual(self.client.calls, [])

    def test_npc_ticket_before_boundary_retains_waiting_for_login_if_settlement_blocked(self):
        self.client.account_state['Teams'] = {'Settings': [
            {'ID': '0', 'TeamSetting': {'RolePosMap': {'role': 0}}}]}
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        def ticket(route, data=None):
            self.client.calls.append((route, data))
            self.move('2026-10-26 08:00:00')
            return {'LogID': 'confirmed-ticket'}
        with self.acc._request_scope(), patch.object(tasks, '_build_role_for_battle', return_value={}), patch.object(
                tasks, '_build_arena_enemy', return_value=({}, [])), patch.object(self.client, 'call', side_effect=ticket):
            result = tasks.arena_sweep_once(self.client, 1, '0')
        self.assertTrue(result['waitingForLogin'])
        self.assertEqual([route for route, _ in self.client.calls], [tasks.ARENA_CHECK_ROUTE])


class GuildWeekSyncTests(unittest.TestCase):
    def setUp(self):
        self.client = WeeklyClient()
        self.now = self.client.now
        self.enterContext(patch('time.time', side_effect=lambda: self.now / 1000))
        self.enterContext(patch.object(config, 'load_accounts', return_value={}))
        self.enterContext(patch('httpx.Client.request', side_effect=AssertionError('No live requests')))
        self.acc = accounts.Account('weekly-boundary@example.invalid', self.client, None)
        self.service = GuildService(self.client, self.acc.guild_runtime)
        self.weekly = GuildWeeklyService(self.service)
        self.client.account_state['GuildData'] = self.client.full()

    def response(self):
        own = copy.deepcopy(self.client.own)
        own['NowGuildQuestWeek'] += 1
        week = own['NowGuildQuestWeek']
        return {'PlayerGuildData': {'PlayerGuildInfo': own},
                'PlayerGuildQuestList': {'Quests': [quest('GuildCheckIn', 0, 5), quest('GuildGetRing', 0, 10)]},
                'Quests': [quest('GuildCheckIn', 1, 5)],
                'GuildQuestList': [{**row, 'Week': week} for row in self.client.tier_rows()]}

    def test_new_week_quest_lists_replace_old_guild_tasks_and_keep_other_tasks(self):
        self.client.account_state['QuestContainer']['Quests'].append(quest('UnrelatedDaily', 7, 10))
        self.service._cache(self.response(), 'GuildHandler.GuildMemberCheckIn')
        rows = {row['StaticID']: row for row in self.client.account_state['QuestContainer']['Quests']}
        self.assertEqual(set(rows), {'GuildCheckIn', 'GuildGetRing', 'UnrelatedDaily'})
        self.assertEqual(rows['GuildCheckIn']['Count'], 1)
        self.assertEqual(rows['GuildGetRing']['Count'], 0)
        self.assertEqual(rows['UnrelatedDaily']['Count'], 7)
        self.assertTrue(self.weekly.snapshot()['personalKnown'])
        self.assertEqual(self.weekly.snapshot()['week'], self.client.week + 1)

    def test_default_and_foreign_member_cannot_replace_weekly_lists(self):
        before = copy.deepcopy(self.client.account_state['QuestContainer'])
        for info in [{'NowGuildQuestWeek': 0}, {'GID': 'foreign', 'NowGuildQuestWeek': 999}]:
            response = self.response()
            response['PlayerGuildData']['PlayerGuildInfo'] = info
            self.service._cache(response, 'GuildHandler.GuildMemberCheckIn')
            self.assertEqual(self.client.account_state['QuestContainer'], before)
            self.assertEqual(self.acc.guild_runtime['weeklyPersonalWeek'], self.client.week)

    def test_public_list_is_synced_even_when_personal_list_is_empty(self):
        response = self.response()
        response['PlayerGuildQuestList']['Quests'] = []
        self.service._cache(response, 'GuildHandler.GuildMemberCheckIn')
        self.assertEqual(self.weekly.snapshot()['week'], self.client.week + 1)
        self.assertFalse(self.weekly.snapshot()['personalKnown'])

    def test_enter_stops_before_donation_if_new_member_week_has_no_new_personal_list(self):
        self.client.week += 1
        self.client.own['NowGuildQuestWeek'] = self.client.week
        with self.assertRaisesRegex(GameError, '周任务尚未同步'):
            self.service.enter()
        self.assertNotIn('enteredRevision', self.acc.guild_runtime)

    def test_unsent_weekly_reward_does_not_lock_the_quest(self):
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.now += DAY
        self.client.now = self.now
        with self.acc._request_scope(), self.assertRaises(TaskDeferred):
            self.weekly.claim_personal('GuildDonatedCourage')
        self.assertEqual(self.acc.guild_runtime.get('weeklyUncertain'), [])
        self.assertEqual(self.client.routes('RewardQuest'), [])

    def test_unsent_member_store_purchase_does_not_hold_currency(self):
        client = StoreClient()
        acc = accounts.Account('shop-boundary@example.invalid', client, None)
        acc.guild_runtime['enteredRevision'] = acc.state_revision
        store = GuildStoreService(GuildService(client, acc.guild_runtime))
        self.now += DAY
        client.now = self.now
        with acc._request_scope(), self.assertRaises(TaskDeferred):
            store.buy('GuildB015', 1, automatic=True)
        self.assertEqual(acc.guild_runtime.get('shopUncertain'), {})
        self.assertEqual(client.routes('BuyCommodity'), [])

    def test_unsent_aid_does_not_hold_materials(self):
        client = AidClient()
        client.records = [aid_record('one')]
        acc = accounts.Account('aid-boundary@example.invalid', client, None)
        service = GuildService(client, acc.guild_runtime)
        service.query()
        acc.guild_runtime['enteredRevision'] = acc.state_revision
        aid = GuildAidService(service)
        aid.note_synced()
        options = aid_options({'hunt': True})
        candidate = aid.candidates(options)[0]
        self.now += DAY
        client.now = self.now
        with acc._request_scope(), patch.object(aid, 'candidates', return_value=[candidate]), self.assertRaises(TaskDeferred):
            aid.support(candidate, options)
        self.assertNotIn('aidUncertain', acc.guild_runtime)
        self.assertEqual(client.routes('SupportGuildAid'), [])


if __name__ == '__main__':
    unittest.main()
