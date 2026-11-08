"""Output class priority, self-stat damage scaling, and Aoi's actual fourth slot."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import abyss_team as team, abyss_loadout as loadout
from tools.test_abyss_auto import roster


class DamageClassification(unittest.TestCase):
    def test_health_and_defence_damage_are_output_even_with_tank_or_support_profiles(self):
        for sid,kind,scale in [('H119','single','HP'),('H007','single','HP'),('H001','area','HP'),
                               ('H192','area','Defence'),('H197','single','HP')]:
            with self.subTest(sid=sid):
                role={'StaticID':sid,'LV':60};value=team.combat_role(role)
                self.assertEqual(value['kind'],kind);self.assertEqual(value['damageStat'],scale)
                self.assertTrue(value['selfScaling'])
                profile=loadout._profile(role,[],True)
                self.assertEqual(profile['priorities'][0],'damage')
                self.assertEqual(profile['damageStat'],scale)
        self.assertEqual(team.combat_role({'StaticID':'H003'})['kind'],'healer')
        self.assertEqual(team.combat_role({'StaticID':'H015'})['kind'],'healer')
        self.assertEqual(team.combat_role({'StaticID':'H189'})['kind'],'tank')

    def test_primary_area_damage_is_not_mistaken_for_single_target_normal_attack(self):
        self.assertEqual(team.combat_role({'StaticID':'H011'})['kind'],'area')
        self.assertEqual(team.combat_role({'StaticID':'H182'})['kind'],'single')

    def state(self,ids):
        state=roster();state['RoleDataContainer']['Roles']=[{'_id':f'r{i}','StaticID':sid,'LV':60,'Star':5}for i,sid in enumerate(ids)]
        return state

    def choose(self,state):
        wave={'Monsters':[],'Buffs':{}}
        with patch.object(team,'fantasy_config',return_value={'Waves':[wave,wave]}):
            return team.choose_teams(state,'MysteriousRealm_1')

    def test_regular_damage_then_scaling_damage_before_tanks_and_healers(self):
        # Eight legal damage roles are available along with tempting fresh support.
        state=self.state(['H119','H007','H017','H002','H182','H121','H197','H025','H011','H192','H001','H189','H003','H015'])
        before=deepcopy(state);result=self.choose(state)
        selected=result['waves']['upper']+result['waves']['lower']
        roles=state['RoleDataContainer']['Roles']
        regular={f'r{i}'for i,role in enumerate(roles) if (value:=team.combat_role(role))['kind'] in ('single','area') and value['damageStat']=='Attack'}
        self.assertTrue(regular<=set(selected))
        for rid in selected:
            self.assertIn(team.combat_role(roles[int(rid[1:])])['kind'],('single','area'))
        self.assertEqual(state,before)

    def test_area_is_used_before_pure_tanks_and_healers(self):
        state=self.state(['H119','H017','H002','H182','H121','H011','H192','H001','H189','H003','H015'])
        result=self.choose(state)
        kinds=[team.combat_role(state['RoleDataContainer']['Roles'][int(rid[1:])])['kind']for rid in result['waves']['upper']+result['waves']['lower']]
        self.assertNotIn('tank',kinds);self.assertNotIn('healer',kinds);self.assertIn('area',kinds)


class AoiFourthSlot(unittest.TestCase):
    def group(self):
        def role(rid,sid,hp,defence,output):
            return {'id':rid,'sid':sid,'panel':{'HP':hp,'Defence':defence},'output':output,'combat':{'kind':'single'}}
        return [role('aoi','H119',20000,1500,5000),role('glass','H182',5000,300,1000),
                role('durable','H192',20000,1500,1100),role('weak','H011',1000,100,30)]

    def test_aoi_protects_a_fragile_strong_ally_not_herself_or_a_weak_unit(self):
        group=self.group();before=deepcopy(group)
        result=team._arrange_group(group,protect_back=True)
        self.assertEqual(result[3]['id'],'glass')
        self.assertNotEqual(result[3]['sid'],'H119')
        self.assertEqual(group,before)

    def test_final_outfits_can_change_the_protected_damage_role(self):
        group=self.group();group[3]['output']=2000
        self.assertEqual(team._arrange_group(group,protect_back=True)[3]['id'],'weak')
        self.assertNotEqual(team._arrange_group(self.group(),protect_back=False)[3]['id'],'glass')

    def test_planned_panels_and_passive_seal_are_used_after_equipment_selection(self):
        state=roster();roles=state['RoleDataContainer']['Roles']
        for index,sid in enumerate(['H119','H182','H192','H011']):roles[index]['StaticID']=sid
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        state['EquipmentContainer']['Datas']=[{'_id':f'planned-{i}'}for i in range(8)]
        targets={f'r{i}':[f'planned-{i}']for i in range(8)}
        def panel(state,role,gear,**kwargs):
            self.assertEqual(gear[0]['_id'],'planned-'+role['_id']['$oid'][1:])
            rid=role['_id']['$oid'];hp,defence,attack={'r0':(20000,1500,1000),'r1':(5000,300,6000),
                'r2':(20000,1500,1000),'r3':(1000,100,100)}.get(rid,(10000,800,1000))
            return {'panel':dict(HP=hp,Defence=defence,Attack=attack,Speed=100,CriticalRate=100,CriticalDamageRate=200,EffectHitRate=0,ResistanceRate=0)}
        plain={'Monsters':[],'Buffs':{}}
        sealed={'Monsters':[],'Buffs':{'DBs':[{'B':'FB_Special_003','Target':'Self'}]}}
        with patch.object(team,'fantasy_config',return_value={'Waves':[plain,plain]}),patch('equipment.stats.role_panel',side_effect=panel):
            arranged=team.arrange_waves(state,waves,targets=targets,scene_id='MysteriousRealm_1')
        self.assertEqual(arranged['upper'][3],'r1')
        with patch.object(team,'fantasy_config',return_value={'Waves':[sealed,plain]}),patch('equipment.stats.role_panel',side_effect=panel):
            arranged=team.arrange_waves(state,waves,targets=targets,scene_id='MysteriousRealm_1')
        self.assertEqual(arranged['upper'][3],'r3')


if __name__=='__main__':unittest.main()
