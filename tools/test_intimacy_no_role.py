"""Persistent native No Role recovery must isolate assets, then actually farm."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import httpx

from backend import abyss_loadout as loadout, intimacy, config, abyss_battle, equipment_sale
from backend.game_client import GameClient, GameError, GameMessage
from backend.tasks import _oid
from equipment import plans
from tools.test_abyss_auto import AssetServer
from tools.test_intimacy import farm_state


class NoRoleRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        override=patch.object(config,'data_dir',return_value=self.root)
        override.start();self.addCleanup(override.stop)
        self.client=AssetServer(farm_state())
        self.snapshots=[];self.cancel=threading.Event()

    def journal(self, equipment=None, artifacts=None):
        loadout._save(loadout._path(self.client,'offline'),{'schema':1,'roles':['r0'],
            'equipment':equipment or {},'artifacts':artifacts or {}})

    def wear_r0(self):
        worn=[]
        for slot in plans.SLOTS:
            item=next(e for e in self.client.server['EquipmentContainer']['Datas'] if plans.equip_slot(e)==slot)
            item['EquipRole']='r0';worn.append(item)
        self.client.bootstrap()
        return worn[0]

    def run_farm(self, call):
        battles=[]
        def battle(client,params):
            battles.append(deepcopy(params['waves']))
            for row in client.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(self.client,'call',side_effect=call), \
             patch.object(intimacy,'choose_carriers',return_value={'upper':'r0','lower':'r1'}), \
             patch.object(abyss_battle,'run_abyss_battle',side_effect=battle), \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}):
            result=intimacy.run(self.client,{'_account_key':'offline','_cancel_event':self.cancel,
                '_on_progress':self.snapshots.append})
        return result,battles

    def test_orphaned_equipment_no_role_is_not_retried_as_an_eternal_start_gate(self):
        item=self.client.server['EquipmentContainer']['Datas'][0]
        aid=_oid(item['_id']);item['EquipRole']='deleted-role';self.client.bootstrap()
        self.journal({aid:'deleted-role'})
        original=self.client.call;rejections=[]
        def strict_server(route,body):
            # The old fake server accepted nonexistent role IDs and missed this.
            roles={_oid(r['_id']) for r in self.client.server['RoleDataContainer']['Roles']}
            if route.startswith(('EquipmentHandler.','ArtifactHandler.')) and body.get('RoleID') not in roles:
                rejections.append((route,body));raise GameMessage('No Role')
            return original(route,body)
        result,battles=self.run_farm(strict_server)
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),1)
        self.assertTrue(rejections)
        self.assertFalse(loadout.pending(self.client,'offline'))
        saved=json.loads(loadout._deferred_path(self.client,'offline').read_text())
        self.assertEqual(saved['originalDestinations']['equipment'],{aid:'deleted-role'})
        self.assertEqual(result['deferredAssets']['equipment'],[aid])
        self.assertIn('未计为已归还',result['detail'])
        self.assertIn(aid,loadout.reserved_equipment(self.client,'offline'))
        # Cold restart retains the original destination and still permits start.
        restarted=AssetServer(self.client.server)
        with patch.object(restarted,'call',side_effect=strict_server):
            recovered=intimacy.recover_before_start(restarted,account_key='offline')
        self.assertTrue(recovered['restored'],recovered)
        self.assertIn(aid,recovered['equipment'])

    def test_rejected_return_is_unloaded_and_farming_continues(self):
        item=self.wear_r0()
        aid=_oid(item['_id']);self.journal({aid:'r8'})
        held={_oid(e['_id']):deepcopy(e) for e in self.client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole'))=='r0'}
        held_artifacts={_oid(a['_id']):deepcopy(a) for a in self.client.server['Artifacts']['Datas'] if _oid(a.get('EquipRole'))=='r0'}
        original=self.client.call
        def reject(route,body):
            if route=='EquipmentHandler.EquipsToRole' and aid in body['EquipIDs']:raise GameMessage('No Role')
            return original(route,body)
        result,battles=self.run_farm(reject)
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),1)
        after={_oid(e['_id']):e for e in self.client.server['EquipmentContainer']['Datas']}
        self.assertEqual(after[aid]['EquipRole'],'')
        self.assertTrue(all(after[eid]==before for eid,before in held.items() if eid!=aid))
        arts={_oid(a['_id']):a for a in self.client.server['Artifacts']['Datas']}
        self.assertTrue(all(arts[art_id]==before for art_id,before in held_artifacts.items()))
        self.assertFalse(any(loadout.deferred_assets(self.client,'offline').values()))
        history=[json.loads(path.read_text()) for path in (self.root/'abyss-recovery/history').glob('*.json')]
        record=next(row for row in history if aid in row.get('warehouseDestinations',{}).get('equipment',{}))
        self.assertEqual(record['originalDestinations']['equipment'][aid],'r8')
        self.assertEqual(record['ownerDiagnostics']['equipment:'+aid]['reason'],'server_no_role')
        self.assertFalse(loadout.pending(self.client,'offline'))

    def test_orphaned_bond_no_role_is_preserved_and_excluded_from_new_loans(self):
        bond=self.client.server['Artifacts']['Datas'][0]
        aid=_oid(bond['_id']);bond['EquipRole']='deleted-role';self.client.bootstrap()
        self.journal(artifacts={aid:'deleted-role'})
        original=self.client.call
        def reject(route,body):
            if route=='ArtifactHandler.UnEquip' and body['ArtifactID']==aid:raise GameMessage('No Role')
            if route=='ArtifactHandler.Equip' and body['ArtifactID']==aid:raise AssertionError('isolated bond borrowed again')
            return original(route,body)
        result,battles=self.run_farm(reject)
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),1)
        self.assertEqual(result['deferredAssets']['artifacts'],[aid])
        record=json.loads(loadout._deferred_path(self.client,'offline').read_text())
        self.assertEqual(record['originalDestinations']['artifacts'][aid],'deleted-role')

    def test_failed_returns_are_skipped_only_after_fresh_server_state_is_confirmed(self):
        item=self.wear_r0()
        aid=_oid(item['_id']);self.journal({aid:'r8'})
        original=self.client.call
        def broken(route,body):
            if route=='EquipmentHandler.EquipsToRole':raise ConnectionError('network down')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=broken):
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertTrue(result['restored']);self.assertFalse(loadout.pending(self.client,'offline'))
        self.assertTrue(loadout._deferred_path(self.client,'offline').exists())
        with patch.object(self.client,'bootstrap',side_effect=GameError('cannot confirm server state')):
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertFalse(result['restored']);self.assertTrue(loadout.pending(self.client,'offline'))

    def test_isolation_write_failure_keeps_active_gate_and_original_owner(self):
        item=self.client.server['EquipmentContainer']['Datas'][0];aid=_oid(item['_id'])
        item['EquipRole']='deleted-role';self.client.bootstrap();self.journal({aid:'deleted-role'})
        original=self.client.call;save=loadout._save
        def reject(route,body):
            if route=='EquipmentHandler.TakeOffEquip' and body['EquipID']==aid:raise GameMessage('No Role')
            return original(route,body)
        def storage(path,record):
            if path.parent.name=='deferred':raise OSError('disk full')
            return save(path,record)
        with patch.object(self.client,'call',side_effect=reject), patch.object(loadout,'_save',side_effect=storage):
            with self.assertRaises(OSError):intimacy.recover_before_start(self.client,account_key='offline')
        value=json.loads(loadout._path(self.client,'offline').read_text())
        self.assertEqual(value['originalDestinations']['equipment'][aid],'deleted-role')

    def test_another_automatic_task_cannot_displace_an_isolated_item_via_its_holder(self):
        item=self.wear_r0();aid=_oid(item['_id'])
        loadout._save(loadout._deferred_path(self.client,'offline'),{'schema':1,'equipment':{aid:'r8'},'artifacts':{}})
        baseline=deepcopy(self.client.server)
        other=next(e for e in self.client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole'))!='r0')
        value={'roles':['r0'],'targets':{'r0':[_oid(other['_id'])]},'equipment':{},'artifacts':{},'artifactTargets':{}}
        with self.assertRaises(loadout.LoadoutUnavailable):loadout.apply(self.client,value,account_key='offline')
        self.assertEqual(self.client.server,baseline)
        self.assertFalse(loadout.pending(self.client,'offline'))

    def test_reported_plain_http_no_role_on_first_apply_recovers_and_farms_without_second_click(self):
        evidence=json.loads((Path(__file__).parent/'fixtures/artifact_unequip_no_role.json').read_text())
        bond=self.client.server['Artifacts']['Datas'][0]
        bond.update(_id={'$oid':evidence['data']['ArtifactID']},EquipRole=evidence['data']['RoleID'])
        self.client.bootstrap()
        original=self.client.call;rejections=[]
        wire=GameClient.__new__(GameClient)
        wire.aid='offline';wire.session_id='offline-session'
        wire._sync_proxy=lambda:None
        def transport(method,url,**kwargs):
            payload=json.loads(kwargs['content'])
            self.assertEqual(payload['route'],evidence['route'])
            self.assertEqual({key:payload['data'][key] for key in evidence['data']},evidence['data'])
            rejections.append(payload['route'])
            return httpx.Response(evidence['httpStatus'],text=evidence['responseText'],request=httpx.Request(method,url))
        wire._request_http=transport
        def reject(route,body):
            if route==evidence['route'] and body['ArtifactID']==evidence['data']['ArtifactID']:
                wire.account_state=deepcopy(self.client.account_state)
                return wire.call(route,wire._auth_data(body))
            return original(route,body)
        result,battles=self.run_farm(reject)
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),1)
        # One failed first apply + two recovery attempts with fresh sync. Final
        # cleanup must not replay the isolated old bond after farming succeeds.
        self.assertEqual(len(rejections),3)
        saved=json.loads(loadout._deferred_path(self.client,'offline').read_text())
        self.assertEqual(saved['originalDestinations']['artifacts'],{evidence['data']['ArtifactID']:evidence['data']['RoleID']})

    def test_final_cleanup_preserves_old_isolation_when_a_different_bond_is_rejected(self):
        first,second=self.client.server['Artifacts']['Datas'][:2]
        old_id,new_id=(_oid(item['_id']) for item in (first,second))
        first['EquipRole']='removed-old';second['EquipRole']='removed-new';self.client.bootstrap()
        loadout._save(loadout._deferred_path(self.client,'offline'),{'schema':1,'roles':[],
            'equipment':{},'artifacts':{old_id:'removed-old'},
            'originalDestinations':{'equipment':{},'artifacts':{old_id:'removed-old'}}})
        self.journal(artifacts={new_id:'removed-new'})
        original=self.client.call;sent=[]
        def reject(route,body):
            if route=='ArtifactHandler.UnEquip':
                sent.append(body['ArtifactID']);raise GameMessage('No Role')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=reject):
            result=intimacy.restore_all(self.client,account_key='offline')
        self.assertTrue(result['restored'],result)
        self.assertNotIn(old_id,sent)
        saved=json.loads(loadout._deferred_path(self.client,'offline').read_text())
        self.assertEqual(saved['originalDestinations']['artifacts'],{old_id:'removed-old',new_id:'removed-new'})
        # A later start / explicit restore can retry after the server state was
        # repaired; isolation is not erased or treated as an actual return.
        self.assertTrue(intimacy.restore_all(self.client,account_key='offline',retry_deferred=True)['restored'])
        self.assertFalse(any(loadout.deferred_assets(self.client,'offline').values()))

    def test_full_sync_replaces_stale_role_and_asset_lists_before_return(self):
        item=self.wear_r0();aid=_oid(item['_id']);self.journal({aid:'r8'})
        self.client.account_state['RoleDataContainer']['Roles']=[]
        self.client.account_state['EquipmentContainer']['Datas']=[]
        events=[];bootstrap=self.client.bootstrap;call=self.client.call
        def sync():
            events.append('sync');return bootstrap()
        def request(route,body):
            events.append(route)
            self.assertIn('r8',{_oid(r['_id']) for r in plans._roles(self.client)})
            return call(route,body)
        with patch.object(self.client,'bootstrap',side_effect=sync),patch.object(self.client,'call',side_effect=request):
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertTrue(result['restored'],result)
        self.assertEqual(events[0],'sync');self.assertEqual(item['EquipRole'],'r8')
        self.assertFalse(any(result['fallback'].values()))

    def test_no_role_resync_resolves_changed_instance_before_retry(self):
        item=self.wear_r0();aid=_oid(item['_id']);self.journal({aid:'r8'})
        saved=loadout._load(loadout._path(self.client,'offline'))
        role=next(r for r in self.client.server['RoleDataContainer']['Roles'] if _oid(r['_id'])=='r8')
        saved['ownerRoles']={'r8':role['StaticID']};loadout._save(loadout._path(self.client,'offline'),saved)
        call=self.client.call;destinations=[]
        def request(route,body):
            if route=='EquipmentHandler.EquipsToRole' and body['EquipIDs']==[aid]:
                destinations.append(body['RoleID'])
                if len(destinations)==1:
                    role['_id']='new-r8'
                    raise GameMessage('No Role')
            return call(route,body)
        with patch.object(self.client,'call',side_effect=request):
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertTrue(result['restored'],result)
        self.assertEqual(destinations,['r8','new-r8']);self.assertEqual(item['EquipRole'],'new-r8')
        self.assertFalse(any(loadout.deferred_assets(self.client,'offline').values()))

    def test_incomplete_sync_keeps_originals_and_does_not_mutate_inventory(self):
        item=self.wear_r0();aid=_oid(item['_id']);self.journal({aid:'r8'})
        self.client.server.pop('RoleDataContainer')
        with patch.object(self.client,'call',wraps=self.client.call) as call:
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertFalse(result['restored']);call.assert_not_called()
        self.assertIn('完整角色信息',result['error'])
        self.assertEqual(loadout._load(loadout._path(self.client,'offline'))['originalDestinations']['equipment'],{aid:'r8'})

    def test_transient_no_role_on_apply_replans_without_requiring_new_isolation(self):
        call=self.client.call;rejected=[]
        def request(route,body):
            if route=='EquipmentHandler.EquipsToRole' and not rejected:
                rejected.append(route);raise GameMessage('No Role')
            return call(route,body)
        result,battles=self.run_farm(request)
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),1)
        self.assertFalse(any(loadout.deferred_assets(self.client,'offline').values()))

    def test_unrelated_apply_error_is_preserved_instead_of_masked_by_handler(self):
        def request(route,body):raise GameError('original operation failed')
        result,battles=self.run_farm(request)
        self.assertFalse(result['ok']);self.assertFalse(battles)
        self.assertIn('original operation failed',result['detail'])
        self.assertNotIn('has no attribute',result['detail'])

    def test_no_role_during_later_passenger_batch_continues_same_worker(self):
        call=self.client.call;battles=[];rejected=[]
        def request(route,body):
            if battles and not rejected and route=='EquipmentHandler.EquipsToRole' and body['RoleID']=='r7' and len(body['EquipIDs'])==6:
                rejected.append(route);raise GameMessage('No Role')
            return call(route,body)
        def battle(client,params):
            battles.append(deepcopy(params['waves']))
            owned={_oid(r['_id']):r['StaticID'] for r in client.server['RoleDataContainer']['Roles']}
            sids={owned[rid] for team in params['waves'].values() for rid in team if rid}
            for row in client.server['LineRoleDataContainer']['RoleList']:
                if row['Info']['StaticID'] in sids:row['Info']['IntimacyLV']=10
            self.assertLessEqual(len(battles),2)
            return {'ok':True,'settlementConfirmed':True}
        with patch.object(self.client,'call',side_effect=request), \
             patch.object(intimacy,'choose_carriers',return_value={'upper':'r0','lower':'r1'}), \
             patch.object(abyss_battle,'run_abyss_battle',side_effect=battle), \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}):
            result=intimacy.run(self.client,{'_account_key':'offline','_cancel_event':self.cancel})
        self.assertTrue(result['ok'],result);self.assertEqual(len(battles),2);self.assertEqual(len(rejected),1)

    def test_optimistic_response_cannot_clear_unreturned_assets(self):
        item=self.wear_r0();aid=_oid(item['_id']);self.journal({aid:'r8'})
        call=self.client.call
        def request(route,body):
            if route=='EquipmentHandler.EquipsToRole' and body['EquipIDs']==[aid]:
                fake=deepcopy(item);fake['EquipRole']='r8'
                return {'NewEquip':fake}  # Server still has the original holder.
            return call(route,body)
        with patch.object(self.client,'call',side_effect=request):
            result=intimacy.recover_before_start(self.client,account_key='offline')
        self.assertTrue(result['restored']);self.assertFalse(result['fullyRestored'])
        self.assertEqual(result['deferred']['equipment'],[aid]);self.assertEqual(item['EquipRole'],'r0')
        self.assertIn(aid,loadout.reserved_equipment(self.client,'offline'))


if __name__=='__main__':unittest.main()
