"""Offline event-shop settlement and scheduler checks; no game connections."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend import accounts, activity_store as shop, config, runtime_data, tasks
from backend.game_client import GameClient, GameMessage, TaskDeferred

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_793_714_000_000
CURRENT, OLD = 'ActivityH806', 'ActivityH610'
ITEM = CURRENT + '_2'


def catalog():
    stores = [dict(ID=sid, Type='SideStory', ActivityID=aid)
              for sid, aid in [(CURRENT, 'BranchH806'), (OLD, 'BranchH610')]]
    activities = [dict(ID=aid, Name=name, RemainDay='7') for aid, name in
                  [('BranchH806', '甜蜜顾问的优雅清算'), ('BranchH610', '远离喧嚣的插曲')]]
    commodities = [dict(ID=sid + suffix, Store=sid, ActivityID=aid, LimitCount=limit,
                        PriceItemID=currency, Price=price, ItemID=item, ItemCount=amount)
                   for sid, aid, currency in [(CURRENT, 'BranchH806', 'AH806'), (OLD, 'BranchH610', 'AH610')]
                   for suffix, limit, price, item, amount in
                   [('_2', '5', '90', 'TH806', '5'), ('_1', '1', '1200', '211', '1'),
                    ('_28', '-1', '15', '1', '2500')]]
    return shop.build_catalog(stores, commodities, activities, {'TH806': '专属契约', '211': '技能道具'}, {})


def state():
    return {'Utc': NOW, 'ItemContainer': {'Items': [
        {'StaticID': 'AH806', 'Count': 478}, {'StaticID': 'AH610', 'Count': 180}]},
        'StoreRecordContainer': {'Records': []},
        'ActivityDBSettingContainer': {'SettingList': [
            {'ActivityID': 'BranchH806', 'StartTime': {'$date': NOW - shop.DAY_MS},
             'EndTime': {'$date': NOW + shop.DAY_MS}},
            {'ActivityID': 'BranchH610', 'StartTime': {'$date': NOW - 20 * shop.DAY_MS},
             'EndTime': {'$date': NOW - 6 * shop.DAY_MS}}]}}


class ReplayClient(GameClient):
    def __init__(self):
        self.aid, self.session_id = 'test-aid', 'test-session'
        self.account_state = state()
        self.calls, self.transform, self.error = [], None, None

    def call(self, route, data=None):
        self.calls.append((route, deepcopy(data)))
        if self.error:
            raise self.error
        saved, count = data['Record'], data['Count']
        definition = next(row for row in catalog()['stores'][saved['Store']]['goods']
                          if row['id'] == saved['StaticID'])
        balance = next(item['Count'] for item in self.account_state['ItemContainer']['Items']
                       if item['StaticID'] == definition['currency'])
        # The captured H806 exchange used Count=5, spent 450, retained 28,
        # received TH806 x25, and returned BuyCount=5.
        record = {**saved, '_id': {'$oid': saved['StaticID']}, 'BuyCount': saved['BuyCount'] + count}
        result = {'CommodityRecord': record,
                  'CostItems': [{'Item': {'StaticID': definition['currency'], 'Count': count * definition['price']},
                                 'NowItem': {'StaticID': definition['currency'],
                                             'Count': balance - count * definition['price']}}],
                  'Drop': {'Items': [{'Item': {'StaticID': definition['item'],
                                              'Count': count * definition['amount']}}]}}
        return self.transform(result) if self.transform else result


class ActivityStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(self.folder.name)))
        self.enterContext(patch.object(shop, 'catalog', side_effect=catalog))
        self.enterContext(patch.object(shop.game_clock, 'now_ms', return_value=NOW))
        self.enterContext(patch.object(config, 'get_request_delay_config', return_value={'enabled': False}))
        self.client, self.runtime = ReplayClient(), {}

    def status(self, items=None, now=NOW):
        return shop.status(self.client.account_state, {'items': items or [ITEM]}, self.runtime, now_ms=now)

    def run_shop(self, items=None, on_partial=None):
        return shop.task(self.client, {'items': items or [ITEM]}, self.runtime, on_partial)

    def test_two_shops_include_previous_exchange_week_and_close_on_time(self):
        self.assertEqual([row['id'] for row in self.status()['stores']], [CURRENT, OLD])
        self.assertEqual([row['id'] for row in self.status(now=NOW + shop.DAY_MS)['stores']], [CURRENT])
        self.assertEqual(self.status(now=NOW + 8 * shop.DAY_MS)['stores'], [])
        self.run_shop([OLD + '_2'])
        self.assertEqual(self.client.calls[0][1]['Count'], 2)

    def test_catalog_and_executor_exclude_unlimited_even_when_selected(self):
        self.assertNotIn(CURRENT + '_28', [row['id'] for row in catalog()['stores'][CURRENT]['goods']])
        result = self.run_shop([CURRENT + '_28'])
        self.assertTrue(result['skipped'])
        self.assertEqual(self.client.calls, [])

    def test_same_shop_in_a_new_event_period_does_not_inherit_old_targets(self):
        params = {'items': [ITEM], 'periods': {CURRENT: NOW - shop.DAY_MS}}
        self.assertTrue(shop.status(self.client.account_state, params, now_ms=NOW)['ready'])
        self.client.account_state['ActivityDBSettingContainer']['SettingList'][0]['StartTime']['$date'] += 1000
        self.assertFalse(shop.status(self.client.account_state, params, now_ms=NOW)['ready'])

    def test_additional_activity_condition_is_respected_without_ending_exchange_week_early(self):
        data = catalog()
        data['activities']['Version_Test'] = {'IsUnlimitTime': ''}
        data['stores'][CURRENT]['goods'][0]['activities'].append('Version_Test')
        with patch.object(shop, 'catalog', return_value=data):
            self.assertFalse(self.status()['ready'])
            self.client.account_state['ActivityDBSettingContainer']['SettingList'].append(
                {'ActivityID': 'Version_Test', 'IsIgnoreTime': True})
            self.assertTrue(self.status()['ready'])

    def test_closing_during_purchase_delay_prevents_next_shop_request(self):
        self.client.account_state['ActivityDBSettingContainer']['SettingList'][1]['EndTime']['$date'] = NOW - 7 * shop.DAY_MS + 1
        with patch.object(shop.game_clock, 'now_ms', side_effect=[NOW, NOW + 2]):
            self.run_shop([ITEM, OLD + '_2'])
        self.assertEqual(len(self.client.calls), 1)

    def test_capture_batch_confirms_balance_and_count_and_never_repeats(self):
        result = self.run_shop()
        self.assertTrue(result['ok'], result)
        self.assertIn('×25', result['detail'])
        self.assertEqual(self.client.calls[0][0], 'StoreHandler.BuyCommodity')
        self.assertEqual(self.client.calls[0][1]['Count'], 5)
        self.assertEqual(self.status()['stores'][0]['wallet']['AH806'], 28)
        self.assertEqual(self.status()['stores'][0]['goods'][0]['remaining'], 0)
        self.assertEqual(self.status()['note'], '所选商品已全部买完')
        self.run_shop()
        self.assertEqual(len(self.client.calls), 1)

    def test_insufficient_balance_waits_without_request_then_inventory_increase_resumes(self):
        self.client.account_state['ItemContainer']['Items'][0]['Count'] = 80
        self.assertEqual(self.status()['note'], '活动道具不足')
        self.run_shop()
        self.run_shop()
        self.assertEqual(self.client.calls, [])
        self.client._apply_now_items({'Drop': {'Items': [{'NowItem': {'StaticID': 'AH806', 'Count': 180}}]}})
        self.run_shop()
        self.assertEqual(self.client.calls[0][1]['Count'], 2)
        self.assertFalse(self.status()['ready'])
        self.assertEqual(self.status()['note'], '活动道具不足')

    def test_timeout_pauses_shared_currency_but_other_shop_continues(self):
        def transform(result):
            if result['CommodityRecord']['Store'] == CURRENT:
                raise TimeoutError('timeout')
            return result
        self.client.transform = transform
        result = self.run_shop([ITEM, CURRENT + '_1', OLD + '_2'])
        self.assertIn('等待登录同步刷新', result['detail'])
        self.assertEqual(len(self.client.calls), 2)
        self.run_shop([ITEM, CURRENT + '_1', OLD + '_2'])
        self.assertEqual(len(self.client.calls), 2)

    def test_missing_cost_holds_currency_and_missing_record_pauses_only_product(self):
        for field in ['CostItems', 'CommodityRecord']:
            with self.subTest(field=field):
                self.client, self.runtime = ReplayClient(), {}
                def transform(result):
                    result.pop(field)
                    return result
                self.client.transform = transform
                self.run_shop()
                self.assertFalse(self.status()['ready'])
                self.assertIn(ITEM, self.runtime['waitingForLogin'])
                self.run_shop()
                self.assertEqual(len(self.client.calls), 1)
                self.assertEqual(self.runtime['waitingForLogin'][ITEM]['hold'], field == 'CostItems')

    def test_record_without_id_does_not_allow_purchase_loop(self):
        def transform(result):
            result['CommodityRecord'].pop('_id')
            return result
        self.client.transform = transform
        self.run_shop()
        self.assertFalse(self.status()['ready'])
        self.assertEqual(len(self.client.calls), 1)

    def test_partial_logs_emit_success_and_failure_once_each(self):
        emitted = []
        def transform(result):
            if result['CommodityRecord']['Store'] == OLD:
                raise TimeoutError('timeout')
            return result
        self.client.transform = transform
        result = self.run_shop([ITEM, OLD + '_2'], emitted.append)
        self.assertEqual([row['ok'] for row in emitted], [True, False])
        self.assertTrue(result['liveLogged'])
        self.assertTrue(result['retryManaged'])

    def test_item_not_enough_waits_for_higher_balance_instead_of_five_minute_retry(self):
        self.client.error = GameMessage('Item not enough')
        self.run_shop()
        self.assertEqual(self.status()['note'], '活动道具不足')
        self.assertNotIn('retryAtMs', self.runtime)
        self.client.error = None
        self.run_shop()
        self.assertEqual(len(self.client.calls), 1)
        self.client._apply_now_items({'Drop': {'Items': [{'NowItem': {'StaticID': 'AH806', 'Count': 500}}]}})
        self.run_shop()
        self.assertEqual(len(self.client.calls), 2)

    def test_stopping_after_first_request_prevents_second_purchase(self):
        self.client.activity_store_enabled = lambda: not self.client.calls
        with self.assertRaises(TaskDeferred):
            self.run_shop([ITEM, OLD + '_2'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertNotIn(ITEM, self.runtime['waitingForLogin'])

    def test_unknown_balance_or_missing_record_container_never_authorizes_purchase(self):
        self.client.account_state.pop('ItemContainer')
        self.assertFalse(self.status()['ready'])
        self.assertEqual(self.status()['note'], '等待登录同步刷新')
        self.client.account_state = state()
        self.client.account_state.pop('StoreRecordContainer')
        self.run_shop()
        self.assertEqual(self.client.calls, [])

    def test_inventory_wakes_only_interested_enabled_account_and_due_gates_use_status(self):
        acc = object.__new__(accounts.Account)
        acc.client = self.client
        acc.toggles, acc.params = {'activity_store': True}, {'activity_store': {'items': [ITEM]}}
        acc.task_runtime, acc._wake = {}, threading.Event()
        acc.guild_runtime = {}
        acc._on_inventory_increase({'1'})
        self.assertFalse(acc._wake.is_set())
        acc._on_inventory_increase({'AH806'})
        self.assertTrue(acc._wake.is_set())
        acc._enabled_ids = lambda: ['activity_store']
        acc.automation_wait_reason = lambda: None
        acc.next_claims = lambda now: {'activity_store': self.status()}
        self.assertEqual(acc._due_ids(), ['activity_store'])
        self.client.account_state['ItemContainer']['Items'][0]['Count'] = 0
        self.assertEqual(acc._due_ids(), [])

    def test_server_clock_deadline_is_converted_to_local_scheduler_time(self):
        result = tasks.next_claim_times(self.client.account_state, {}, now_ms=NOW, local_now_ms=NOW + 60_000)
        self.assertEqual(result['activity_store']['nextMs'], NOW + shop.DAY_MS + 60_000)


if __name__ == '__main__':
    unittest.main()
