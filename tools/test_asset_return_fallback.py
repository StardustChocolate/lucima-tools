"""Original-owner evidence and warehouse fallback, including cold restarts."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from backend import abyss_loadout as loadout, config
from backend.game_client import GameMessage
from backend.tasks import _oid
from tools.test_abyss_auto import AssetServer, gear_fixture


class ReturnEvidence(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        override=patch.object(config,'data_dir',return_value=self.root);override.start();self.addCleanup(override.stop)
        state,self.waves=gear_fixture();self.client=AssetServer(state)
        self.item=next(e for e in self.client.server['EquipmentContainer']['Datas'] if _oid(e.get('EquipRole'))=='r1')
        self.item['EquipRole']='r0'
        next(r for r in self.client.server['RoleDataContainer']['Roles'] if _oid(r['_id'])=='r8')['StaticID']='H002'
        self.client.bootstrap()
        self.aid=_oid(self.item['_id'])

    def save(self, owner, **extra):
        loadout._save(loadout._path(self.client),dict(schema=1,equipment={self.aid:owner},artifacts={},roles=[],**extra))

    def test_static_id_in_old_record_is_resolved_to_unique_owned_instance(self):
        role=next(r for r in self.client.server['RoleDataContainer']['Roles'] if _oid(r['_id'])=='r8')
        self.save(role['StaticID'])
        result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result);self.assertEqual(self.item['EquipRole'],'r8')
        self.assertFalse(result['fallback']['equipment'])

    def test_removed_instance_is_remapped_only_with_recorded_role_identity(self):
        role=next(r for r in self.client.server['RoleDataContainer']['Roles'] if _oid(r['_id'])=='r8')
        self.save('old-r8',ownerRoles={'old-r8':role['StaticID']})
        self.assertTrue(loadout.restore(self.client)['restored'])
        self.assertEqual(self.item['EquipRole'],'r8')

    def test_duplicate_static_id_never_guesses_original_owner(self):
        roles=self.client.server['RoleDataContainer']['Roles']
        roles[1]['StaticID']=roles[0]['StaticID'];self.client.bootstrap()
        self.save('old-instance',ownerRoles={'old-instance':roles[0]['StaticID']})
        result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result);self.assertEqual(self.item['EquipRole'],'')
        self.assertIn(self.aid,result['fallback']['equipment'])

    def test_plan_drift_is_detected_before_any_equipment_request(self):
        value=loadout.plan(self.client.account_state,self.waves)
        aid=next(iter(value['equipment']));self.client.account_state['EquipmentContainer']['Datas']=deepcopy(self.client.account_state['EquipmentContainer']['Datas'])
        next(e for e in self.client.account_state['EquipmentContainer']['Datas'] if _oid(e['_id'])==aid)['EquipRole']='new-owner'
        with patch.object(self.client,'call') as call:
            with self.assertRaisesRegex(loadout.LoadoutUnavailable,'归属已变化'):loadout.apply(self.client,value)
        call.assert_not_called();self.assertFalse(loadout.pending(self.client))

    def test_original_owners_are_written_before_first_mutation(self):
        orphan=next(e for e in self.client.server['EquipmentContainer']['Datas'] if not _oid(e.get('EquipRole')))
        orphan['EquipRole']='deleted-role';self.client.bootstrap()
        value=loadout.plan(self.client.account_state,self.waves)
        before=loadout._owners(self.client.server['EquipmentContainer']['Datas']);original=self.client.call
        def call(route,body):
            record=json.loads(loadout._path(self.client).read_text())
            self.assertTrue(all(record['originalDestinations']['equipment'][aid]==before[aid] for aid in record['equipment']))
            self.assertIn('r0',record['ownerRoles'])
            return original(route,body)
        with patch.object(self.client,'call',side_effect=call):loadout.apply(self.client,value)
        self.assertEqual(json.loads(loadout._path(self.client).read_text())['originalDestinations']['equipment'][_oid(orphan['_id'])],'deleted-role')
        self.assertTrue(loadout.restore(self.client)['restored'])

    def test_confirmed_inventory_fallback_never_replays_rejected_owner_after_restart(self):
        self.save('r8');original=self.client.call
        def reject(route,body):
            if route=='EquipmentHandler.EquipsToRole':raise GameMessage('No Role')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=reject):result=loadout.restore(self.client)
        self.assertTrue(result['restored'],result);self.assertEqual(self.item['EquipRole'],'')
        record=json.loads(next((self.root/'abyss-recovery/history').glob('*.json')).read_text())
        self.assertEqual(record['originalDestinations']['equipment'][self.aid],'r8')
        # Simulate termination after the server unload but before journal closure.
        loadout._save(loadout._path(self.client),record)
        restarted=AssetServer(self.client.server)
        with patch.object(restarted,'call',wraps=restarted.call) as call:
            result=loadout.restore_many(restarted,[None])
        self.assertTrue(result['restored'],result)
        self.assertFalse(any(c.args[0]=='EquipmentHandler.EquipsToRole' for c in call.call_args_list))
        self.assertIn(self.aid,result['fallback']['equipment'])

    def test_failed_unload_keeps_durable_original_and_does_not_claim_inventory_success(self):
        self.save('r8');original=self.client.call
        def reject(route,body):
            if route in ('EquipmentHandler.EquipsToRole','EquipmentHandler.TakeOffEquip'):raise GameMessage('No Role')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=reject):result=loadout.restore_many(self.client,[None],defer_no_role=True)
        self.assertTrue(result['restored'],result);self.assertIn(self.aid,result['deferred']['equipment'])
        self.assertEqual(self.item['EquipRole'],'r0')
        record=json.loads(loadout._deferred_path(self.client).read_text())
        self.assertEqual(record['originalDestinations']['equipment'][self.aid],'r8')


if __name__=='__main__':unittest.main()
