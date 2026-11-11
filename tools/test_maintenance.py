"""Offline maintenance lifecycle checks; no live accounts, requests or scheduler threads."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from backend import accounts, appcore, config, maintenance, tasks, updater
from backend.game_client import GameClient, GameError, MaintenanceError
from backend.guild import GUILD_ENTRY_ROUTES
from backend.resource_update import service as resource_service
from backend.updater import service as update_service
from tools.test_daily_reset import DailyClient, stamp


class Client(DailyClient):
    is_connected = GameClient.is_connected

    def bootstrap(self):
        maintenance.service.require_open()
        state = super().bootstrap()
        self._mark_connected()
        return state


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.service = maintenance.Maintenance()
        self.now = stamp('2026-10-28 12:00:00')
        self.enterContext(patch.object(maintenance, 'service', self.service))
        self.enterContext(patch.object(accounts, 'maintenance_service', self.service))
        self.enterContext(patch.object(config, 'load_accounts', return_value={}))
        self.enterContext(patch.object(config, 'save_account'))
        self.enterContext(patch.object(config, 'get_auto_relogin_config', return_value={
            'enabled': False, 'delaySeconds': 0}))
        self.enterContext(patch.object(update_service, 'is_installing', return_value=False))
        self.enterContext(patch('time.time', return_value=self.now / 1000))
        self.enterContext(patch('httpx.Client.request', side_effect=AssertionError('No live requests')))
        self.start = self.enterContext(patch.object(accounts.Account, 'start_scheduler'))
        self.client, self.acc = self.make_account('first@example.invalid')
        self.enterContext(patch.dict(tasks.TASKS, {'maintenance_probe': {
            'kind': 'toggle', 'name': 'Probe', 'fn': self.probe}}))
        self.acc.toggles['maintenance_probe'] = True

    def make_account(self, email):
        client = Client()
        client.now = self.now
        client.account_state['Utc'] = str(self.now // 1000)
        acc = accounts.Account(email, client, None)
        client.account = acc
        return client, acc

    @staticmethod
    def probe(client):
        client.call('Test.Automatic', client._auth_data())
        return {'ok': True, 'detail': 'done'}

    def close_server(self):
        self.service.observe({'Info': {'IsOpen': False}})

    def open_server(self):
        self.service.observe({'Info': {'IsOpen': True}})

    def test_pause_all_accounts_without_waiting_for_running_task_lock(self):
        second, acc2 = self.make_account('second@example.invalid')
        events = [threading.Event(), threading.Event()]
        for event in events:
            self.service.track_event(event)
        self.acc.lock.acquire()
        try:
            self.close_server()
            self.assertFalse(self.client.is_connected())
            self.assertFalse(second.is_connected())
            self.assertTrue(all(event.is_set() for event in events))
            self.assertTrue(self.acc._wake.is_set())
            self.assertTrue(acc2._maintenance_pending)
        finally:
            self.acc.lock.release()
        self.assertEqual(self.acc.automation_wait_reason(), '等待维护结束')
        self.assertTrue(self.acc.toggles['maintenance_probe'])
        self.assertFalse(self.acc._stop.is_set())
        with self.assertRaises(MaintenanceError):
            self.acc._before_game_request('Test.Manual')
        self.assertEqual(self.acc.run_toggles(['maintenance_probe'], 'test'), [])
        self.acc._state_refresh_at = 0
        with patch.object(self.client, 'bootstrap') as login:
            self.assertFalse(self.acc._refresh_state_if_due())
            self.assertFalse(self.acc._auto_relogin_if_due())
            self.assertFalse(self.acc._maintenance_relogin_if_due())
        login.assert_not_called()

    def test_explicit_reopen_logs_in_once_even_when_auto_relogin_disabled(self):
        self.close_server()
        self.open_server()
        self.open_server()
        with patch.object(self.client, 'bootstrap', wraps=self.client.bootstrap) as login:
            self.assertTrue(self.acc._maintenance_relogin_if_due())
            self.assertFalse(self.acc._maintenance_relogin_if_due())
        self.assertEqual(login.call_count, 1)
        self.assertTrue(self.client.is_connected())
        self.assertIsNone(self.acc.automation_wait_reason())
        self.assertEqual(self.acc.automation_wait_reason(guild_task=True), '等待佣兵团同步')
        self.assertTrue(self.acc.run_toggles(['maintenance_probe'], 'test')[0]['ok'])
        self.acc._run_guild_if_due()
        self.assertIsNone(self.acc.automation_wait_reason())
        self.assertTrue(self.acc.run_toggles(['maintenance_probe'], 'test')[0]['ok'])
        routes = [route for route, _ in self.client.calls]
        self.assertEqual(routes, ['AccountHandler.Login', 'Test.Automatic'] + [route for _, route, _ in GUILD_ENTRY_ROUTES]
                         + ['GuildHandler.GuildMemberCheckIn', 'Test.Automatic'])

    def test_missing_malformed_or_repeated_fields_do_not_release_maintenance(self):
        self.close_server()
        for body in ({}, {'Info': {}}, {'IsOpen': None}, {'IsOpen': 'true'},
                     {'IsOpen': 2}, {'IsOpen': []}, {'Info': {'IsOpen': False}}):
            self.service.observe(body)
            self.assertTrue(self.service.active)
        self.assertEqual(self.service.epoch, 1)
        self.assertEqual(len(self.acc.logs_since(0)), 1)
        self.assertFalse(self.acc._maintenance_relogin_if_due())
        self.service.observe({'IsOpen': 1})
        self.assertFalse(self.service.active)
        self.assertTrue(self.acc._maintenance_relogin_if_due())

    def test_relogin_failure_stays_paused_without_repeated_attempts(self):
        self.close_server()
        self.open_server()
        with patch.object(self.client, 'bootstrap', side_effect=GameError('offline')) as login:
            self.assertFalse(self.acc._maintenance_relogin_if_due())
            self.open_server()
            self.assertFalse(self.acc._maintenance_relogin_if_due())
            self.assertFalse(self.acc._auto_relogin_if_due())
        self.assertEqual(login.call_count, 1)
        self.assertEqual(self.acc.automation_wait_reason(), '等待维护后重登')
        self.assertFalse(self.client.is_connected())
        self.client.bootstrap()
        self.acc.note_state_refreshed()
        self.assertFalse(self.acc._maintenance_pending)

    def test_new_cycle_and_maintenance_reappearing_during_login(self):
        self.close_server()
        self.open_server()
        def reclosed():
            self.close_server()
            raise MaintenanceError('maintenance resumed')
        with patch.object(self.client, 'bootstrap', side_effect=reclosed):
            self.assertFalse(self.acc._maintenance_relogin_if_due())
        self.assertEqual(self.service.epoch, 2)
        self.assertTrue(self.acc._maintenance_pending)
        self.open_server()
        self.assertTrue(self.acc._maintenance_relogin_if_due())
        self.assertFalse(self.acc._maintenance_relogin_if_due())

    def test_logout_and_manual_login_do_not_trigger_extra_automatic_login(self):
        self.close_server()
        self.acc.stop_scheduler()
        self.open_server()
        with patch.object(self.client, 'bootstrap') as login:
            self.assertFalse(self.acc._maintenance_relogin_if_due())
        login.assert_not_called()
        _, acc = self.make_account('manual@example.invalid')
        self.close_server()
        self.open_server()
        acc.client.bootstrap()
        acc.note_state_refreshed()
        with patch.object(acc.client, 'bootstrap') as login:
            self.assertFalse(acc._maintenance_relogin_if_due())
        login.assert_not_called()

    def test_accounts_and_tasks_registered_during_maintenance_are_blocked(self):
        self.close_server()
        client, acc = self.make_account('late@example.invalid')
        event = threading.Event()
        self.service.track_event(event)
        self.assertTrue(event.is_set())
        self.assertFalse(client.is_connected())
        self.assertTrue(acc._maintenance_pending)

    def test_older_open_bulletin_cannot_release_a_newer_maintenance_window(self):
        self.service.observe({'Info': {'IsOpen': False}, 'ServerTime': {'$date': self.now}})
        self.service.observe({'Info': {'IsOpen': True}, 'ServerTime': {'$date': self.now - 1000}})
        self.assertTrue(self.service.active)
        self.assertFalse(self.acc._maintenance_relogin_if_due())
        self.service.observe({'Info': {'IsOpen': True}, 'ServerTime': {'$date': self.now + 1000}})
        self.assertFalse(self.service.active)

    def test_response_supersedes_in_flight_bulletins_even_without_server_time(self):
        token = self.service.observation_token()
        self.service.observe_response('Server Maintain_Server Maintain')
        self.service.observe({'Info': {'IsOpen': True}}, token=token)
        self.assertTrue(self.service.active)
        token = self.service.observation_token()
        self.service.observe_response('Server Maintain')
        self.service.observe({'Info': {'IsOpen': True}}, token=token)
        self.assertTrue(self.service.active)
        self.service.observe({'Info': {'IsOpen': True}}, token=self.service.observation_token())
        self.assertFalse(self.service.active)
        self.assertEqual(self.service.epoch, 1)

    def test_maintenance_marker_accepts_only_explicit_messages(self):
        for value in ('Server Maintain', 'Server Maintain_Server Maintain',
                      ' server  maintain ', '"Server Maintain"',
                      {'Message': 'Server Maintain'}, {'error': {'message': 'Server Maintain'}}):
            self.assertTrue(maintenance.is_maintenance_message(value))
        for value in ({'Info': {'Dialog': 'Server Maintain'}}, 'Server Maintain maybe',
                      'some Server Maintain words', {'IsOpen': True}, None, [], {}):
            self.assertFalse(maintenance.is_maintenance_message(value))

    def test_game_responses_stop_all_accounts_and_recover_once(self):
        for value in (b'Server Maintain_Server Maintain', b'"Server Maintain"',
                      json.dumps({'Message': 'Server Maintain'}).encode()):
            with self.subTest(response=value):
                c = GameClient.__new__(GameClient)
                c.account_state = {}
                c._sync_proxy = Mock()
                c._request_http = Mock(return_value=httpx.Response(200, content=value,
                    request=httpx.Request('PUT', 'https://offline.invalid')))
                event = threading.Event()
                self.service.track_event(event)
                with self.assertRaises(MaintenanceError):
                    c.call('GuildHandler.QueryGuildDataForSync', {})
                self.assertTrue(event.is_set())
                self.assertFalse(self.client.is_connected())
                self.assertEqual(c.connection_status()['reason'], 'maintenance')
                with self.assertRaises(MaintenanceError):
                    c.call('Test.NextTask', {})
                self.assertEqual(c._request_http.call_count, 1)
                self.open_server()
                with patch.object(self.client, 'bootstrap', wraps=self.client.bootstrap) as login:
                    self.assertTrue(self.acc._maintenance_relogin_if_due())
                    self.assertFalse(self.acc._maintenance_relogin_if_due())
                self.assertEqual(login.call_count, 1)

    def test_developer_console_response_also_stops_tasks(self):
        c = GameClient.__new__(GameClient)
        c.account_state, c.aid, c.session_id = {}, 'test-aid', 'test-session'
        c._sync_proxy = Mock()
        c._request_http = Mock(return_value=httpx.Response(200,
            content=b'Server Maintain_Server Maintain', request=httpx.Request('PUT', 'https://offline.invalid')))
        result = appcore._dev_call_locked(c, 'Test.Developer', {}, False)
        self.assertEqual(result['rawText'], 'Server Maintain_Server Maintain')
        self.assertTrue(self.service.active)
        self.assertFalse(self.client.is_connected())
        self.assertFalse(c.is_connected())

    def test_software_scheduler_drives_the_resource_tick_with_its_same_delay(self):
        for ok, expected in ((True, 3900), (False, updater.RETRY_INTERVAL)):
            with self.subTest(software_check_ok=ok):
                service = updater.UpdateService()
                with patch.object(service._wake, 'wait', side_effect=[False, RuntimeError('end loop')]) as wait, \
                        patch.object(service, 'check_now', return_value=ok), \
                        patch.object(updater, 'routine_check_delay', return_value=3900), \
                        patch.object(resource_service, 'on_software_check') as resource_tick:
                    with self.assertRaisesRegex(RuntimeError, 'end loop'):
                        service._run()
                resource_tick.assert_called_once_with(expected)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [0.0, expected])

    def test_resource_notification_failure_does_not_stop_the_software_scheduler(self):
        service = updater.UpdateService()
        with patch.object(service._wake, 'wait', side_effect=[False, RuntimeError('end loop')]) as wait, \
                patch.object(service, 'check_now', return_value=True), \
                patch.object(updater, 'routine_check_delay', return_value=3900), \
                patch.object(resource_service, 'on_software_check', side_effect=RuntimeError('worker unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'end loop'):
                service._run()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [0.0, 3900])

    def test_logged_out_account_is_unregistered_from_maintenance_recovery(self):
        self.close_server()
        self.acc.close()
        self.start.reset_mock()
        self.open_server()
        self.start.assert_not_called()
        self.assertFalse(self.acc._maintenance_relogin_if_due())

    def test_queued_operation_cannot_continue_after_maintenance_and_recovery(self):
        with self.service.operation_scope():
            self.close_server()
            self.open_server()
            # The independent scheduler can recover while an older API call
            # is still queued. Its operation context is deliberately separate.
            thread = threading.Thread(target=self.acc._maintenance_relogin_if_due)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(self.client.is_connected())
            with self.assertRaises(MaintenanceError):
                self.acc._before_game_request('Test.OldManualCall')
        with self.service.operation_scope():
            self.service.require_open()

    def test_account_api_reports_maintenance_and_allows_cancel_and_status(self):
        self.close_server()
        with patch.object(accounts, 'get', return_value=self.acc):
            status = appcore.connection_status(self.acc.email)
            self.assertEqual(status['reason'], 'maintenance')
            self.assertTrue(status['maintenance'])
            with self.assertRaises(appcore.ApiError) as raised:
                appcore._require(self.acc.email)
            self.assertIn('维护', str(raised.exception))
            self.assertEqual(appcore._require(self.acc.email, allow_disconnected=True), self.acc)
            self.assertTrue(appcore.abyss_cancel(self.acc.email)['ok'])

    def test_transport_blocks_preauthenticated_and_login_requests_but_not_bulletins(self):
        c = GameClient.__new__(GameClient)
        c.account_state = {}
        c._sync_proxy = Mock()
        c._client = Mock()
        c._client.request.return_value = httpx.Response(200, json={'Info': {'IsOpen': False}},
                                                       request=httpx.Request('PUT', 'https://offline.invalid'))
        self.close_server()
        with patch.object(resource_service, 'observe', side_effect=self.service.observe):
            c.call('GameServerDBSettingHandler.QueryBulletinInfoResult', {})
        self.assertEqual(c._client.request.call_count, 1)
        for route in ('Test.Manual', 'AccountHandler.Login'):
            with self.assertRaises(MaintenanceError):
                c.call(route, {'AID': 'test', 'SessionID': 'test'})
            with self.assertRaises(MaintenanceError):
                c._request_http('PUT', 'https://offline.invalid', game_route=route)
        self.assertEqual(c._client.request.call_count, 1)

    def test_login_bulletin_stops_before_account_login(self):
        c = GameClient.__new__(GameClient)
        c._sync_login_auth = Mock()
        c.game_token, c._login_common = 'offline-token', {}
        def call(route, data):
            self.assertEqual(route, 'GameServerDBSettingHandler.QueryBulletinInfoResult')
            self.close_server()
            return {}
        c.call = Mock(side_effect=call)
        with self.assertRaises(MaintenanceError):
            c.bootstrap()
        self.assertEqual(c.call.call_count, 1)

    def test_scheduler_recovery_keeps_login_guild_and_ordinary_task_order(self):
        self.close_server()
        self.open_server()
        def due(prefix, *, login_only):
            if login_only:
                with self.acc._request_scope(login_task=True):
                    self.client.call('Test.Signin', self.client._auth_data())
            else:
                self.acc.run_toggles(['maintenance_probe'], prefix)
                self.acc._stop.set()
        with patch.object(self.acc, 'run_due', side_effect=due), patch.object(
                self.acc, '_run_startup_tasks'), patch.object(accounts.redeem_codes, 'refresh_if_due'), patch.object(
                accounts.redeem_codes, 'revision', return_value=0):
            self.acc._run_loop()
        routes = [route for route, _ in self.client.calls]
        self.assertEqual(routes, ['AccountHandler.Login', 'Test.Signin']
                         + [route for _, route, _ in GUILD_ENTRY_ROUTES]
                         + ['GuildHandler.GuildMemberCheckIn', 'Test.Automatic'])

    def test_active_websocket_closes_before_sending_more_commands(self):
        from backend import abyss_battle
        from tools.test_activity_progress import client_fixture
        c = client_fixture()
        c.call = Mock(return_value={'_id': 'room', 'ServerID': 1, 'AID': 'aid', 'Domain': 'wss://offline.invalid'})
        info = {'SceneData': {'StaticID': 'BH610_1_13'}, 'CampData1': {'PositionRoleMap': {
            '0': c.account_state['RoleDataContainer']['Roles'][0]}}}
        sent, closed = [], []
        service = self.service
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): closed.append(True)
            async def send(self, raw): sent.append(json.loads(raw))
            async def recv(self):
                service.observe({'IsOpen': False})
                service.observe({'IsOpen': True})
                return json.dumps({'NetID': 1})
        with patch('websockets.legacy.client.connect', return_value=Socket()):
            with self.assertRaises(GameError):
                abyss_battle.run_activity_battle(c, info)
        self.assertEqual(sent, [{'ServerID': 1}])
        self.assertEqual(closed, [True])
        self.assertEqual(c._activity_pending[1], 'BH610_1_13')


if __name__ == '__main__':
    unittest.main()
