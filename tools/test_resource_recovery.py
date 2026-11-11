"""Offline natural-recovery checks; no requests or artificial inventory writes."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import resource_recovery as recovery
from backend.game_client import GameClient

SERVER_MS = 1_790_000_000_000
LOCAL_MS = SERVER_MS + 7200_000


def item(sid, count, maximum, last=SERVER_MS):
    return {"StaticID": sid, "Count": count,
            "TimeCollect": {"LastCollectTime": {"$date": last}, "MaxCount": maximum}}


class ResourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = LOCAL_MS
        self.enterContext(patch("backend.game_client.time.time", side_effect=lambda: self.clock / 1000))
        self.enterContext(patch.object(recovery, "intervals", return_value={"3": 300, "4": 3600}))
        self.client = GameClient.__new__(GameClient)
        self.client._replace_account_state({"Utc": SERVER_MS / 1000, "ItemContainer": {"Items": [
            item("3", 30, 60), item("4", 1, 5)]}})

    def test_exact_boundaries_and_missed_ticks_use_server_clock(self):
        original = deepcopy(self.client.account_state)
        version = self.client.inventory_meta()
        self.clock += 299_999
        first = self.client.resource_gauges()
        self.assertEqual(first["stamina"]["current"], 30)
        self.assertEqual(first["stamina"]["recovery"]["nextInMs"], 1)
        self.clock += 1
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 31)
        self.clock = LOCAL_MS + 3600_000
        final = self.client.resource_gauges()
        self.assertEqual(final["stamina"]["current"], 42)
        self.assertEqual(final["flag"]["current"], 2)
        self.assertEqual(self.client.account_state, original)
        self.assertEqual(self.client.inventory_meta(), version)

    def test_cap_and_overflow_do_not_accumulate_recovery(self):
        self.client._replace_account_state({"Utc": SERVER_MS / 1000, "ItemContainer": {"Items": [
            item("3", 80, 60), item("4", 4, 5)]}})
        self.clock += 24 * 3600_000
        gauges = self.client.resource_gauges()
        self.assertEqual(gauges["stamina"]["current"], 80)
        self.assertEqual(gauges["flag"]["current"], 5)
        self.assertNotIn("recovery", gauges["stamina"])
        self.assertNotIn("recovery", gauges["flag"])

    def test_confirmed_spend_reanchors_timer_without_double_counting(self):
        self.clock += 600_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 32)
        self.client._apply_now_items({"CostItems": [{"NowItem": item("3", 20, 60, SERVER_MS + 600_000)}]})
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 20)
        self.clock += 300_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 21)

    def test_reward_above_cap_then_spend_restarts_from_returned_timer(self):
        self.client._apply_now_items({"Drop": {"Items": [{"NowItem": item("3", 70, 60)}]}})
        self.clock += 3600_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 70)
        self.client._apply_now_items({"CostItems": [{"NowItem": item("3", 59, 60, SERVER_MS + 3600_000)}]})
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 59)
        self.clock += 300_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 60)

    def test_partial_balance_without_timer_waits_for_the_next_complete_timer(self):
        self.clock += 600_000
        self.client._apply_now_items({"CostItems": [{"NowItem": {"StaticID": "3", "Count": 20}}]})
        self.clock += 300_000
        value = self.client.resource_gauges()["stamina"]
        self.assertEqual(value["current"], 20)
        self.assertEqual(value["max"], 60)
        self.assertNotIn("recovery", value)
        self.client._apply_now_items({"CostItems": [{"NowItem": item("3", 19, 60, SERVER_MS + 900_000)}]})
        self.clock += 300_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 20)

    def test_disconnect_freezes_and_login_replaces_both_clock_and_count(self):
        self.clock += 300_000
        self.client._mark_disconnected()
        self.clock += 3600_000
        value = self.client.resource_gauges()["stamina"]
        self.assertEqual(value["current"], 31)
        self.assertNotIn("recovery", value)
        self.client._replace_account_state({"Utc": (SERVER_MS + 3900_000) / 1000,
                                           "ItemContainer": {"Items": [item("3", 10, 70, SERVER_MS + 3900_000)]}})
        self.client._mark_connected()
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 10)
        self.clock += 300_000
        self.assertEqual(self.client.resource_gauges()["stamina"]["current"], 11)

    def test_missing_timing_or_invalid_configuration_stays_at_confirmed_count(self):
        for timer in [None, {}, {"MaxCount": 60}, {"MaxCount": 60, "LastCollectTime": "bad"}]:
            with self.subTest(timer=timer):
                self.assertEqual(recovery.gauge({"Count": 30, "TimeCollect": timer}, SERVER_MS, 300)["current"], 30)
        for seconds in [None, 0, -1, True, "300"]:
            self.assertEqual(recovery.gauge(item("3", 30, 60), SERVER_MS + 300_000, seconds)["current"], 30)

    def test_empty_response_corrects_estimate_without_overwriting_server_inventory(self):
        original = deepcopy(self.client.account_state)
        version = self.client.inventory_meta()
        self.clock += 4 * 3600_000
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 5)
        self.client.note_resource_empty("4")
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 0)
        self.clock += 2 * 3600_000
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 2)
        self.assertEqual(self.client.account_state, original)
        self.assertEqual(self.client.inventory_meta(), version)

    def test_authoritative_flag_balance_clears_empty_correction_and_wakes_even_if_raw_did_not_rise(self):
        for count in (2, 5):
            with self.subTest(count=count):
                self.client._replace_account_state({"Utc": SERVER_MS / 1000, "ItemContainer": {
                    "Items": [item("4", 5, 5)]}})
                self.client.inventory_increase_callback = Mock()
                self.client.note_resource_empty("4")
                self.client._apply_now_items({"Drop": {"Items": [{"NowItem": item("4", count, 5)}]}})
                self.assertEqual(self.client.resource_gauges()["flag"]["current"], count)
                self.client.inventory_increase_callback.assert_called_once_with({"4"})

    def test_new_login_replaces_empty_resource_correction(self):
        self.client.note_resource_empty("4")
        self.client._replace_account_state({"Utc": SERVER_MS / 1000, "ItemContainer": {
            "Items": [item("4", 4, 5)]}})
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 4)

    def test_existing_status_and_log_poll_share_the_projected_gauges(self):
        from backend import appcore
        acc = SimpleNamespace(email="resource-test", client=self.client, state_revision=1,
                              state_synced_at=LOCAL_MS, state_sync_error=None,
                              logs_since=lambda since: [], guild_snapshot=lambda: {})
        self.clock += 600_000
        with patch.object(appcore.accounts, "get", return_value=acc):
            poll = appcore.auto_logs(acc.email)
            status = appcore.inventory(acc.email)["status"]
        self.assertEqual(poll["resourceGauges"]["stamina"]["current"], 32)
        self.assertEqual(status["stamina"], poll["resourceGauges"]["stamina"])
        self.assertEqual(self.client.account_state["ItemContainer"]["Items"][0]["Count"], 30)

    def test_table_intervals_are_not_hardcoded_and_ignore_other_items(self):
        rules = recovery.build_intervals([
            {"ID": "3", "DynamicField1": "ItemCollectSecond#120"},
            {"ID": "4", "DynamicField2": "ItemCollectSecond#1800"},
            {"ID": "72", "DynamicField1": "ItemCollectSecond#600"}])
        self.assertEqual(rules, {"3": 120, "4": 1800})
        with self.assertRaises(ValueError):
            recovery.build_intervals([{"ID": "3", "DynamicField1": "ItemCollectSecond#0"}])


if __name__ == "__main__":
    unittest.main()
