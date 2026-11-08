"""Offline weekly reward, reset, timeout and shared scheduler regressions."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.test_guild_aid import AidClient, NOW, accounts, config
from backend.game_client import GameError
from backend.guild import GuildService, has_guild
from backend.guild_quests import DAY_MS, WEEKLY_POLL_MS, GuildWeeklyService, weekly_catalog
from tools import build_guild_quests


def quest(identifier, count, target, rewarded=False):
    return {"StaticID": identifier, "Count": count, "IsFinish": count >= target, "IsRewarded": rewarded}


class WeeklyClient(AidClient):
    def __init__(self):
        super().__init__()
        self.week, self.score = 666, 450
        self.ids = ["GuildCheckIn", "GuildDonatedCourage", "GuildGetRing"]
        self.own.update(NowGuildQuestWeek=self.week, PlayerGuildQuestSave={"LastRewardQuestWeek": self.week})
        self.personal = [quest("GuildCheckIn", 3, 5), quest("GuildDonatedCourage", 12, 10),
                         quest("GuildGetRing", 10, 10), quest("GuildGetAmulet", 10, 10)]
        self.account_state.update(PlayerGuildInfo=copy.deepcopy(self.own),
                                  QuestContainer={"Quests": copy.deepcopy(self.personal)})
        self.fail_personal = self.fail_tiers = self.bad_personal = self.bad_tiers = False
        self.after_claim = None

    def tier_rows(self):
        return [{"StaticID": identifier, "GID": "guild", "Week": self.week,
                 "QuestData": quest(identifier, self.score, meta["target"])}
                for identifier, meta in weekly_catalog()["tiers"].items()]

    def full(self):
        data = super().full()
        data["Record"].update(LastResetQuestWeek=self.week, WeeklyAssistanceScore=80,
                              GuildQuestIDList=copy.deepcopy(self.ids))
        data["GuildQuestList"] = self.tier_rows()
        return data

    def call(self, route, data=None):
        if route not in ("QuestHandler.RewardQuest", "QuestHandler.RewardGuildQuest"):
            return super().call(route, data)
        self.calls.append((route, copy.deepcopy(data)))
        if route == "QuestHandler.RewardQuest":
            identifier = data["RewardQuestInfos"][0]["ID"]
            current = next(row for row in self.personal if row["StaticID"] == identifier)
            assert current["IsFinish"] and not current["IsRewarded"]
            current["IsRewarded"] = True
            self.score += 25
            if self.fail_personal:
                self.fail_personal = False
                raise GameError("personal timed out after commit")
            result = {"Results": [{"Quests": [copy.deepcopy(current)], "GuildQuestDataList": self.tier_rows(),
                                   "GuildDropResult": {"Items": [{"Item": {"StaticID": "82", "Count": 25},
                                                                  "NowItem": {"StaticID": "82", "Count": 628575}}]}}],
                      # Actual personal reward replies contain this default object.
                      "PlayerGuildInfo": {"NowGuildQuestWeek": 665, "DayDonateGoldCount": 0}}
            if self.bad_personal:
                result = {"Results": []}
        else:
            saved = self.own["PlayerGuildQuestSave"]
            saved.update(LastRewardQuestWeek=self.week, RewardedGuildQuestList=[
                identifier for identifier, meta in weekly_catalog()["tiers"].items() if self.score >= meta["target"]])
            if self.fail_tiers:
                self.fail_tiers = False
                raise GameError("tiers timed out after commit")
            result = {"GuildQuestDataList": self.tier_rows(), "PlayerGuildInfo": copy.deepcopy(self.own),
                      "Results": [{} for _ in saved["RewardedGuildQuestList"]]}
            if self.bad_tiers:
                result = {"Results": []}
        result["Utc"] = str(self.now // 1000)
        self._apply_quest_updates(result)
        if self.after_claim:
            self.after_claim(route)
        return result


class GuildWeeklyTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW / 1000).start()
        self.addCleanup(patch.stopall)
        patch.object(config, "load_accounts", return_value={}).start()
        patch.object(config, "save_account").start()
        patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 300, "jitter": 20}).start()
        patch("httpx.Client.request", side_effect=AssertionError("No live requests")).start()
        self.client = WeeklyClient()
        self.acc = accounts.Account("weekly@example.invalid", self.client, None)
        self.acc.toggles["guild_auto_weekly"] = True
        self.weekly = GuildWeeklyService(GuildService(self.client, self.acc.guild_runtime))

    def run_weekly(self):
        self.acc._run_guild_if_due()

    def advance(self, milliseconds):
        self.clock.return_value += milliseconds / 1000
        self.client.now += milliseconds

    def sync_only(self):
        self.acc.toggles["guild_auto_weekly"] = False
        self.run_weekly()
        self.acc.toggles["guild_auto_weekly"] = True

    def test_claims_selected_completed_personal_before_newly_unlocked_tier(self):
        self.run_weekly()
        claimed = [data["RewardQuestInfos"][0]["ID"] for data in self.client.routes("RewardQuest")]
        self.assertEqual(claimed, ["GuildDonatedCourage", "GuildGetRing"])
        self.assertTrue(self.client.calls[-1][0].endswith("RewardGuildQuest"))
        self.assertEqual(self.client.calls[-1][1], {"AID": "local-player", "SessionID": "local-session"})
        status = self.weekly.snapshot()
        self.assertEqual((status["personalClaimed"], status["tiersClaimed"], status["score"]), (2, 1, 500))
        self.assertTrue(has_guild(self.client.account_state))
        # Guild lifetime NowItem must neither replace weekly score nor enter inventory.
        self.assertNotIn("82", {row["StaticID"] for row in self.client.account_state["ItemContainer"]["Items"]})
        self.assertEqual(self.acc._enabled_ids(), [])
        count = len(self.client.calls)
        self.run_weekly()
        self.assertEqual(len(self.client.calls), count)

    def test_disabled_still_shows_progress_without_reward_requests(self):
        self.acc.toggles.clear()
        self.run_weekly()
        self.assertEqual(len(self.client.calls), 5)
        self.assertEqual([row["count"] for row in self.weekly.snapshot()["personal"]], [3, 12, 10])
        self.advance(WEEKLY_POLL_MS)
        self.run_weekly()
        self.assertEqual(len(self.client.calls), 5)

    def test_score_uses_current_tier_progress_not_contribution_or_guild_inventory(self):
        self.client.score = 75
        self.sync_only()
        guild = self.client.account_state["GuildData"]
        guild["ItemContainer"] = {"Items": [{"StaticID": "82", "Count": 628575}]}
        self.assertEqual(guild["Record"]["WeeklyAssistanceScore"], 80)
        self.assertEqual(self.weekly.snapshot()["score"], 75)
        # Knowing public points does not require knowing personal reward claims.
        self.client.account_state["PlayerGuildInfo"].pop("PlayerGuildQuestSave")
        status = self.weekly.snapshot()
        self.assertFalse(status["rewardsKnown"])
        self.assertEqual(status["score"], 75)
        self.assertTrue(all(row["state"] == "unknown" for row in status["tiers"]))

    def test_score_ignores_other_guilds_weeks_unknown_tiers_and_invalid_counts(self):
        self.sync_only()
        guild = self.client.account_state["GuildData"]
        template = copy.deepcopy(guild["GuildQuestList"][0])
        for overrides in ({"GID": "another-guild"}, {"Week": self.client.week - 1},
                          {"StaticID": "FutureUnknownTier"}):
            with self.subTest(overrides=overrides):
                guild["GuildQuestList"] = [{**template, **overrides}]
                self.assertIsNone(self.weekly.snapshot()["score"])
        for count in (None, True, -1, "75"):
            with self.subTest(count=count):
                guild["GuildQuestList"] = [{**template, "QuestData": {"Count": count}}]
                self.assertIsNone(self.weekly.snapshot()["score"])
        guild["GuildQuestList"] = []
        self.assertIsNone(self.weekly.snapshot()["score"])
        guild["GuildQuestList"] = [{**template, "QuestData": {"Count": 0}}]
        self.assertEqual(self.weekly.snapshot()["score"], 0)

    def test_replaying_reward_response_keeps_points_and_contribution_unchanged(self):
        self.sync_only()
        service = self.weekly.guild
        result = service.claim_quest("GuildDonatedCourage")
        service._cache(result)
        self.assertEqual(self.weekly.snapshot()["score"], 475)
        self.assertEqual(self.client.account_state["GuildData"]["Record"]["WeeklyAssistanceScore"], 80)

    def test_enable_uses_cached_eligibility_without_query(self):
        self.sync_only()
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.assertEqual(len(self.client.routes("RewardQuest")), 2)

    def test_hourly_empty_checks_are_silent_and_share_one_query_with_aid(self):
        self.acc.toggles["guild_auto_aid"] = True
        self.acc.params["guild_auto_aid"] = {"request": False, "hunt": True, "element": True}
        self.run_weekly()
        seq = self.acc.log_seq
        self.advance(WEEKLY_POLL_MS - 1)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.advance(1)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(self.acc.log_seq, seq)

    def test_weekly_only_polls_hourly(self):
        self.run_weekly()
        self.advance(WEEKLY_POLL_MS)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)

    def test_external_claim_respected_even_when_public_is_rewarded_false(self):
        self.client.score = 3000
        self.client.own["PlayerGuildQuestSave"]["RewardedGuildQuestList"] = list(weekly_catalog()["tiers"])
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 0)
        self.assertEqual(self.weekly.snapshot()["tiersClaimed"], 6)

    def test_public_rewarded_flag_is_not_used_as_personal_claim_state(self):
        self.client.score = 3000
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 1)
        self.assertEqual(self.weekly.snapshot()["tiersClaimed"], 6)
        self.assertTrue(all(not row["QuestData"]["IsRewarded"] for row in self.client.account_state["GuildData"]["GuildQuestList"]))

    def test_group_timeout_queries_after_five_minutes_without_duplicate_claim(self):
        self.client.fail_tiers = True
        self.run_weekly()
        self.assertTrue(self.weekly.snapshot()["error"])
        self.advance(299_999)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.advance(1)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 1)
        self.assertEqual(self.weekly.snapshot()["tiersClaimed"], 1)

    def test_personal_timeout_never_blindly_repeats_after_guild_query(self):
        self.client.fail_personal = True
        self.run_weekly()
        self.advance(300_000)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardQuest")), 2)  # one timeout, other task succeeds
        state = self.weekly.snapshot()
        self.assertEqual(state["personal"][1]["state"], "uncertain")
        self.advance(WEEKLY_POLL_MS)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardQuest")), 2)
        # A fresh login restores the actual personal claim flag.
        self.client.account_state["QuestContainer"]["Quests"] = copy.deepcopy(self.client.personal)
        self.acc._note_guild_login()
        self.run_weekly()
        self.assertEqual(self.weekly.snapshot()["personalClaimed"], 2)

    def test_empty_reward_response_is_not_logged_as_success(self):
        self.client.bad_personal = True
        self.run_weekly()
        self.assertEqual(self.weekly.snapshot()["personal"][1]["state"], "uncertain")
        logs = [entry["result"] for entry in self.acc.logs_since(0) if entry["result"]["id"] == "guild_auto_weekly"]
        self.assertTrue(logs and all(not row["ok"] for row in logs))

    def test_empty_group_response_is_unconfirmed_then_recovered_by_sync(self):
        self.client.bad_tiers = True
        self.run_weekly()
        self.assertTrue(self.weekly.snapshot()["error"])
        self.advance(300_000)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 1)
        self.assertEqual(self.weekly.snapshot()["tiersClaimed"], 1)

    def test_week_change_never_relabels_cached_personal_progress(self):
        self.run_weekly()
        self.client.week += 1
        self.client.score = 0
        self.client.own["NowGuildQuestWeek"] = self.client.week
        self.client.own["PlayerGuildQuestSave"] = {"LastRewardQuestWeek": self.client.week}
        self.client.personal = [quest("GuildCheckIn", 0, 5), quest("GuildDonatedCourage", 0, 10), quest("GuildGetRing", 0, 10)]
        self.advance(DAY_MS)
        self.run_weekly()
        status = self.weekly.snapshot()
        self.assertFalse(status["personalKnown"])
        self.assertTrue(all(row["count"] is None for row in status["personal"]))
        self.assertEqual(len(self.client.routes("RewardQuest")), 2)
        self.client.account_state["QuestContainer"]["Quests"] = copy.deepcopy(self.client.personal)
        self.acc._note_guild_login()
        self.run_weekly()
        self.assertTrue(self.weekly.snapshot()["personalKnown"])
        self.assertEqual([row["count"] for row in self.weekly.snapshot()["personal"]], [0, 0, 0])

    def test_day_boundary_queries_before_using_old_progress(self):
        self.sync_only()
        self.acc.guild_runtime["weeklySyncedDay"] -= 1
        self.run_weekly()
        routes = [route for route, _ in self.client.calls[5:]]
        self.assertEqual(routes[0], "GuildHandler.QueryGuildDataForSync")

    def test_unknown_or_inconsistent_personal_progress_is_not_claimed(self):
        self.client.ids.append("GuildFutureUnknown")
        self.client.personal[1]["IsFinish"] = False
        self.client.personal[2]["Count"] = 0
        self.client.account_state["QuestContainer"]["Quests"] = copy.deepcopy(self.client.personal)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardQuest")), 0)
        self.assertEqual(self.weekly.snapshot()["personal"][-1]["state"], "unknown")
        self.assertGreater(self.acc._guild_due_delay(), 0)

    def test_foreign_and_old_week_public_rows_do_not_trigger_reward(self):
        for row in self.client.personal:
            row["IsRewarded"] = True
        self.client.account_state["QuestContainer"]["Quests"] = copy.deepcopy(self.client.personal)
        self.sync_only()
        rows = self.client.account_state["GuildData"]["GuildQuestList"]
        for row in rows:
            row["QuestData"].update(Count=99999, IsFinish=True)
            row["GID"] = "another-guild"
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 0)
        for row in rows:
            row.update(GID="guild", Week=665)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 0)

    def test_partial_personal_reply_cannot_wipe_member_state(self):
        self.sync_only()
        self.client.account_state["PlayerGuildInfo"]["DayDonateGoldCount"] = 7
        self.client.after_claim = lambda route: self.acc.toggles.update(guild_auto_weekly=False)
        self.run_weekly()
        self.assertEqual(self.client.account_state["PlayerGuildInfo"]["DayDonateGoldCount"], 7)
        self.assertTrue(has_guild(self.client.account_state))

    def test_disabling_mid_batch_stops_next_request(self):
        self.client.after_claim = lambda route: self.acc.toggles.update(guild_auto_weekly=False)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardQuest")), 1)
        self.assertEqual(len(self.client.routes("RewardGuildQuest")), 0)

    def test_no_guild_or_disconnection_never_sends_requests(self):
        self.client.account_state["PlayerGuildInfo"]["GID"] = ""
        self.run_weekly()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.weekly.snapshot()["personal"], [])
        self.client.account_state["PlayerGuildInfo"]["GID"] = "guild"
        self.client.connected = False
        self.run_weekly()
        self.assertEqual(self.client.calls, [])

    def test_shared_query_failure_is_not_repeated_by_other_feature(self):
        self.acc.toggles["guild_auto_aid"] = True
        self.acc.params["guild_auto_aid"] = {"request": False, "hunt": True}
        self.run_weekly()
        self.advance(WEEKLY_POLL_MS)
        self.client.fail_query = True
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertGreaterEqual(self.acc._guild_due_delay(), 300)
        self.advance(300_000)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 2)

    def test_quest_progress_event_wakes_weekly_scheduler(self):
        self.run_weekly()
        self.acc._wake.clear()
        revision = self.acc._guild_revision
        self.client.personal[0].update(Count=5, IsFinish=True)
        self.client._apply_quest_updates({"Quests": [quest("GuildCheckIn", 5, 5)]})
        self.assertTrue(self.acc._wake.is_set())
        self.assertGreater(self.acc._guild_revision, revision)
        self.run_weekly()
        self.assertEqual(len(self.client.routes("RewardQuest")), 3)
        self.assertEqual(self.weekly.snapshot()["personalClaimed"], 3)

    def test_catalog_and_local_reward_icons(self):
        root = Path(__file__).resolve().parents[1]
        catalog = weekly_catalog()
        self.assertEqual(catalog["personal"]["GuildDonatedGold"]["target"], 200000)
        self.assertEqual(catalog["personal"]["GuildDonatedCourage"]["points"], 20)
        self.assertEqual(catalog["personal"]["GuildGetHead"]["points"], 25)
        self.assertEqual(catalog["personal"]["GuildCH1AP"]["points"], 0)
        self.assertEqual([row["target"] for row in catalog["tiers"].values()], [500, 1000, 1500, 2000, 2400, 2800])
        for row in catalog["tiers"].values():
            for reward in row["rewards"]:
                self.assertTrue((root / "assets/items" / f"{reward['item']}.png").is_file())

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "assets_full").is_dir(),
                         "Optional extracted game tables are not distributed")
    def test_catalog_matches_local_source_tables(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(weekly_catalog(), build_guild_quests.build(root / "assets_full"))


if __name__ == "__main__":
    unittest.main()
