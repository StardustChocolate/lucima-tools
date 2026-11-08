"""Offline shop exclusions, native catalog additions, and cooperative farming."""
from copy import deepcopy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from backend import tasks,appcore,intimacy,abyss_battle,config
from backend.game_client import GameClient,GameError
from tools import test_intimacy as fixtures


def record(sid,index):
    return {'_id':f'shelf-{index}','Store':tasks.SECRET_STORE_ID,'BuyCount':0,
            'DropResult':{'Items':[{'Item':{'StaticID':sid,'Count':1}}]}}


class FreeShop(unittest.TestCase):
    def execute(self,params):
        purchases=[];shelf=[record(sid,i) for i,sid in enumerate(tasks.DEFAULT_SHOP_WANTED)]
        def call(route,body):
            if route=='StoreHandler.ResetRandomStore':return {'Records':deepcopy(shelf)}
            if route=='StoreHandler.BuyCommodity':
                sid=tasks._record_goods_sid(body['Record']);purchases.append(sid)
                return {'Drop':{'Items':[{'Item':{'StaticID':sid,'Count':1}}]}}
            raise AssertionError(route)
        client=SimpleNamespace(account_state={'StoreRecordContainer':{'Records':deepcopy(shelf)}},
                               _auth_data=lambda value:value,call=call)
        result=tasks.task_shop_free(client,params)
        self.assertTrue(result['ok'],result)
        return purchases

    def test_exclusions_apply_to_both_old_and_refreshed_shelves(self):
        actual=self.execute({'wanted':tasks.DEFAULT_SHOP_WANTED,'excludedChipIds':['EC11','EC51','5','invalid']})
        self.assertEqual(actual,['5','6','EC21','EC31','EC41','EC61']*2)

    def test_empty_selection_never_falls_back_to_buying_everything(self):
        self.assertEqual(self.execute({'wanted':[]}),[])
        self.assertEqual(self.execute({'wanted':tasks.BUY_TARGETS[2]['ids'],
                                      'excludedChipIds':tasks.BUY_TARGETS[2]['ids']}),[])

    def test_legacy_default_and_friendship_new_goods_have_exact_prices_and_limits(self):
        self.assertEqual(self.execute({}),tasks.DEFAULT_SHOP_WANTED*2)
        for sid,cost,gain in [('FriendShip12',1000,('77',1)),('FriendShip6',100,('72',20)),('FriendShip7',100,('73',20))]:
            self.assertIn(sid,tasks.STORE_GOODS['FriendShip'])
            self.assertEqual(tasks.COMMODITY_PRICE[sid]['cost'],('24',cost))
            self.assertEqual(tasks.COMMODITY_PRICE[sid]['gain'],[gain])
            self.assertEqual(tasks._commodity_catalog()[sid]['limit'],1)
            self.assertEqual(tasks._commodity_catalog()[sid]['resetType'],'Day')


class CooperativeFarm(unittest.TestCase):
    setUp=fixtures.RunTests.setUp
    owners=fixtures.RunTests.owners

    def test_other_shop_task_runs_while_farming_websocket_is_waiting_then_assets_restore(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        baseline=self.owners();lock=threading.Lock()
        acc=SimpleNamespace(email='offline',lock=lock,client=self.client,note_inventory_changed=Mock(),push_log=Mock())
        original=self.client.call
        def call(route,body):
            if route=='StoreHandler.ResetRandomStore':return {'Records':[]}
            return original(route,body)
        self.client.call=call
        def battle(client,params):
            entered.set()
            if not release.wait(4):raise AssertionError('test shop was blocked by the farming job')
            for row in client.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(appcore,'_require',return_value=acc), \
             patch.object(intimacy,'choose_carriers',return_value={'upper':'r0','lower':'r1'}), \
             patch.object(abyss_battle,'run_abyss_battle',side_effect=battle), \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}):
            appcore.intimacy_start('offline')
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(lock.acquire(timeout=.5),'shop must acquire lock during the websocket battle')
                try:self.assertTrue(tasks.task_shop_free(self.client,{'wanted':[]})['ok'])
                finally:lock.release()
                self.assertTrue(self.client._intimacy_reserved_equipment)
            finally:release.set()
            for _ in range(150):
                if not appcore.intimacy_snapshot('offline')['running']:break
                finished.wait(.02)
            self.assertFalse(appcore.intimacy_snapshot('offline')['running'])
        self.assertEqual(self.owners(),baseline)
        self.assertEqual(self.client._intimacy_reserved_equipment,set())

    def test_concurrent_gear_changes_are_rejected_while_unrelated_http_calls_are_allowed(self):
        client=GameClient.__new__(GameClient)
        client._intimacy_asset_owner=threading.get_ident()+1
        client._intimacy_reserved_equipment={'loan'}
        with patch.object(client,'_call_serial',return_value={'ok':True}):
            self.assertTrue(client.call('StoreHandler.ResetRandomStore',{})['ok'])
            for route in ('EquipmentHandler.EquipsToRole','ArtifactHandler.Equip'):
                with self.assertRaises(GameError):client.call(route,{})
            with self.assertRaises(GameError):client.call('EquipmentHandler.SellEquips',{'IDs':['loan']})
            self.assertTrue(client.call('EquipmentHandler.SellEquips',{'IDs':['other']})['ok'])


if __name__=='__main__':unittest.main()
