"""Exact inventory rankings against independent exhaustive small inventories."""
from collections import Counter
from copy import deepcopy
from itertools import product
import random
import time
import unittest
from unittest.mock import patch

from backend.tasks import SET_CN
from equipment import library, plans, stats


REFERENCES = {
    'speed': dict(HP=5380, Attack=1119, Defence=483, Speed=129, CriticalRate=23,
                  CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=0, PinchRate=3),
    'hit': dict(HP=5542, Attack=1057, Defence=532, Speed=118, CriticalRate=15,
                CriticalDamageRate=150, EffectHitRate=60, ResistanceRate=0, PinchRate=3),
    'resist': dict(HP=5254, Attack=649, Defence=694, Speed=105, CriticalRate=15,
                   CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=50, PinchRate=3),
    'survival': dict(HP=6267, Attack=984, Defence=638, Speed=117, CriticalRate=15,
                     CriticalDamageRate=150, EffectHitRate=0, ResistanceRate=0, PinchRate=3),
}


def gear(slot, number=0, name='Health', **props):
    return {'_id': {'$oid': f'{slot}-{number}'}, 'StaticID': 'E006' + str(plans.SLOTS.index(slot) + 1),
            'LV': 15, 'Set': name, 'MainProp': {},
            'SubProps': {'SourceValues': [{'PropertyType': k, 'Value': v} for k, v in props.items()]}}


def naked_panel(key, equips):
    """Independent addition and rounding, with no library calculation calls."""
    mods, counts = Counter(), Counter(e.get('Set') for e in equips)
    for eq in equips:
        for prop in [eq.get('MainProp') or {}, *(eq.get('SubProps') or {}).get('SourceValues', [])]:
            if prop.get('PropertyType'):
                mods[prop['PropertyType']] += prop['Value']
    for name, count in counts.items():
        info = stats.tables()['sets'].get(name)
        if info:
            for prop, value in info['rates'].items():
                mods[prop] += value * (count // info['count'])
    panel = {}
    for stat, value in REFERENCES[key].items():
        if stat in ('HP', 'Attack', 'Defence', 'Speed'):
            total = value * (1 + mods[stat + 'Rate']) + mods[stat + 'Value']
            panel[stat] = round(total, 2) if stat == 'Speed' else int(total + .5 + 1e-9)
        else:
            panel[stat] = round(value + 100 * mods[stat], 2 if stat in ('EffectHitRate', 'ResistanceRate') else 1)
    return panel


def metric(key, panel):
    if key == 'survival':
        return panel['HP'] * (1 + panel['Defence'] / 300)
    return panel[{'speed': 'Speed', 'hit': 'EffectHitRate', 'resist': 'ResistanceRate'}[key]]


def set_summary(counts):
    return sorted(f'{SET_CN[name]}套×{count}' for name, count in counts.items() if count)


def build_sets(build):
    counts = Counter()
    for name in build['sets']:
        for key, label in SET_CN.items():
            if name.startswith(label + '套×'):
                counts[key] = int(name.rsplit('×', 1)[1])
    return counts


def tier(key, combo):
    counts = Counter(e['Set'] for e in combo)
    if key == 'speed':
        return 0
    if key == 'survival':
        return 0 if set(counts) <= {'Health', 'Defense'} and all(c % 2 == 0 for c in counts.values()) else None
    main = 'Hit' if key == 'hit' else 'Resist'
    if set(counts) - {main, 'Health', 'Defense'} or any(c % 2 for c in counts.values()) or counts[main] < 2:
        return None
    return (6 - counts[main]) // 2


def brute_best(key, remaining):
    slots = [[e for e in remaining if e['LV'] == 15 and plans.equip_slot(e) == slot] for slot in plans.SLOTS]
    best, best_rank = None, None
    for combo in product(*slots):
        if len({plans._oid(e['_id']) for e in combo}) != 6:
            continue
        priority = tier(key, combo)
        if priority is None:
            continue
        panel = naked_panel(key, combo)
        if int(panel['Speed'] + .5 + 1e-9) < {'hit': 200, 'resist': 150}.get(key, 0):
            continue
        rank = (-priority, metric(key, panel))
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best = priority, metric(key, panel)
    return best


class RankingTests(unittest.TestCase):
    def assert_exhaustive(self, equips):
        before = deepcopy(equips)
        by_id = {plans._oid(e['_id']): e for e in equips}
        with patch.object(stats, 'role_mods', side_effect=AssertionError('awakening counted twice')), \
                patch.object(stats, 'role_base', side_effect=AssertionError('wrong bare panel')):
            rows = library.rankings(equips)
        self.assertEqual([row['key'] for row in rows], ['speed', 'hit', 'resist', 'survival'])
        for row in rows:
            key = row['key']
            self.assertEqual(row['referencePanel'], REFERENCES[key])
            self.assertEqual(row['minSpeed'], {'hit': 200, 'resist': 150}.get(key))
            self.assertEqual(row['limit'], 6 if key == 'survival' else 3)
            self.assertIsInstance(row['reference'], str)
            self.assertEqual(row['reference'], {'speed': '兔女郎爱莉卡', 'hit': '蜜茶多恩',
                                                'resist': '乌尔德', 'survival': 'Aoi Hinamori'}[key])
            self.assertEqual(row['unit'], {'speed': '速', 'hit': '%', 'resist': '%', 'survival': '分'}[key])
            remaining = list(equips)
            seen = set()
            for build in row['builds']:
                selected = [by_id[eid] for eid in build['equipIds']]
                self.assertEqual([plans.equip_slot(e) for e in selected], list(plans.SLOTS))
                self.assertTrue(all(e['LV'] == 15 for e in selected))
                self.assertFalse(seen.intersection(build['equipIds']))
                self.assertEqual(len(set(build['equipIds'])), 6)
                panel = naked_panel(key, selected)
                self.assertEqual(build['panel'], panel)
                self.assertEqual(build['speed'], panel['Speed'])
                self.assertAlmostEqual(build['value'], metric(key, panel))
                want = brute_best(key, remaining)
                self.assertIsNotNone(want)
                self.assertEqual((tier(key, selected), build['value']), want)
                active = Counter()
                for name, count in Counter(e['Set'] for e in selected).items():
                    active[name] = count // stats.tables()['sets'][name]['count']
                self.assertEqual(sorted(build['sets']), set_summary(active))
                seen.update(build['equipIds'])
                remaining = [e for e in remaining if plans._oid(e['_id']) not in seen]
            if len(row['builds']) < row['limit']:
                self.assertIsNone(brute_best(key, remaining))
        self.assertEqual(equips, before)
        return rows

    def test_random_small_inventories_match_brute_force_each_greedy_round(self):
        for seed in range(16):
            rng = random.Random(seed)
            # Two coherent primary/auxiliary families plus scattered sets.
            names = (('Hit', 'Health'), ('Resist', 'Defense'), ('Health', 'Defense'), ('Speed', 'Revenge'))[seed % 4]
            equips = [gear(slot, n, rng.choice((*names, 'Health', 'Defense')),
                           HPValue=rng.randrange(500), HPRate=rng.randrange(31)/100,
                           DefenceValue=rng.randrange(80), DefenceRate=rng.randrange(31)/100,
                           SpeedValue=rng.randrange(10, 30), EffectHitRate=rng.randrange(40)/100,
                           ResistanceRate=rng.randrange(40)/100)
                      for slot in plans.SLOTS for n in range(3)]
            with self.subTest(seed=seed):
                self.assert_exhaustive(equips)

    def test_all_reference_panels_and_percentage_scaling_use_awakened_bare_values(self):
        for key, name in (('speed', 'Speed'), ('hit', 'Hit'), ('resist', 'Resist'), ('survival', 'Health')):
            equips = [gear(slot, name=name) for slot in plans.SLOTS]
            equips[0]['SubProps']['SourceValues'] = [
                {'PropertyType': 'HPRate', 'Value': .5}, {'PropertyType': 'DefenceRate', 'Value': .5},
                {'PropertyType': 'SpeedValue', 'Value': 90}]
            row = next(row for row in library.rankings(equips) if row['key'] == key)
            self.assertEqual(row['builds'][0]['panel'], naked_panel(key, equips))
        for name, rate in (('Speed', .16), ('Revenge', .12)):
            equips = [gear(slot, name=name) for slot in plans.SLOTS]
            build = library.rankings(equips)[0]['builds'][0]
            self.assertEqual(build['speed'], round(129 * (1 + rate), 2))
        self.assertEqual(library.speed_builds([gear(slot) for slot in plans.SLOTS])[0]['speed'], 129)

    def test_displayed_speed_boundaries_half_up_and_main_speed(self):
        for key, name, boundary, minimum in (('hit', 'Hit', 81.5, 200), ('resist', 'Resist', 44.5, 150)):
            for total, feasible in ((boundary-.01, False), (boundary, True), (boundary+.01, True)):
                equips = [gear(slot, name=name) for slot in plans.SLOTS]
                equips[-1]['MainProp'] = {'PropertyType': 'SpeedValue', 'Value': total}
                row = next(row for row in self.assert_exhaustive(equips) if row['key'] == key)
                self.assertEqual(bool(row['builds']), feasible)
                if feasible:
                    self.assertEqual(row['builds'][0]['speed'], round(minimum-.5+total-boundary, 2))

    def test_tiers_fall_back_on_speed_infeasibility_then_consume_in_rank_order(self):
        for key, main in (('hit', 'Hit'), ('resist', 'Resist')):
            equips = []
            for slot in plans.SLOTS:
                equips += [gear(slot, n, main, SpeedValue=15, EffectHitRate=.1, ResistanceRate=.1)
                           for n in range(3)]
            # Huge lower-tier score cannot outrank a feasible six-primary build.
            equips += [gear(slot, 3, 'Health', SpeedValue=45, EffectHitRate=2, ResistanceRate=2)
                       for slot in plans.SLOTS]
            row = next(row for row in self.assert_exhaustive(equips) if row['key'] == key)
            self.assertEqual([build_sets(b)[main] for b in row['builds']], [3, 3, 3])
            # Six primary pieces exist but lack speed. Four plus one complete
            # health set becomes feasible. Exhaustion leaves no extra ranks.
            equips = [gear(slot, 0, main) for slot in plans.SLOTS]
            equips += [gear(slot, 1, 'Health', SpeedValue=45) for slot in plans.SLOTS[-2:]]
            row = next(row for row in self.assert_exhaustive(equips) if row['key'] == key)
            self.assertEqual(build_sets(row['builds'][0]), Counter({main: 2, 'Health': 1}))
            self.assertEqual(len(row['builds']), 1)
            # Explicit 2-primary + 2-health + 2-defense fallback.
            equips = [gear(slot, 0, (main, main, 'Health', 'Health', 'Defense', 'Defense')[i], SpeedValue=16)
                      for i, slot in enumerate(plans.SLOTS)]
            row = next(row for row in self.assert_exhaustive(equips) if row['key'] == key)
            self.assertEqual(build_sets(row['builds'][0]), Counter({main: 1, 'Health': 1, 'Defense': 1}))

    def test_tier_changes_after_consumption_and_classes_are_independent(self):
        equips = [gear(slot, n, 'Hit' if n == 0 or i < 4 else 'Health', SpeedValue=16)
                  for i, slot in enumerate(plans.SLOTS) for n in range(2)]
        row = next(row for row in self.assert_exhaustive(equips) if row['key'] == 'hit')
        self.assertEqual([build_sets(b) for b in row['builds']],
                         [Counter({'Hit': 3}), Counter({'Hit': 2, 'Health': 1})])
        equips = [gear(slot, n, 'Hit' if i < 2 else 'Health', SpeedValue=16, HPRate=.1)
                  for i, slot in enumerate(plans.SLOTS) for n in range(3)]
        rows = self.assert_exhaustive(equips)
        self.assertEqual(rows[0]['builds'][0]['equipIds'], rows[1]['builds'][0]['equipIds'])

    def test_low_scoring_fast_item_beyond_top_k_is_required(self):
        equips = [gear(slot, 0, 'Hit', EffectHitRate=.3) for slot in plans.SLOTS[:-1]]
        equips += [gear('boots', n, 'Hit', EffectHitRate=2-n*.001) for n in range(100)]
        equips += [gear('boots', 100, 'Hit', SpeedValue=82, EffectHitRate=.01)]
        rows = self.assert_exhaustive(equips)
        self.assertIn('boots-100', rows[1]['builds'][0]['equipIds'])

    def test_no_solution_partial_sets_and_strict_plus15(self):
        self.assert_exhaustive([])
        self.assert_exhaustive([gear(slot) for slot in plans.SLOTS[:-1]])
        for key, primary in (('hit', 'Hit'), ('resist', 'Resist'), ('survival', 'Health')):
            equips = [gear(slot, name=(primary if i < 3 else 'Defense'), SpeedValue=30)
                      for i, slot in enumerate(plans.SLOTS)]
            row = next(row for row in self.assert_exhaustive(equips) if row['key'] == key)
            self.assertEqual(row['builds'], [])
        equips = [gear(slot, name='Hit', SpeedValue=20) for slot in plans.SLOTS]
        for level in (0, 14, 16):
            equips[-1]['LV'] = level
            self.assertTrue(all(not row['builds'] for row in library.rankings(equips)))

    def test_each_two_primary_auxiliary_pattern_and_odd_auxiliary_rejection(self):
        for key, main in (('hit', 'Hit'), ('resist', 'Resist')):
            for health in (0, 2, 4):
                names = [main] * 2 + ['Health'] * health + ['Defense'] * (4-health)
                equips = [gear(slot, name=name, SpeedValue=16) for slot, name in zip(plans.SLOTS, names)]
                row = next(r for r in self.assert_exhaustive(equips) if r['key'] == key)
                self.assertEqual(len(row['builds']), 1)
            names = [main] * 2 + ['Health'] * 3 + ['Defense']
            equips = [gear(slot, name=name, SpeedValue=16) for slot, name in zip(plans.SLOTS, names)]
            row = next(r for r in self.assert_exhaustive(equips) if r['key'] == key)
            self.assertEqual(row['builds'], [])

    def test_main_and_sub_properties_flat_and_percent_and_duplicate_inventory_identity(self):
        equips = [gear(slot, name='Hit', SpeedValue=16, DefenceValue=40) for slot in plans.SLOTS]
        equips[0]['MainProp'] = {'PropertyType': 'HPValue', 'Value': 2700}
        equips[1]['MainProp'] = {'PropertyType': 'HPRate', 'Value': .6}
        equips[2]['MainProp'] = {'PropertyType': 'DefenceRate', 'Value': .6}
        equips[4]['MainProp'] = {'PropertyType': 'EffectHitRate', 'Value': .6}
        self.assert_exhaustive(equips)
        for equip in equips:
            equip.update(EquipRole={'$oid': 'already-worn'}, IsLock=True)
        repeated = equips + deepcopy(equips)
        rows = library.rankings(repeated)
        self.assertEqual([len(row['builds']) for row in rows], [1, 1, 0, 0])
        self.assertEqual(rows[1]['builds'][0]['panel'], naked_panel('hit', equips))

    def test_survival_keeps_balanced_candidate_below_convex_hull(self):
        equips = [gear(slot, name='Health') for slot in plans.SLOTS]
        # (HP, DEF+300) endpoints and balanced point. The balanced option is
        # below their convex hull but its product beats either endpoint.
        equips[0]['SubProps']['SourceValues'] = [
            {'PropertyType': 'HPValue', 'Value': 12000}]
        equips += [gear('weapon', 1, 'Health', DefenceValue=1000),
                   gear('weapon', 2, 'Health', HPValue=5500, DefenceValue=450)]
        rows = self.assert_exhaustive(equips)
        self.assertIn('weapon-2', rows[3]['builds'][0]['equipIds'])

    def test_overview_exposes_rankings_without_changing_quality_or_reference(self):
        equips = [gear(slot, name='Health') for slot in plans.SLOTS]
        overview = library.overview(equips)
        self.assertEqual(overview['rankings'], library.rankings(equips))
        self.assertEqual(overview['reference'], '兔女郎爱莉卡 · Lv60 满觉醒 · 无羁绊')
        self.assertEqual(library.REFERENCE_ROLE, {'StaticID': 'H153', 'LV': 60, 'AwakenLV': 6})

    def test_six_survival_ranks_reconsider_dominated_items(self):
        equips = [gear(slot, n, 'Health' if n % 2 == 0 else 'Defense',
                       HPRate=.4-n*.02, DefenceRate=.3-n*.02)
                  for slot in plans.SLOTS for n in range(6)]
        rows = library.rankings(equips)
        self.assertEqual(len(rows[3]['builds']), 6)
        remaining = list(equips)
        # All six slots have identical choices; 6**6 is small enough for a
        # full first-rank check, followed by decreasing exhaustive pools.
        for build in rows[3]['builds']:
            self.assertEqual(build['value'], brute_best('survival', remaining)[1])
            selected = set(build['equipIds'])
            remaining = [e for e in remaining if plans._oid(e['_id']) not in selected]

    def test_realistic_600_item_inventory_is_exact_and_fast_without_mutation(self):
        rng = random.Random(600)
        equips = [gear(slot, n, rng.choice(('Hit', 'Resist', 'Health', 'Defense', 'Speed', 'Revenge')),
                       SpeedValue=rng.randrange(8, 27), HPRate=rng.randrange(40)/100,
                       DefenceRate=rng.randrange(40)/100, EffectHitRate=rng.randrange(40)/100,
                       ResistanceRate=rng.randrange(40)/100)
                  for slot in plans.SLOTS for n in range(100)]
        before = deepcopy(equips)
        started = time.perf_counter()
        rows = library.rankings(equips)
        self.assertLess(time.perf_counter() - started, 5)
        self.assertEqual([len(row['builds']) for row in rows], [3, 3, 3, 6])
        self.assertEqual(equips, before)


if __name__ == '__main__':
    unittest.main()
