"""Offline set alternatives, verified locks, weekly crafting and event-gated VIP."""
from copy import deepcopy
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
import itertools
import sys
import threading
import unittest

from backend import automation_supplies as rules,chip_crafting as chips,vip_supplies as vip,config,tasks,token_vault,equipment_sale,updater
from backend.game_client import GameClient,GameError
from equipment import auto,plans,stats
from equipment.tests.test_auto import fixture
from tools.test_abyss_auto import AssetServer

NOW=1793923200000  # 2026-11-06 08:00 in +08.


def rec(identifier,store,bought=0):
    return {'_id':{'$oid':'record-'+identifier},'StaticID':identifier,'Store':store,'BuyCount':bought,'LastResetTime':{'$date':NOW}}


class SetAlternatives(unittest.TestCase):
    def test_four_piece_alternatives_form_only_four_plus_two(self):
        groups=auto.selected_set_groups(['Attack','Destruction','Immunity','Critical'])
        self.assertEqual(groups,[{'Attack':4,'Immunity':2},{'Attack':4,'Critical':2},
                                 {'Destruction':4,'Immunity':2},{'Destruction':4,'Critical':2}])

    def test_immunity_is_fixed_and_other_twos_repeat(self):
        self.assertEqual(auto.selected_set_groups(['Immunity','Health','Defense']),[
            {'Immunity':2,'Health':4},{'Immunity':2,'Health':2,'Defense':2},{'Immunity':2,'Defense':4}])

    def test_three_two_piece_sets_cover_all_repeated_combinations(self):
        self.assertEqual(auto.selected_set_groups(['Health','Defense']),[
            {'Health':6},{'Health':4,'Defense':2},{'Health':2,'Defense':4},{'Defense':6}])

    def test_global_max_matches_independent_exhaustive_search_across_alternatives(self):
        state,roles=fixture(count=3,roles_count=1)
        equips=state['EquipmentContainer']['Datas']
        for index,item in enumerate(equips):
            item['Set']=('Attack','Destruction','Critical')[index%3];item['EquipRole']=''
        groups=auto.selected_set_groups(['Attack','Destruction','Critical'])
        for objective in ('damage','survival'):
            result=auto.search(state,roles['r0'],set_options=['Attack','Destruction','Critical'],critical_target=0,
                               objective=objective,include_worn=True,complete=True)
            wanted=[]
            for combo in itertools.product(*[[e for e in equips if plans.equip_slot(e)==slot] for slot in plans.SLOTS]):
                counts=Counter(e['Set'] for e in combo)
                if not any(all(counts[name]>=count for name,count in group.items()) for group in groups):continue
                panel=stats.role_panel(state,roles['r0'],list(combo),include_artifact_effects=True)['panel']
                wanted.append(auto.metrics(panel,0)[objective])
            self.assertEqual(max(row['metrics'][objective] for row in result['results']),max(wanted))
            for row in result['results']:
                selected={tasks._oid(e['_id']):e for e in equips};counts=Counter(selected[eid]['Set'] for eid in row['equipIds'].values())
                self.assertTrue(any(all(counts[name]>=count for name,count in group.items()) for group in groups))


class LockClient(AssetServer):
    def __init__(self,state):super().__init__(state);self.lose_reply=False
    def call(self,route,body):
        if route!='EquipmentHandler.SwitchLock':return super().call(route,body)
        self.calls.append((route,deepcopy(body)));selected=[]
        for row in self.server['EquipmentContainer']['Datas']:
            if tasks._oid(row['_id']) in body['EquipIDs']:
                row['IsLock']=not row['IsLock'];selected.append(deepcopy(row))
        if self.lose_reply:raise GameError('reply lost after commit')
        return {'Equips':selected}


class EquipmentLocks(unittest.TestCase):
    def setUp(self):
        state,_=fixture(count=1,roles_count=1)
        for row in state['EquipmentContainer']['Datas']:row.update(IsLock=False)
        self.client=LockClient(state);self.eid=tasks._oid(state['EquipmentContainer']['Datas'][0]['_id'])

    def test_repeated_desired_state_does_not_toggle_back(self):
        self.assertTrue(plans.set_locked(self.client,self.eid,True)['verified'])
        self.assertTrue(plans.set_locked(self.client,self.eid,True)['verified'])
        self.assertEqual(len(self.client.calls),1)
        self.assertTrue(plans.set_locked(self.client,self.eid,False)['verified'])
        self.assertFalse(self.client.server['EquipmentContainer']['Datas'][0]['IsLock'])

    def test_lost_reply_is_verified_without_replaying_toggle(self):
        self.client.lose_reply=True
        result=plans.set_locked(self.client,self.eid,True)
        self.assertTrue(result['verified']);self.assertEqual(len(self.client.calls),1)

    def test_bulk_only_toggles_unlocked_plus_fifteen_after_refresh(self):
        rows=self.client.server['EquipmentContainer']['Datas'];rows[0]['IsLock']=True;rows[1]['LV']=14
        result=plans.set_locked(self.client,all_max=True)
        self.assertEqual(result['count'],4)
        sent=self.client.calls[0][1]['EquipIDs']
        self.assertNotIn(tasks._oid(rows[0]['_id']),sent);self.assertNotIn(tasks._oid(rows[1]['_id']),sent)


class SupplyClient(AssetServer):
    _apply_commodity_record=GameClient._apply_commodity_record
    _apply_now_items=GameClient._apply_now_items
    _ensure_inventory_state=GameClient._ensure_inventory_state
    _reset_inventory_locked=GameClient._reset_inventory_locked
    _inventory_item=staticmethod(GameClient._inventory_item)
    def __init__(self,count=40):
        state={'Utc':str(NOW//1000),'Info':{},'ItemContainer':{'Items':[{'StaticID':'1','Count':10_000_000}]},
            'ArkScienceParkData':{'IngredientStorageBuildingLV':3},
            'StoreRecordContainer':{'Records':[rec('RandomEquipment','CraftCharm'),rec('RandomAccessory','CraftCharm'),rec('VIPGIFT_VIP0','VIPGift')]},
            'EquipmentContainer':{'Datas':[{'_id':{'$oid':f'mat-{i}'},'StaticID':'E0091','LV':0,'ClassLV':3,
                                         'IsLock':False,'EquipRole':''} for i in range(count)]},
            'SceneDataContainer':{'Scenes':[{'StaticID':'1_3_6','PassCount':1,'Stars':[True]}]},
            'StatusContainer':{'List':[]},'ActivityDBSettingContainer':{'SettingList':[]}}
        super().__init__(state);self.fail_reply=False
    def call(self,route,body):
        self.calls.append((route,deepcopy(body)))
        if route=='EquipmentHandler.SellEquips':
            self.server['EquipmentContainer']['Datas']=[row for row in self.server['EquipmentContainer']['Datas'] if tasks._oid(row['_id']) not in body['IDs']]
            return {'Drop':{'Items':[{'Item':{'StaticID':'1','Count':100}}]}}
        if route=='StoreHandler.Craft':
            sid=body['CommodityID'];ids={row['Equipment']['_id'] for row in body['CostMaterials']}
            selected=[row for row in self.server['EquipmentContainer']['Datas'] if tasks._oid(row['_id']) in ids]
            assert len(selected)==len(ids) and all(chips.material(row,tasks._equip_ref()) for row in selected)
            self.server['EquipmentContainer']['Datas']=[row for row in self.server['EquipmentContainer']['Datas'] if tasks._oid(row['_id']) not in ids]
            store='CraftCharm';price=50000 if sid=='RandomEquipment' else 80000
        elif route=='StoreHandler.BuyCommodity':sid=body['Record']['StaticID'];store='VIPGift';price=10000
        else:raise AssertionError(route)
        current=next(row for row in self.server['StoreRecordContainer']['Records'] if row['StaticID']==sid)
        current['BuyCount']+=1
        wallet=self.server['ItemContainer']['Items'][0];wallet['Count']-=price
        if self.fail_reply:raise GameError('reply lost')
        return {'CommodityRecord':deepcopy(current),'CostItems':[{'NowItem':deepcopy(wallet)}],
                'Drop':{'Items':[{'Item':{'StaticID':'EC14' if store=='CraftCharm' else '3','Count':1 if store=='CraftCharm' else 100}}]}}


class AutomaticSupplies(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.enterContext(patch.object(rules,'now',side_effect=lambda:self.now))
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(config,'data_dir',return_value=Path(directory.name)))
        self.client=SupplyClient();self.runtime={}

    def test_crafts_two_each_and_releases_sale_reservation_only_when_all_done(self):
        self.client._auto_chip_crafting_enabled=True
        self.assertTrue(chips.protected_ids(self.client))
        result=chips.run(self.client,runtime=self.runtime)
        self.assertTrue(result['ok'],result)
        self.assertEqual([body['CommodityID'] for route,body in self.client.calls],['RandomEquipment']*2+['RandomAccessory']*2)
        self.assertEqual([len(body['CostMaterials']) for _,body in self.client.calls],[8,8,9,9])
        self.assertFalse(chips.protected_ids(self.client));self.assertEqual(len(result['lines']),4)
        self.assertTrue(chips.run(self.client,runtime=self.runtime)['skipped']);self.assertEqual(len(self.client.calls),4)

    def test_incomplete_materials_are_reserved_and_locked_worn_upgraded_are_excluded(self):
        client=SupplyClient(5);client._auto_chip_crafting_enabled=True
        self.assertTrue(chips.run(client,runtime={})['skipped']);self.assertTrue(chips.protected_ids(client))
        recipe=rules.catalog()['recipes'][0]
        for field,value in [('IsLock',True),('EquipRole','hero'),('LV',1)]:
            changed=deepcopy(client.account_state);changed['EquipmentContainer']['Datas'][0][field]=value
            self.assertFalse(chips.material(changed['EquipmentContainer']['Datas'][0],tasks._equip_ref()))

    def test_lost_craft_reply_waits_then_refreshes_counters_without_old_material_replay(self):
        self.client.fail_reply=True
        self.assertFalse(chips.run(self.client,runtime=self.runtime)['ok'])
        first={row['Equipment']['_id'] for row in self.client.calls[0][1]['CostMaterials']}
        self.assertTrue(chips.run(self.client,runtime=self.runtime)['skipped']);self.assertEqual(len(self.client.calls),1)
        self.now+=300001;self.client.fail_reply=False
        result=chips.run(self.client,runtime=self.runtime);self.assertTrue(result['ok'],result)
        for _,body in self.client.calls[1:]:self.assertFalse(first&{row['Equipment']['_id'] for row in body['CostMaterials']})

    def test_weekly_reset_makes_recipe_actionable_again(self):
        chips.run(self.client,runtime=self.runtime)
        self.now+=7*86_400_000
        self.assertTrue(any(left for _,left in chips.remaining(self.client.account_state,self.now)))

    def test_live_borrower_defers_refreshing_mutations_without_blocking_other_work(self):
        self.client._intimacy_asset_owner=threading.get_ident()+1
        with patch.object(self.client,'bootstrap') as login:
            self.assertTrue(chips.run(self.client,runtime=self.runtime)['skipped'])
            self.assertTrue(vip.run(self.client,runtime=self.runtime)['skipped'])
            login.assert_not_called()

    def test_sweep_sales_craft_first_then_sell_remaining_seventy_level_equipment(self):
        self.client._auto_chip_crafting_enabled=True
        self.client._chip_crafting_context=lambda:self.runtime
        self.client._chip_crafting_on_result=Mock()
        result=equipment_sale.sell(self.client)
        self.assertTrue(result['ok'],result)
        self.assertEqual([route for route,_ in self.client.calls],['StoreHandler.Craft']*4+['EquipmentHandler.SellEquips'])
        self.assertEqual(result['sold'],6)
        self.client._chip_crafting_on_result.assert_called_once()

    def test_sweep_sale_retains_materials_until_both_weekly_recipes_are_done(self):
        self.client=SupplyClient(5);self.client._auto_chip_crafting_enabled=True
        self.client._chip_crafting_context=lambda:self.runtime
        result=equipment_sale.sell(self.client)
        self.assertEqual(result['sold'],0);self.assertEqual(self.client.calls,[])

    def test_no_event_future_event_and_innate_vip_do_not_buy(self):
        self.client.account_state['StatusContainer']['List']=[{'StaticID':'VIPHuntExtraDrop','EndTime':{'$date':NOW+3600000}}]
        self.assertFalse(vip.status(self.client.account_state,now_ms=NOW)['ready'])
        self.client.account_state['ActivityDBSettingContainer']['SettingList']=[{'ActivityID':'Elf','StartTime':{'$date':NOW+3600000},'EndTime':{'$date':NOW+7200000}}]
        status=vip.status(self.client.account_state,now_ms=NOW)
        self.assertFalse(status['ready']);self.assertEqual(status['nextMs'],NOW+3600000)

    def test_activity_schedule_triggers_only_one_monthly_purchase(self):
        self.client.server['ActivityDBSettingContainer']['SettingList']=[{'ActivityID':'Hunt','StartTime':{'$date':NOW-1000},'EndTime':{'$date':NOW+3600000}}]
        self.client.bootstrap()
        result=vip.run(self.client,runtime=self.runtime);self.assertTrue(result['ok'],result)
        self.assertEqual(len(self.client.calls),1)
        self.assertTrue(vip.run(self.client,runtime=self.runtime)['skipped']);self.assertEqual(len(self.client.calls),1)

    def test_expired_status_does_not_trigger_but_current_event_buff_does(self):
        state=self.client.account_state
        state['StatusContainer']['List']=[{'StaticID':'GoldActivity100','EndTime':{'$date':NOW-1}}]
        self.assertFalse(vip.status(state,now_ms=NOW)['ready'])
        state['StatusContainer']['List'][0]['EndTime']={'$date':NOW+1000}
        self.assertTrue(vip.status(state,now_ms=NOW)['ready'])


class MacKeychain(unittest.TestCase):
    def test_frozen_mac_app_never_offers_or_installs_windows_update(self):
        service=updater.UpdateService()
        service._payload={'latestVersion':'99.0.0','minSupportedVersion':'99.0.0','platforms':{'windows':{}}}
        with patch.object(config,'PLATFORM','macos'),patch.object(sys,'frozen',True,create=True):
            state=service.status()
            self.assertEqual(state['platform'],'macos')
            self.assertFalse(state['installSupported']);self.assertFalse(state['available']);self.assertFalse(state['forced'])
            with self.assertRaises(updater.UpdateError):service.install()

    def test_secret_is_kept_in_native_keychain_and_settings_only_hold_account_reference(self):
        security=SimpleNamespace(**{name:name for name in ['kSecClass','kSecClassGenericPassword','kSecAttrService','kSecAttrAccount',
            'kSecReturnData','kSecMatchLimit','kSecMatchLimitOne','kSecValueData']},errSecDuplicateItem=-25299,
            SecItemAdd=Mock(return_value=(0,None)),SecItemUpdate=Mock(return_value=0),SecItemCopyMatching=Mock(return_value=(0,b'offline-secret')))
        with patch.dict(sys.modules,{'Security':security}):
            reference=token_vault.protect('macos','offline-secret','account')
            self.assertNotIn('offline-secret',reference)
            self.assertEqual(token_vault.unprotect('macos',reference,'account'),'offline-secret')
            with self.assertRaises(token_vault.TokenVaultError):token_vault.unprotect('macos',reference,'other-account')


if __name__=='__main__':unittest.main()
