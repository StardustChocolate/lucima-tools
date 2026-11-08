"""Boss counters and temporary purpose-specific loadouts; no live account calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import abyss_team as team, abyss_loadout as loadout, abyss_battle as battle, config
from backend.abyss_state import metadata
from backend.tasks import _oid
from equipment import plans, stats, recommend
from tools.test_abyss_auto import roster, gear_fixture, AssetServer
from tools.test_abyss_battle import unit


class BossCounterContracts(unittest.TestCase):
    def state(self):
        state=roster()
        ids=['H124','H017','H005','H010','H002','H011','H121','H182','H003','H015','H001','H007']
        state['RoleDataContainer']['Roles']=[{'_id':f'r{i}','StaticID':sid,'LV':60,'Star':5} for i,sid in enumerate(ids)]
        return state

    def wave(self):
        return {'Monsters':[{'M':'FH029','Boss':True}],'Buffs':{}}

    def test_real_dark_fly_and_recovery_boss_mechanisms(self):
        dark=team.role_traits({'StaticID':'H124','LV':60})
        self.assertTrue(dark['break']);self.assertTrue(dark['blockBuff']);self.assertTrue(dark['soulBlockBuff'])
        traits,_=team._enemy_traits(self.wave())
        self.assertTrue(traits['recovery']);self.assertTrue(traits['protection'])
        self.assertTrue(team.skill_status({'StaticID':'H124'}, {'StaticID':'H124S3','Level':7},'CannotBuff',soul=True,ignore=True))
        self.assertFalse(team.skill_status({'StaticID':'H124'}, {'StaticID':'H124S3','Level':7},'CannotBuff',ignore=True))

    def test_real_boss_rules_cover_both_halves_with_break_block_buff_and_anti_heal(self):
        state=self.state();before=deepcopy(state)
        with patch.object(team,'fantasy_config',return_value={'Waves':[self.wave(),self.wave()]}):
            result=team.choose_teams(state,'MysteriousRealm_1',offensive=True)
        roles={_oid(r['_id']):r for r in state['RoleDataContainer']['Roles']}
        selected=result['waves']['upper']+result['waves']['lower']
        self.assertEqual(len(set(selected)),8)
        for wave,ids in result['waves'].items():
            traits=[team.role_traits(roles[rid]) for rid in ids]
            for counter in ('break','blockBuff','antiHeal'):self.assertTrue(any(t[counter] for t in traits),counter)
            self.assertIn(result['breakerRoles'][wave],ids)
            self.assertTrue(result['reasons'][wave]['counters']['antiHeal'])
        self.assertEqual(state,before)

    def test_offensive_retry_prefers_all_damage_even_when_supports_are_fresh(self):
        state=self.state()
        wave={'Monsters':[{'M':'FH057','Boss':True}],'Buffs':{}}
        with patch.object(team,'fantasy_config',return_value={'Waves':[wave,wave]}):
            result=team.choose_teams(state,'MysteriousRealm_1',offensive=True,previous=[f'r{i}'for i in range(8)])
        chosen=set(result['waves']['upper']+result['waves']['lower'])
        self.assertFalse(chosen & {'r8','r9'})
        self.assertTrue(all(team.combat_role(state['RoleDataContainer']['Roles'][int(rid[1:])])['kind'] in ('single','area') for rid in chosen))

    def test_immune_boss_does_not_request_ineffective_block_buff(self):
        state=self.state();traits,enemies=team._enemy_traits(self.wave());traits['immune'].add('CannotBuff')
        with patch.object(team,'fantasy_config',return_value={'Waves':[self.wave(),self.wave()]}), \
             patch.object(team,'_enemy_traits',return_value=(traits,enemies)):
            result=team.choose_teams(state,'MysteriousRealm_1')
        for reasons in result['reasons'].values():self.assertNotIn('blockBuff',reasons['counters'])

    def test_offensive_team_keeps_required_job_and_reports_necessary_support(self):
        state=self.state();wave=self.wave()
        wave['Buffs']={'Bs':[{'B':'FG_Job_003','Target':'Self'}]}
        with patch.object(team,'fantasy_config',return_value={'Waves':[wave,wave]}):
            result=team.choose_teams(state,'MysteriousRealm_1',offensive=True)
        roles={_oid(r['_id']):r for r in state['RoleDataContainer']['Roles']}
        for ids in result['waves'].values():
            jobs=[metadata()['tables']['Role'][roles[rid]['StaticID']]['Job']for rid in ids]
            self.assertIn('Medic',jobs)
        self.assertFalse(any('治疗角色穿输出装兜底' in text for text in result['warnings']))
        for reason in result['reasons'].values():self.assertEqual(reason['medicCount'],{'required':1,'selected':1})

    def test_dark_fly_uses_dispel_and_block_buff_against_protected_boss(self):
        actor=unit('1-0-0',StaticID='H124',NowSP=100,NowSkillCooldownMap={},Skills={'Skills':[
            {'StaticID':f'H124S{i}','Level':7}for i in (1,2,3)]})
        boss=unit('2-0-0',StaticID='FH029',IsBoss=True,NowStatusList=[{'StaticID':'Invincible'}])
        choice=battle._priority_debuff_action(actor,[actor],[boss],20)
        self.assertEqual(choice,('H124S3',7,boss['id']))
        self.assertTrue(battle._soul_allowed(actor,choice[0],20,[actor],[boss],True))
        actor['NowSkillCooldownMap']={'H124S3':2}
        self.assertNotEqual(battle._priority_debuff_action(actor,[actor],[boss],20),choice)
        actor['NowStatusList']=[{'StaticID':'Silence'}]
        self.assertIsNone(battle._priority_debuff_action(actor,[actor],[boss],20))

    def test_anti_heal_cast_targets_recovery_boss_and_does_not_repeat_existing_debuff(self):
        actor=unit('1-0-0',StaticID='H010',NowSP=100,NowSkillCooldownMap={},Skills={'Skills':[
            {'StaticID':f'H010S{i}','Level':7}for i in (1,2,3)]})
        boss=unit('2-0-0',StaticID='FH029',IsBoss=True,NowStatusList=[{'StaticID':'ReduceDef'}])
        choice=battle._priority_debuff_action(actor,[actor],[boss],0)
        self.assertIsNotNone(choice)
        self.assertTrue(team.skill_status(actor,{'StaticID':choice[0],'Level':choice[1]},'Unhealable'))
        boss['NowStatusList'].append({'StaticID':'Unhealable'})
        self.assertIsNone(battle._priority_debuff_action(actor,[actor],[boss],0))


class PurposeLoadoutContracts(unittest.TestCase):
    def test_complete_slow_breaker_is_rebuilt_for_speed_before_damage_roles(self):
        state,waves=gear_fixture()
        for item in state['EquipmentContainer']['Datas']:
            index=int(_oid(item['_id']).split('-')[1])
            if index==0:item['EquipRole']='r0'
            if index not in (0,1) and item['MainProp']['PropertyType']=='SpeedValue':item['MainProp']['Value']=0
            if index in (0,1):
                item.update(Set='',MainProp={'PropertyType':'AttackValue','Value':50000 if index==0 else 1},
                    SubProps={'SourceValues':[{'PropertyType':'SpeedValue','Value':0 if index==0 else 10},
                                              {'PropertyType':'EffectHitRate','Value':.2}]})
        before=deepcopy(state)
        result=loadout.plan(state,waves,utility_roles={'r0':['break']})
        target=result['targets']['r0']
        self.assertTrue(all(eid.endswith('-1')for eid in target))
        self.assertEqual(len({eid for ids in result['targets'].values()for eid in ids}),48)
        self.assertEqual(state,before)

    def test_offensive_retry_rebuilds_complete_outfits_and_restores_original_owners(self):
        state,waves=gear_fixture()
        for item in state['EquipmentContainer']['Datas']:
            if _oid(item['_id']).endswith('-0'):item['EquipRole']='r0'
        with tempfile.TemporaryDirectory() as tmp,patch.object(config,'data_dir',return_value=Path(tmp)), \
             patch.object(recommend,'search',wraps=recommend.search) as search:
            server=AssetServer(state);before=loadout._owners(state['EquipmentContainer']['Datas'])
            result=loadout.plan(state,waves,offensive=True,utility_roles={'r0':['break']})
            roles={call.args[1]['_id']['$oid'] for call in search.call_args_list}
            self.assertEqual(roles,set(waves['upper']+waves['lower']))
            for call in search.call_args_list:
                profile=call.kwargs['profile_override']
                if call.args[1]['_id']['$oid']=='r0':self.assertEqual(profile['ranking'],'speed-first')
                else:self.assertEqual(profile['priorities'][0],'damage')
            loadout.apply(server,result)
            self.assertTrue(loadout.restore(server)['restored'])
            self.assertEqual(loadout._owners(server.server['EquipmentContainer']['Datas']),before)
            self.assertFalse(loadout.pending(server))


if __name__=='__main__':unittest.main()
