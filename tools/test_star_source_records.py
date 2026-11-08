"""Confirmed star-source purchases: monthly grouping, transport and persistence."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import httpx
from backend import config, recruitment as archive, tasks
from backend.game_client import GameClient
from tools.test_recruitment import STAMP
from tools.test_shop_records import shelf, purchase


def star_record():
    return tasks._synth_record(tasks.STAR_SOURCE_COMMODITY_ID, tasks.STAR_SOURCE_STORE_ID)


def star_receipt(identifier='gear-one', sid='E0011', set_id='Speed', stamp=STAMP, bought=1):
    current = star_record()
    current.update(_id={'$oid': 'star-shelf'}, BuyCount=bought, LastBuyTime={'$date': stamp})
    return {'Utc': str(stamp // 1000), 'CommodityRecord': current,
            'Drop': {'Items': [{'Equipment': {'_id': {'$oid': identifier}, 'StaticID': sid, 'Set': set_id}}]},
            'CostItems': [{'Item': {'StaticID': '29', 'Count': 10}}]}


class StarSourceRecords(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = patch.object(config, '_DATA_DIR', Path(temporary.name))
        patcher.start(); self.addCleanup(patcher.stop)
        self.client = SimpleNamespace(aid='star-account', account_state={})

    def summary(self, client=None):
        return archive.shop_summary(archive.account_key(client or self.client), archive.catalog())

    def buy(self, response, record=None, index=0):
        archive.observe_shop_purchase(self.client, {'Record': record or star_record(), 'ItemIndex': index, 'Count': 1}, response)

    def transport(self, response, status=200):
        client = GameClient('test'); self.addCleanup(client.close)
        client.aid = self.client.aid; client.session_id = 'offline-session'; client.account_state = {}
        client._client.close()
        def respond(request):
            return httpx.Response(status, json=response) if isinstance(response, dict) else httpx.Response(status, text=response)
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        return client

    def test_first_buy_with_synthesized_empty_record_is_observed_by_real_task_and_transport(self):
        response = star_receipt()
        client = self.transport(response)
        with patch.object(client, '_sync_proxy'):
            self.assertTrue(tasks.star_source_buy(client, 0)['ok'])
        month = self.summary()['starSource']['months'][0]
        self.assertEqual((month['month'], month['total']), ('2026-10', 1))
        self.assertEqual([(e['slotName'], e['setName'], e['count']) for e in month['equipment']], [('武器', '速度', 1)])
        self.assertNotIn('days', month)
        self.assertEqual(self.summary()['months'], [])

    def test_months_group_by_actual_equipment_part_and_set_without_daily_data(self):
        for i, (sid, set_id) in enumerate((('E0011', 'Speed'), ('E0021', 'Speed'), ('E0012', 'Speed'), ('E0011', 'Attack'))):
            self.buy(star_receipt(str(i), sid, set_id, bought=i+1))
        # Month follows the confirmed purchase date, not the request's reset.
        self.buy(star_receipt('next-month', 'E0016', 'Speed', stamp=1793462460000))
        months = self.summary()['starSource']['months']
        self.assertEqual([(m['month'], m['total']) for m in months], [('2026-11', 1), ('2026-10', 4)])
        self.assertEqual({(e['slot'], e['set']): e['count'] for e in months[1]['equipment']},
                         {('weapon', 'Speed'): 2, ('weapon', 'Attack'): 1, ('helmet', 'Speed'): 1})
        self.assertTrue(all('days' not in m for m in months))

    def test_same_equipment_is_counted_once_across_replayed_response_and_restart(self):
        response = star_receipt()
        for index in (0, 1, 2): self.buy(response, index=index)
        self.buy(star_receipt('second-gear'), index=0)
        restarted = SimpleNamespace(aid=self.client.aid, account_state={})
        self.assertEqual(self.summary(restarted)['starSource']['months'][0]['total'], 2)
        self.assertEqual(self.summary(SimpleNamespace(aid='other-account', account_state={}))['starSource']['months'], [])

    def test_response_without_equipment_id_uses_stable_server_receipt_and_counter(self):
        response = star_receipt()
        response['Drop']['Items'][0]['Equipment'].pop('_id')
        for _ in range(3): self.buy(response)
        second = deepcopy(response)
        second['CommodityRecord']['BuyCount'] = 2
        self.buy(second)
        self.assertEqual(self.summary()['starSource']['months'][0]['total'], 2)

    def test_utc_is_used_when_first_purchase_record_still_has_minimum_date(self):
        response = star_receipt()
        response['Drop']['Items'][0]['Equipment'].pop('_id')
        response['CommodityRecord']['LastBuyTime'] = {'$date': tasks._NEVER_MS}
        for _ in range(2): self.buy(response)
        self.assertEqual(self.summary()['starSource']['months'][0]['total'], 1)

    def test_purchase_drops_override_preview_and_unknown_equipment_is_not_mislabelled(self):
        record = star_record()
        record['DropResult'] = {'Items': [{'Equipment': {'StaticID': 'E0011', 'Set': 'Attack'}}]}
        self.buy(star_receipt('new', 'UNLISTED', 'NewGameSet'), record)
        row = self.summary()['starSource']['months'][0]['equipment'][0]
        self.assertEqual((row['staticID'], row['slotName'], row['setName']), ('UNLISTED', '未知部位', 'NewGameSet'))

    def test_preview_queries_refreshes_and_failed_buys_do_not_create_months(self):
        preview = {'CustomEquipDropList': [{'Equipment': {'StaticID': 'E0011', 'Set': 'Speed'}}]}
        client = self.transport(preview)
        with patch.object(client, '_sync_proxy'):
            tasks.star_source_query(client, {})
            tasks.star_source_refresh(client, {})
        client = self.transport('NotEnoughItem')
        with patch.object(client, '_sync_proxy'):
            self.assertFalse(tasks.star_source_buy(client, 0)['ok'])
        self.assertEqual(self.summary()['starSource']['months'], [])

    def test_ack_counter_or_preview_without_awarded_equipment_is_never_counted(self):
        for body in ({'ok': True}, {'CommodityRecord': star_receipt()['CommodityRecord']},
                     {'CustomEquipDropList': star_receipt()['Drop']['Items']}):
            with self.assertRaisesRegex(Exception, '缺少已购装备'): self.buy(body)
        self.assertEqual(self.summary()['starSource']['months'], [])

    def test_star_and_secret_shop_records_never_share_totals(self):
        secret = shelf(equip={'StaticID': 'E0011', 'Set': 'Attack'})
        archive.observe_shop_purchase(self.client, {'Record': secret, 'Count': 1}, purchase(secret))
        self.buy(star_receipt())
        result = self.summary()
        self.assertEqual(result['months'][0]['equipment'][0]['set'], 'Attack')
        self.assertEqual(result['months'][0]['purchases'], 1)
        self.assertEqual(result['starSource']['months'][0]['equipment'][0]['set'], 'Speed')

    def test_mismatched_record_does_not_create_a_purchase(self):
        response = star_receipt()
        response['CommodityRecord']['Store'] = 'SecretShop'
        with self.assertRaisesRegex(Exception, '不匹配'): self.buy(response)
        self.assertEqual(self.summary()['starSource']['months'], [])

    def test_disk_failure_does_not_fail_or_repeat_a_successful_game_purchase(self):
        client = self.transport(star_receipt())
        with (patch.object(client, '_sync_proxy'),
              patch('backend.recruitment.database', side_effect=OSError('只读')),
              patch('backend.game_client.log.exception') as log):
            self.assertTrue(tasks.star_source_buy(client, 0)['ok'])
            self.assertEqual(len(client._request_history), 1)
            self.assertIn('只读', client._recruitment_record_error)
            log.assert_called_once()


if __name__ == '__main__': unittest.main()
