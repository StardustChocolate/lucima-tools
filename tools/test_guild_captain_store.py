"""Offline rules and scheduler coverage for captain-shop automation."""
import copy
import csv
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tools.test_guild_aid import AidClient, NOW
from backend import accounts, config
from backend import guild_captain_store
from backend.game_client import GameError, GameMessage
from backend.guild import CAPTAIN_BUFF_STATUS_SYNC_MS, CaptainShopInsufficientCurrency, GuildService
from backend.guild_captain_store import (
    BUFF_REFRESH_MS, CAPTAIN_GOODS,
    GuildCaptainStoreService, captain_store_options,
)


class CaptainGuild:
    def __init__(self, client, runtime=None):
        self.client = client
        self.runtime = runtime if runtime is not None else {}
        self.now = NOW
        self.purchases = []
        self.failure = None

    def now_ms(self):
        return self.now

    def buy(self, identifier, count=1, **_):
        self.purchases.append(identifier)
        if self.failure:
            raise self.failure
        state = self.client.account_state
        records = state["GuildData"]["GuildStoreRecord"]["Records"]
        record = next((row for row in records if row["StaticID"] == identifier), None)
        if record is None:
            record = {"StaticID": identifier, "Store": "CaptainShop", "BuyCount": 0}
            records.append(record)
        record["BuyCount"] += 1
        record["LastResetTime"] = {"$date": self.now}
        if identifier in {"GuildMoneyBonus", "GuildHeroExp"}:
            statuses = state.setdefault("StatusContainer", {}).setdefault("List", [])
            statuses.append({"StaticID": identifier, "EndTime": {"$date": self.now + 12 * 3600 * 1000}})
        return {"CommodityRecord": copy.deepcopy(record)}


def make_client():
    client = AidClient()
    client.account_state["PlayerGuildInfo"]["GID"] = "guild"
    client.account_state["PlayerGuildInfo"]["Authority"] = 1
    client.account_state["GuildData"] = {
        "Info": {"_id": "guild", "Name": "Test"},
        "GuildStoreRecord": {"Records": []},
        "ItemContainer": {"Items": [{"StaticID": "80", "Count": 500000},
                                    {"StaticID": "81", "Count": 1000}]},
    }
    client.account_state["StatusContainer"] = {"List": []}
    return client


class GuildCaptainStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=NOW / 1000).start()
        self.addCleanup(patch.stopall)
        self.client = make_client()
        self.guild = CaptainGuild(self.client)
        self.service = GuildCaptainStoreService(self.guild)

    def rows(self):
        return {row["id"]: row for row in self.service.snapshot()["goods"]}

    def test_android_web_root_does_not_redirect_backend_catalog(self):
        guild_captain_store._catalog.cache_clear()
        self.addCleanup(guild_captain_store._catalog.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            web = Path(directory) / "web"
            web.mkdir()
            with patch.dict("os.environ", {"ARK_ASSET_ROOT": str(web), "ARK_PLATFORM": "android"}):
                rows = self.rows()
        self.assertEqual(set(rows), set(CAPTAIN_GOODS))
        self.assertTrue(rows["GuildMoneyBonus"]["available"])
        self.assertEqual(rows["GuildEnergy50"]["reset"], "Day")
        self.assertEqual(rows["GuildBox"]["reset"], "Week")

    def test_empty_buffs_are_due_and_product_selection_is_filtered(self):
        rows = self.rows()
        self.assertTrue(rows["GuildMoneyBonus"]["available"])
        self.assertTrue(rows["GuildHeroExp"]["available"])
        self.assertEqual(captain_store_options({"items": ["GuildBox", "unknown", "GuildBox"]}), ["GuildBox"])

    def test_shared_currency_balances_use_guild_inventory_instead_of_personal_inventory(self):
        self.client.account_state["ItemContainer"]["Items"].extend([
            {"StaticID": "80", "Count": 999999}, {"StaticID": "81", "Count": 999}])
        self.assertEqual(self.service.snapshot()["currencies"], {"80": 500000, "81": 1000})
        self.assertEqual(self.rows()["GuildEnergy30"]["balance"], 500000)
        self.client.account_state["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 0
        self.assertEqual(self.service.snapshot()["currencies"]["81"], 0)
        self.client.account_state["GuildData"].pop("ItemContainer")
        self.assertEqual(self.service.snapshot()["currencies"], {"80": None, "81": None})

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "assets_full/文本/数据表/sd_Commodity.txt").is_file(),
                         "Optional extracted game tables are not distributed")
    def test_captain_prices_and_currencies_match_official_commodity_table(self):
        path = Path(__file__).resolve().parents[1] / "assets_full/文本/数据表/sd_Commodity.txt"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            official = {row["ID"]: row for row in csv.DictReader(stream, delimiter="@")
                        if row["Store"] == "CaptainShop"}
        self.assertEqual(set(CAPTAIN_GOODS), set(official))
        for identifier, meta in CAPTAIN_GOODS.items():
            self.assertEqual(meta["currency"], official[identifier]["PriceItemID"])
            self.assertEqual(meta["price"], int(official[identifier]["Price"]))

    def test_buff_threshold_is_strict_and_expiry_is_scheduled(self):
        self.client.account_state["StatusContainer"]["List"] = [{
            "StaticID": "GuildMoneyBonus", "EndTime": {"$date": NOW + BUFF_REFRESH_MS},
        }]
        rows = self.rows()
        self.assertFalse(rows["GuildMoneyBonus"]["eligible"])
        delay = self.service.next_delay({"items": ["GuildMoneyBonus"]})
        self.assertGreater(delay, 0)
        self.guild.now += 2
        self.assertTrue(self.rows()["GuildMoneyBonus"]["available"])

    def test_insufficient_shared_currency_waits_for_balance_and_does_not_become_retry(self):
        self.client.account_state["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 10
        rows = self.rows()
        self.assertTrue(rows["GuildBox"]["conditionMet"])
        self.assertFalse(rows["GuildBox"]["affordable"])
        self.assertFalse(rows["GuildBox"]["available"])
        delay = self.service.next_delay({"items": ["GuildBox"]})
        self.assertIsNone(delay)
        self.assertEqual(self.guild.runtime["captainShopWatchCurrencies"], {"81"})

        self.guild.now += 10 * 60 * 1000
        self.assertIsNone(self.service.next_delay({"items": ["GuildBox"]}))
        self.client.account_state["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 300
        self.assertEqual(self.service.next_delay({"items": ["GuildBox"]}), 0)

    def test_buff_status_sync_pending_blocks_repurchase_but_schedules_status_refresh(self):
        self.guild.runtime["captainShopBuffStatusPending"] = {"GuildMoneyBonus": {"afterMs": NOW}}
        self.guild.runtime["captainShopStatusSyncRetryAtMs"] = {
            "GuildMoneyBonus": int(time.time() * 1000) + CAPTAIN_BUFF_STATUS_SYNC_MS}
        row = self.rows()["GuildMoneyBonus"]
        self.assertTrue(row["statusPending"])
        self.assertTrue(row["blocked"])
        self.assertFalse(row["available"])
        self.assertEqual(self.service.next_delay({"items": ["GuildMoneyBonus"]}),
                         CAPTAIN_BUFF_STATUS_SYNC_MS / 1000)

    def test_daily_and_weekly_limits_follow_reset_records(self):
        records = self.client.account_state["GuildData"]["GuildStoreRecord"]["Records"]
        records.extend([
            {"StaticID": "GuildEnergy50", "Store": "CaptainShop", "BuyCount": 1,
             "LastResetTime": {"$date": NOW}},
            {"StaticID": "GuildBox", "Store": "CaptainShop", "BuyCount": 1,
             "LastResetTime": {"$date": NOW - 8 * 24 * 3600 * 1000}},
        ])
        rows = self.rows()
        self.assertEqual(rows["GuildEnergy50"]["remaining"], 0)
        self.assertEqual(rows["GuildBox"]["remaining"], 1)

    def test_bad_or_uncertain_item_does_not_block_other_goods(self):
        self.guild.runtime["captainShopUncertain"] = {"GuildMoneyBonus": True}
        rows = self.rows()
        self.assertTrue(rows["GuildMoneyBonus"]["blocked"])
        self.assertTrue(rows["GuildBox"]["available"])

    def test_nonprivileged_member_cannot_auto_buy_or_schedule_purchases(self):
        self.client.account_state["PlayerGuildInfo"]["Authority"] = 3
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["access"]["allowed"])
        self.assertIn("仅限团长和副团长", snapshot["access"]["reason"])
        self.assertTrue(all(not row["eligible"] and not row["available"] for row in snapshot["goods"]))
        self.assertIsNone(self.service.next_delay({"items": ["GuildBox"]}))
        with self.assertRaisesRegex(GameError, "仅限团长和副团长"):
            self.service.buy("GuildBox")
        self.assertEqual(self.guild.purchases, [])

    def test_missing_authority_disables_captain_shop(self):
        self.client.account_state["PlayerGuildInfo"].pop("Authority")
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["access"]["allowed"])
        self.assertIn("职位信息未确认", snapshot["access"]["reason"])
        self.assertTrue(all(not row["available"] for row in snapshot["goods"]))

    def test_malformed_buff_expiry_is_reported_without_buying(self):
        self.client.account_state["StatusContainer"]["List"] = [{"StaticID": "GuildMoneyBonus"}]
        row = self.rows()["GuildMoneyBonus"]
        self.assertIn("未确认", row["error"])
        self.assertFalse(row["eligible"])

    def test_scheduler_buys_only_selected_goods_and_stops_at_limits(self):
        with patch.object(config, "load_accounts", return_value={}), \
                patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 0, "jitter": 0}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        fake_guild = CaptainGuild(self.client, acc.guild_runtime)
        fake_guild.now = int(time.time() * 1000)
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildMoneyBonus", "GuildEnergy50", "GuildBox"]}
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._store_captain_guild_locked()
            acc._store_captain_guild_locked()
        self.assertEqual(fake_guild.purchases, ["GuildMoneyBonus", "GuildEnergy50", "GuildBox"])

    def test_scheduler_waits_for_shared_balance_instead_of_retrying_purchase(self):
        with patch.object(config, "load_accounts", return_value={}), \
                patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 0, "jitter": 0}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        fake_guild = CaptainGuild(self.client, acc.guild_runtime)
        fake_guild.now = int(time.time() * 1000)
        fake_guild._call = Mock(side_effect=AssertionError("No balance polling"))
        self.client.account_state["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 10
        acc.guild_runtime["enteredRevision"] = acc.state_revision
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildBox"]}
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._run_guild_if_due()
            self.assertEqual(fake_guild.purchases, [])
            self.assertFalse(acc.guild_runtime.get("captainShopRetryAtMs"))
            self.assertEqual(acc.guild_runtime["captainShopWatchCurrencies"], {"81"})
            self.assertIsNone(acc._guild_due_delay())

            self.clock.return_value += 10 * 60
            fake_guild.now += 10 * 60 * 1000
            acc._run_guild_if_due()
            fake_guild._call.assert_not_called()
            self.assertEqual(fake_guild.purchases, [])
            self.assertEqual(acc.logs_since(0), [])

            cache = GuildService(self.client, acc.guild_runtime)
            refreshed = copy.deepcopy(self.client.account_state["GuildData"])
            refreshed["ItemContainer"]["Items"][1]["Count"] = 20
            acc._wake.clear()
            cache._cache({"GuildData": refreshed}, "GuildHandler.QueryFullGuildData")
            self.assertTrue(acc._wake.is_set())
            acc._run_guild_if_due()
            self.assertEqual(fake_guild.purchases, [])
            self.assertFalse(acc.guild_runtime.get("captainShopRetryAtMs"))

            acc._wake.clear()
            cache._cache({"GuildDropResult": {"Items": [
                {"NowItem": {"StaticID": "81", "Count": 300}}]}})
            self.assertTrue(acc._wake.is_set())
            acc._run_guild_if_due()
            self.assertEqual(acc.guild_runtime["captainShopWatchCurrencies"], set())
            acc._wake.clear()
            cache._cache({"GuildDropResult": {"Items": [
                {"NowItem": {"StaticID": "81", "Count": 400}}]}})
            self.assertFalse(acc._wake.is_set())
        self.assertEqual(fake_guild.purchases, ["GuildBox"])

    def test_personal_and_shared_balance_notifications_are_independent(self):
        with patch.object(config, "load_accounts", return_value={}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        acc.toggles.update(guild_auto_store=True, guild_auto_captain_store=True)
        acc.guild_runtime.update(shopWatchCurrencies={"29"}, captainShopWatchCurrencies={"81"})
        for notify, currency, wakes in (
                (self.client.inventory_increase_callback, "81", False),
                (self.client.inventory_increase_callback, "29", True),
                (self.client.guild_currency_increase_callback, "29", False),
                (self.client.guild_currency_increase_callback, "80", False),
                (self.client.guild_currency_increase_callback, "81", True)):
            acc._wake.clear()
            notify({currency})
            self.assertEqual(acc._wake.is_set(), wakes)
        acc.guild_runtime["captainShopRetryAtMs"] = {"GuildBox": NOW + accounts.TASK_RETRY_MS}
        self.client.guild_currency_increase_callback({"81"})
        self.assertEqual(acc.guild_runtime["captainShopRetryAtMs"]["GuildBox"], NOW + accounts.TASK_RETRY_MS)
        acc.toggles["guild_auto_captain_store"] = False
        acc._wake.clear()
        self.client.guild_currency_increase_callback({"81"})
        self.assertFalse(acc._wake.is_set())

    def test_pending_buff_status_query_runs_when_scheduled_without_rebuying(self):
        with patch.object(config, "load_accounts", return_value={}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildMoneyBonus"]}
        acc.guild_runtime.update(
            enteredRevision=acc.state_revision,
            captainShopBuffStatusPending={"GuildMoneyBonus": {"afterMs": NOW}},
            captainShopStatusSyncRetryAtMs={"GuildMoneyBonus": NOW + CAPTAIN_BUFF_STATUS_SYNC_MS})
        fake_guild = CaptainGuild(self.client, acc.guild_runtime)
        cache = GuildService(self.client, acc.guild_runtime)
        status = {"_id": {"$oid": "buff-status"}, "StaticID": "GuildMoneyBonus",
                  "EndTime": {"$date": NOW + 12 * 3600 * 1000}}
        fake_guild._call = Mock(side_effect=lambda route: cache._cache({"AccountStatusList": [status]}, route))
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._run_guild_if_due()
            fake_guild._call.assert_not_called()
            self.clock.return_value += CAPTAIN_BUFF_STATUS_SYNC_MS / 1000
            fake_guild.now += CAPTAIN_BUFF_STATUS_SYNC_MS
            acc._run_guild_if_due()
        fake_guild._call.assert_called_once_with("ServerStatusHandler.QueryGuildStatus")
        self.assertEqual(fake_guild.purchases, [])
        self.assertFalse(acc.guild_runtime.get("captainShopBuffStatusPending"))

    def test_fresh_balance_rejection_is_reclassified_as_waiting_not_five_minute_retry(self):
        with patch.object(config, "load_accounts", return_value={}), \
                patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 0, "jitter": 0}):
            acc = accounts.Account("captain@example.invalid", self.client, None)

        class StaleBalanceGuild(CaptainGuild):
            def buy(self, identifier, count=1, **_):
                self.purchases.append(identifier)
                self.client.account_state["GuildData"]["ItemContainer"]["Items"][1]["Count"] = 10
                raise CaptainShopInsufficientCurrency("81", 10, 300)

        fake_guild = StaleBalanceGuild(self.client, acc.guild_runtime)
        fake_guild.now = int(time.time() * 1000)
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildBox"]}
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._store_captain_guild_locked()

        self.assertEqual(fake_guild.purchases, ["GuildBox"])
        self.assertFalse(acc.guild_runtime.get("captainShopRetryAtMs"))
        self.assertFalse(acc.guild_runtime.get("captainShopUncertain"))
        self.assertEqual(acc.guild_runtime["captainShopWatchCurrencies"], {"81"})

    def test_known_purchase_rejection_waits_before_retry(self):
        with patch.object(config, "load_accounts", return_value={}), \
                patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 0, "jitter": 0}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        fake_guild = CaptainGuild(self.client, acc.guild_runtime)
        fake_guild.now = int(time.time() * 1000)
        fake_guild.failure = GameMessage("OverCount")
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildBox"]}
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._store_captain_guild_locked()
            self.assertEqual(len(fake_guild.purchases), 1)
            retry_at = acc.guild_runtime["captainShopRetryAtMs"]["GuildBox"]
            self.assertGreaterEqual(retry_at, fake_guild.now + accounts.TASK_RETRY_MS)
            self.assertLessEqual(retry_at, fake_guild.now + accounts.TASK_RETRY_MS + 1000)
            self.assertEqual(acc.guild_runtime.get("captainShopUncertain"), None)

    def test_uncertain_purchase_blocks_only_that_item_until_login(self):
        with patch.object(config, "load_accounts", return_value={}), \
                patch.object(config, "get_request_delay_config", return_value={"enabled": False, "ms": 0, "jitter": 0}):
            acc = accounts.Account("captain@example.invalid", self.client, None)
        fake_guild = CaptainGuild(self.client, acc.guild_runtime)
        fake_guild.now = int(time.time() * 1000)
        fake_guild.failure = RuntimeError("reply lost after commit")
        acc.toggles["guild_auto_captain_store"] = True
        acc.params["guild_auto_captain_store"] = {"items": ["GuildMoneyBonus", "GuildBox"]}
        with patch("backend.accounts.GuildService", side_effect=lambda client, runtime=None, *args, **kwargs:
                   _attach_runtime(fake_guild, runtime)):
            acc._store_captain_guild_locked()
            acc._store_captain_guild_locked()
        self.assertEqual(fake_guild.purchases, ["GuildMoneyBonus", "GuildBox"])
        self.assertTrue(acc.guild_runtime["captainShopUncertain"]["GuildMoneyBonus"])


def _attach_runtime(guild, runtime):
    guild.runtime = runtime if runtime is not None else guild.runtime
    return guild


if __name__ == "__main__":
    unittest.main()
