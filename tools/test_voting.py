"""Replay sanitized vote captures; no account credentials or network requests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import accounts, config, runtime_data, tasks, voting
from backend.game_client import GameClient, GameMessage, TaskDeferred

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tools/fixtures/voting.json').read_text(encoding='utf-8'))
NOW = int(FIXTURE['state']['Utc'])
TARGET = {'targets': {'Vote03': 'H149'}}


class ReplayClient(GameClient):
    def __init__(self):
        self.aid, self.session_id = 'test-aid', 'test-session'
        self.account_state = copy.deepcopy(FIXTURE['state'])
        self.calls, self.responses = [], copy.deepcopy(FIXTURE['responses'])

    def call(self, route, data=None):
        self.calls.append((route, copy.deepcopy(data)))
        key = data['RewardQuestInfos'][0]['ID'] if route == 'QuestHandler.RewardQuest' else 'vote'
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        result = copy.deepcopy(value)
        self._apply_now_items(result)
        self._apply_quest_updates(result)
        return result


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(self.folder.name)))
        self.enterContext(patch.object(voting.time, 'time', return_value=NOW))
        self.enterContext(patch.object(config, 'get_request_delay_config', return_value={'enabled': False}))
        self.client, self.runtime = ReplayClient(), {}

    def run_vote(self, params=TARGET, on_partial=None):
        return voting.task(self.client, params, self.runtime, on_partial)

    def test_capture_claims_then_votes_then_claims_diamonds_and_never_repeats(self):
        result = self.run_vote()
        self.assertTrue(result['ok'], result)
        self.assertEqual([data['RewardQuestInfos'][0]['ID'] if 'RewardQuestInfos' in data else 'vote'
                          for _, data in self.client.calls],
                         ['Vote03Online', 'Vote03UpgradeEquip', 'Vote03Arena', 'Vote03Scene', 'vote', 'Vore03Finish'])
        vote = self.client.calls[4]
        self.assertEqual(vote[0], 'VoteHandler.VoteRole')
        self.assertEqual({key: vote[1][key] for key in FIXTURE['voteRequest']}, FIXTURE['voteRequest'])
        self.assertIn('10票', result['detail'])
        self.assertIn('50钻石', result['detail'])
        self.assertEqual(voting._balance(self.client.account_state, 'T03'), 0)
        count = len(self.client.calls)
        self.assertTrue(self.run_vote()['skipped'])
        self.assertEqual(len(self.client.calls), count)

    def test_no_target_still_claims_tickets_without_spending_them(self):
        result = self.run_vote({})
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.client.calls), 4)
        self.assertEqual(voting._balance(self.client.account_state, 'T03'), 10)
        state = voting.status(self.client.account_state, {}, self.runtime)
        self.assertFalse(state['ready'])
        self.assertEqual(state['note'], '暂不投票')

    def test_partial_progress_claims_only_confirmed_ready_quests(self):
        for q in self.client.account_state['QuestContainer']['Quests']:
            if q['StaticID'] != 'Vote03Online':
                q.update(IsFinish=False, Count=0)
        self.run_vote({})
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(voting._balance(self.client.account_state, 'T03'), 2)

    def test_candidates_come_from_official_full_list_and_old_activities_do_not_inherit_target(self):
        state = voting.status(self.client.account_state, TARGET, candidates=True)
        self.assertEqual(len(state['activities']), 1)
        activity = state['activities'][0]
        self.assertEqual(len(activity['candidates']), 117)
        ruby = next(c for c in activity['candidates'] if c['id'] == 'H149')
        self.assertEqual((ruby['element'], ruby['job']), ('Ice', 'Defender'))
        self.assertTrue(activity['targetValid'])
        self.assertEqual(voting.status(self.client.account_state, {'targets': {'Vote02': 'H149'}})
                         ['activities'][0]['target'], '')

    def test_invalid_target_keeps_tickets_and_does_not_pick_a_replacement(self):
        result = self.run_vote({'targets': {'Vote03': 'H806'}})
        self.assertTrue(result['ok'])
        self.assertTrue(all(route == 'QuestHandler.RewardQuest' for route, _ in self.client.calls))
        self.assertIn('失效', voting.status(self.client.account_state, {'targets': {'Vote03': 'H806'}})['note'])

    def test_vote_timeout_blocks_only_this_activity_until_login_reset(self):
        self.client.responses['vote'] = TimeoutError('timeout')
        result = self.run_vote()
        self.assertFalse(result['ok'])
        self.assertIn('等待登录同步刷新', result['detail'])
        state = voting.status(self.client.account_state, TARGET, self.runtime)
        self.assertFalse(state['ready'])
        self.assertEqual(state['note'], '等待登录同步刷新')
        count = len(self.client.calls)
        self.run_vote()
        self.assertEqual(len(self.client.calls), count)
        self.runtime.clear()  # successful login replaces both runtime and inventory
        self.client.responses['vote'] = copy.deepcopy(FIXTURE['responses']['vote'])
        self.assertTrue(self.run_vote()['ok'])

    def test_leaderboard_response_without_cost_is_not_proof_of_success(self):
        self.client.responses['vote'] = {'VoteDataList': [{'ActivityID': 'Vote03', 'RoleID': 'H149', 'Value': 0}]}
        result = self.run_vote()
        self.assertFalse(result['ok'])
        self.assertIn('Vote03', self.runtime['waitingForLogin'])
        self.assertFalse(any(data.get('RewardQuestInfos', [{}])[0].get('ID') == 'Vore03Finish'
                             for _, data in self.client.calls))

    def test_unknown_claim_result_does_not_move_on_to_spending(self):
        self.client.responses['Vote03Online'] = {'Results': []}
        self.assertFalse(self.run_vote()['ok'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(voting.status(self.client.account_state, TARGET, self.runtime)['ready'])

    def test_guard_deferral_and_explicit_rejection_do_not_claim_a_success(self):
        self.client.responses['Vote03Online'] = TaskDeferred('maintenance')
        with self.assertRaises(TaskDeferred): self.run_vote()
        self.assertNotIn('Vote03', self.runtime['waitingForLogin'])
        self.client.responses['Vote03Online'] = GameMessage('not ready')
        self.assertFalse(self.run_vote()['ok'])
        self.assertNotIn('Vote03', self.runtime['waitingForLogin'])

    def test_switch_off_after_claim_stops_remaining_requests(self):
        enabled = True
        self.client.vote_enabled = lambda: enabled
        def stop(_):
            nonlocal enabled
            enabled = False
        with self.assertRaises(TaskDeferred): self.run_vote(on_partial=stop)
        self.assertEqual(len(self.client.calls), 1)

    def test_no_activity_requests_after_end_and_next_start_is_scheduled(self):
        settings = self.client.account_state['ActivityDBSettingContainer']['SettingList']
        vote = next(row for row in settings if row['ActivityID'] == 'Vote03')
        vote['EndTime']['$date'] = (NOW - 1) * 1000
        self.assertTrue(self.run_vote()['skipped'])
        self.assertEqual(self.client.calls, [])
        vote['StartTime']['$date'] = (NOW + 3600) * 1000
        vote['EndTime']['$date'] = (NOW + 7200) * 1000
        self.assertEqual(voting.status(self.client.account_state)['nextMs'], (NOW + 3600) * 1000)

    def test_scheduler_receives_events_waits_for_login_and_runs_no_game_queries(self):
        self.enterContext(patch.object(config, 'load_accounts', return_value={}))
        self.enterContext(patch.object(config, 'save_account'))
        acc = accounts.Account('voting-test', self.client, None)
        self.addCleanup(acc._stop.set)
        acc.toggles = {'activity_vote': True}
        acc.params = {'activity_vote': copy.deepcopy(TARGET)}
        self.assertIn('activity_vote', acc._due_ids())
        acc._wake.clear()
        acc._on_inventory_increase({'T03'})
        self.assertTrue(acc._wake.is_set())
        acc._wake.clear()
        acc._on_quest_updates()
        self.assertTrue(acc._wake.is_set())
        self.assertEqual(self.client.calls, [])
        self.enterContext(patch.object(acc, 'daily_state_current', return_value=False))
        self.assertNotIn('activity_vote', acc._due_ids())
        self.assertEqual(acc.run_toggles(['activity_vote'], 'test'), [])

    def test_streams_only_real_actions_and_task_metadata_is_automatic(self):
        lines = []
        result = self.run_vote(on_partial=lines.append)
        self.assertTrue(result['liveLogged'])
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(line['ok'] for line in lines))
        self.assertEqual(tasks.TASKS['activity_vote']['kind'], 'toggle')
        self.assertFalse(tasks.next_claim_times(self.client.account_state, {'activity_vote': TARGET})['activity_vote']['ready'])


if __name__ == '__main__':
    unittest.main()
