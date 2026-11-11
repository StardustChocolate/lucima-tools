"""Offline guild protocol and account lifecycle regression checks."""
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import accounts, appcore, config, server
from backend.game_client import GameClient, GameError, GameMessage
from backend.guild import (CaptainBuffStatusPending, CaptainShopInsufficientCurrency,
                           GUILD_ROUTES, GuildService,
                           commodity_record, quest_response_summary)


def client(responses):
    result = Mock(spec=GameClient)
    result.account_state = {}
    result.is_connected.return_value = True
    result.connection_status.return_value = {"connected": True}
    result._auth_data.side_effect = lambda extra=None: {"AID": "test-aid", "SessionID": "test-session", **(extra or {})}
    result.call.side_effect = responses
    return result


def guild(ids):
    return {"GuildData": {"Info": {"Name": "Test guild", "_id": {"$oid": "guild-id"}},
                          "Record": {"GuildQuestIDList": ids}}}


def captain_store_guild(records, authority=1, *, include_member=True):
    data = {"Info": {"Name": "Test guild", "_id": {"$oid": "guild-id"}},
            "GuildStoreRecord": {"Records": records},
            "ItemContainer": {"Items": [{"StaticID": "80", "Count": 204000},
                                        {"StaticID": "81", "Count": 306}]}}
    if include_member:
        data["MemberList"] = [{
            "PlayerInfo": {"_id": {"$oid": "player-id"}, "CUID": 101},
            "PlayerGuildInfo": {"GID": "guild-id", "Authority": authority},
        }]
    return {"GuildData": data}


def captain_store_client(responses):
    c = client(responses)
    c.account_state = {
        "Info": {"_id": {"$oid": "player-id"}, "CUID": 101,
                 "GuildSubInfo": {"_id": {"$oid": "guild-id"}}},
        "PlayerGuildInfo": {"GID": "guild-id", "Authority": 1},
    }
    return c


class GuildProtocolTests(unittest.TestCase):
    def test_overview_reuses_session_and_keeps_partial_business_errors(self):
        c = client([guild([]), GameMessage("CantCheckIn"), {}, {}])
        data = GuildService(c).overview()
        self.assertEqual(data["guild"], guild([]))
        self.assertIn("CantCheckIn", data["full"]["error"])
        self.assertEqual([call.args[0] for call in c.call.call_args_list], [route for _, route in GUILD_ROUTES])
        self.assertTrue(all(call.args[1] == {"AID": "test-aid", "SessionID": "test-session"}
                            for call in c.call.call_args_list))

    def test_overview_stops_auth_errors_and_handles_game_errors(self):
        for error in (GameMessage("Duplicate Login"), GameMessage("invalid token"), GameError("session expired")):
            with self.subTest(error=str(error)):
                c = client([error])
                with self.assertRaises(type(error)):
                    GuildService(c).overview()
                self.assertEqual(c.call.call_count, 1)
        c = client([GameError("temporary failure"), {}, {}, {}])
        self.assertIn("temporary failure", GuildService(c).overview()["guild"]["error"])

    def test_checkin_order_and_attendance_route(self):
        c = client([{}, {"GuildData": {"Info": {"Name": "Test guild", "GuildLV": 18}}}, {}, {}, {},
                    {"Utc": "1792072766", "PlayerGuildData": {"PlayerGuildInfo": {
                        "GID": "guild-id", "LastCheckInTime": {"$date": 1792072765765}}}}, {}])
        c.account_state = {"Info": {"GuildSubInfo": {"_id": "guild-id"}},
                           "PlayerGuildInfo": {"GID": "guild-id", "LastCheckInTime": {"$date": 0}},
                           "ActivityDBSettingContainer": {"SettingList": [
                               {"ActivityID": "Version_GVG", "IsIgnoreTime": True}]}}
        service = GuildService(c)
        service.checkin()
        service.attendance()
        from backend.guild import GUILD_ENTRY_ROUTES
        self.assertEqual([call.args[0] for call in c.call.call_args_list],
                         [route for _, route, _ in GUILD_ENTRY_ROUTES] +
                         ["GuildHandler.GuildMemberCheckIn", "GuildHandler.GuildMemberDayCheckReward"])
        self.assertEqual(c.account_state["PlayerGuildInfo"]["LastCheckInTime"], {"$date": 1792072765765})

    def test_quest_ids_require_live_guild_and_validate_and_deduplicate(self):
        c = client([guild(["one", " one ", 2])])
        self.assertEqual(GuildService(c).quest_ids(), {"guildName": "Test guild", "questIds": ["one", "2"]})
        invalid = [{}, {"GuildData": None}, guild("bad"), guild({}), guild([True]), guild([""]), guild([None]),
                   guild([str(i) for i in range(513)])]
        for reply in invalid:
            with self.subTest(reply=str(reply)[:60]):
                with self.assertRaises(GameError):
                    GuildService(client([reply])).quest_ids()

    def test_quest_payload_accepts_empty_results_without_asserting_rewards(self):
        for reply in ({}, {"Results": None}, {"Results": []}, {"Results": [{}]},
                      {"Results": [{"Drop": None, "Quests": [], "GuildQuestDataList": [], "HasMail": False}]}):
            with self.subTest(reply=reply):
                c = client([reply])
                result = GuildService(c).claim_quest("GuildCheckIn")
                self.assertEqual(quest_response_summary(result)["state"], "received")
                c.call.assert_called_once_with("QuestHandler.RewardQuest", {
                    "AID": "test-aid", "SessionID": "test-session",
                    "RewardQuestInfos": [{"ID": "GuildCheckIn", "Index": 0}]})

    def test_malformed_rewards_stop_and_explicit_errors_are_rejected(self):
        for reply in ({"Results": {}}, {"Results": [1]}, {"Results": [{"Quests": {}}]},
                      {"Results": [{"Quests": [1]}]}, {"Results": [{"Drop": []}]},
                      {"Results": [{"HasMail": "true"}]}):
            with self.subTest(reply=reply), self.assertRaises(GameError):
                GuildService(client([reply])).claim_quest("one")
        for reply in ("OverCount", {"success": False, "Reason": "OverCount"},
                      {"Results": [{"_isErrorResponse": True, "message": "OverCount"}]}):
            with self.subTest(reply=reply), self.assertRaises(GameMessage):
                GuildService(client([reply])).claim_quest("one")
        self.assertEqual(GuildService(client([{"ErrorCode": "info", "Reason": "notice"}])).claim_quest("one"),
                         {"ErrorCode": "info", "Reason": "notice"})

    def test_claim_does_not_reapply_nested_balances_or_rollback_last_result(self):
        c = GameClient.__new__(GameClient)
        c.account_state = {"ItemContainer": {"Items": [{"StaticID": "1", "Count": 10}]}}
        reply = {"Results": [{"Drop": {"Items": [{"NowItem": {"StaticID": "1", "Count": 20}}]}},
                             {"Drop": {"Items": [{"NowItem": {"StaticID": "1", "Count": 30}}]}}]}
        c._auth_data = Mock(return_value={})
        with patch.object(c, "_apply_now_items", wraps=c._apply_now_items) as merge:
            def call(*_):
                c._apply_now_items(reply)
                return reply
            c.call = Mock(side_effect=call)
            result = GuildService(c).claim_quest("one")
            merge.assert_called_once_with(reply)
        self.assertEqual(c.inventory_snapshot()["items"][0]["Count"], 30)
        self.assertEqual(quest_response_summary(result)["state"], "success")

    def test_buy_uses_captain_record_and_exact_platform_fields(self):
        record = {"_id": {"$oid": "record-id"}, "StaticID": "GuildBox", "Store": "CaptainShop",
                  "BuyCount": 2, "LastBuyTime": {"$date": 0}, "Ignored": "not sent"}
        returned = {"_id": {"$oid": "record-id"}, "StaticID": "GuildBox", "Store": "CaptainShop",
                    "BuyCount": 3, "FreeBuyCount": 0, "GuaranteedCount": 0}
        c = captain_store_client([captain_store_guild([record]),
                                  {"CommodityRecord": returned,
                                   "CostItems": [{"Item": {"StaticID": "81", "Count": 300},
                                                  "NowItem": {"StaticID": "81", "Count": 6}}]}])
        result = GuildService(c).buy("GuildBox")
        c.call.assert_called_with("GuildItemHandler.BuyGuildCommodity", {
            "AID": "test-aid", "SessionID": "test-session", "Record": commodity_record("GuildBox", record),
            "Count": 0, "ItemIndex": -1, "LoginType": "ID_Password", "NewErolabs": 0,
            "Platform": "Android", "SelectRoleID": ""})
        self.assertEqual(c.call.call_args.args[1]["Record"]["LastBuyTime"], "1970/1/1 0:0:0")
        self.assertNotIn("Ignored", c.call.call_args.args[1]["Record"])
        self.assertEqual(result["CommodityRecord"]["BuyCount"], 3)
        self.assertEqual(c.account_state["GuildData"]["GuildStoreRecord"]["Records"][0], returned)
        balances = {row["StaticID"]: row["Count"] for row in c.account_state["GuildData"]["ItemContainer"]["Items"]}
        self.assertEqual(balances, {"80": 204000, "81": 6})
        self.assertNotIn("ItemContainer", c.account_state)
        for item_id, count in (("unknown", 1), ("GuildBox", 2), ("GuildBox", True)):
            with self.subTest(item_id=item_id, count=count), self.assertRaises(GameError):
                GuildService(c).buy(item_id, count)
        self.assertEqual(c.call.call_count, 2)

    def test_guild_queries_refresh_shared_balances_and_preserve_personal_inventory(self):
        from backend.guild_captain_store import GuildCaptainStoreService
        refreshed = captain_store_guild([])
        refreshed["GuildData"]["ItemContainer"]["Items"][0]["Count"] = 0
        c = captain_store_client([captain_store_guild([]), refreshed])
        c.account_state["ItemContainer"] = {"Items": [{"StaticID": "28", "Count": 10}]}
        service = GuildService(c)
        for expected in (204000, 0):
            service._call("GuildHandler.QueryFullGuildData")
            self.assertEqual(GuildCaptainStoreService(service).snapshot()["currencies"], {"80": expected, "81": 306})
        self.assertEqual(c.account_state["ItemContainer"]["Items"], [{"StaticID": "28", "Count": 10}])

    def test_buy_without_record_uses_default_and_string_date_is_preserved(self):
        returned = {"_id": {"$oid": "new-record"}, "StaticID": "GuildEnergy30",
                    "Store": "CaptainShop", "BuyCount": 1}
        c = captain_store_client([captain_store_guild([]), {"CommodityRecord": returned}])
        self.assertEqual(GuildService(c).buy("GuildEnergy30")["CommodityRecord"], returned)
        self.assertEqual(c.call.call_args.args[1]["Record"], commodity_record("GuildEnergy30"))
        self.assertEqual(commodity_record("GuildBox", {"LastBuyTime": "2026/1/2 3:4:5"})["LastBuyTime"],
                         "2026/1/2 3:4:5")
        self.assertEqual(c.account_state["GuildData"]["GuildStoreRecord"]["Records"], [returned])

    def test_buying_buff_queries_and_caches_the_new_timed_status(self):
        record = {"_id": {"$oid": "buff-record"}, "StaticID": "GuildMoneyBonus",
                  "Store": "CaptainShop", "BuyCount": 0}
        returned = {**record, "BuyCount": 1}
        status = {"_id": {"$oid": "buff-status"}, "StaticID": "GuildMoneyBonus",
                  "EndTime": {"$date": int(time.time() * 1000) + 12 * 60 * 60 * 1000}}
        c = captain_store_client([captain_store_guild([record]),
                                  {"CommodityRecord": returned},
                                  {"AccountStatusList": [status]}])
        c.account_state["StatusContainer"] = {"List": []}
        c._apply_account_status_updates.side_effect = lambda body: GameClient._apply_account_status_updates(c, body)
        runtime = {}

        GuildService(c, runtime).buy("GuildMoneyBonus")

        self.assertEqual([call.args[0] for call in c.call.call_args_list], [
            "GuildHandler.QueryFullGuildData", "GuildItemHandler.BuyGuildCommodity",
            "ServerStatusHandler.QueryGuildStatus"])
        self.assertEqual(c.account_state["StatusContainer"]["List"], [status])
        self.assertFalse(runtime.get("captainShopBuffStatusPending"))

    def test_captain_buy_rechecks_public_currency_from_fresh_guild_data(self):
        record = {"StaticID": "GuildBox", "Store": "CaptainShop", "BuyCount": 0}
        full = captain_store_guild([record])
        full["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 299
        c = captain_store_client([full])

        with self.assertRaises(CaptainShopInsufficientCurrency):
            GuildService(c).buy("GuildBox", required_currency="81", required_amount=300)

        self.assertEqual(c.call.call_count, 1)

    def test_buff_in_purchase_response_confirms_new_expiry_using_server_time(self):
        now = 1792116000000
        record = {"_id": {"$oid": "buff-record"}, "StaticID": "GuildMoneyBonus",
                  "Store": "CaptainShop", "BuyCount": 0}
        status = {"_id": {"$oid": "buff-status"}, "StaticID": "GuildMoneyBonus",
                  "EndTime": {"$date": now + 12 * 3600 * 1000}}
        full = {**captain_store_guild([record]), "Utc": str(now // 1000)}
        c = captain_store_client([full, {"CommodityRecord": {**record, "BuyCount": 1},
                                        "AccountStatusList": [status]}])
        c.account_state["StatusContainer"] = {"List": [{**status, "EndTime": {"$date": now + 3600 * 1000}}]}
        c._apply_account_status_updates.side_effect = lambda body: GameClient._apply_account_status_updates(c, body)
        runtime = {}

        with patch("time.time", return_value=now / 1000 + 24 * 3600):
            GuildService(c, runtime).buy("GuildMoneyBonus")

        self.assertEqual(c.call.call_count, 2)
        self.assertEqual(c.account_state["StatusContainer"]["List"], [status])
        self.assertFalse(runtime.get("captainShopBuffStatusPending"))

    def test_unmergeable_status_does_not_release_pending_buff_purchase(self):
        record = {"StaticID": "GuildHeroExp", "Store": "CaptainShop", "BuyCount": 0}
        status = {"StaticID": "GuildHeroExp", "EndTime": {"$date": int(time.time() * 1000) + 12 * 3600 * 1000}}
        c = captain_store_client([captain_store_guild([record]),
                                  {"CommodityRecord": {**record, "BuyCount": 1}},
                                  {"AccountStatusList": [status]}])
        c._apply_account_status_updates.side_effect = lambda body: GameClient._apply_account_status_updates(c, body)
        runtime = {}

        with self.assertRaises(CaptainBuffStatusPending):
            GuildService(c, runtime).buy("GuildHeroExp")

        self.assertIn("GuildHeroExp", runtime["captainShopBuffStatusPending"])
        self.assertFalse((c.account_state.get("StatusContainer") or {}).get("List"))

    def test_missing_buff_status_after_confirmed_buy_waits_for_status_only(self):
        record = {"StaticID": "GuildHeroExp", "Store": "CaptainShop", "BuyCount": 0}
        returned = {**record, "BuyCount": 1}
        c = captain_store_client([captain_store_guild([record]), {"CommodityRecord": returned},
                                  {"Event": "None", "AccountStatusList": []}])
        runtime = {}

        with self.assertRaises(CaptainBuffStatusPending):
            GuildService(c, runtime).buy("GuildHeroExp")

        self.assertEqual(c.call.call_count, 3)
        self.assertIn("GuildHeroExp", runtime["captainShopBuffStatusPending"])
        self.assertEqual(c.account_state["GuildData"]["GuildStoreRecord"]["Records"][0]["BuyCount"], 1)

    def test_full_guild_refresh_notifies_waiters_when_shared_currency_increases(self):
        c = captain_store_client([])
        previous = captain_store_guild([])["GuildData"]
        previous["ItemContainer"]["Items"] = [
            {"StaticID": "80", "Count": 100}, {"StaticID": "81", "Count": 300}]
        c.account_state["GuildData"] = previous
        callback = Mock()
        c.guild_currency_increase_callback = callback
        updated = captain_store_guild([])
        updated["GuildData"]["ItemContainer"]["Items"] = [
            {"StaticID": "80", "Count": 204000}, {"StaticID": "81", "Count": 10}]

        GuildService(c, {})._cache(updated, "GuildHandler.QueryFullGuildData")

        callback.assert_called_once_with({"80"})

    def test_buy_unconfirmed_result_is_not_retried_or_cached(self):
        record = {"_id": {"$oid": "record-id"}, "StaticID": "GuildEnergy50",
                  "Store": "CaptainShop", "BuyCount": 0}
        bad_responses = (
            {},
            {"CommodityRecord": {**record, "BuyCount": 1, "StaticID": "GuildEnergy30"}},
            {"CommodityRecord": {**record, "BuyCount": 0}},
            {"CommodityRecord": {**record, "BuyCount": 1, "_id": {"$oid": "other-id"}}},
        )
        for response in bad_responses:
            with self.subTest(response=response):
                c = captain_store_client([captain_store_guild([record]), response])
                with self.assertRaisesRegex(GameError, "未确认"):
                    GuildService(c).buy("GuildEnergy50")
                self.assertEqual(c.call.call_count, 2)
                self.assertEqual(c.account_state["GuildData"]["GuildStoreRecord"]["Records"][0]["BuyCount"], 0)

    def test_captain_shop_requires_leader_or_deputy_from_fresh_member_list(self):
        for authority in (1, 2):
            with self.subTest(authority=authority):
                returned = {"StaticID": "GuildBox", "Store": "CaptainShop", "BuyCount": 1}
                c = captain_store_client([captain_store_guild([], authority), {"CommodityRecord": returned}])
                GuildService(c).buy("GuildBox")
                self.assertEqual(c.call.call_count, 2)

        for authority in (3, None):
            with self.subTest(authority=authority):
                guild_reply = captain_store_guild([], authority if authority is not None else 1)
                if authority is None:
                    guild_reply["GuildData"]["MemberList"][0]["PlayerGuildInfo"].pop("Authority")
                c = captain_store_client([guild_reply])
                with self.assertRaisesRegex(GameError, "职位未确认|团长和副团长"):
                    GuildService(c).buy("GuildBox")
                self.assertEqual(c.call.call_count, 1)

    def test_captain_shop_fails_closed_when_self_is_missing_from_fresh_roster(self):
        c = captain_store_client([captain_store_guild([], include_member=False)])
        with self.assertRaisesRegex(GameError, "职位未确认"):
            GuildService(c).buy("GuildBox")
        self.assertEqual(c.call.call_count, 1)

    def test_malformed_shop_does_not_send_purchase(self):
        for reply in ({"GuildData": "bad"}, {"GuildStoreRecord": "bad"},
                      {"GuildStoreRecord": {"Records": {}}}):
            with self.subTest(reply=reply):
                c = client([reply])
                with self.assertRaises(GameError):
                    GuildService(c).buy("GuildBox")
                self.assertEqual(c.call.call_count, 1)


class GuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.acc = SimpleNamespace(email="test@example.invalid", client=client([]), lock=threading.Lock())
        self.acc.require_current_day = Mock()
        self.acc._request_scope = lambda **kwargs: nullcontext()
        self.acc.state_revision = 1
        self.acc.guild_runtime = {"enteredRevision": 1}
        self.lookup = patch.object(accounts, "get", return_value=self.acc)
        self.acc.guild_snapshot = Mock(return_value={"joined": True})
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def test_busy_account_rejects_query_without_waiting(self):
        self.acc.lock.acquire()
        try:
            with self.assertRaises(appcore.ApiError) as raised:
                appcore.guild_overview(self.acc.email)
            self.assertEqual(raised.exception.status, 409)
        finally:
            self.acc.lock.release()
        self.acc.client.call.assert_not_called()

    def test_removed_repeat_reward_routes_reject_without_game_requests(self):
        for method, path, body in (
            ("GET", "/api/guild/quests", {}),
            ("POST", "/api/guild/quests/query", {}),
            ("POST", "/api/guild/quests/cancel", {"id": "old-job"}),
            ("POST", "/api/guild/quests/start", {"mode": "score"}),
            ("POST", "/api/guild/quests/start", {"mode": "rounds"}),
            ("POST", "/api/guild/quests/start", {}),
        ):
            with self.subTest(method=method, path=path, body=body):
                with self.assertRaises(appcore.ApiError) as raised:
                    server._route_api(method, path, {"account": self.acc.email, **body},
                                      {"account": self.acc.email})
                self.assertEqual(raised.exception.status, 404)
        self.acc.client.call.assert_not_called()

    def test_business_errors_are_api_errors_and_lock_is_released(self):
        self.acc.client.call.side_effect = GameMessage("CantCheckIn")
        with self.assertRaises(appcore.ApiError) as raised:
            appcore.guild_action(self.acc.email, "attendance")
        self.assertEqual(raised.exception.status, 400)
        self.assertFalse(self.acc.lock.locked())

    def test_action_exposes_cached_status_and_only_supported_actions(self):
        self.acc.client.call.side_effect = [{}]
        with patch.object(appcore, "_status_snapshot", return_value={"status": {"gold": 99}}):
            data = server._route_api("POST", "/api/guild/action", {"account": self.acc.email, "action": "attendance"}, {})
        self.assertEqual(data, {"ok": True, "result": {}, "status": {"gold": 99}})
        for action in ("donate", "leave", "join", "aid", None):
            with self.subTest(action=action), self.assertRaises(appcore.ApiError):
                appcore.guild_action(self.acc.email, action)
        self.assertEqual(self.acc.client.call.call_count, 1)

    def test_manual_guild_refresh_wakes_scheduler_to_recheck_cached_status(self):
        self.acc._wake = threading.Event()
        self.acc.refresh_guild_locked = Mock()
        with patch.object(self.acc, "refresh_guild_locked"), \
                patch.object(appcore, "_status_snapshot", return_value={"guild": {"joined": True}}):
            self.assertEqual(appcore.guild_overview(self.acc.email), {"ok": True, "guild": {"joined": True}})
        self.assertTrue(self.acc._wake.is_set())

    def test_register_closes_old_account_outside_registry_lock_without_starting_tasks(self):
        old, new = Mock(), Mock()
        def close():
            acquired = accounts._reg_lock.acquire(blocking=False)
            self.assertTrue(acquired, "registry lock held while closing account")
            if acquired:
                accounts._reg_lock.release()
            self.assertIs(accounts._accounts["test"], new)
        old.close.side_effect = close
        with patch.object(accounts, "_accounts", {"test": old}), patch.object(accounts, "Account", return_value=new):
            self.assertIs(accounts.add("test", object(), None), new)
        old.close.assert_called_once_with()
        new.start_scheduler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
