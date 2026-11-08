"""Offline elite teams, support slots, five defeats, leases and exact returns."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import activity_team, activity_elite, activity_progress, abyss_loadout, abyss_battle, appcore, config
from backend.abyss_state import metadata, initialize_units, sync_activity_support, sync_units
from backend.game_client import GameError, GameClient
from backend.tasks import _oid
from tools.test_abyss_auto import AssetServer
from tools.test_intimacy import farm_state

SID = 'BH806_1_13'


def state_fixture():
    state = farm_state()
    state['ItemContainer'] = {'Items':[{'StaticID':'3','Count':9999}]}
    state['ActivityDBSettingContainer']['SettingList'].extend([
        {'ActivityID':aid,'IsIgnoreTime':True}for aid in ('BranchH806','Version_Elite')])
    state['SceneDataContainer']['Scenes'] = [{'StaticID':f'BH806_1_{i}','Stars':[1,0,0]}for i in range(1,13)]
    state['Teams'] = {'Settings':[{'ID':'user-team','TeamSetting':{'RolePosMap':{'r0':0}}}]}
    return state


def friend(sid='H165', cuid=123, level=60):
    row=metadata()['tables']['Role'][sid]
    role={'_id':'peer-'+str(cuid),'StaticID':sid,'LV':level,'Star':6,'AwakenLV':6,
          'Skills':{'Skills':[{'StaticID':s.split(':')[0],'Level':1}for s in row.get('SkillIDs','').split('~')if s]}}
    return {'IsFriend':True,'PlayerInfo':{'CUID':cuid,'Name':'测试好友'},
            'RoleDataList':[{'SupportJob':'Captain','Role':role}]}


def ownership(state):
    return {key:{_oid(row['_id']):_oid(row.get('EquipRole'))or''for row in state.get(container,{}).get(field,[])}
            for key,container,field in [('equipment','EquipmentContainer','Datas'),('artifacts','Artifacts','Datas')]}


class EliteTeamTests(unittest.TestCase):
    def test_owned_aoi_and_big_brother_start_first_with_big_brother_in_slot_four_then_try_counters(self):
        state=state_fixture()
        for index,sid in ((0,'H165'),(2,'H119')):
            role=deepcopy(friend(sid)['RoleDataList'][0]['Role']);role['_id']='r'+str(index)
            state['RoleDataContainer']['Roles'][index]=role
        scene=metadata()['tables']['Scene'][SID]
        first=activity_team.choose(state,scene,[friend()])
        self.assertIn('H119',first['staticIds']);self.assertIn('H165',first['staticIds'])
        self.assertEqual(first['positions']['r0'],3)
        self.assertEqual(len(first['healerNames']),1)
        second=activity_team.choose(state,scene,[friend()],usage=Counter(first['staticIds']),excluded={first['key']})
        self.assertFalse({'H119','H165'}.issubset(second['staticIds']))
        if 'r0'in second['positions']:self.assertEqual(second['positions']['r0'],3)
        self.assertNotEqual(first['key'],second['key'])

    def test_hidden_urd_heal_does_not_qualify_for_active_healer_and_active_healer_uses_tank_gear(self):
        state=state_fixture()
        urd=deepcopy(friend('H156')['RoleDataList'][0]['Role']);urd['_id']='urd'
        state['RoleDataContainer']['Roles'].append(urd)
        self.assertFalse(activity_team.active_healing(urd)['heal'])
        chosen=activity_team.choose(state,metadata()['tables']['Scene'][SID],[friend()])
        self.assertNotIn('urd',chosen['roles'])
        self.assertIn('乔依丝',chosen['healerNames'])
        profile=next(iter(chosen['profileOverrides'].values()))
        self.assertEqual(profile['priorities'][:2],['HP','Defence'])
        self.assertNotIn('damage',profile['priorities'])

    def test_old_death_events_cannot_recreate_placeholder_after_friend_replaces_hero(self):
        state=state_fixture();entry=friend();scene=metadata()['tables']['Scene'][SID]
        chosen=activity_team.choose(state,scene,[entry])
        info=activity_progress.start_info(state,SID,'',chosen['support'],role_positions=chosen['positions'])
        waves={'Waves':[{'Monsters':w['Ms']} for w in scene['Waves']]}
        units=initialize_units(info,waves,single_team=True)
        old='1-0-0';new='1-2-0'
        dead_event={'TargetRoleID':old,'IsCanSyncRole':1,'NowHP':0}
        frame={'ActionResult':{'NewSupportRole':{'ID':new,'CampPosition':0,
                'RoleData':{'_id':entry['RoleDataList'][0]['Role']['_id']}},
                'SkillEventList':[{'RoleEventList':[dead_event]}]}}
        retired=sync_activity_support(frame,units,info,waves)
        sync_units(frame,units,ignored_ids=retired)
        sync_units({'ActionResult':{'SkillEventList':[{'RoleEventList':[dead_event]}]}},units,ignored_ids=retired)
        self.assertNotIn(old,units)
        self.assertEqual(units[new]['StaticID'],'H165')
        self.assertEqual(len([key for key in units if key.startswith('1-')]),4)
        snapshot=appcore._battle_progress_brief(abyss_battle._progress_snapshot(SID,2,units,single_team=True))
        borrowed=next(row for row in snapshot['allies']if row['id']==new)
        self.assertTrue(borrowed['avatar']);self.assertTrue(borrowed['healthPending'])
        self.assertIsNone(borrowed['hpPercent']);self.assertFalse(borrowed['dead'])
        sync_units({'ActionResult':{'SkillEventList':[{'RoleEventList':[{'TargetRoleID':new,'IsCanSyncRole':1,'NowHP':1500}]}]}},units,ignored_ids=retired)
        borrowed=next(row for row in abyss_battle._progress_snapshot(SID,2,units,single_team=True)['allies']if row['id']==new)
        self.assertFalse(borrowed['healthPending']);self.assertGreater(borrowed['hpPercent'],0)
    def test_big_brother_in_vanguard_slot_is_preferred_over_captain_and_keeps_its_job(self):
        entry=friend('H027')
        vanguard=friend('H165')['RoleDataList'][0];vanguard['SupportJob']='Vanguard'
        entry['RoleDataList'].append(vanguard)
        chosen=activity_team.choose(state_fixture(),metadata()['tables']['Scene'][SID],[entry])
        self.assertEqual(chosen['supportStaticId'],'H165')
        self.assertEqual(chosen['support']['Job'],'Vanguard')

    def test_automatic_friend_query_keeps_other_job_slots_even_if_captain_is_unset(self):
        from backend.tasks import query_support_friends
        entry=friend();entry['RoleDataList'][0]['SupportJob']='Vanguard'
        client=SimpleNamespace(call=Mock(return_value={'Datas':[entry]}),_auth_data=lambda:{})
        self.assertEqual(query_support_friends(client)[1],{})
        self.assertEqual(query_support_friends(client,include_all_jobs=True)[1],{'123':entry})

    def test_four_owned_starters_include_healer_breaker_and_aoe_with_friend_in_reserve(self):
        state=state_fixture(); entry=friend(); before=deepcopy((state,entry))
        scene=metadata()['tables']['Scene'][SID]
        chosen=activity_team.choose(state,scene,[entry,friend('H146',124)])
        self.assertEqual(chosen['supportStaticId'],'H165')
        self.assertEqual(len(chosen['roles']),4)
        self.assertEqual(len(chosen['healerNames']),1)
        self.assertTrue(any(activity_team._candidate(r,state)['breaker']
            for r in state['RoleDataContainer']['Roles']if _oid(r['_id'])in chosen['roles']))
        self.assertTrue(any(activity_team.abyss_team.combat_role(r)['kind']=='area'
            for r in state['RoleDataContainer']['Roles']if _oid(r['_id'])in chosen['roles']))
        self.assertEqual(len(scene['Waves']),3)
        info=activity_progress.start_info(state,SID,'ignored',chosen['support'],role_positions=chosen['positions'])
        waves={'Waves':[{'Monsters':wave['Ms']}for wave in scene['Waves']]}
        units=initialize_units(info,waves,single_team=True)
        allies=[u for key,u in units.items()if key.startswith('1-0-')]
        self.assertEqual(len(allies),4)
        self.assertEqual(len({u['StaticID']for u in allies}),4)
        self.assertNotIn('H165',[u['StaticID']for u in allies])
        self.assertFalse(any(u.get('_IsSupport') for u in allies))
        self.assertEqual((state,entry),before)

    def test_owned_big_brother_starts_and_borrowed_big_brother_replaces_only_after_server_event(self):
        state=state_fixture();entry=friend()
        own=deepcopy(entry['RoleDataList'][0]['Role']);own['_id']='r0'
        state['RoleDataContainer']['Roles'][0]=own
        scene=metadata()['tables']['Scene'][SID]
        chosen=activity_team.choose(state,scene,[entry])
        self.assertIn('r0',chosen['roles']);self.assertIn('H165',chosen['staticIds'])
        self.assertEqual(chosen['supportStaticId'],'H165')
        info=activity_progress.start_info(state,SID,'ignored',chosen['support'],role_positions=chosen['positions'])
        waves={'Waves':[{'Monsters':wave['Ms']}for wave in scene['Waves']]}
        units=initialize_units(info,waves,single_team=True)
        allies=[u for key,u in units.items()if key.startswith('1-')]
        self.assertEqual(len(allies),4);self.assertFalse(any(u.get('_IsSupport') for u in allies))
        victim=next(u for u in allies if u['_id']!='r0')
        victim['NowHP']=0;victim['IsDieOut']=True
        pos=victim['Position'];uid='1-2-'+str(pos)
        sync_activity_support({'ActionResult':{'NewSupportRole':{'ID':uid,'CampPosition':pos,
            'RoleData':entry['RoleDataList'][0]['Role'],'NowHP':1234}}},units,info,waves)
        self.assertEqual(len([key for key in units if key.startswith('1-')]),4)
        self.assertEqual(units[uid]['_id'],entry['RoleDataList'][0]['Role']['_id'])
        self.assertTrue(units[uid]['_IsSupport'])
        self.assertTrue(any(u['_id']=='r0' for key,u in units.items()if key.startswith('1-')))

    def test_failure_selects_a_different_group_and_keeps_big_brother_reserve_priority(self):
        state=state_fixture();scene=metadata()['tables']['Scene'][SID];friends=[friend(),friend('H146',124)]
        first=activity_team.choose(state,scene,friends)
        second=activity_team.choose(state,scene,friends,excluded={first['key']},
            usage=Counter({**{sid:1 for sid in first['staticIds']},'support:H165':1}))
        self.assertNotEqual(first['key'],second['key'])
        self.assertEqual(second['supportStaticId'],'H165')
        self.assertEqual(len(second['healerNames']),1)

    def test_no_friend_uses_four_own_heroes_and_can_use_a_nonmaximum_healer(self):
        state=state_fixture()
        for role in state['RoleDataContainer']['Roles']:
            if activity_team._candidate(role,state)['healer']:
                role['LV']=50
        chosen=activity_team.choose(state,metadata()['tables']['Scene'][SID])
        self.assertEqual(len(chosen['roles']),4)
        self.assertIsNone(chosen['support'])
        self.assertEqual(len(chosen['healerNames']),1)

    def test_no_healer_refuses_before_any_borrow_and_nonfriends_are_ignored(self):
        state=state_fixture()
        state['RoleDataContainer']['Roles']=[r for r in state['RoleDataContainer']['Roles']
            if not activity_team._candidate(r,state)['healer']]
        stranger=friend('H029');stranger['IsFriend']=False
        with self.assertRaisesRegex(GameError,'治疗'):
            activity_team.choose(state,metadata()['tables']['Scene'][SID],[stranger])

    def test_reserve_breaker_cannot_replace_an_owned_starter_breaker(self):
        state=state_fixture()
        state['RoleDataContainer']['Roles']=[r for r in state['RoleDataContainer']['Roles']
            if not activity_team._candidate(r,state)['breaker']]
        with self.assertRaisesRegex(GameError,'破甲'):
            activity_team.choose(state,metadata()['tables']['Scene'][SID],[friend('H121')])


class EliteTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.object(config,'data_dir',return_value=Path(self.directory.name)))
        self.client=AssetServer(state_fixture());self.cancel=threading.Event();self.before=deepcopy(self.client.server)
        self.provider=Mock(return_value=[friend(),friend('H146',124)])

    def run_elite(self, outcome, **kwargs):
        with patch.object(abyss_battle,'run_activity_battle',side_effect=outcome) as battle:
            result=activity_elite.run(self.client,SID,cancel_event=self.cancel,
                support_provider=self.provider,account_key='test',**kwargs)
        self.assertEqual(ownership(self.client.server),ownership(self.before))
        self.assertEqual(self.client.server['Teams'],self.before['Teams'])
        self.assertFalse(abyss_loadout.pending(self.client,'test'))
        return result,battle

    def win(self,client,info,**kwargs):
        self.assertEqual(len(info['CampData1']['PositionRoleMap']),4)
        self.assertIn('Support',info)
        self.assertTrue(kwargs['reserve_boss_tools'])
        client.server['SceneDataContainer']['Scenes'].append({'StaticID':SID,'Stars':[1,0,0]})
        client.bootstrap()
        return {'ok':True,'confirmed':True,'lines':['获得测试掉落']}

    def test_win_restores_all_original_owners_and_keeps_confirmed_drops(self):
        result,battle=self.run_elite(self.win)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['lines'],['获得测试掉落'])
        self.assertEqual(battle.call_count,1)
        self.assertIsNone(self.client._automation_asset_owner)

    def test_five_confirmed_losses_change_teams_and_restore_before_next_challenge(self):
        previous=None;seen=[]
        def lose(client,info,**kwargs):
            nonlocal previous
            if previous is not None:
                self.assertEqual(ownership_before_next[-1],ownership(self.before))
            seen.append(tuple(sorted(row['StaticID']for row in info['CampData1']['PositionRoleMap'].values()))+
                        ((info.get('Support') or {}).get('PlayerRoleData',{}).get('RoleData',{}).get('StaticID',''),))
            previous=info
            return {'ok':False,'confirmed':True,'detail':'战斗失败'}
        ownership_before_next=[]
        original_choose=activity_team.choose
        def choose(state,*args,**kwargs):
            ownership_before_next.append(ownership(state))
            return original_choose(state,*args,**kwargs)
        with patch.object(activity_team,'choose',side_effect=choose):
            result,battle=self.run_elite(lose)
        self.assertEqual(battle.call_count,5)
        self.assertEqual(len(set(seen)),5)
        self.assertFalse(result['ok'])
        self.assertIn('手动挑战',result['detail'])
        self.assertEqual(self.provider.call_count,5)

    def test_cancel_after_partial_equip_returns_assets_and_never_starts_room(self):
        def cancel_on_equip():
            if self.client.calls[-1][0]=='EquipmentHandler.EquipsToRole':
                self.cancel.set();self.client.after_call=None
        self.client.after_call=cancel_on_equip
        result,battle=self.run_elite(self.win)
        self.assertFalse(result['ok']);battle.assert_not_called()

    def test_cancel_in_battle_reacquires_lock_and_returns_assets(self):
        lock=threading.Lock();lock.acquire()
        def cancel(client,info,**kwargs):
            self.assertTrue(lock.acquire(blocking=False))
            lock.release();self.cancel.set()
            return {'ok':False,'confirmed':True}
        try:
            result,battle=self.run_elite(cancel,account_lock=lock)
            self.assertTrue(lock.locked());self.assertFalse(result['ok'])
        finally:lock.release()

    def test_unknown_result_is_synced_and_unfinished_stage_changes_teams_up_to_five(self):
        result,battle=self.run_elite(lambda *args,**kwargs:{'ok':False,'confirmed':False})
        self.assertFalse(result['ok']);self.assertEqual(battle.call_count,5)
        self.assertIsNone(self.client._activity_pending)
        self.assertTrue(all(row['syncedUncleared']for row in result['attempts']))

    def test_used_support_rejection_changes_friend_after_restoring_and_is_not_an_unknown_result(self):
        used=[]
        def fight(client,info,**kwargs):
            cuid=info['Support']['PlayerRoleData']['PlayerInfo']['CUID']
            used.append(cuid)
            if len(used)==1:
                raise abyss_battle.SupportUnavailable('战斗服务器返回错误：Support Use Twice')
            self.assertNotEqual(cuid,used[0])
            return self.win(client,info,**kwargs)
        result,battle=self.run_elite(fight)
        self.assertTrue(result['ok'],result)
        self.assertEqual(battle.call_count,2)
        self.assertFalse(result['attempts'][0]['battleAttempted'])
        self.assertTrue(result['attempts'][0]['supportUnavailable'])
        self.assertIsNone(self.client._activity_pending)

    def test_failed_return_keeps_journal_and_reserves_original_items_until_recovery(self):
        with patch.object(abyss_battle,'run_activity_battle',return_value={'ok':False,'confirmed':True}) as battle, \
             patch.object(abyss_loadout,'restore',return_value={'restored':False,'error':'网络不可用'}):
            result=activity_elite.run(self.client,SID,cancel_event=self.cancel,
                support_provider=self.provider,account_key='test')
        self.assertEqual(battle.call_count,1)
        self.assertTrue(result['recoveryRequired'])
        self.assertIn('归还尚未确认',result['detail'])
        self.assertTrue(abyss_loadout.pending(self.client,'test'))
        self.assertTrue(self.client._automation_reserved_equipment)
        recovered=abyss_loadout.restore(self.client,account_key='test')
        self.assertTrue(recovered['restored'])
        self.assertFalse(self.client._automation_reserved_equipment)
        self.assertEqual(ownership(self.client.server),ownership(self.before))

    def test_lost_ack_recovers_new_clear_from_return_bootstrap_without_duplicate_battle(self):
        def interrupted(client,info,**kwargs):
            self.win(client,info,**kwargs)
            raise abyss_battle.BattleUnconfirmed('结算确认中断')
        result,battle=self.run_elite(interrupted)
        self.assertTrue(result['ok']);self.assertEqual(battle.call_count,1)

    def test_static_id_error_after_clear_is_reconciled_as_one_confirmed_win(self):
        def settled(client,info,**kwargs):
            self.win(client,info,**kwargs)
            raise KeyError('StaticID')
        result,battle=self.run_elite(settled)
        self.assertTrue(result['ok'],result);self.assertTrue(result['confirmed'])
        self.assertEqual(battle.call_count,1)
        self.assertTrue(result['attempts'][0]['recoveredSettlement'])
        self.assertIsNone(self.client._activity_pending)

    def test_clear_receipt_survives_stale_logins_during_asset_return(self):
        def acknowledged(client,info,**kwargs):
            client.account_state['SceneDataContainer']['Scenes'].append({'StaticID':SID,'Stars':[1,0,0]})
            return {'ok':True,'confirmed':True}
        result,battle=self.run_elite(acknowledged)
        self.assertTrue(result['ok'],result);self.assertEqual(battle.call_count,1)
        self.assertTrue(activity_progress.cleared(activity_progress.records(self.client.account_state).get(SID)))

    def test_late_server_clear_after_static_id_error_is_checked_without_replaying(self):
        original=self.client.bootstrap;after_error=False;syncs=0
        def bootstrap():
            nonlocal syncs
            if after_error:
                syncs+=1
                if syncs==3:self.client.server['SceneDataContainer']['Scenes'].append({'StaticID':SID,'Stars':[1,0,0]})
            original()
        def interrupted(*args,**kwargs):
            nonlocal after_error
            after_error=True
            raise KeyError('StaticID')
        with patch.object(self.client,'bootstrap',side_effect=bootstrap):
            result,battle=self.run_elite(interrupted)
        self.assertTrue(result['ok'],result);self.assertEqual(battle.call_count,1)

    def test_static_id_error_without_server_clear_never_fabricates_completion(self):
        def missing(*args,**kwargs):raise KeyError('StaticID')
        result,battle=self.run_elite(missing)
        self.assertFalse(result['ok'])
        self.assertEqual(battle.call_count,5)
        self.assertFalse(activity_progress.cleared(activity_progress.records(self.client.account_state).get(SID)))

    def test_preparing_borrow_has_healer_survival_profile_and_never_equips_friend(self):
        original=abyss_loadout.plan;plans=[]
        def plan(*args,**kwargs):
            self.assertTrue(kwargs['profile_overrides'])
            self.assertTrue(all('damage'not in p['priorities']for p in kwargs['profile_overrides'].values()))
            result=original(*args,**kwargs);plans.append(result);return result
        with patch.object(abyss_loadout,'plan',side_effect=plan):
            self.run_elite(self.win)
        self.assertEqual(len(plans[0]['targets']),4)
        for route,body in self.client.calls:
            self.assertFalse(str(body.get('RoleID','')).startswith(('peer-','support:')))

    def test_stage_sequencer_ignores_selected_team_on_thirteen_and_uses_fresh_clear_after_return(self):
        emitted=[];progress=[]
        with patch.object(abyss_battle,'run_activity_battle',side_effect=self.win):
            result=activity_progress.run(self.client,'BH806','nonexistent-user-team',
                cancel_event=self.cancel,support_provider=self.provider,account_key='test',
                on_partial=emitted.append,on_progress=progress.append)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['progress']['completed'],13)
        self.assertEqual(result['progress']['total'],13)
        self.assertEqual(progress[-1]['completed'],13)
        self.assertEqual(result['completedScenes'],[SID])
        self.assertEqual(len(result['eliteAttempts']),1)
        self.assertEqual(ownership(self.client.server),ownership(self.before))
        self.assertEqual(len(emitted),1)
        self.assertTrue(any('不使用所选队伍'in row.get('detail','')for row in progress))


class ActivityLeaseTests(unittest.TestCase):
    def test_gear_and_relogin_are_blocked_but_unrelated_shops_remain_available(self):
        client=GameClient.__new__(GameClient)
        client._automation_asset_owner=threading.get_ident()+1
        client._automation_asset_label='活动自动推图'
        client._automation_reserved_equipment={'loan'}
        with patch.object(client,'_call_serial',return_value={'ok':True}):
            self.assertTrue(client.call('StoreHandler.ResetRandomStore',{})['ok'])
            for route in ('EquipmentHandler.EquipsToRole','ArtifactHandler.Equip','RoomHandler.CreateRoomByScene'):
                with self.assertRaisesRegex(GameError,'活动自动推图'):client.call(route,{})
            with self.assertRaises(GameError):client.call('EquipmentHandler.SellEquips',{'IDs':['loan']})
            self.assertTrue(client.call('EquipmentHandler.SellEquips',{'IDs':['other']})['ok'])
            with self.assertRaisesRegex(GameError,'借装任务'):client.bootstrap()

    def test_other_battles_cannot_start_while_an_activity_session_is_running(self):
        acc=SimpleNamespace(email='activity-user',client=SimpleNamespace(account_state={}))
        with patch.object(appcore,'_require',return_value=acc), \
             patch.dict(appcore._activity_sessions,{'activity-user':threading.Event()}):
            with self.assertRaises(appcore.ApiError):appcore.intimacy_start('activity-user')
            with self.assertRaises(appcore.ApiError):appcore.activity_progress_run('activity-user','BH806','T')

    def test_account_close_cancels_activity_and_waits_for_cleanup_before_closing_transport(self):
        from backend.accounts import Account
        done=threading.Event();cancel=threading.Event();closed=threading.Event()
        account=SimpleNamespace(stop_scheduler=Mock(),lock=threading.Lock(),
            activity_cancel_event=cancel,activity_done_event=done,client=SimpleNamespace(close=closed.set))
        Account.close(account)
        self.assertTrue(cancel.is_set());self.assertFalse(closed.is_set())
        done.set();self.assertTrue(closed.wait(2))


if __name__=='__main__':unittest.main()
