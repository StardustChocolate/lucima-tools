"""Exact medical slots from official Abyss effects, including main priorities."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from backend import abyss_team as team
from backend.abyss_state import metadata
from backend.game_client import GameError
from backend.tasks import _oid
from tools.test_abyss_auto import roster


def wave(*effects):
    return {'Monsters':[], 'Buffs':{'Bs':[{'B':sid,'Target':'Self'} for sid in effects]}}


class MedicalSlots(unittest.TestCase):
    def choose(self,upper,lower,*,state=None):
        state=state or roster();before=deepcopy(state)
        with patch.object(team,'fantasy_config',return_value={'Waves':[upper,lower]}):
            result=team.choose_teams(state,'MysteriousRealm_3')
        self.assertEqual(state,before)
        jobs={_oid(r['_id']):metadata()['tables']['Role'][r['StaticID']]['Job'] for r in state['RoleDataContainer']['Roles']}
        counts=[sum(jobs[rid]=='Medic' for rid in result['waves'][key]) for key in ('upper','lower')]
        self.assertEqual(len(set(sum(result['waves'].values(),[]))),8)
        return counts,result

    def test_official_presence_and_turn_effects_require_only_one_provider(self):
        for effects in [('FG_Job_003',),('FG_Job_045',),('FG_Job_065',),('FG_Job_003','FG_Job_045')]:
            with self.subTest(effects=effects):
                self.assertEqual(team.wave_policy(wave(*effects))['medicCount'],1)

    def test_official_two_medics_repeated_stat_conditions_are_not_added(self):
        self.assertEqual(team.wave_policy(wave('FG_Job_057','FG_Job_003','FG_Job_045'))['medicCount'],2)

    def test_no_requirement_selects_no_medics_in_either_half(self):
        counts,_=self.choose(wave(),wave());self.assertEqual(counts,[0,0])

    def test_screenshot_stage_has_one_medic_upper_and_none_lower(self):
        counts,result=self.choose(wave('FG_Job_003','FG_Job_045'),wave('FG_Job_004','FG_Job_016'))
        self.assertEqual(counts,[1,0])
        self.assertEqual(result['reasons']['upper']['medicCount'],{'required':1,'selected':1})

    def test_two_and_one_are_exact_and_halves_do_not_share_roles(self):
        counts,_=self.choose(wave('FG_Job_057'),wave('FG_Job_045'));self.assertEqual(counts,[2,1])

    def test_both_halves_can_require_two(self):
        counts,_=self.choose(wave('FG_Job_057'),wave('FG_Job_057'));self.assertEqual(counts,[2,2])

    def test_priority_mains_cannot_bypass_medical_count(self):
        state=roster();state['RoleDataContainer']['Roles'] += [{'_id':'main-a','StaticID':'H165','LV':60,'Star':5},
                                                              {'_id':'main-b','StaticID':'H146','LV':60,'Star':5}]
        counts,result=self.choose(wave('FG_Job_057'),wave('FG_Job_045'),state=state)
        self.assertEqual(counts,[2,1])
        self.assertTrue(any(r['preferredMains'] for r in result['reasons'].values()))

    def test_insufficient_medics_stops_instead_of_violating_count(self):
        state=roster();state['RoleDataContainer']['Roles']=[r for r in state['RoleDataContainer']['Roles']
            if metadata()['tables']['Role'][r['StaticID']]['Job']!='Medic']
        with self.assertRaisesRegex(GameError,'需要恰好 2 名医疗'):
            self.choose(wave('FG_Job_057'),wave(),state=state)


if __name__=='__main__':unittest.main()
