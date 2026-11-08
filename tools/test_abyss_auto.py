"""Offline automatic Abyss teams, soul strategy, restoration and progression."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
import tempfile
import threading
import unittest

from backend import abyss_team as team,abyss_loadout as loadout,abyss_progression as progression,abyss_battle as battle,config
from backend.abyss_state import metadata
from backend.tasks import normalize_abyss_config,abyss_scenes,_oid
from backend.game_client import GameError,GameMessage
from equipment import plans,stats
from equipment.tests.test_auto import fixture
from tools.test_abyss_battle import unit


def roster():
    ids=[sid for sid in sorted(stats.tables()['roles']) if sid.startswith('H')][:24]
    return {'RoleDataContainer':{'Roles':[{'_id':{'$oid':f'r{i}'},'StaticID':sid,'LV':60,'Star':5} for i,sid in enumerate(ids)]},
            'EquipmentContainer':{'Datas':[]},'Artifacts':{'Datas':[]},
            'SceneDataContainer':{'Scenes':[{'StaticID':'MysteriousRealm_1','Stars':[0,0,0,0]}]},
            'ActivityDBSettingContainer':{'SettingList':[{'ActivityID':aid,'IsIgnoreTime':True} for aid in ('Version_Fantasy','Version_Fantasy01_1-6')]}}


class TeamContracts(unittest.TestCase):
    def test_all_historical_effects_compile_with_matching_provenance(self):
        data=team.catalog()
        self.assertEqual(len(data['effects']),117)
        self.assertEqual(len(data['configurations']),222)
        for entry in data['configurations']:
            for wave in entry['waves']:team.wave_policy(wave)

    def test_empty_manual_and_partly_empty_are_rejected_but_auto_empty_is_valid(self):
        state=roster();waves={'upper':[],'lower':[]}
        with self.assertRaises(GameError):normalize_abyss_config(state,'MysteriousRealm_1',waves)
        result=normalize_abyss_config(state,'MysteriousRealm_1',waves,auto_advance=True)
        self.assertTrue(result['autoConfigure'])
        with self.assertRaises(GameError):normalize_abyss_config(state,'MysteriousRealm_1',{'upper':['r0'],'lower':[]},auto_advance=True)
        fixed=normalize_abyss_config(state,'MysteriousRealm_1',{'upper':['r0'],'lower':['r1']},auto_advance=True)
        self.assertNotIn('autoConfigure',fixed)

    def test_joint_eight_unique_full_level_and_breakers_in_both_halves(self):
        state=roster();state['RoleDataContainer']['Roles'][-1]['LV']=59;before=deepcopy(state)
        result=team.choose_teams(state,'MysteriousRealm_1')
        ids=result['waves']['upper']+result['waves']['lower']
        self.assertEqual(len(ids),8);self.assertEqual(len(set(ids)),8)
        roles={_oid(role['_id']):role for role in state['RoleDataContainer']['Roles']}
        self.assertTrue(all(roles[rid]['LV']==60 for rid in ids))
        for wave in ('upper','lower'):self.assertTrue(result['reasons'][wave]['breakers'])
        self.assertEqual(state,before)
        again=team.choose_teams(state,'MysteriousRealm_1',previous=ids)
        self.assertNotEqual(set(ids),set(again['waves']['upper']+again['waves']['lower']))

    def test_required_job_and_forbidden_job_are_hard_constraints(self):
        state=roster();wave=deepcopy(metadata()['tables']['Fantasy']['MysteriousRealm_1'][0]['Waves'][0])
        wave['Buffs']={'Bs':[{'B':'FG_Job_006','Target':'Self'}],'DBs':[{'B':'FB_Job_002','Target':'Self'}]}
        with patch.object(team,'fantasy_config',return_value={'Waves':[wave,wave]}):
            result=team.choose_teams(state,'MysteriousRealm_1')
        roles={_oid(role['_id']):metadata()['tables']['Role'][role['StaticID']] for role in state['RoleDataContainer']['Roles']}
        for ids in result['waves'].values():
            self.assertIn('Sniper',[roles[rid]['Job'] for rid in ids])
            self.assertNotIn('Warrior',[roles[rid]['Job'] for rid in ids])

    def test_non_job_status_and_function_rules_are_kept(self):
        rule=team.wave_policy({'Buffs':{'Bs':[{'B':'FG_Debuff_010','Target':'Enemy'},{'B':'FG_Buff_015','Target':'Self'}],
                                       'DBs':[{'B':'FB_Fun_004','Target':'Self'},{'B':'FB_Special_003','Target':'Self'},{'B':'FB_Immune_003','Target':'Enemy'}]}})
        self.assertIn('Burn',rule['debuffs']);self.assertIn('Immune',rule['buffs'])
        self.assertIn('CounterSkill',rule['flags']);self.assertIn('SealPassive',rule['flags']);self.assertIn('ReduceSpeed',rule['antiDebuffs'])
        self.assertIn('FixedDamage',team.role_traits({'StaticID':'H138'})['flags'])
        self.assertIn('SpeedScale',team.role_traits({'StaticID':'H182'})['flags'])

    def test_no_eight_full_level_roster_stops_before_any_change(self):
        state=roster();state['RoleDataContainer']['Roles']=state['RoleDataContainer']['Roles'][:7]
        with self.assertRaisesRegex(GameError,'8 名'):team.choose_teams(state,'MysteriousRealm_1')
        with self.assertRaises(GameError):team.choose_teams(roster(),'MysteriousRealm_1',cancelled=lambda:True)


class SoulContracts(unittest.TestCase):
    def actor(self,sid='H121',uid='1-0-0'):
        return unit(uid,StaticID=sid,NowSP=100,Skills={'Skills':[{'StaticID':f'{sid}S{i}','Level':1} for i in (1,2,3)]},NowSkillCooldownMap={})

    def test_activity_holds_armor_break_cooldown_and_soul_until_third_wave(self):
        actor=self.actor();enemy=unit('2-0-0',StaticID='H001',IsBoss=True)
        for wave in (0,1):
            choice,_,burn=battle._battle_action(actor,[actor],[enemy],20,use_soul=True,prioritize_soul_break=True,
                reserve_boss_tools=True,wave_index=wave,wave_count=3)
            self.assertIsNotNone(choice)
            self.assertEqual(choice[0],'H121S1')
            self.assertFalse(burn)
            self.assertEqual(actor['NowSkillCooldownMap'],{})
        choice,decision,burn=battle._battle_action(actor,[actor],[enemy],20,use_soul=True,prioritize_soul_break=True,
            reserve_boss_tools=True,wave_index=2,wave_count=3)
        self.assertEqual(choice[0],'H121S3')
        self.assertEqual(decision,'soul_armor_break');self.assertTrue(burn)

    def test_activity_first_two_waves_limit_every_nonmedic_and_joined_friend_to_s1(self):
        enemy=unit('2-0-0',StaticID='H002')
        for sid in ('H001','H119','H146','H165','H121'):
            actor=self.actor(sid);actor['_IsSupport']=True
            before=deepcopy(actor)
            for wave in (0,1):
                with self.subTest(sid=sid,wave=wave):
                    choice,_,burn=battle._battle_action(actor,[actor],[enemy],40,use_soul=True,
                        prioritize_soul_break=True,reserve_boss_tools=True,wave_index=wave,wave_count=3)
                    self.assertEqual(choice[0],sid+'S1')
                    self.assertFalse(burn)
                    self.assertEqual(actor,before)

    def test_activity_medic_keeps_active_heal_in_both_mob_waves_without_burning_soul(self):
        healer=self.actor('H029');injured=self.actor('H165','1-0-1');injured['NowHP']=10
        injured['NowStatusList']=[{'StaticID':'Poison'}]
        enemy=unit('2-0-0',StaticID='H002')
        for wave in (0,1):
            choice,_,burn=battle._battle_action(healer,[healer,injured],[enemy],40,use_soul=True,
                prioritize_soul_break=True,reserve_boss_tools=True,wave_index=wave,wave_count=3)
            self.assertEqual(choice,('H029S3',1,injured['id']))
            self.assertFalse(burn)

    def test_mob_s1_is_the_first_live_form_skill_without_reordering_native_container(self):
        actor=self.actor('H146');actor['Skills']['Skills'] += [
            {'StaticID':'H146S4','Level':3},{'StaticID':'H146S5','Level':4}]
        before=deepcopy(actor);enemy=unit('2-0-0',StaticID='H002')
        with patch.object(battle,'status_properties',side_effect=lambda u:
                {'AllSkillReplace':'H146S4~H146S5'}if u is actor else {}):
            choice,_,burn=battle._battle_action(actor,[actor],[enemy],40,use_soul=True,
                prioritize_soul_break=True,reserve_boss_tools=True,wave_index=1,wave_count=3)
        self.assertEqual(choice,('H146S4',3,enemy['id']));self.assertFalse(burn)
        self.assertEqual(actor,before)

    def test_elite_soul_saves_even_a_partial_balance_for_ready_ignore_resistance_break(self):
        damage=self.actor('H119');breaker=self.actor('H121','1-0-1')
        enemy=unit('2-0-0',StaticID='H002',IsBoss=True)
        for balance in (10,20,30,40):
            self.assertFalse(battle._elite_soul_allowed(damage,'H119S1',balance,[damage,breaker],[enemy]))
        self.assertTrue(battle._elite_soul_allowed(breaker,'H121S3',20,[damage,breaker],[enemy]))
        enemy['NowStatusList']=[{'StaticID':'ReduceDef'}]
        self.assertTrue(battle._elite_soul_allowed(damage,'H119S1',10,[damage,breaker],[enemy]))

    def test_boss_damage_action_waits_for_break_then_burns_and_regular_abyss_ai_stays_open(self):
        damage=self.actor('H119');damage['NowSkillCooldownMap']={'H119S3':2}
        breaker=self.actor('H121','1-0-1');enemy=unit('2-0-0',StaticID='H002',IsBoss=True)
        def act():
            return battle._battle_action(damage,[damage,breaker],[enemy],40,use_soul=True,
                prioritize_soul_break=True,reserve_boss_tools=True,wave_index=2,wave_count=3)
        self.assertEqual(act()[0][0],'H119S1');self.assertFalse(act()[2])
        enemy['NowStatusList']=[{'StaticID':'ReduceDef'}]
        self.assertTrue(act()[2])
        regular=self.actor('H165')
        choice,_,_=battle._battle_action(regular,[regular],[enemy],40,use_soul=True)
        self.assertEqual(choice[0],'H165S3')

    def test_elite_unavailable_breaker_does_not_block_damage_burn(self):
        damage=self.actor('H119');enemy=unit('2-0-0',StaticID='H002',IsBoss=True)
        for fields in ({'NowHP':0},{'NowStatusList':[{'StaticID':'Silence'}]},
                       {'NowSkillCooldownMap':{'H121S3':2}}):
            breaker=self.actor('H121','1-0-1');breaker.update(fields)
            self.assertTrue(battle._elite_soul_allowed(damage,'H119S1',10,[damage,breaker],[enemy]))

    def test_elite_healer_does_not_consume_damage_soul_after_armor_break(self):
        healer=self.actor('H029');enemy=unit('2-0-0',StaticID='H002',IsBoss=True,
            NowStatusList=[{'StaticID':'ReduceDef'}])
        self.assertFalse(battle._elite_soul_allowed(healer,'H029S3',100,[healer],[enemy]))

    def test_soul_ignore_resistance_break_uses_real_addvalue_nodes(self):
        actor=self.actor();record={'StaticID':'H121S3','Level':1}
        self.assertFalse(team.skill_break(actor,record,ignore=True))
        self.assertTrue(team.skill_break(actor,record,soul=True,ignore=True))
        enemy=unit('2-0-0',StaticID='H001')
        chosen=battle._soul_break_action(actor,[actor],[enemy],20)
        self.assertEqual(chosen,('H121S3',1,'2-0-0'))

    def test_soul_never_bypasses_cost_cooldown_or_control(self):
        enemy=unit('2-0-0',StaticID='H001');actor=self.actor()
        self.assertIsNone(battle._soul_break_action(actor,[actor],[enemy],0))
        actor['NowSkillCooldownMap']={'H121S3':1}
        self.assertIsNone(battle._soul_break_action(actor,[actor],[enemy],20))
        actor['NowSkillCooldownMap']={};actor['NowStatusList']=[{'StaticID':'Silence'}]
        self.assertIsNone(battle._soul_break_action(actor,[actor],[enemy],20))

    def test_soul_is_reserved_for_ally_breaker_then_spent_when_enough(self):
        source=self.actor('H001');ally=self.actor('H121','1-0-1');enemy=unit('2-0-0',StaticID='H002')
        self.assertFalse(battle._soul_allowed(source,'H001S2',20,[source,ally],[enemy],True))
        self.assertTrue(battle._soul_allowed(source,'H001S2',30,[source,ally],[enemy],True))
        enemy['NowStatusList']=[{'StaticID':'ReduceDef'}]
        self.assertTrue(battle._soul_allowed(source,'H001S2',20,[source,ally],[enemy],True))
        self.assertIsNone(battle._soul_break_action(ally,[source,ally],[enemy],20))

    def test_boss_break_has_priority_and_missing_catalog_keeps_manual_ai_available(self):
        actor=self.actor();boss=unit('2-0-0',StaticID='H001',IsBoss=True);summon=unit('2-0-1',StaticID='H002',NowHP=10)
        self.assertEqual(battle._soul_break_action(actor,[actor],[summon,boss],20)[2],boss['id'])
        with patch.object(team,'catalog',side_effect=GameError('规则尚未更新')):
            self.assertIsNone(battle._soul_break_action(actor,[actor],[boss],20))
            self.assertTrue(battle._soul_allowed(actor,'H121S3',20,[actor],[boss],True))


class AssetServer:
    """Separate authoritative state; local assumptions cannot pass restoration checks."""
    def __init__(self,state):
        self.server=deepcopy(state);self.account_state=deepcopy(state);self.login_id='offline';self.calls=[];self.fail_bootstrap=False;self.after_call=None
    def _auth_data(self,value):return value
    def bootstrap(self):
        if self.fail_bootstrap:raise GameError('模拟读取失败')
        self.account_state=deepcopy(self.server)
    def call(self,route,body):
        self.calls.append((route,deepcopy(body)))
        eq=self.server.get('EquipmentContainer',{}).get('Datas',[])
        if route=='EquipmentHandler.EquipsToRole':
            for eid in body['EquipIDs']:
                selected=next(e for e in eq if _oid(e['_id'])==eid)
                if _oid(selected.get('EquipRole'))==body['RoleID']:raise GameError('_id Same')
                for item in eq:
                    if _oid(item.get('EquipRole'))==body['RoleID'] and plans.equip_slot(item)==plans.equip_slot(selected):item['EquipRole']=''
                selected['EquipRole']=body['RoleID']
            response={'NewEquips':deepcopy(eq)}
        elif route=='EquipmentHandler.TakeOffEquip':
            selected=next(e for e in eq if _oid(e['_id'])==body['EquipID']);selected['EquipRole']='';response={'NewEquip':deepcopy(selected)}
        elif route.startswith('ArtifactHandler.'):
            artifacts=self.server['Artifacts']['Datas'];selected=next(a for a in artifacts if _oid(a['_id'])==body['ArtifactID'])
            for art in artifacts:
                if _oid(art.get('EquipRole'))==body['RoleID']:art['EquipRole']=''
            selected['EquipRole']=body['RoleID'] if route.endswith('.Equip') else ''
            response={'Artifact':deepcopy(artifacts)}
        else:raise AssertionError('Unexpected live route '+route)
        if self.after_call:self.after_call()
        return response


def gear_fixture():
    state,_=fixture(9,roles_count=10)
    state['Artifacts']={'Datas':[]}
    for e in state['EquipmentContainer']['Datas']:
        j=int(_oid(e['_id']).split('-')[1])
        if j==0:
            e['EquipRole']='';e['MainProp']['Value']=0
        if j==8:
            e['MainProp']['Value']*=3
    return state,{'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}


class RestorationContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(config,'data_dir',return_value=Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
        self.state,self.waves=gear_fixture();self.client=AssetServer(self.state)
    def owners(self):return loadout._owners(self.client.server['EquipmentContainer']['Datas'])

    def test_real_plan_uses_recommender_and_forty_eight_distinct_assets(self):
        value=loadout.plan(self.state,self.waves)
        self.assertEqual(len(set(itertools_chain(value['targets'].values()))),48)
        self.assertTrue(value['equipment'])
        before=self.owners();journal_seen=[]
        self.client.after_call=lambda:journal_seen.append(loadout.pending(self.client))
        loadout.apply(self.client,value)
        self.assertTrue(all(journal_seen))
        result=loadout.restore(self.client)
        self.assertTrue(result['restored']);self.assertEqual(self.owners(),before);self.assertFalse(loadout.pending(self.client))

    def test_partial_wear_failure_still_has_durable_recovery(self):
        value=loadout.plan(self.state,self.waves);before=self.owners()
        def timeout():raise GameError('模拟响应丢失，服务器可能已穿戴')
        self.client.after_call=timeout
        with self.assertRaises(GameError):loadout.apply(self.client,value)
        self.assertTrue(loadout.pending(self.client));self.client.after_call=None
        self.assertTrue(loadout.restore(self.client)['restored']);self.assertEqual(self.owners(),before)

    def test_failed_sync_keeps_journal_then_next_successful_sync_returns_assets(self):
        value=loadout.plan(self.state,self.waves);before=self.owners();loadout.apply(self.client,value)
        self.client.fail_bootstrap=True
        calls=len(self.client.calls)
        self.assertFalse(loadout.restore(self.client)['restored']);self.assertTrue(loadout.pending(self.client))
        self.assertEqual(len(self.client.calls),calls)
        restarted=AssetServer(self.client.server)
        self.assertTrue(loadout.restore(restarted)['restored'])
        self.assertEqual(loadout._owners(restarted.server['EquipmentContainer']['Datas']),before)
        self.assertEqual(loadout.restore(restarted),{'restored':True,'changed':False})

    def test_artifact_transfer_and_original_empty_slot_are_restored(self):
        self.client.server['Artifacts']={'Datas':[{'_id':'a0','StaticID':'A0131','LV':30,'EquipRole':'r0'},{'_id':'a1','StaticID':'A0347','LV':30,'EquipRole':'r8'}]};self.client.bootstrap()
        value={'roles':['r0'],'targets':{},'equipment':{},'artifacts':{},'artifactTargets':{'r0':'a1'}}
        loadout.apply(self.client,value)
        self.assertEqual(loadout._owners(self.client.server['Artifacts']['Datas']),{'a0':'','a1':'r0'})
        self.assertTrue(loadout.restore(self.client)['restored'])
        self.assertEqual(loadout._owners(self.client.server['Artifacts']['Datas']),{'a0':'r0','a1':'r8'})

    def test_not_enough_assets_never_writes_journal_or_changes_state(self):
        self.state['EquipmentContainer']['Datas']=self.state['EquipmentContainer']['Datas'][:6]
        before=deepcopy(self.state)
        with self.assertRaises(GameError):loadout.plan(self.state,self.waves)
        self.assertFalse(loadout.pending(self.client));self.assertEqual(before,self.state)

    def test_orphan_and_duplicate_ownership_are_unequipped_and_do_not_block_abyss(self):
        items=self.client.server['EquipmentContainer']['Datas']
        orphan=next(e for e in items if plans.equip_slot(e)=='helmet' and not _oid(e.get('EquipRole')))
        orphan['EquipRole']='deleted-role'
        weapons=[e for e in items if plans.equip_slot(e)=='weapon'][:2]
        for item in weapons:item['EquipRole']='r0'
        unknown=deepcopy(orphan);unknown.update(_id='unknown-slot',StaticID='E0999',EquipRole='r1')
        unknown['MainProp']={'PropertyType':'AttackValue','Value':1}
        items.append(unknown);self.client.bootstrap()
        before=deepcopy(self.client.account_state)
        value=loadout.plan(self.client.account_state,self.waves)
        self.assertEqual(self.client.account_state,before,'planning itself must not change live owners')
        self.assertIn(_oid(orphan['_id']),value['cleanupEquipment'])
        self.assertIn(_oid(weapons[0]['_id']),value['cleanupEquipment'])
        self.assertNotIn('unknown-slot',value['cleanupEquipment'])
        self.assertEqual(len({eid for group in value['targets'].values() for eid in group}),48)
        loadout.apply(self.client,value)
        self.assertEqual(self.owners()['unknown-slot'],'r1')
        result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result)
        for eid in value['cleanupEquipment']:self.assertEqual(self.owners()[eid],'')
        self.assertFalse(loadout.pending(self.client))

    def test_removed_owner_or_missing_asset_in_old_record_is_parked_or_skipped(self):
        worn=next(e for e in self.client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole')))
        eid=_oid(worn['_id'])
        loadout._save(loadout._path(self.client),{'schema':1,'equipment':{eid:'deleted-role','gone':'r0'},'artifacts':{},'roles':[]})
        result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result)
        self.assertNotEqual(self.owners()[eid],'deleted-role')
        self.assertIn(eid,result['fallback']['equipment'])
        self.assertEqual(result['fallback']['missingEquipment'],['gone'])
        self.assertFalse(loadout.pending(self.client))

    def test_orphan_and_duplicate_bonds_are_unequipped_and_left_in_inventory(self):
        self.client.server['Artifacts']={'Datas':[
            {'_id':'orphan','StaticID':'A0131','LV':30,'EquipRole':'deleted-role'},
            {'_id':'duplicate-a','StaticID':'A0131','LV':30,'EquipRole':'r0'},
            {'_id':'duplicate-b','StaticID':'A0347','LV':30,'EquipRole':'r0'}]}
        self.client.bootstrap()
        value=loadout.plan(self.client.account_state,self.waves)
        self.assertEqual(set(value['cleanupArtifacts']),{'orphan','duplicate-a','duplicate-b'})
        loadout.apply(self.client,value)
        self.assertTrue(all(not _oid(a.get('EquipRole')) for a in self.client.server['Artifacts']['Datas']))
        result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result)
        self.assertTrue(all(not _oid(a.get('EquipRole')) for a in self.client.server['Artifacts']['Datas']))
        self.assertFalse(loadout.pending(self.client))


def itertools_chain(rows):
    return [item for row in rows for item in row]


class ProgressionContracts(unittest.TestCase):
    def setUp(self):
        self.state=roster();self.client=SimpleNamespace(account_state=self.state,login_id='offline',bootstrap=Mock())
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(config,'data_dir',return_value=Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
        self.meta={'tables':{'Scene':{'MysteriousRealm_1':{},'MysteriousRealm_2':{}},'Fantasy':{'MysteriousRealm_1':[1],'MysteriousRealm_2':[1]}}}
        for target in [patch.object(progression,'metadata',return_value=self.meta),patch.object(config,'get_request_delay_config',return_value={'enabled':False}),patch.object(progression.time,'sleep')]:target.start();self.addCleanup(target.stop)
        self.params={'sceneId':'MysteriousRealm_1','waves':{'upper':['r0'],'lower':['r1']},'autoAdvance':True}
    def settle(self,sid,stars):
        rows=self.state['SceneDataContainer']['Scenes'];row=next((r for r in rows if r['StaticID']==sid),None)
        if row is None:row={'StaticID':sid};rows.append(row)
        row['Stars']=[1]*stars+[0]*(4-stars)

    def test_picker_snapshots_publish_confirmed_stars_before_advancing(self):
        events=[];self.params['_on_progress']=events.append
        def run(client,params):
            self.settle(params['sceneId'],4)
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok'])
        first=next(row for row in events if row.get('phase')=='settled')
        self.assertEqual(first['sceneId'],'MysteriousRealm_1')
        self.assertEqual(first['scenes'][0]['stars'],[1,1,1,1])
        next_stage=next(row for row in events if row.get('sceneId')=='MysteriousRealm_2')
        self.assertEqual(next_stage['scenes'][0]['stars'],[1,1,1,1])
        self.assertEqual(result['lastSceneId'],'MysteriousRealm_2')
        self.assertEqual(next(row for row in result['scenes'] if row['id']=='MysteriousRealm_2')['stars'],[1,1,1,1])
        self.assertEqual(events[-1]['phase'],'finished')

    def test_full_backpack_stops_before_borrowing_or_creating_battle(self):
        self.state['AccountSaveData']={'RolePackMaxCount':len(self.state['RoleDataContainer']['Roles'])}
        self.params['waves']={'upper':[],'lower':[]}
        before=deepcopy(self.state)
        with patch.object(team,'choose_teams') as choose, patch.object(loadout,'apply') as equip, \
             patch.object(battle,'run_abyss_battle') as run:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertIn('角色背包已满',result['detail'])
        choose.assert_not_called();equip.assert_not_called();run.assert_not_called()
        self.assertEqual(self.state,before)

    def test_old_missing_equipment_record_warns_once_and_does_not_block_both_stages(self):
        path=loadout._path(self.client)
        loadout._save(path,{'schema':1,'equipment':{'sold-equipment':'old-owner'},'artifacts':{},'roles':[]})
        events=[];self.params['_on_progress']=events.append
        def run(client,params):
            self.assertFalse(loadout.pending(client))
            self.settle(params['sceneId'],4)
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(loadout,'restore',wraps=loadout.restore) as restore,patch.object(battle,'run_abyss_battle',side_effect=run) as fight:
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertFalse(result['recoveryRequired'])
        self.assertEqual(fight.call_count,2);self.assertEqual(restore.call_count,1)
        self.assertIn('缺失物品已记录',result['recoveryWarning'])
        self.assertTrue(any(event.get('recoveryWarning') for event in events))
        self.assertFalse(path.exists());self.assertEqual(len(list((path.parent/'history').glob('*.json'))),1)

    def test_old_recovery_success_is_transparent_and_not_repeated(self):
        path=loadout._path(self.client)
        loadout._save(path,{'schema':1,'equipment':{},'artifacts':{},'roles':[]})
        def run(client,params):
            self.settle(params['sceneId'],4)
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(loadout,'restore',wraps=loadout.restore) as restore,patch.object(battle,'run_abyss_battle',side_effect=run):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(result['recoveryWarning'],'')
        self.assertEqual(restore.call_count,1);self.assertFalse(path.exists())

    def test_definitive_server_backpack_error_restores_once_without_retry_or_readback(self):
        self.params['waves']={'upper':[],'lower':[]}
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        with patch.object(team,'choose_teams',return_value={'waves':waves,'warnings':[],'reasons':{}}), \
             patch.object(loadout,'plan',return_value={}),patch.object(loadout,'apply'), \
             patch.object(loadout,'pending',side_effect=[False,True]), \
             patch.object(loadout,'restore',return_value={'restored':True}) as restore, \
             patch.object(battle,'run_abyss_battle',side_effect=GameError('角色背包已满，服务器拒绝本次战斗结算')) as run:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertIn('角色背包已满',result['detail'])
        self.assertEqual(run.call_count,1);self.assertEqual(restore.call_count,1)
        self.client.bootstrap.assert_not_called();self.assertEqual(result['completedScenes'],[])

    def test_four_stars_advances_immediately(self):
        def run(client,params):self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run) as call:
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(call.call_count,2)
        self.assertEqual([r['attempt'] for r in result['attempts']],[1,1])
        self.assertTrue(all(c.args[1]['useSoul'] for c in call.call_args_list))

    def test_five_non_four_star_clears_then_advances(self):
        def run(client,params):self.settle(params['sceneId'],3 if params['sceneId'].endswith('_1') else 4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run) as call:
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(call.call_count,6)
        self.assertEqual(len([a for a in result['attempts'] if a['sceneId'].endswith('_1')]),5)

    def test_five_losses_without_clear_stops_at_same_stage(self):
        with patch.object(battle,'run_abyss_battle',return_value={'ok':False,'settlementConfirmed':True}) as call:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertEqual(call.call_count,5);self.assertEqual(result['completedScenes'],[])
        self.assertIn('没有找到合适的阵容，过关失败',result['detail'])

    def test_no_legal_auto_team_reports_formation_failure_before_borrowing(self):
        self.params['waves']={'upper':[],'lower':[]}
        with patch.object(team,'choose_teams',side_effect=GameError('合法满级角色不足')) as choose, \
             patch.object(loadout,'apply') as equip,patch.object(battle,'run_abyss_battle') as fight:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertIn('没有找到合适的阵容，过关失败',result['detail'])
        self.assertIn('合法满级角色不足',result['detail'])
        equip.assert_not_called();fight.assert_not_called();self.assertEqual(choose.call_count,1)

    def test_first_attempt_and_all_retries_use_output_teams_in_every_stage(self):
        self.params['waves']={'upper':[],'lower':[]};events=[];self.params['_on_progress']=events.append
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        utilities={'r0':['break'],'r4':['break','blockBuff']};calls=[]
        def fight(client,params):
            calls.append(params['sceneId'])
            if len(calls)==1:return {'ok':False,'settlementConfirmed':True}
            self.settle(params['sceneId'],1 if params['sceneId'].endswith('_1') else 4)
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(team,'choose_teams',return_value={'waves':waves,'reasons':{},'warnings':[],'utilityRoles':utilities}) as choose, \
             patch.object(loadout,'plan',return_value={}) as plan,patch.object(loadout,'apply'), \
             patch.object(battle,'run_abyss_battle',side_effect=fight):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(calls,['MysteriousRealm_1']*5+['MysteriousRealm_2'])
        self.assertEqual([c.kwargs['offensive'] for c in choose.call_args_list],[True]*6)
        self.assertEqual([c.kwargs['offensive'] for c in plan.call_args_list],[True]*6)
        self.assertTrue(all(c.kwargs['utility_roles']==utilities for c in plan.call_args_list))
        self.assertTrue(all(e['attemptLimit']==5 for e in events if 'attemptLimit'in e))
        self.assertTrue(any('输出阵容' in e.get('message','') for e in events))
        self.assertIn('已获 1 星通关','\n'.join(result['lines']))

    def test_a_previous_one_star_clear_survives_later_losses_and_advances_after_five(self):
        calls=[]
        def fight(client,params):
            calls.append(params['sceneId'])
            if len(calls)==1:self.settle(params['sceneId'],1)
            if params['sceneId'].endswith('_2'):self.settle(params['sceneId'],4)
            return {'ok':len(calls)==1 or params['sceneId'].endswith('_2'),'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=fight):result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(calls,['MysteriousRealm_1']*5+['MysteriousRealm_2'])
        self.assertEqual([a['stars'] for a in result['attempts']],[1,0,0,0,0,4])
        self.assertEqual(result['completedScenes'],['MysteriousRealm_1','MysteriousRealm_2'])

    def test_unknown_settlement_or_disconnect_does_not_replay_battle(self):
        for behavior in (GameError('断线'),{'ok':True,'settlementConfirmed':False}):
            with self.subTest(behavior=behavior),patch.object(battle,'run_abyss_battle',side_effect=behavior if isinstance(behavior,Exception) else None,return_value=behavior) as call:
                result=progression.run(self.client,self.params)
                self.assertFalse(result['ok']);self.assertEqual(call.call_count,1)

    def test_timeout_with_new_server_four_stars_advances_after_readback(self):
        events=[];self.params['_on_progress']=events.append
        server=deepcopy(self.state)
        def refresh():self.state.clear();self.state.update(deepcopy(server))
        self.client.bootstrap.side_effect=refresh
        def run(client,params):
            if params['sceneId']=='MysteriousRealm_1':
                server['SceneDataContainer']['Scenes'][0]['Stars']=[1]*4
                raise battle.BattleUnconfirmed('战斗长时间未推进')
            self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run) as call:
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(call.call_count,2)
        self.assertEqual(result['completedScenes'],['MysteriousRealm_1','MysteriousRealm_2'])
        self.assertTrue(result['attempts'][0]['recoveredSettlement'])
        self.assertTrue(any(e['phase']=='checking' for e in events))

    def test_old_three_stars_without_new_evidence_cannot_trigger_replay(self):
        for stars in ([1,1,1,0],[1,1,1,1]):
            record={'cleared':True,'stars':stars,'passCount':1}
            self.assertFalse(progression._fresh_clear(record,deepcopy(record)))
        self.settle('MysteriousRealm_1',3)
        with patch.object(battle,'run_abyss_battle',side_effect=battle.BattleUnconfirmed('断线')) as call:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertEqual(call.call_count,1)
        self.assertEqual(self.client.bootstrap.call_count,3)
        self.assertIn('新的战斗结算',result['detail'])

    def test_increased_pass_count_confirms_three_stars_then_retries_same_stage(self):
        self.settle('MysteriousRealm_1',3);self.state['SceneDataContainer']['Scenes'][0]['PassCount']=1
        self.client.bootstrap.side_effect=lambda:self.state['SceneDataContainer']['Scenes'][0].update(PassCount=2)
        calls=[]
        def run(client,params):
            calls.append(params['sceneId'])
            if len(calls)==1:raise battle.BattleUnconfirmed('断线')
            self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run):result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(calls,['MysteriousRealm_1','MysteriousRealm_1','MysteriousRealm_2'])
        self.assertEqual([a['attempt'] for a in result['attempts']],[1,2,1])
        self.assertEqual(result['attempts'][0]['stars'],3)

    def test_server_eventual_settlement_is_polled_without_creating_another_room(self):
        def refresh():
            if self.client.bootstrap.call_count==2:self.settle('MysteriousRealm_1',4)
        self.client.bootstrap.side_effect=refresh
        def run(client,params):
            if params['sceneId']=='MysteriousRealm_1':raise battle.BattleUnconfirmed('断线')
            self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=run) as call:result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(call.call_count,2);self.assertEqual(self.client.bootstrap.call_count,2)

    def test_empty_auto_teams_restore_assets_before_confirming_timeout_and_advancing(self):
        self.params['waves']={'upper':[],'lower':[]};pending={'value':False};order=[]
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        def apply(*a,**k):pending['value']=True;order.append('apply')
        def restore(*a,**k):pending['value']=False;order.append('restore');return {'restored':True}
        def refresh():order.append('confirm');self.settle('MysteriousRealm_1',4)
        self.client.bootstrap.side_effect=refresh
        def run(client,params):
            order.append('battle')
            if params['sceneId']=='MysteriousRealm_1':raise battle.BattleUnconfirmed('长时间未推进')
            self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(team,'choose_teams',return_value={'waves':waves,'reasons':{},'warnings':[]}),patch.object(loadout,'plan',return_value={}),patch.object(loadout,'apply',side_effect=apply),patch.object(loadout,'pending',side_effect=lambda *a,**k:pending['value']),patch.object(loadout,'restore',side_effect=restore),patch.object(battle,'run_abyss_battle',side_effect=run):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertTrue(result['autoConfigured'])
        self.assertEqual(order,['apply','battle','restore','confirm','apply','battle','restore'])
        self.assertEqual(self.params['waves'],{'upper':[],'lower':[]})

    def test_cancellation_after_timeout_cannot_confirm_or_advance(self):
        signal=threading.Event();self.params['_cancel_event']=signal
        def run(*a,**k):signal.set();raise battle.BattleUnconfirmed('断线')
        with patch.object(battle,'run_abyss_battle',side_effect=run) as call:result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertIn('已中止',result['detail']);self.assertEqual(call.call_count,1)
        self.client.bootstrap.assert_not_called()

    def test_auto_plan_equips_restores_each_attempt_and_publishes_runtime_teams(self):
        self.params['waves']={'upper':[],'lower':[]};events=[];self.params['_on_progress']=events.append
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']};order=[];pending={'value':False}
        def apply(*args,**kwargs):order.append('apply');pending['value']=True
        def restore(*args,**kwargs):order.append('restore');pending['value']=False;return {'restored':True}
        def run(client,params):order.append('battle');self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(team,'choose_teams',return_value={'waves':waves,'reasons':{},'warnings':[]}) as choose,patch.object(loadout,'plan',return_value={}),patch.object(loadout,'apply',side_effect=apply),patch.object(loadout,'pending',side_effect=lambda *a,**k:pending['value']),patch.object(loadout,'restore',side_effect=restore),patch.object(battle,'run_abyss_battle',side_effect=run):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok']);self.assertEqual(order,['apply','battle','restore','apply','battle','restore'])
        self.assertEqual(choose.call_count,2);self.assertTrue(any(e.get('waves')==waves for e in events))
        self.assertEqual(self.params['waves'],{'upper':[],'lower':[]})

    def test_restore_failure_prevents_next_stage_and_reports_recovery(self):
        self.params['waves']={'upper':[],'lower':[]};pending={'value':False}
        def apply(*a,**k):pending['value']=True
        def run(client,params):self.settle(params['sceneId'],4);return {'ok':True,'settlementConfirmed':True}
        with patch.object(team,'choose_teams',return_value={'waves':{'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']},'reasons':{},'warnings':[]}),patch.object(loadout,'plan',return_value={}),patch.object(loadout,'apply',side_effect=apply),patch.object(loadout,'pending',side_effect=lambda *a,**k:pending['value']),patch.object(loadout,'restore',return_value={'restored':False,'error':'未确认归还'}),patch.object(battle,'run_abyss_battle',side_effect=run) as call:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertTrue(result['recoveryRequired']);self.assertEqual(call.call_count,1)

    def test_real_teams_and_real_recommender_borrow_then_restore_over_two_stages(self):
        state=roster();inventory,_=fixture(16,roles_count=24)
        state['EquipmentContainer']=inventory['EquipmentContainer']
        for item in state['EquipmentContainer']['Datas']:item['EquipRole']='r'+str(8+int(_oid(item['_id']).split('-')[1]))
        legal=[r for r in state['RoleDataContainer']['Roles'] if metadata()['tables']['Role'][r['StaticID']]['Job']!='Medic'][:8]
        for role in state['RoleDataContainer']['Roles']:role['LV']=60 if role in legal else 59
        client=AssetServer(state);before=loadout._owners(client.server['EquipmentContainer']['Datas'])
        params={**self.params,'waves':{'upper':[],'lower':[]}};seen=[]
        def fight(c,request):
            ids=request['waves']['upper']+request['waves']['lower'];gear=[]
            for rid in ids:
                owned,_=plans.worn_items(plans._equips(c),rid);self.assertEqual(len(owned),6);gear.extend(_oid(item['_id']) for item in owned)
            self.assertEqual(len(set(gear)),48);self.assertTrue(loadout.pending(c));seen.append(request['sceneId'])
            rows=c.server['SceneDataContainer']['Scenes'];record=next((r for r in rows if r['StaticID']==request['sceneId']),None)
            if record is None:record={'StaticID':request['sceneId']};rows.append(record)
            record['Stars']=[1]*4;c.bootstrap();return {'ok':True,'settlementConfirmed':True}
        with patch.object(battle,'run_abyss_battle',side_effect=fight):result=progression.run(client,params)
        self.assertTrue(result['ok']);self.assertEqual(len(seen),2)
        self.assertEqual(loadout._owners(client.server['EquipmentContainer']['Datas']),before)
        self.assertFalse(loadout.pending(client))

    def test_cancel_after_borrowing_still_runs_restoration_and_never_advances(self):
        params={**self.params,'waves':{'upper':[],'lower':[]},'_cancel_event':threading.Event()};pending={'value':False};calls=[]
        def apply(*a,**k):pending['value']=True
        def restore(*a,**k):calls.append('restore');pending['value']=False;return {'restored':True}
        def fight(*a,**k):params['_cancel_event'].set();raise GameError('战斗已中止')
        with patch.object(team,'choose_teams',return_value={'waves':{'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']},'reasons':{},'warnings':[]}),patch.object(loadout,'plan',return_value={}),patch.object(loadout,'apply',side_effect=apply),patch.object(loadout,'pending',side_effect=lambda *a,**k:pending['value']),patch.object(loadout,'restore',side_effect=restore),patch.object(battle,'run_abyss_battle',side_effect=fight) as battle_call:
            result=progression.run(self.client,params)
        self.assertFalse(result['ok']);self.assertEqual(calls,['restore']);self.assertEqual(battle_call.call_count,1)


class ProgressionNoRoleIsolation(unittest.TestCase):
    """Automatic borrowing must survive a hero instance the server no longer knows."""

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        patch.object(config,'data_dir',return_value=Path(self.temp.name)).start()
        self.state=roster();inventory,_=fixture(16,roles_count=24)
        self.state['EquipmentContainer']=inventory['EquipmentContainer']
        for item in self.state['EquipmentContainer']['Datas']:
            item['EquipRole']='r'+str(8+int(_oid(item['_id']).split('-')[1]))
        legal=[r for r in self.state['RoleDataContainer']['Roles'] if metadata()['tables']['Role'][r['StaticID']]['Job']!='Medic'][:8]
        for role in self.state['RoleDataContainer']['Roles']:role['LV']=60 if role in legal else 59
        self.state['SceneDataContainer']={'Scenes':[{'StaticID':'MysteriousRealm_1','Stars':[0,0,0,0]}]}
        self.client=AssetServer(self.state)
        meta={'tables':{'Scene':{'MysteriousRealm_1':{}},'Fantasy':{'MysteriousRealm_1':[1]}}}
        for target in (patch.object(progression,'metadata',return_value=meta),
                       patch.object(config,'get_request_delay_config',return_value={'enabled':False}),
                       patch.object(progression.time,'sleep')):
            target.start();self.addCleanup(target.stop)
        self.params={'sceneId':'MysteriousRealm_1','waves':{'upper':[],'lower':[]},'autoAdvance':True,'_account_key':'offline'}

    def strict_server(self):
        original=self.client.call
        def call(route,body):
            roles={_oid(r['_id']) for r in self.client.server['RoleDataContainer']['Roles']}
            if route.startswith(('EquipmentHandler.','ArtifactHandler.')) and body.get('RoleID') not in roles:
                raise GameMessage('No Role')
            return original(route,body)
        return call

    def settle(self,client,params):
        rows=client.server['SceneDataContainer']['Scenes']
        record=next((r for r in rows if r['StaticID']==params['sceneId']),None)
        if record is None:record={'StaticID':params['sceneId']};rows.append(record)
        record['Stars']=[1,1,1,1];client.bootstrap()
        return {'ok':True,'settlementConfirmed':True}

    def test_orphaned_loan_is_isolated_and_the_stage_still_clears(self):
        item=next(e for e in self.client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole')))
        aid=_oid(item['_id']);item['EquipRole']='deleted-role';self.client.bootstrap()
        with patch.object(self.client,'call',side_effect=self.strict_server()), \
             patch.object(battle,'run_abyss_battle',side_effect=lambda c,p:self.settle(c,p)):
            result=progression.run(self.client,self.params)
        self.assertTrue(result['ok'],result)
        self.assertIn(aid,loadout.deferred_assets(self.client,'offline')['equipment'])
        self.assertFalse(loadout.pending(self.client,'offline'))
        self.assertIn('保留原始归属',result['recoveryWarning'])
        # The isolated piece keeps its recorded owner instead of being reported as returned.
        saved=json.loads(loadout._deferred_path(self.client,'offline').read_text())
        self.assertEqual(saved['originalDestinations']['equipment'][aid],'deleted-role')

    def test_repeated_apply_rejections_stop_after_a_bounded_number_of_plans(self):
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        original=self.client.call;plans_seen=[]
        def call(route,body):
            if route=='EquipmentHandler.EquipsToRole' and body.get('RoleID')=='r0':
                plans_seen.append(body['RoleID']);raise GameMessage('No Role')
            return original(route,body)
        with patch.object(team,'choose_teams',return_value={'waves':waves,'reasons':{},'warnings':[]}), \
             patch.object(self.client,'call',side_effect=call), \
             patch.object(battle,'run_abyss_battle',side_effect=lambda c,p:self.settle(c,p)) as fight:
            result=progression.run(self.client,self.params)
        self.assertFalse(result['ok']);self.assertIn('No Role',result['detail'])
        self.assertEqual(len(plans_seen),progression.APPLY_RECOVERY_LIMIT+1)
        self.assertEqual(fight.call_count,0)
        self.assertFalse(loadout.pending(self.client,'offline'))


if __name__=='__main__':unittest.main()
