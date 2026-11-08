"""Offline quantity, daily-limit and confirmed-receipt regressions."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from backend import appcore, energy_exchange as energy
from backend.game_client import GameClient, GameError
from backend.management_catalog import read_rows


def milliseconds(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=energy.BEIJING).timestamp() * 1000)


NOW = milliseconds('2026-10-30T10:00:00')


def state(vip=12, *, diamonds=25_377, used=1, reset=NOW):
    return {'VIPInfo': {'LV': vip}, 'ItemContainer': {'Items': [
        {'StaticID': sid, 'Count': count} for sid, count in [('37', 13), ('2', diamonds), ('83', 280), ('3', 402)]]},
        'StoreRecordContainer': {'Records': [{'_id': {'$oid': 'diamond-record'}, 'StaticID': 'Normal11',
            'Store': 'Normal', 'BuyCount': used, 'LastResetTime': {'$date': reset}}]}}


class EnergyExchangeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(energy.time, 'time', return_value=NOW / 1000))

    def client(self):
        client = GameClient.__new__(GameClient)
        client.aid, client.session_id = 'offline', 'offline'
        client.account_state = state()
        def call(route, payload):
            self.assertEqual(route, 'StoreHandler.BuyCommodity')
            sid, count = payload['Record']['StaticID'], payload['Count']
            rule = energy.catalog()['commodities'][sid]
            items = {item['StaticID']: item['Count'] for item in client.account_state['ItemContainer']['Items']}
            record = deepcopy(payload['Record'])
            record.update(BuyCount=record['BuyCount'] + count, LastResetTime={'$date': NOW})
            response = {'CostItems': [{'NowItem': {'StaticID': rule['currency'], 'Count': items[rule['currency']] - rule['price'] * count}}],
                'Drop': {'Items': [{'Item': {'StaticID': rule['item'], 'Count': rule['gain'] * count},
                                   'NowItem': {'StaticID': rule['item'], 'Count': items[rule['item']] + rule['gain'] * count}}]},
                'CommodityRecord': record}
            # Real transport already merges balances before returning a receipt.
            client._apply_now_items(response)
            client._apply_commodity_record(response)
            return response
        client.call = Mock(side_effect=call)
        return client

    def test_catalog_matches_official_commodity_and_vip_tables(self):
        source = Path(__file__).resolve().parents[1] / 'build/season-refresh-1028/pure-reader'
        if not source.is_dir():
            self.skipTest('Official local tables not available')
        self.assertEqual(energy.build_catalog(lambda name: read_rows(source / f'sd_{name}.txt'), 'catalog_115942'), energy.catalog())

    def test_vip_and_balances_jointly_limit_maximum(self):
        rows = {r['id']: r for r in energy.snapshot(state(), now_ms=NOW)}
        self.assertEqual((rows['Normal11']['dailyLimit'], rows['Normal11']['remaining'], rows['Normal11']['maxBuy']), (33, 32, 32))
        self.assertEqual((rows['Normal6']['maxBuy'], rows['HeartShop8']['maxBuy']), (13, 2))
        vip0 = energy.snapshot(state(0, used=0, diamonds=90), now_ms=NOW)[1]
        self.assertEqual((vip0['dailyLimit'], vip0['maxBuy']), (2, 2))
        poor = energy.snapshot(state(diamonds=89), now_ms=NOW)[1]
        self.assertEqual(poor['maxBuy'], 2)
        exhausted = energy.snapshot(state(used=33), now_ms=NOW)[1]
        self.assertEqual((exhausted['maxBuy'], exhausted['remaining']), (0, 0))

    def test_reset_is_beijing_eight_and_projection_keeps_raw_record(self):
        source = state(used=33, reset=milliseconds('2026-10-29T08:02:00'))
        before = deepcopy(source)
        self.assertEqual(energy.snapshot(source, now_ms=milliseconds('2026-10-30T07:59:59'))[1]['maxBuy'], 0)
        self.assertEqual(energy.snapshot(source, now_ms=milliseconds('2026-10-30T08:00:00'))[1]['maxBuy'], 33)
        self.assertEqual(source, before)

    def test_missing_record_uses_zero_bought_but_unknown_vip_blocks_diamonds(self):
        source = state(); source['StoreRecordContainer']['Records'] = []
        self.assertEqual(energy.snapshot(source, now_ms=NOW)[1]['remaining'], 33)
        source.pop('VIPInfo')
        rows = energy.snapshot(source, now_ms=NOW)
        self.assertEqual(rows[1]['maxBuy'], 0)
        self.assertIn('未同步', rows[1]['reason'])
        self.assertEqual(rows[0]['maxBuy'], 13)

    def test_selected_count_cost_gain_balances_and_missing_record_id_update(self):
        client = self.client()
        result = energy.buy(client, 'Normal11', 32)
        self.assertEqual(result['detail'], '体力管理：消耗 960 钻石，获得 1920 体力')
        self.assertEqual(client.call.call_args.args[1]['Count'], 32)
        rows = energy.snapshot(client.account_state, now_ms=NOW)
        self.assertEqual((rows[1]['current'], rows[1]['remaining'], rows[1]['maxBuy']), (24_417, 0, 0))
        self.assertFalse(client._energy_exchange_uncertain)
        energy.buy(client, 'HeartShop8', 2)
        rows = energy.snapshot(client.account_state, now_ms=NOW)
        self.assertEqual((rows[0]['current'], rows[2]['current']), (33, 80))
        energy.buy(client, 'Normal6', 33)
        rows = energy.snapshot(client.account_state, now_ms=NOW)
        self.assertEqual(rows[0]['current'], 0)
        self.assertEqual(client.call.call_count, 3)

    def test_invalid_or_excess_counts_never_send_purchase(self):
        for count in [0, -1, True, '2', 1.5, None, 33, 2**40]:
            client = self.client()
            with self.subTest(count=count), self.assertRaises(GameError):
                energy.buy(client, 'Normal11', count)
            client.call.assert_not_called()
        client = self.client()
        with self.assertRaises(GameError):
            energy.buy(client, 'not-an-energy-item', 1)
        client.call.assert_not_called()

    def test_unconfirmed_receipt_keeps_uncertain_and_never_retries(self):
        for response in [None, {}, {'Drop': []}, {'CostItems': [], 'CommodityRecord': {}}]:
            client = self.client(); client.call = Mock(return_value=response)
            with self.subTest(response=response), self.assertRaisesRegex(GameError, '未确认'):
                energy.buy(client, 'Normal11', 1)
            self.assertTrue(client._energy_exchange_uncertain)
            self.assertEqual(client.call.call_count, 1)

    def test_missing_daily_counter_forces_resync_before_another_options_read(self):
        client = self.client()
        purchase = client.call.side_effect
        def missing_counter(route, payload):
            response = purchase(route, payload)
            response.pop('CommodityRecord')
            return response
        client.call.side_effect = missing_counter
        with self.assertRaisesRegex(GameError, '剩余次数未确认'):
            energy.buy(client, 'Normal11', 1)
        self.assertTrue(client._energy_exchange_uncertain)
        client.bootstrap = Mock()
        acc = SimpleNamespace(client=client, lock=threading.RLock(), note_state_refreshed=Mock())
        with patch.object(appcore, '_require', return_value=acc), patch.object(appcore, '_status_snapshot', return_value={}):
            appcore.energy_options('offline')
            appcore.energy_options('offline')
        client.bootstrap.assert_called_once_with()
        acc.note_state_refreshed.assert_called_once_with()
        self.assertFalse(client._energy_exchange_uncertain)


if __name__ == '__main__':
    unittest.main()
