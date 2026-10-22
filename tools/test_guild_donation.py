"""Offline guild donation tests, including account scheduling and real balance merging."""
import copy
import unittest
from unittest.mock import patch

from backend import accounts, appcore, config
from backend.game_client import GameClient, GameError
from backend.guild import GUILD_DONATIONS, GUILD_ENTRY_ROUTES, GuildService, donation_reserves

NOW = 1792116000000


class DonationClient(GameClient):
    def __init__(self, gold=50000, courage=3):
        self.aid, self.session_id = "donor", "local-session"
        self.connected = True
        self.now = NOW
        self.own = {"GID": "guild", "LastCheckInTime": {"$date": NOW},
                    "DayDonateGoldCount": 0, "DayDonateCourageCount": 0,
                    "CanLastDayCheckReward": False}
        self.info = {"CUID": "donor"}
        self.balance = {"1": gold, "28": courage}
        self.account_state = {"Info": self.info, "Utc": str(NOW // 1000),
                              "PlayerGuildInfo": copy.deepcopy(self.own),
                              "ItemContainer": {"Items": [{"StaticID": k, "Count": v}
                                                          for k, v in self.balance.items()]}}
        self.account_state["ActivityDBSettingContainer"] = {"SettingList": [
            {"ActivityID": "Version_GVG", "IsIgnoreTime": True}]}
        self.calls, self.action_states = [], []
        self.timeout_routes, self.unconfirmed_routes = set(), set()
        self.after_donate = None

    def is_connected(self):
        return self.connected

    def call(self, route, data=None):
        self.calls.append((route, copy.deepcopy(data)))
        result = {"Utc": str(self.now // 1000)}
        if route == "AccountHandler.QueryAccountInfo":
            result["Info"] = copy.deepcopy(self.info)
        elif route == "GuildHandler.QueryFullGuildData":
            result["GuildData"] = {"Info": {"Name": "Guild", "_id": "guild", "GuildLV": 18},
                                   "Record": {"LastCheckInCount": 1},
                                   "MemberList": [{"PlayerInfo": self.info,
                                                   "PlayerGuildInfo": copy.deepcopy(self.own)}]}
        elif route == "GuildHandler.GuildMemberCheckIn":
            self.own.update(LastCheckInTime={"$date": self.now}, DayDonateGoldCount=0,
                            DayDonateCourageCount=0)
            result["PlayerGuildData"] = {"PlayerGuildInfo": copy.deepcopy(self.own)}
        elif route in [rule["route"] for rule in GUILD_DONATIONS.values()]:
            self.action_states.append(self.account.guild_snapshot())
            kind, rule = next((key, rule) for key, rule in GUILD_DONATIONS.items() if rule["route"] == route)
            count = data["Count"]
            assert data["ItemID"] == rule["item"]
            assert self.own[rule["field"]] + count <= rule["limit"]
            assert count * rule["cost"] <= self.balance[rule["item"]]
            self.own[rule["field"]] += count
            self.balance[rule["item"]] -= count * rule["cost"]
            result["PlayerGuildInfo"] = copy.deepcopy(self.own)
            result["CostItems"] = [{"NowItem": {"StaticID": rule["item"], "Count": self.balance[rule["item"]]}}]
            if self.after_donate:
                self.after_donate(kind)
            if route in self.timeout_routes:
                raise GameError("response timed out")
            if route in self.unconfirmed_routes:
                del result["PlayerGuildInfo"]
        elif route not in [route for _, route, _ in GUILD_ENTRY_ROUTES]:
            raise AssertionError(f"Unexpected route: {route}")
        self._apply_now_items(result)
        return result

    def donations(self):
        return [(route, data["Count"]) for route, data in self.calls
                if route in [rule["route"] for rule in GUILD_DONATIONS.values()]]


class GuildDonationTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW / 1000).start()
        self.addCleanup(patch.stopall)
        patch.object(config, "load_accounts", return_value={}).start()
        patch.object(config, "save_account").start()
        patch("httpx.Client.request", side_effect=AssertionError("No live requests")).start()
        self.client = DonationClient()
        self.acc = accounts.Account("donor@example.invalid", self.client, None)
        self.client.account = self.acc
        # Existing protocol cases deliberately spend the entire available balance.
        self.acc.params["guild_auto_donate"] = {"reserve": {"gold": 0, "courage": 0}}
        self.service = GuildService(self.client, self.acc.guild_runtime, {"gold": 0, "courage": 0})

    def test_affordable_count_is_bounded_by_balance_and_daily_remainder(self):
        for balance, donated, expected in ((0, 0, 0), (4999, 0, 0), (5000, 0, 1),
                                           (12999, 0, 2), (50000, 0, 10), (999999, 7, 3),
                                           (999999, 10, 0), (999999, 11, 0)):
            with self.subTest(balance=balance, donated=donated):
                self.client.account_state["ItemContainer"]["Items"][0]["Count"] = balance
                self.client.account_state["PlayerGuildInfo"]["DayDonateGoldCount"] = donated
                self.assertEqual(self.service.donation_status()["gold"]["count"], expected)
        self.client.account_state["PlayerGuildInfo"]["DayDonateCourageCount"] = 2
        self.assertEqual(self.service.donation_status()["courage"]["count"], 1)
        self.assertEqual(self.client.calls, [])

    def test_login_donates_both_once_and_uses_confirmed_absolute_balances(self):
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 10), ("GuildHandler.DonateCourage", 3)])
        self.assertEqual(len(self.client.calls), 7)
        self.assertEqual([row["Count"] for row in self.client.account_state["ItemContainer"]["Items"]], [0, 0])
        for state in self.client.action_states:
            self.assertFalse(state["loading"])
            self.assertIn(state["donating"], ("gold", "courage"))
        self.acc._run_guild_if_due()
        for enabled in (False, True):
            self.acc.toggles["guild_auto_donate"] = enabled
            self.acc.notify_config_changed()
            self.acc._run_guild_if_due()
        self.assertEqual(len(self.client.calls), 7)
        self.assertEqual(self.acc._enabled_ids(), [])
        self.assertIsNone(self.acc._guild_due_delay())

    def test_toggle_and_selection_only_send_selected_donations(self):
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [])
        self.acc.toggles["guild_auto_donate"] = True
        self.acc.params["guild_auto_donate"].update(gold=False, courage=False)
        self.acc._run_guild_if_due()
        self.assertIsNone(self.acc._guild_due_delay())
        self.acc.params["guild_auto_donate"]["courage"] = True
        self.acc.notify_config_changed()
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateCourage", 3)])
        self.assertEqual(len(self.client.calls), 6)

    def test_reserves_reduce_affordable_donation_count(self):
        self.acc.params["guild_auto_donate"] = {
            "gold": True, "courage": True,
            "reserve": {"gold": 50000, "courage": 2},
        }
        service = GuildService(self.client, self.acc.guild_runtime,
                               donation_reserves(self.acc.params["guild_auto_donate"]))
        self.assertEqual(service.donation_status()["gold"]["count"], 0)
        self.assertEqual(service.donation_status()["courage"]["count"], 1)
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateCourage", 1)])

    def test_weekly_quest_defaults_gate_scheduler_snapshot_and_spending(self):
        self.acc.params.pop("guild_auto_donate")
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [])
        self.assertIsNone(self.acc._guild_due_delay())
        for gold, courage, expected in ((200000, 10, (0, 0)), (204999, 10, (0, 0)),
                                         (205000, 11, (1, 1)), (250000, 13, (10, 3))):
            with self.subTest(gold=gold, courage=courage):
                self.client.balance.update({"1": gold, "28": courage})
                self.client._apply_now_items({"Drop": {"Items": [
                    {"NowItem": {"StaticID": key, "Count": value}}
                    for key, value in self.client.balance.items()]}})
                rows = self.acc.guild_snapshot()["donations"]
                self.assertEqual((rows["gold"]["count"], rows["courage"]["count"]), expected)
                self.assertEqual(self.acc._guild_due_delay(), 0 if any(expected) else None)
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 10), ("GuildHandler.DonateCourage", 3)])
        self.assertEqual(self.client.balance, {"1": 200000, "28": 10})
        self.assertIsNone(self.acc._guild_due_delay())

    def test_missing_and_invalid_reserves_use_defaults_but_explicit_zero_is_preserved(self):
        for settings in (None, {}, {"gold": True}, {"reserve": None}, {"reserve": {}},
                         {"reserve": {"gold": -1, "courage": True}},
                         {"reserve": {"gold": "invalid", "courage": 1.5}}):
            with self.subTest(settings=settings):
                self.assertEqual(donation_reserves(settings), {"gold": 200000, "courage": 10})
        self.assertEqual(donation_reserves({"reserve": {"gold": 0, "courage": 0}}), {"gold": 0, "courage": 0})
        self.assertEqual(donation_reserves({"reserve": {"gold": " 50000 "}}), {"gold": 50000, "courage": 10})
        self.assertEqual(GuildService(self.client).donation_status()["gold"]["count"], 0)

    def test_partial_donation_can_top_up_from_later_cached_inventory(self):
        self.client.balance.update({"1": 12999, "28": 2})
        self.client.account_state["ItemContainer"]["Items"] = [{"StaticID": k, "Count": v}
                                                              for k, v in self.client.balance.items()]
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 2), ("GuildHandler.DonateCourage", 2)])
        self.assertIsNone(self.acc._guild_due_delay())
        self.client.balance["1"] = 99999
        self.client._apply_now_items({"Drop": {"Items": [{"NowItem": {"StaticID": "1", "Count": 99999}}]}})
        self.assertEqual(self.acc._guild_due_delay(), 0)
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations()[-1], ("GuildHandler.DonateGold", 8))
        self.assertEqual(len(self.client.calls), 8)

    def test_new_day_waits_for_real_checkin_response_to_reset_counts(self):
        self.client.own.update(DayDonateGoldCount=10, DayDonateCourageCount=3)
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [])
        self.clock.return_value += 86400
        self.client.now += 86400000
        self.assertEqual(self.service.donation_status()["gold"]["count"], 0)
        self.acc.note_state_refreshed()
        self.acc._run_guild_if_due()
        routes = [route for route, _ in self.client.calls]
        self.assertEqual(routes.count("GuildHandler.GuildMemberCheckIn"), 1)
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 10), ("GuildHandler.DonateCourage", 3)])

    def test_unknown_counters_or_no_membership_never_spend(self):
        for invalid in (None, -1, True, "0", 0.5):
            with self.subTest(invalid=invalid):
                self.client.account_state["PlayerGuildInfo"]["DayDonateGoldCount"] = invalid
                self.assertEqual(self.service.donate_if_due("gold"), 0)
        self.acc.toggles["guild_auto_donate"] = True
        self.client.account_state["PlayerGuildInfo"]["GID"] = ""
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.calls, [])

    def test_uncertain_gold_does_not_repeat_or_block_courage_or_trigger_queries(self):
        self.client.timeout_routes.add("GuildHandler.DonateGold")
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 10), ("GuildHandler.DonateCourage", 3)])
        self.assertIn("gold", self.acc.guild_snapshot()["donationErrors"])
        self.assertIsNone(self.acc.guild_snapshot()["donating"])
        self.clock.return_value += 300
        for enabled in (False, True):
            self.acc.toggles["guild_auto_donate"] = enabled
            self.acc.notify_config_changed()
            self.acc._run_guild_if_due()
        self.assertEqual(len(self.client.calls), 7)
        with self.acc.lock:
            self.acc.refresh_guild_locked()
        self.assertEqual(len(self.client.calls), 12)
        self.assertEqual(len(self.client.donations()), 2)
        self.assertEqual(self.acc.guild_snapshot()["donationErrors"], {})

    def test_unconfirmed_response_stops_spending_same_resource(self):
        self.client.unconfirmed_routes.add("GuildHandler.DonateGold")
        self.acc.toggles["guild_auto_donate"] = True
        self.acc._run_guild_if_due()
        self.assertIn("gold", self.acc.guild_snapshot()["donationErrors"])
        self.client._apply_now_items({"Drop": {"Items": [{"NowItem": {"StaticID": "1", "Count": 999999}}]}})
        self.acc._run_guild_if_due()
        self.assertEqual(len(self.client.donations()), 2)

    def test_disable_between_requests_skips_second_resource(self):
        self.acc.toggles["guild_auto_donate"] = True
        self.client.after_donate = lambda kind: self.acc.toggles.update(guild_auto_donate=False)
        self.acc._run_guild_if_due()
        self.assertEqual(self.client.donations(), [("GuildHandler.DonateGold", 10)])

    def test_settings_preserve_other_task_parameters_and_accounts(self):
        self.acc.params["unrelated"] = {"keep": True}
        with patch.object(accounts, "get", return_value=self.acc):
            result = appcore.set_toggles(self.acc.email, {"guild_auto_donate": True},
                                         {"guild_auto_donate": {"gold": True, "courage": False}})
        self.assertTrue(result["params"]["unrelated"]["keep"])
        self.assertEqual(result["params"]["guild_auto_donate"], {"gold": True, "courage": False})
        self.assertEqual(self.client.calls, [])
        config.save_account.assert_called_once()
        other = accounts.Account("other@example.invalid", DonationClient(), None)
        self.assertNotIn("guild_auto_donate", other.toggles)
        self.assertNotIn("guild_auto_donate", other.params)


if __name__ == "__main__":
    unittest.main()
