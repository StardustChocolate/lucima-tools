"""The native attendance result carries separate personal and guild quest data."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from backend.game_client import GameClient
from backend.guild import GuildService
from backend.guild_quests import GuildWeeklyService
from tools.test_guild_weekly import WeeklyClient, quest
from tools.test_guild_aid import NOW


class CheckInProgress(unittest.TestCase):
    def setup_client(self):
        client=WeeklyClient()
        client.own['LastCheckInTime']={'$date':NOW-86_400_000}
        client.account_state['PlayerGuildInfo']=deepcopy(client.own)
        client.account_state['GuildData']=client.full()
        client.personal[0]=quest('GuildCheckIn',0,5)
        client.account_state['QuestContainer']['Quests']=deepcopy(client.personal)
        client.quest_update_callback=Mock()
        return client

    def test_native_result_updates_attendance_and_both_quest_lists_once(self):
        for container in (False,True):
            with self.subTest(container=container):
                c=self.setup_client();original=c.call
                def call(route,data=None):
                    result=original(route,data)
                    if route=='GuildHandler.GuildMemberCheckIn':
                        c.personal[0]['Count']=1
                        result['PlayerGuildQuestList']={'Quests':deepcopy(c.personal)} if container else deepcopy(c.personal)
                        result['GuildQuestList']=c.tier_rows()
                    return result
                c.call=call
                service=GuildService(c,{})
                self.assertTrue(service.checkin_if_due())
                status=GuildWeeklyService(service).snapshot()
                row=next(r for r in status['personal'] if r['id']=='GuildCheckIn')
                self.assertEqual(row['count'],1)
                self.assertTrue(status['personalKnown'])
                self.assertEqual(status['score'],450)
                self.assertFalse(service.checkin_if_due())
                self.assertEqual(len(c.routes('GuildMemberCheckIn')),1)
                c.quest_update_callback.assert_called_once()

    def test_partial_week_response_does_not_confirm_stale_other_personal_tasks(self):
        c=self.setup_client();runtime={'weeklyPersonalWeek':c.week-1}
        service=GuildService(c,runtime)
        service._cache({'PlayerGuildQuestList':[quest('GuildCheckIn',1,5)]})
        self.assertFalse(GuildWeeklyService(service).snapshot()['personalKnown'])
        self.assertEqual(next(q for q in c.account_state['QuestContainer']['Quests'] if q['StaticID']=='GuildCheckIn')['Count'],1)

    def test_other_guild_or_week_cannot_replace_public_quest_progress(self):
        c=self.setup_client();service=GuildService(c,{})
        old=deepcopy(c.account_state['GuildData']['GuildQuestList'])
        rows=c.tier_rows();rows[0]['GID']='other-guild';rows[1]['Week']=c.week-1
        rows[0]['QuestData']['Count']=99999;rows[1]['QuestData']['Count']=99999
        service._cache({'GuildQuestList':rows[:2]})
        self.assertEqual(c.account_state['GuildData']['GuildQuestList'],old)

    def test_missing_personal_result_never_invents_one_attendance(self):
        c=self.setup_client();service=GuildService(c,{'weeklyPersonalWeek':c.week})
        self.assertTrue(service.checkin_if_due())
        row=next(r for r in GuildWeeklyService(service).snapshot()['personal'] if r['id']=='GuildCheckIn')
        self.assertEqual(row['count'],0)


if __name__=='__main__':unittest.main()
