"""Bounded end-of-lower-wave protocol tests, with no game or network access."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from backend import abyss_battle as battle, abyss_progression as progression, abyss_team, abyss_loadout, config
from backend.game_client import GameError


def role_event(uid, hp):
    return {'TargetRoleID': uid, 'IsCanSyncRole': 1, 'NowHP': hp}


def update(wave, hp, *, step='RoleAction', **extra):
    return {'Step': step, 'RoundResult': {'NowWaveIndex': wave, 'NowRoundRoleID': f'1-{wave}-0'},
            'ActionResult': {'SkillEventList': [{'RoleEventList': [role_event(f'2-{wave}-0', hp)]}]}, **extra}


def reward(scene='S', stars=4):
    return {'SceneData': {'StaticID': scene, 'Stars': [1]*stars+[0]*(4-stars), 'PassCount': 1}}


class BattleSocket:
    def __init__(self, frames, tail=None):
        self.frames=deepcopy(frames);self.tail=tail;self.sent=[];self.received=0;self.now=0;self.closed=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True
    async def send(self,raw):self.sent.append(json.loads(raw))
    async def recv(self):
        self.received+=1;self.now+=1
        if self.received>100:return None  # Finite even for the broken implementation.
        value=self.frames.pop(0) if self.frames else self.tail(self) if self.tail else None
        if isinstance(value,BaseException):
            if isinstance(value,TimeoutError):self.now+=4
            raise value
        return json.dumps(value) if value is not None else None


def units(actor_hp=100):
    return {uid:{'id':uid,'StaticID':'H001','Position':0,'NowHP':actor_hp if uid=='1-1-0' else 100,
                 'MaxHP':100,'NowStatusList':[]} for uid in ('1-0-0','1-1-0','2-0-0','2-1-0')}


def patches(client,socket,*,actor_hp=100):
    return [patch('websockets.legacy.client.connect',return_value=socket),
            patch.object(battle,'_start_info',return_value={}),
            patch.object(battle,'fantasy_config',return_value={'Waves':[{},{}]}),
            patch.object(battle,'initialize_units',return_value=units(actor_hp)),
            patch.object(battle,'monotonic',side_effect=lambda:socket.now),
            patch.object(battle,'refresh_abilities'),
            patch.object(battle,'_choose_action',return_value=None),
            patch.object(battle,'_fallback_action',return_value=None)]


def client():
    return SimpleNamespace(account_state={'RoleDataContainer':{'Roles':[{'_id':'lower','StaticID':'H001'}]}},
        aid='offline',_auth_data=lambda data:data,
        call=lambda *args:{'_id':'room','ServerID':'offline','Domain':'ws://offline.invalid/'},
        apply_scene_reward=lambda body:None)


HANDSHAKE=[{'NetID':1},{'CType':0,'Index':0,'OID':'offline'}]


class LowerWaveSettlement(unittest.IsolatedAsyncioTestCase):
    async def run_socket(self,frames,tail=None,*,actor_hp=100):
        socket=BattleSocket(frames,tail);c=client();events=[];applied=[];c.apply_scene_reward=applied.append
        for context in patches(c,socket,actor_hp=actor_hp):self.enterContext(context)
        try:
            result=await battle._run(c,'S',{'upper':[],'lower':['lower']},on_progress=events.append)
        except GameError as error:result=error
        self.assertTrue(socket.closed)
        return result,socket,events,applied

    async def test_end_ack_precedes_dead_actor_or_empty_target_branches(self):
        for hp in (0,100):
            with self.subTest(actor_hp=hp):
                end=update(1,0,step='NowRoleBeforeRound',NetBattleGameOverCmd={})
                def after_ack(socket):
                    if end in socket.sent:return {'BattleEndData':{'Result':'Win'},'SceneRewardData':reward()}
                    return None
                result,socket,_,applied=await self.run_socket([*HANDSHAKE,end],after_ack,actor_hp=hp)
                self.assertIsInstance(result,dict)
                self.assertTrue(result['ok']);self.assertTrue(result['settlementConfirmed'])
                self.assertEqual(sum(value==end for value in socket.sent),1)
                self.assertFalse(any('SourceID' in value for value in socket.sent))
                self.assertEqual(applied,[reward()])

    async def test_lower_defeat_has_deadline_despite_alternating_battle_frames(self):
        def repeated(socket):return {'Step':'RoleAction' if socket.received%2 else 'NowRoleAfterRound'}
        result,socket,events,_=await self.run_socket([*HANDSHAKE,update(1,0)],repeated)
        self.assertIsInstance(result,battle.BattleUnconfirmed)
        self.assertIn('结算',str(result))
        self.assertLessEqual(socket.received,20)
        self.assertTrue(any(value.get('phase')=='settling' for value in events))

    async def test_game_over_without_result_also_has_independent_deadline(self):
        result,socket,_,_=await self.run_socket([*HANDSHAKE,{'NetBattleGameOverCmd':{}}],
            lambda socket:{'Step':'RoleAction' if socket.received%2 else 'NextRound'})
        self.assertIsInstance(result,battle.BattleUnconfirmed);self.assertLessEqual(socket.received,20)

    async def test_split_result_and_reward_survive_heartbeat_gap_longer_than_five_seconds(self):
        for first,last in (({'BattleEndData':{'Result':'Win'}},{'SceneRewardData':reward()}),
                           ({'SceneRewardData':reward()},{'BattleEndData':{'Result':'Win'}})):
            with self.subTest(first=first):
                result,socket,_,applied=await self.run_socket([*HANDSHAKE,first,*[{'HB':0}]*8,last])
                self.assertIsInstance(result,dict)
                self.assertTrue(result['ok']);self.assertTrue(result['settlementConfirmed'])
                self.assertEqual(applied,[reward()])
                self.assertEqual(socket.sent.count({'HB':1}),8)

    async def test_receive_timeout_keeps_waiting_for_the_other_settlement_frame(self):
        for first,last in (({'BattleEndData':{'Result':'Win'}},{'SceneRewardData':reward()}),
                           ({'SceneRewardData':reward()},{'BattleEndData':{'Result':'Win'}})):
            with self.subTest(first=first):
                result,socket,_,applied=await self.run_socket([*HANDSHAKE,first,TimeoutError(),last])
                self.assertTrue(result['ok']);self.assertTrue(result['settlementConfirmed'])
                self.assertIn({'HB':1},socket.sent);self.assertEqual(applied,[reward()])

    async def test_duplicate_partial_settlement_cannot_renew_its_deadline(self):
        for frame in ({'BattleEndData':{'Result':'Win'}},{'SceneRewardData':reward()}):
            with self.subTest(frame=frame):
                result,socket,_,_=await self.run_socket([*HANDSHAKE,frame],lambda socket:deepcopy(frame))
                self.assertLessEqual(socket.received,20)
                if 'BattleEndData' in frame:
                    self.assertTrue(result['ok']);self.assertFalse(result['settlementConfirmed'])
                else:self.assertIsInstance(result,battle.BattleUnconfirmed)

    async def test_upper_wave_defeat_still_waits_for_lower_wave(self):
        result,_,events,_=await self.run_socket([*HANDSHAKE,update(0,0),*[{'HB':0}]*20,
            update(1,100),{'BattleEndData':{'Result':'Win'},'SceneRewardData':reward()}])
        self.assertTrue(result['ok'])
        self.assertFalse(any(value.get('phase')=='settling' and value.get('wave')==0 for value in events))

    async def test_defeat_identifies_only_the_failed_half_including_final_frame_index(self):
        for wave, expected in ((0, {'upper': False, 'lower': None}), (1, {'upper': True, 'lower': False})):
            with self.subTest(wave=wave):
                result, _, _, _ = await self.run_socket([*HANDSHAKE,
                    update(wave, 50, BattleEndData={'Result': 'Lose'})])
                self.assertFalse(result['ok'])
                self.assertEqual(result['halfResults'], expected)

    async def test_confirmed_win_identifies_both_halves_as_passed(self):
        result, _, _, _ = await self.run_socket([*HANDSHAKE,
            {'BattleEndData': {'Result': 'Win'}, 'SceneRewardData': reward()}])
        self.assertEqual(result['halfResults'], {'upper': True, 'lower': True})

    async def test_revive_clears_only_the_hp_based_settlement_deadline(self):
        result,_,events,_=await self.run_socket([*HANDSHAKE,update(1,0),*[{'HB':0}]*4,
            update(1,50),*[{'HB':0}]*20,{'BattleEndData':{'Result':'Win'},'SceneRewardData':reward()}])
        self.assertTrue(result['ok'])
        self.assertTrue(any(value.get('phase')=='settling' for value in events))
        self.assertTrue(any(value.get('phase')=='battle' and value.get('enemies',[{}])[0].get('hpPercent')==50 for value in events))


class EmptyTeamsThroughActualSocket(unittest.TestCase):
    """Run the real progression and real battle runner, not a mocked outcome."""
    def execute(self, *, first_stars=4, missing_settlement=False):
        from tools.test_abyss_auto import roster
        state=roster();c=client();c.account_state=state;c.login_id='offline';c.bootstrap=Mock()
        events=[];sockets=[];room_scenes=[];order=[];pending=[False]
        waves={'upper':['r0','r1','r2','r3'],'lower':['r4','r5','r6','r7']}
        current=[None]
        def make_room(route,body):
            self.assertEqual(route,'RoomHandler.CreateRoomByScene')
            sid=body['SceneID'];room_scenes.append(sid)
            end=update(1,0,step='NowRoleBeforeRound',NetBattleGameOverCmd={})
            def tail(socket):
                if missing_settlement:
                    return {'Step':'RoleAction' if socket.received%2 else 'NowRoleAfterRound'}
                self.assertIn(end,socket.sent,'Server settlement is withheld until the end acknowledgement arrives')
                return {'BattleEndData':{'Result':'Win'},'SceneRewardData':reward(sid,first_stars if sid.endswith('_1') else 4)}
            socket=BattleSocket([*HANDSHAKE,update(1,0) if missing_settlement else end],tail)
            current[0]=socket;sockets.append(socket);order.append('battle')
            return {'_id':'room','ServerID':'offline','Domain':'ws://offline.invalid/'}
        def apply_reward(body):
            row=body['SceneData'];rows=state['SceneDataContainer']['Scenes']
            old=next((r for r in rows if r['StaticID']==row['StaticID']),None)
            if old is None:rows.append(deepcopy(row))
            else:old.update(deepcopy(row))
        def equip(*a,**kw):pending[0]=True;order.append('equip')
        def restore(*a,**kw):pending[0]=False;order.append('restore');return {'restored':True}
        c.call=make_room;c.apply_scene_reward=apply_reward
        meta={'tables':{'Scene':{f'MysteriousRealm_{i}':{} for i in (1,2)},
                        'Fantasy':{f'MysteriousRealm_{i}':[1] for i in (1,2)}}}
        temporary=self.enterContext(tempfile.TemporaryDirectory())
        for context in [patch.object(config,'data_dir',return_value=Path(temporary)),
                        patch.object(config,'get_request_delay_config',return_value={'enabled':False}),
                        patch.object(progression,'metadata',return_value=meta),
                        patch.object(progression.time,'sleep'),
                        patch.object(abyss_team,'choose_teams',return_value={'waves':waves,'reasons':{},'warnings':[]}),
                        patch.object(abyss_loadout,'plan',return_value={}),
                        patch.object(abyss_loadout,'apply',side_effect=equip),
                        patch.object(abyss_loadout,'pending',side_effect=lambda *a,**kw:pending[0]),
                        patch.object(abyss_loadout,'restore',side_effect=restore),
                        patch('websockets.legacy.client.connect',side_effect=lambda *a,**kw:current[0]),
                        patch.object(battle,'_start_info',return_value={}),
                        patch.object(battle,'fantasy_config',return_value={'Waves':[{},{}]}),
                        patch.object(battle,'initialize_units',side_effect=lambda *a,**kw:units()),
                        patch.object(battle,'monotonic',side_effect=lambda:current[0].now if current[0] else 0),
                        patch.object(battle,'refresh_abilities'),
                        patch.object(battle,'_choose_action',return_value=None),
                        patch.object(battle,'_fallback_action',return_value=None)]:self.enterContext(context)
        params={'sceneId':'MysteriousRealm_1','autoAdvance':True,'waves':{'upper':[],'lower':[]},'_on_progress':events.append}
        result=progression.run(c,params)
        self.assertEqual(params['waves'],{'upper':[],'lower':[]})
        self.assertTrue(all(socket.closed for socket in sockets));self.assertFalse(pending[0])
        return result,room_scenes,order,events,c

    def test_four_stars_received_after_end_ack_restores_assets_and_enters_next_stage(self):
        result,scenes,order,events,c=self.execute()
        self.assertTrue(result['ok']);self.assertTrue(result['autoConfigured'])
        self.assertEqual(scenes,['MysteriousRealm_1','MysteriousRealm_2'])
        self.assertEqual(order,['equip','battle','restore']*2)
        self.assertEqual([row['stars'] for row in result['attempts']],[4,4])
        self.assertTrue(any(e.get('phase')=='settling' for e in events))
        c.bootstrap.assert_not_called()

    def test_three_stars_retries_five_times_before_next_stage(self):
        result,scenes,order,_,_=self.execute(first_stars=3)
        self.assertTrue(result['ok']);self.assertEqual(scenes,['MysteriousRealm_1']*5+['MysteriousRealm_2'])
        self.assertEqual(order,['equip','battle','restore']*6)
        self.assertEqual([row['attempt'] for row in result['attempts']],list(range(1,6))+[1])

    def test_hp_zero_without_authoritative_clear_never_unlocks_or_replays(self):
        result,scenes,order,events,c=self.execute(missing_settlement=True)
        self.assertFalse(result['ok']);self.assertEqual(result['completedScenes'],[])
        self.assertEqual(scenes,['MysteriousRealm_1']);self.assertEqual(order,['equip','battle','restore'])
        self.assertTrue(any(e.get('phase')=='settling' for e in events))
        self.assertTrue(any(e.get('phase')=='checking' for e in events));self.assertEqual(c.bootstrap.call_count,3)


if __name__=='__main__':unittest.main()
