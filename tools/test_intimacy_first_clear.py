"""Floor-one prerequisite uses normal Abyss rules before any friendship party."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import abyss_battle, abyss_loadout, abyss_team, config, intimacy
from tools import test_intimacy as fixtures


class FirstClear(unittest.TestCase):
    setUp = fixtures.RunTests.setUp
    owners = fixtures.RunTests.owners

    def unopened_record(self):
        self.client.server['SceneDataContainer']['Scenes'] = [
            {'StaticID': '1Ext_3_10', 'Stars': [1, 0, 0]}]
        for role in self.client.server['RoleDataContainer']['Roles']:
            role['LV'] = 60
        self.client.bootstrap()

    def clear(self):
        self.client.server['SceneDataContainer']['Scenes'] = [
            {'StaticID': intimacy.SCENE_ID, 'Stars': [1, 0, 0, 0], 'PassCount': 1}]
        self.client.bootstrap()

    def all_full(self):
        for row in self.client.server['LineRoleDataContainer']['RoleList']:
            row['Info']['IntimacyLV'] = 10

    def run_farm(self, battle, *, real_teams=False):
        baseline = self.owners()

        def carriers(state, **kwargs):
            self.assertTrue(next(s for s in state['SceneDataContainer']['Scenes']
                                 if s['StaticID'] == intimacy.SCENE_ID)['Stars'][0])
            self.assertEqual(self.owners(), baseline, 'first-clear loans return before farming')
            return {'upper': 'r0', 'lower': 'r1'}

        team = {'waves': {'upper': ['r0', 'r1', 'r2', 'r3'], 'lower': ['r4', 'r5', 'r6', 'r7']},
                'reasons': {}, 'utilityRoles': {}, 'warnings': []}
        with patch.object(intimacy, 'choose_carriers', side_effect=carriers) as choose_carriers, \
             patch.object(abyss_team, 'choose_teams', **({'wraps': abyss_team.choose_teams} if real_teams else {'return_value': team})) as choose_teams, \
             patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle) as fight, \
             patch.object(config, 'get_request_delay_config', return_value={'enabled': False}), \
             patch.object(intimacy, 'RETRY_BASE_SECONDS', 0), \
             patch.object(intimacy.abyss_progression.time, 'sleep'):
            result = intimacy.run(self.client, self.params)
        return result, choose_carriers, choose_teams, fight

    def test_missing_record_runs_real_eight_person_rules_then_farms_after_one_star(self):
        self.unopened_record()
        baseline = self.owners()
        calls = []

        def battle(client, params):
            calls.append({key: deepcopy(value) for key, value in params.items() if not key.startswith('_')})
            self.assertEqual(params['sceneId'], intimacy.SCENE_ID)
            self.assertTrue(params['useSoul'])
            self.assertTrue(params['prioritizeSoulBreak'])
            if len(calls) == 1:
                ids = [rid for wave in params['waves'].values() for rid in wave if rid]
                self.assertEqual(len(set(ids)), 8)
                self.assertTrue(abyss_loadout.pending(client, 'offline'))
                self.clear()
            else:
                self.assertNotIn('_on_progress', params, 'farming keeps its existing settlement-only display')
                self.all_full()
            return {'ok': True, 'settlementConfirmed': True}

        result, carriers, teams, _ = self.run_farm(battle, real_teams=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(teams.call_count, 1)
        self.assertEqual(carriers.call_count, 1)
        self.assertEqual(result['firstClearAttempts'], 1)
        self.assertEqual(result['rounds'], 1)
        self.assertEqual(result['attempts'], 1)
        self.assertTrue(any(row['phase'] == 'first_clear' and row['teams']['upper'] for row in self.snapshots))
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_existing_zero_star_record_retries_rule_teams_then_continues_farming(self):
        self.unopened_record()
        self.client.server['SceneDataContainer']['Scenes'] = [
            {'StaticID': intimacy.SCENE_ID, 'Stars': [0, 0, 0, 0]}]
        calls = []

        def battle(client, params):
            calls.append({key: deepcopy(value) for key, value in params.items() if not key.startswith('_')})
            if len(calls) == 1:
                return {'ok': False, 'settlementConfirmed': True}
            if len(calls) == 2:
                self.clear()
            else:
                self.all_full()
            return {'ok': True, 'settlementConfirmed': True}

        result, carriers, teams, _ = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(teams.call_count, 2)
        self.assertEqual(carriers.call_count, 1)
        self.assertEqual(result['firstClearAttempts'], 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(teams.call_args.kwargs['previous'], [f'r{i}' for i in range(8)])

    def test_five_first_clear_losses_stop_without_farming_and_restore_assets(self):
        self.unopened_record()
        baseline = self.owners()
        result, carriers, teams, fight = self.run_farm(lambda *args: {'ok': False, 'settlementConfirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('首通未完成', result['detail'])
        self.assertEqual(result['firstClearAttempts'], 5)
        self.assertEqual(result['rounds'], 0)
        self.assertEqual(teams.call_count, 5)
        self.assertEqual(fight.call_count, 5)
        carriers.assert_not_called()
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_disconnect_with_fresh_server_clear_continues_without_replaying_first_clear(self):
        self.unopened_record()
        calls = []

        def battle(client, params):
            calls.append(params)
            if len(calls) == 1:
                self.clear()
                raise abyss_battle.BattleUnconfirmed('首通连接中断')
            self.all_full()
            return {'ok': True, 'settlementConfirmed': True}

        result, carriers, teams, fight = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(teams.call_count, 1)
        self.assertEqual(carriers.call_count, 1)
        self.assertEqual(fight.call_count, 2)

    def test_unconfirmed_server_clear_never_starts_farming(self):
        self.unopened_record()
        baseline = self.owners()
        result, carriers, _, fight = self.run_farm(lambda *args: {'ok': True, 'settlementConfirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('服务器通关确认', result['detail'])
        self.assertEqual(fight.call_count, 1)
        carriers.assert_not_called()
        self.assertEqual(self.owners(), baseline)

    def test_cancel_in_first_clear_battle_restores_and_does_not_farm(self):
        self.unopened_record()
        baseline = self.owners()

        def battle(client, params):
            self.assertIs(params['_cancel_event'], self.cancel)
            self.cancel.set()
            raise intimacy.GameError('挑战已中止')

        result, carriers, _, fight = self.run_farm(battle)
        self.assertFalse(result['ok'])
        self.assertTrue(result['cancelled'])
        self.assertEqual(fight.call_count, 1)
        carriers.assert_not_called()
        self.assertEqual(self.owners(), baseline)
        self.assertFalse(intimacy.recovery_pending(self.client, 'offline'))

    def test_prior_one_star_clear_goes_directly_to_farming(self):
        self.clear()

        def battle(*args):
            self.all_full()
            return {'ok': True, 'settlementConfirmed': True}

        result, carriers, teams, fight = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['firstClearAttempts'], 0)
        teams.assert_not_called()
        self.assertEqual(carriers.call_count, 1)
        self.assertEqual(fight.call_count, 1)

    def test_all_friendship_full_skips_first_clear_and_all_borrowing(self):
        self.unopened_record()
        self.all_full()
        result, carriers, teams, fight = self.run_farm(lambda *args: self.fail('no battle needed'))
        self.assertTrue(result['ok'], result)
        carriers.assert_not_called()
        teams.assert_not_called()
        fight.assert_not_called()
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
