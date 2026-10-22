"""Offline guild protocol, job and account lifecycle regression checks."""
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import accounts, appcore, config, server
from backend.game_client import GameClient, GameError, GameMessage
from backend.guild import (CaptainBuffStatusPending, CaptainShopInsufficientCurrency,
                           GUILD_ROUTES, GuildService,
                           commodity_record, quest_response_summary)
from backend.guild_jobs import GuildJobManager


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
        "Info": {"_id": {"$oid": "player-id"}, "CUID": 101},
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
        c.account_state = {"PlayerGuildInfo": {"GID": "guild-id", "LastCheckInTime": {"$date": 0}},
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


class GuildJobTests(unittest.TestCase):
    def setUp(self):
        self.acc = SimpleNamespace(email="test@example.invalid", client=client([]), lock=threading.Lock())
        self.registry = {self.acc.email: self.acc}
        self.service = Mock()
        self.service.quest_ids.return_value = {"guildName": "Test", "questIds": ["one", "two"]}
        self.service.claim_quest.return_value = {}
        self.manager = GuildJobManager(service_factory=lambda _: self.service,
                                       account_lookup=self.registry.get, interval=0)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.manager.cancel(self.acc.email)
        self.wait_finished()

    def wait_finished(self, job_id=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.manager.status(self.acc.email, job_id)
            if not result["running"] and self.acc.email not in self.manager._active:
                return result
            time.sleep(.005)
        self.fail("guild worker failed to stop")

    def test_ordered_rounds_snapshot_once_and_detached_status(self):
        self.manager.start_rewards(self.acc, rounds=2)
        result = self.wait_finished()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["noDetails"], 4)
        self.assertEqual([call.args[0] for call in self.service.claim_quest.call_args_list],
                         ["one", "two", "one", "two"])
        self.service.quest_ids.assert_called_once_with()
        result["results"].clear()
        self.assertEqual(len(self.manager.status(self.acc.email)["results"]), 4)

    def test_business_rejection_continues_but_auth_and_transport_stop(self):
        self.service.claim_quest.side_effect = [GameMessage("OverCount"), {}]
        self.manager.start_rewards(self.acc, rounds=1)
        result = self.wait_finished()
        self.assertEqual((result["state"], result["failed"], result["noDetails"]), ("completed", 1, 1))
        for error in (GameMessage("Duplicate Login"), GameMessage("invalid token"), OSError("network down")):
            self.service.claim_quest.reset_mock(side_effect=True)
            self.service.claim_quest.side_effect = error
            self.manager.start_rewards(self.acc, rounds=1)
            result = self.wait_finished()
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["attempted"], 1)
            self.service.claim_quest.assert_called_once_with("one")

    def test_cancel_waiting_for_account_lock_sends_no_request(self):
        self.acc.lock.acquire()
        try:
            job = self.manager.start_rewards(self.acc, rounds=1)
            self.assertEqual(self.manager.cancel(self.acc.email, job["id"])["state"], "stopping")
        finally:
            self.acc.lock.release()
        self.assertEqual(self.wait_finished()["state"], "cancelled")
        self.service.quest_ids.assert_not_called()

    def test_cancel_during_request_waits_for_reply_and_stops_next_request(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def claim(_):
            entered.set()
            release.wait(2)
            return {"Results": [{"HasMail": True}]}
        self.service.claim_quest.side_effect = claim
        job = self.manager.start_rewards(self.acc, rounds=2)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.manager.cancel(self.acc.email, job["id"])["state"], "stopping")
            self.assertEqual(self.manager.status(self.acc.email)["completed"], 0)
        finally:
            release.set()
        result = self.wait_finished()
        self.assertEqual((result["state"], result["succeeded"]), ("cancelled", 1))
        self.service.claim_quest.assert_called_once_with("one")

    def test_cancel_interrupts_configured_request_delay(self):
        self.manager._interval = None
        with patch.object(config, "get_request_delay_config", return_value={"enabled": True, "ms": 60000, "jitter": 0}):
            self.manager.start_rewards(self.acc, rounds=1)
            deadline = time.monotonic() + 1
            while self.manager.status(self.acc.email)["completed"] == 0 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(self.manager.status(self.acc.email)["completed"], 1)
            acquired = self.acc.lock.acquire(timeout=.5)
            self.assertTrue(acquired, "request spacing must not block scheduled account tasks")
            if acquired:
                self.acc.lock.release()
            self.manager.cancel(self.acc.email)
            self.assertEqual(self.wait_finished()["state"], "cancelled")
        self.service.claim_quest.assert_called_once_with("one")

    def test_each_game_call_owns_account_lock(self):
        def listing():
            self.assertTrue(self.acc.lock.locked())
            return {"questIds": ["one"]}
        def claim(_):
            self.assertTrue(self.acc.lock.locked())
            return {}
        self.service.quest_ids.side_effect = listing
        self.service.claim_quest.side_effect = claim
        self.manager.start_rewards(self.acc, rounds=1)
        self.assertEqual(self.wait_finished()["state"], "completed")
        self.assertFalse(self.acc.lock.locked())

    def test_same_request_is_not_replayed_and_concurrent_job_is_rejected(self):
        request_id = "12345678-1234-1234-1234-123456789abc"
        self.acc.lock.acquire()
        try:
            first = self.manager.start_rewards(self.acc, rounds=1, requestId=request_id)
            second = self.manager.start_rewards(self.acc, rounds=99, requestId=request_id)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["rounds"], 1)
            with self.assertRaises(GameError):
                self.manager.start_rewards(self.acc, rounds=1)
        finally:
            self.acc.lock.release()
        self.wait_finished()
        self.manager.start_rewards(self.acc, rounds=1, requestId=request_id)
        self.assertEqual(self.service.claim_quest.call_count, 2)

    def test_session_replacement_stops_worker_and_rejects_stale_start(self):
        self.acc.lock.acquire()
        try:
            self.manager.start_rewards(self.acc, rounds=1)
            self.registry[self.acc.email] = object()
        finally:
            self.acc.lock.release()
        self.assertEqual(self.wait_finished()["state"], "failed")
        self.service.claim_quest.assert_not_called()
        with self.assertRaises(GameError):
            self.manager.start_rewards(self.acc, rounds=1)

    def test_disconnected_account_never_sends_requests(self):
        self.acc.client.is_connected.return_value = False
        self.manager.start_rewards(self.acc, rounds=1)
        self.assertEqual(self.wait_finished()["state"], "failed")
        self.service.quest_ids.assert_not_called()

    def test_result_and_history_limits_keep_request_deduplication(self):
        request_id = "12345678-1234-1234-1234-123456789abc"
        self.service.quest_ids.return_value = {"questIds": ["one", "two", "three"]}
        self.manager.start_rewards(self.acc, rounds=100, requestId=request_id)
        result = self.wait_finished()
        self.assertEqual(result["completed"], 300)
        self.assertEqual(len(result["results"]), 200)
        self.assertEqual(len(result["logs"]), 100)
        for _ in range(10):
            self.manager.start_rewards(self.acc, rounds=1)
            self.wait_finished()
        self.assertEqual(len(self.manager._jobs), 10)
        self.assertEqual(self.manager._cancel_events, {})
        before = self.service.claim_quest.call_count
        self.assertEqual(self.manager.start_rewards(self.acc, rounds=1, requestId=request_id)["state"], "unknown")
        self.assertEqual(self.service.claim_quest.call_count, before)

    def test_invalid_rounds_and_request_ids_never_start(self):
        for rounds in (0, 101, True, 1.5, "bad"):
            with self.subTest(rounds=rounds), self.assertRaises(GameError):
                self.manager.start_rewards(self.acc, rounds=rounds)
        with self.assertRaises(GameError):
            self.manager.start_rewards(self.acc, requestId="not-a-uuid")
        self.service.quest_ids.assert_not_called()


class GuildIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.acc = SimpleNamespace(email="test@example.invalid", client=client([]), lock=threading.Lock())
        self.lookup = patch.object(accounts, "get", return_value=self.acc)
        self.acc.guild_snapshot = Mock(return_value={"joined": True})
        self.lookup.start()
        self.addCleanup(self.lookup.stop)

    def test_busy_account_rejects_query_without_waiting_and_status_remains_available(self):
        self.acc.lock.acquire()
        try:
            with self.assertRaises(appcore.ApiError) as raised:
                appcore.guild_overview(self.acc.email)
            self.assertEqual(raised.exception.status, 409)
            with patch.object(appcore.guild_jobs, "status", return_value={"state": "running"}):
                self.assertEqual(server._route_api("GET", "/api/guild/quests", {}, {"account": self.acc.email}),
                                 {"state": "running", "guild": {"joined": True}})
        finally:
            self.acc.lock.release()
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

    def test_status_and_cancel_available_after_duplicate_login(self):
        self.acc.client.connection_status.return_value = {"connected": False, "reason": "duplicate_login"}
        with patch.object(appcore.guild_jobs, "status", return_value={"state": "failed"}), \
                patch.object(appcore.guild_jobs, "cancel", return_value={"state": "stopping"}):
            self.assertEqual(appcore.guild_quests_status(self.acc.email)["state"], "failed")
            self.assertEqual(appcore.guild_quests_cancel(self.acc.email, "job")["state"], "stopping")
        with self.assertRaises(appcore.ApiError) as raised:
            appcore.guild_overview(self.acc.email)
        self.assertEqual(raised.exception.status, 409)

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
