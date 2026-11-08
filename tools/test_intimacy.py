"""Offline friendship queue, carrier fitting, transactional assets and workers."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
import httpx
from equipment import plans
from unittest.mock import Mock, patch

from backend import intimacy, abyss_loadout, abyss_team, abyss_battle, appcore, config
from backend.game_client import GameError
from backend.tasks import _oid
from backend.tasks import task_abyss_auto
from tools.test_abyss_auto import AssetServer, gear_fixture, roster


def farm_state():
    state, _ = gear_fixture()
    sids = ['H002', 'H006', 'H026', 'H027', 'H028', 'H029', 'H030', 'H031', 'H018', 'H076']
    for role, sid in zip(state['RoleDataContainer']['Roles'], sids):
        role['StaticID'] = sid
    state['LineRoleDataContainer'] = {'RoleList': [
        {'Info': {'StaticID': sid, 'IntimacyLV': 10 if i < 2 else 3, 'Intimacy': 1,
                  'IntimacyToNextLevel': 80}} for i, sid in enumerate(sids)]}
    state['SceneDataContainer'] = {'Scenes': [{'StaticID': intimacy.SCENE_ID, 'Stars': [1, 1, 1, 1]}]}
    state['ActivityDBSettingContainer'] = roster()['ActivityDBSettingContainer']
    state['Artifacts'] = {'Datas': [
        {'_id': 'a0', 'StaticID': 'A0008', 'LV': 30, 'EquipRole': 'r3'},
        {'_id': 'a1', 'StaticID': 'A0004', 'LV': 30, 'EquipRole': 'r8'},
        {'_id': 'a2', 'StaticID': 'A0009', 'LV': 1, 'EquipRole': 'r0'},
        {'_id': 'a3', 'StaticID': 'A0005', 'LV': 1, 'EquipRole': 'r1'}]}
    for i in range(8):
        state['Artifacts']['Datas'].append({'_id': f'universal-{i}', 'StaticID': 'A0038', 'LV': 30, 'EquipRole': ''})
    for item in list(state['EquipmentContainer']['Datas']):
        if not _oid(item['_id']).endswith('-8'):
            continue
        for i in range(12):
            extra = deepcopy(item); extra['_id'] = f'{_oid(item["_id"])}-free-{i}'; extra['EquipRole'] = ''
            if extra['StaticID'] == 'R001': extra['MainProp'] = {'PropertyType': 'AttackRate', 'Value': .65}
            state['EquipmentContainer']['Datas'].append(extra)
    return state


class QueueTests(unittest.TestCase):
    def test_native_requirements_supply_denominators_for_real_server_fields(self):
        state = {'LineRoleDataContainer': {'RoleList': [{'Info': {'StaticID': sid, 'IntimacyLV': level, 'Intimacy': exp}}
                 for sid, level, exp in [('H001', 5, 116), ('H002', 6, 153), ('H003', 7, 65)]]}}
        actual = intimacy.friendship(state)
        self.assertEqual([actual[sid]['intimacyMax'] for sid in ('H001', 'H002', 'H003')], [150, 200, 250])
        self.assertEqual(sum(intimacy.intimacy_levels().values()), 1500)

    def test_promoted_three_star_is_excluded_and_four_star_is_kept_even_at_six_stars(self):
        state = farm_state()
        state['RoleDataContainer']['Roles'][-1]['Star'] = 6
        state['RoleDataContainer']['Roles'][2]['Star'] = 6
        rows = intimacy.eligible(state)
        self.assertNotIn('H076', {row['staticId'] for row in rows})
        self.assertIn('H026', {row['staticId'] for row in rows})
        self.assertEqual(next(row for row in rows if row['staticId'] == 'H026')['initialStar'], 4)

    def test_duplicate_instances_share_line_progress_and_only_one_is_queued(self):
        state = farm_state()
        state['RoleDataContainer']['Roles'].append({'_id': 'copy', 'StaticID': 'H026', 'LV': 1,
                                                   'IntimacyLV': 0, 'Intimacy': 0})
        rows = [row for row in intimacy.eligible(state) if row['staticId'] == 'H026']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['intimacyLevel'], 3)
        self.assertEqual(rows[0]['intimacyExp'], 1)
        self.assertEqual(rows[0]['intimacyMax'], 80)

    def test_missing_progress_never_masquerades_as_zero_or_complete(self):
        state = farm_state()
        state['LineRoleDataContainer']['RoleList'][2]['Info'].pop('Intimacy')
        with self.assertRaisesRegex(GameError, '缺少服务器亲密度'):
            intimacy.eligible(state)

    def test_missing_native_definition_does_not_silently_exclude_new_heroes(self):
        state = farm_state()
        state['RoleDataContainer']['Roles'].append({'_id': 'new', 'StaticID': 'H999', 'Star': 5})
        with self.assertRaisesRegex(GameError, '初始星级'):
            intimacy.eligible(state)

    def test_each_three_person_batch_stays_until_everyone_is_full_then_only_that_half_rotates(self):
        state = farm_state(); carriers = {'upper': 'r0', 'lower': 'r1'}
        waves = intimacy.make_waves(state, carriers)
        picked = [rid for group in waves.values() for rid in group if rid]
        self.assertEqual(len(picked), 8)
        self.assertEqual(len(set(picked)), 8)
        self.assertNotIn('r9', picked)
        done = waves['upper'][1]
        role = next(r for r in state['RoleDataContainer']['Roles'] if _oid(r['_id']) == done)
        next(row['Info'] for row in state['LineRoleDataContainer']['RoleList']
             if row['Info']['StaticID'] == role['StaticID'])['IntimacyLV'] = 10
        after = intimacy.make_waves(state, carriers, waves)
        self.assertEqual(after, waves)
        for rid in waves['upper'][1:]:
            role = next(r for r in state['RoleDataContainer']['Roles'] if _oid(r['_id']) == rid)
            next(row['Info'] for row in state['LineRoleDataContainer']['RoleList']
                 if row['Info']['StaticID'] == role['StaticID'])['IntimacyLV'] = 10
        after = intimacy.make_waves(state, carriers, waves)
        self.assertNotIn(done, after['upper'] + after['lower'])
        self.assertEqual(waves['lower'], after['lower'])
        self.assertFalse(set(waves['upper'][1:]) & set(after['upper'][1:]) - {None})

    def test_last_passenger_uses_empty_slots_and_carrier_only_remainder_is_valid(self):
        state = farm_state(); carriers = {'upper': 'r0', 'lower': 'r1'}
        for row in state['LineRoleDataContainer']['RoleList']:
            if row['Info']['StaticID'] != 'H026': row['Info']['IntimacyLV'] = 10
        waves = intimacy.make_waves(state, carriers)
        self.assertEqual(sum(rid is not None for group in waves.values() for rid in group), 3)
        state['LineRoleDataContainer']['RoleList'][2]['Info']['IntimacyLV'] = 10
        waves = intimacy.make_waves(state, carriers)
        self.assertEqual(sum(rid is not None for group in waves.values() for rid in group), 2)

    def test_each_half_uses_one_highest_character_level_and_two_lowest(self):
        state=farm_state();carriers={'upper':'r0','lower':'r1'}
        levels={'r2':60,'r3':55,'r4':1,'r5':5,'r6':10,'r7':30,'r8':40,'r9':1}
        for role in state['RoleDataContainer']['Roles']:
            rid=_oid(role['_id'])
            if rid in levels:role['LV']=levels[rid]
        # Deliberately rank friendship in a different order from character Lv.
        relations={row['Info']['StaticID']:row['Info'] for row in state['LineRoleDataContainer']['RoleList']}
        relations['H028']['IntimacyLV']=9
        waves=intimacy.make_waves(state,carriers)
        passengers=lambda wave:[rid for rid in waves[wave] if rid and rid not in carriers.values()]
        self.assertEqual(passengers('upper'),['r2','r4','r5'])
        self.assertEqual(passengers('lower'),['r3','r6','r7'])
        self.assertNotIn('r9',passengers('upper')+passengers('lower'),'native three-star hero stays excluded')

    def test_level_priority_does_not_replace_an_unfinished_batch(self):
        state=farm_state();carriers={'upper':'r0','lower':'r1'}
        waves=intimacy.make_waves(state,carriers)
        used={rid for team in waves.values() for rid in team if rid}
        future=next(role for role in state['RoleDataContainer']['Roles']
                    if _oid(role['_id']) not in used and role['StaticID']!='H076')
        future['LV']=60
        for role in state['RoleDataContainer']['Roles']:
            if _oid(role['_id']) in used and _oid(role['_id']) not in carriers.values():role['LV']=1
        self.assertEqual(intimacy.make_waves(state,carriers,waves),waves)


class CarrierTests(unittest.TestCase):
    def test_boss_element_is_countered_and_failed_pair_is_not_selected_again(self):
        state = roster()
        wave = {'Monsters': [{'M': 'FH002', 'Boss': True}], 'Buffs': {}}
        with patch.object(intimacy, 'fantasy_config', return_value={'Waves': [wave, wave]}):
            first = intimacy.choose_carriers(state)
            second = intimacy.choose_carriers(state, excluded=[tuple(first[wave] for wave in intimacy.WAVES)])
        self.assertNotEqual(first, second)
        roles = {_oid(r['_id']): r for r in state['RoleDataContainer']['Roles']}
        for rid in first.values():
            self.assertEqual(intimacy.metadata()['tables']['Role'][roles[rid]['StaticID']]['Element'], 'Ice')

    def test_real_damage_roles_are_unique_and_obey_forbidden_jobs(self):
        state = roster()
        wave = {'Monsters': [{'M': 'FH002', 'Boss': True}],
                'Buffs': {'DBs': [{'B': 'FB_Job_002', 'Target': 'Self'}]}}
        with patch.object(intimacy, 'fantasy_config', return_value={'Waves': [wave, wave]}):
            chosen = intimacy.choose_carriers(state)
        roles = {_oid(r['_id']): r for r in state['RoleDataContainer']['Roles']}
        self.assertEqual(len(set(chosen.values())), 2)
        for rid in chosen.values():
            self.assertNotEqual(intimacy.metadata()['tables']['Role'][roles[rid]['StaticID']]['Job'], 'Warrior')
            self.assertIn(abyss_team.combat_role(roles[rid])['kind'], ('single', 'area'))

    def test_no_full_level_carriers_stops_before_borrowing(self):
        state = farm_state()
        for role in state['RoleDataContainer']['Roles']: role['LV'] = 59
        with self.assertRaisesRegex(GameError, 'Lv60'):
            intimacy.choose_carriers(state)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(config, 'data_dir', return_value=Path(self.temp.name))
        self.path_patch.start(); self.addCleanup(self.path_patch.stop)
        self.client = AssetServer(farm_state())
        self.before = deepcopy(self.client.server)
        self.cancel = threading.Event(); self.snapshots = []
        self.params = {'_cancel_event': self.cancel, '_on_progress': self.snapshots.append, '_account_key': 'offline'}

    def owners(self):
        return (abyss_loadout._owners(self.client.server['EquipmentContainer']['Datas']),
                abyss_loadout._owners(self.client.server['Artifacts']['Datas']))

    def run_farm(self, battle):
        with patch.object(intimacy, 'choose_carriers', return_value={'upper': 'r0', 'lower': 'r1'}), \
             patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle), \
             patch.object(config, 'get_request_delay_config', return_value={'enabled': False}), \
             patch.object(intimacy, 'RETRY_BASE_SECONDS', 0, create=True):
            return intimacy.run(self.client, self.params)

    def test_only_carriers_get_twelve_distinct_pieces_and_compatible_bonds_then_restore_all_owners(self):
        baseline = self.owners()
        plan = intimacy.plan_loadout(self.client.account_state, {'upper': 'r0', 'lower': 'r1'})
        self.assertEqual(set(plan['targets']), {'r0', 'r1'})
        self.assertEqual(len({eid for group in plan['targets'].values() for eid in group}), 12)
        self.assertEqual(len(set(plan['artifactTargets'].values())), 2)
        abyss_loadout.apply(self.client, plan)
        self.assertNotEqual(self.owners(), baseline)
        self.assertTrue(abyss_loadout.restore(self.client)['restored'])
        self.assertEqual(self.owners(), baseline)

    def test_universal_bond_is_reserved_for_carrier_without_a_job_specific_alternative(self):
        state = farm_state()
        state['Artifacts']['Datas'] = [
            {'_id': 'universal', 'StaticID': 'A0038', 'LV': 30, 'EquipRole': ''},
            {'_id': 'warrior-only', 'StaticID': 'A0008', 'LV': 1, 'EquipRole': ''}]
        result = intimacy.plan_loadout(state, {'upper': 'r0', 'lower': 'r1'})
        self.assertEqual(result['artifactTargets'], {'r0': 'warrior-only', 'r1': 'universal'})

    def test_both_passenger_batches_have_full_distinct_outfits_and_bonds_and_restore_independently(self):
        baseline = self.owners(); carriers = {'upper': 'r0', 'lower': 'r1'}
        waves = intimacy.make_waves(self.client.account_state, carriers)
        core = intimacy.plan_loadout(self.client.account_state, carriers)
        abyss_loadout.apply(self.client, core)
        active = {'carriers': core}
        for wave in intimacy.WAVES:
            plan = intimacy.plan_passengers(self.client.account_state, carriers, waves, wave, active)
            self.assertEqual(len(plan['targets']), 3)
            self.assertEqual(len(plan['artifactTargets']), 3)
            self.assertEqual(len({eid for group in plan['targets'].values() for eid in group}), 18)
            for other in active.values():
                self.assertFalse({eid for group in plan['targets'].values() for eid in group}
                                 & {eid for group in other['targets'].values() for eid in group})
                self.assertFalse(set(plan['artifactTargets'].values()) & set(other['artifactTargets'].values()))
            abyss_loadout.apply(self.client, plan, account_key=intimacy.passenger_key(self.client, 'offline', wave))
            active[wave] = plan
        self.assertEqual(len({eid for plan in active.values() for group in plan['targets'].values() for eid in group}), 48)
        protected = {eid: owner for eid, owner in self.owners()[0].items() if owner in carriers.values()}
        for wave in reversed(intimacy.WAVES):
            self.assertTrue(abyss_loadout.restore(self.client, account_key=intimacy.passenger_key(self.client, 'offline', wave))['restored'])
        after = self.owners()[0]
        self.assertTrue(all(after[eid] == owner for eid, owner in protected.items()))
        self.assertTrue(intimacy.restore_all(self.client, account_key='offline')['restored'])
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_passenger_output_shortage_falls_back_to_a_complete_survival_outfit(self):
        state = farm_state(); messages = []
        for item in state['EquipmentContainer']['Datas']:
            if item['StaticID'] == 'R001': item['MainProp'] = {'PropertyType': 'HPRate', 'Value': .65}
        value = abyss_loadout.plan(state, {'upper':['r3'],'lower':[]}, role_ids=['r3'], utility_roles={},
                                  equip_artifacts=True, survival_fallback=True, progress=messages.append)
        self.assertEqual(len(value['targets']['r3']), 6)
        self.assertTrue(value['artifactTargets']['r3'])
        self.assertTrue(any('改用完整生存装备' in row['message'] for row in messages))

    def test_partial_batch_completion_keeps_everyone_and_other_half_outfits_until_its_own_completion(self):
        baseline = self.owners(); calls = []; outfits = {}; halves = {}
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            owned = {_oid(r['_id']): r for r in client.server['RoleDataContainer']['Roles']}
            relation = {row['Info']['StaticID']: row['Info'] for row in client.server['LineRoleDataContainer']['RoleList']}
            if len(calls) == 1:
                halves.update({wave: [rid for rid in params['waves'][wave] if rid not in ('r0','r1',None)] for wave in intimacy.WAVES})
                outfits.update(self.owners()[0])
                relation[owned[halves['upper'][0]]['StaticID']]['IntimacyLV'] = 10
            elif len(calls) == 2:
                self.assertEqual(calls[0], calls[1], 'a single full passenger must not split the batch')
                for rid in halves['upper']: relation[owned[rid]['StaticID']]['IntimacyLV'] = 10
                for rid in halves['lower']: relation[owned[rid]['StaticID']]['Intimacy'] += 1
            elif len(calls) == 3:
                self.assertEqual(calls[1]['lower'], calls[2]['lower'])
                now = self.owners()[0]
                protected_roles = {'r0','r1'}
                self.assertTrue(all(now[eid] == owner for eid, owner in outfits.items() if owner in protected_roles))
                for rid in halves['lower']:
                    self.assertEqual(len(plans.worn_items(client.server['EquipmentContainer']['Datas'], rid)[0]), 6)
                self.assertFalse(set(halves['upper']) & set(params['waves']['upper']))
                for row in relation.values(): row['IntimacyLV'] = 10
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_passengers_prioritize_farming_and_can_borrow_from_a_future_queued_hero(self):
        state = farm_state(); carriers = {'upper':'r0', 'lower':'r1'}
        waves = intimacy.make_waves(state, carriers)
        main = intimacy.plan_loadout(state, carriers)
        client = AssetServer(state)
        abyss_loadout.apply(client, main)
        weapon = next(item for item in client.server['EquipmentContainer']['Datas']
                      if _oid(item.get('EquipRole')) == 'r7' and plans.equip_slot(item) == 'weapon')
        weapon['MainProp']['Value'] = 1e9
        client.bootstrap()
        value = intimacy.plan_passengers(client.account_state, carriers, waves, 'upper', {'carriers':main})
        selected = {eid for group in value['targets'].values() for eid in group}
        self.assertIn(_oid(weapon['_id']), selected)
        self.assertFalse(selected & {eid for group in main['targets'].values() for eid in group})
        self.assertTrue(abyss_loadout.restore(client)['restored'])

    def test_survival_fallback_is_unnecessary_when_complete_output_jewelry_exists(self):
        state = farm_state(); messages = []
        # H027 deals defence-scaling damage, so its output ring is defence.
        for item in state['EquipmentContainer']['Datas']:
            if item['StaticID'] == 'R001': item['MainProp'] = {'PropertyType': 'DefenceRate', 'Value': .65}
        value = abyss_loadout.plan(state, {'upper':['r3'],'lower':[]}, role_ids=['r3'], utility_roles={},
                                  equip_artifacts=True, survival_fallback=True, progress=messages.append)
        equipped = { _oid(item['_id']):item for item in state['EquipmentContainer']['Datas'] }
        ring = next(equipped[eid] for eid in value['targets']['r3'] if equipped[eid]['StaticID']=='R001')
        self.assertEqual(ring['MainProp']['PropertyType'], 'DefenceRate')
        self.assertFalse(any('生存' in row['message'] for row in messages))

    def test_fresh_settlement_friendship_skips_the_full_relogin_after_each_battle(self):
        baseline = self.owners(); armed = False; restoring = False; leaked = []
        original_bootstrap = self.client.bootstrap
        def bootstrap():
            if armed and not restoring:
                leaked.append(1)
            return original_bootstrap()
        self.client.bootstrap = bootstrap
        def battle(client, params):
            nonlocal armed
            # SceneRewardData merges LineRoleDatas into the local account state;
            # the server copy is updated only so the final asset restore matches.
            for state in (client.account_state, client.server):
                for row in state['LineRoleDataContainer']['RoleList']:
                    row['Info']['IntimacyLV'] = 10
            armed = True
            return {'ok': True, 'settlementConfirmed': True, 'intimacySynced': True}
        original_restore = intimacy.restore_all
        def restore_all(*args, **kwargs):
            nonlocal restoring
            restoring = True
            return original_restore(*args, **kwargs)
        with patch.object(intimacy, 'restore_all', side_effect=restore_all):
            result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(leaked, [])
        self.assertEqual(self.owners(), baseline)

    def test_repeated_confirmed_battles_rotate_finished_passengers_and_stop_at_ten(self):
        baseline = self.owners(); calls = []
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            owned = {_oid(r['_id']): r for r in client.server['RoleDataContainer']['Roles']}
            sids = {owned[rid]['StaticID'] for group in params['waves'].values() for rid in group if rid}
            for row in client.server['LineRoleDataContainer']['RoleList']:
                if row['Info']['StaticID'] in sids: row['Info']['IntimacyLV'] = 10
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 2)
        first = {rid for wave in calls[0].values() for rid in wave if rid} - {'r0', 'r1'}
        second = {rid for wave in calls[1].values() for rid in wave if rid} - {'r0', 'r1'}
        self.assertEqual(len(second), 1)
        self.assertFalse(first & second)
        self.assertFalse(set(calls[0]['upper'][1:]) & set(calls[1]['upper'][1:]) - {None})
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(abyss_loadout.pending(self.client))
        self.assertEqual(self.snapshots[-2]['remaining'], 0)

    def test_two_upper_defeats_switch_only_upper_and_low_star_win_keeps_the_new_mains(self):
        baseline = self.owners()
        selections = []; calls = []
        def choose(state, **kwargs):
            self.assertEqual(self.owners(), baseline, 'previous pair must be restored before trying another')
            selections.append(1)
            return {'upper': 'r0', 'lower': 'r1'}
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) <= 2:
                # A received defeat frame is sufficient to switch; loss rewards
                # need not contain a SceneData progress record.
                return {'ok': False, 'settlementConfirmed': False, 'raw': {'Result': 'Lose'},
                        'halfResults': {'upper': False, 'lower': None}}
            owned = {_oid(r['_id']): r for r in client.server['RoleDataContainer']['Roles']}
            sids = {owned[rid]['StaticID'] for group in params['waves'].values() for rid in group if rid}
            for row in client.server['LineRoleDataContainer']['RoleList']:
                if row['Info']['StaticID'] in sids: row['Info']['IntimacyLV'] = 10
            return {'ok': True, 'stars': 1, 'settlementConfirmed': True}
        with patch.object(intimacy, 'choose_carriers', side_effect=choose), \
             patch.object(intimacy, 'choose_half_carriers', return_value=['r2']), \
             patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle), \
             patch.object(config, 'get_request_delay_config', return_value={'enabled': False}), \
             patch.object(intimacy, 'RETRY_BASE_SECONDS', 0):
            result = intimacy.run(self.client, self.params)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(selections), 1)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[2]['upper'][0], 'r2')
        self.assertTrue(all(group['lower'][0] == 'r1' for group in calls))
        self.assertEqual(self.owners(), baseline)
        self.assertTrue(any(snapshot.get('carriersLocked') for snapshot in self.snapshots))

    def test_partial_equipment_application_is_still_restored_on_cancel(self):
        baseline = self.owners()
        self.client.after_call = self.cancel.set
        result = self.run_farm(lambda *args: self.fail('must not start battle'))
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(abyss_loadout.pending(self.client))

    def test_cancel_during_lower_batch_application_restores_all_three_journals(self):
        baseline = self.owners()
        def after_call():
            if all(abyss_loadout.pending(self.client, intimacy.passenger_key(self.client, 'offline', wave))
                   for wave in intimacy.WAVES): self.cancel.set()
        self.client.after_call = after_call
        result = self.run_farm(lambda *args: self.fail('must not battle after cancellation'))
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_normal_abyss_recovers_a_leftover_passenger_journal_before_starting(self):
        baseline = self.owners(); carriers = {'upper':'r0','lower':'r1'}
        waves = intimacy.make_waves(self.client.account_state, carriers)
        value = intimacy.plan_passengers(self.client.account_state, carriers, waves, 'upper', {})
        abyss_loadout.apply(self.client, value, account_key=intimacy.passenger_key(self.client, 'offline', 'upper'))
        self.assertTrue(intimacy.recovery_pending(self.client, 'offline'))
        self.assertFalse(abyss_loadout.pending(self.client))
        def battle(client, params):
            self.assertEqual(self.owners(), baseline)
            self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))
            return {'ok':True}
        with patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle):
            self.assertTrue(task_abyss_auto(self.client, {'_account_key':'offline','sceneId':intimacy.SCENE_ID,'waves':waves})['ok'])

    def test_cancellation_inside_battle_restores_assets_despite_set_cancel_signal(self):
        baseline = self.owners()
        def battle(client, params):
            self.cancel.set(); raise GameError('战斗已中止')
        result = self.run_farm(battle)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['ok'])
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(abyss_loadout.pending(self.client))

    def test_unconfirmed_win_rechecks_and_retries_without_claiming_a_completed_round(self):
        calls = []
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) == 4:
                self.cancel.set()
            return {'ok': True, 'settlementConfirmed': False}
        result = self.run_farm(battle)
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(calls), 4)
        self.assertEqual(result['rounds'], 0)
        self.assertTrue(all(group == calls[0] for group in calls))

    def test_delayed_friendship_growth_keeps_the_same_batch_and_continues_past_three_rounds(self):
        calls = []
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) == 5:
                for row in client.server['LineRoleDataContainer']['RoleList']:
                    row['Info']['IntimacyLV'] = 10
            if len(calls) > 6:
                self.cancel.set()
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(group == calls[0] for group in calls))
        self.assertFalse(abyss_loadout.pending(self.client))

    def test_native_per_level_progress_runs_120_battles_and_only_rotates_at_ten(self):
        baseline = self.owners()
        for row in self.client.server['LineRoleDataContainer']['RoleList'][2:]:
            row['Info'].update(IntimacyLV=1, Intimacy=0)
            row['Info'].pop('IntimacyToNextLevel', None)
        calls = []
        levels = intimacy.intimacy_levels()
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) > 121:
                self.cancel.set()
            ids = {rid for group in params['waves'].values() for rid in group if rid}
            sids = {r['StaticID'] for r in client.server['RoleDataContainer']['Roles'] if _oid(r['_id']) in ids}
            for row in client.server['LineRoleDataContainer']['RoleList']:
                info = row['Info']
                if info['StaticID'] not in sids or info['IntimacyLV'] >= 10:
                    continue
                info['Intimacy'] += 25
                while info['IntimacyLV'] < 10 and info['Intimacy'] >= levels[info['IntimacyLV']]:
                    info['Intimacy'] -= levels[info['IntimacyLV']]
                    info['IntimacyLV'] += 1
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 120)
        self.assertTrue(all(group == calls[0] for group in calls[:60]))
        self.assertTrue(all(group == calls[60] for group in calls[60:]))
        self.assertNotEqual(calls[0], calls[60])
        self.assertEqual(self.owners(), baseline)

    def test_temporary_lost_battle_connection_verifies_progress_and_keeps_carriers(self):
        calls = []
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) > 3:
                self.cancel.set()
            for row in client.server['LineRoleDataContainer']['RoleList'][2:]:
                row['Info']['IntimacyLV'] = 4 if len(calls) == 1 else 10
                row['Info']['Intimacy'] = 0
            if len(calls) == 1:
                raise abyss_battle.BattleUnconfirmed('战斗连接在结算确认前断开')
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(result['rounds'], 2)

    def test_snapshot_network_timeout_retries_without_reequipping_or_stopping(self):
        original = self.client.bootstrap
        calls = []
        def bootstrap():
            calls.append(1)
            if len(calls) in (1, 3):
                raise httpx.ReadTimeout('temporary timeout')
            return original()
        def battle(client, params):
            for row in client.server['LineRoleDataContainer']['RoleList']:
                row['Info']['IntimacyLV'] = 10
            return {'ok': True, 'settlementConfirmed': True}
        with patch.object(self.client, 'bootstrap', side_effect=bootstrap):
            result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertGreaterEqual(len(calls), 4)

    def test_already_complete_never_selects_carriers_or_borrows(self):
        for row in self.client.server['LineRoleDataContainer']['RoleList']: row['Info']['IntimacyLV'] = 10
        with patch.object(intimacy, 'choose_carriers') as choose:
            result = intimacy.run(self.client, self.params)
        self.assertTrue(result['ok']); choose.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_restore_sync_failure_preserves_loans_until_server_state_can_be_read(self):
        def battle(client, params):
            client.fail_bootstrap = True; raise GameError('连接中断')
        result = self.run_farm(battle)
        self.assertTrue(result.get('recoveryRequired'))
        self.assertFalse(result['ok'])
        self.assertTrue(abyss_loadout.pending(self.client))
        self.client.fail_bootstrap=False
        self.assertTrue(intimacy.recover_before_start(self.client,account_key='offline')['restored'])
        self.assertFalse(abyss_loadout.pending(self.client))


class WorkerTests(unittest.TestCase):
    def account(self, email):
        return SimpleNamespace(email=email, lock=threading.Lock(), client=SimpleNamespace(),
                               note_inventory_changed=Mock(), push_log=Mock())

    def test_toggles_status_endpoint_reads_cache_while_a_game_task_holds_the_lock(self):
        from backend.server import _route_api
        acc=self.account('settings-offline')
        acc.toggles={'guild_auto_signin':True};acc.params={}
        acc.lock.acquire()
        try:
            with patch.object(appcore,'_require',return_value=acc) as require, \
                 patch.object(config,'load_accounts',side_effect=AssertionError('must not read settings from disk')):
                result=_route_api('GET','/api/toggles',{}, {'account':acc.email})
                self.assertEqual(result['toggles'],{'guild_auto_signin':True})
                require.assert_called_once_with(acc.email,allow_disconnected=True)
        finally:acc.lock.release()

    def test_cancel_is_responsive_and_shared_session_blocks_another_start(self):
        acc = self.account('intimacy-offline'); entered = threading.Event(); finished = threading.Event()
        def work(client, params):
            entered.set()
            try:
                self.assertTrue(params['_cancel_event'].wait(3))
                return {'ok': False, 'detail': '已中止并归还'}
            finally: finished.set()
        with patch.object(appcore, '_require', return_value=acc), patch.object(intimacy, 'run', side_effect=work):
            started = appcore.intimacy_start(acc.email)
            self.assertTrue(started['intimacy']['running']); self.assertTrue(entered.wait(1))
            self.assertTrue(appcore._abyss_busy(acc.email))
            self.assertFalse(appcore._abyss_snapshot(acc.email)['running'])
            with self.assertRaises(appcore.ApiError): appcore.intimacy_start(acc.email)
            self.assertTrue(appcore.intimacy_cancel(acc.email)['ok'])
            self.assertTrue(finished.wait(2))
            self.assertTrue(acc.intimacy_done_event.wait(2))
            self.assertTrue(acc.lock.acquire(timeout=2)); acc.lock.release()
            self.assertFalse(appcore.intimacy_snapshot(acc.email)['running'])
            self.assertIn('已中止并归还', appcore.intimacy_snapshot(acc.email)['progress']['message'])

    def test_two_running_accounts_publish_independent_nonblocking_revisions(self):
        accounts = {name: self.account(name) for name in ('intimacy-alpha', 'intimacy-beta')}
        entered = {name: threading.Event() for name in accounts}
        callbacks, stops = {}, {}
        for name, acc in accounts.items():
            acc.client.name = name

        def work(client, params):
            name = client.name
            callbacks[name] = params['_on_progress']
            stops[name] = params['_cancel_event']
            callbacks[name]({'phase': 'battle', 'teams': {'upper': [
                {'staticId': 'H026' if name.endswith('alpha') else 'H032', 'name': name}]}})
            entered[name].set()
            params['_cancel_event'].wait(3)
            return {'ok': False, 'detail': '已中止并归还'}

        with patch.object(appcore, '_require', side_effect=lambda name, **_: accounts[name]), \
                patch.object(intimacy, 'run', side_effect=work):
            try:
                for name in accounts:
                    appcore.intimacy_start(name)
                    self.assertTrue(entered[name].wait(1))
                    self.assertTrue(accounts[name].lock.acquire(blocking=False))
                    accounts[name].lock.release()
                alpha = appcore.intimacy_snapshot('intimacy-alpha')
                beta = appcore.intimacy_snapshot('intimacy-beta')
                self.assertTrue(alpha['running'] and beta['running'])
                self.assertEqual(alpha['progress']['teams']['upper'][0]['staticId'], 'H026')
                self.assertEqual(beta['progress']['teams']['upper'][0]['staticId'], 'H032')
                callbacks['intimacy-alpha']({'phase': 'progress', 'teams': {}})
                self.assertGreater(appcore.intimacy_snapshot('intimacy-alpha')['revision'], alpha['revision'])
                self.assertEqual(appcore.intimacy_snapshot('intimacy-beta')['revision'], beta['revision'])
                appcore.intimacy_cancel('intimacy-alpha')
                self.assertTrue(accounts['intimacy-alpha'].intimacy_done_event.wait(2))
                self.assertTrue(accounts['intimacy-alpha'].lock.acquire(timeout=2))
                accounts['intimacy-alpha'].lock.release()
                self.assertFalse(appcore.intimacy_snapshot('intimacy-alpha')['running'])
                self.assertTrue(appcore.intimacy_snapshot('intimacy-beta')['running'])
            finally:
                for event in stops.values():
                    event.set()
                for acc in accounts.values():
                    if acc.lock.acquire(timeout=2):
                        acc.lock.release()


if __name__ == '__main__':
    unittest.main()
