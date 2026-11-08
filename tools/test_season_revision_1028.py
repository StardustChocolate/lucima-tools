"""New-period availability, preferred mains, incomplete bonds, resource transactions."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest
from unittest.mock import patch

from backend import abyss_team, abyss_loadout, intimacy, activity_progress, resource_store, resource_sync
from backend.abyss_state import metadata, fantasy_config
from backend.tasks import abyss_scenes, _oid
from equipment import stats
from tools.test_abyss_auto import roster, AssetServer
from tools.test_intimacy import farm_state

ROOT = Path(__file__).resolve().parents[1]


def current_state():
    state = roster()
    state['ActivityDBSettingContainer']['SettingList'].extend([
        {'ActivityID': aid, 'IsIgnoreTime': True} for aid in ('Fantasy_H806', 'BranchH806', 'Version_Elite')])
    state['SceneDataContainer']['Scenes'] = [{'StaticID': f'MysteriousRealm_{i}', 'Stars': [1, 1, 1, 1]}
                                            for i in range(1, 7)]
    return state


class NewPeriod(unittest.TestCase):
    def test_new_period_unlocks_stage_seven_from_six_clears_without_inventing_later_unlocks(self):
        state = current_state()
        self.assertEqual([row['floor'] for row in abyss_scenes(state)], list(range(1, 8)))
        for i in range(7, 11):
            self.assertIsNotNone(fantasy_config(f'MysteriousRealm_{i}', state))
            state['SceneDataContainer']['Scenes'].append({'StaticID': f'MysteriousRealm_{i}', 'Stars': [1, 0, 0, 0]})
        self.assertEqual(len(abyss_scenes(state)), 10)

    def test_new_activity_is_listed_before_first_clear_and_expired_period_is_hidden(self):
        state = current_state()
        rows = activity_progress.scenes(state, 'BH806')
        self.assertEqual(len(rows), 13)  # Hell still requires its independent server flag.
        state['ActivityDBSettingContainer']['SettingList'].append({'ActivityID': 'Version_Hell', 'IsIgnoreTime': True})
        self.assertEqual(len(activity_progress.scenes(state, 'BH806')), 14)
        self.assertTrue(all(not row['cleared'] for row in rows))
        next(row for row in state['ActivityDBSettingContainer']['SettingList'] if row['ActivityID']=='BranchH806').update(IsIgnoreTime=False,
            StartTime={'$date': 0}, EndTime={'$date': 1})
        self.assertEqual(activity_progress.scenes(state, 'BH806'), [])

    def test_new_bond_has_full_properties_and_no_longer_needs_legacy_index(self):
        state = roster(); role = state['RoleDataContainer']['Roles'][0]
        state['Artifacts'] = {'Datas': [{'_id': 'new-bond', 'StaticID': 'A0464', 'LV': 30, 'EquipRole': 'r0'}]}
        legacy = deepcopy(stats.tables()); legacy['artifacts'].pop('A0464')
        with patch.object(stats, 'tables', return_value=legacy):
            self.assertGreater(stats.role_panel(state, role, [], include_artifact_effects=True)['panel']['HP'], 0)


class PreferredMains(unittest.TestCase):
    def state(self):
        state = roster()
        state['RoleDataContainer']['Roles'].extend([
            {'_id': sid, 'StaticID': sid, 'LV': 60, 'Star': 5} for sid in ('H165', 'H146')])
        state['Artifacts'] = {'Datas': [{'_id': 'unknown', 'StaticID': 'future-bond', 'EquipRole': 'H165'}]}
        return state

    def test_farming_prioritizes_both_over_buffs_and_element_matchups_and_unknown_worn_bond(self):
        state = self.state(); before = deepcopy(state)
        wave = {'Monsters': [{'M': 'FH002', 'Boss': True}], 'Buffs': {}}
        rule = abyss_team.wave_policy(wave); rule['preferredElements'] = {'Ice'}
        with patch.object(intimacy, 'fantasy_config', return_value={'Waves': [wave, wave]}), \
             patch.object(abyss_team, 'wave_policy', return_value=rule):
            result = intimacy.choose_carriers(state)
            self.assertEqual(set(result.values()), {'H165', 'H146'})
        self.assertEqual(state, before)

    def test_hard_job_ban_and_non_maximum_level_override_priority(self):
        state = self.state()
        wave = {'Monsters': [{'M': 'FH002', 'Boss': True}],
                'Buffs': {'DBs': [{'B': 'FB_Job_002', 'Target': 'Self'}]}}
        state['RoleDataContainer']['Roles'][-2]['LV'] = 59
        with patch.object(intimacy, 'fantasy_config', return_value={'Waves': [wave, wave]}):
            result = intimacy.choose_carriers(state)
        self.assertFalse({'H165', 'H146'}.intersection(result.values()))

    def test_passive_seal_cancels_special_priority(self):
        state = self.state(); role = state['RoleDataContainer']['Roles'][-2]
        rule = abyss_team.wave_policy({'Buffs': {}}); rule['flags'] = {'SealPassive'}
        self.assertFalse(abyss_team.preferred_main(role, rule, abyss_team.role_traits(role, seal_passive=True)))

    def test_automatic_abyss_places_one_priority_main_in_each_half_even_without_bonus_composition(self):
        state = self.state()
        rule = abyss_team.wave_policy({'Buffs': {}})
        rule['requiredJobs'] = {'Medic': 4}
        with patch.object(abyss_team, 'fantasy_config', return_value={'Waves': [{}, {}]}), \
             patch.object(abyss_team, 'wave_policy', return_value=rule):
            result = abyss_team.choose_teams(state, 'MysteriousRealm_1')
        for wave in ('upper', 'lower'):
            self.assertEqual(len({'H165', 'H146'}.intersection(result['waves'][wave])), 1)

    def test_unknown_only_bond_does_not_stop_planning_or_return_transaction(self):
        state = farm_state(); state['Artifacts']['Datas'] = [{'_id': 'unknown', 'StaticID': 'future', 'EquipRole': 'r0', 'LV': 1}]
        before = deepcopy(state)
        with TemporaryDirectory() as directory, patch('backend.config.data_dir', return_value=Path(directory)):
            plan = intimacy.plan_loadout(state, {'upper': 'r0', 'lower': 'r1'})
            self.assertEqual(plan['artifactTargets'], {'r0': '', 'r1': ''})
            self.assertEqual(state, before)
            client = AssetServer(state)
            abyss_loadout.apply(client, plan)
            self.assertEqual(client.server['Artifacts']['Datas'][0]['EquipRole'], '')
            result = abyss_loadout.restore(client)
            self.assertTrue(result['restored'])
            self.assertFalse(abyss_loadout.pending(client))
            self.assertFalse(any('__empty_bond_' in str(body) for _, body in client.calls))


class AutomaticResourceSync(unittest.TestCase):
    def test_android_web_root_keeps_python_package_data_in_its_real_location(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'ARK_ASSET_ROOT':directory}), \
             patch('backend.config.data_dir',return_value=Path(directory)):
            self.assertEqual(resource_store.path('equipment/stat_tables.json'),ROOT/'equipment/stat_tables.json')
            self.assertEqual(resource_store.path('backend/equip_ref.json'),ROOT/'backend/equip_ref.json')
            self.assertEqual(resource_store.path('backend/commodity_catalog.json'),ROOT/'backend/commodity_catalog.json')
            self.assertEqual(resource_store.path('assets/abyss_battle_static.json'),Path(directory)/'assets/abyss_battle_static.json')

    def test_active_farming_defers_publication_and_does_not_wait_for_its_account_lock(self):
        from backend import appcore
        with patch('backend.accounts.all_accounts',return_value=[]), \
             patch.dict(appcore._abyss_sessions,{'active-farm':object()}), \
             patch.object(resource_store,'activate') as publish:
            self.assertFalse(resource_sync._publish_when_idle(Path('/unused')))
            publish.assert_not_called()

    def test_official_bundle_pipeline_publishes_atomically_persists_and_keeps_old_generation_on_bad_download(self):
        evidence = ROOT / 'build/season-refresh-1028'
        if not (evidence / 'catalog_115942.json').is_file():
            self.skipTest('Local official-bundle evidence is not present')
        info = json.loads((evidence / 'public-bulletin.json').read_text())['Info']
        old_bundled = resource_store.bundled
        with TemporaryDirectory() as directory, patch('backend.config.data_dir', return_value=Path(directory)):
            base = Path(directory)
            old_manifest = base / 'old.json'; old_manifest.write_text('{"version":"catalog_115657"}')
            def bundled(relative):
                return old_manifest if relative == 'assets/game_resource_manifest.json' else old_bundled(relative)
            downloads = []
            def download(client, url, destination):
                name = url.rsplit('/', 1)[-1]; downloads.append(name)
                shutil.copyfile(evidence / name, destination)
            def publish(root):
                self.assertFalse((base / 'game-resources/current.json').exists())
                resource_store.activate(root)
                return True
            with patch.object(resource_store, 'bundled', side_effect=bundled):
                resource_sync.synchronize(info=info, download=download, publish=publish)
                self.assertEqual(len(downloads), 3)
                self.assertEqual(metadata()['version'], 'ark2-115942')
                resource_store._roots.pop(str(base / 'game-resources'))
                self.assertEqual(resource_store.active_root().name, 'catalog_115942')
                self.assertFalse(resource_sync.synchronize(info=info, download=download))
                self.assertEqual(len(downloads), 3, 'Already-current resources must not be downloaded again')
                pointer = (base / 'game-resources/current.json').read_bytes()
                bad_info = {**info, 'NewCatalogName': 'catalog_115943'}
                with self.assertRaises(ValueError):
                    resource_sync.synchronize(info=bad_info,
                        download=lambda client, url, destination: destination.write_text('{}'))
                self.assertEqual((base / 'game-resources/current.json').read_bytes(), pointer)
                self.assertEqual(metadata()['version'], 'ark2-115942')
        metadata.cache_clear(); abyss_team.catalog.cache_clear(); stats._TABLES = None


if __name__ == '__main__':
    unittest.main()
