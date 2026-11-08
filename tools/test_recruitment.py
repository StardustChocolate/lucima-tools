"""Native history schema, durable deduplication, account isolation and transport hooks."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from backend import config, recruitment as archive, appcore, resource_sync
from backend.game_client import GameClient, GameMessage, DuplicateLoginError
from backend.recruitment_catalog import table_rows

ROOT = Path(__file__).resolve().parents[1]
STAMP = 1793232000000


class Client:
    def __init__(self, aid='account-one'):
        self.aid = aid
        self.account_state = {'Info': {'_id': {'$oid': aid}}, 'StoreRecordContainer': {'Records': [
            {'Store': 'Summon', 'StaticID': 'NormalSummon', 'GuaranteedCount': 131},
            {'Store': 'Summon', 'StaticID': 'GalaxySummon'},
            {'Store': 'Summon', 'StaticID': 'MysticSummonH133', 'GuaranteedCount': 31},
            {'Store': 'Summon', 'StaticID': 'AcyivitySummonH178', 'GuaranteedCount': 7}]},
            'ActivityDBSettingContainer': {'SettingList': [
                {'ActivityID': sid, 'IsIgnoreTime': True, 'StartTime': {'$date': STAMP - 10000},
                 'EndTime': {'$date': STAMP + 1000000}} for sid in ('MysticSummonH133', 'AcyivitySummonH178')]}}
        self.logs = {'NormalSummon': [native_log('r1', 'H001'), native_log('r2', 'A0350'), native_log('r3', 'H133')],
                     'GalaxySummon': [], 'Mystic': [native_log('m1', 'H133'), native_log('m2', 'H001'), native_log('m3', 'H133')],
                     'AcyivitySummonH178': [native_log('l1', 'H178'), native_log('l2', 'A0350')]}
        self.calls, self.fail, self.bootstraps = [], set(), 0

    def _auth_data(self, body): return {**body, 'AID': self.aid, 'SessionID': 'test-session'}
    def bootstrap(self): self.bootstraps += 1; return self.account_state
    def call(self, route, body):
        self.calls.append((route, body))
        sid = body.get('CommodityStaticID', 'Mystic')
        if sid in self.fail: raise GameMessage('测试断网')
        return {'Logs': deepcopy(self.logs[sid])}


def native_log(identifier, sid, stamp=STAMP):
    return {'_id': {'$oid': identifier}, 'StaticID': sid, 'Time': {'$date': stamp}}


def shelves(stamp=STAMP):
    return {'Records': [{'Store': 'SecretShop', '_id': {'$oid': 's' + str(i)},
                        'LastResetTime': {'$date': stamp}, 'StaticID': 'SecretShop' + str(i + 1),
                        'DropResult': {'Items': [{'Item': {'StaticID': sid, 'Count': count}}]}}
                       for i, (sid, count) in enumerate([('5', 5), ('6', 50), ('1', 20), ('1', 30), ('1', 10), ('1', 40)])]}


class RecruitmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.patcher = patch.object(config, '_DATA_DIR', Path(self.temporary.name))
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def test_official_rules_are_current_and_differ_between_pool_types(self):
        data = archive.catalog()
        self.assertAlmostEqual(data['pools']['NormalSummon']['rates']['five'], .0315)
        self.assertAlmostEqual(data['pools']['GalaxySummon']['rates']['five'], .025)
        self.assertAlmostEqual(data['pools']['MysticSummonH133']['rates']['lightDark'], .00625)
        self.assertEqual(data['pools']['AcyivitySummonH178']['pityLimit'], 120)
        self.assertEqual(data['pools']['AcyivitySummonH178']['upArtifacts'], ['A0350'])

    def test_native_table_reader_tolerates_unused_corrupt_text_and_rejects_consumed_damage(self):
        path = Path(self.temporary.name) / 'sd_Role.txt'
        path.write_bytes(b'ID@IsHero@NAME@InitStar@Element@Description\nH001@1@Name@5@Fire@\xb2\n')
        self.assertEqual(list(table_rows(path))[0]['Element'], 'Fire')
        path.write_bytes(b'ID@IsHero@NAME@InitStar@Element@Description\nH001@1@Name@5@\xb2@unused\n')
        with self.assertRaises(ValueError): list(table_rows(path))

    def test_history_deduplicates_ids_but_keeps_distinct_results_at_same_time(self):
        client = Client()
        first = archive.refresh(client)
        pool = next(p for p in first['pools'] if p['kind'] == 'normal')
        self.assertEqual(pool['total'], 3)
        self.assertEqual(pool['pity']['remaining'], 49)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[0][1]['CommodityStaticID'], 'NormalSummon')
        second = archive.refresh(client)
        self.assertEqual(next(p for p in second['pools'] if p['kind'] == 'normal')['total'], 3)
        # Server retention shrinks; old saved records stay, a new ID is appended.
        client.logs['NormalSummon'] = [native_log('r3', 'H133'), native_log('r4', 'H001', STAMP + 1)]
        result = archive.refresh(client)
        self.assertEqual(next(p for p in result['pools'] if p['kind'] == 'normal')['total'], 4)
        self.assertEqual(archive.refresh(Client('other-account'))['pools'][0]['total'], 3)

    def test_partial_failure_retains_previous_stats_and_pool_sync_time(self):
        client = Client(); first = archive.refresh(client)
        old = next(p for p in first['pools'] if p['kind'] == 'normal')
        client.fail.add('NormalSummon')
        result = archive.refresh(client)
        pool = next(p for p in result['pools'] if p['kind'] == 'normal')
        self.assertTrue(pool['error']); self.assertEqual(pool['total'], 3)
        self.assertEqual(pool['updatedAt'], old['updatedAt']); self.assertIsNone(result['updatedAt'])
        self.assertFalse(next(p for p in result['pools'] if p['kind'] == 'galaxy')['error'])

    def test_invalid_history_is_atomic_and_missing_counters_stay_unknown(self):
        client = Client(); archive.refresh(client)
        client.logs['NormalSummon'] = [native_log('new', 'H001'), {'StaticID': 'H001'}]
        result = archive.refresh(client)
        self.assertEqual(next(p for p in result['pools'] if p['kind'] == 'normal')['total'], 3)
        client.account_state['StoreRecordContainer']['Records'][0].pop('GuaranteedCount')
        pool = next(p for p in archive.refresh(client)['pools'] if p['kind'] == 'normal')
        self.assertIsNone(pool['pity']['remaining'])

    def test_mystic_shared_logs_filter_period_and_count_actual_intervals(self):
        client = Client(); client.logs['Mystic'].append(native_log('old', 'H133', STAMP - 20000))
        pool = next(p for p in archive.refresh(client)['pools'] if p['kind'] == 'mystic')
        self.assertEqual(pool['total'], 3)
        self.assertEqual(pool['counts']['lightDark'], 2)
        self.assertEqual(pool['lightDarkIntervals'][0]['pulls'], 2)
        self.assertEqual(pool['pity']['remaining'], 169)
        self.assertEqual(pool['upRoles'], ['H133'])

    def test_unknown_result_is_never_guessed_to_be_low_rarity(self):
        client = Client(); client.logs['GalaxySummon'] = [native_log('unknown', 'FutureHero')]
        pool = next(p for p in archive.refresh(client)['pools'] if p['kind'] == 'galaxy')
        self.assertEqual(pool['counts']['unknown'], 1)
        self.assertIsNone(pool['fiveRate'])

    def test_rotation_keeps_confirmed_mystic_history_and_limited_pool_excludes_old_period(self):
        client = Client()
        client.logs['AcyivitySummonH178'].append(native_log('past', 'H178', STAMP - 100000))
        result = archive.refresh(client)
        self.assertEqual(next(p for p in result['pools'] if p['kind'] == 'limited')['total'], 2)
        client.account_state['ActivityDBSettingContainer']['SettingList'] = []
        client.account_state['StoreRecordContainer']['Records'] = client.account_state['StoreRecordContainer']['Records'][:2]
        result = archive.refresh(client)
        pool = next(p for p in result['pools'] if p['kind'] == 'mystic')
        self.assertEqual(pool['total'], 3)
        self.assertFalse(pool['active'])

    def test_shop_counts_units_round_hits_free_paid_and_east_eight_months(self):
        client = Client(); payload = {'StoreID': 'SecretShop', 'IsUseGold': 1}
        for _ in range(2): archive.observe_shop(client, payload, shelves())
        archive.observe_shop(client, {'StoreID': 'SecretShop', 'IsUseGold': 0}, shelves(STAMP + 1000))
        result = archive.shop_summary(archive.account_key(client), archive.catalog())
        month = result['months'][0]
        self.assertEqual(month['refreshes'], 2)
        self.assertEqual((month['paid'], month['free']), (1, 1))
        self.assertEqual((month['recruit'], month['mystery']), (10, 100))
        self.assertEqual((month['recruitHits'], month['mysteryHits']), (2, 2))
        self.assertEqual(archive.shop_summary(archive.account_key(Client('other')), archive.catalog())['months'], [])
        # 2026-10-31 16:01 UTC belongs to November in game time.
        archive.observe_shop(client, payload, shelves(1793462460000))
        self.assertEqual(archive.shop_summary(archive.account_key(client), archive.catalog())['months'][0]['month'], '2026-11')

    def test_real_transport_observes_success_and_never_turns_save_failure_into_retry(self):
        client = GameClient('test'); self.addCleanup(client.close)
        client.aid = 'transport-test'; client.account_state = {}
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=shelves())))
        with patch.object(client, '_sync_proxy'):
            client.call('StoreHandler.ResetRandomStore', {'StoreID': 'SecretShop', 'IsUseGold': 1})
            with patch.object(archive, 'observe_shop', side_effect=OSError('磁盘只读')):
                result = client.call('StoreHandler.ResetRandomStore', {'StoreID': 'SecretShop', 'IsUseGold': 1})
                self.assertEqual(result, shelves())
                self.assertIn('磁盘只读', client._recruitment_record_error)
        self.assertEqual(archive.shop_summary(archive.account_key(client), archive.catalog())['months'][0]['refreshes'], 1)

    def test_entry_reads_new_login_snapshot_and_never_spends_resources(self):
        client = Client()
        account = SimpleNamespace(client=client, lock=threading.Lock(), reset_task_runtime=lambda: None, note_state_refreshed=lambda: None)
        with patch.object(appcore, '_require', return_value=account), patch.object(appcore, '_status_snapshot', return_value={}):
            appcore.recruitment_refresh('first'); appcore.recruitment_refresh('first')
        self.assertEqual(client.bootstraps, 2)
        self.assertTrue(all(route.startswith('SummonLogHandler.Query') for route, _ in client.calls))
        account.lock.acquire()
        with patch.object(appcore, '_require', return_value=account):
            with self.assertRaises(appcore.ApiError): appcore.recruitment_refresh('first')
        account.lock.release()

    def test_entry_failure_preserves_duplicate_login_signal_and_releases_lock(self):
        account = SimpleNamespace(client=Client(), lock=threading.Lock())
        with patch.object(appcore, '_require', return_value=account):
            with patch.object(account.client, 'bootstrap', side_effect=OSError('断网')):
                with self.assertRaises(appcore.ApiError): appcore.recruitment_refresh('first')
            self.assertFalse(account.lock.locked())
            with patch.object(account.client, 'bootstrap', side_effect=DuplicateLoginError('Duplicate Login')):
                with self.assertRaises(DuplicateLoginError): appcore.recruitment_refresh('first')
            self.assertFalse(account.lock.locked())


if __name__ == '__main__': unittest.main()
