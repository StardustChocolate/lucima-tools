"""角色推荐：独立穷举、属性方向、资产保护与旧条件模式隔离。"""
import copy
import itertools
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.abyss_state import metadata
from backend.game_client import GameError
from equipment import api, auto, bonds, plans, recommend, stats
from equipment.tests.test_auto import fixture, config
from equipment.tests.test_bonds import artifact


class RecommendationTests(unittest.TestCase):
    def test_document_profiles_and_live_roles_have_valid_distinct_directions(self):
        data = recommend.catalog()
        self.assertEqual(len(data['roles']), 10)
        self.assertGreaterEqual(sum(map(len, data['roles'].values())), 20)
        self.assertEqual(len(data['sourceSha256']), 64)
        for sid in stats.tables()['roles']:
            with self.subTest(sid=sid):
                rows = recommend.profiles({'StaticID': sid})
                self.assertTrue(rows)
                self.assertEqual(len({p['id'] for p in rows}), len(rows))
                for row in rows:
                    self.assertIn(row['damageStat'], ('HP', 'Defence', 'Attack'))
                    self.assertTrue(set(row['priorities']) <= {*recommend.SCALES, 'damage'})
                    self.assertIn('scope', row)

    def test_defence_damage_and_no_critical_are_not_generic_attack_score(self):
        panel = dict(HP=10000, Defence=1000, Attack=1000, Speed=200, EffectHitRate=100,
                     ResistanceRate=100, CriticalRate=50, CriticalDamageRate=200)
        defence = recommend.profiles({'StaticID': 'H192'})[0]
        self.assertEqual(defence['damageStat'], 'Defence')
        self.assertEqual(recommend.metric(panel, defence), recommend.metric({**panel, 'Attack': 9000}, defence))
        self.assertGreater(recommend.metric({**panel, 'Defence': 1200}, defence), recommend.metric(panel, defence))
        for profile in recommend.profiles({'StaticID': 'H189'}):
            self.assertFalse(profile['critical'])
            self.assertEqual(recommend.metric(panel, profile), recommend.metric({**panel, 'CriticalRate': 100, 'CriticalDamageRate': 350}, profile))
        sample = dict(id='probe', priorities=['damage'], damageStat='Attack', critical=True)
        # 0 暴击 = 1 倍，50% 暴击、200% 暴伤 = 1.5 倍，不能变成必暴。
        self.assertAlmostEqual(recommend.metric(panel, sample) / recommend.metric({**panel, 'CriticalRate': 0}, sample), 1.5)

    def test_mochizuki_directions_are_explicit_and_fast_crit_is_a_hard_rule(self):
        profiles = {row['id']: row for row in recommend.profiles({'StaticID': 'H182'})}
        self.assertEqual(profiles['finisher']['title'], '输出优先')
        self.assertEqual(profiles['speed-pressure']['title'], '速度＋输出')
        fast = profiles['speed-crit']
        self.assertEqual(fast['title'], '最高速度＋百暴')
        self.assertEqual(fast['ranking'], 'speed-first')
        self.assertEqual(fast['hardConditions'], [{'stat': 'CriticalRate', 'min': 100, 'max': None}])
        self.assertEqual(fast['metricLabel'], '速度优先')

    def test_fast_crit_matches_independent_oracle_and_never_trades_speed_for_damage(self):
        state, roles = fixture(3)
        role = roles['r0']
        role['StaticID'] = 'H182'
        for index, item in enumerate(state['EquipmentContainer']['Datas']):
            choice = index % 3
            item.update(Set='', EquipRole='', SubProps={'SourceValues': [
                {'PropertyType': 'SpeedValue', 'Value': 10-choice},
                {'PropertyType': 'CriticalRate', 'Value': (.16, .18, .04)[choice]},
                {'PropertyType': 'AttackValue', 'Value': (0, 30000, 100000)[choice]},
            ]})
            if plans.equip_slot(item) == 'boots':
                item['MainProp']['Value'] = 70-choice
        before = copy.deepcopy(state)
        result = recommend.search(state, role, profile_id='speed-crit', include_worn=True)
        rows = [row for row in result['results'] if row['profileId'] == 'speed-crit']
        choices = [[item for item in state['EquipmentContainer']['Datas'] if plans.equip_slot(item) == slot] for slot in plans.SLOTS]
        panels = [stats.role_panel(state, role, list(items), include_artifact_effects=True)['panel'] for items in itertools.product(*choices)]
        # 独立参考：先过百暴，再比较速度、实际有效攻击、生命。
        key = lambda panel: (panel['Speed'], panel['Attack'] * min(350, panel['CriticalDamageRate']) / 100, panel['HP'])
        valid = sorted((panel for panel in panels if panel['CriticalRate'] >= 100), key=key, reverse=True)
        self.assertEqual([key(row['panel']) for row in rows], [key(panel) for panel in valid[:recommend.RESULT_LIMIT]])
        self.assertGreater(max(panel['Attack'] for panel in panels if panel['Speed'] < rows[0]['panel']['Speed']), rows[0]['panel']['Attack'])
        self.assertTrue(all(row['panel']['CriticalRate'] >= 100 for row in rows))
        self.assertEqual(rows[0]['advisorScore'], rows[0]['panel']['Speed'])
        other = [row for row in result['results'] if row['profileId'] != 'speed-crit']
        self.assertTrue(any(row['panel']['CriticalRate'] < 100 for row in other), '百暴限制只作用于抢速用途')
        self.assertEqual(state, before)

    def test_fast_crit_conflict_does_not_silently_choose_another_direction(self):
        state, roles = fixture(2)
        roles['r0']['StaticID'] = 'H182'
        result = recommend.search(state, roles['r0'], profile_id='speed-crit', include_worn=True,
                                  conditions=[{'stat': 'CriticalRate', 'max': 90}])
        self.assertEqual(result['results'], [])
        self.assertEqual(result['profileCounts']['speed-crit'], 0)
        self.assertGreater(result['profileCounts']['finisher'], 0)
        self.assertIn('冲突', result['unavailableReason'])

    def test_fast_crit_beam_keeps_eligible_current_speed_as_baseline(self):
        state, roles = fixture(7)
        role = roles['r0']
        role['StaticID'] = 'H182'
        for index, item in enumerate(state['EquipmentContainer']['Datas']):
            choice = index % 7
            item.update(Set='', EquipRole='r0' if choice == 6 else '', SubProps={'SourceValues': [
                {'PropertyType': 'SpeedValue', 'Value': 25-choice},
                {'PropertyType': 'CriticalRate', 'Value': .15 if choice == 6 else 0},
            ]})
        worn, _ = plans.worn_items(state['EquipmentContainer']['Datas'], 'r0')
        current = stats.role_panel(state, role, worn, include_artifact_effects=True)['panel']
        self.assertGreaterEqual(current['CriticalRate'], 100)
        with patch.object(recommend, 'EXACT_LIMIT', 1), patch.dict(recommend.BUDGETS, {'standard': (1, 1)}):
            result = recommend.search(state, role, profile_id='speed-crit', include_worn=True)
        rows = [row for row in result['results'] if row['profileId'] == 'speed-crit']
        self.assertFalse(result['exhaustive'])
        self.assertTrue(rows)
        self.assertGreaterEqual(rows[0]['panel']['Speed'], current['Speed'])
        self.assertTrue(all(row['panel']['CriticalRate'] >= 100 for row in rows))

    def test_fast_crit_uses_current_bond_and_set_bonus_even_in_beam_bounds(self):
        state, roles = fixture(1)
        role = roles['r0']
        role['StaticID'] = 'H182'
        raw = stats.panel(stats.role_base(role), stats.role_mods(state, role, include_artifact_effects=True))
        for item in state['EquipmentContainer']['Datas']:
            item.update(Set='', SubProps={'SourceValues': [{'PropertyType': 'CriticalRate', 'Value': (.95-raw['CriticalRate'])/6}]})
        before = recommend.search(state, role, profile_id='speed-crit', include_worn=True)
        self.assertEqual(before['profileCounts']['speed-crit'], 0)
        state['Artifacts'] = {'Datas': [artifact('A0377')]}
        saved = copy.deepcopy(state)
        with patch.object(recommend, 'EXACT_LIMIT', 0):
            after = recommend.search(state, role, profile_id='speed-crit', include_worn=True)
        self.assertGreater(after['profileCounts']['speed-crit'], 0)
        self.assertGreaterEqual(after['results'][0]['panel']['CriticalRate'], 100)
        self.assertEqual(state, saved)
        state.pop('Artifacts')
        for item in state['EquipmentContainer']['Datas']:
            item['Set'] = 'Critical'
        with patch.object(recommend, 'EXACT_LIMIT', 0):
            with_set = recommend.search(state, role, profile_id='speed-crit', include_worn=True)
        self.assertGreater(with_set['profileCounts']['speed-crit'], 0, '套装暴击加成也必须计入可行性上界')

    def test_exact_search_matches_independent_cartesian_reference_at_three_sizes(self):
        for size in (1, 2, 3):
            with self.subTest(size=size):
                state, roles = fixture(size)
                role = roles['r0']
                state['Artifacts'] = {'Datas': [artifact('A0347')]}
                before = copy.deepcopy(state)
                result = recommend.search(state, role, include_worn=True)
                choices = [[item for item in state['EquipmentContainer']['Datas'] if plans.equip_slot(item) == slot] for slot in plans.SLOTS]
                all_panels = [stats.role_panel(state, role, list(items), include_artifact_effects=True)['panel'] for items in itertools.product(*choices)]
                self.assertTrue(result['exhaustive'])
                for profile in result['profiles']:
                    rows = [row for row in result['results'] if row['profileId'] == profile['id']]
                    minimums = {rule['stat']: rule['min'] for rule in profile.get('hardConditions', []) if rule.get('min') is not None}
                    if profile.get('hitTarget'):
                        minimums['EffectHitRate'] = max(100, minimums.get('EffectHitRate', 0))
                    valid = [panel for panel in all_panels if all(panel[stat] >= minimum for stat, minimum in minimums.items())]
                    if not valid:
                        self.assertEqual(rows, [])
                        continue
                    if profile.get('ranking') in ('speed-first', 'hit-first'):
                        best = max(panel['Speed' if profile['ranking'] == 'speed-first' else 'EffectHitRate'] for panel in valid)
                    else:
                        best = max(recommend.metric(panel, profile) for panel in valid)
                    self.assertAlmostEqual(rows[0]['advisorScore'], best, places=2)
                    self.assertEqual(len(rows), min(len(valid), recommend.RESULT_LIMIT))
                    self.assertTrue(all(len(set(row['equipIds'].values())) == 6 for row in rows))
                self.assertEqual(state, before, '推荐不改变账号装备或羁绊归属')

    def test_same_speed_retains_different_attributes_and_asset_instances(self):
        state, roles = fixture(3)
        for item in state['EquipmentContainer']['Datas']:
            if plans.equip_slot(item) == 'boots':
                item['MainProp']['Value'] = 20
        result = recommend.search(state, roles['r0'], include_worn=True)
        rows = [r for r in result['results'] if r['profileId'] == result['profiles'][0]['id']]
        self.assertEqual(len(rows), recommend.RESULT_LIMIT)
        grouped = {}
        for row in rows:
            grouped.setdefault(row['panel']['Speed'], []).append(row)
        self.assertTrue(any(len(group)>1 and len({tuple(r['equipIds'].values()) for r in group})>1 for group in grouped.values()))

    def test_hidden_role_exclusions_are_dormant_when_other_worn_disabled(self):
        state, roles = fixture(2)
        configs = [{**config('r0'), 'excludedRoleIds': ['r1']}, {**config('r1'), 'excludedRoleIds': []}]
        for mode in ('conditions', 'advisor'):
            entries = [{**c, 'mode': mode} for c in configs]
            a = auto.search_multi(state, roles, entries, include_worn=False, complete=True)
            clean = copy.deepcopy(entries)
            clean[0]['excludedRoleIds'] = []
            b = auto.search_multi(state, roles, clean, include_worn=False, complete=True)
            self.assertEqual(a[0]['results'], b[0]['results'])

    def test_protected_and_reserved_assets_never_enter_recommendation(self):
        state, roles = fixture(3)
        result = recommend.search(state, roles['r0'], include_worn=True, excluded_roles=['r1'], exclude=['e0-2'])
        owners = {plans._oid(e['_id']): plans._oid(e['EquipRole']) for e in state['EquipmentContainer']['Datas']}
        self.assertTrue(result['results'])
        for row in result['results']:
            self.assertNotIn('e0-2', row['equipIds'].values())
            self.assertNotIn('r1', [owners[eid] for eid in row['equipIds'].values()])

    def test_hard_ranges_and_repeated_sets_are_not_relaxed(self):
        state, roles = fixture(3)
        for item in state['EquipmentContainer']['Datas']:
            item['Set'] = 'Health'
        result = recommend.search(state, roles['r0'], include_worn=True, sets=['Health', 'Health'],
                                  conditions=[{'stat': 'Speed', 'min': 130, 'max': 132}])
        self.assertTrue(result['results'])
        self.assertTrue(all(130<=r['panel']['Speed']<=132 and len(r['sets'])==3 for r in result['results']))
        impossible = recommend.search(state, roles['r0'], include_worn=True, conditions=[{'stat': 'Speed', 'min': 9999}])
        self.assertEqual(impossible['results'], [])
        self.assertTrue(impossible['exhaustive'])

    def test_large_inventory_is_bounded_deterministic_and_reports_incomplete(self):
        state, roles = fixture(7)
        with patch.object(recommend, 'EXACT_LIMIT', 1), patch.dict(recommend.BUDGETS, {'standard': (20, 5)}):
            result = recommend.search(state, roles['r0'], include_worn=True)
            repeated = recommend.search(state, roles['r0'], include_worn=True)
        self.assertFalse(result['exhaustive'])
        self.assertTrue(result['report']['prefilterApplied'])
        self.assertTrue(result['results'])
        self.assertEqual(result, repeated)
        self.assertLess(result['report']['nodes'], 3000)
        self.assertTrue(all(len(r['equipIds'])==6 for r in result['results']))

    def test_cancel_and_invalid_options_never_produce_partial_results(self):
        state, roles = fixture(3)
        with self.assertRaises(auto.SearchCancelled):
            recommend.search(state, roles['r0'], cancelled=lambda: True)
        signal = threading.Event()
        def report(progress):
            if progress['nodes']:
                signal.set()
        with self.assertRaises(auto.SearchCancelled):
            recommend.search(state, roles['r0'], progress=report, cancelled=signal.is_set)
        for options in ({'profile_id': 'forged'}, {'depth': 'unlimited'}, {'conditions': [{'stat': 'unknown'}]}, {'sets': ['unknown']}):
            with self.subTest(options=options), self.assertRaises(auto.BuildError):
                recommend.search(state, roles['r0'], **options)

    def test_self_cleanse_does_not_reclassify_damage_role_as_healer(self):
        rows = recommend.profiles({'StaticID': 'H130'})
        self.assertIn('output', {profile['id'] for profile in rows})
        self.assertIn('control', {profile['id'] for profile in rows})

    def test_mixed_batch_is_readonly_mutually_exclusive_and_stale_after_bond_change(self):
        state, roles = fixture(3)
        client = SimpleNamespace(account_state=state, call=Mock())
        acc = SimpleNamespace(client=client, lock=threading.Lock())
        entries = [{'roleId': 'r0', 'mode': 'advisor', 'profileId': 'control', 'excludedRoleIds': []}, {**config('r1'), 'excludedRoleIds': []}]
        result = api.handle(acc, 'autoMulti', {'entries': entries, 'includeWorn': True}, lambda sid: None)
        first, second = result['entries']
        self.assertTrue(first['advisor'])
        self.assertNotIn('advisor', second)
        self.assertEqual(first['results'][0]['profileId'], 'control')
        picked = lambda row: set(row['results'][row['pick']]['equipIds'].values())
        self.assertFalse(picked(first) & picked(second))
        changed = api.handle(acc, 'chooseMulti', {'batchId': result['batchId'], 'index': 0, 'pick': 1}, lambda sid: None)
        self.assertFalse(picked(changed['entries'][0]) & picked(changed['entries'][1]))
        client.call.assert_not_called()
        state['Artifacts'] = {'Datas': [artifact('A0347')]}
        with self.assertRaisesRegex(GameError, '数据已变化'):
            api.handle(acc, 'chooseMulti', {'batchId': result['batchId'], 'index': 0, 'pick': 0}, lambda sid: None)

    def test_missing_slots_return_no_partial_outfits(self):
        state, roles = fixture(1)
        state['EquipmentContainer']['Datas'].pop()
        result = recommend.search(state, roles['r0'], include_worn=True)
        self.assertTrue(result['exhaustive'])
        self.assertEqual(result['results'], [])
        self.assertIn(0, result['pool'].values())


if __name__ == '__main__':
    unittest.main()
