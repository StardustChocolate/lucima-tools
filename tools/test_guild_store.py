"""Offline member-shop protocol, reset and scheduler regression coverage."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tools.test_guild_aid import AidClient, NOW
from backend import accounts, config
from backend.game_client import GameError
from backend.guild import GuildService
from backend.guild_store import GuildStoreService, store_catalog, store_options, reset_at, record_stamp
from tools import build_guild_store


class StoreClient(AidClient):
    def __init__(self):
        super().__init__()
        self.account_state['StoreRecordContainer'] = {'Records': []}
        self.fail_buy = False
        self.bad_reply = False
        self.missing_cost = False
        self.after_buy = None
        self.set_balance('29', 5000)
        self.set_balance('30', 5000)
        self.account_state['ActivityDBSettingContainer']['SettingList'] += [
            {'ActivityID': key, 'IsIgnoreTime': True} for key in ('Version_Guild_A0213', 'Version_Guild_A0201')]

    def set_balance(self, item, count):
        self.balance[item] = count
        self._apply_now_items({'Drop': {'Items': [{'NowItem': {'StaticID': item, 'Count': count}}]}})

    def call(self, route, data=None):
        if route != 'StoreHandler.BuyCommodity':
            return super().call(route, data)
        self.calls.append((route, copy.deepcopy(data)))
        record = copy.deepcopy(data['Record'])
        identifier, count = record['StaticID'], data['Count']
        meta = store_catalog()[identifier]
        cost = count * meta['price']
        assert self.balance[meta['currency']] >= cost
        self.balance[meta['currency']] -= cost
        record.update(_id={'$oid': identifier}, BuyCount=record['BuyCount'] + count,
                      LastBuyTime={'$date': self.now}, LastResetTime={'$date': self.now})
        result = {'CommodityRecord': record, 'Utc': str(self.now // 1000),
                  'CostItems': [{'NowItem': {'StaticID': meta['currency'], 'Count': self.balance[meta['currency']]}}],
                  'Drop': {'Items': [{'NowItem': {'StaticID': meta['item'], 'Count': meta['amount'] * count}}]}}
        if self.after_buy:
            self.after_buy()
        if self.fail_buy:
            raise GameError('timeout after commit')
        if self.bad_reply:
            result['CommodityRecord'] = None
        if self.missing_cost:
            result.pop('CostItems')
        self._apply_now_items(result)
        self._apply_commodity_record(result)
        return result


class GuildStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('time.time', return_value=NOW / 1000).start()
        self.addCleanup(patch.stopall)
        patch.object(config, 'load_accounts', return_value={}).start()
        patch.object(config, 'get_request_delay_config', return_value={'enabled': False, 'ms': 300, 'jitter': 20}).start()
        patch('httpx.Client.request', side_effect=AssertionError('No live requests')).start()
        self.client = StoreClient()
        self.acc = accounts.Account('store@example.invalid', self.client, None)
        self.acc.guild_runtime['enteredRevision'] = self.acc.state_revision
        self.service = GuildStoreService(GuildService(self.client, self.acc.guild_runtime))

    def select(self, *ids):
        self.acc.toggles['guild_auto_store'] = True
        self.acc.params['guild_auto_store'] = {'items': list(ids)}

    def purchases(self):
        return self.client.routes('BuyCommodity')

    def run_shop(self):
        self.acc._run_guild_if_due()

    def row(self, identifier):
        return next(row for row in self.service.snapshot()['goods'] if row['id'] == identifier)

    def test_catalog_and_all_icons_present(self):
        root = build_guild_store.ROOT
        data = store_catalog()
        self.assertEqual(sum(bool(row['reset']) for row in data.values()), 7)
        self.assertEqual(len(data), 12)
        for row in data.values():
            self.assertTrue((root / 'assets/items' / (row['item'] + '.png')).is_file())

    @unittest.skipUnless((build_guild_store.ROOT / 'assets_full').is_dir(),
                         'Optional extracted game tables are not distributed')
    def test_catalog_matches_local_source_tables(self):
        self.assertEqual(store_catalog(), build_guild_store.build(build_guild_store.ROOT / 'assets_full')['goods'])

    def test_off_no_guild_and_manual_choices_never_auto_buy(self):
        self.acc._store_guild_locked()
        self.select('GuildA0213', 'GuildAC01', 'unknown')
        self.run_shop()
        self.assertEqual(store_options(self.acc.params['guild_auto_store']), [])
        self.select('GuildB015')
        self.client.account_state['PlayerGuildInfo']['GID'] = ''
        self.run_shop()
        self.assertEqual(self.purchases(), [])

    def test_bulk_auto_buys_remaining_and_sleeps_until_reset(self):
        self.select('GuildB015', 'GuildContractMystic')
        self.run_shop()
        purchases = self.purchases()
        self.assertEqual({p['Record']['StaticID']: p['Count'] for p in purchases}, {'GuildB015': 5, 'GuildContractMystic': 1})
        self.assertEqual(purchases[0]['Platform'], 'WebGLPlayer')
        self.assertEqual(purchases[0]['ItemIndex'], -1)
        self.assertEqual(self.acc._enabled_ids(), [])
        self.assertEqual(self.acc.guild_runtime['shopWatchCurrencies'], set())
        self.assertEqual(self.acc._guild_due_delay(), (reset_at(NOW, 'Week') - NOW) / 1000)
        self.run_shop()
        self.assertEqual(len(self.purchases()), 2)
        self.assertFalse(any('Query' in route for route, _ in self.client.calls))

    def test_insufficient_funds_watch_only_unfinished_currency(self):
        self.client.set_balance('29', 61)
        self.select('GuildB015')
        self.run_shop()
        self.assertEqual(self.purchases()[0]['Count'], 2)
        self.assertEqual(self.row('GuildB015')['remaining'], 3)
        self.assertEqual(self.acc.guild_runtime['shopWatchCurrencies'], {'29'})
        self.acc._wake.clear()
        self.client.set_balance('30', 6000)
        self.assertFalse(self.acc._wake.is_set())
        self.client.set_balance('29', 200)
        self.assertTrue(self.acc._wake.is_set())
        self.run_shop()
        self.assertEqual(self.purchases()[-1]['Count'], 3)
        self.acc._wake.clear()
        self.client.set_balance('29', 9000)
        self.assertFalse(self.acc._wake.is_set())
        self.assertEqual(len(self.purchases()), 2)

    def test_week_and_month_reset_without_mutating_cache_before_purchase(self):
        self.select('GuildB015', 'GuildCertificateEterna')
        self.run_shop()
        before = copy.deepcopy(self.client.account_state['StoreRecordContainer'])
        next_week = reset_at(NOW, 'Week')
        self.clock.return_value = next_week / 1000
        self.client.now = next_week
        self.assertEqual(self.row('GuildB015')['remaining'], 5)
        self.assertEqual(self.row('GuildCertificateEterna')['remaining'], 0)
        self.assertEqual(self.client.account_state['StoreRecordContainer'], before)
        self.run_shop()
        self.assertEqual(len(self.purchases()), 3)
        next_month = reset_at(NOW, 'Month')
        self.clock.return_value = next_month / 1000
        self.client.now = next_month
        self.assertEqual(self.row('GuildCertificateEterna')['remaining'], 2)

    def test_reset_at_uses_utc_boundary_and_handles_year_rollover(self):
        now = int(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(datetime.fromtimestamp(reset_at(now, 'Month') / 1000, timezone.utc).isoformat(), '2027-01-01T00:00:00+00:00')
        self.assertEqual(record_stamp('0001/1/1 0:00:00'), -62135596800000)
        self.assertEqual(record_stamp('2026/10/2 0:20:21'), record_stamp('2026-10-02T00:20:21Z'))

    def test_missing_record_creates_default_record_but_missing_container_blocks(self):
        self.select('GuildB015')
        self.run_shop()
        self.assertEqual(self.purchases()[0]['Record']['_id'], '')
        self.assertEqual(self.purchases()[0]['Record']['BuyCount'], 0)
        self.assertEqual(self.purchases()[0]['Record']['Store'], 'MemberShop')
        del self.client.account_state['StoreRecordContainer']
        self.select('GuildTesseract')
        self.run_shop()
        self.assertEqual(len(self.purchases()), 1)
        self.assertTrue(self.row('GuildTesseract')['error'])

    def test_timeout_blocks_until_new_full_login_not_guild_refresh(self):
        self.client.fail_buy = True
        self.select('GuildB015')
        self.run_shop()
        self.assertTrue(self.row('GuildB015')['blocked'])
        self.acc.refresh_guild_locked(log_success=False)
        self.run_shop()
        self.assertEqual(len(self.purchases()), 1)
        self.assertTrue(self.row('GuildB015')['blocked'])
        # A real new snapshot confirms the committed purchase.
        self.client.account_state['StoreRecordContainer']['Records'] = [{
            '_id': {'$oid': 'GuildB015'}, 'Store': 'MemberShop', 'StaticID': 'GuildB015',
            'BuyCount': 5, 'LastResetTime': {'$date': NOW}}]
        self.acc._note_guild_login()
        self.assertFalse(self.row('GuildB015')['blocked'])
        self.acc._store_guild_locked()
        self.assertEqual(len(self.purchases()), 1)

    def test_malformed_response_does_not_retry_or_log_success(self):
        self.select('GuildB015')
        self.client.bad_reply = True
        self.run_shop()
        self.run_shop()
        self.assertEqual(len(self.purchases()), 1)
        self.assertTrue(self.row('GuildB015')['blocked'])
        self.assertFalse(self.acc.logs_since(0)[0]['result']['ok'])

    def test_unknown_balance_holds_same_currency_after_timeout_or_missing_cost(self):
        for error in ('fail_buy', 'missing_cost'):
            with self.subTest(error=error):
                setattr(self.client, error, True)
                self.select('GuildB015', 'GuildContractMystic')
                self.run_shop()
                self.assertEqual(len(self.purchases()), 1)
                self.assertTrue(self.row('GuildB015')['blocked'])
                self.assertTrue(self.row('GuildContractMystic')['blocked'])
                self.assertFalse(self.row('GuildTesseract')['blocked'])
                self.acc.guild_runtime.pop('shopUncertain')
                self.client.calls.clear()
                self.client.account_state['StoreRecordContainer']['Records'] = []
                setattr(self.client, error, False)

    def test_switch_off_between_purchases_stops_queue(self):
        self.select('GuildB015', 'GuildTesseract')
        self.client.after_buy = lambda: self.acc.toggles.update(guild_auto_store=False)
        self.run_shop()
        self.assertEqual(len(self.purchases()), 1)

    def test_changing_selection_between_purchases_is_respected(self):
        self.select('GuildB015', 'GuildTesseract')
        self.client.after_buy = lambda: self.acc.params.update(guild_auto_store={'items': []})
        self.run_shop()
        self.assertEqual(len(self.purchases()), 1)

    def test_manual_buy_validates_category_quantity_activity_and_actual_balance(self):
        with self.assertRaises(GameError):
            self.service.buy('GuildB015', 1)
        with self.assertRaises(GameError):
            self.service.buy('GuildA0213', 1, automatic=True)
        for count in (0, -1, True, 1.5, 2):
            with self.assertRaises(GameError):
                self.service.buy('GuildA0213', count)
        self.service.buy('GuildA0213', 1)
        self.assertEqual(self.row('GuildA0213')['remaining'], 0)
        with self.assertRaises(GameError):
            self.service.buy('GuildA0213', 1)
        self.service.buy('GuildAC01', 3)
        self.assertEqual(self.service.snapshot()['currencies']['30'], 4460)
        self.assertIsNone(self.row('GuildAC01')['remaining'])
        self.client.account_state['ActivityDBSettingContainer']['SettingList'] = []
        with self.assertRaises(GameError):
            self.service.buy('GuildA0201', 1)

    def test_account_runtime_and_selections_are_isolated(self):
        self.select('GuildB015')
        self.client.fail_buy = True
        self.run_shop()
        other = accounts.Account('other@example.invalid', StoreClient(), None)
        self.assertFalse(other.toggles.get('guild_auto_store'))
        self.assertNotIn('shopUncertain', other.guild_runtime)


if __name__ == '__main__':
    unittest.main()
