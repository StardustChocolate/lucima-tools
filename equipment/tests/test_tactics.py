"""真实角色技能审核及独立配装参考；全程离线、不操作游戏账号。"""
import copy
import itertools
import math
import unittest
from unittest.mock import patch

from backend.abyss_state import metadata
from equipment import auto, bonds, plans, recommend, stats, tactics
from equipment.tests.test_auto import fixture
from equipment.tests.test_bonds import artifact


def ids(sid, **values):
    return {row['id']: row for row in recommend.profiles({'StaticID': sid, 'AwakenLV': 6, **values})}


class TacticalSkillTests(unittest.TestCase):
    def test_pulling_allies_does_not_invent_effectiveness_or_crit_requirement(self):
        for sid in ('H009', 'H034', 'H044', 'H075', 'H123', 'H204', 'H604'):
            with self.subTest(role=sid):
                profile = ids(sid)['speed-pull']
                self.assertFalse(profile['hitTarget'])
                self.assertFalse(profile['critical'])
                self.assertEqual(profile['hardConditions'], [])
                self.assertTrue(all(not e['requiresHit'] and not e['reactive'] for e in profile['evidence']))

    def test_push_slow_silence_seal_and_cooldown_are_all_recognized(self):
        cases = {'H005': {'push', 'slow'}, 'H019': {'cooldown'}, 'H054': {'silence', 'push'},
                 'H188': {'silence', 'seal', 'suppress', 'cooldown'}, 'H612': {'slow', 'push'}}
        for sid, wanted in cases.items():
            with self.subTest(role=sid):
                profiles = ids(sid)
                fast = profiles['speed-hit']
                self.assertEqual(fast['ranking'], 'speed-first')
                self.assertTrue(fast['hitTarget'])
                self.assertTrue(wanted <= {row['kind'] for row in fast['evidence']})
                self.assertEqual(profiles['hit-speed']['ranking'], 'hit-first')

    def test_critical_pull_and_mitsuki_are_not_generic_hit_builds(self):
        moon, matilda = ids('H182'), ids('H031')
        self.assertNotIn('speed-hit', moon)
        self.assertEqual(moon['speed-crit']['hardConditions'][0]['min'], 100)
        self.assertTrue(all(not e['requiresHit'] for e in moon['speed-crit']['evidence']))
        self.assertTrue(any(e['criticalTrigger'] for e in matilda['speed-crit']['evidence']))
        self.assertNotIn('speed-pull', matilda)

    def test_reactive_passive_pull_is_not_a_free_opening_skill(self):
        for sid in ('H137', 'H156', 'H193'):
            with self.subTest(role=sid):
                self.assertNotIn('speed-pull', ids(sid))
        reactive = tactics.analyze({'StaticID': 'H193', 'AwakenLV': 6})['effects']
        self.assertTrue(any(e['kind'] == 'pull' and e['reactive'] for e in reactive))

    def test_awakening_and_skill_levels_change_actual_available_uses(self):
        self.assertNotIn('speed-pull', ids('H123', AwakenLV=0))
        self.assertIn('speed-pull', ids('H123', AwakenLV=6))
        base = ids('H188', Skills={'Skills': [{'StaticID': 'H188S3', 'Level': 1}]})['speed-hit']
        enhanced = ids('H188', Skills={'Skills': [{'StaticID': 'H188S3', 'Level': 6}]})['speed-hit']
        rate = lambda p: next(e['rate'] for e in p['evidence'] if e['kind'] == 'silence')
        self.assertEqual(rate(base), rate(enhanced), '强化技能3不能凭空提高技能1发生率')
        low = ids('H014', Skills={'Skills': [{'StaticID': 'H014S2', 'Level': 1}]})['speed-hit']
        high = ids('H014', Skills={'Skills': [{'StaticID': 'H014S2', 'Level': 6}]})['speed-hit']
        silence = lambda p: next(e['rate'] for e in p['evidence'] if e['rootSkill'] == 'H014S2' and e['kind'] == 'silence')
        self.assertEqual(silence(low), 85)
        self.assertEqual(silence(high), 100)

    def test_soul_ignore_resistance_is_separate_from_normal_hit_and_records_cost(self):
        profiles = ids('H188')
        self.assertTrue(all(e['requiresHit'] and not e['soulOnly'] for e in profiles['speed-hit']['evidence']))
        soul = profiles['speed-soul']
        self.assertFalse(soul['hitTarget'])
        self.assertTrue(all(e['soulOnly'] and e['ignoreResistance'] and e['soulCost'] > 0 for e in soul['evidence']))
        self.assertIn('未烧魂', soul['description'])
        # 奈兰希尔的烧魂无视抗性属于技能2驱散，不能扩成技能3推条也无视抗性。
        bypass = ids('H005')['speed-soul']['evidence']
        self.assertTrue(all(e['rootSkill'] == 'H005S2' and e['kind'] == 'strip' for e in bypass))

    def test_speed_penalty_and_position_trigger_do_not_offer_generic_first_speed(self):
        profiles = ids('H202')
        self.assertNotIn('speed-hit', profiles)
        self.assertIn('hit-speed', profiles)
        self.assertTrue(all('队友触发' in row['caution'] for row in profiles.values()))

    def test_current_roster_has_unique_auditable_profiles_and_no_mutation(self):
        for sid in stats.tables()['roles']:
            role = {'StaticID': sid, 'AwakenLV': 6}
            before = copy.deepcopy(role)
            rows = recommend.profiles(role)
            with self.subTest(role=sid):
                self.assertEqual(role, before)
                self.assertEqual(len(rows), len({row['id'] for row in rows}))
                for row in rows:
                    for effect in row.get('evidence', []):
                        self.assertIn(effect['rootSkill'], metadata()['tables']['Skill'])
                        self.assertIn(effect['kind'], tactics.LABELS)

    def test_hidden_cycle_and_friendly_effects_cannot_become_enemy_control(self):
        def dynamic(*nodes):
            return {f'DynamicField{i}': {'__type__': 'hash', 'name': 'Fun', 'value': node} for i, node in enumerate(nodes)}
        tables = {'Role': {'H900': {'SkillIDs': 'H900S1~H900S2:Hide'}}, 'RoleAwaken': {}, 'SkillLevel': {}, 'Status': {},
                  'Skill': {'H900S1': {'Dynamic': dynamic({'Fun': 'AddTime', 'TargetCamp': 'My', 'Target': 'Self', 'Value': .5},
                                                        {'Fun': 'AddStatus', 'TargetCamp': 'My', 'Target': 'Self', 'FID': 'Silence'})},
                            'H900S2': {'Dynamic': dynamic({'Fun': 'AddTime', 'Value': -.5}, {'Fun': 'AdditionSkill', 'FID': 'H900S2'})}}}
        with patch.object(tactics, 'metadata', return_value={'tables': tables}):
            result = tactics._analyze({'StaticID': 'H900'})
            self.assertEqual(result['effects'], [])
            tables['Skill']['H900S1']['Dynamic'] = dynamic({'Fun': 'AdditionSkill', 'FID': 'H900S2'})
            reached = tactics._analyze({'StaticID': 'H900'})['effects']
            self.assertEqual(len(reached), 1)
            self.assertEqual(reached[0]['kind'], 'push')
            self.assertTrue(reached[0]['conditional'])


class TacticalSearchTests(unittest.TestCase):
    def inventory(self):
        state, roles = fixture(3)
        roles['r0']['StaticID'] = 'H005'
        for index, item in enumerate(state['EquipmentContainer']['Datas']):
            choice = index % 3
            item.update(Set='', EquipRole='', SubProps={'SourceValues': [
                {'PropertyType': 'SpeedValue', 'Value': (20, 18, 12)[choice]},
                {'PropertyType': 'EffectHitRate', 'Value': (0, .15, .4)[choice]},
                {'PropertyType': 'AttackValue', 'Value': (0, 20000, 100000)[choice]},
            ]})
        return state, roles

    def test_fast_hit_and_hit_first_match_independent_cartesian_oracles(self):
        state, roles = self.inventory()
        role = roles['r0']
        choices = [[item for item in state['EquipmentContainer']['Datas'] if plans.equip_slot(item) == slot] for slot in plans.SLOTS]
        panels = [stats.role_panel(state, role, list(items), include_artifact_effects=True)['panel'] for items in itertools.product(*choices)]
        saved = copy.deepcopy(state)
        for target in (0, 100, 150):
            valid = [p for p in panels if p['EffectHitRate'] >= target]
            for profile, key in [('speed-hit', lambda p: (p['Speed'], p['EffectHitRate'], p['HP'], p['Defence'])),
                                 ('hit-speed', lambda p: (p['EffectHitRate'], p['Speed'], p['HP'], p['Defence']))]:
                with self.subTest(profile=profile, target=target):
                    result = recommend.search(state, role, profile_id=profile, hit_target=target, include_worn=True)
                    rows = [row for row in result['results'] if row['profileId'] == profile]
                    expected = sorted(valid, key=key, reverse=True)[:recommend.RESULT_LIMIT]
                    self.assertEqual([key(row['panel']) for row in rows], [key(p) for p in expected])
                    self.assertTrue(all(row['panel']['EffectHitRate'] >= target for row in rows))
        self.assertEqual(state, saved)

    def test_hit_target_is_independent_per_role_and_reservations_remain_exclusive(self):
        state, roles = self.inventory()
        roles['r1']['StaticID'] = 'H005'
        entries = [{'roleId': 'r0', 'mode': 'advisor', 'profileId': 'speed-hit', 'hitTarget': 150},
                   {'roleId': 'r1', 'mode': 'advisor', 'profileId': 'speed-hit', 'hitTarget': 50}]
        rows = auto.search_multi(state, roles, entries, include_worn=True)
        self.assertTrue(all(row['results'] for row in rows))
        picks = [row['results'][row['pick']] for row in rows]
        self.assertGreaterEqual(picks[0]['panel']['EffectHitRate'], 150)
        self.assertGreaterEqual(picks[1]['panel']['EffectHitRate'], 50)
        self.assertFalse(set(picks[0]['equipIds'].values()) & set(picks[1]['equipIds'].values()))

    def test_bounded_search_keeps_hit_items_that_enable_the_fastest_complete_outfit(self):
        state, roles = fixture(7)
        role = roles['r0']
        role['StaticID'] = 'H005'
        for index, item in enumerate(state['EquipmentContainer']['Datas']):
            slot, choice = divmod(index, 7)
            speed, hit = ((30-choice, 0) if choice < 5 else (30-choice, .8)) if slot == 0 else ((30, 0) if choice == 0 else (0, .2))
            item.update(Set='', EquipRole='', MainProp={'PropertyType': 'AttackValue', 'Value': 0},
                        SubProps={'SourceValues': [{'PropertyType': 'SpeedValue', 'Value': speed}, {'PropertyType': 'EffectHitRate', 'Value': hit}]})
        with patch.object(recommend, 'EXACT_LIMIT', 1), patch.dict(recommend.BUDGETS, {'standard': (4, 7)}):
            result = recommend.search(state, role, profile_id='speed-hit', hit_target=80, include_worn=True)
        self.assertTrue(result['results'])
        # Independent small integer reference: sacrifice 5 speed in slot 1 to
        # gain 80 hit, rather than sacrifice 30 speed in four later slots.
        base_speed = stats.role_base(role)['Speed']
        self.assertEqual(result['results'][0]['panel']['Speed'], round(base_speed+25+30*5))
        self.assertGreaterEqual(result['results'][0]['panel']['EffectHitRate'], 80)

    def test_conflicting_hit_range_is_not_relaxed_or_applied_to_ally_pull(self):
        state, roles = self.inventory()
        result = recommend.search(state, roles['r0'], profile_id='speed-hit', hit_target=150, include_worn=True,
                                  conditions=[{'stat': 'EffectHitRate', 'max': 80}])
        self.assertEqual(result['results'], [])
        self.assertIn('状态命中至少 150%', result['unavailableReason'])
        self.assertIn('冲突', result['unavailableReason'])
        self.assertGreater(result['profileCounts']['output'], 0)
        roles['r0']['StaticID'] = 'H044'
        ally = recommend.search(state, roles['r0'], profile_id='speed-pull', hit_target=500, include_worn=True)
        self.assertTrue(ally['results'])
        self.assertLess(ally['results'][0]['panel']['EffectHitRate'], 500)

    def test_current_bond_counts_towards_hit_and_beam_bounds(self):
        state, roles = fixture(1)
        role = roles['r0']
        role['StaticID'] = 'H188'
        for item in state['EquipmentContainer']['Datas']:
            item.update(Set='', SubProps={'SourceValues': []})
        current = stats.role_panel(state, role, state['EquipmentContainer']['Datas'], include_artifact_effects=True)['panel']
        target = current['EffectHitRate'] + 15
        self.assertEqual(recommend.search(state, role, profile_id='speed-hit', hit_target=target, include_worn=True)['results'], [])
        job = metadata()['tables']['Role']['H188']['Job']
        bond = next(sid for sid, value in metadata()['tables']['Artifact'].items() if value.get('Job') in (job, 'Other') and
                    bonds.permanent_mods(artifact(sid))[0].get('EffectHitRate', 0) >= .15)
        state['Artifacts'] = {'Datas': [artifact(bond)]}
        with patch.object(recommend, 'EXACT_LIMIT', 0):
            result = recommend.search(state, role, profile_id='speed-hit', hit_target=target, include_worn=True)
        self.assertTrue(result['results'])
        self.assertGreaterEqual(result['results'][0]['panel']['EffectHitRate'], target)

    def test_invalid_hit_targets_are_rejected_before_search(self):
        state, roles = self.inventory()
        for value in (True, None, '150', -1, 501, math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(auto.BuildError):
                recommend.search(state, roles['r0'], profile_id='speed-hit', hit_target=value)
