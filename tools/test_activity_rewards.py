"""Official side-story reward catalogs and confirmed claim handling."""
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from backend import activity_rewards as rewards, runtime_data
from backend.game_client import GameClient, GameMessage


def fixture_catalog():
    activities = [dict(ID='BranchH806', Type='SideStory', Name='T_Activity', IsUnlimitTime='')]
    groups = [dict(ID='BranchH806', Type='Branch', ActivityID='BranchH806'),
              dict(ID='BranchH806_Achievement', Type='BranchAchievement', ActivityID='BranchH806'),
              dict(ID='Branch1', Type='Branch', ActivityID='BranchH001')]
    quests = [dict(ID='BranchH806_1', Group='BranchH806', ActivityID='BranchH806', ActivityIDs='', Drops='2'),
              dict(ID='BranchH806', Group='BranchH806', ActivityID='BranchH806', ActivityIDs='', Drops='2'),
              dict(ID='BranchH806_Achievement_1', Group='BranchH806_Achievement', ActivityID='BranchH806', ActivityIDs='', Drops='2'),
              dict(ID='BranchH806_Achievement_9', Group='BranchH806_Achievement', ActivityID='BranchH806', ActivityIDs='Version_Hell', Drops='2'),
              dict(ID='BranchH806_Achievement_10', Group='Branch1', ActivityID='BranchH806', ActivityIDs='', Drops='2')]
    return rewards.build_catalog(quests, groups, activities, {'T_Activity': 'H806活动'}, 'catalog-test')


class ActivityRewardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(runtime_data, 'registry', runtime_data.Registry(self.temp.name)))
        self.enterContext(patch.object(rewards, 'catalog', return_value=fixture_catalog()))
        self.enterContext(patch.object(rewards, 'game_clock'))
        rewards.game_clock.now_ms.return_value = 1_800_000_000_000
        self.state = {'ActivityDBSettingContainer': {'SettingList': [
            {'ActivityID': 'BranchH806', 'IsIgnoreTime': True},
            {'ActivityID': 'Version_Hell', 'IsIgnoreTime': True}]},
            'QuestContainer': {'Quests': [
                {'StaticID': 'BranchH806_1', 'IsFinish': True, 'IsRewarded': False},
                {'StaticID': 'BranchH806', 'IsFinish': False, 'IsRewarded': False},
                {'StaticID': 'BranchH806_Achievement_1', 'IsFinish': True, 'IsRewarded': False},
                {'StaticID': 'BranchH806_Achievement_9', 'IsFinish': True, 'IsRewarded': False},
                {'StaticID': 'BranchH806_Achievement_10', 'IsFinish': True, 'IsRewarded': False}]}}
        self.client = GameClient.__new__(GameClient)
        self.client.account_state = self.state
        self.client._auth_data = lambda payload: payload

    def respond(self, route, payload):
        updates = []
        for entry in payload['RewardQuestInfos']:
            sid = entry['ID']
            row = next(q for q in self.state['QuestContainer']['Quests'] if q['StaticID'] == sid)
            row['IsRewarded'] = True
            updates.append(deepcopy(row))
        return {'Results': [{'Quests': updates, 'Drop': {'Items': [
            {'Item': {'StaticID': '2', 'Count': len(updates) * 50}}]}}]}

    def test_catalog_uses_groups_and_does_not_include_misgrouped_event_quest(self):
        event = fixture_catalog()['activities']['BranchH806']
        self.assertEqual([q['id'] for q in event['story']], ['BranchH806_1', 'BranchH806'])
        self.assertEqual([q['id'] for q in event['achievement']], [
            'BranchH806_Achievement_1', 'BranchH806_Achievement_9'])

    def test_claims_chapter_then_newly_unlocked_story_total(self):
        calls = []
        def claim(route, payload):
            calls.append([row['ID'] for row in payload['RewardQuestInfos']])
            result = self.respond(route, payload)
            if calls[-1] == ['BranchH806_1']:
                self.state['QuestContainer']['Quests'][1]['IsFinish'] = True
            return result
        self.client.call = claim
        result = rewards.claim(self.client, 'story')
        self.assertEqual(calls, [['BranchH806_1'], ['BranchH806']])
        self.assertTrue(result['ok'])
        self.assertEqual(result['claimedIds'], ['BranchH806_1', 'BranchH806'])
        self.assertEqual(result['items'], {'2': 100})

    def test_claims_only_unlocked_achievements_and_checks_server_confirmation(self):
        self.client.call = self.respond
        result = rewards.claim(self.client, 'achievement')
        self.assertTrue(result['ok'])
        self.assertEqual(result['claimedIds'], ['BranchH806_Achievement_1', 'BranchH806_Achievement_9'])
        self.assertEqual(self.state['QuestContainer']['Quests'][-1]['IsRewarded'], False)

    def test_rejects_partial_response_and_does_not_repeat_claims(self):
        self.client.call = lambda route, payload: {'Results': []}
        result = rewards.claim(self.client, 'achievement')
        self.assertFalse(result['ok'])
        self.assertIn('未确认', result['detail'])

    def test_server_error_is_reported_without_retry(self):
        self.client.call = lambda *args: (_ for _ in ()).throw(GameMessage('failed'))
        result = rewards.claim(self.client, 'achievement')
        self.assertFalse(result['ok'])
        self.assertIn('failed', result['detail'])


if __name__ == '__main__':
    unittest.main()
