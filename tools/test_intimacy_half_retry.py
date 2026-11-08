"""Independent half failure streaks and one/two-main farming with real journals."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend import intimacy, abyss_battle, abyss_loadout
from tools import test_intimacy as fixtures
from backend.tasks import _oid


class HalfRetry(unittest.TestCase):
    setUp = fixtures.RunTests.setUp
    owners = fixtures.RunTests.owners
    run_farm = fixtures.RunTests.run_farm

    def all_full(self, client):
        for row in client.server['LineRoleDataContainer']['RoleList']:
            row['Info']['IntimacyLV'] = 10

    def outfit(self, rid):
        return tuple(frozenset(aid for aid, owner in group.items() if owner == rid) for group in self.owners())

    def test_after_initial_win_twelve_lower_losses_switch_five_times_then_add_second_main(self):
        baseline = self.owners(); calls = []; good_outfit = None
        for role in self.client.server['RoleDataContainer']['Roles']: role['LV'] = 60
        self.client.bootstrap()
        def battle(client, params):
            nonlocal good_outfit
            calls.append(deepcopy(params['waves']))
            if good_outfit is None: good_outfit = self.outfit('r0')
            self.assertEqual(self.outfit('r0'), good_outfit, 'successful upper main keeps exact gear and bond')
            self.assertEqual(params['waves']['upper'][0], 'r0')
            if len(calls) == 1: return {'ok': True, 'settlementConfirmed': True}
            if len(calls) <= 13:
                return {'ok': False, 'halfResults': {'upper': True, 'lower': False}, 'raw': {'Result': 'Lose'}}
            self.all_full(client)
            return {'ok': True, 'settlementConfirmed': True}
        result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 14)
        # Initial main, then exactly five distinct single replacements, two attempts each.
        singles = [calls[index]['lower'][0] for index in (1, 3, 5, 7, 9, 11)]
        self.assertEqual(len(set(singles)), 6)
        for index in (1, 3, 5, 7, 9, 11):
            self.assertEqual(calls[index]['lower'][0], calls[index+1]['lower'][0])
        snapshot = next(row for row in self.snapshots if row['phase'] == 'battle' and row['attempts'] == 14 and 'battle' not in row)
        self.assertEqual(sum(member['carrier'] for member in snapshot['teams']['upper']), 1)
        self.assertEqual(sum(member['carrier'] for member in snapshot['teams']['lower']), 2)
        self.assertEqual(snapshot['carrierSwitches'], {'upper': 0, 'lower': 5})
        self.assertEqual(len({rid for group in calls[-1].values() for rid in group if rid}), 8)
        self.assertEqual(self.owners(), baseline)

    def test_upper_and_lower_have_separate_streaks_and_passed_halves_reset_only_themselves(self):
        outcomes = [
            {'upper': False, 'lower': None},  # upper 1
            {'upper': True, 'lower': False},  # upper reset, lower 1
            {'upper': False, 'lower': None},  # upper 1, lower still 1
            {'upper': False, 'lower': None},  # upper changes once, lower still 1
            {'upper': True, 'lower': False},  # lower changes once
        ]
        calls = []; replacements = []
        def replace(state, carriers, wave, **kwargs):
            replacements.append(wave)
            return ['r2' if wave == 'upper' else 'r3']
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) <= len(outcomes): return {'ok': False, 'halfResults': outcomes[len(calls)-1]}
            self.all_full(client)
            return {'ok': True, 'settlementConfirmed': True}
        with patch.object(intimacy, 'choose_half_carriers', side_effect=replace):
            result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(replacements, ['upper', 'lower'])
        self.assertTrue(all(group['upper'][0] == 'r0' for group in calls[:4]))
        self.assertEqual(calls[4]['upper'][0], 'r2')
        self.assertEqual(calls[4]['lower'][0], 'r1')
        self.assertEqual(calls[5]['lower'][0], 'r3')
        self.assertEqual(result['progress']['carrierSwitches'], {'upper': 1, 'lower': 1})

    def test_transport_and_unknown_outcomes_never_count_as_two_defeats(self):
        calls = []
        def battle(client, params):
            calls.append(deepcopy(params['waves']))
            if len(calls) in (1, 2): raise abyss_battle.BattleUnconfirmed('结算暂未确认')
            if len(calls) in (3, 4): return {'ok': False, 'raw': {'Result': 'Unknown'}}
            self.all_full(client)
            return {'ok': True, 'settlementConfirmed': True}
        with patch.object(intimacy, 'choose_half_carriers') as replace:
            result = self.run_farm(battle)
        self.assertTrue(result['ok'], result)
        replace.assert_not_called()
        self.assertTrue(all(group == calls[0] for group in calls))

    def test_mixed_main_counts_keep_the_other_passenger_batch_and_never_duplicate_heroes(self):
        state = self.client.account_state
        singles = {'upper': 'r0', 'lower': 'r1'}
        prior = intimacy.make_waves(state, singles)
        # Promote a lower passenger to its second main.
        second = prior['lower'][1]
        carriers = {'upper': ['r0'], 'lower': ['r1', second]}
        waves = intimacy.make_waves(state, carriers, prior)
        self.assertEqual(waves['upper'], prior['upper'])
        self.assertEqual(waves['lower'][:2], ['r1', second])
        ids = [rid for team in waves.values() for rid in team if rid]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(waves['lower']), 4)

    def test_both_halves_can_use_two_mains_with_distinct_assets_and_full_restoration(self):
        baseline = self.owners()
        carriers = {'upper': ['r0', 'r2'], 'lower': ['r1', 'r3']}
        waves = intimacy.make_waves(self.client.account_state, carriers)
        loadout = intimacy.plan_loadout(self.client.account_state, carriers)
        self.assertEqual(len(loadout['targets']), 4)
        abyss_loadout.apply(self.client, loadout, account_key='offline')
        active = {'carriers': loadout}
        for wave in intimacy.WAVES:
            plan = intimacy.plan_passengers(self.client.account_state, carriers, waves, wave, active)
            self.assertEqual(len(plan['targets']), 2)
            abyss_loadout.apply(self.client, plan, account_key=intimacy.passenger_key(self.client, 'offline', wave))
            active[wave] = plan
        self.assertEqual(len({aid for plan in active.values() for group in plan['targets'].values() for aid in group}), 48)
        self.assertEqual(len({aid for plan in active.values() for aid in plan['artifactTargets'].values()}), 8)
        self.assertTrue(intimacy.restore_all(self.client, account_key='offline')['restored'])
        self.assertEqual(self.owners(), baseline)


if __name__ == '__main__': unittest.main()
