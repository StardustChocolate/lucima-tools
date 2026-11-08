"""Return every possible loan and clear each completed attempt."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from backend import abyss_loadout, abyss_battle, config, intimacy, equipment_sale
from backend.game_client import GameMessage, GameError
from backend.tasks import _oid
from tools.test_abyss_auto import AssetServer, gear_fixture
from tools.test_intimacy import farm_state


class StartRecovery(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        override=patch.object(config,'data_dir',return_value=self.root)
        override.start();self.addCleanup(override.stop)

    def test_rejected_return_goes_to_inventory_and_preserves_originals_in_history(self):
        state,waves=gear_fixture();client=AssetServer(state)
        loadout=abyss_loadout.plan(client.account_state,waves)
        abyss_loadout.apply(client,loadout)
        call=client.call;rejected=[]
        def reject_return(route,body):
            if route=='EquipmentHandler.EquipsToRole':
                rejected.append(1);raise GameMessage('No Role')
            return call(route,body)
        with patch.object(client,'call',side_effect=reject_return):
            result=abyss_loadout.recover_before_challenge(client)
        self.assertTrue(rejected)
        self.assertTrue(result['restored'],result)
        self.assertFalse(abyss_loadout.pending(client))
        journal=json.loads(next((self.root/'abyss-recovery/history').glob('*.json')).read_text())
        self.assertEqual(journal['originalDestinations']['equipment'],loadout['equipment'])
        actual=abyss_loadout._owners(client.server['EquipmentContainer']['Datas'])
        self.assertTrue(all(actual[aid]!=loadout['equipment'][aid] for aid in result['fallback']['equipment']))
        self.assertTrue(abyss_loadout.recover_before_challenge(AssetServer(client.server))['restored'])

    def test_transient_return_failure_is_recovered_before_new_farming(self):
        client=AssetServer(farm_state());event=threading.Event()
        item=next(e for e in client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole')) not in ('',None,'r8'))
        aid=_oid(item['_id'])
        abyss_loadout._save(abyss_loadout._path(client,'offline'),{
            'schema':1,'roles':[_oid(item['EquipRole'])],'equipment':{aid:'r8'},'artifacts':{}})
        original=client.call;reject=[True];battles=[]
        def call(route,body):
            if reject[0] and route=='EquipmentHandler.EquipsToRole' and body['RoleID']=='r8':
                reject[0]=False;raise GameMessage('No Role')
            return original(route,body)
        def battle(c,params):
            battles.append(deepcopy(params['waves']))
            self.assertFalse(reject[0])
            # Only this run's new borrow may remain while its battle is active.
            self.assertTrue(abyss_loadout.pending(c,'offline'))
            for row in c.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(client,'call',side_effect=call), \
             patch.object(intimacy,'choose_carriers',return_value={'upper':'r0','lower':'r1'}), \
             patch.object(abyss_battle,'run_abyss_battle',side_effect=battle), \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}):
            result=intimacy.run(client,{'_cancel_event':event,'_account_key':'offline'})
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(battles),1)
        self.assertFalse(intimacy.recovery_pending(client,'offline'))
        self.assertFalse(abyss_loadout.pending(client,'offline'))

    def test_one_rejected_owner_does_not_block_other_equipment_or_bonds(self):
        state,waves=gear_fixture();state['Artifacts']={'Datas':[{'_id':'bond','StaticID':'unknown','EquipRole':'r0'}]}
        client=AssetServer(state);value=abyss_loadout.plan(client.account_state,waves)
        abyss_loadout.apply(client,value)
        value.update(schema=1,artifacts={'bond':'r8'})
        abyss_loadout._save(abyss_loadout._path(client),value)
        returned_to_r8={aid for aid,owner in value['equipment'].items() if owner=='r8'}
        self.assertTrue(returned_to_r8)
        original=client.call
        def call(route,body):
            if route=='EquipmentHandler.EquipsToRole' and body['RoleID'] in value['roles']:
                raise GameMessage('No Role')
            return original(route,body)
        with patch.object(client,'call',side_effect=call):
            result=abyss_loadout.recover_before_challenge(client)
        self.assertTrue(result['restored'],result)
        actual=abyss_loadout._owners(client.server['EquipmentContainer']['Datas'])
        self.assertTrue(all(actual[aid]=='r8' for aid in returned_to_r8))
        self.assertTrue(all(actual[aid]!=value['equipment'][aid] for aid in result['fallback']['equipment']))
        self.assertEqual(client.server['Artifacts']['Datas'][0]['EquipRole'],'r8')
        self.assertFalse(abyss_loadout.pending(client))

    def test_nested_no_role_journals_keep_oldest_owner_and_isolate_before_new_borrow(self):
        client=AssetServer(farm_state());items=client.server['EquipmentContainer']['Datas']
        old=next(e for e in items if not _oid(e.get('EquipRole')))
        bad=_oid(old['_id']);old['EquipRole']='r0';client.bootstrap()
        keys=['offline']+[intimacy.passenger_key(client,'offline',wave) for wave in intimacy.WAVES]
        for index,key in enumerate(keys):
            abyss_loadout._save(abyss_loadout._path(client,key),{'schema':1,'roles':[],
                'equipment':{bad:'' if index==0 else 'r1'},'artifacts':{}})
        original=client.call
        def call(route,body):
            if route=='EquipmentHandler.TakeOffEquip' and body['EquipID']==bad:raise GameMessage('No Role')
            return original(route,body)
        with patch.object(client,'call',side_effect=call):
            recovered=intimacy.recover_before_start(client,account_key='offline')
            self.assertTrue(recovered['restored'])
            self.assertIn(bad,recovered['equipment'])
            self.assertFalse(intimacy.recovery_pending(client,'offline'))
            self.assertIn(bad,abyss_loadout.deferred_assets(client,'offline')['equipment'])
            saved=abyss_loadout._load(abyss_loadout._deferred_path(client,'offline'))
            self.assertEqual(saved['originalDestinations']['equipment'],{bad:''})
            value=intimacy.plan_loadout(client.account_state,{'upper':'r0','lower':'r1'},
                exclude_equipment=recovered['equipment'],exclude_artifacts=recovered['artifacts'])
            self.assertTrue(value is None or bad not in value['equipment'])
            if value:abyss_loadout.apply(client,value,account_key='offline')
            self.assertTrue(abyss_loadout.restore(client,account_key='offline')['restored'])
        self.assertTrue(intimacy.recover_before_start(client,account_key='offline')['restored'])
        self.assertEqual(abyss_loadout._owners(client.server['EquipmentContainer']['Datas'])[bad],'')
        self.assertFalse(intimacy.recovery_pending(client,'offline'))

    def test_unknown_bond_metadata_does_not_prevent_original_owner_return(self):
        state,_=gear_fixture()
        state['Artifacts']={'Datas':[{'_id':'loan','StaticID':'NEW_BOND','LV':30,'EquipRole':'r0'}]}
        client=AssetServer(state)
        abyss_loadout._save(abyss_loadout._path(client),{'schema':1,'roles':['r0'],
            'equipment':{},'artifacts':{'loan':'r8'}})
        original=client.call
        def reject(route,body):
            if route=='ArtifactHandler.Equip':raise GameMessage('No Role')
            return original(route,body)
        with patch.object(client,'call',side_effect=reject):result=abyss_loadout.recover_before_challenge(client)
        self.assertTrue(result['restored'],result)
        self.assertEqual(client.server['Artifacts']['Datas'][0]['EquipRole'],'')
        self.assertEqual(result['fallback']['artifacts'],['loan'])
        self.assertFalse(abyss_loadout.pending(client))

    def test_lost_equip_response_reads_back_without_repeating_committed_items(self):
        state,waves=gear_fixture();client=AssetServer(state)
        value=abyss_loadout.plan(client.account_state,waves);abyss_loadout.apply(client,value)
        original=client.call;sent=[]
        def lost(route,body):
            response=original(route,body)
            if route=='EquipmentHandler.EquipsToRole':
                sent.extend(body['EquipIDs']);raise GameError('response lost')
            return response
        with patch.object(client,'call',side_effect=lost):result=abyss_loadout.restore(client)
        self.assertTrue(result['restored'],result)
        self.assertEqual(len(sent),len(set(sent)))

    def test_invalid_parent_journal_is_preserved_while_valid_child_returns(self):
        client=AssetServer(farm_state())
        item=next(e for e in client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole')))
        aid=_oid(item['_id']);parent=abyss_loadout._path(client,'offline')
        parent.parent.mkdir(parents=True,exist_ok=True);parent.write_text('{damaged')
        child=intimacy.passenger_key(client,'offline','upper')
        abyss_loadout._save(abyss_loadout._path(client,child),{'schema':1,'roles':[],
            'equipment':{aid:''},'artifacts':{}})
        result=intimacy.restore_all(client,account_key='offline')
        self.assertTrue(result['restored'])
        self.assertFalse(parent.exists())
        self.assertIn('warning',result)
        self.assertEqual(abyss_loadout._owners(client.server['EquipmentContainer']['Datas'])[aid],'')


if __name__=='__main__':unittest.main()
