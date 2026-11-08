"""Equipment library calculations and secret-shop identity regressions, offline."""
from copy import deepcopy
from itertools import product
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import appcore, tasks
from equipment import library, plans
from tools.test_equipment_rankings import naked_panel


def native_equipment(count=8):
    refs = tasks._equip_ref()
    ids = {slot: next(sid for sid, ref in refs.items() if ref.get('slot') == slot and ref.get('level') == 85)
           for slot in plans.SLOTS}
    result = []
    for slot in plans.SLOTS:
        for i in range(count):
            result.append({'_id': {'$oid': f'{slot}-{i}'}, 'StaticID': ids[slot], 'LV': 0 if i == count-1 else 15,
                           'ClassLV': 4, 'Set': 'Speed' if i % 2 == 0 else 'Health', 'EquipRole': '', 'IsLock': i % 3 == 0,
                           'MainProp': {'PropertyType': 'SpeedValue' if slot == 'boots' else 'HPValue', 'Value': 45 if slot == 'boots' else 2700},
                           'SubProps': {'SourceValues': [
                               {'PropertyType': 'AttackRate', 'Value': max(.04, .18-i*.015)},
                               {'PropertyType': 'HPRate', 'Value': max(.03, .16-i*.01)},
                               {'PropertyType': 'CriticalRate', 'Value': max(.02, .12-i*.01)},
                               {'PropertyType': 'SpeedValue', 'Value': max(2, 18-i*2)}]}})
    return result


def ui_fixture():
    equips = native_equipment()
    views = [{**plans._equip_view(e), 'quality': library.quality(e), 'roleName': ''} for e in equips]
    from equipment.api import _meta
    return {**_meta(), 'roles': [], 'equips': views, 'library': library.overview(equips)}


class LibraryTests(unittest.TestCase):
    def test_quality_uses_actual_stats_and_fixed_base_for_flat_values(self):
        equip = {'SubProps': {'SourceValues': [
            {'PropertyType': 'AttackValue', 'Value': 88.9},  # 10% of reference attack
            {'PropertyType': 'CriticalRate', 'Value': .10},
            {'PropertyType': 'CriticalDamageRate', 'Value': .10},
            {'PropertyType': 'SpeedValue', 'Value': 13}]}}
        before = deepcopy(equip)
        result = library.quality(equip)
        self.assertEqual(result['score'], 62)
        self.assertEqual(result['grade'], '满配')
        self.assertEqual(equip, before)

    def test_five_builds_match_supplied_naked_panel_and_never_share_equipment(self):
        equips = native_equipment(8)
        by_id = {plans._oid(e['_id']): e for e in equips}
        before = deepcopy(equips)
        for speed_only in (False, True):
            seen = set()
            for build in library.speed_builds(equips, require_speed=speed_only):
                ids = build['equipIds']
                self.assertEqual(len(ids), 6)
                self.assertFalse(seen.intersection(ids))
                selected = [by_id[eid] for eid in ids]
                self.assertEqual({plans.equip_slot(e) for e in selected}, set(plans.SLOTS))
                self.assertTrue(all(e['LV'] == 15 for e in selected))
                self.assertEqual(build['speed'], naked_panel('speed', selected)['Speed'])
                if speed_only:
                    self.assertGreaterEqual(sum(e['Set'] == 'Speed' for e in selected), 4)
                seen.update(ids)
        self.assertEqual(equips, before)

    def test_fastest_build_is_verified_against_exhaustive_search(self):
        equips = native_equipment(4)
        for i, e in enumerate(equips):
            e['LV'] = 15
            e['Set'] = ('Speed', 'Revenge', 'Health')[i % 3]
        by_slot = [[e for e in equips if plans.equip_slot(e) == slot] for slot in plans.SLOTS]
        for speed_only in (False, True):
            best = max(naked_panel('speed', combo)['Speed']
                       for combo in product(*by_slot) if not speed_only or sum(e['Set'] == 'Speed' for e in combo) >= 4)
            self.assertEqual(library.speed_builds(equips, require_speed=speed_only)[0]['speed'], best)

    def test_summary_excludes_unfinished_gear_and_boot_main_from_subspeed_counts(self):
        equips = native_equipment(2)
        s = library.overview(equips)
        self.assertEqual((s['total'], s['maxed'], s['embryos']), (12, 6, 6))
        self.assertEqual(s['axes'][0]['value'], 18)
        self.assertEqual(s['speedCounts'][0]['all'], 6)
        self.assertEqual(s['speedCounts'][-1]['all'], 0)
        self.assertEqual(len(s['speedBuilds']), 1)
        empty = library.overview([])
        self.assertEqual(empty['power'], 0)
        self.assertEqual(empty['speedBuilds'], [])
        equips.pop(0)
        self.assertEqual(library.speed_builds(equips), [])

    def test_speed_ranking_retains_fractional_values(self):
        equips = native_equipment(1)
        for equip in equips:
            equip.update(LV=15, Set='Health', MainProp={'PropertyType': 'HPValue', 'Value': 100})
            equip['SubProps'] = {'SourceValues': []}
        equips[0]['SubProps']['SourceValues'] = [{'PropertyType': 'SpeedValue', 'Value': 1.5}]
        self.assertEqual(library.speed_builds(equips)[0]['speed'], 130.5)


class ShopIdentityTests(unittest.TestCase):
    def setUp(self):
        self.equip = native_equipment(2)[0]
        self.target = {'_id': {'$oid': 'target'}, 'Store': 'SecretShop', 'BuyCount': 0,
                       'DropResult': {'Items': [{'Equipment': self.equip}]}}
        self.other = {'_id': 'other', 'Store': 'SecretShop', 'BuyCount': 0,
                      'DropResult': {'Items': [{'Item': {'StaticID': '5', 'Count': 1}}]}}
        self.client = SimpleNamespace(account_state={'StoreRecordContainer': {'Records': [self.other, self.target]}},
                                      bootstrap=Mock(), _auth_data=lambda x: x,
                                      call=Mock(return_value={'CommodityRecord': {'BuyCount': 1}}))
        self.acc = SimpleNamespace(client=self.client, lock=threading.Lock(), shop_shelf=[self.target, self.other])
        self.enterContext(patch.object(appcore, '_require', return_value=self.acc))
        self.enterContext(patch.object(appcore, '_status_snapshot', return_value={}))

    def test_login_reorders_shelf_but_purchase_resolves_selected_equipment(self):
        result = appcore.shop_buy_ep('offline', 0, tasks.equipment_key(self.equip))
        self.assertTrue(result['ok'])
        self.assertEqual(self.client.call.call_count, 1)
        route, payload = self.client.call.call_args.args
        self.assertEqual(route, 'StoreHandler.BuyCommodity')
        self.assertEqual(payload['Record']['_id'], 'target')
        self.assertEqual(self.target['BuyCount'], 1)

    def test_sold_or_replaced_equipment_does_not_trigger_a_purchase(self):
        key = tasks.equipment_key(self.equip)
        self.target['BuyCount'] = 1
        self.assertFalse(appcore.shop_buy_ep('offline', 0, key)['ok'])
        self.target['BuyCount'] = 0
        self.equip['SubProps']['SourceValues'][0]['Value'] = .01
        self.assertFalse(appcore.shop_buy_ep('offline', 0, key)['ok'])
        self.client.call.assert_not_called()

    def test_transport_order_and_derived_metadata_do_not_change_equipment_identity(self):
        key = tasks.equipment_key(self.equip)
        self.equip['SubProps']['SourceValues'].reverse()
        self.equip['SubProps']['DisplayCache'] = {'transient': True}
        self.equip['_id'] = self.equip['_id']['$oid']
        self.assertEqual(tasks.equipment_key(self.equip), key)
        self.assertTrue(appcore.shop_buy_ep('offline', 0, key)['ok'])

    def test_changed_equipment_id_or_enhancement_invalidates_the_selection(self):
        key = tasks.equipment_key(self.equip)
        self.equip['LV'] = 14
        self.assertNotEqual(tasks.equipment_key(self.equip), key)
        self.equip['LV'] = 15
        self.equip['_id'] = 'replacement'
        self.assertNotEqual(tasks.equipment_key(self.equip), key)

    def test_invalid_index_and_repeated_legacy_click_do_not_send_requests(self):
        for index in (True, -1, '0', 99):
            self.assertFalse(tasks.shop_buy_index(self.client, [self.target], index)['ok'])
        self.target['BuyCount'] = 1
        self.assertFalse(tasks.shop_buy_index(self.client, [self.target], 0)['ok'])
        self.client.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
