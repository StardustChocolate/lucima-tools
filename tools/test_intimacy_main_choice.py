"""Chosen mains, Lv60-only fitting and preservation of unlevelled heroes."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import intimacy,abyss_loadout,abyss_battle,config,appcore
from backend.game_client import GameError
from backend.tasks import _oid
from tools.test_abyss_auto import AssetServer
from tools.test_intimacy import farm_state


def worn(state,owners):
    return {key:{_oid(row['_id']):_oid(row.get('EquipRole')) for row in rows if _oid(row.get('EquipRole')) in owners}
            for key,rows in [('equipment',state['EquipmentContainer']['Datas']),('artifacts',state['Artifacts']['Datas'])]}


class MainChoice(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.object(config,'data_dir',return_value=Path(tmp.name));p.start();self.addCleanup(p.stop)
        self.state=farm_state();self.cancel=threading.Event()

    def run_farm(self,client,battle,**params):
        with patch.object(abyss_battle,'run_abyss_battle',side_effect=battle), \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}), \
             patch.object(intimacy,'RETRY_BASE_SECONDS',0):
            return intimacy.run(client,{'_account_key':'main-choice','_cancel_event':self.cancel,**params})

    def test_partial_choice_fills_only_the_other_half_and_both_choices_can_be_below_60(self):
        chosen=intimacy.choose_carriers(self.state,selected={'upper':'r2'})
        self.assertEqual(chosen['upper'],'r2');self.assertNotEqual(chosen['lower'],'r2')
        for role in self.state['RoleDataContainer']['Roles']:role['LV']=20
        self.assertEqual(intimacy.choose_carriers(self.state,selected={'upper':'r2','lower':'r3'}),{'upper':'r2','lower':'r3'})
        self.assertIsNone(intimacy.plan_loadout(self.state,{'upper':'r2','lower':'r3'}))

    def test_http_start_forwards_the_chosen_mains_and_mode(self):
        from backend.server import _route_api
        chosen={'upper':'r0','lower':'r1'}
        with patch.object(appcore,'intimacy_start',return_value={'ok':True}) as start:
            _route_api('POST','/api/intimacy/start',{'account':'test','mains':chosen,'autoConfigure':False},{})
        start.assert_called_once_with('test',chosen,False)

    def test_main_options_use_real_instance_ids_without_bootstrapping_or_mutations(self):
        from backend.server import _route_api
        client=AssetServer(self.state);before=deepcopy(client.server)
        with patch.object(appcore,'_require',return_value=SimpleNamespace(client=client)), \
             patch.object(client,'bootstrap',side_effect=AssertionError('options read cached state')):
            result=_route_api('GET','/api/intimacy/options',{}, {'account':'test'})
        self.assertEqual({row['roleId']for row in result['roles']},{_oid(row['_id'])for row in self.state['RoleDataContainer']['Roles']})
        self.assertTrue(all(row.get('avatar')for row in result['roles']))
        self.assertEqual(client.server,before);self.assertEqual(client.calls,[])

    def test_invalid_foreign_duplicate_and_incomplete_choices_never_borrow(self):
        client=AssetServer(self.state)
        for mains,auto in [({'upper':'foreign'},True),({'upper':'r0','lower':'r0'},True),({'upper':'r0'},False),({'upper':['r0']},True)]:
            with self.subTest(mains=mains):
                result=self.run_farm(client,lambda *args:self.fail('must not battle'),mains=mains,autoConfigure=auto)
                self.assertFalse(result['ok'],result);self.assertEqual(client.calls,[])
                self.assertFalse(intimacy.recovery_pending(client,'main-choice'))

    def test_only_60_level_targets_and_low_level_gear_is_not_borrowed(self):
        low={'r0','r3','r4','r5','r6','r7','r8','r9'}
        for role in self.state['RoleDataContainer']['Roles']:
            if _oid(role['_id']) in low:role['LV']=59
        # A low-level future passenger has a tempting item; it must stay put.
        protected_eq,protected_art=intimacy.protected_assets(self.state)
        for item in self.state['EquipmentContainer']['Datas']:
            if _oid(item.get('EquipRole'))=='r8':item['MainProp']['Value']=1e12
        client=AssetServer(self.state);before=worn(client.server,low)
        carriers={'upper':'r0','lower':'r1'}
        main=intimacy.plan_loadout(client.account_state,carriers)
        self.assertEqual(set(main['targets']),{'r1'})
        selected={eid for ids in main['targets'].values()for eid in ids}
        self.assertFalse(selected & protected_eq)
        self.assertFalse(set(main['artifactTargets'].values()) & protected_art)
        abyss_loadout.apply(client,main,account_key='main-choice')
        waves=intimacy.make_waves(client.account_state,carriers)
        passenger=intimacy.plan_passengers(client.account_state,carriers,waves,'upper',{'carriers':main})
        self.assertEqual(set(passenger['targets']),{'r2'})
        abyss_loadout.apply(client,passenger,account_key=intimacy.passenger_key(client,'main-choice','upper'))
        self.assertIsNone(intimacy.plan_passengers(client.account_state,carriers,waves,'lower',{'carriers':main,'upper':passenger}))
        self.assertEqual(worn(client.server,low),before)
        self.assertTrue(intimacy.restore_all(client,account_key='main-choice')['restored'])
        self.assertEqual(worn(client.server,low),before)

    def test_chosen_mains_remain_after_defeats_and_low_heroes_never_change_during_farming(self):
        low={'r3','r4','r5','r6','r7','r8','r9'}
        for role in self.state['RoleDataContainer']['Roles']:
            if _oid(role['_id']) in low:role['LV']=1
        client=AssetServer(self.state);before=deepcopy(client.server);kept=worn(before,low);calls=[]
        client.after_call=lambda:self.assertEqual(worn(client.server,low),kept)
        def battle(c,params):
            calls.append(deepcopy(params['waves']))
            self.assertEqual(params['waves']['upper'][0],'r0');self.assertEqual(params['waves']['lower'][0],'r1')
            if len(calls)<3:return {'ok':False,'settlementConfirmed':True,'halfResults':{'upper':False,'lower':False}}
            for row in c.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(intimacy,'choose_half_carriers',side_effect=AssertionError('chosen mains must stay')):
            result=self.run_farm(client,battle,mains={'upper':'r0','lower':'r1'},autoConfigure=False)
        self.assertTrue(result['ok'],result);self.assertEqual(len(calls),3)
        for container in ('EquipmentContainer','Artifacts'):self.assertEqual(client.server[container],before[container])

    def test_newly_reached_60_main_and_passenger_are_fitted_on_next_round(self):
        for role in self.state['RoleDataContainer']['Roles']:
            if _oid(role['_id']) not in {'r1'}:role['LV']=59
        client=AssetServer(self.state);calls=[]
        def battle(c,params):
            calls.append(deepcopy(params['waves']))
            root=abyss_loadout._path(c,'main-choice')
            if len(calls)==1:
                self.assertEqual(set(__import__('json').loads(root.read_text())['targets']),{'r1'})
                for role in c.server['RoleDataContainer']['Roles']:
                    if _oid(role['_id']) in {'r0','r2'}:role['LV']=60
            else:
                self.assertEqual(set(__import__('json').loads(root.read_text())['targets']),{'r0','r1'})
                child=abyss_loadout._path(c,intimacy.passenger_key(c,'main-choice','upper'))
                self.assertEqual(set(__import__('json').loads(child.read_text())['targets']),{'r2'})
                for row in c.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        result=self.run_farm(client,battle,mains={'upper':'r0','lower':'r1'})
        self.assertTrue(result['ok'],result);self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
