"""Offline summon settlement and cancellation tests; never contact the game."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from backend import config, gacha, runtime_data
from backend.game_client import GameClient, GameMessage

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tools/fixtures/gacha.json').read_text(encoding='utf-8'))
NOW = FIXTURE['now']
POOL = 'AcyivitySummonH806'
PARAMS = {'poolId': POOL, 'count': 3, 'stopRole': False, 'stopArtifact': False}


class ReplayClient(GameClient):
    def __init__(self, sample='ordinary', *, currency='5', balance=50):
        self.aid, self.session_id = 'test-aid', 'test-session'
        self.sample = deepcopy(FIXTURE['samples'][sample])
        self.account_state = {
            'StoreRecordContainer': {'Records': [deepcopy(self.sample['before'])]},
            'ItemContainer': {'Items': [{'StaticID': currency, 'Count': balance}]},
            'ActivityDBSettingContainer': {'SettingList': [
                {'ActivityID': POOL, 'StartTime': {'$date': (NOW - 3600) * 1000},
                 'EndTime': {'$date': (NOW + 3600) * 1000}, 'Order': 1}]},
        }
        self.calls, self.connected, self.error, self.transform = [], True, None, None
        self.entered, self.release = None, None

    def is_connected(self):
        return self.connected

    def call(self, route, data=None):
        self.calls.append((route, deepcopy(data)))
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError('test response gate timed out')
        if self.error:
            raise self.error
        result = deepcopy(self.sample['response'])
        result['CommodityRecord'] = {**deepcopy(data['Record']),
                                     'BuyCount': int(data['Record']['BuyCount']) + 1}
        currency = data['SelcetCostItemID']
        result['CostItems'] = [{'Item': {'StaticID': currency, 'Count': 5},
                                'NowItem': {'StaticID': currency,
                                            'Count': gacha.balance(self.account_state, currency) - 5}}]
        if self.transform:
            result = self.transform(result, data)
        return result


class GachaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(folder.name)))
        self.enterContext(patch.object(gacha.time, 'time', return_value=NOW))
        self.enterContext(patch.object(config, 'get_request_delay_config', return_value={'enabled': False}))
        self.accounts, self.logs = {}, []
        self.manager = gacha.JobManager(self.accounts.get)
        self.acc = self.account('first')

    def account(self, email, client=None):
        acc = SimpleNamespace(email=email, client=client or ReplayClient(), lock=threading.Lock(),
                              state_revision=1, _stop=threading.Event(),
                              push_log=lambda result, prefix='': self.logs.append((email, result)))
        self.accounts[email] = acc
        return acc

    def start(self, acc=None, **overrides):
        acc = acc or self.acc
        self.manager.start(acc, {**PARAMS, **overrides}, str(uuid.uuid4()))
        return self.wait(acc)

    def wait(self, acc=None):
        acc = acc or self.acc
        deadline = time.monotonic() + 4
        while self.manager.status(acc)['running']:
            if time.monotonic() > deadline:
                self.fail('summon job failed to finish')
            threading.Event().wait(.005)
        return self.manager.status(acc)

    def test_capture_includes_virtual_up_and_exact_cost(self):
        pool = gacha.catalog()['pools'][POOL]
        for key, sample in FIXTURE['samples'].items():
            result, cost = gacha.settle(sample['response'], pool, '5', sample['before'], False)
            self.assertEqual(cost, 5)
            self.assertEqual(result['id'], sample['expected'])
        self.assertTrue(gacha.results(FIXTURE['samples']['up']['response'])[0]['virtual'])

    def test_virtual_and_real_copy_of_same_drop_are_deduplicated(self):
        value = deepcopy(FIXTURE['samples']['up']['response'])
        value['Drop']['Items'].extend(value['VirtualDrop']['Items'])
        self.assertEqual(len(gacha.results(value)), 1)

    def test_stops_at_requested_count_and_uses_latest_record(self):
        result = self.start()
        self.assertEqual(result['completed'], 3)
        self.assertEqual(len(self.acc.client.calls), 3)
        self.assertEqual(result['spent'], 15)
        self.assertEqual(gacha.balance(self.acc.client.account_state, '5'), 35)
        self.assertEqual([d['Count'] for _, d in self.acc.client.calls], [1, 1, 1])
        counts = [d['Record']['BuyCount'] for _, d in self.acc.client.calls]
        self.assertEqual(counts, list(range(counts[0], counts[0] + 3)))

    def test_virtual_up_stops_before_next_draw(self):
        self.acc.client = ReplayClient('up')
        result = self.start(stopRole=True, count=10)
        self.assertEqual(result['completed'], 1)
        self.assertIn('UP 角色', result['message'])
        self.assertEqual(len(self.acc.client.calls), 1)

    def test_either_enabled_target_stops_on_up_artifact(self):
        self.acc.client = ReplayClient('artifact')
        def artifact(response, data):
            response['Drop']['Items'][0]['Artifact']['StaticID'] = 'A0464'
            return response
        self.acc.client.transform = artifact
        result = self.start(stopRole=True, stopArtifact=True)
        self.assertEqual(result['completed'], 1)
        self.assertIn('UP 羁绊', result['message'])

    def test_ordinary_role_does_not_trigger_up_stop(self):
        self.assertEqual(self.start(stopRole=True)['completed'], 3)

    def test_specific_contracts_are_used_before_switching_to_ordinary(self):
        self.acc.client = ReplayClient(currency='TH806', balance=10)
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': '5', 'Count': 500})
        result = self.start(preferExclusive=True)
        self.assertEqual(result['completed'], 3)
        self.assertEqual([data['SelcetCostItemID'] for _, data in self.acc.client.calls], ['TH806', 'TH806', '5'])
        self.assertEqual(result['balances'], {'TH806': 0, '5': 495})
        self.assertEqual([entry['currency'] for entry in result['results']], ['TH806', 'TH806', '5'])
        self.assertIn('蜜拉贝儿招募契约', self.logs[0][1]['detail'])

    def test_disabled_specific_priority_uses_only_ordinary_contracts(self):
        self.acc.client = ReplayClient(currency='TH806', balance=100)
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': '5', 'Count': 50})
        result = self.start(preferExclusive=False)
        self.assertEqual(result['completed'], 3)
        self.assertFalse(result['params']['preferExclusive'])
        self.assertEqual([data['SelcetCostItemID'] for _, data in self.acc.client.calls], ['5', '5', '5'])
        self.assertEqual(result['balances'], {'TH806': 100, '5': 35})

    def test_disabled_specific_priority_stops_when_ordinary_runs_out(self):
        self.acc.client = ReplayClient(currency='TH806', balance=100)
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': '5', 'Count': 10})
        result = self.start(preferExclusive=False)
        self.assertEqual(result['completed'], 2)
        self.assertEqual([data['SelcetCostItemID'] for _, data in self.acc.client.calls], ['5', '5'])
        self.assertEqual(result['balances'], {'TH806': 100, '5': 0})
        self.assertIn('契约不足', result['message'])

    def test_disabled_specific_priority_without_ordinary_sends_no_request(self):
        self.acc.client = ReplayClient(currency='TH806', balance=100)
        result = self.start(preferExclusive=False)
        self.assertEqual(result['completed'], 0)
        self.assertEqual(self.acc.client.calls, [])
        self.assertEqual(result['balances']['TH806'], 100)

    def test_unusable_specific_remainder_is_preserved_until_all_contracts_run_out(self):
        self.acc.client = ReplayClient(currency='TH806', balance=4)
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': '5', 'Count': 10})
        result = self.start()
        self.assertEqual(result['completed'], 2)
        self.assertEqual([data['SelcetCostItemID'] for _, data in self.acc.client.calls], ['5', '5'])
        self.assertEqual(result['balances'], {'TH806': 4, '5': 0})
        self.assertIn('契约不足', result['message'])

    def test_specific_priority_is_independent_of_catalog_order_and_legacy_selection(self):
        definitions = deepcopy(gacha.catalog())
        definitions['pools'][POOL]['prices'].reverse()
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': 'TH806', 'Count': 5})
        with patch.object(gacha, 'catalog', return_value=definitions):
            result = self.start(count=1, currency='5')
        self.assertEqual(result['completed'], 1)
        self.assertNotIn('currency', result['params'])
        self.assertTrue(result['params']['preferExclusive'])
        self.assertEqual(self.acc.client.calls[0][1]['SelcetCostItemID'], 'TH806')
        self.assertEqual(result['balances'], {'5': 50, 'TH806': 0})

    def test_unconfirmed_specific_draw_never_falls_back_to_ordinary(self):
        self.acc.client = ReplayClient(currency='TH806', balance=5)
        self.acc.client.account_state['ItemContainer']['Items'].append({'StaticID': '5', 'Count': 500})
        self.acc.client.error = TimeoutError('reply lost')
        result = self.start()
        self.assertTrue(result['blocked'])
        self.assertEqual([data['SelcetCostItemID'] for _, data in self.acc.client.calls], ['TH806'])
        self.assertEqual(gacha.balance(self.acc.client.account_state, '5'), 500)

    def test_displayed_balances_follow_later_account_updates(self):
        self.start(count=1)
        self.acc.client.account_state['ItemContainer']['Items'][0]['Count'] = 70
        self.assertEqual(self.manager.status(self.acc)['balances']['5'], 70)

    def test_displayed_pity_follows_login_snapshot_after_job_finishes(self):
        self.start(count=1)
        self.acc.state_revision += 1
        self.acc.client.account_state['StoreRecordContainer'] = {'Records': [
            {'StaticID': POOL, 'Store': 'Summon', 'GuaranteedCount': 57}]}
        self.assertEqual(self.manager.status(self.acc)['guaranteedCount'], 57)
        self.acc.client.account_state['StoreRecordContainer']['Records'] = []
        self.assertEqual(self.manager.status(self.acc)['guaranteedCount'], 0)

    def test_insufficient_balance_sends_no_request(self):
        self.acc.client = ReplayClient(balance=4)
        self.assertEqual(self.start()['completed'], 0)
        self.assertEqual(self.acc.client.calls, [])

    def test_timeout_blocks_new_jobs_until_login_refresh(self):
        self.acc.client.error = TimeoutError('reply lost')
        result = self.start()
        self.assertEqual(result['state'], 'uncertain')
        with self.assertRaisesRegex(Exception, '等待登录同步刷新'):
            self.start()
        self.assertEqual(len(self.acc.client.calls), 1)
        self.acc.state_revision += 1
        self.assertFalse(self.manager.status(self.acc)['blocked'])
        self.assertIn('同步已完成', self.manager.status(self.acc)['message'])
        self.acc.client.error = None
        self.assertEqual(self.start(count=1)['completed'], 1)

    def test_incomplete_settlement_never_retries(self):
        self.acc.client.transform = lambda response, data: {'Drop': response['Drop']}
        self.assertTrue(self.start()['blocked'])
        self.assertEqual(len(self.acc.client.calls), 1)

    def test_unexpected_currency_stops_and_blocks(self):
        def wrong_cost(response, data):
            response['CostItems'][0]['Item']['StaticID'] = '2'
            return response
        self.acc.client.transform = wrong_cost
        self.assertTrue(self.start()['blocked'])
        self.assertEqual(len(self.acc.client.calls), 1)

    def test_explicit_server_failure_stops_without_retry(self):
        self.acc.client.error = GameMessage('Artifact backpack is full')
        result = self.start()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('羁绊背包已满', result['message'])
        self.assertEqual(len(self.acc.client.calls), 1)

    def test_cancel_in_flight_confirms_current_draw_then_stops(self):
        self.acc.client.entered, self.acc.client.release = threading.Event(), threading.Event()
        self.manager.start(self.acc, PARAMS, str(uuid.uuid4()))
        self.assertTrue(self.acc.client.entered.wait(2))
        self.assertEqual(self.manager.cancel(self.acc)['state'], 'stopping')
        self.acc.client.release.set()
        result = self.wait()
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(self.acc.client.calls), 1)
        self.assertFalse(result['blocked'])

    def test_cancel_while_waiting_for_account_sends_no_request(self):
        with self.acc.lock:
            self.manager.start(self.acc, PARAMS, str(uuid.uuid4()))
            self.manager.cancel(self.acc)
        self.assertEqual(self.wait()['completed'], 0)
        self.assertEqual(self.acc.client.calls, [])

    def test_repeated_start_request_is_idempotent(self):
        request = str(uuid.uuid4())
        self.manager.start(self.acc, {**PARAMS, 'count': 1}, request)
        self.wait()
        self.manager.start(self.acc, {**PARAMS, 'count': 1}, request)
        self.assertEqual(len(self.acc.client.calls), 1)

    def test_second_account_runs_independently(self):
        second = self.account('second')
        with self.acc.lock:
            self.manager.start(self.acc, PARAMS, str(uuid.uuid4()))
            self.assertEqual(self.start(second)['completed'], 3)
            self.manager.cancel(self.acc)
        self.assertEqual(self.wait()['completed'], 0)

    def test_new_login_stops_waiting_job_without_spending(self):
        with self.acc.lock:
            self.manager.start(self.acc, PARAMS, str(uuid.uuid4()))
            self.acc.state_revision += 1
        self.assertEqual(self.wait()['completed'], 0)
        self.assertEqual(self.acc.client.calls, [])

    def test_expired_and_special_pools_are_not_available(self):
        ids = {pool['id'] for pool in gacha.options(self.acc.client.account_state)}
        self.assertIn(POOL, ids)
        self.assertNotIn('SummonNewbie', ids)
        self.assertNotIn('MysticSummonH186', ids)
        self.acc.client.account_state['ActivityDBSettingContainer']['SettingList'][0]['EndTime']['$date'] = (NOW - 1) * 1000
        self.assertNotIn(POOL, {pool['id'] for pool in gacha.options(self.acc.client.account_state)})
        with self.assertRaisesRegex(Exception, '已经结束'):
            self.start()

    def test_invalid_draw_counts_and_targets_send_no_requests(self):
        for count in [0, -1, True, 1.5, '1e3', 10000]:
            with self.assertRaises(Exception):
                self.start(count=count)
        with self.assertRaises(Exception):
            self.start(poolId='NormalSummon', stopRole=True)
        self.assertEqual(self.acc.client.calls, [])

    def test_stale_counter_and_missing_balance_are_unconfirmed(self):
        pool = gacha.catalog()['pools'][POOL]
        sample = FIXTURE['samples']['ordinary']
        response = deepcopy(sample['response'])
        response['CommodityRecord']['BuyCount'] = sample['before']['BuyCount']
        with self.assertRaises(gacha.Unconfirmed):
            gacha.settle(response, pool, '5', sample['before'], False)
        response = deepcopy(sample['response'])
        response['CostItems'][0].pop('NowItem')
        with self.assertRaises(gacha.Unconfirmed):
            gacha.settle(response, pool, '5', sample['before'], False)

    def test_normal_free_draw_is_followed_by_paid_draws_under_same_lock(self):
        client = self.acc.client
        saved = client.account_state['StoreRecordContainer']['Records'][0]
        saved.update(StaticID='NormalSummon', FreeBuyCount=0)
        def normal(response, data):
            if data['Record']['FreeBuyCount'] == 0:
                response['CommodityRecord']['BuyCount'] = data['Record']['BuyCount']
                response['CommodityRecord']['FreeBuyCount'] = 1
                response['CostItems'] = []
            return response
        client.transform = normal
        result = self.start(poolId='NormalSummon', preferExclusive=False)
        self.assertEqual(result['completed'], 3)
        self.assertEqual(result['spent'], 10)
        self.assertEqual(gacha.balance(client.account_state, '5'), 40)
        self.assertEqual([data['SelcetCostItemID'] for _, data in client.calls], ['', '5', '5'])

    def test_maintenance_cancels_and_never_resumes_job(self):
        from backend import maintenance
        service = maintenance.Maintenance()
        with patch.object(maintenance, 'service', service), self.acc.lock:
            self.manager.start(self.acc, PARAMS, str(uuid.uuid4()))
            service.observe_response('Server Maintain')
        self.assertEqual(self.wait()['completed'], 0)
        self.assertEqual(self.acc.client.calls, [])

    def test_api_routes_use_same_job_and_cancel_without_account_lock(self):
        from backend import appcore, server
        self.acc.params, self.acc.toggles = {}, {}
        with patch.object(appcore, '_require', return_value=self.acc), \
                patch.object(appcore, 'gacha_jobs', self.manager), patch.object(config, 'save_account') as save_account:
            options = server._route_api('GET', '/api/gacha/options', {}, {'account': 'first'})
            self.assertIn(POOL, {pool['id'] for pool in options['pools']})
            with self.acc.lock:
                result = server._route_api('POST', '/api/gacha/start',
                                          {'account': 'first', 'params': {**PARAMS, 'preferExclusive': False},
                                           'requestId': str(uuid.uuid4())}, {})
                self.assertTrue(result['job']['running'])
                self.assertFalse(self.acc.params['gacha']['preferExclusive'])
                self.assertFalse(save_account.call_args.kwargs['params']['gacha']['preferExclusive'])
                canceled = server._route_api('POST', '/api/gacha/cancel', {'account': 'first'}, {})
                self.assertEqual(canceled['job']['state'], 'stopping')
            self.assertEqual(self.wait()['completed'], 0)

    def test_saving_draft_preserves_auto_tasks_without_starting_any_draw(self):
        from backend import appcore, server
        self.acc.params = {'arena_npc': {'teamId': '1'}, 'gacha': {'poolId': 'ExpiredPool'}}
        self.acc.toggles = {'arena_npc': True, 'daily_stamina': True}
        self.acc.notify_config_changed = lambda: None
        selected = {**PARAMS, 'preferExclusive': False}
        with patch.object(appcore, '_require', return_value=self.acc), \
                patch.object(appcore, 'gacha_jobs', self.manager), patch.object(config, 'save_account') as save_account:
            server._route_api('POST', '/api/toggles',
                              {'account': 'first', 'toggles': None, 'params': {'gacha': selected}}, {})
            restored = server._route_api('GET', '/api/gacha/options', {}, {'account': 'first'})
            self.assertEqual(selected, restored['config'])
            self.assertEqual({'teamId': '1'}, self.acc.params['arena_npc'])
            self.assertEqual({'arena_npc': True, 'daily_stamina': True}, self.acc.toggles)
            self.assertEqual(selected, save_account.call_args.kwargs['params']['gacha'])
            self.assertFalse(restored['job']['running'])
            self.assertEqual([], self.acc.client.calls)

    def test_new_official_pool_compiles_roles_artifacts_and_contract_prices(self):
        value = gacha.build_catalog([
            {'ID': 'NewPool', 'Name': 'new-name', 'Store': 'Summon', 'ActivityID': 'NewPool',
             'Drops': 'NewDrop', 'GuaranteedRoleIDs': 'H999', 'CostType': 'Select',
             'PriceItemID': 'TH999', 'Price': '5', 'PriceItemID2': '5', 'Price2': '5'}],
            [{'ID': 'NewPool', 'Type': 'SummonPickUp', 'SummonArtifactID': 'A0999'}],
            [{'DropID': 'NewDrop', 'TagString': 'A0999'}],
            {'H999': '新团员', 'A0999': '新羁绊'}, {'A0999': 4}, {'new-name': '新卡池'}, 'catalog_999')
        pool = value['pools']['NewPool']
        self.assertEqual(pool['roles'][0]['id'], 'H999')
        self.assertEqual(pool['artifacts'][0]['name'], '新羁绊')
        self.assertEqual([price['item'] for price in pool['prices']], ['TH999', '5'])
        self.assertEqual(value['catalog'], 'catalog_999')


if __name__ == '__main__':
    unittest.main()
