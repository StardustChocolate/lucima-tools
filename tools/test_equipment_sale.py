"""Fixed sale rules, confirmed income and incremental sweep inventory."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from backend import equipment_sale as sales, tasks
from backend.game_client import GameError


REFS = {f'E{level}': {'level': level, 'slot': 'weapon'} for level in (1, 70, 71, 85, 90)}


def equip(index=0, level=85, tier=2, **fields):
    return {'_id': {'$oid': f'e{index}'}, 'StaticID': f'E{level}', 'ClassLV': tier,
            'IsLock': False, 'EquipRole': '', 'Enhance': 0, **fields}


def drop(amount=100):
    return {'Drop': {'Items': [{'Item': {'StaticID': sid, 'Count': amount + index},
                              'NowItem': {'StaticID': sid, 'Count': 999999}}
                             for index, sid in enumerate(('1', *sales.POWDER_IDS))]}}


class Client:
    def __init__(self, rows, responses=None):
        self.account_state = {'EquipmentContainer': {'Datas': deepcopy(rows)}}
        self.calls = []
        self.responses = responses
        self.refreshed = 0

    def _auth_data(self, body):
        return {'AID': 'test', 'SessionID': 'test', **body}

    def call(self, route, body):
        self.calls.append((route, body))
        result = self.responses[len(self.calls) - 1] if self.responses else drop()
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def bootstrap(self):
        self.refreshed += 1
        self.account_state['EquipmentContainer']['Datas'] = []


class EquipmentSaleTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(tasks, '_equip_ref', return_value=REFS)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_all_tiers_and_level_boundaries(self):
        for level in (1, 70, 71, 85, 90):
            for tier in range(5):
                with self.subTest(level=level, tier=tier):
                    self.assertEqual(sales.eligible(equip(level=level, tier=tier), REFS),
                                     (tier != 4 or level <= 70) and not (tier == 3 and level >= 85))

    def test_epic85_is_opt_in_but_upgrades_locks_and_worn_items_are_never_sold(self):
        self.assertFalse(sales.eligible(equip(tier=3),REFS))
        self.assertTrue(sales.eligible(equip(tier=3),REFS,include_epic85=True))
        for fields in ({'Enhance':1},{'Enhance':15},{'Enhance':None},{'Enhance':'broken'},
                       {'EnhanceLevel':3},{'IsLock':True},{'EquipRole':'r0'}):
            self.assertFalse(sales.eligible(equip(tier=3,**fields),REFS,include_epic85=True),fields)
        missing=equip();missing.pop('Enhance')
        self.assertFalse(sales.eligible(missing,REFS,include_epic85=True))
        self.assertFalse(sales.eligible(equip(tier=4),REFS,include_epic85=True))

    def test_borrowed_unequipped_baseline_gear_is_reserved_from_automatic_sales(self):
        client=Client([equip(1),equip(2)])
        client._intimacy_reserved_equipment={'e1'}
        result=sales.sell(client,include_epic85=True)
        self.assertEqual(result['sold'],1)
        self.assertEqual(client.calls[0][1]['IDs'],['e2'])

    def test_locks_owners_and_unknown_metadata_are_retained(self):
        for fields in ({'IsLock': True}, {'IsLock': 'true'}, {'EquipRole': 'role'},
                       {'EquipRole': {'$oid': 'role'}}, {'StaticID': 'missing'},
                       {'ClassLV': 5}, {'ClassLV': None}, {'ClassLV': True}, {'_id': None}):
            with self.subTest(fields=fields):
                self.assertFalse(sales.eligible(equip(**fields), REFS))

    def test_sell_batches_and_six_powders_use_drop_counts_not_absolute_wallet(self):
        client = Client([equip(i) for i in range(72)] + [equip(100, tier=4), equip(101, IsLock=True)])
        result = sales.sell(client)
        self.assertTrue(result['ok'])
        self.assertEqual(result['sold'], 72)
        self.assertEqual([len(body['IDs']) for _, body in client.calls], [35, 35, 2])
        self.assertTrue(all(route == 'EquipmentHandler.SellEquips' for route, _ in client.calls))
        self.assertTrue(all(set(body) == {'IDs', 'AID', 'SessionID'} for _, body in client.calls))
        self.assertEqual({row['id']: row['count'] for row in result['rewardSummary']},
                         {sid: (100 + i) * 3 for i, sid in enumerate(('1', *sales.POWDER_IDS))})
        self.assertEqual(len(client.account_state['EquipmentContainer']['Datas']), 2)
        self.assertEqual(sales.sell(client)['sold'], 0)
        self.assertEqual(len(client.calls), 3)

    def test_partial_failure_keeps_confirmed_income_and_does_not_retry(self):
        client = Client([equip(i) for i in range(50)], [drop(7), GameError('backpack error')])
        result = sales.sell(client)
        self.assertFalse(result['ok'])
        self.assertEqual(result['sold'], 35)
        self.assertEqual(result['rewardSummary'][0]['count'], 7)
        self.assertIn('backpack error', result['detail'])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(client.account_state['EquipmentContainer']['Datas']), 15)

    def test_missing_confirmation_never_removes_or_counts_equipment(self):
        client = Client([equip()], [{'Utc': 123}])
        result = sales.sell(client)
        self.assertFalse(result['ok'])
        self.assertEqual(result['sold'], 0)
        self.assertEqual(result['rewardSummary'], [])
        self.assertEqual(len(client.account_state['EquipmentContainer']['Datas']), 1)
        self.assertIn('未自动重试', result['detail'])
        # A subsequent explicit enable refreshes ownership instead of resending.
        self.assertEqual(sales.sell(client)['sold'], 0)
        self.assertEqual(client.refreshed, 1)
        self.assertEqual(len(client.calls), 1)

    def test_duplicate_inventory_oids_are_sent_only_once(self):
        client = Client([equip(), equip()])
        self.assertEqual(sales.sell(client)['sold'], 1)
        self.assertEqual(client.calls[0][1]['IDs'], ['e0'])

    def test_drop_inventory_is_merged_by_oid_and_sold_on_next_pass(self):
        client = Client([equip(0, tier=4)])
        response = {'Drop': {'Items': [{'Equipment': equip(1)}, {'Equipment': equip(1)},
                                     {'Equipment': equip(0, tier=4, Enhance=15)}]}}
        sales.merge_equipment_drop(client, response)
        sales.merge_equipment_drop(client, response)
        rows = client.account_state['EquipmentContainer']['Datas']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['Enhance'], 15)
        self.assertEqual(sales.sell(client)['sold'], 1)

    def test_reward_summary_collects_all_resources_without_double_counting_now_item(self):
        result = drop(2)
        result['Drop']['Items'] += [{'Item': {'StaticID': '1', 'Count': 3}},
                                   {'Equipment': equip()}, {'Role': {'StaticID': 'H001'}},
                                   {'Artifact': {'StaticID': 'A001'}}, {'Item': {'StaticID': '1', 'Count': -7}}]
        rows = {(row['kind'], row['id']): row['count'] for row in sales.reward_summary(result)}
        self.assertEqual(rows[('Item', '1')], 5)
        self.assertEqual(rows[('Equipment', 'E85')], 1)
        self.assertEqual(rows[('Role', 'H001')], 1)
        self.assertEqual(rows[('Artifact', 'A001')], 1)

    def test_finish_scene_exposes_rewards_and_keeps_equipment_for_periodic_sales(self):
        response = drop()
        response['Drop']['Items'].append({'Equipment': equip(2)})
        client = Client([], [response])
        client.account_state.update(Teams={'Settings':[{'ID':'T','TeamSetting':{'RolePosMap':{'r':'0'}}}]},
                                    SceneDataContainer={'Scenes':[{'StaticID':'HuntFire_11'}]})
        with patch.object(tasks, '_build_role_for_battle', return_value={}), \
             patch.object(tasks, 'parse_equipment', return_value={'name':'测试装备'}):
            result = tasks.sweep_once(client, 'HuntFire_11', 'T')
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['rewardSummary']), 8)
        self.assertEqual(len(client.account_state['EquipmentContainer']['Datas']), 1)


if __name__ == '__main__':
    unittest.main()
