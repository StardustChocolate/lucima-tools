"""An old unresolved journal cannot contaminate a later normal transaction."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from backend import abyss_loadout as loadout, config
from tools.test_abyss_auto import AssetServer, gear_fixture


class OldRecoveryRecords(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.patch=patch.object(config,'data_dir',return_value=Path(self.temp.name));self.patch.start();self.addCleanup(self.patch.stop)
        state,self.waves=gear_fixture();self.client=AssetServer(state)

    def test_missing_old_asset_is_closed_then_new_loadout_restores_its_own_baseline(self):
        path=loadout._path(self.client)
        loadout._save(path,{'schema':1,'equipment':{'gone':'r0'},'artifacts':{},'roles':[]})
        result=loadout.recover_before_challenge(self.client)
        self.assertTrue(result['restored']);self.assertEqual(result['fallback']['missingEquipment'],['gone']);self.assertFalse(loadout.pending(self.client))
        baseline=loadout._owners(self.client.server['EquipmentContainer']['Datas'])
        current=loadout.plan(self.client.account_state,self.waves)
        self.assertNotIn('gone',current['equipment'])
        loadout.apply(self.client,current)
        self.assertTrue(loadout.pending(self.client))
        self.assertTrue(loadout.restore(self.client)['restored'])
        self.assertEqual(loadout._owners(self.client.server['EquipmentContainer']['Datas']),baseline)
        self.assertFalse(loadout.pending(self.client))
        self.assertEqual(len(list((path.parent/'history').glob('*.json'))),1)

    def test_bad_old_journal_is_archived_without_blocking_the_next_start(self):
        path=loadout._path(self.client);path.parent.mkdir(parents=True);path.write_text('{bad-json')
        result=loadout.recover_before_challenge(self.client)
        self.assertTrue(result['restored']);self.assertTrue(result['closed']);self.assertFalse(path.exists())
        self.assertEqual(next((path.parent/'history').glob('*.json')).read_text(),'{bad-json')
        self.assertEqual(loadout.recover_before_challenge(self.client),{'restored':True,'changed':False})


if __name__=='__main__':unittest.main()
