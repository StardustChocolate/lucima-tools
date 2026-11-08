"""Missing hero data resumes farming without holding the account lock."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from backend import appcore, abyss_battle, config, intimacy, resource_store, resource_sync
from backend.abyss_state import metadata
from backend.game_client import GameError, GameMessage
from tools import test_intimacy as fixtures


class MissingRoles(unittest.TestCase):
    setUp = fixtures.RunTests.setUp

    def test_no_role_recovery_updates_missing_new_role_metadata_before_replanning(self):
        table=deepcopy(metadata());definition=table['tables']['Role']['H026']
        original=self.client.call;rejected=[]
        def request(route,body):
            if route=='EquipmentHandler.EquipsToRole' and not rejected:
                rejected.append(route);table['tables']['Role'].pop('H026')
                raise GameMessage('No Role')
            return original(route,body)
        def synchronize(**kwargs):table['tables']['Role']['H026']=definition
        def battle(client,params):
            self.assertIn('H026',table['tables']['Role'])
            for row in client.server['LineRoleDataContainer']['RoleList']:row['Info']['IntimacyLV']=10
            return {'ok':True,'settlementConfirmed':True}
        with patch('backend.abyss_state.metadata',return_value=table), \
             patch.object(intimacy,'metadata',return_value=table), \
             patch.object(resource_sync,'synchronize',side_effect=synchronize) as sync, \
             patch.object(self.client,'call',side_effect=request), \
             patch.object(intimacy,'choose_carriers',return_value={'upper':'r0','lower':'r1'}), \
             patch.object(abyss_battle,'run_abyss_battle',side_effect=battle) as fight, \
             patch.object(config,'get_request_delay_config',return_value={'enabled':False}):
            result=intimacy.run(self.client,self.params)
        self.assertTrue(result['ok'],result);sync.assert_called_once();fight.assert_called_once()

    def test_missing_definition_downloads_then_automatically_farms_with_lock_released(self):
        table = deepcopy(metadata())
        original = table['tables']['Role'].pop('H026')
        lock = threading.Lock()
        self.params['_account_lock'] = lock

        def synchronize(**kwargs):
            self.assertTrue(kwargs['force'])
            self.assertTrue(lock.acquire(blocking=False), 'metadata publication must be able to take the account lock')
            lock.release()
            self.assertIn('offline', resource_sync._waiting_accounts)
            table['tables']['Role']['H026'] = original

        def battle(client, params):
            for row in client.server['LineRoleDataContainer']['RoleList']:
                row['Info']['IntimacyLV'] = 10
            return {'ok': True, 'settlementConfirmed': True}

        with patch('backend.abyss_state.metadata', return_value=table), \
             patch.object(intimacy, 'metadata', return_value=table), \
             patch.object(resource_sync, 'synchronize', side_effect=synchronize) as sync, \
             patch.object(intimacy, 'choose_carriers', return_value={'upper': 'r0', 'lower': 'r1'}), \
             patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle) as fight, \
             patch.object(config, 'get_request_delay_config', return_value={'enabled': False}):
            result = intimacy.run(self.client, self.params)
        self.assertTrue(result['ok'], result)
        sync.assert_called_once()
        fight.assert_called_once()
        self.assertTrue(any(row['phase'] == 'syncing_resources' and 'H026' in row['message'] for row in self.snapshots))
        self.assertNotIn('offline', resource_sync._waiting_accounts)
        self.assertFalse(lock.locked())

    def test_failed_download_waits_for_automatic_retry_and_remains_cancellable(self):
        table = deepcopy(metadata()); table['tables']['Role'].pop('H806')
        messages = []
        def progress(message):
            messages.append(message)
            if '自动重试' in message:
                self.cancel.set()
        with patch('backend.abyss_state.metadata', return_value=table), \
             patch.object(resource_sync, 'synchronize', side_effect=ConnectionError('offline')):
            with self.assertRaisesRegex(GameError, '已中止'):
                resource_sync.ensure_roles({'H806'}, account_key='offline', cancel_event=self.cancel, on_progress=progress)
        self.assertTrue(any('自动获取角色资料' in message for message in messages))
        self.assertTrue(any('自动重试' in message for message in messages))
        self.assertNotIn('offline', resource_sync._waiting_accounts)

    def test_publisher_allows_only_the_paused_intimacy_session(self):
        with patch('backend.accounts.all_accounts', return_value=[]), \
             patch.object(appcore, '_abyss_sessions', {'offline': object()}), \
             patch.object(appcore, '_abyss_session_modes', {'offline': 'intimacy'}), \
             patch.object(appcore, '_activity_sessions', {}), \
             patch.object(resource_sync, '_waiting_accounts', {'offline'}), \
             patch.object(resource_store, 'activate') as activate:
            self.assertTrue(resource_sync._publish_when_idle(Path('/unused')))
            activate.assert_called_once()
            appcore._abyss_sessions['another'] = object()
            self.assertFalse(resource_sync._publish_when_idle(Path('/unused')))
            self.assertEqual(activate.call_count, 1)

    def test_same_version_repair_uses_a_separate_generation_and_preserves_previous_files(self):
        with TemporaryDirectory() as folder, patch.object(config, 'data_dir', return_value=Path(folder)):
            base = Path(folder) / 'game-resources'; base.mkdir()
            version = 'catalog_999999'
            for generation, content in ((version, b'old complete data'), (version + '-0123abcd', b'repaired data')):
                root = base / generation; root.mkdir()
                (root / 'data.json').write_bytes(content)
                (root / 'manifest.json').write_text(json.dumps({'version': version, 'files': {
                    'data.json': hashlib.sha256(content).hexdigest()}}))
            original = base / version
            repaired = base / (version + '-0123abcd')
            resource_store.activate(original)
            resource_store.activate(repaired)
            resource_store._roots.pop(str(base))
            self.assertEqual(resource_store.active_root(), repaired)
            self.assertEqual((original / 'data.json').read_bytes(), b'old complete data')
            self.assertEqual(json.loads((base / 'current.json').read_text())['version'], version)


if __name__ == '__main__':
    unittest.main()
