"""Guild membership comes from the account summary, even when GID is absent."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from backend.game_client import GameClient, GameError
from backend.guild import GuildService, guild_id, has_guild
from tools.test_guild import captain_store_client, captain_store_guild


def login_snapshot():
    return {
        "Info": {"_id": {"$oid": "player-id"}, "CUID": 101,
                 "GuildSubInfo": {"_id": {"$oid": "guild-id"}}},
        "PlayerGuildInfo": {"_id": {"$oid": "player-id"}, "Authority": 1,
                            "DayDonateGoldCount": 4},
        "GuildData": {"Info": {"_id": {"$oid": "guild-id"},
                               "Name": "Test guild", "GuildLV": 18, "MemberCount": 29}},
    }


class GuildMembershipTests(unittest.TestCase):
    def setUp(self):
        self.state = login_snapshot()
        self.client = SimpleNamespace(account_state=self.state)
        self.service = GuildService(self.client)

    def test_summary_identifies_membership_without_member_gid(self):
        self.assertEqual(guild_id(self.state), "guild-id")
        status = self.service.snapshot()
        self.assertTrue(status["joined"])
        self.assertEqual((status["name"], status["level"], status["memberCount"]),
                         ("Test guild", 18, 29))
        self.state["Info"]["GuildSubInfo"]["_id"] = "guild-id"
        self.assertTrue(has_guild(self.state))
        self.state["PlayerGuildInfo"]["GID"] = "outdated-guild-id"
        self.assertEqual(guild_id(self.state), "guild-id")

    def test_missing_summary_does_not_use_old_member_or_other_guild_data(self):
        for summary in (None, {}, {"_id": ""}, {"_id": {}}, {"_id": []}, "guild-id"):
            with self.subTest(summary=summary):
                state = deepcopy(self.state)
                state["Info"]["GuildSubInfo"] = summary
                state["PlayerGuildInfo"]["GID"] = "old-guild-id"
                self.assertFalse(has_guild(state))
        for state in (None, {}, [], {"Info": None}, {"Info": []}):
            with self.subTest(state=state):
                self.assertFalse(has_guild(state))

    def test_partial_response_preserves_summary_but_explicit_clear_removes_it(self):
        self.service._cache({"Info": {"LV": 70}})
        self.service._cache({"Drop": {"Items": []}})
        self.assertEqual(guild_id(self.state), "guild-id")
        self.service._cache({"Info": {"GuildSubInfo": None}})
        self.assertFalse(has_guild(self.state))

    def test_full_relogin_removes_previous_membership(self):
        client = GameClient.__new__(GameClient)
        client._replace_account_state(deepcopy(self.state))
        self.assertTrue(has_guild(client.account_state))
        client._replace_account_state({"Info": {"CUID": 101},
                                       "PlayerGuildInfo": {"GID": "", "Authority": 3}})
        self.assertFalse(has_guild(client.account_state))

    def test_scene_partial_account_info_preserves_summary(self):
        client = GameClient.__new__(GameClient)
        client._replace_account_state(deepcopy(self.state))
        client.apply_scene_reward({"AccountInfo": {"LV": 70}})
        self.assertEqual(guild_id(client.account_state), "guild-id")

    def test_full_guild_refresh_accepts_own_member_without_gid(self):
        member = {"PlayerInfo": deepcopy(self.state["Info"]),
                  "PlayerGuildInfo": {"_id": {"$oid": "player-id"}, "Authority": 1,
                                      "DayDonateGoldCount": 7}}
        guild = deepcopy(self.state["GuildData"])
        guild["MemberList"] = [member]
        self.service._cache({"GuildData": guild})
        self.assertEqual(self.state["PlayerGuildInfo"]["DayDonateGoldCount"], 7)
        self.assertTrue(has_guild(self.state))
        guild["Info"]["_id"] = "foreign-guild"
        with self.assertRaises(GameError):
            self.service._cache({"GuildData": guild})
        self.assertEqual(self.state["GuildData"]["Info"]["_id"], {"$oid": "guild-id"})

    def test_member_updates_without_gid_require_own_player_id(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                update = {"_id": {"$oid": "player-id"}, "DayDonateGoldCount": 8}
                response = {"PlayerGuildInfo": update}
                self.service._cache({"PlayerGuildData": response} if nested else response)
                self.assertEqual(self.state["PlayerGuildInfo"], update)
                for rejected in ({"NowGuildQuestWeek": 0, "DayDonateGoldCount": 0},
                                 {"_id": "another-player", "DayDonateGoldCount": 0},
                                 {"_id": "player-id", "GID": "another-guild",
                                  "DayDonateGoldCount": 0}):
                    self.service._cache({"PlayerGuildInfo": rejected})
                    self.assertEqual(self.state["PlayerGuildInfo"], update)
                self.assertTrue(has_guild(self.state))

    def test_captain_purchase_retains_role_checks_without_gid(self):
        for authority in (1, 2, 3):
            with self.subTest(authority=authority):
                reply = captain_store_guild([], authority)
                reply["GuildData"]["MemberList"][0]["PlayerGuildInfo"].pop("GID")
                client = captain_store_client([reply, {"CommodityRecord": {
                    "StaticID": "GuildBox", "Store": "CaptainShop", "BuyCount": 1}}])
                client.account_state["PlayerGuildInfo"].pop("GID")
                service = GuildService(client)
                if authority == 3:
                    with self.assertRaisesRegex(GameError, "团长和副团长"):
                        service.buy("GuildBox")
                    self.assertEqual(client.call.call_count, 1)
                else:
                    service.buy("GuildBox")
                    self.assertEqual(client.call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
