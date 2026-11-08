"""Abyss boss preference, subordinate to existing team composition rules."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import abyss_team as team
from backend.abyss_state import metadata
from backend.tasks import _oid
from tools.test_abyss_auto import roster


class ElementRelationships(unittest.TestCase):
    def test_all_twenty_five_pairs_match_the_requested_cycle(self):
        order=['Fire','Ice','Earth','Light','Dark']
        expected=[
            [0,-1,1,0,0],
            [1,0,-1,0,0],
            [-1,1,0,0,0],
            [0,0,0,0,1],
            [0,0,0,1,0],
        ]
        for i,a in enumerate(order):
            for j,b in enumerate(order):
                with self.subTest(attacker=a,defender=b):self.assertEqual(team.element_matchup(a,b),expected[i][j])

    def test_dark_zero_is_valid_and_unknown_elements_do_not_invent_advantage(self):
        self.assertEqual(team.element_matchup(0,4),1)
        self.assertEqual(team.element_matchup(4,0),1)
        self.assertEqual(team.element_matchup(3,1),1)
        self.assertEqual(team.element_matchup(1,2),1)
        self.assertEqual(team.element_matchup(2,3),1)
        self.assertEqual(team.element_matchup(None,'Fire'),0)
        self.assertEqual(team.element_matchup('unknown','Ice'),0)


class TeamElementPriority(unittest.TestCase):
    def select(self,elements,*,boss='Fire',kinds=None,forbidden=(),add=None,penalized=()):
        state=roster();state['RoleDataContainer']['Roles']=state['RoleDataContainer']['Roles'][:len(elements)]
        roles=state['RoleDataContainer']['Roles'];by_id={_oid(r['_id']):i for i,r in enumerate(roles)}
        model=metadata();model={**model,'tables':{**model['tables'],'Role':deepcopy(model['tables']['Role'])}}
        for r,element in zip(roles,elements):model['tables']['Role'][r['StaticID']]['Element']=element
        if penalized:
            for index,role in enumerate(roles):model['tables']['Role'][role['StaticID']]['SPType']='FightingSpirit' if index in penalized else ''
        kinds=kinds or ['single']*len(elements)
        for role,kind in zip(roles,kinds):model['tables']['Role'][role['StaticID']]['Job']='Medic' if kind=='healer' else 'Warrior'
        profile={'id':'probe','title':'输出','damageStat':'Attack','critical':True,'priorities':['damage','Speed','Attack']}
        def combat(role,**kwargs):
            kind=kinds[by_id[_oid(role['_id'])]]
            return {'kind':kind,'tier':{'single':0,'area':1,'tank':2,'healer':3}[kind],
                    'damageStat':'Attack','profile':profile,'protectsBack':False}
        traits=team.role_traits({'StaticID':'H017','LV':60})
        enemy={'buffs':set(),'debuffs':set(),'flags':set(),'immune':set(),'recovery':False,'protection':False}
        targets=[{'Element':boss,'isBoss':True}]
        if add:targets.append({'Element':add,'isBoss':False})
        rule=team.wave_policy({'Buffs':{}});rule['forbiddenElements']=set(forbidden)
        if penalized:rule['flags'].add('NoSP')
        def panel(state,role,worn,**kwargs):
            element=elements[by_id[_oid(role['_id'])]]
            # Disadvantaged units have much higher raw attack. The element
            # preference should still decide when composition is equivalent.
            attack=100000 if team.element_matchup(element,boss)<0 else 1000
            return {'panel':dict(HP=10000,Defence=1000,Attack=attack,Speed=100,CriticalRate=50,CriticalDamageRate=150,
                                  ResistanceRate=0,EffectHitRate=100)}
        with patch.object(team,'metadata',return_value=model), \
             patch.object(team,'fantasy_config',return_value={'Waves':[{},{}]}), \
             patch.object(team,'wave_policy',return_value=rule), \
             patch.object(team,'_enemy_traits',return_value=(enemy,targets)), \
             patch.object(team,'role_traits',side_effect=lambda *a,**k:deepcopy(traits)), \
             patch.object(team,'combat_role',side_effect=combat), \
             patch('equipment.recommend.profiles',return_value=[profile]), \
             patch('equipment.stats.role_base',return_value={'HP':10000,'Attack':1000,'Defence':1000,'Speed':100}), \
             patch('equipment.stats.role_panel',side_effect=panel):
            result=team.choose_teams(state,'MysteriousRealm_1')
        chosen=[by_id[rid] for wave in result['waves'].values()for rid in wave]
        self.assertEqual(len(set(chosen)),8)
        return chosen,result

    def test_advantaged_roles_are_preferred_when_composition_is_equal(self):
        elements=['Ice']*8+['Fire']*4+['Earth']*4
        chosen,_=self.select(elements)
        self.assertTrue(all(elements[i]=='Ice'for i in chosen))

    def test_neutral_roles_fill_missing_advantage_before_disadvantaged_roles(self):
        elements=['Ice']*2+['Fire']*2+['Dark']*2+['Light']*2+['Earth']*4
        chosen,_=self.select(elements)
        self.assertEqual(sum(elements[i]=='Ice'for i in chosen),2)
        self.assertFalse(any(elements[i]=='Earth'for i in chosen))

    def test_disadvantaged_roles_are_only_the_remaining_fallback(self):
        elements=['Ice']*2+['Fire']*2+['Dark']*2+['Earth']*4
        chosen,result=self.select(elements)
        self.assertEqual(sum(elements[i]=='Earth'for i in chosen),2)
        self.assertTrue(any('被Boss属性克制' in warning for warning in result['warnings']))

    def test_light_dark_mutual_advantage_is_not_penalized(self):
        for boss,other in [('Dark','Light'),('Light','Dark')]:
            with self.subTest(boss=boss):
                elements=[other]*8+['Fire']*4
                chosen,result=self.select(elements,boss=boss)
                self.assertTrue(all(elements[i]==other for i in chosen))
                self.assertTrue(all(not reasons['elementMatchups']['disadvantaged'] for reasons in result['reasons'].values()))

    def test_boss_element_takes_priority_over_summon_element(self):
        elements=['Ice']*8+['Earth']*8
        chosen,result=self.select(elements,add='Ice')
        self.assertTrue(all(elements[i]=='Ice'for i in chosen))
        self.assertTrue(all(r['bossElements']==['Fire']for r in result['reasons'].values()))

    def test_existing_output_priority_precedes_advantaged_healers(self):
        elements=['Fire']*6+['Earth']*2+['Ice']*4
        chosen,_=self.select(elements,kinds=['single']*8+['healer']*4)
        self.assertTrue(all(i<8 for i in chosen))

    def test_existing_single_target_priority_precedes_advantaged_area_units(self):
        elements=['Fire']*8+['Ice']*4
        chosen,_=self.select(elements,kinds=['single']*8+['area']*4)
        self.assertTrue(all(i<8 for i in chosen))

    def test_existing_stage_exclusion_precedes_element_advantage(self):
        elements=['Ice']*8+['Fire']*4+['Dark']*4
        chosen,_=self.select(elements,forbidden=['Ice'])
        self.assertTrue(all(elements[i]!='Ice'for i in chosen))

    def test_existing_stage_penalty_precedes_element_advantage(self):
        elements=['Ice']*8+['Fire']*8
        chosen,_=self.select(elements,penalized=set(range(8)))
        self.assertTrue(all(elements[i]=='Fire'for i in chosen))


if __name__=='__main__':unittest.main()
