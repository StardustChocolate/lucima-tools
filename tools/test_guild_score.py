"""Offline checks for explicitly started, score-limited weekly reward jobs."""
import copy
import threading
import time
import unittest
from unittest.mock import patch

from tools.test_guild_weekly import WeeklyClient
from backend import accounts, appcore, config, server
from backend.game_client import GameError, GameMessage
from backend.guild import GuildService
from backend.guild_jobs import GuildJobManager
from backend.guild_quests import GuildWeeklyService, weekly_catalog


class ScoreClient(WeeklyClient):
    def __init__(self):
        super().__init__()
        self.score = 2725
        self.gain = None
        self.claim_error = None
        self.omit_delta = False
        self.minimal_repeat_reply = False

    def call(self, route, data=None):
        if route != 'QuestHandler.RewardQuest':
            return super().call(route, data)
        self.calls.append((route, copy.deepcopy(data)))
        identifier = data['RewardQuestInfos'][0]['ID']
        current = next(row for row in self.personal if row['StaticID'] == identifier)
        assert current['IsFinish']  # Repeated rewards intentionally allow IsRewarded.
        repeated = current['IsRewarded']
        current['IsRewarded'] = True
        gain = weekly_catalog()['personal'][identifier]['points'] if self.gain is None else self.gain
        self.score += gain
        if self.claim_error:
            raise self.claim_error
        response = {'Results': [{'Quests': [copy.deepcopy(current)]}], 'Utc': str(self.now // 1000)}
        if not self.omit_delta:
            response['Results'][0].update(GuildQuestDataList=self.tier_rows(), GuildDropResult={'Items': [
                {'Item': {'StaticID': '82', 'Count': gain}, 'NowItem': {'StaticID': '82', 'Count': 999999}}]})
        if self.minimal_repeat_reply and repeated:
            # Actual repeated replies omit Results and contain a default member.
            response = {'PlayerGuildInfo': {'NowGuildQuestWeek': 0}, 'Utc': str(self.now // 1000)}
        self._apply_quest_updates(response)
        if self.after_claim:
            self.after_claim(route)
        return response


class GuildScoreTests(unittest.TestCase):
    def setUp(self):
        patch.object(config, 'load_accounts', return_value={}).start()
        patch.object(config, 'save_account').start()
        patch('httpx.Client.request', side_effect=AssertionError('No live requests')).start()
        self.addCleanup(patch.stopall)
        self.client = ScoreClient()
        self.acc = accounts.Account('score@example.invalid', self.client, None)
        self.registry = {self.acc.email: self.acc}
        self.manager = GuildJobManager(account_lookup=self.registry.get, interval=0)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.manager.cancel(self.acc.email)
        self.wait_finished()

    def wait_finished(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.manager.status(self.acc.email)
            if not job['running'] and self.acc.email not in self.manager._active:
                return job
            time.sleep(.005)
        self.fail('score worker did not stop')

    def start(self):
        self.manager.start_score(self.acc)
        return self.wait_finished()

    def claims(self):
        return [body['RewardQuestInfos'][0]['ID'] for body in self.client.routes('RewardQuest')]

    def test_highest_point_finished_task_repeats_planned_times_then_verifies_score(self):
        result = self.start()
        self.assertEqual((result['state'], result['score'], result['target']), ('completed', 2800, 2800))
        self.assertEqual(self.claims(), ['GuildGetRing'] * 3)
        self.assertEqual(result['completed'], 3)
        self.assertEqual((result['planned'], result['pointsPerClaim']), (3, 25))
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)
        self.assertEqual(len(self.client.routes('RewardGuildQuest')), 0)
        self.assertEqual(self.acc.guild_snapshot()['weekly']['score'], 2800)
        self.assertNotIn('weeklyScoreJob', self.acc.guild_runtime)
        self.assertTrue(self.acc.logs_since(0)[-1]['result']['ok'])

    def test_fractional_last_claim_rounds_up_once(self):
        self.client.score = 2799
        for row in self.client.personal:
            if row['IsFinish']:
                row['IsRewarded'] = True
        self.client.account_state['QuestContainer']['Quests'] = copy.deepcopy(self.client.personal)
        result = self.start()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(self.claims(), ['GuildGetRing'])
        self.assertEqual((result['planned'], result['score']), (1, 2824))

    def test_twenty_point_task_is_used_when_no_completed_twenty_five_point_task(self):
        self.client.ids = ['GuildDonatedCourage']
        result = self.start()
        self.assertEqual((result['state'], result['score']), ('completed', 2805))
        self.assertEqual((result['planned'], result['pointsPerClaim']), (4, 20))
        self.assertEqual(self.claims(), ['GuildDonatedCourage'] * 4)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_target_already_met_no_reward_requests(self):
        self.client.score = 3000
        self.assertEqual(self.start()['state'], 'completed')
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 1)

    def test_no_completed_or_unconfirmed_personal_quests_never_claim(self):
        for known in (True, False):
            with self.subTest(known=known):
                for row in self.client.personal:
                    row.update(Count=0, IsFinish=False)
                self.client.account_state['QuestContainer']['Quests'] = copy.deepcopy(self.client.personal)
                if not known:
                    self.acc.guild_runtime['weeklyPersonalWeek'] = 665
                result = self.start()
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(self.claims(), [])

    def test_no_gain_stops_after_planned_batch_and_final_verification_without_extra_claims(self):
        self.client.gain = 0
        result = self.start()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('尚未达到目标', result['message'])
        self.assertEqual(len(self.claims()), 3)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_missing_delta_is_confirmed_by_query_and_reaches_target(self):
        self.client.score = 2775
        self.client.omit_delta = True
        result = self.start()
        self.assertEqual((result['state'], result['score']), ('completed', 2800))
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_two_minimal_repeat_replies_need_only_one_final_query(self):
        self.client.minimal_repeat_reply = True
        self.client.ids = ['GuildGetRing']
        result = self.start()
        self.assertEqual((result['state'], result['score'], result['completed']), ('completed', 2800, 3))
        self.assertEqual(self.claims(), ['GuildGetRing'] * 3)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)
        self.assertEqual(self.acc.guild_snapshot()['weekly']['score'], 2800)
        self.assertEqual(self.client.account_state['GuildData']['Record']['WeeklyAssistanceScore'], 80)

    def test_large_batch_from_seventy_five_uses_two_queries_without_fabricating_progress(self):
        self.client.score = 75
        self.client.minimal_repeat_reply = True
        self.client.ids = ['GuildGetRing']
        observed = []
        self.client.after_claim = lambda _: observed.append(self.acc.guild_snapshot()['weekly']['score'])
        result = self.start()
        self.assertEqual((result['state'], result['score'], result['planned']), ('completed', 2800, 109))
        self.assertEqual(len(self.claims()), 109)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)
        self.assertEqual(observed, [75] + [100] * 108)

    def test_incorrect_config_gain_fails_final_verification_instead_of_extending_batch(self):
        self.client.gain = 20
        self.client.minimal_repeat_reply = True
        result = self.start()
        self.assertEqual((result['state'], result['score'], result['completed']), ('failed', 2785, 3))
        self.assertIn('尚未达到目标', result['message'])
        self.assertEqual(len(self.claims()), 3)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_confirmed_early_target_stops_remainder_of_planned_batch(self):
        self.client.gain = 50
        result = self.start()
        self.assertEqual((result['state'], result['planned'], result['completed']), ('completed', 3, 2))
        self.assertEqual(len(self.claims()), 2)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_final_query_failure_is_not_reported_as_completed(self):
        def fail_final_query(_):
            if len(self.claims()) == 3:
                self.client.fail_query = True
        self.client.after_claim = fail_final_query
        result = self.start()
        self.assertEqual((result['state'], result['completed']), ('failed', 3))
        self.assertEqual(len(self.claims()), 3)
        self.assertEqual(len(self.client.routes('QueryGuildDataForSync')), 2)

    def test_missing_fixed_points_does_not_guess_or_claim(self):
        catalog = copy.deepcopy(weekly_catalog())
        for row in catalog['personal'].values():
            row.pop('points', None)
        with patch('backend.guild_jobs.weekly_catalog', return_value=catalog):
            result = self.start()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('固定积分配置', result['message'])
        self.assertEqual(self.claims(), [])

    def test_business_rejection_and_lost_reply_stop_without_automatic_retries(self):
        for error in (GameMessage('OverCount'), GameError('timeout after commit')):
            with self.subTest(error=error):
                self.client.claim_error = error
                self.acc.guild_runtime.pop('weeklyUncertain', None)
                result = self.start()
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['attempted'], 1)
                self.assertIn('GuildGetRing', self.acc.guild_runtime['weeklyUncertain'])
        self.assertEqual(len(self.claims()), 2)

    def test_no_guild_or_disconnected_never_queries_or_claims(self):
        self.client.account_state['PlayerGuildInfo']['GID'] = ''
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual(self.client.calls, [])
        self.client.account_state['PlayerGuildInfo']['GID'] = 'guild'
        self.client.connected = False
        self.assertEqual(self.start()['state'], 'failed')
        self.assertEqual(self.client.calls, [])

    def test_cancel_waiting_for_account_sends_no_query_and_duplicate_request_is_deduplicated(self):
        request_id = 'c8190496-e4ee-4843-9708-55951302fbd6'
        self.acc.lock.acquire()
        try:
            first = self.manager.start_score(self.acc, requestId=request_id)
            second = self.manager.start_score(self.acc, requestId=request_id)
            self.assertEqual(first['id'], second['id'])
            with self.assertRaises(GameError):
                self.manager.start_score(self.acc)
            self.manager.cancel(self.acc.email, first['id'])
        finally:
            self.acc.lock.release()
        self.assertEqual(self.wait_finished()['state'], 'cancelled')
        self.assertEqual(self.client.calls, [])

    def test_cancel_during_reply_stops_next_claim_and_automatic_weekly_is_suspended(self):
        def after_claim(_):
            weekly = GuildWeeklyService(GuildService(self.client, self.acc.guild_runtime))
            self.assertIsNone(weekly.next_delay())
            self.acc.toggles['guild_auto_weekly'] = True
            self.acc._weekly_guild_locked()
            self.manager.cancel(self.acc.email)
        self.client.after_claim = after_claim
        result = self.start()
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['completed'], 1)
        self.assertEqual(len(self.claims()), 1)
        self.assertNotIn('weeklyScoreJob', self.acc.guild_runtime)

    def test_relogin_guild_change_or_week_change_stops_next_claim(self):
        for change in ('relogin', 'guild', 'week'):
            with self.subTest(change=change):
                self.client = ScoreClient()
                self.acc.client = self.client
                self.acc.guild_runtime.clear()
                self.acc.guild_runtime['weeklyPersonalWeek'] = self.client.week
                def after_claim(_):
                    if change == 'relogin':
                        self.acc.state_revision += 1
                        self.acc.guild_runtime.clear()
                    elif change == 'guild':
                        self.client.account_state['PlayerGuildInfo']['GID'] = 'another-guild'
                    else:
                        self.client.account_state['GuildData']['Record']['LastResetQuestWeek'] += 1
                self.client.after_claim = after_claim
                result = self.start()
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(len(self.claims()), 1)

    def test_score_mode_is_routed_and_status_includes_cached_guild(self):
        with patch.object(accounts, 'get', return_value=self.acc), \
                patch.object(appcore, 'connection_status', return_value={'connected': True}), \
                patch.object(appcore, 'guild_jobs', self.manager):
            job = server._route_api('POST', '/api/guild/quests/start',
                                    {'account': self.acc.email, 'mode': 'score'}, {})
            self.wait_finished()
            status = appcore.guild_quests_status(self.acc.email, job['id'])
            self.assertEqual(status['mode'], 'score')
            self.assertEqual(status['guild']['weekly']['score'], 2800)


if __name__ == '__main__':
    unittest.main()
