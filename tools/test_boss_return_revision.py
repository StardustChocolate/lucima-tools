"""Boss-only selection and best-effort final returns through real loadout journals."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from backend import abyss_battle, abyss_loadout, abyss_team, activity_team, activity_elite, config, equipment_sale
from backend.abyss_state import metadata
from backend.tasks import _oid
from backend.game_client import GameError
from tools.test_abyss_auto import AssetServer
from tools.test_abyss_battle import unit
from tools.test_activity_elite import state_fixture, SID, ownership, friend


class BossFocus(unittest.TestCase):
    def actor(self,sid):
        return unit('1-0-0',StaticID=sid,NowSP=100,
                    Skills={'Skills':[{'StaticID':sid+'S'+str(i),'Level':1}for i in (1,2,3)]},NowSkillCooldownMap={})

    def test_live_ai_soul_and_fallback_target_boss_despite_low_hp_add(self):
        boss=unit('2-0-1',StaticID='H002',IsBoss=True,NowHP=99999)
        add=unit('2-0-0',StaticID='H001',NowHP=1)
        for sid in ('H119','H121','H165','H146'):
            for use_soul in (False,True):
                with self.subTest(sid=sid,soul=use_soul):
                    actor=self.actor(sid)
                    choice,_,_=abyss_battle._battle_action(actor,[actor],[add,boss],40,
                        use_soul=use_soul,prioritize_soul_break=True,reserve_boss_tools=True,wave_index=2,wave_count=3)
                    self.assertIsNotNone(choice)
                    self.assertEqual(choice[2],boss['id'])
        actor=self.actor('H119')
        with patch.object(abyss_battle,'_choose_action',return_value=None):
            choice,_,_=abyss_battle._battle_action(actor,[actor],[add,boss],0)
        self.assertEqual(choice[2],boss['id'])

    def test_dead_boss_never_switches_to_surviving_add(self):
        actor=self.actor('H119');boss=unit('2-0-1',StaticID='H002',IsBoss=True,NowHP=0)
        add=unit('2-0-0',StaticID='H001',NowHP=1)
        choice,_,_=abyss_battle._battle_action(actor,[actor],[boss,add],40,use_soul=True,prioritize_soul_break=True)
        self.assertIsNone(choice)

    def test_normal_mob_wave_retains_all_target_candidates(self):
        actor=self.actor('H119');mobs=[unit('2-0-0',StaticID='H001'),unit('2-0-1',StaticID='H002')]
        with patch.object(abyss_battle,'_choose_action',return_value=('H119S1',1,mobs[1]['id'])) as choose:
            result=abyss_battle._battle_action(actor,[actor],mobs,0)
        self.assertEqual(choose.call_args.args[-1],mobs)
        self.assertEqual(result[0][2],mobs[1]['id'])

    def test_add_taunt_does_not_send_an_illegal_boss_command(self):
        actor=self.actor('H119');actor['IsTaunt']=True
        boss=unit('2-0-1',StaticID='H002',IsBoss=True)
        add=unit('2-0-0',StaticID='H001')
        with patch.object(abyss_battle,'_target_pass',return_value=add['id']):
            choice,reason,burn=abyss_battle._battle_action(actor,[actor],[boss,add],40,use_soul=True)
        self.assertIsNone(choice);self.assertEqual(reason,'taunted_by_add');self.assertFalse(burn)

    def test_boss_team_and_loadout_ignore_add_elements_and_abilities(self):
        state=state_fixture();scene=deepcopy(metadata()['tables']['Scene'][SID])
        boss=next(m for m in scene['Waves'][-1]['Ms'] if m.get('Boss'))
        scene['Waves'][-1]['Ms']=[boss]
        baseline=activity_team.choose(state,scene,[friend()])
        base_loadout=abyss_loadout.plan(state,{},role_ids=baseline['roles'],offensive=True,
            utility_roles=baseline['utilityRoles'],profile_overrides=baseline['profileOverrides'],equip_artifacts=True)
        # Unknown adds must not even need local metadata to configure this boss.
        scene['Waves'][-1]['Ms'].append({'M':'UNINDEXED_ADD','Boss':False})
        chosen=activity_team.choose(state,scene,[friend()])
        self.assertEqual(chosen,baseline)
        changed=abyss_loadout.plan(state,{},role_ids=chosen['roles'],offensive=True,
            utility_roles=chosen['utilityRoles'],profile_overrides=chosen['profileOverrides'],equip_artifacts=True)
        for field in ('targets','artifactTargets'):self.assertEqual(changed[field],base_loadout[field])
        self.assertEqual(abyss_team._enemy_traits({'Monsters':[boss]}),
                         abyss_team._enemy_traits({'Monsters':scene['Waves'][-1]['Ms']}))


class EliteReturns(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)
        override=patch.object(config,'data_dir',return_value=self.path);override.start();self.addCleanup(override.stop)

    def test_stage_13_final_return_survives_broken_progress_callback(self):
        state=state_fixture();client=AssetServer(state);before=ownership(client.server)
        def progress(value):
            if value.get('phase')=='restoring':raise RuntimeError('view destroyed')
        with patch.object(abyss_battle,'run_activity_battle',return_value={'ok':False,'confirmed':True}):
            activity_elite.run(client,SID,cancel_event=threading.Event(),account_key='test',on_progress=progress)
        self.assertEqual(ownership(client.server),before)
        self.assertFalse(abyss_loadout.pending(client,'test'))

    def test_stage_13_returns_other_loans_when_one_piece_rejects_then_retries(self):
        state=state_fixture();client=AssetServer(state);before=ownership(client.server)
        original=client.call;rejected=[];battle_entered=[False]
        def battle(*args,**kwargs):battle_entered[0]=True;return {'ok':False,'confirmed':True}
        def call(route,body):
            if battle_entered[0] and route=='EquipmentHandler.EquipsToRole':
                bad=next((aid for aid in body['EquipIDs'] if before['equipment'].get(aid)==body['RoleID']),None)
                if not rejected and bad:rejected.append(bad)
                if rejected and rejected[0] in body['EquipIDs']:raise GameError('one broken item')
            return original(route,body)
        with patch.object(abyss_battle,'run_activity_battle',side_effect=battle),patch.object(client,'call',side_effect=call):
            result=activity_elite.run(client,SID,cancel_event=threading.Event(),account_key='test')
        self.assertTrue(rejected)
        self.assertTrue(result['recoveryRequired'],result)
        actual=ownership(client.server)
        self.assertEqual({aid for aid in before['equipment']if actual['equipment'][aid]!=before['equipment'][aid]},set(rejected))
        restarted=AssetServer(client.server);restarted._asset_recovery_key='test'
        reserved=abyss_loadout.reserved_equipment(restarted)
        self.assertIn(rejected[0],reserved)
        self.assertTrue(abyss_loadout.restore(restarted,account_key='test')['restored'])
        self.assertEqual(ownership(restarted.server),before)


if __name__=='__main__':unittest.main()
