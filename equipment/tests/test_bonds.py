"""羁绊面板、完整筛选与更换确认。协议仅以内存服务模拟。"""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.game_client import GameError
from equipment import api, auto, bonds, plans, stats
from equipment.tests.test_auto import fixture, config


def artifact(sid, owner='r0', aid='a0', level=30):
    return {'_id': {'$oid': aid}, 'StaticID': sid, 'LV': level, 'EquipRole': owner}


class BondPanelTests(unittest.TestCase):
    def test_base_interpolation_keeps_existing_truncation_contract(self):
        # 原生 float32 比率会将 394 算成 393.999...，不能更换羁绊后少 1 生命。
        mods, _ = bonds.permanent_mods(artifact('A0042', level=15))
        self.assertEqual(mods['HPValue'], 394)

    def test_critical_bonus_and_actual_level_enhancement(self):
        for level, critical in [(1, .07), (15, .105), (30, .14)]:
            with self.subTest(level=level):
                mods, omitted = bonds.permanent_mods(artifact('A0302', level=level))
                self.assertAlmostEqual(mods['CriticalRate'], critical, places=6)
                self.assertTrue(omitted, '战斗回血不当作常驻面板')
        self.assertAlmostEqual(bonds.permanent_mods(artifact('A0240'))[0]['CriticalRate'], .15)

    def test_conditional_and_team_bonuses_are_excluded(self):
        mods, omitted = bonds.permanent_mods(artifact('A0377'))
        self.assertTrue(omitted)
        self.assertNotIn('AttackFinalRate', mods, '队伍光环不计')
        self.assertAlmostEqual(mods['EffectHitRate'], .30, places=6)
        self.assertAlmostEqual(mods['CriticalRate'], .10)
        conditional, omitted = bonds.permanent_mods(artifact('A0025'))
        self.assertTrue(omitted)
        self.assertNotIn('SpeedFinalRate', conditional)
        self.assertNotIn('AttackFinalRate', conditional)

    def test_critical_bonus_changes_feasibility_and_matches_displayed_panel(self):
        state, roles = fixture(1)
        opts = dict(critical_target=65, include_worn=True, complete=True)
        before = auto.search(state, roles['r0'], **opts)
        self.assertEqual(before['results'], [])
        state['Artifacts'] = {'Datas': [artifact('A0347')]}
        after = auto.search(state, roles['r0'], **opts)
        self.assertTrue(after['results'])
        for combo in after['results']:
            gear = [e for e in state['EquipmentContainer']['Datas'] if plans._oid(e['_id']) in combo['equipIds'].values()]
            expected = stats.role_panel(state, roles['r0'], gear, include_artifact_effects=True)
            self.assertEqual(combo['panel'], expected['panel'])
            self.assertGreaterEqual(combo['panel']['CriticalRate'], 65)
        state['Artifacts']['Datas'][0]['EquipRole'] = ''
        self.assertEqual(auto.search(state, roles['r0'], **opts)['results'], [])

    def test_final_multipliers_scale_gear_and_sets_consistently_with_bounds(self):
        for sid in ('A0131', 'A0320', 'A0011'):
            with self.subTest(sid=sid):
                state, roles = fixture(2)
                state['Artifacts'] = {'Datas': [artifact(sid)]}
                # 此处只验证数值算法；职业兼容在穿戴 API 单独验证。
                result = auto.search(state, roles['r0'], critical_target=0, objective='survival', include_worn=True, complete=True)
                self.assertTrue(result['results'])
                for combo in result['results']:
                    gear = [e for e in state['EquipmentContainer']['Datas'] if plans._oid(e['_id']) in combo['equipIds'].values()]
                    expected = stats.role_panel(state, roles['r0'], gear, include_artifact_effects=True)['panel']
                    self.assertEqual(combo['panel'], expected)
                wanted = result['results'][0]['panel']['Defence']
                constrained = auto.search(state, roles['r0'], critical_target=0, objective='survival', include_worn=True, complete=True,
                                          conditions=[{'stat': 'Defence', 'min': wanted, 'max': wanted}])
                self.assertTrue(constrained['results'], '上界剪枝不能漏掉含羁绊最终倍率的可行装备')
                self.assertTrue(all(c['panel']['Defence'] == wanted for c in constrained['results']))

    def test_overview_exposes_owned_catalog_and_effective_current_panel(self):
        state, roles = fixture(1)
        state['Artifacts'] = {'Datas': [artifact('A0347'), artifact('A0131', '', 'a1')]}
        acc = SimpleNamespace(client=SimpleNamespace(account_state=state), lock=threading.Lock())
        value = api.handle(acc, 'overview', {}, lambda sid: None)
        role = next(r for r in value['roles'] if r['id'] == 'r0')
        self.assertEqual(role['job'], 'Defender')
        self.assertEqual(role['artifact']['id'], 'a0')
        self.assertEqual(role['artifact']['name'], '酸苦的过往')
        self.assertIn('暴击率 +15%', role['artifact']['bonuses'])
        self.assertEqual(len(value['artifacts']), 2)
        gear, _ = plans.worn_items(state['EquipmentContainer']['Datas'], 'r0')
        raw = stats.role_panel(state, roles['r0'], gear)['panel']
        self.assertEqual(role['current']['panel']['CriticalRate'], raw['CriticalRate'] + 15)

    def test_missing_metadata_and_duplicate_owner_block_incomplete_filtering(self):
        state, roles = fixture(1)
        for inventory in ([artifact('unknown')], [artifact('A0131'), artifact('A0347', aid='a1')]):
            state['Artifacts'] = {'Datas': inventory}
            with self.assertRaises(GameError):
                auto.search(state, roles['r0'], complete=True)
            acc = SimpleNamespace(client=SimpleNamespace(account_state=state), lock=threading.Lock())
            role = api.handle(acc, 'overview', {}, lambda sid: None)['roles'][0]
            self.assertIn('currentWarning', role)
            self.assertNotIn('current', role)


class BondEquipTests(unittest.TestCase):
    def setUp(self):
        state, self.roles = fixture(1)
        state['Artifacts'] = {'Datas': [artifact('A0131'), artifact('A0347', 'r1', 'a1'), artifact('A0240', '', 'a2')]}
        self.client = SimpleNamespace(account_state=state, _auth_data=lambda data: data, call=Mock(), bootstrap=Mock())
        self.acc = SimpleNamespace(client=self.client, lock=threading.Lock(), equipment_batch={'old': True}, note_state_refreshed=Mock())

    def handle(self, aid='a1', rid='r0'):
        return api.handle(self.acc, 'setArtifact', {'roleId': rid, 'artifactId': aid}, lambda sid: None)

    def response(self, method, data):
        self.assertIn(method, ['ArtifactHandler.Equip', 'ArtifactHandler.UnEquip'])
        target = copy.deepcopy(next(a for a in bonds.inventory(self.client.account_state) if plans._oid(a['_id']) == data['ArtifactID']))
        old = copy.deepcopy(bonds.worn(self.client.account_state, data['RoleID'])[0])
        old['EquipRole'] = ''
        target['EquipRole'] = data['RoleID'] if method.endswith('.Equip') else ''
        return {'NewArtifact': target, 'OldInRoleArtifact': old}

    def test_transfer_updates_both_roles_and_invalidates_batch_without_extra_mutations(self):
        self.client.call.side_effect = self.response
        result = self.handle()
        self.assertTrue(result['verified'])
        self.client.call.assert_called_once_with('ArtifactHandler.Equip', {'RoleID': 'r0', 'ArtifactID': 'a1'})
        self.client.bootstrap.assert_not_called()
        self.assertIsNone(self.acc.equipment_batch)
        r0, r1 = result['overview']['roles'][:2]
        self.assertEqual(r0['artifact']['id'], 'a1')
        self.assertIsNone(r1['artifact'])
        self.assertGreater(r0['current']['panel']['CriticalRate'], r1['current']['panel']['CriticalRate'])

    def test_same_bond_is_a_noop_and_unequip_verifies(self):
        self.assertTrue(self.handle('a0')['verified'])
        self.client.call.assert_not_called()
        self.client.call.return_value = {'Artifact': artifact('A0131', '')}
        result = self.handle('')
        self.assertTrue(result['verified'])
        self.assertIsNone(result['overview']['roles'][0]['artifact'])
        self.client.call.assert_called_once_with('ArtifactHandler.UnEquip', {'RoleID': 'r0', 'ArtifactID': 'a0'})

    def test_invalid_unowned_and_incompatible_ids_never_send_mutations(self):
        for aid, rid in [('missing', 'r0'), ('a2', 'r0'), ([], 'r0'), ('a1', 'missing')]:
            with self.subTest(aid=aid):
                self.assertFalse(self.handle(aid, rid)['verified'])
        self.client.call.assert_not_called()
        self.client.bootstrap.assert_not_called()

    def test_incomplete_response_readback_confirms_only_actual_ownership(self):
        self.client.call.return_value = {}
        result = self.handle()
        self.assertFalse(result['verified'])
        self.client.bootstrap.assert_called_once()
        self.assertEqual(result['overview']['roles'][0]['artifact']['id'], 'a0')

    def test_network_timeout_readback_does_not_duplicate_mutation(self):
        self.client.call.side_effect = GameError('模拟响应丢失')
        def readback():
            for a in bonds.inventory(self.client.account_state):
                a['EquipRole'] = 'r0' if plans._oid(a['_id']) == 'a1' else ''
        self.client.bootstrap.side_effect = readback
        self.assertTrue(self.handle()['verified'])
        self.client.call.assert_called_once()
        self.client.bootstrap.assert_called_once()

    def test_failed_readback_blocks_filtering_until_reload(self):
        self.client.call.return_value = {}
        self.client.bootstrap.side_effect = GameError('模拟断线')
        result = self.handle()
        self.assertFalse(result['verified'])
        self.assertTrue(result['overview']['artifactUncertain'])
        with self.assertRaisesRegex(GameError, '状态尚未同步'):
            api.handle(self.acc, 'autoMulti', {'entries': [config('r0')]}, lambda sid: None)
        self.client.bootstrap.side_effect = None
        value = api.handle(self.acc, 'reload', {}, lambda sid: None)
        self.assertFalse(value['artifactUncertain'])


if __name__ == '__main__':
    unittest.main()
