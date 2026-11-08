"""Stage effects, damage families, real training and ForceConquer priorities."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from backend import abyss_team as team
from backend.abyss_state import metadata
from backend.tasks import _oid
from tools.test_abyss_auto import roster


class TrainingAndSkillMetadata(unittest.TestCase):
    def test_skill_upgrades_ignore_hidden_skills_duplicates_and_promotion_stars(self):
        role={'StaticID':'H146','Star':6,'AwakenLV':4,'Skills':{'Skills':[
            {'StaticID':'H146S1','Level':2},{'StaticID':'H146S1','Level':4},
            {'StaticID':'H146S2','Level':1},{'StaticID':'H146S3','Level':3},
            {'StaticID':'H146S4','Level':99},{'StaticID':'H146S5','Level':99},
            {'StaticID':'not-owned-skill','Level':99}]}}
        self.assertEqual(team.training_preference(role),(5,4))
        role['Star']=1
        self.assertEqual(team.training_preference(role),(5,4))
        self.assertEqual(team.training_preference({'StaticID':'H146'}),(0,0))

    def test_favorable_descriptions_are_recognized_from_effective_game_flags(self):
        for sid in ('H122','H137','H146'):
            with self.subTest(sid=sid):
                traits=team.role_traits({'StaticID':sid,'LV':60})
                self.assertTrue(traits['forceConquer'])
                self.assertTrue(traits['favorableSkills'])
        self.assertFalse(team.role_traits({'StaticID':'H017','LV':60})['forceConquer'])


class SelectionPriority(unittest.TestCase):
    def choose(self,specs,*,desired=(),forbidden=(),boss='Light',buffs=(),debuffs=(),stage_flags=(),medics=0):
        state=roster();roles=state['RoleDataContainer']['Roles'][:len(specs)]
        state['RoleDataContainer']['Roles']=roles
        indices={_oid(role['_id']):i for i,role in enumerate(roles)}
        data=metadata();data={**data,'tables':{**data['tables'],'Role':deepcopy(data['tables']['Role'])}}
        for i,(role,spec) in enumerate(zip(roles,specs)):
            role.update(AwakenLV=spec.get('awaken',0),Star=spec.get('star',5))
            definition=data['tables']['Role'][role['StaticID']]
            definition['Element']=spec.get('element','Light')
            definition['Job']='Medic' if spec.get('kind')=='healer' else 'Warrior'
            role['Skills']={'Skills':[{'StaticID':raw.split(':')[0],'Level':spec.get('level',1)} for raw in definition['SkillIDs'].split('~') if ':Hide' not in raw]}
        def spec(role):return specs[indices[_oid(role['_id'])]]
        base=team.role_traits({'StaticID':'H017','LV':60})
        def traits(role,**kwargs):
            value=deepcopy(base);item=spec(role)
            value.update(flags=set(item.get('flags',())),buffs=set(item.get('buffs',())),debuffs=set(item.get('debuffs',())),heal=False,
                         buffTargets={key:set(item.get('buffScope',('ally',))) for key in item.get('buffs',())},
                         passive=False,nonAttack=False,strip=False,cleanse=False,scale=item.get('scale','Attack'),
                         break_=True,forceConquer=item.get('force',False),favorableSkills={'test'} if item.get('force') else set())
            value['break']=item.get('break',True);value['soulBreak']=False
            return value
        profile={'id':'test','title':'输出','damageStat':'Attack','critical':True,'priorities':['damage','Speed','Attack']}
        def combat(role,**kwargs):
            item=spec(role);kind=item.get('kind','single');scale=item.get('scale','Attack')
            return {'kind':kind,'tier':{'single':0,'area':1,'tank':2,'healer':3}[kind],
                    'damageStat':scale,'profile':{**profile,'damageStat':scale},'protectsBack':False}
        rule=team.wave_policy({'Buffs':{}});rule['desiredFlags']=set(desired);rule['forbiddenElements']=set(forbidden)
        rule['buffs']=set(buffs);rule['debuffs']=set(debuffs);rule['flags']=set(stage_flags)
        rule['medicCount']=medics
        if medics:rule['requiredJobs']['Medic']=medics
        enemy={'buffs':set(),'debuffs':set(),'flags':set(),'immune':set(),'recovery':False,'protection':False}
        panel=dict(HP=10000,Attack=1000,Defence=1000,Speed=100,CriticalRate=100,CriticalDamageRate=150,EffectHitRate=100,ResistanceRate=0)
        with patch.object(team,'metadata',return_value=data),patch.object(team,'fantasy_config',return_value={'Waves':[{},{}]}), \
             patch.object(team,'wave_policy',return_value=rule),patch.object(team,'_enemy_traits',return_value=(enemy,[{'Element':boss,'isBoss':True}])), \
             patch.object(team,'role_traits',side_effect=traits),patch.object(team,'combat_role',side_effect=combat), \
             patch('equipment.stats.role_base',return_value=panel),patch('equipment.stats.role_panel',return_value={'panel':panel}), \
             patch('equipment.recommend.profiles',return_value=[profile]),patch('equipment.recommend.metric',return_value=100):
            before=deepcopy(state);result=team.choose_teams(state,'MysteriousRealm_1')
        self.assertEqual(state,before)
        selected=[indices[rid] for ids in result['waves'].values() for rid in ids]
        self.assertEqual(len(set(selected)),8)
        return selected,result

    def test_regular_area_output_precedes_health_and_defence_single_output(self):
        selected,_=self.choose([{'kind':'area'} for _ in range(8)]+[{'scale':'HP','level':7,'awaken':6} for _ in range(4)]+[{'scale':'Defence'} for _ in range(4)])
        self.assertEqual(set(selected),set(range(8)))

    def test_scaling_output_is_still_used_before_pure_tanks_and_healers(self):
        specs=[{} for _ in range(6)]+[{'scale':'HP'},{'scale':'Defence'},{'kind':'tank'},{'kind':'healer'}]
        selected,_=self.choose(specs)
        self.assertEqual(set(selected),set(range(8)))

    def test_stage_hp_effect_overrides_regular_damage_preference(self):
        selected,_=self.choose([{} for _ in range(8)]+[{'scale':'HP'} for _ in range(8)],desired=['HPScale'])
        self.assertEqual(set(selected),set(range(8,16)))

    def test_stage_skill_bonus_cannot_add_an_unrequested_medic(self):
        selected,_=self.choose([{} for _ in range(8)]+[{'kind':'healer','flags':['FixedDamage']} for _ in range(2)],desired=['FixedDamage'])
        self.assertEqual(set(selected),set(range(8)))

    def test_stage_effects_override_breaker_and_growth_preferences(self):
        selected,_=self.choose([{'level':7,'awaken':6} for _ in range(8)]+[{'flags':['FixedDamage'],'break':False} for _ in range(8)],desired=['FixedDamage'])
        self.assertEqual(set(selected),set(range(8,16)))

    def test_skill_upgrades_precede_awakening_under_equal_conditions(self):
        selected,_=self.choose([{'level':1,'awaken':6} for _ in range(8)]+[{'level':5,'awaken':0} for _ in range(8)])
        self.assertEqual(set(selected),set(range(8,16)))

    def test_awakening_breaks_skill_ties_without_using_promotion_stars(self):
        selected,_=self.choose([{'star':6,'level':1,'awaken':0} for _ in range(8)]+[{'star':5,'level':1,'awaken':6} for _ in range(8)])
        self.assertEqual(set(selected),set(range(8,16)))

    def test_trained_candidates_outside_original_top_twenty_are_retained(self):
        selected,_=self.choose([{} for _ in range(16)]+[{'level':7} for _ in range(8)])
        self.assertEqual(set(selected),set(range(16,24)))

    def test_favorable_attack_skill_removes_element_disadvantage(self):
        specs=[{'element':'Dark'} for _ in range(6)]+[{'element':'Fire','force':True} for _ in range(2)]+[{'element':'Fire'} for _ in range(4)]
        selected,result=self.choose(specs,boss='Ice')
        self.assertEqual(set(selected),set(range(8)))
        self.assertTrue(all(not reason['elementMatchups']['disadvantaged'] for reason in result['reasons'].values()))
        self.assertEqual(sum(len(reason['favorableElementSkills']) for reason in result['reasons'].values()),2)

    def test_favorable_skill_never_bypasses_a_stage_element_exclusion(self):
        selected,_=self.choose([{'element':'Dark'} for _ in range(8)]+[{'element':'Fire','force':True,'level':7} for _ in range(4)],boss='Ice',forbidden=['Fire'])
        self.assertEqual(set(selected),set(range(8)))

    def test_one_shared_shield_provider_per_half_leaves_three_damage_slots(self):
        specs=[{'kind':'healer','buffs':['Shield']} for _ in range(4)]+[{} for _ in range(8)]
        selected,result=self.choose(specs,buffs=['Shield'],medics=1)
        self.assertEqual(sum(i<4 for i in selected),2)
        for reason in result['reasons'].values():
            self.assertEqual(len(reason['roleTypes']['healer']),1)
            self.assertEqual(len(reason['roleTypes']['single']),3)

    def test_combined_provider_covers_two_effects_without_duplicate_helpers(self):
        specs=[{'kind':'healer','buffs':['Shield','Immune']} for _ in range(4)]+[{} for _ in range(8)]
        selected,_=self.choose(specs,buffs=['Shield','Immune'],medics=1)
        self.assertEqual(sum(i<4 for i in selected),2)

    def test_self_only_shield_on_a_healer_does_not_enable_the_damage_team(self):
        specs=[{'kind':'healer','buffs':['Shield'],'buffScope':['self']} for _ in range(4)]+[{} for _ in range(8)]
        selected,_=self.choose(specs,buffs=['Shield'])
        self.assertEqual(set(selected),set(range(4,12)))

    def test_duplicate_enemy_status_providers_do_not_crowd_out_damage(self):
        specs=[{'kind':'tank','debuffs':['Burn']} for _ in range(4)]+[{} for _ in range(8)]
        selected,_=self.choose(specs,debuffs=['Burn'])
        self.assertEqual(sum(i<4 for i in selected),2)

    def test_favorable_skill_is_preferred_even_over_natural_element_advantage(self):
        selected,_=self.choose([{'element':'Dark','level':6} for _ in range(8)]+[{'element':'Fire','force':True} for _ in range(2)],boss='Light')
        self.assertTrue({8,9}<=set(selected))

    def test_stage_damage_penalty_still_overrides_favorable_skill(self):
        selected,_=self.choose([{} for _ in range(8)]+[{'force':True,'flags':['CounterSkill']} for _ in range(2)],stage_flags=['CounterSkill'])
        self.assertEqual(set(selected),set(range(8)))

    def test_single_armor_damage_hybrid_is_kept_without_inventing_team_break(self):
        specs=[{'kind':'area'} for _ in range(2)]+[{'level':6,'break':False} for _ in range(6)]+[{'flags':['Penetrate'],'break':False} for _ in range(2)]
        selected,_=self.choose(specs)
        self.assertTrue({8,9}<=set(selected))


class RealBuffRecipients(unittest.TestCase):
    def test_team_shield_and_self_shield_have_different_recipients(self):
        self.assertEqual(team.role_traits({'StaticID':'H009','LV':60})['buffTargets']['Shield'],{'ally'})
        self.assertEqual(team.role_traits({'StaticID':'H001','LV':60})['buffTargets']['Shield'],{'self'})

    def test_big_brother_is_real_single_target_penetration_not_fake_team_break(self):
        role={'StaticID':'H165','LV':60,'AwakenLV':6}
        traits=team.role_traits(role)
        self.assertIn('Penetrate',traits['flags'])
        self.assertFalse(traits['break'])
        self.assertEqual(team.combat_role(role)['kind'],'single')


if __name__=='__main__':unittest.main()
