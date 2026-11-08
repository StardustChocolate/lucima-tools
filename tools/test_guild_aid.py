"""Offline aid protocol and scheduler tests, with game-shaped mutation replies."""
import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix="lucima-aid-tests-")


def _cleanup():
    logger = logging.getLogger("lucima")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(Path(_data.name)):
            logger.removeHandler(handler)
            handler.close()
    _data.cleanup()


unittest.addModuleCleanup(_cleanup)
with patch.dict(os.environ, {"ARK_DATA_DIR": _data.name}):
    from backend import accounts, config

NOW = 1792116000000
from backend.game_client import GameClient, GameError
from backend.guild import GUILD_ENTRY_ROUTES, GuildService
from backend.guild_aid import AID_POLL_MS, DAY_MS, GuildAidService, aid_catalog, aid_options
from tools import build_guild_aid


def aid_record(identifier, item="CR54", requester=202, count=0, supporters=None):
    return {"_id": {"$oid": identifier}, "GID": "guild", "ItemID": item,
            "Requester": {"CUID": requester}, "NowCount": count, "SupporterList": supporters or []}


class AidClient(GameClient):
    def __init__(self):
        self.aid, self.session_id = "local-player", "local-session"
        self.connected = True
        self.now = NOW
        self.info = {"CUID": 101, "Name": "Local"}
        self.own = {"GID": "guild", "LastCheckInTime": {"$date": NOW},
                    "LastRequestGuildAidTime": {"$date": NOW - DAY_MS},
                    "DaySupportGuildAidRewardCount": 0, "CanLastDayCheckReward": False,
                    "DayDonateGoldCount": 0, "DayDonateCourageCount": 0}
        self.balance = {"CR54": 8, "R008": 3, "CR11": 8, "C001": 5, "30": 0}
        self.records = []
        self.omit_empty_aid_list = False
        self.account_state = {"Info": copy.deepcopy(self.info), "PlayerGuildInfo": copy.deepcopy(self.own),
                              "Utc": str(NOW // 1000), "ItemContainer": {"Items": [
                                  {"StaticID": k, "Count": v} for k, v in self.balance.items()]}}
        self.account_state["ActivityDBSettingContainer"] = {"SettingList": [
            {"ActivityID": "Version_GVG", "IsIgnoreTime": True}]}
        self.calls = []
        self.fail_query = False
        self.fail_support = False
        self.fail_request = False
        self.bad_support = False
        self.after_support = None
        self.on_request = None

    def is_connected(self):
        return self.connected

    def full(self):
        result = {"Info": {"_id": "guild", "Name": "Guild", "GuildLV": 18}, "Record": {"LastCheckInCount": 1},
                  "MemberList": [{"PlayerInfo": self.info, "PlayerGuildInfo": copy.deepcopy(self.own)}],
                  "GuildAidItemInfoList": copy.deepcopy(self.records)}
        if self.omit_empty_aid_list and not self.records:
            result.pop("GuildAidItemInfoList")
        return result

    def call(self, route, data=None):
        self.calls.append((route, copy.deepcopy(data)))
        result = {"Utc": str(self.now // 1000)}
        if route == "AccountHandler.QueryAccountInfo":
            result["Info"] = self.info
        elif route in ("GuildHandler.QueryFullGuildData", "GuildHandler.QueryGuildDataForSync"):
            if self.fail_query:
                self.fail_query = False
                raise GameError("query timed out")
            result["GuildData"] = self.full()
            # The captured SelfGuildInfo is a default object, not the live member.
            result["SelfGuildInfo"] = {"DaySupportGuildAidRewardCount": 0}
        elif route == "GuildHandler.GuildMemberCheckIn":
            self.own.update(LastCheckInTime={"$date": self.now}, DaySupportGuildAidRewardCount=0)
            result["PlayerGuildData"] = {"PlayerGuildInfo": copy.deepcopy(self.own)}
        elif route == "GuildHandler.RequestGuildAid":
            assert self.own["LastRequestGuildAidTime"]["$date"] // DAY_MS != self.now // DAY_MS
            self.own["LastRequestGuildAidTime"] = {"$date": self.now}
            record = aid_record("our-request-" + str(self.now // DAY_MS), data["ItemID"], 101)
            self.records.append(record)
            result["GuildAidItemInfo"] = copy.deepcopy(record)
            if self.on_request:
                self.on_request()
            if self.fail_request:
                self.fail_request = False
                raise GameError("request timed out after commit")
        elif route == "GuildHandler.SupportGuildAid":
            record = next(row for row in self.records if row["_id"]["$oid"] == data["GuildAidItemInfoID"])
            rule = aid_catalog()["rules"][aid_catalog()["items"][record["ItemID"]]["rule"]]
            item = record["ItemID"]
            assert 101 not in record["SupporterList"]
            assert record["NowCount"] + rule["cost"] <= rule["max"]
            assert self.own["DaySupportGuildAidRewardCount"] + rule["reward"] <= 200
            assert self.balance[item] >= rule["cost"]
            record["SupporterList"].append(101)
            record["NowCount"] += rule["cost"]
            self.balance[item] -= rule["cost"]
            self.balance["30"] += rule["reward"]
            self.own["DaySupportGuildAidRewardCount"] += rule["reward"]
            result.update(GuildAidItemInfo=copy.deepcopy(record),
                          CostItems=[{"Item": {"StaticID": item, "Count": rule["cost"]},
                                      "NowItem": {"StaticID": item, "Count": self.balance[item]}}],
                          Drop={"Items": [{"Item": {"StaticID": "30", "Count": rule["reward"]},
                                           "NowItem": {"StaticID": "30", "Count": self.balance["30"]}}]})
            if self.after_support:
                self.after_support()
            if self.fail_support:
                self.fail_support = False
                raise GameError("support timed out after commit")
            if self.bad_support:
                result["GuildAidItemInfo"] = None
        elif route not in {r for _, r, _ in GUILD_ENTRY_ROUTES}:
            raise AssertionError(route)
        self._apply_now_items(result)
        return result

    def routes(self, suffix):
        return [data for route, data in self.calls if route.endswith(suffix)]


class GuildAidTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW / 1000).start()
        self.addCleanup(patch.stopall)
        patch.object(config, "load_accounts", return_value={}).start()
        patch.object(config, "save_account").start()
        self.delay = patch.object(config, "get_request_delay_config", return_value={
            "enabled": False, "ms": 300, "jitter": 20}).start()
        patch("httpx.Client.request", side_effect=AssertionError("No live requests")).start()
        self.client = AidClient()
        self.acc = accounts.Account("aid@example.invalid", self.client, None)
        self.acc.toggles["guild_auto_aid"] = True
        self.acc.params["guild_auto_aid"] = {"request": True, "item": "CR54", "hunt": True, "element": True}
        self.aid = GuildAidService(GuildService(self.client, self.acc.guild_runtime))

    def run_aid(self):
        self.acc._run_guild_if_due()

    def test_login_requests_once_supports_allowed_items_and_merges_balances(self):
        self.client.records = [aid_record("h1"), aid_record("h2"), aid_record("e1", "R008"),
                               aid_record("c1", "C001")]
        self.run_aid()
        self.assertEqual(len(self.client.calls), 9)  # 5 entry + request + 3 supports
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["h1", "h2", "e1"])
        self.assertEqual(self.acc.guild_snapshot()["aid"]["reward"], 80)
        self.assertTrue(self.acc.guild_snapshot()["aid"]["requested"])
        balances = {r["StaticID"]: r["Count"] for r in self.client.account_state["ItemContainer"]["Items"]}
        self.assertEqual((balances["CR54"], balances["R008"], balances["30"]), (4, 2, 80))
        self.run_aid()
        self.assertEqual(len(self.client.calls), 9)
        self.assertEqual(self.acc._enabled_ids(), [])
        self.assertEqual(self.client.calls[5][1], {"AID": "local-player", "SessionID": "local-session", "ItemID": "CR54"})
        self.assertIn("3 次", self.acc.logs_since(0)[-1]["result"]["detail"])

    def test_self_full_duplicate_foreign_and_insufficient_records_are_skipped(self):
        self.client.records = [aid_record("self", requester=101), aid_record("full", count=8),
                               aid_record("helped", supporters=[101]), aid_record("foreign"),
                               aid_record("unknown", "R003"), aid_record("empty", "CR14"),
                               aid_record("valid", "R008")]
        self.client.records[3]["GID"] = "another-guild"
        self.run_aid()
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["valid"])

    def test_reward_limit_checks_each_reward_and_can_fill_last_ten(self):
        self.client.own["DaySupportGuildAidRewardCount"] = 190
        self.client.records = [aid_record("too-much"), aid_record("also-too-much", "R008"), aid_record("last-ten", "CR11")]
        self.run_aid()
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["last-ten"])
        self.assertEqual(self.acc.guild_snapshot()["aid"]["reward"], 200)
        self.assertGreater(self.acc._guild_due_delay(), AID_POLL_MS / 1000)

    def test_hourly_query_discovers_new_requests_and_empty_checks_are_silent(self):
        self.run_aid()
        logs = self.acc.log_seq
        self.clock.return_value += 3599
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.clock.return_value += 1
        self.client.now += AID_POLL_MS
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(self.acc.log_seq, logs)
        self.client.records.append(aid_record("new", "R008"))
        self.clock.return_value += 3600
        self.client.now += AID_POLL_MS
        self.run_aid()
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 1)
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)

    def test_external_request_is_respected_and_selection_waits_until_next_day(self):
        self.client.own["LastRequestGuildAidTime"] = {"$date": NOW}
        self.run_aid()
        self.acc.params["guild_auto_aid"]["item"] = "R008"
        self.acc.notify_config_changed()
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 0)
        self.client.now += DAY_MS
        self.clock.return_value += DAY_MS / 1000
        self.run_aid()
        self.assertEqual(len(self.client.routes("GuildMemberCheckIn")), 1)
        self.assertEqual([row["ItemID"] for row in self.client.routes("RequestGuildAid")], ["R008"])
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)

    def test_omitted_empty_list_stays_silent_and_polls_hourly(self):
        self.client.omit_empty_aid_list = True
        self.acc.params["guild_auto_aid"]["item"] = ""
        self.run_aid()
        self.assertTrue(self.acc.guild_runtime["aidSynced"])
        self.assertIsNone(self.acc.guild_snapshot()["aid"]["error"])
        self.assertEqual(self.acc._guild_due_delay(), 3600)
        logs = self.acc.log_seq
        self.clock.return_value += 300
        self.client.now += 300_000
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.clock.return_value += 3300
        self.client.now += 3_300_000
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(self.acc.log_seq, logs)
        self.assertEqual(self.acc._guild_due_delay(), 3600)
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 0)
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 0)

    def test_omitted_empty_list_allows_first_request_without_extra_query(self):
        self.client.omit_empty_aid_list = True
        self.run_aid()
        self.assertTrue(self.acc.guild_snapshot()["aid"]["requested"])
        self.assertIsNone(self.acc.guild_snapshot()["aid"]["error"])
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 0)
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)

    def test_retry_recovers_when_query_omits_empty_list(self):
        self.client.omit_empty_aid_list = True
        self.acc.toggles["guild_auto_aid"] = False
        self.run_aid()
        self.acc.guild_runtime.update(aidSynced=False, aidError="previous query failed",
                                     aidRetryAtMs=NOW + 300_000)
        self.acc.toggles["guild_auto_aid"] = True
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)
        self.clock.return_value += 300
        self.client.now += 300_000
        self.run_aid()
        self.assertIsNone(self.acc.guild_snapshot()["aid"]["error"])
        self.assertNotIn("aidRetryAtMs", self.acc.guild_runtime)
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)

    def test_malformed_list_or_missing_own_state_still_blocks_aid(self):
        service = GuildService(self.client, self.acc.guild_runtime)
        for value in (None, {}, "", False, 0):
            with self.subTest(value=value):
                data = self.client.full()
                data["GuildAidItemInfoList"] = value
                service._cache({"GuildData": data})
                self.assertFalse(self.aid.note_synced())
                self.assertFalse(self.aid.request_due(aid_options(self.acc.params["guild_auto_aid"])))
                self.assertEqual(self.aid.candidates(aid_options(self.acc.params["guild_auto_aid"])), [])
        data = self.client.full()
        data.pop("GuildAidItemInfoList")
        data["MemberList"] = []
        service._cache({"GuildData": data})
        self.assertFalse(self.aid.note_synced())

    def test_toggle_off_and_no_membership_do_not_poll_or_mutate(self):
        self.acc.toggles["guild_auto_aid"] = False
        self.run_aid()
        self.clock.return_value += 7200
        self.run_aid()
        self.assertEqual(len(self.client.calls), 5)
        self.assertIsNone(self.acc._guild_due_delay())
        self.acc.toggles["guild_auto_aid"] = True
        self.client.account_state["PlayerGuildInfo"]["GID"] = ""
        self.run_aid()
        self.assertEqual(len(self.client.calls), 5)

    def test_options_can_request_only_or_support_one_category(self):
        self.acc.params["guild_auto_aid"].update(request=False, hunt=False)
        self.client.records = [aid_record("h"), aid_record("e", "R008")]
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 0)
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["e"])
        self.acc.params["guild_auto_aid"].update(request=True, hunt=False, element=False)
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)

    def test_enabling_uses_fresh_cache_without_repeating_entry(self):
        self.acc.toggles["guild_auto_aid"] = False
        self.run_aid()
        self.acc.toggles["guild_auto_aid"] = True
        self.acc.notify_config_changed()
        self.run_aid()
        self.assertEqual(len(self.client.calls), 6)
        self.assertEqual(len(self.client.routes("QueryFullGuildData")), 1)
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 0)

    def test_catalyst_only_supports_and_keeps_hourly_checks_until_reward_limit(self):
        self.acc.params["guild_auto_aid"].update(request=False, hunt=False, element=False, catalyst=True)
        self.client.records = [aid_record("h"), aid_record("e", "R008"), aid_record("c1", "C001")]
        self.run_aid()
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["c1"])
        self.assertEqual(self.acc.guild_snapshot()["aid"]["reward"], 40)
        balances = {r["StaticID"]: r["Count"] for r in self.client.account_state["ItemContainer"]["Items"]}
        self.assertEqual(balances["C001"], 4)
        self.assertEqual(self.acc._guild_due_delay(), 3600)
        self.clock.return_value += 3600
        self.client.now += AID_POLL_MS
        self.client.records.extend(aid_record(f"c{i}", "C001") for i in range(2, 7))
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")],
                         ["c1", "c2", "c3", "c4", "c5"])
        self.assertEqual(self.acc.guild_snapshot()["aid"]["reward"], 200)
        self.assertGreater(self.acc._guild_due_delay(), 3600)
        self.acc.params["guild_auto_aid"]["catalyst"] = False
        self.assertIsNone(self.acc._guild_due_delay())

    def test_query_failure_retries_after_five_minutes_without_mutation(self):
        self.run_aid()
        self.clock.return_value += 3600
        self.client.now += AID_POLL_MS
        self.client.fail_query = True
        self.run_aid()
        self.assertAlmostEqual(self.acc._guild_due_delay(), 300)
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.clock.return_value += 300
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 2)
        self.assertIsNone(self.acc.guild_snapshot()["aid"]["error"])

    def test_uncertain_request_queries_before_retry_and_does_not_duplicate_committed_request(self):
        self.client.fail_request = True
        self.run_aid()
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.clock.return_value += 300
        self.run_aid()
        self.assertEqual(len(self.client.routes("QueryGuildDataForSync")), 1)
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.assertTrue(self.acc.guild_snapshot()["aid"]["requested"])

    def test_uncertain_support_reserves_unreturned_cost_and_continues_after_query(self):
        self.client.balance["CR54"] = 2
        self.client.account_state["ItemContainer"]["Items"][0]["Count"] = 2
        self.client.records = [aid_record("uncertain"), aid_record("no-stock"), aid_record("can-help", "R008")]
        self.client.fail_support = True
        self.run_aid()
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 1)
        self.clock.return_value += 300
        self.run_aid()
        self.assertEqual([r["GuildAidItemInfoID"] for r in self.client.routes("SupportGuildAid")], ["uncertain", "can-help"])
        self.assertEqual(self.acc.guild_snapshot()["aid"]["reward"], 50)

    def test_unconfirmed_reply_does_not_repeat_spend_with_stale_record(self):
        self.client.records = [aid_record("bad")]
        self.client.bad_support = True
        self.run_aid()
        self.assertIn("bad", self.acc.guild_runtime["aidUncertain"])
        self.assertFalse(self.acc.guild_runtime["aidUncertain"]["bad"]["reserve"])
        self.run_aid()
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 1)

    def test_disable_during_request_stops_subsequent_supports(self):
        self.client.records = [aid_record("h")]
        self.client.on_request = lambda: self.acc.toggles.update(guild_auto_aid=False)
        self.run_aid()
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 0)

    def test_disable_during_support_stops_batch_and_separate_accounts_have_no_shared_state(self):
        self.client.records = [aid_record("h1"), aid_record("h2")]
        self.client.after_support = lambda: self.acc.toggles.update(guild_auto_aid=False)
        self.run_aid()
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 1)
        other = accounts.Account("other@example.invalid", AidClient(), None)
        self.assertEqual(other.client.calls, [])
        self.assertFalse(other.guild_snapshot()["aid"]["requested"])
        self.assertNotIn("aidSynced", other.guild_runtime)

    def test_unknown_fields_and_unselected_materials_do_not_send_mutations(self):
        self.acc.params["guild_auto_aid"].update(item="bad", hunt=False, element=False)
        self.run_aid()
        self.assertEqual(len(self.client.calls), 5)
        self.assertIsNone(self.acc._guild_due_delay())
        self.acc.params["guild_auto_aid"] = {"request": True, "item": "CR54"}
        del self.client.own["LastRequestGuildAidTime"]
        self.acc.guild_runtime["aidSynced"] = False
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 0)
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 0)
        self.assertIn("缺少", self.acc.guild_snapshot()["aid"]["error"])

    def test_request_interval_is_used_and_disabling_during_wait_stops_the_batch(self):
        self.client.records = [aid_record("h1"), aid_record("h2")]
        self.delay.return_value = {"enabled": True, "ms": 300, "jitter": 0}
        with patch.object(self.acc._stop, "wait", side_effect=lambda seconds: self.acc.toggles.update(guild_auto_aid=False)) as wait:
            self.run_aid()
        wait.assert_called_once_with(.2)
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 0)

    def test_manual_refresh_and_relogin_use_server_state_and_reset_uncertainty(self):
        self.client.records = [aid_record("h")]
        self.run_aid()
        with self.acc.lock:
            self.acc.refresh_guild_locked()
        self.acc.note_state_refreshed()
        self.assertNotIn("aidSynced", self.acc.guild_runtime)
        self.run_aid()
        self.assertEqual(len(self.client.routes("RequestGuildAid")), 1)
        self.assertEqual(len(self.client.routes("SupportGuildAid")), 1)


class GuildAidCatalogTests(unittest.TestCase):
    def test_checked_in_catalog_contains_only_game_aid_groups_and_has_all_local_icons(self):
        catalog = aid_catalog()
        self.assertEqual(len(catalog["items"]), 54)
        self.assertNotIn("R003", catalog["items"])
        self.assertEqual(catalog["rules"][catalog["items"]["CR54"]["rule"]],
                         {"group": "hunt", "max": 8, "cost": 2, "reward": 30})
        for identifier in catalog["items"]:
            self.assertTrue((build_guild_aid.ROOT / "assets/items" / f"{identifier}.png").is_file())
        self.assertEqual(aid_options(None),
                         {"request": True, "item": "", "hunt": True, "element": True, "catalyst": False})

    def test_builder_joins_tags_not_id_prefix_and_refuses_missing_localization(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tables = root / "文本/数据表"
            tables.mkdir(parents=True)
            (tables / "sd_GuildAidItem.txt").write_text("ID@MaxCount@PerSupportCount@RewardCount\nMat4@8@2@30\n", encoding="utf-8")
            (tables / "sd_Item.txt").write_text("ID@Name@Tags\nArbitrary@T_Test@Other~Mat4\nCR54@T_Excluded@Other\n", encoding="utf-8")
            loc = root / "文本/本地化/loc_CHS_FINAL.json"
            loc.parent.mkdir()
            loc.write_text(json.dumps({"T_Test": "原文"}), encoding="utf-8")
            result = build_guild_aid.build(root)
            self.assertEqual(result["items"], {"Arbitrary": {"name": "原文", "rule": "Mat4"}})
            loc.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing guild aid item name"):
                build_guild_aid.build(root)


if __name__ == "__main__":
    unittest.main()
