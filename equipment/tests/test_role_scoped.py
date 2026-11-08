"""按角色排除与预存方案穿戴；所有游戏协议均在内存中模拟。"""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.game_client import GameError
from equipment import api, auto, plans, stats
from equipment.tests.test_auto import config, fixture


class RoleExclusionTests(unittest.TestCase):
    def setUp(self):
        self.state, self.roles = fixture(4)
        self.acc = SimpleNamespace(client=SimpleNamespace(account_state=self.state), lock=threading.Lock())

    def handle(self, action, **body):
        return api.handle(self.acc, action, body, lambda sid: None)

    def test_each_role_uses_its_own_exclusions_and_keeps_server_candidate_order(self):
        entries = [{**config('r0'), 'excludedRoleIds': ['r2']},
                   {**config('r1'), 'excludedRoleIds': ['r0']}]
        self.acc.equipment_excluded_roles = ['r3']  # 旧轮次规则不影响新客户端。
        with patch.object(auto, 'search', wraps=auto.search) as search:
            result = self.handle('autoMulti', entries=entries, includeWorn=True)
        self.assertEqual([call.kwargs['excluded_roles'] for call in search.call_args_list], [['r2'], ['r0']])
        owners = {plans._oid(e['_id']): plans._oid(e['EquipRole']) for e in self.state['EquipmentContainer']['Datas']}
        for entry, excluded in zip(result['entries'], [{'r2'}, {'r0'}]):
            self.assertTrue(entry['results'])
            for combo in entry['results']:
                self.assertFalse(excluded.intersection(owners[eid] for eid in combo['equipIds'].values()))
        self.assertEqual(result['configs'], entries)
        self.assertEqual(result['options']['excluded_roles'], [])
        self.assertEqual(self.acc.equipment_excluded_roles, ['r3'])

    def test_empty_role_exclusions_override_legacy_round_exclusions(self):
        self.acc.equipment_excluded_roles = ['r1', 'r2', 'r3']
        result = self.handle('autoMulti', entries=[{**config('r0'), 'excludedRoleIds': []}], includeWorn=True)
        self.assertEqual(set(result['entries'][0]['pool'].values()), {4})

    def test_changing_candidate_preserves_each_roles_exclusions(self):
        entries = [{**config('r0'), 'excludedRoleIds': ['r2']}, {**config('r1'), 'excludedRoleIds': ['r0']}]
        batch = self.handle('autoMulti', entries=entries, includeWorn=True)
        self.acc.equipment_excluded_roles = ['r4']
        changed = self.handle('chooseMulti', batchId=batch['batchId'], index=0, pick=0)
        self.assertEqual(changed['configs'], entries)
        self.assertEqual(changed['entries'][0]['pick'], 0)

    def test_invalid_role_exclusions_are_rejected_before_search(self):
        for ids in ('r1', {'r1': True}, [None], ['missing'], ['r0']):
            with self.subTest(ids=ids), patch.object(auto, 'search_multi') as search:
                with self.assertRaises(GameError):
                    self.handle('autoMulti', entries=[{**config('r0'), 'excludedRoleIds': ids}])
                search.assert_not_called()


class SavedPlanWearTests(unittest.TestCase):
    def setUp(self):
        self.state, self.roles = fixture(4)
        self.calls = []
        self.saved = [[f'e{slot}-{index}' for slot in range(6)] for index in range(4)]
        self.client = SimpleNamespace(account_state=self.state, _auth_data=lambda data: data, call=self.call)
        self.client.bootstrap = lambda: self.fail('unexpected bootstrap; protocol response must already reflect the applied equipment')
        self.acc = SimpleNamespace(client=self.client, lock=threading.Lock())

    def call(self, method, data):
        self.calls.append((method, copy.deepcopy(data)))
        if method == 'EquipPlanHandler.QueryPlanByRoleID':
            return {'PlanEquipIDs': {str(index): ids for index, ids in enumerate(self.saved)}}
        if method == 'EquipmentHandler.EquipsToRole':
            plans._assume_worn(self.client, data['RoleID'], data['EquipIDs'])
            return {}
        if method == 'EquipmentHandler.TakeOffEquip':
            next(e for e in plans._equips(self.client) if plans._oid(e['_id']) == data['EquipID'])['EquipRole'] = ''
            return {}
        self.fail(f'unexpected protocol call {method}')

    def handle(self, **body):
        return api.handle(self.acc, 'equipSavedPlan', body, lambda sid: None)

    def test_buttons_one_to_four_use_the_matching_server_saved_plan_and_refresh_attributes(self):
        for index in range(4):
            with self.subTest(index=index):
                result = self.handle(roleId='r0', planIndex=index, equipIds=['forged'])
                self.assertTrue(result['verified'])
                actual = {plans._oid(e['_id']) for e in plans._equips(self.client) if plans._oid(e['EquipRole']) == 'r0'}
                self.assertEqual(actual, set(self.saved[index]))
                role = next(role for role in result['overview']['roles'] if role['id'] == 'r0')
                worn, _ = plans.worn_items(plans._equips(self.client), 'r0')
                self.assertEqual(role['current']['panel'], stats.role_panel(self.state, self.roles['r0'], worn)['panel'])
                self.assertEqual(role['current']['count'], 6)
                self.assertEqual(result['planIndex'], index)
        self.assertFalse(any(method == 'EquipPlanHandler.SavePlan' for method, _ in self.calls))

    def test_current_plan_does_not_resubmit_already_worn_ids(self):
        self.assertTrue(self.handle(roleId='r0', planIndex=0)['verified'])
        self.assertEqual([method for method, _ in self.calls], ['EquipPlanHandler.QueryPlanByRoleID'])

    def test_partial_saved_plan_removes_only_unfilled_slots(self):
        self.saved[1] = self.saved[1][:-1]
        result = self.handle(roleId='r0', planIndex=1)
        self.assertTrue(result['verified'])
        self.assertEqual([body['EquipID'] for method, body in self.calls if method == 'EquipmentHandler.TakeOffEquip'], ['e5-0'])
        role = next(role for role in result['overview']['roles'] if role['id'] == 'r0')
        self.assertEqual(role['worn'], 5)
        self.assertEqual(role['current']['count'], 5)

    def test_empty_unknown_or_duplicate_slot_plans_never_change_equipment(self):
        for ids in ([], ['missing'], ['e0-0', 'e0-1']):
            with self.subTest(ids=ids):
                self.calls.clear()
                before = copy.deepcopy(self.state)
                self.saved[0] = ids
                with self.assertRaises(GameError): self.handle(roleId='r0', planIndex=0)
                self.assertEqual(self.state, before)
                self.assertEqual([method for method, _ in self.calls], ['EquipPlanHandler.QueryPlanByRoleID'])

    def test_invalid_role_and_plan_index_do_not_query_or_wear(self):
        for body in ({'roleId':'missing','planIndex':0}, {'roleId':'r0','planIndex':True},
                     {'roleId':'r0','planIndex':-1}, {'roleId':'r0','planIndex':4}):
            with self.subTest(body=body), self.assertRaises(GameError): self.handle(**body)
        self.assertEqual(self.calls, [])

    def test_failed_wear_returns_actual_overview_and_never_claims_success(self):
        original = self.client.call
        def fail_wear(method, data):
            if method == 'EquipmentHandler.EquipsToRole': raise GameError('离线模拟断线')
            return original(method, data)
        self.client.call = fail_wear
        result = self.handle(roleId='r0', planIndex=1)
        self.assertFalse(result['verified'])
        self.assertIn('模拟断线', result['error'])
        role = next(role for role in result['overview']['roles'] if role['id'] == 'r0')
        self.assertEqual(role['current']['count'], 6)
        self.assertIsNone(self.acc.equipment_batch)


if __name__ == '__main__': unittest.main()
