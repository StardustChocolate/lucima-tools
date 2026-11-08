"""离线规则与分配回归；不连接游戏、不穿戴真实账号装备。"""
import copy
import itertools
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from equipment import auto, api, gen_stat_tables, plans, stats
from backend.game_client import GameError


def fixture(count=3, roles_count=6):
    roles=[{'_id':{'$oid':f'r{i}'},'StaticID':'H001','LV':60,'Star':5} for i in range(roles_count)]
    eq=[]
    for k,(sid,prop) in enumerate(zip(('W001','EH001','C001','N001','R001','S001'),('AttackValue','HPValue','DefenceValue','CriticalDamageRate','EffectHitRate','SpeedValue'))):
        for j in range(count):
            value=(100+j*40) if k<3 else ((.5+j*.1) if k<5 else 20+j*3)
            eq.append({'_id':{'$oid':f'e{k}-{j}'},'StaticID':sid,'LV':15,'Set':('Attack','Critical','Health')[j%3],
                'MainProp':{'PropertyType':prop,'Value':value},'EquipRole':f'r{j%roles_count}',
                'SubProps':{'SourceValues':[{'PropertyType':'AttackRate','Value':.03*(count-j)},
                                           {'PropertyType':'CriticalRate','Value':.06*(count-j)},
                                           {'PropertyType':'ResistanceRate','Value':.03*j}]}})
    return {'RoleDataContainer':{'Roles':roles},'EquipmentContainer':{'Datas':eq}}, {f'r{i}':r for i,r in enumerate(roles)}


def config(rid):
    return {'roleId':rid,'criticalTarget':50,'objective':'damage','sets':[],'conditions':[], 'secondary':[]}


def combo(speed,score,res=100,hit=100):
    return {'panel':{'Speed':speed,'ResistanceRate':res,'EffectHitRate':hit},'metrics':{'damage':score,'survival':score}}


def ranked_combo(speed, score, name, res=100, hit=100):
    row = combo(speed, score, res, hit)
    row['panel'].update(CriticalRate=100, CriticalDamageRate=350)
    row['equipIds'] = {slot: f'{name}-{slot}' for slot in plans.SLOTS}
    return row


def speed_winners(rows, objective='damage', secondary=(), target=100, limit=1000):
    """独立参考：每个显示速度先取最高主评分，再对速度组按原排序排列。"""
    def rank(c):
        p = c['panel']
        key = (*[p[s] for s in secondary], c['metrics'][objective])
        return key if objective == 'survival' else (*key, -max(0,p['CriticalRate']-target), -max(0,p['CriticalDamageRate']-350))
    winners = {}
    for row in rows:
        speed = row['panel']['Speed']
        old = winners.get(speed)
        if old is None or (row['metrics'][objective], rank(row)) > (old['metrics'][objective], rank(old)):
            winners[speed] = row
    return sorted(winners.values(), key=rank, reverse=True)[:limit]


def joint_fixture():
    state, roles = fixture(6)
    for k, equip in enumerate(state['EquipmentContainer']['Datas']):
        _, j = divmod(k, 6)
        speed, critical, attack = ((25, 0, 1000+j) if j < 2 else
                                   ((0, .3, 1000+j) if j < 4 else (16.3+(j-4)*.1, .15, 100+j-4)))
        equip.update(Set='Health', EquipRole='', MainProp={'PropertyType':'SpeedValue', 'Value':speed},
                     SubProps={'SourceValues':[{'PropertyType':'CriticalRate', 'Value':critical},
                                               {'PropertyType':'AttackValue', 'Value':attack}]})
    return state, roles


def speed_window_fixture(count=32):
    state, roles = fixture(count)
    for k, equip in enumerate(state['EquipmentContainer']['Datas']):
        _, j = divmod(k, count)
        speed, attack = ((41, 1000) if j < (count-2)//2 else
                         ((0, 1000) if j < count-2 else ((15.4, 100) if j == count-2 else (15.6, 200))))
        equip.update(Set='Health', EquipRole='', MainProp={'PropertyType':'AttackValue', 'Value':attack},
                     SubProps={'SourceValues':[{'PropertyType':'SpeedValue', 'Value':speed},
                                               {'PropertyType':'CriticalRate', 'Value':.15}]})
    return state, roles


class ScoringTests(unittest.TestCase):
    def test_crit_targets_and_damage_cap(self):
        p={'Attack':1000,'HP':9000,'Defence':600,'CriticalRate':50,'CriticalDamageRate':350}
        self.assertEqual(auto.metrics(p,100)['damage'],1750)
        self.assertEqual(auto.metrics(p,50)['damage'],3500)
        self.assertEqual(auto.metrics({**p,'CriticalRate':100},50)['damage'],3500)
        self.assertEqual(auto.metrics({**p,'CriticalRate':0},0)['damage'],3500)
        self.assertEqual(auto.metrics({**p,'CriticalRate':120,'CriticalDamageRate':400},100)['damage'],3500)
        self.assertEqual(p['CriticalRate'],50)
        self.assertEqual(auto.metrics(p,100)['survival'],27000)

    def test_candidate_limit_keeps_rank_and_reports_limitation(self):
        top = auto._TopCandidates(3, 'damage', ['Speed'], 100)
        for i in range(50): top.offer(ranked_combo(300-i, 1000+i*100, str(i)))
        self.assertEqual([c['panel']['Speed'] for c in top.results()], [300, 299, 298])
        self.assertTrue(top.limited)

    def test_dominated_and_close_scores_are_still_retained(self):
        top = auto._TopCandidates(30, 'damage', ['Speed'], 100)
        for i, (speed, score) in enumerate(((252,1000),(251,1020),(250,900),(245,1150))):
            top.offer(ranked_combo(speed, score, str(i)))
        self.assertEqual([c['panel']['Speed'] for c in top.results()], [252,251,250,245])
        self.assertFalse(top.limited)

    def test_same_speed_keeps_only_one_stable_representative(self):
        top = auto._TopCandidates(30, 'damage', ['Speed'], 100)
        a, b = ranked_combo(252,1000,'a'), ranked_combo(252,1000,'b')
        top.offer(a); top.offer(copy.deepcopy(a)); top.offer(b)
        self.assertEqual(top.results(), [a])

    def test_same_speed_uses_selected_primary_objective(self):
        damage, survival = ranked_combo(204,3000,'damage'), ranked_combo(204,2000,'survival')
        damage['metrics']['survival'], survival['metrics']['survival'] = 1000,9000
        for objective, expected in (('damage',damage),('survival',survival)):
            for secondary in ([], ['Speed'], ['EffectHitRate'], ['ResistanceRate']):
                with self.subTest(objective=objective, secondary=secondary):
                    top = auto._TopCandidates(30,objective,secondary,100)
                    for row in (damage,survival,copy.deepcopy(damage)): top.offer(row)
                    self.assertEqual(top.results(),[expected])

    def test_same_speed_primary_wins_over_hit_and_resistance(self):
        for stat in ('EffectHitRate','ResistanceRate'):
            top = auto._TopCandidates(30,'damage',[stat],100)
            weaker = ranked_combo(204,1000,'weaker',res=150,hit=150)
            stronger = ranked_combo(204,2000,'stronger',res=10,hit=10)
            other = ranked_combo(205,1500,'other',res=100,hit=100)
            for row in (weaker,other,stronger): top.offer(row)
            self.assertEqual(top.results(),[other,stronger])

    def test_same_speed_does_not_fill_distinct_speed_limit(self):
        top = auto._TopCandidates(2,'damage',['Speed'],100)
        for i in range(100): top.offer(ranked_combo(204,1000+i,str(i)))
        top.offer(ranked_combo(203,900,'other'))
        self.assertEqual([c['panel']['Speed'] for c in top.results()],[204,203])
        self.assertEqual(top.results()[0]['metrics']['damage'],1099)
        self.assertFalse(top.limited)

    def test_primary_replacement_can_lower_group_rank_without_losing_other_speed(self):
        top = auto._TopCandidates(1,'damage',['EffectHitRate'],100)
        old = ranked_combo(204,1000,'old',hit=150)
        other = ranked_combo(205,1500,'other',hit=100)
        new = ranked_combo(204,2000,'new',hit=10)
        top.offer(old)
        self.assertFalse(top.skips((100,1500,0,0),205,205))
        top.offer(other); top.offer(new)
        self.assertEqual(top.results(),[other])
        self.assertTrue(top.limited)

    def test_group_pruning_requires_every_reachable_speed_to_have_a_better_primary(self):
        top = auto._TopCandidates(1,'damage',['EffectHitRate'],100)
        top.offer(ranked_combo(204,2000,'a',hit=10))
        self.assertTrue(top.skips((150,1900,0,0),204,204))
        self.assertFalse(top.skips((150,1900,0,0),204,205))
        self.assertFalse(top.skips((150,2100,0,0),204,204))

    def test_secondary_selection_order_precedes_primary_score(self):
        rows = [ranked_combo(252,1000,'a',100),ranked_combo(245,2000,'b',150),ranked_combo(252,1500,'c',90)]
        for objective in ('damage','survival'):
            for dims in (['Speed','ResistanceRate'], ['ResistanceRate','Speed']):
                top = auto._TopCandidates(30, objective, dims, 100)
                for row in rows: top.offer(row)
                expected = speed_winners(rows,objective,dims)
                self.assertEqual(top.results(),expected)

    def test_rank_key_uses_primary_score_to_break_secondary_ties(self):
        rows = [ranked_combo(250,2000,'a'),ranked_combo(252,1000,'b'),ranked_combo(252,1500,'c')]
        for objective in ('damage','survival'):
            result=sorted(rows,key=lambda c:auto._rank_key(c,objective,['Speed'],100),reverse=True)
            self.assertEqual([(c['panel']['Speed'],c['metrics'][objective]) for c in result],[(252,1500),(252,1000),(250,2000)])

    def test_joint_frontier_compression_only_relaxes_bounds(self):
        points = [(i,1000-i) for i in range(1000)]
        frontier = auto._joint_frontier(points)
        self.assertLessEqual(len(frontier),256)
        self.assertTrue(all(any(x>=px and y>=py for x,y in frontier) for px,py in points))

    def test_window_compression_keeps_low_speed_and_only_relaxes_other_stats(self):
        rows = [(i/10, i/10, tuple((j+1)*(1000-i) for j in range(len(auto.STATS)))) for i in range(1000)]
        profile = auto._window_profile(rows)
        self.assertLessEqual(len(profile),128)
        lookup = auto._WindowLookup(profile)
        for lo, _, values in rows:
            bound = lookup.query(lo,lo)
            self.assertIsNotNone(bound)
            self.assertTrue(all(a>=b for a,b in zip(bound,values)))
        self.assertIsNone(lookup.query(-2,-1))
        self.assertIsNone(lookup.query(101,102))


class SearchTests(unittest.TestCase):
    def test_complete_mode_ignores_node_and_result_limits(self):
        state,roles = fixture(2)
        for k,e in enumerate(state['EquipmentContainer']['Datas']):
            slot,j = divmod(k,2)
            e.update(Set='Health',EquipRole='',MainProp={'PropertyType':'SpeedValue','Value':(2**slot)*j},
                     SubProps={'SourceValues':[{'PropertyType':'CriticalRate','Value':.15}]})
        progress = []
        with patch.object(auto,'NODE_LIMIT',1):
            result = auto.search(state,roles['r0'],complete=True,limit=1,progress=progress.append)
        self.assertTrue(result['searchComplete'])
        self.assertTrue(result['bestConfirmed'])
        self.assertFalse(result['truncated'])
        self.assertFalse(result['moreCandidatesPossible'])
        self.assertIsNone(result['candidateLimit'])
        self.assertEqual(len(result['results']),64)
        self.assertEqual(len({c['panel']['Speed'] for c in result['results']}),64)
        self.assertGreater(result['nodes'],1)
        self.assertEqual(progress[-1]['pending'],0)

    def test_complete_mode_matches_exhaustive_speed_winners(self):
        state,roles = fixture(3)
        for k,e in enumerate(state['EquipmentContainer']['Datas']):
            e['Set'] = ('Speed','Critical','Health')[k%3]
            e['SubProps']['SourceValues'].append({'PropertyType':'SpeedValue','Value':(k//3+1)*(k%3)})
            e['SubProps']['SourceValues'].append({'PropertyType':'CriticalRate','Value':.1})
        options = [[e for e in state['EquipmentContainer']['Datas'] if plans.equip_slot(e)==s] for s in plans.SLOTS]
        for objective,secondary in (('damage',[]),('survival',['ResistanceRate','Speed']),('damage',['EffectHitRate'])):
            feasible = []
            for choice in itertools.product(*options):
                p = stats.role_panel(state,roles['r0'],list(choice))['panel']
                if not 130<=p['Speed']<=170 or objective=='damage' and p['CriticalRate']<100: continue
                feasible.append({'panel':p,'metrics':auto.metrics(p,100)})
            expected = speed_winners(feasible,objective,secondary)
            with patch.object(auto,'NODE_LIMIT',1):
                actual = auto.search(state,roles['r0'],complete=True,include_worn=True,objective=objective,
                                     secondary=secondary,conditions=[{'stat':'Speed','min':130,'max':170}],limit=1)
            self.assertTrue(actual['searchComplete'])
            self.assertEqual({c['panel']['Speed']:auto._rank_key(c,objective,secondary,100) for c in actual['results']},
                             {c['panel']['Speed']:auto._rank_key(c,objective,secondary,100) for c in expected})
            self.assertEqual([auto._rank_key(c,objective,secondary,100) for c in actual['results']],
                             [auto._rank_key(c,objective,secondary,100) for c in expected])

    def test_complete_mode_returns_more_than_200_speed_groups(self):
        state,roles = fixture(1)
        boots = state['EquipmentContainer']['Datas'][-1]
        for i in range(1,205):
            other = copy.deepcopy(boots)
            other['_id'] = {'$oid':f'complete-boots-{i}'}
            other['MainProp']['Value'] += i
            state['EquipmentContainer']['Datas'].append(other)
        result = auto.search(state,roles['r0'],complete=True,critical_target=50,limit=1)
        self.assertTrue(result['searchComplete'])
        self.assertEqual(len(result['results']),205)
        self.assertFalse(result['moreCandidatesPossible'])

    def test_screenshot_220_speed_prefers_2800_attack_306_cd_over_2834_295(self):
        state,roles = fixture(1)
        fixed = stats.panel(stats.role_base(roles['r0']),stats.role_mods(state,roles['r0']))
        for e in state['EquipmentContainer']['Datas']:
            e.update(Set='Health',EquipRole='',MainProp={},SubProps={'SourceValues':[]})
        boots = state['EquipmentContainer']['Datas'].pop()
        for name,attack,cd in (('game',2800,3.06),('old-candidate',2834,2.95)):
            e = copy.deepcopy(boots)
            e['_id'] = {'$oid':name}
            e['MainProp'] = {'PropertyType':'SpeedValue','Value':220-fixed['Speed']}
            e['SubProps']['SourceValues'] = [
                {'PropertyType':'AttackValue','Value':attack-fixed['Attack']},
                {'PropertyType':'CriticalRate','Value':.85-fixed['CriticalRate']},
                {'PropertyType':'CriticalDamageRate','Value':cd-fixed['CriticalDamageRate']}]
            state['EquipmentContainer']['Datas'].append(e)
        with patch.object(auto,'NODE_LIMIT',1):
            result = auto.search(state,roles['r0'],critical_target=85,conditions=[{'stat':'Speed','min':220,'max':220}],complete=True)
        self.assertTrue(result['searchComplete'])
        self.assertEqual(len(result['results']),1)
        winner = result['results'][0]
        self.assertEqual(winner['equipIds']['boots'],'game')
        self.assertEqual((winner['panel']['Speed'],winner['panel']['Attack'],winner['panel']['CriticalDamageRate']),(220,2800,306))
        self.assertEqual(winner['metrics']['damage'],8568)

    def test_complete_mode_cancellation_never_returns_partial_candidates(self):
        state,roles = fixture(3)
        with self.assertRaises(auto.SearchCancelled):
            auto.search(state,roles['r0'],complete=True,cancelled=lambda:True)

    def test_output_priority_with_speed_window_finds_stronger_204_not_only_219(self):
        state, roles = speed_window_fixture()
        # 高攻击件的速度只有0或41；独立上界会虚构区间内可穿6件高攻击。
        # 精确参考：2件41速+3件0速+1件15.6速，204速/5871攻/105暴击。
        # 此前300000节点仅得219速/5071攻的一套，漏掉输出高15.8%的方案。
        with patch.object(auto,'NODE_LIMIT',10000):
            result = auto.search(state,roles['r0'],critical_target=100,
                                 conditions=[{'stat':'Speed','min':203,'max':220}])
        self.assertTrue(result['bestConfirmed'])
        self.assertTrue(result['results'])
        self.assertEqual(len({c['panel']['Speed'] for c in result['results']}),len(result['results']))
        self.assertGreater(result['neighborNodes'],0)
        self.assertLessEqual(result['neighborNodes'],2500)
        self.assertLessEqual(result['nodes'],10000)
        best = result['results'][0]
        self.assertEqual((best['panel']['Speed'],best['panel']['Attack']), (204,5871))
        self.assertEqual(best['metrics']['damage'],8806.5)
        self.assertTrue(all(203<=c['panel']['Speed']<=220 and c['panel']['CriticalRate']>=100 for c in result['results']))
        self.assertTrue(result['moreCandidatesPossible'])
        self.assertFalse(result['searchComplete'])

    def test_complete_candidate_exploration_finds_stronger_204_within_shared_budget(self):
        state, roles = speed_window_fixture(4)
        base = stats.role_base(roles['r0'])
        fixed = stats.panel(base,stats.role_mods(state,roles['r0']))
        by_slot = {slot:[] for slot in plans.SLOTS}
        raw = {auto._oid(e['_id']):e for e in state['EquipmentContainer']['Datas']}
        pool = {}
        for eid,e in raw.items():
            vec = stats.contribution(base,stats.equip_mods(e))
            it = {'id':eid,'slot':plans.equip_slot(e),'set':e['Set'],'vec':tuple(vec[s] for s in auto.STATS),'aux':0}
            by_slot[it['slot']].append(it)
            pool[eid] = it
        calls = 0
        def validate(picked,total=None,counts=None):
            nonlocal calls
            if total is not None:
                calls += 1
                expected = tuple(fixed[s]+sum(it['vec'][k] for it in picked) for k,s in enumerate(auto.STATS))
                self.assertTrue(all(abs(a-b)<1e-8 for a,b in zip(total,expected)))
                self.assertEqual(counts,{'Health':6})
            panel = stats.role_panel(state,roles['r0'],[raw[it['id']] for it in picked])['panel']
            if not (203<=panel['Speed']<=220 and panel['CriticalRate']>=100): return None
            return {'equipIds':{it['slot']:it['id'] for it in picked},'panel':panel,'metrics':auto.metrics(panel,100)}
        seed = validate([by_slot[s][j] for s,j in zip(plans.SLOTS,(0,0,1,1,3,3))])
        self.assertEqual(seed['panel']['Speed'],219)
        top = auto._TopCandidates(200,'damage',[],100)
        top.offer(seed)
        original = copy.deepcopy(state)
        used = auto._explore_neighbors(seed,pool,by_slot,tuple(fixed[s] for s in auto.STATS),validate,top,120)
        self.assertEqual(used,120)
        self.assertEqual(calls,used)
        self.assertEqual(state,original)
        self.assertGreater(len(top.results()),1)
        self.assertEqual(top.results()[0]['panel']['Speed'],204)
        self.assertGreater(top.results()[0]['metrics']['damage'],seed['metrics']['damage'])

    def test_speed_window_and_critical_target_keep_best_of_same_speed_combinations(self):
        state, roles = joint_fixture()
        for secondary in ([], ['Speed'], ['ResistanceRate'], ['EffectHitRate']):
            with patch.object(auto,'NODE_LIMIT',1000):
                result = auto.search(state,roles['r0'],critical_target=100,secondary=secondary,
                                     conditions=[{'stat':'Speed','min':203,'max':220}])
            self.assertTrue(result['searchComplete'])
            self.assertEqual(len(result['results']),1)
            self.assertTrue(all(c['panel']['Speed']==204 and c['panel']['CriticalRate']>=100 for c in result['results']))
            expected = stats.role_panel(state,roles['r0'],[e for k,e in enumerate(state['EquipmentContainer']['Datas']) if k%6==5])['panel']
            self.assertEqual(result['results'][0]['panel'],expected)

    def test_joint_speed_and_critical_conditions_group_all_64_valid_combinations(self):
        state, roles = joint_fixture()
        for secondary in ([], ['Speed']):
            with patch.object(auto, 'NODE_LIMIT', 1000):
                result = auto.search(state, roles['r0'], critical_target=100, secondary=secondary,
                                     conditions=[{'stat':'Speed', 'min':203}], tolerance=100)
            self.assertFalse(result['truncated'])
            self.assertTrue(result['searchComplete'])
            self.assertFalse(result['moreCandidatesPossible'])
            self.assertEqual(len(result['results']), 1)
            self.assertTrue(all(c['panel']['Speed'] >= 203 and c['panel']['CriticalRate'] >= 100 for c in result['results']))
            self.assertTrue(all(c['panel']['Speed'] == 204 for c in result['results']))
            self.assertEqual([c['metrics']['damage'] for c in result['results']],
                             sorted([c['metrics']['damage'] for c in result['results']], reverse=True))

    def test_candidate_cap_is_not_reported_as_total_feasible_count(self):
        state, roles = fixture(1)
        boots = state['EquipmentContainer']['Datas'][-1]
        for i in range(1,12):
            other = copy.deepcopy(boots)
            other['_id'] = {'$oid':f'boots-{i}'}
            other['MainProp']['Value'] += i
            state['EquipmentContainer']['Datas'].append(other)
        result = auto.search(state, roles['r0'], critical_target=50, secondary=['Speed'], limit=10)
        self.assertEqual(len(result['results']),10)
        self.assertEqual(len({c['panel']['Speed'] for c in result['results']}),10)
        self.assertEqual(result['candidateLimit'],10)
        self.assertFalse(result['searchComplete'])
        self.assertTrue(result['moreCandidatesPossible'])
        self.assertFalse(result['truncated'])

    def test_valid_current_equipment_is_preserved_even_if_budget_runs_out(self):
        state, roles = joint_fixture()
        for k, equip in enumerate(state['EquipmentContainer']['Datas']):
            if k % 6 == 4: equip['EquipRole'] = 'r0'
        with patch.object(auto, 'NODE_LIMIT', 0):
            result = auto.search(state, roles['r0'], critical_target=100,
                                 conditions=[{'stat':'Speed', 'min':203}])
        self.assertEqual(len(result['results']),1)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['searchComplete'])
        self.assertEqual(result['results'][0]['panel']['Speed'],204)
        self.assertGreaterEqual(result['results'][0]['panel']['CriticalRate'],100)

    def test_critical_target_filters_faster_below_target_in_all_rankings(self):
        state, roles = fixture(1)
        for equip in state['EquipmentContainer']['Datas']:
            equip['Set'] = 'Health'
            equip['SubProps']['SourceValues'] = [p for p in equip['SubProps']['SourceValues'] if p['PropertyType'] != 'CriticalRate']
        boots = state['EquipmentContainer']['Datas'].pop()
        for name, speed, critical, attack in (('below', 40, .849, 500), ('exact', 35, .85, 100), ('above', 30, .95, 800)):
            equip = copy.deepcopy(boots)
            equip['_id'] = {'$oid':name}
            equip['MainProp'] = {'PropertyType':'SpeedValue', 'Value':speed}
            equip['SubProps']['SourceValues'] = [
                {'PropertyType':'CriticalRate', 'Value':critical}, {'PropertyType':'AttackValue', 'Value':attack},
                {'PropertyType':'EffectHitRate', 'Value':speed / 100}, {'PropertyType':'ResistanceRate', 'Value':speed / 100}]
            state['EquipmentContainer']['Datas'].append(equip)
        for secondary in ([], ['Speed'], ['EffectHitRate'], ['ResistanceRate'], list(auto.SECONDARY)):
            with self.subTest(secondary=secondary):
                result = auto.search(state, roles['r0'], critical_target=100, secondary=secondary, tolerance=0)
                self.assertTrue(result['results'])
                self.assertFalse(result['truncated'])
                self.assertTrue(all(c['panel']['CriticalRate'] >= 100 for c in result['results']))
                ids = {c['equipIds']['boots'] for c in result['results']}
                self.assertEqual(ids, {'exact', 'above'})
                if secondary: self.assertEqual(result['results'][0]['equipIds']['boots'], 'exact')
                else: self.assertEqual(result['results'][0]['equipIds']['boots'], 'above')
        unrestricted = auto.search(state, roles['r0'], critical_target=0, secondary=['Speed'])
        self.assertEqual(unrestricted['results'][0]['equipIds']['boots'], 'below')

    def test_hit_and_resistance_priority_without_minimum_under_budget(self):
        for stat in ('EffectHitRate', 'ResistanceRate'):
            state, roles = fixture(16)
            for k, equip in enumerate(state['EquipmentContainer']['Datas']):
                slot, j = divmod(k, 16)
                value = .2 + (.02 if slot >= 4 and j >= 8 else 0)
                attack = 100 + j * 20
                if slot == 5 and j == 14:
                    value, attack = .21, 1000
                equip.update(Set='Speed' if j < 8 else 'Attack', EquipRole='',
                             MainProp={'PropertyType':stat, 'Value':value},
                             SubProps={'SourceValues':[{'PropertyType':'AttackValue', 'Value':attack},
                                                       {'PropertyType':'CriticalRate', 'Value':.2}]})
            rows = []
            for conditions in ([], [{'stat':stat, 'min':120}]):
                with patch.object(auto, 'NODE_LIMIT', 800):
                    result = auto.search(state, roles['r0'], sets=['Speed'], secondary=[stat], conditions=conditions)
                self.assertTrue(result['bestConfirmed'])
                # 同速度下高输出的靴子命中/抗性略低，仍须选主评分更高者。
                self.assertEqual(result['results'][0]['panel'][stat], 123)
                self.assertEqual(len(result['results']), 1)
                if conditions:
                    self.assertLess(result['bestPossible'][stat], 200)
                rows.append(result['results'][0])
            self.assertEqual(rows[0]['panel'], rows[1]['panel'])

    def test_speed_priority_finds_fastest_before_budget_without_minimum(self):
        state, roles = fixture(16)
        for k, equip in enumerate(state['EquipmentContainer']['Datas']):
            slot, j = divmod(k, 16)
            speed = (20 if slot < 5 else 28) + (2 if slot >= 4 and j >= 8 else 0)
            attack = 100 + j * 20
            if slot == 5 and j == 14:
                speed, attack = 29, 1000  # 254 速输出更高，需保留为下一档取舍。
            equip.update(Set='Speed' if j < 8 else 'Attack', EquipRole='',
                         MainProp={'PropertyType':'SpeedValue', 'Value':speed},
                         SubProps={'SourceValues':[{'PropertyType':'AttackValue', 'Value':attack},
                                                   {'PropertyType':'CriticalRate', 'Value':.2}]})
        rows = []
        for conditions in ([], [{'stat':'Speed', 'min':250}]):
            with patch.object(auto, 'NODE_LIMIT', 800):
                result = auto.search(state, roles['r0'], sets=['Speed'], secondary=['Speed'], conditions=conditions)
            self.assertTrue(result['bestConfirmed'])
            self.assertLessEqual(result['nodes'], 800)
            speeds = [c['panel']['Speed'] for c in result['results']]
            self.assertEqual(speeds[0], 255)
            self.assertGreater(len(result['results']), 2)
            rows.append(result['results'][0])
        self.assertEqual(rows[0]['panel'], rows[1]['panel'])
        self.assertEqual(rows[0]['metrics']['damage'], rows[1]['metrics']['damage'])

    def test_secondary_exhaustive_reference_with_sets_and_stat_limits(self):
        state, roles = fixture(3)
        pool = state['EquipmentContainer']['Datas']
        for k, equip in enumerate(pool):
            slot, j = divmod(k, 3)
            equip['Set'] = ('Speed', 'Torrent', 'Hit')[j]
            equip['SubProps']['SourceValues'].append({'PropertyType':'SpeedValue', 'Value':(slot + 1) * (2 - j)})
        options = [[e for e in pool if plans.equip_slot(e) == s] for s in plans.SLOTS]
        for objective, dims, required, conditions in (
            ('damage', ['Speed'], ['Speed'], []),
            ('survival', ['Speed'], ['Torrent'], []),
            ('damage', ['Speed', 'ResistanceRate'], [], [{'stat':'Speed', 'max':170}]),
            ('survival', ['ResistanceRate', 'Speed'], ['Hit'], [{'stat':'EffectHitRate', 'min':60}]),
            ('damage', list(auto.SECONDARY), ['Hit', 'Hit'], [{'stat':'Speed', 'min':130}]),
        ):
            with self.subTest(objective=objective, dims=dims, sets=required):
                need = auto.normalize_sets(required)
                expected = []
                for choice in itertools.product(*options):
                    if any(sum(e['Set'] == name for e in choice) < count for name, count in need.items()): continue
                    p = stats.role_panel(state, roles['r0'], list(choice))['panel']
                    if objective == 'damage' and p['CriticalRate'] < 100: continue
                    if any((c.get('min') is not None and p[c['stat']] < c['min']) or
                           (c.get('max') is not None and p[c['stat']] > c['max']) for c in conditions): continue
                    candidate = {'panel':p, 'metrics':auto.metrics(p, 100 if objective == 'damage' else 0)}
                    expected.append(candidate)
                def rank(c):
                    p=c['panel']
                    key=(*[p[s] for s in dims],c['metrics'][objective])
                    return key if objective=='survival' else (*key,-max(0,p['CriticalRate']-100),-max(0,p['CriticalDamageRate']-350))
                expected=speed_winners(expected,objective,dims,limit=100)
                result = auto.search(state, roles['r0'], include_worn=True, sets=required, conditions=conditions,
                                     objective=objective, secondary=dims, tolerance=0, limit=100)
                self.assertFalse(result['truncated'])
                self.assertEqual(result['bestConfirmed'], bool(expected))
                def summary(rows):
                    return [(tuple(c['panel'][s] for s in dims), c['metrics'][objective]) for c in rows]
                self.assertEqual(summary(result['results']), summary(expected))

    def test_secondary_budget_does_not_claim_unproven_best(self):
        state, roles = fixture(3)
        with patch.object(auto, 'NODE_LIMIT', 1):
            result = auto.search(state, roles['r0'], include_worn=True, secondary=['Speed'])
        self.assertTrue(result['truncated'])
        self.assertFalse(result['bestConfirmed'])

    def test_hit_priority_does_not_confirm_pending_speed_group_representatives(self):
        state, roles = fixture(3)
        for secondary in (['EffectHitRate'], ['ResistanceRate','Speed']):
            with patch.object(auto,'NODE_LIMIT',40):
                result = auto.search(state,roles['r0'],include_worn=True,secondary=secondary)
            self.assertTrue(result['results'])
            self.assertTrue(result['truncated'])
            self.assertFalse(result['bestConfirmed'])

    def test_exhaustive_reference_single_objectives_and_sets(self):
        state,roles=fixture(2)
        for equip in state['EquipmentContainer']['Datas']:
            equip['SubProps']['SourceValues'].append({'PropertyType':'CriticalRate', 'Value':.1})
        for objective,sets in itertools.product(('damage','survival'),([],['Attack'],['Critical','Critical'])):
            role=roles['r0']; pool=state['EquipmentContainer']['Datas']
            options=[[e for e in pool if plans.equip_slot(e)==s] for s in plans.SLOTS]
            expected=[]
            for choice in itertools.product(*options):
                counts={s:sum(e['Set']==s for e in choice) for s in set(sets)}
                if any(counts[s]<n for s,n in auto.normalize_sets(sets).items()):continue
                p=stats.role_panel(state,role,list(choice))['panel']
                if objective == 'damage' and p['CriticalRate'] < 100: continue
                expected.append({'panel':p,'metrics':auto.metrics(p,100)})
            actual=auto.search(state,role,sets=sets,objective=objective,include_worn=True,limit=100)
            self.assertFalse(actual['truncated'])
            expected = speed_winners(expected,objective)
            self.assertEqual([c['metrics'][objective] for c in expected],[c['metrics'][objective] for c in actual['results']])

    def test_secondary_thresholds_inclusive_and_same_tuple_best(self):
        state,roles=fixture(2)
        raw=auto.search(state,roles['r0'],include_worn=True,limit=100,critical_target=50)
        thresholds={s:min(c['panel'][s] for c in raw['results']) for s in auto.SECONDARY}
        actual=auto.search(state,roles['r0'],include_worn=True,secondary=list(auto.SECONDARY),conditions=[{"stat":s,"min":v} for s,v in thresholds.items()],tolerance=0,critical_target=50)
        self.assertTrue(actual['results'])
        for c in actual['results']:
            self.assertTrue(all(c['panel'][s]>=v for s,v in thresholds.items()))
            same=[x for x in raw['results'] if auto._bucket(x['panel'],auto.SECONDARY)==auto._bucket(c['panel'],auto.SECONDARY)]
            self.assertIn(c['metrics']['damage'],[x['metrics']['damage'] for x in same])
        impossible=auto.search(state,roles['r0'],include_worn=True,secondary=['Speed','EffectHitRate'],conditions=[{'stat':'EffectHitRate','min':10000}])
        self.assertFalse(impossible['results'])

    def test_secondary_sort_does_not_add_thresholds(self):
        state, roles = fixture(2)
        result = auto.search(state, roles['r0'], include_worn=True, secondary=['Speed'], secondary_min={'Speed':99999},critical_target=50)
        self.assertTrue(result['results'])
        self.assertNotIn('Speed', result['bestPossible'])

    def test_survival_ignores_critical_values_and_target(self):
        state, roles = fixture(3)
        a = auto.search(state, roles['r0'], include_worn=True, objective='survival', critical_target=100)
        for i, eq in enumerate(state['EquipmentContainer']['Datas']):
            eq['SubProps']['SourceValues'].append({'PropertyType':'CriticalRate','Value':i * .2})
            eq['SubProps']['SourceValues'].append({'PropertyType':'CriticalDamageRate','Value':i * .3})
        b = auto.search(state, roles['r0'], include_worn=True, objective='survival', critical_target='ignored')
        self.assertEqual([c['equipIds'] for c in a['results']], [c['equipIds'] for c in b['results']])
        self.assertEqual([c['metrics']['survival'] for c in a['results']], [c['metrics']['survival'] for c in b['results']])

    def test_low_critical_is_disqualified_unless_target_satisfied_or_zero(self):
        state,roles=fixture(1)
        for secondary in ([], ['Speed'], ['EffectHitRate'], ['ResistanceRate']):
            result=auto.search(state,roles['r0'],critical_target=100,secondary=secondary)
            self.assertFalse(result['results'])
            self.assertLess(result['bestPossible']['CriticalRate'],100)
            for target in (0, 50):
                allowed=auto.search(state,roles['r0'],critical_target=target,secondary=secondary)
                self.assertTrue(allowed['results'])
                self.assertGreaterEqual(allowed['results'][0]['panel']['CriticalRate'],target)

    def test_six_roles_unique_and_insufficient(self):
        state,roles=fixture(6)
        result=auto.search_multi(state,roles,[config(r) for r in roles],include_worn=True,limit=3)
        ids=[eid for r in result for eid in r['results'][r['pick']]['equipIds'].values()]
        self.assertEqual(len(ids),36);self.assertEqual(len(set(ids)),36)
        api._validate_results(result)
        small,roles=fixture(1)
        result=auto.search_multi(small,roles,[config(r) for r in roles],include_worn=True)
        self.assertEqual(sum(bool(r.get('results')) for r in result),1)

    def test_chosen_only_reserved_and_prefix_stable(self):
        state,roles=fixture(3)
        configs=[config('r0'),config('r1')]
        results=auto.search_multi(state,roles,configs,include_worn=True,limit=4)
        prefix=copy.deepcopy(results[:1]);prefix[0]['pick']=1;prefix[0]['done']=True
        changed=auto.search_multi(state,roles,configs,include_worn=True,prefix=prefix,limit=4)
        self.assertEqual(changed[0],prefix[0]);api._validate_results(changed)
        chosen=set(prefix[0]['results'][1]['equipIds'].values())
        pool=auto.candidate_pool(state,roles['r1'],only_max=True,include_worn=True,exclude=chosen)
        self.assertEqual(len(pool),12)
        self.assertTrue(changed[1]['results'])

    def test_pool_compatibility_duplicate_ids_and_filters(self):
        state,roles=fixture(3)
        state['EquipmentContainer']['Datas'].append(copy.deepcopy(state['EquipmentContainer']['Datas'][0]))
        pool=auto.candidate_pool(state,roles['r0'],only_max=True,include_worn=False,free_roles={'r0','r1'})
        self.assertEqual(len(pool),12)
        state['EquipmentContainer']['Datas'][0]['LV']=14
        self.assertTrue(all(e['LV']==15 for e in auto.candidate_pool(state,roles['r0'],only_max=True,include_worn=True)))

    def test_equal_scores_stable_and_same_speed_equipment_ids_collapsed(self):
        state, roles = fixture(3)
        for slot in plans.SLOTS:
            group = [e for e in state['EquipmentContainer']['Datas'] if plans.equip_slot(e) == slot]
            for e in group[1:]:
                for key in ('Set', 'MainProp', 'SubProps'): e[key] = copy.deepcopy(group[0][key])
        a = auto.search(state, roles['r0'], include_worn=True)
        b = auto.search(state, roles['r0'], include_worn=True)
        self.assertEqual(a, b)
        self.assertEqual(len({c['metrics']['damage'] for c in a['results']}), 1)
        gradient = auto.search(state, roles['r0'], include_worn=True, secondary=['Speed'])
        self.assertEqual(len(gradient['results']), 1)
        self.assertFalse(gradient['moreCandidatesPossible'])
        self.assertTrue(gradient['searchComplete'])
        self.assertEqual(len({tuple(c['equipIds'][s] for s in plans.SLOTS) for c in gradient['results']}),len(gradient['results']))

    def test_negative_set_effect_bounds_match_exhaustive(self):
        state, roles = fixture(2)
        for i, e in enumerate(state['EquipmentContainer']['Datas']): e['Set'] = 'Torrent' if i % 2 else 'Health'
        choices = [[e for e in state['EquipmentContainer']['Datas'] if plans.equip_slot(e) == s] for s in plans.SLOTS]
        expected = []
        for choice in itertools.product(*choices):
            panel = stats.role_panel(state, roles['r0'], list(choice))['panel']
            expected.append({'panel':panel,'metrics':auto.metrics(panel,100)})
        actual = auto.search(state, roles['r0'], include_worn=True, objective='survival', limit=5)
        expected = speed_winners(expected,'survival',limit=5)
        self.assertEqual([c['metrics']['survival'] for c in actual['results']], [c['metrics']['survival'] for c in expected])

    def test_large_pool_budget_and_joint_targets(self):
        state, roles = fixture(16)
        with patch.object(auto, 'NODE_LIMIT', 5000):
            result = auto.search(state, roles['r0'], include_worn=True, secondary=list(auto.SECONDARY),
                                 secondary_min={'Speed': 0, 'ResistanceRate': 0, 'EffectHitRate': 0})
        self.assertLessEqual(result['nodes'], 5001)
        self.assertTrue(result['results'])
        self.assertEqual(len({tuple(c['equipIds'][s] for s in plans.SLOTS) for c in result['results']}), len(result['results']))
        for c in result['results']: self.assertEqual(len(set(c['equipIds'].values())), 6)

    def test_validation_missing_slot_equal_scores_and_deterministic_budget(self):
        state,roles=fixture(3)
        with self.assertRaises(auto.BuildError):auto.search(state,roles['r0'],critical_target=-1)
        with self.assertRaises(auto.BuildError):auto.search(state,roles['r0'],critical_target=float('nan'))
        with self.assertRaises(auto.BuildError):auto.search_multi(state,roles,[config('r0')]*2)
        with self.assertRaises(auto.BuildError):auto.search(state,roles['r0'],secondary=['Speed'],conditions=[{'stat':'Speed','min':200,'max':100}])
        with patch.object(auto,'NODE_LIMIT',40):
            a=auto.search(state,roles['r0'],include_worn=True);b=auto.search(state,roles['r0'],include_worn=True)
            self.assertTrue(a['truncated']);self.assertEqual(a,b)
        state['EquipmentContainer']['Datas']=[e for e in state['EquipmentContainer']['Datas'] if plans.equip_slot(e)!='boots']
        self.assertFalse(auto.search(state,roles['r0'],include_worn=True)['results'])


class ImprintTests(unittest.TestCase):
    def test_generator_preserves_initial_bonus_and_level_increment(self):
        rows=[{'ID':'five','Base.DynamicField1':'CriticalRate#0','LevelAdd.DynamicField1':'CriticalRate#0.028'},
              {'ID':'four','Base.DynamicField1':'CriticalRate#0.021','LevelAdd.DynamicField1':'CriticalRate#0.021'},
              {'ID':'three','Base.DynamicField1':'CriticalRate#0.028','LevelAdd.DynamicField1':'CriticalRate#0.014'}]
        self.assertEqual(gen_stat_tables.imprint_bonuses(rows),{
            'five':{'prop':'CriticalRate','base':0,'add':.028},
            'four':{'prop':'CriticalRate','base':.021,'add':.021},
            'three':{'prop':'CriticalRate','base':.028,'add':.014}})

    def test_each_rarity_uses_initial_bonus_at_rank_d_then_level_increments(self):
        expected={'5S_Self_CriticalRate':[0,0,.028,.056,.084,.112,.14,.168],
                  '4S_Self_CriticalRate':[0,.021,.042,.063,.084,.105,.126,.147],
                  '3S_Self_CriticalRate':[0,.028,.042,.056,.070,.084,.098,.112]}
        for key,values in expected.items():
            with patch.dict(stats.tables()['roles']['H001'],{'selfImprint':key}):
                for lv,value in enumerate(values):
                    with self.subTest(imprint=key,level=lv):
                        role={'_id':'hero','StaticID':'H001','LV':60,'ImprintLV':lv,'IsSelfImprint':True}
                        self.assertAlmostEqual(stats.role_mods({},role).get('CriticalRate',0),value)

    def test_team_imprint_does_not_enter_the_personal_panel(self):
        role={'_id':'hero','StaticID':'H167','LV':60,'ImprintLV':3,'IsSelfImprint':False}
        self.assertEqual(stats.role_mods({},role).get('CriticalRate',0),0)


class CatFestivalImprintTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((Path(__file__).parent/'fixtures'/'catfestival_imprint_plan.json').read_text())
        self.state=self.data['state'];self.role=self.state['RoleDataContainer']['Roles'][0]

    def search(self,target=100):
        return auto.search(self.state,self.role,critical_target=target,include_worn=True,complete=True)

    def test_actual_six_items_reproduce_98_6_percent_and_every_other_game_value(self):
        panel=stats.role_panel(self.state,self.role,self.state['EquipmentContainer']['Datas'])['panel']
        self.assertEqual(panel,self.data['expectedPanel'])
        for key,value in self.data['oldPanel'].items():
            if key!='CriticalRate':self.assertEqual(panel[key],value)

    def test_under_target_real_equipment_is_rejected_by_the_search(self):
        result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertEqual(result['results'],[])
        accepted=self.search(98.6)
        self.assertTrue(accepted['searchComplete'])
        self.assertEqual(accepted['results'][0]['panel'],self.data['expectedPanel'])

    def test_old_imprint_formula_reproduces_the_exact_wrong_panel_and_false_pass(self):
        # 旧公式对本例等效于额外叠加一档 2.8%，独立复现截图右侧的101.4%。
        ref=stats.tables()['imprint']['5S_Self_CriticalRate']
        with patch.dict(ref,{'base':.028}):
            result=self.search()
        self.assertEqual(result['results'][0]['panel'],self.data['oldPanel'])

    def test_replacing_with_an_actual_higher_crit_item_can_reach_the_target(self):
        self.state['EquipmentContainer']['Datas'].append(self.data['alternativeHelmet'])
        result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertTrue(result['results'])
        self.assertTrue(all(c['panel']['CriticalRate']>=100 for c in result['results']))
        self.assertTrue(all(c['equipIds']['helmet']=='higher-crit-helmet' for c in result['results']))


class PassiveSkillFieldTests(unittest.TestCase):
    def test_passive_dynamic_fields_and_hidden_skill_ids_are_exported(self):
        roles=[{'ID':'hero','IsHero':'1','SkillIDs':'passive~active~hidden:Hide'}]
        skills=[{'ID':'passive','SkillType':'Passive','DynamicField2':'CriticalRate#0.2',
                 'DynamicField3':'ResistanceRate#0.2','DynamicField4':'Fun#{Fun:"AddStatus"}'},
                {'ID':'active','DynamicField1':'CriticalRate#0.9','PassiveProp.DynamicField1':'EffectHitRate#0.3'},
                {'ID':'hidden','PassiveProp.DynamicField1':'CriticalDamageRate#0.1'}]
        self.assertEqual(gen_stat_tables.skill_passives(roles,skills),
                         {'hero':{'CriticalRate':.2,'ResistanceRate':.2,'EffectHitRate':.3,'CriticalDamageRate':.1}})

    def test_level_export_keeps_actual_thresholds_and_excludes_active_attack_fields(self):
        roles=[{'ID':'hero','IsHero':'1','SkillIDs':'passive~active'}]
        skills=[{'ID':'passive','SkillType':'Passive'},{'ID':'active','SkillType':''}]
        levels=[{'SkillID':'passive','LV':'2','DynamicField2':'CriticalRate#0.01'},
                {'SkillID':'passive','LV':'8','DynamicField2':'CriticalRate#0.02'},
                {'SkillID':'active','LV':'2','DynamicField1':'CriticalRate#0.9'},
                {'SkillID':'active','LV':'3','PassiveProp.DynamicField1':'ResistanceRate#0.1'}]
        self.assertEqual(gen_stat_tables.skill_level_passives(roles,skills,levels),
                         {'hero':{'passive':[{'lv':2,'CriticalRate':.01},{'lv':8,'CriticalRate':.02}],
                                  'active':[{'lv':3,'ResistanceRate':.1}]}})


class DailianSkillLevelTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((Path(__file__).parent/'fixtures'/'dailian_skill_levels_plan.json').read_text())
        self.state=self.data['state'];self.role=self.state['RoleDataContainer']['Roles'][0]

    def search(self):
        return auto.search(self.state,self.role,sets=['Lifesteal','Resist'],critical_target=100,
            conditions=[{'stat':'ResistanceRate','min':100}],include_worn=True,complete=True)

    def set_passive_level(self,level):
        next(r for r in self.role['Skills']['Skills'] if r['StaticID']=='H192S2')['Level']=level

    def test_actual_six_items_and_raw_role_skills_match_every_game_panel_value(self):
        panel=stats.role_panel(self.state,self.role,self.state['EquipmentContainer']['Datas'])
        self.assertEqual(panel['panel'],self.data['expectedPanel'])
        self.assertCountEqual(panel['sets'],['Lifesteal','Resist'])
        self.assertEqual(next(s['Level'] for s in self.role['Skills']['Skills'] if s['StaticID']=='H192S2'),8)

    def test_same_conditions_now_find_the_real_100_crit_107_resist_plan(self):
        result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertEqual(len(result['results']),1)
        self.assertEqual(result['results'][0]['panel'],self.data['expectedPanel'])

    def test_omitting_both_passive_fields_reproduces_the_false_no_equipment_result(self):
        with patch.dict(stats.tables()['skillPassives'],{'H192':{}}), \
             patch.dict(stats.tables()['skillPassiveLevels'],{'H192':{}}):
            panel=stats.role_panel(self.state,self.role,self.state['EquipmentContainer']['Datas'])['panel']
            self.assertEqual(panel,self.data['oldPanel'])
            result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertEqual(result['results'],[])
        self.assertEqual(result['bestPossible'],{'CriticalRate':70,'ResistanceRate':87})

    def test_strengthening_follows_the_actual_skill_level(self):
        for level,crit in ((1,.35),(2,.36),(5,.39),(8,.45)):
            with self.subTest(level=level):
                self.set_passive_level(level)
                mods=stats.role_mods(self.state,self.role)
                self.assertAlmostEqual(mods['CriticalRate'],crit-.15)
                self.assertAlmostEqual(mods['ResistanceRate'],.38)
        self.set_passive_level(1)
        self.assertEqual(stats.role_panel(self.state,self.role,self.state['EquipmentContainer']['Datas'])['panel']['CriticalRate'],90)
        self.assertFalse(self.search()['results'])

    def test_missing_or_other_roles_skill_levels_are_not_assumed_to_be_maximum(self):
        for value in ({},{'Skills':[{'StaticID':'H175S2','Level':6}]}):
            with self.subTest(value=value):
                self.role['Skills']=value
                self.assertFalse(self.search()['results'])
                self.assertAlmostEqual(stats.role_mods(self.state,self.role)['CriticalRate'],.2)


class SkillPassiveTests(unittest.TestCase):
    def test_generator_only_includes_direct_permanent_stats_of_owned_role_skills(self):
        roles = [{'ID':'hero','IsHero':'1','SkillIDs':'S1~S2~S1'},
                 {'ID':'boss','IsHero':'0','SkillIDs':'S1'}]
        skills = [
            {'ID':'S1','PassiveProp.DynamicField1':'CriticalRate#0.15',
             'PassiveProp.DynamicField2':'EffectHitRate#0.3',
             'DynamicField1':'ResistanceRate#9'},
            {'ID':'S2','PassiveProp.DynamicField1':'Fun#{Trigger:"StartBattle",CriticalRate:0.9}',
             'PassiveProp.DynamicField2':'CounterSkill#S3',
             'PassiveProp.DynamicField3':'ResistanceRate#nan'},
            {'ID':'not-a-role-skill','PassiveProp.DynamicField1':'CriticalRate#1'},
        ]
        self.assertEqual(gen_stat_tables.skill_passives(roles,skills),
                         {'hero':{'CriticalRate':.15,'EffectHitRate':.3}})

    def test_hit_and_resistance_passives_enter_the_same_stat_pipeline(self):
        for sid,stat,value in (('H006','EffectHitRate',.3),('H156','ResistanceRate',.2),
                               ('H609','CriticalRate',.25),('H609','EffectHitRate',.5)):
            with self.subTest(role=sid,stat=stat):
                role={'_id':'hero','StaticID':sid,'LV':60}
                self.assertAlmostEqual(stats.role_mods({},role).get(stat,0),value)


class LunaEquipmentPlanTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((Path(__file__).parent/'fixtures'/'luna_equipment_plan.json').read_text())
        self.state=self.data['state']
        self.role=self.state['RoleDataContainer']['Roles'][0]
        self.items={e['_id']:e for e in self.state['EquipmentContainer']['Datas']}
        self.game=[self.items[eid] for eid in self.data['gamePlan'].values()]

    def search(self):
        return auto.search(self.state,self.role,sets=['Destruction','Penetration'],
            conditions=[{'stat':'Speed','min':184}],critical_target=100,include_worn=True,complete=True)

    def test_bare_and_six_piece_panels_match_every_screenshot_value(self):
        self.assertEqual(stats.role_panel(self.state,self.role,[])['panel'],self.data['expectedBare'])
        self.assertEqual(stats.role_panel(self.state,self.role,self.game)['panel'],self.data['expectedPlan'])
        self.assertAlmostEqual(auto.metrics(self.data['expectedPlan'],100)['damage'],10826.72)

    def test_old_15_percent_omission_reproduces_the_exact_first_three_rows(self):
        with patch.dict(stats.tables()['skillPassives'],{'H606':{}}):
            self.assertEqual(stats.role_panel(self.state,self.role,self.game)['panel']['CriticalRate'],85)
            result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertEqual([c['panel'] for c in result['results'][:3]],self.data['expectedOldTop3'])

    def test_other_roles_items_are_available_and_best_185_beats_game_plan(self):
        allowed=auto.candidate_pool(self.state,self.role,only_max=True,include_worn=True)
        blocked=auto.candidate_pool(self.state,self.role,only_max=True,include_worn=False)
        gameids=set(self.data['gamePlan'].values())
        self.assertTrue(gameids.issubset({e['_id'] for e in allowed}))
        self.assertFalse(gameids.issubset({e['_id'] for e in blocked}))
        result=self.search()
        self.assertTrue(result['searchComplete'])
        self.assertEqual(len(result['results']),len({c['panel']['Speed'] for c in result['results']}))
        self.assertGreaterEqual(next(c for c in result['results'] if c['panel']['Speed']==185)['metrics']['damage'],10826.72)
        self.assertTrue(all(c['panel']['CriticalRate']>=100 for c in result['results']))

    def test_all_actual_item_combinations_match_an_independent_enumeration(self):
        options=[[e for e in self.items.values() if plans.equip_slot(e)==s] for s in plans.SLOTS]
        feasible=[]
        for es in itertools.product(*options):
            row=stats.role_panel(self.state,self.role,list(es));p=row['panel']
            if p['Speed']>=184 and p['CriticalRate']>=100 and set(row['sets'])=={'Destruction','Penetration'}:
                feasible.append({'panel':p,'metrics':auto.metrics(p,100)})
        expected=speed_winners(feasible)
        self.assertEqual({c['panel']['Speed']:c['metrics']['damage'] for c in self.search()['results']},
                         {c['panel']['Speed']:c['metrics']['damage'] for c in expected})


class RealEquipmentPlanTests(unittest.TestCase):
    """用户实际两套装备的九件物品，不能用虚构六件属性替代。"""
    def setUp(self):
        self.data = json.loads((Path(__file__).parent / 'fixtures' / 'h121_equipment_plans.json').read_text())
        self.state = self.data['state']
        self.role = self.state['RoleDataContainer']['Roles'][0]
        self.items = {e['_id']:e for e in self.state['EquipmentContainer']['Datas']}

    def test_both_game_plans_have_six_distinct_correct_slots(self):
        for index, ids in self.data['plans'].items():
            with self.subTest(plan=index):
                self.assertEqual([plans.equip_slot(self.items[eid]) for eid in ids],list(plans.SLOTS))
                for e in self.items.values(): e['EquipRole'] = 'role' if e['_id'] in ids else ''
                worn,conflicts = plans.worn_items(list(self.items.values()),'role')
                self.assertFalse(conflicts)
                self.assertEqual(len(worn),6)

    def test_both_panels_match_every_game_value(self):
        for index, ids in self.data['plans'].items():
            with self.subTest(plan=index):
                actual = stats.role_panel(self.state,self.role,[self.items[eid] for eid in ids])
                self.assertEqual(actual['panel'],self.data['expected'][index])
                self.assertCountEqual(actual['sets'],['Speed','Critical'])

    def test_artifact_interpolation_truncates_before_panel_addition(self):
        mods = stats.role_mods(self.state,self.role)
        self.state['Artifacts']['Datas'].clear()
        no_artifact = stats.role_mods(self.state,self.role)
        self.assertEqual(mods['AttackValue']-no_artifact['AttackValue'],142)
        self.assertEqual(mods['HPValue']-no_artifact['HPValue'],217)

    def test_complete_search_matches_all_eight_real_combinations(self):
        # 按截图给出的真实部位独立分组，避免参考枚举也使用待检验的部位识别。
        choices = [[self.items[eid] for eid in ids] for ids in (
            ('plan1-weapon','plan2-weapon'),('plan1-helmet','plan2-helmet'),('shared-armor',),
            ('plan1-necklace','plan2-necklace'),('shared-ring',),('shared-boots',))]
        rows = []
        for es in itertools.product(*choices):
            row = stats.role_panel(self.state,self.role,list(es))
            p = row['panel']
            if 203<=p['Speed']<=225 and set(row['sets'])=={'Speed','Critical'}:
                rows.append({'panel':p,'metrics':auto.metrics(p,85)})
        for objective in ('damage','survival'):
            feasible = [r for r in rows if objective=='survival' or r['panel']['CriticalRate']>=85]
            for secondary in ([],['Speed'],['EffectHitRate'],['ResistanceRate']):
                with self.subTest(objective=objective,secondary=secondary):
                    actual = auto.search(self.state,self.role,sets=['Speed','Critical'],
                        conditions=[{'stat':'Speed','min':203,'max':225}],critical_target=85,
                        objective=objective,secondary=secondary,complete=True)
                    expected = speed_winners(feasible,objective,secondary,85)
                    self.assertTrue(actual['searchComplete'])
                    self.assertEqual({r['panel']['Speed']:r['metrics'][objective] for r in actual['results']},
                                     {r['panel']['Speed']:r['metrics'][objective] for r in expected})
                    if objective=='damage':
                        speed220 = next(r for r in actual['results'] if r['panel']['Speed']==220)
                        self.assertGreaterEqual(speed220['metrics']['damage'],8568)

    def test_new_event_equipment_slots_do_not_follow_id_suffix(self):
        expected = {'E0462':'weapon','E0463':'helmet','E0464':'helmet','E0465':'weapon',
                    'E0466':'weapon','E0467':'weapon','E0468':'weapon'}
        for sid,slot in expected.items():
            with self.subTest(sid=sid): self.assertEqual(plans.equip_slot({'StaticID':sid}),slot)
        with patch.dict(stats.tables()['parts'],{'E0464':'armor'}):
            self.assertEqual(plans.equip_slot({'StaticID':'E0464'}),'armor')

    def test_unknown_eligible_event_item_stops_instead_of_silent_omission(self):
        unknown = {'_id':'unknown-event','StaticID':'E0494','LV':15,'EquipRole':''}
        self.state['EquipmentContainer']['Datas'].append(unknown)
        self.assertIsNone(plans.equip_slot(unknown))
        with self.assertRaisesRegex(auto.BuildError,'E0494'):
            auto.search(self.state,self.role,complete=True)
        # 等级、占用、排除规则先执行；不可用的未知物品不阻止本轮搜索。
        for update,options in (({'LV':0},{}),({'LV':15,'EquipRole':'other'},{}),
                               ({'EquipRole':''},{'exclude':['unknown-event']})):
            unknown.update(update)
            pool = auto.candidate_pool(self.state,self.role,only_max=True,include_worn=False,**options)
            self.assertEqual(len(pool),9)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.state,self.roles=fixture(3)
        self.client=SimpleNamespace(account_state=self.state)
        self.acc=SimpleNamespace(client=self.client,lock=threading.Lock())
    def handle(self,action,**body):return api.handle(self.acc,action,body,lambda sid:None)
    def start(self):return self.handle('autoMulti',entries=[config('r0'),config('r1')],includeWorn=True)

    def wait_search(self, started):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            value = self.handle('searchStatus',searchId=started['searchId'])
            if value['status'] not in ('running','cancelling'): return value
            time.sleep(.005)
        self.fail('background search did not complete')

    def test_background_complete_search_and_server_owned_result(self):
        with patch.object(auto,'NODE_LIMIT',1):
            started = self.handle('autoMulti',entries=[config('r0')],includeWorn=True,background=True)
            value = self.wait_search(started)
        self.assertEqual(value['status'],'done')
        batch = value['result']
        self.assertTrue(batch['options']['complete'])
        self.assertTrue(batch['entries'][0]['searchComplete'])
        self.assertGreater(batch['entries'][0]['nodes'],1)
        self.assertEqual(batch['batchId'],self.acc.equipment_batch['id'])
        with self.assertRaisesRegex(GameError,'失效'):
            self.handle('searchStatus',searchId='other-job')

    def test_background_stop_is_available_while_account_lock_is_held(self):
        entered = threading.Event()
        def long_search(*args,**kwargs):
            self.assertTrue(kwargs['complete'])
            kwargs['progress']({'nodes':123,'speedCount':2,'roleIndex':1,'roleCount':1})
            entered.set()
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                kwargs['progress']({'nodes':123,'speedCount':2,'roleIndex':1,'roleCount':1})
                if kwargs['cancelled'](): raise auto.SearchCancelled('已停止计算')
                time.sleep(.005)
            self.fail('cancel signal was not received')
        with patch.object(auto,'search_multi',side_effect=long_search):
            started = self.handle('autoMulti',entries=[config('r0')],background=True)
            self.assertTrue(entered.wait(1))
            running = self.handle('searchStatus',searchId=started['searchId'])
            self.assertEqual(running['progress']['nodes'],123)
            with self.assertRaisesRegex(GameError,'正在进行'):
                self.handle('autoMulti',entries=[config('r0')],background=True)
            self.handle('cancelSearch',searchId=started['searchId'])
            stopped = self.wait_search(started)
        self.assertEqual(stopped['status'],'cancelled')
        self.assertNotIn('result',stopped)
        self.assertIsNone(getattr(self.acc,'equipment_batch',None))

    def test_background_errors_are_returned_without_partial_results(self):
        started = self.handle('autoMulti',entries=[],background=True)
        value = self.wait_search(started)
        self.assertEqual(value['status'],'failed')
        self.assertIn('请选择',value['error'])
        self.assertNotIn('result',value)

    def test_candidate_beyond_first_display_page_can_be_selected_and_equipped(self):
        # 以二进制速度贡献产生64个不同显示速度，验证分组后的分页选择。
        state,roles = fixture(2)
        for k,e in enumerate(state['EquipmentContainer']['Datas']):
            slot,j = divmod(k,2)
            e.update(Set='Health',EquipRole='',MainProp={'PropertyType':'SpeedValue','Value':(2**slot)*j},
                     SubProps={'SourceValues':[{'PropertyType':'CriticalRate','Value':.15}]})
        self.client.account_state = state
        batch = self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        self.assertGreater(len(batch['entries'][0]['results']),30)
        chosen = self.handle('chooseMulti', batchId=batch['batchId'], index=0, pick=35)
        expected = list(chosen['entries'][0]['results'][35]['equipIds'].values())
        def equip(client, rid, ids, verify): plans._assume_worn(client, rid, ids)
        with patch.object(plans,'equip_to_role',side_effect=equip) as wear:
            result = self.handle('equipMulti', batchId=batch['batchId'], index=0)
        wear.assert_called_once_with(self.client,'r0',expected,verify=True)
        self.assertTrue(result['entries'][0]['done'])
        self.assertEqual(result['entries'][0]['pick'],35)

    def test_new_round_invalidates_old_results_without_changing_equipment(self):
        batch = self.start()
        state = copy.deepcopy(self.client.account_state)
        result = self.handle('newRound')
        self.assertIsNone(self.acc.equipment_batch)
        self.assertEqual(self.client.account_state, state)
        self.assertIn('overview', result)
        for action in ('chooseMulti', 'equipMulti', 'saveMultiPlan'):
            with self.assertRaisesRegex(GameError, '失效'):
                self.handle(action, batchId=batch['batchId'], index=0, pick=0, planIndex=0)
        self.handle('newRound')
        self.assertEqual(self.client.account_state, state)

    def test_batch_snapshot_prefix_and_server_owned_selections(self):
        batch=self.start();saved=copy.deepcopy(self.acc.equipment_batch['state'])
        chosen=self.handle('chooseMulti',batchId=batch['batchId'],index=0,pick=1,equipIds=['forged'])
        self.assertEqual(chosen['entries'][0]['results'],batch['entries'][0]['results'])
        self.assertEqual(self.acc.equipment_batch['state'],saved)
        api._validate_results(chosen['entries'])
        with self.assertRaises(GameError):self.handle('chooseMulti',batchId=batch['batchId'],index=0,pick=99999)
        self.state['EquipmentContainer']['Datas'][0]['LV']=14
        with self.assertRaisesRegex(GameError,'变化'):self.handle('chooseMulti',batchId=batch['batchId'],index=0,pick=0)

    def test_equip_sequence_lock_and_confirmation(self):
        batch=self.start()
        with self.assertRaisesRegex(GameError,'顺序'):self.handle('equipMulti',batchId=batch['batchId'],index=1)
        def fake_equip(client,rid,ids,verify):plans._assume_worn(client,rid,ids)
        with patch.object(plans,'equip_to_role',side_effect=fake_equip),patch.object(plans,'overview',return_value={'roles':[],'equips':[]}):
            result=self.handle('equipMulti',batchId=batch['batchId'],index=0)
            self.assertTrue(result['entries'][0]['done'])
            with self.assertRaisesRegex(GameError,'锁定'):self.handle('chooseMulti',batchId=batch['batchId'],index=0,pick=1)
            after=self.handle('chooseMulti',batchId=batch['batchId'],index=1,pick=1)
            self.assertEqual(after['entries'][0],result['entries'][0])
            final=self.handle('equipMulti',batchId=batch['batchId'],index=1)
            self.assertTrue(final['entries'][1]['done']);api._validate_results(final['entries'])

    def test_unconfirmed_equip_not_marked_success_and_no_retry(self):
        batch=self.start()
        with patch.object(plans,'equip_to_role',return_value={}):
            with self.assertRaises(GameError):self.handle('equipMulti',batchId=batch['batchId'],index=0)
            self.assertFalse(self.acc.equipment_batch['results'][0]['done'])
            with self.assertRaisesRegex(GameError,'变化'):self.handle('equipMulti',batchId=batch['batchId'],index=0)

    def test_four_saved_slots_then_equip(self):
        for slot in range(4):
            self.setUp()
            batch = self.start()
            expected = list(batch['entries'][0]['results'][0]['equipIds'].values())
            def fake_equip(client, rid, ids, verify): plans._assume_worn(client, rid, ids)
            with patch.object(plans, 'save_plan', return_value={'verified':True}) as save, patch.object(plans, 'equip_to_role', side_effect=fake_equip), patch.object(plans, 'overview', return_value={'roles':[], 'equips':[]}):
                result = self.handle('saveMultiPlan', batchId=batch['batchId'], index=0, planIndex=slot, equipIds=['forged'])
                save.assert_called_once_with(self.client, 'r0', slot, expected)
                self.assertTrue(result['saved']['verified'])
                self.assertTrue(result['entries'][0]['done'])
                api._validate_results(result['entries'])

    def test_unverified_plan_does_not_equip(self):
        batch = self.start()
        with patch.object(plans, 'save_plan', return_value={'verified':False}), patch.object(plans, 'equip_to_role') as equip:
            result = self.handle('saveMultiPlan', batchId=batch['batchId'], index=0, planIndex=0)
            equip.assert_not_called()
            self.assertFalse(result['entries'][0]['done'])
        with self.assertRaises(GameError):self.handle('saveMultiPlan', batchId=batch['batchId'], index=0, planIndex=4)

    def test_removed_routes_and_alias_data(self):
        for action in ('auto','plans','savePlan','panel','takeOff','equip'):
            with self.assertRaises(GameError):self.handle(action)
        aliases=api._aliases();self.assertIn('团长',aliases['H001']['aliases'])
        js=(Path(__file__).resolve().parents[1]/'web/equipment.js').read_text()
        self.assertNotIn('eqpManual',js);self.assertNotIn('runAuto',js)


class SurvivalCriticalFilterTests(unittest.TestCase):
    def test_full_survival_search_matches_exhaustive_critical_range_filter(self):
        state, roles = fixture(3)
        role = roles['r0']
        pool = state['EquipmentContainer']['Datas']
        options = [[e for e in pool if plans.equip_slot(e) == slot] for slot in plans.SLOTS]
        feasible = []
        for items in itertools.product(*options):
            p = stats.role_panel(state, role, items)['panel']
            if 100 <= p['CriticalRate'] <= 110:
                feasible.append((p['Speed'], p['HP']*(1+p['Defence']/300)))
        self.assertTrue(feasible)
        expected = {speed:max(value for s,value in feasible if s==speed) for speed,_ in feasible}
        for secondary in ([], ['Speed'], ['ResistanceRate']):
            result = auto.search(state, role, objective='survival', include_worn=True, complete=True,
                                 conditions=[{'stat':'CriticalRate','min':100,'max':110}], secondary=secondary)
            self.assertTrue(result['searchComplete'])
            self.assertEqual({c['panel']['Speed']:c['metrics']['survival'] for c in result['results']}, expected)
            self.assertTrue(all(100<=c['panel']['CriticalRate']<=110 for c in result['results']))

    def test_output_target_and_explicit_critical_range_are_intersected(self):
        state, roles = fixture(3)
        for bounds, found in (({'min':90,'max':110},True),({'max':99},False)):
            conditions = [{'stat':'CriticalRate', **bounds}]
            before = copy.deepcopy(conditions)
            result = auto.search(state,roles['r0'],include_worn=True,complete=True,
                                 critical_target=100,conditions=conditions)
            self.assertEqual(bool(result['results']), found)
            self.assertTrue(all(100<=c['panel']['CriticalRate']<=110 for c in result['results']))
            self.assertEqual(conditions,before)


class ExcludedRoleTests(unittest.TestCase):
    def setUp(self):
        self.state,self.roles=fixture(3)
        self.client=SimpleNamespace(account_state=self.state)
        self.acc=SimpleNamespace(client=self.client,lock=threading.Lock())

    def handle(self,action,**body):
        return api.handle(self.acc,action,body,lambda sid:None)

    def test_protected_owner_overrides_selected_roles_and_include_worn(self):
        for include_worn in (False,True):
            pool=auto.candidate_pool(self.state,self.roles['r0'],only_max=True,include_worn=include_worn,
                                     free_roles={'r1','r2'},excluded_roles=['r0','r1'])
            self.assertTrue(pool)
            self.assertEqual({plans._oid(e['EquipRole']) for e in pool},{'r2'})

    def test_full_search_matches_enumeration_and_never_seeds_protected_current_wear(self):
        pool=[e for e in self.state['EquipmentContainer']['Datas'] if e['EquipRole']!='r0']
        options=[[e for e in pool if plans.equip_slot(e)==s] for s in plans.SLOTS]
        for objective in ('damage','survival'):
            expected={}
            for items in itertools.product(*options):
                p=stats.role_panel(self.state,self.roles['r0'],items)['panel']
                if objective=='damage' and p['CriticalRate']<50:continue
                value=auto.metrics(p,50)[objective]
                expected[p['Speed']]=max(expected.get(p['Speed'],0),value)
            result=auto.search(self.state,self.roles['r0'],critical_target=50,objective=objective,
                               include_worn=True,excluded_roles=['r0'],complete=True)
            self.assertTrue(result['searchComplete'])
            self.assertEqual({c['panel']['Speed']:c['metrics'][objective] for c in result['results']},expected)
            owners={plans._oid(e['_id']):plans._oid(e['EquipRole']) for e in self.state['EquipmentContainer']['Datas']}
            self.assertTrue(all(owners[eid]!='r0' for c in result['results'] for eid in c['equipIds'].values()))

    def test_exclusions_accumulate_invalidate_old_batches_and_survive_reconfiguration(self):
        before=copy.deepcopy(self.state)
        old=self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        first=self.handle('excludeRole',roleId='r1')
        self.assertEqual([r['id'] for r in first['overview']['excludedRoles']],['r1'])
        for action in ('chooseMulti','equipMulti','saveMultiPlan'):
            with self.assertRaisesRegex(GameError,'失效'):
                self.handle(action,batchId=old['batchId'],index=0,pick=0,planIndex=0)
        self.handle('excludeRole',roleId='r2')
        self.handle('excludeRole',roleId='r1')
        self.assertEqual([r['id'] for r in self.handle('overview')['excludedRoles']],['r1','r2'])
        for objective in ('damage','survival'):
            cfg={**config('r0'),'objective':objective,'conditions':[{'stat':'Speed','min':1}]}
            batch=self.handle('autoMulti',entries=[cfg],includeWorn=True,excludedRoles=[])
            self.assertEqual(batch['options']['excluded_roles'],['r1','r2'])
            self.assertEqual([r['id'] for r in batch['excludedRoles']],['r1','r2'])
            self.assertTrue(batch['entries'][0]['results'])
            self.assertTrue(all(eid.endswith('-0') for c in batch['entries'][0]['results'] for eid in c['equipIds'].values()))
        self.client.bootstrap=lambda:None
        self.assertEqual([r['id'] for r in self.handle('reload')['excludedRoles']],['r1','r2'])
        self.assertEqual(self.state,before)

    def test_changed_exclusions_block_old_selection_even_if_batch_was_retained(self):
        batch=self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        self.acc.equipment_excluded_roles=['r1']
        for action in ('chooseMulti','equipMulti','saveMultiPlan'):
            with self.assertRaisesRegex(GameError,'排除角色已变化'):
                self.handle(action,batchId=batch['batchId'],index=0,pick=0,planIndex=0)

    def test_exclusions_apply_to_every_character_and_reselection(self):
        self.handle('excludeRole',roleId='r2')
        cfgs=[config('r0'),config('r1')]
        batch=self.handle('autoMulti',entries=cfgs,includeWorn=True)
        picked=self.handle('chooseMulti',batchId=batch['batchId'],index=0,pick=0)
        for entry in picked['entries']:
            self.assertTrue(all(not eid.endswith('-2') for c in entry.get('results',[]) for eid in c['equipIds'].values()))
        api._validate_results(picked['entries'])

    def test_only_new_round_clears_exclusions_and_accounts_are_isolated(self):
        self.handle('excludeRole',roleId='r0')
        other=SimpleNamespace(client=SimpleNamespace(account_state=copy.deepcopy(self.state)),lock=threading.Lock())
        self.assertEqual(api.handle(other,'overview',{},lambda sid:None)['excludedRoles'],[])
        before=copy.deepcopy(self.state)
        result=self.handle('newRound')
        self.assertEqual(result['overview']['excludedRoles'],[])
        self.assertEqual(self.state,before)
        result=self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        self.assertEqual(result['options']['excluded_roles'],[])

    def test_invalid_owner_is_rejected_without_changing_round(self):
        old=self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        for value in (None,'',{},'not-in-account'):
            with self.assertRaisesRegex(GameError,'有效'):
                self.handle('excludeRole',roleId=value)
        self.assertEqual(self.acc.equipment_batch['id'],old['batchId'])
        self.assertEqual(self.handle('overview')['excludedRoles'],[])

    def test_start_applies_all_pending_exclusions_in_one_search(self):
        before=copy.deepcopy(self.state)
        self.assertEqual(self.handle('overview')['excludedRoles'],[])
        batch=self.handle('autoMulti',entries=[config('r0')],includeWorn=True,
                          excludedRoleIds=['r1','r2','r1'])
        self.assertEqual(batch['options']['excluded_roles'],['r1','r2'])
        self.assertTrue(batch['entries'][0]['results'])
        self.assertTrue(all(eid.endswith('-0') for c in batch['entries'][0]['results'] for eid in c['equipIds'].values()))
        self.assertEqual(self.state,before)
        self.assertEqual(self.handle('autoMulti',entries=[config('r0')],includeWorn=True,
                                     excludedRoleIds=[])['options']['excluded_roles'],['r1','r2'])

    def test_invalid_bulk_exclusions_do_not_partially_change_the_round(self):
        batch=self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        for value in (None,'r1',[{}],['r1','not-in-account']):
            with self.assertRaisesRegex(GameError,'无效角色'):
                self.handle('autoMulti',entries=[config('r0')],includeWorn=True,excludedRoleIds=value)
        self.assertEqual(self.handle('overview')['excludedRoles'],[])
        self.assertEqual(self.acc.equipment_batch['id'],batch['batchId'])

    def test_failed_recalculation_keeps_the_applied_bulk_exclusions(self):
        self.handle('autoMulti',entries=[config('r0')],includeWorn=True)
        with patch.object(auto,'search_multi',side_effect=auto.SearchCancelled('计算已停止')):
            with self.assertRaisesRegex(GameError,'计算已停止'):
                self.handle('autoMulti',entries=[config('r0')],includeWorn=True,excludedRoleIds=['r1','r2'])
        self.assertEqual([r['id'] for r in self.handle('overview')['excludedRoles']],['r1','r2'])
        self.assertIsNone(self.acc.equipment_batch)


class CurrentEquipmentTests(unittest.TestCase):
    def test_overview_summarizes_actual_equipment_including_below_max_and_partial_wear(self):
        state, roles = fixture(2)
        owned = [e for e in state['EquipmentContainer']['Datas'] if e['EquipRole']=='r0']
        owned[0]['LV'] = 13
        for items in (owned, owned[:-1]):
            with self.subTest(count=len(items)):
                owned[-1]['EquipRole'] = 'r0' if len(items)==6 else ''
                client = SimpleNamespace(account_state=state)
                acc = SimpleNamespace(client=client,lock=threading.Lock())
                view = api.handle(acc,'overview',{},lambda sid:None)
                current = next(r['current'] for r in view['roles'] if r['id']=='r0')
                expected = stats.role_panel(state,roles['r0'],items)
                self.assertEqual((current['panel'],current['sets'],current['count']),
                                 (expected['panel'],expected['sets'],len(items)))
                self.assertEqual(current['aux'],sum(e['auxScore'] for e in view['equips'] if e['role']=='r0'))
                empty = next(r for r in view['roles'] if r['id']=='r5')['current']
                self.assertEqual(empty['count'],0)
                self.assertEqual(empty['panel'],stats.role_panel(state,roles['r5'],[])['panel'])

    def test_duplicate_slot_uses_effective_item_and_warns(self):
        state, roles = fixture(2)
        extra = copy.deepcopy(state['EquipmentContainer']['Datas'][0])
        extra.update(_id={'$oid':'extra'},MainProp={'PropertyType':'AttackValue','Value':999})
        state['EquipmentContainer']['Datas'].append(extra)
        acc=SimpleNamespace(client=SimpleNamespace(account_state=state),lock=threading.Lock())
        role=next(r for r in api.handle(acc,'overview',{},lambda sid:None)['roles'] if r['id']=='r0')
        worn,_=plans.worn_items(state['EquipmentContainer']['Datas'],'r0')
        self.assertEqual(role['current']['panel'],stats.role_panel(state,roles['r0'],worn)['panel'])
        self.assertEqual((role['current']['count'],role['worn']),(6,7))
        self.assertIn('重复',role['currentWarning'])


class TakeOffAllTests(unittest.TestCase):
    def setUp(self):
        state, _ = fixture(2)
        spare = copy.deepcopy(state['EquipmentContainer']['Datas'][0])
        spare.update(_id={'$oid': 'spare'}, EquipRole='')
        state['EquipmentContainer']['Datas'].append(spare)
        self.server = copy.deepcopy(state)
        self.calls = []
        self.client = SimpleNamespace(account_state=state, _auth_data=lambda data: data)

        def bootstrap():
            self.client.account_state = copy.deepcopy(self.server)
        self.client.bootstrap = bootstrap

        def call(route, data):
            self.calls.append((route, data))
            equip = next(e for e in self.server['EquipmentContainer']['Datas'] if plans._oid(e['_id']) == data['EquipID'])
            self.assertEqual(plans._oid(equip['EquipRole']), data['RoleID'])
            equip['EquipRole'] = ''
            return {'NewEquip': copy.deepcopy(equip)}
        self.client.call = call

    def test_unloads_every_role_from_latest_snapshot_and_leaves_spares(self):
        # 当前页面的快照过期，批量操作仍须同步后处理全部穿戴者。
        for equip in self.client.account_state['EquipmentContainer']['Datas']:
            equip['EquipRole'] = ''
        self.server['EquipmentContainer']['Datas'][0]['EquipRole'] = {'$oid': 'other-role'}
        result = plans.take_off_all(self.client)
        self.assertTrue(result['verified'])
        self.assertEqual((result['total'], result['removed'], result['remaining'], result['roleCount']), (12, 12, 0, 3))
        self.assertEqual(len(self.calls), 12)
        self.assertTrue(all(route == 'EquipmentHandler.TakeOffEquip' and data['UpdateNewIDs'] == [] for route, data in self.calls))
        self.assertNotIn('spare', [data['EquipID'] for _, data in self.calls])

    def test_missing_response_is_confirmed_by_refresh(self):
        real_call = self.client.call
        def call(route, data):
            real_call(route, data)
            return {'Utc': 0}
        self.client.call = call
        self.assertTrue(plans.take_off_all(self.client)['verified'])
        self.assertEqual(len(self.calls), 12)

    def test_unconfirmed_response_skips_item_and_continues_without_assuming_removed(self):
        self.client.call = lambda route, data: self.calls.append((route, data)) or {'Utc': 0}
        result = plans.take_off_all(self.client)
        self.assertFalse(result['verified'])
        self.assertEqual((result['removed'], result['remaining']), (0, 12))
        self.assertEqual(len(self.calls), 12)
        self.assertIn('尚未确认', result['error'])

    def test_partial_failure_continues_and_reports_actual_server_state(self):
        real_call = self.client.call
        def call(route, data):
            if len(self.calls) == 2:
                # 请求已经生效，但回包丢失，最终回读应统计为已卸下。
                real_call(route, data)
                raise GameError('连接中断')
            return real_call(route, data)
        self.client.call = call
        result = plans.take_off_all(self.client)
        self.assertTrue(result['verified'])
        self.assertEqual((result['removed'], result['remaining']), (12, 0))
        self.assertEqual(len(self.calls), 12)
        self.assertIn('连接中断', result['error'])

    def test_final_refresh_failure_is_not_reported_as_success(self):
        bootstrap = self.client.bootstrap
        count = 0
        def refresh():
            nonlocal count
            count += 1
            if count > 1: raise GameError('同步失败')
            bootstrap()
        self.client.bootstrap = refresh
        result = plans.take_off_all(self.client)
        self.assertFalse(result['verified'])
        self.assertIn('最终同步失败', result['error'])
        self.assertEqual(result['removed'], 12)

    def test_api_invalidates_batch_and_returns_updated_overview(self):
        acc = SimpleNamespace(client=self.client, lock=threading.Lock(), equipment_batch={'id': 'old'})
        result = api.handle(acc, 'takeOffAll', {}, lambda sid: None)
        self.assertIsNone(acc.equipment_batch)
        self.assertTrue(result['verified'])
        self.assertTrue(all(not equip['role'] for equip in result['overview']['equips']))

    def test_initial_sync_failure_invalidates_batch_and_sends_no_unload(self):
        self.client.bootstrap = lambda: (_ for _ in ()).throw(GameError('同步失败'))
        acc = SimpleNamespace(client=self.client, lock=threading.Lock(), equipment_batch={'id': 'old'})
        with self.assertRaisesRegex(GameError, '同步失败'):
            api.handle(acc, 'takeOffAll', {}, lambda sid: None)
        self.assertIsNone(acc.equipment_batch)
        self.assertFalse(self.calls)

    def test_no_equipment_worn_is_a_successful_noop(self):
        for equip in self.server['EquipmentContainer']['Datas']:
            equip['EquipRole'] = ''
        result = plans.take_off_all(self.client)
        self.assertTrue(result['verified'])
        self.assertEqual((result['removed'], result['roleCount'], result['remaining']), (0, 0, 0))
        self.assertFalse(self.calls)

    def test_unload_current_role_keeps_every_other_role_and_spare_unchanged(self):
        other = [copy.deepcopy(e) for e in self.server['EquipmentContainer']['Datas'] if e['EquipRole']!='r0']
        # 新快照多出的同部位归属也需要卸下，不能只处理摘要的六件。
        extra=copy.deepcopy(self.server['EquipmentContainer']['Datas'][0])
        extra['_id']={'$oid':'extra'}
        self.server['EquipmentContainer']['Datas'].append(extra)
        acc=SimpleNamespace(client=self.client,lock=threading.Lock(),equipment_batch={'id':'old'})
        result=api.handle(acc,'takeOffRole',{'roleId':'r0'},lambda sid:None)
        self.assertTrue(result['verified'])
        self.assertEqual((result['total'],result['removed'],result['remaining']),(7,7,0))
        self.assertTrue(all(data['RoleID']=='r0' for _,data in self.calls))
        self.assertEqual([e for e in self.server['EquipmentContainer']['Datas'] if e['EquipRole']],other[:-1])
        self.assertEqual(self.server['EquipmentContainer']['Datas'][12],other[-1])
        self.assertIsNone(acc.equipment_batch)
        role=next(r for r in result['overview']['roles'] if r['id']=='r0')
        self.assertEqual(role['current']['count'],0)
        self.assertEqual(role['current']['sets'],[])

    def test_single_role_partial_failure_reports_only_target_remaining(self):
        real=self.client.call
        def fail(route,data):
            if len(self.calls)==2: raise GameError('连接中断')
            return real(route,data)
        self.client.call=fail
        result=plans.take_off_role(self.client,'r0')
        self.assertFalse(result['verified'])
        self.assertEqual((result['removed'],result['remaining']),(2,4))
        self.assertEqual(sum(e['EquipRole']=='r1' for e in self.server['EquipmentContainer']['Datas']),6)

    def test_single_role_rejects_invalid_or_disappeared_role_without_unloading(self):
        acc=SimpleNamespace(client=self.client,lock=threading.Lock(),equipment_batch={'id':'old'})
        for role_id in ('missing','',None,{}):
            with self.assertRaisesRegex(GameError,'有效'):
                api.handle(acc,'takeOffRole',{'roleId':role_id},lambda sid:None)
        self.server['RoleDataContainer']['Roles']=[]
        with self.assertRaisesRegex(GameError,'不在当前账号'):
            api.handle(acc,'takeOffRole',{'roleId':'r0'},lambda sid:None)
        self.assertIsNone(acc.equipment_batch)
        self.assertFalse(self.calls)

    def test_single_role_without_worn_items_does_not_unload_others(self):
        result=plans.take_off_role(self.client,'r5')
        self.assertTrue(result['verified'])
        self.assertEqual((result['total'],result['removed'],result['remaining']),(0,0,0))
        self.assertFalse(self.calls)


if __name__=='__main__':unittest.main()
