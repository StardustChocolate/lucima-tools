"""Offline arena window/receipt/scheduler regressions; no account or network."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import httpx
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

_data = tempfile.TemporaryDirectory(prefix="lucima-arena-reward-")
with patch.dict(os.environ, {"ARK_DATA_DIR": _data.name}):
    from backend import accounts, appcore, arena_reward as reward, config, server, tasks
    from backend.game_client import GameClient, GameError, GameMessage, DuplicateLoginError
    from backend.updater import service as update_service


def _cleanup():
    logger = logging.getLogger("lucima")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(Path(_data.name)):
            logger.removeHandler(handler)
            handler.close()
    _data.cleanup()


unittest.addModuleCleanup(_cleanup)
WEEK = reward.WEEK_MS


def stamp(value):
    date = datetime.fromisoformat(value)
    return int((date if date.tzinfo else date.replace(tzinfo=reward.BEIJING)).timestamp() * 1000)


START = stamp("2026-11-02T05:30:00")
END = stamp("2026-11-02T07:40:00")


def week(claimed=False, field="IsChoosedReward"):
    return {"PVPData": {"PVPWeekInfo": {field: claimed}}}


def receipt(sid="6", count=42, balance=9000):
    return {**week(True), "Drop": {"Items": [
        {"Item": {"StaticID": sid, "Count": count}, "NowItem": {"StaticID": sid, "Count": balance}}]}}


class ArenaRewardTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.folder = tempfile.TemporaryDirectory(prefix="arena-reward-case-")
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(config, "_DATA_DIR", Path(self.folder.name)))
        self.enterContext(patch.object(config, "_SETTINGS_FILE", Path(self.folder.name)/'settings.json'))
        self.enterContext(patch.object(reward.time, "time", side_effect=lambda: self.now / 1000))
        self.enterContext(patch.object(config, "get_auto_relogin_config", return_value={"enabled": False}))
        self.enterContext(patch.object(accounts, "_next_daily_state_refresh", side_effect=lambda: self.now / 1000 + 86400))
        self.enterContext(patch.object(accounts.redeem_codes, "next_refresh_delay", return_value=86400))
        self.enterContext(patch.object(update_service, "is_installing", return_value=False))
        self.enterContext(patch("httpx.Client.request", side_effect=AssertionError("Unexpected HTTP")))
        self.client = Mock(account_state={"Info": {"Name": "Offline"}}, aid="aid", session_id="sid")
        self.client._auth_data.side_effect = lambda extra=None: {"AID": "aid", "SessionID": "sid", **(extra or {})}
        self.client.is_connected.return_value = True
        self.acc = accounts.Account("offline@example.com", self.client, None)
        self.addCleanup(self.acc.stop_scheduler)

    def enable(self, **changes):
        cfg = reward.configure({}, {**reward.DEFAULTS, "enabled": True, **changes}, self.now)
        self.acc.params[reward.TASK_ID] = cfg
        self.acc.toggles[reward.TASK_ID] = True
        return cfg

    def test_defaults_disabled_yellow_ticket_no_configurable_time(self):
        self.assertEqual(reward.options(), {"enabled": False, "rewardItem": "6"})
        self.assertTrue(reward.run(self.client)["skipped"])
        self.client.call.assert_not_called()
        self.assertNotIn(reward.TASK_ID, [row["id"] for row in tasks.manual_tasks()])
        self.assertIn(reward.TASK_ID, [row["id"] for row in tasks.toggle_tasks()])

    def test_beijing_window_boundaries_independent_of_host_timezone(self):
        for now, ready, deadline in ((START-1, False, START), (START, True, START),
                                     (END-1, True, START), (END, False, START+WEEK),
                                     (END+1, False, START+WEEK),
                                     (START+86400_000, False, START+WEEK),
                                     (START+WEEK, True, START+WEEK)):
            with self.subTest(now=now):
                state = reward.status({"enabled": True}, now)
                self.assertEqual(state["ready"], ready)
                self.assertEqual(state["nextMs"], deadline)
                self.assertEqual(reward.in_window(now), ready)
        self.assertEqual(stamp("2026-11-01T21:30:00+00:00"), START)
        self.assertEqual(reward.next_slot({}, START), START)
        self.assertEqual(reward.next_slot({}, START, strictly_after=True), START+WEEK)

    def test_old_time_and_deadline_never_authorize_outside_window(self):
        for now in (START-1, END, START+86400_000):
            for old_deadline in (START-WEEK, START+60_000, START+3*WEEK):
                cfg = {"enabled": True, "rewardItem": "2", "weekday": 7, "hour": 23,
                       "minute": 59, "nextRunAtMs": old_deadline}
                with self.subTest(now=now, deadline=old_deadline):
                    self.now = now
                    self.assertFalse(reward.status(cfg, now)["ready"])
                    self.assertTrue(reward.run(self.client, cfg)["skipped"])
                    self.assertTrue(reward.claim(self.client, "2")["skipped"])
        self.client.call.assert_not_called()

    def test_old_config_migrates_ignoring_obsolete_malformed_time_fields(self):
        old = {"enabled": True, "rewardItem": "2", "weekday": "bad", "hour": True,
               "minute": 99, "nextRunAtMs": START+3*WEEK,
               "lastCompletedAtMs": START-WEEK, "lastResult": "竞技场本期周奖励已领取"}
        config.save_account(self.acc.email, params={reward.TASK_ID: old})
        restored = accounts.Account(self.acc.email, self.client, None)
        cfg = restored.params[reward.TASK_ID]
        self.assertTrue(restored.toggles[reward.TASK_ID])
        self.assertEqual(cfg["rewardItem"], "2")
        self.assertEqual(cfg["nextRunAtMs"], START)
        self.assertTrue(restored.next_claims()[reward.TASK_ID]["ready"])
        self.assertIsNone(restored.next_claims()[reward.TASK_ID]["lastReward"])
        for key in ("weekday", "hour", "minute"):
            self.assertNotIn(key, cfg)
        self.assertEqual(config.load_accounts()[self.acc.email]["params"][reward.TASK_ID], cfg)

    def test_invalid_old_reward_choice_blocks_task_without_breaking_login(self):
        config.save_account(self.acc.email, toggles={reward.TASK_ID: True},
                            params={reward.TASK_ID: {"enabled": True, "rewardItem": "bad"}})
        restored = accounts.Account(self.acc.email, self.client, None)
        self.assertIn("二选一", restored.next_claims()[reward.TASK_ID]["blocked"])
        self.assertEqual(restored.run_due(), [])
        self.client.call.assert_not_called()

    def test_enable_mid_window_and_currency_edit_keep_current_window(self):
        self.now += 45*60_000
        cfg = self.enable()
        self.assertTrue(reward.status(cfg, self.now)["ready"])
        changed = reward.configure(cfg, {**reward.options(cfg), "rewardItem": "2"}, self.now)
        self.assertEqual(changed["nextRunAtMs"], START)
        disabled = reward.configure(changed, {**reward.options(changed), "enabled": False}, self.now)
        self.assertNotIn("nextRunAtMs", disabled)
        self.now = END
        cfg = self.enable()
        self.assertEqual(cfg["nextRunAtMs"], START+WEEK)

    def test_both_currency_ids_auth_and_actual_award_deltas(self):
        for currency in ("6", "2"):
            with self.subTest(currency=currency):
                self.client.call.reset_mock()
                self.client.call.side_effect = [week(False), receipt(currency, 42, 99999)]
                result = reward.claim(self.client, currency)
                self.assertTrue(result["ok"])
                self.assertEqual(result["lastReward"]["items"], [
                    {"itemStaticId": currency, "name": reward.REWARDS[currency], "count": 42}])
                self.assertEqual(result["lastReward"]["claimedAtMs"], self.now)
                self.assertNotIn("99999", result["detail"])
                self.assertEqual(self.client.call.call_args_list[1].args,
                    (reward.CLAIM_ROUTE, {"AID": "aid", "SessionID": "sid", "ItemStaticID": currency}))

    def test_receipt_kind_wins_over_requested_kind_and_sums_only_item_deltas(self):
        response = receipt("2", 10, 9000)
        response["Drop"]["Items"] += [
            {"Item": {"StaticID": 2, "Count": 5}, "NowItem": {"StaticID": "2", "Count": 9005}},
            {"Item": {"StaticID": "other", "Count": 3}}]
        response["ItemContainer"] = {"Items": [{"StaticID": "6", "Count": 88888}]}
        self.client.call.side_effect = [week(False), response]
        result = reward.claim(self.client, "6")
        self.assertEqual([(i["itemStaticId"], i["count"]) for i in result["lastReward"]["items"]],
                         [("2", 15), ("other", 3)])
        self.assertIn("钻石 ×15", result["detail"])
        self.assertNotIn("黄票", result["detail"])

    def test_missing_invalid_quantities_are_unknown_never_balances(self):
        for count in (None, True, "42", -1, 1.5):
            with self.subTest(count=count):
                self.client.call.side_effect = [week(False), receipt(count=count)]
                result = reward.claim(self.client, "6")
                self.assertIsNone(result["lastReward"]["items"][0]["count"])
                self.assertIn("未返回数量", result["detail"])
                self.assertNotIn("9000", result["detail"])
        response = {**week(True), "Drop": {"Items": [{"NowItem": {"StaticID": "6", "Count": 9000}}]}}
        self.client.call.side_effect = [week(False), response]
        result = reward.claim(self.client, "6")
        self.assertEqual(result["lastReward"]["items"], [])
        self.assertIn("未返回奖励财物明细，未返回数量", result["detail"])

    def test_all_known_claimed_flags_never_create_a_tool_success(self):
        for flag in reward.CLAIMED_FIELDS:
            self.client.call.reset_mock()
            cfg = self.enable()
            self.client.call.side_effect = [week(True, flag)]
            result = reward.run(self.client, cfg)
            self.assertTrue(result["alreadyClaimed"])
            self.assertEqual(self.client.call.call_count, 1)
            self.assertNotIn("lastReward", cfg)
            self.assertNotIn("lastCompletedAtMs", cfg)
            self.assertEqual(cfg["completedWindowStartMs"], START)
            self.assertEqual(cfg["nextRunAtMs"], START+WEEK)

    def test_already_claimed_preserves_previous_success_even_after_reenable(self):
        cfg = self.enable()
        previous = {"claimedAtMs": START-WEEK, "requestedItem": "2",
                    "items": [{"itemStaticId": "2", "name": "钻石", "count": 100}]}
        cfg.update(lastReward=deepcopy(previous), lastCompletedAtMs=START-WEEK)
        self.client.call.side_effect = [week(True)]
        reward.run(self.client, cfg)
        self.assertEqual(cfg["lastReward"], previous)
        self.assertEqual(cfg["lastCompletedAtMs"], START-WEEK)
        off = reward.configure(cfg, {"enabled": False, "rewardItem": "2"}, self.now)
        on = reward.configure(off, {"enabled": True, "rewardItem": "2"}, self.now)
        self.assertFalse(reward.status(on, self.now)["ready"])

    def test_missing_fresh_week_state_never_uses_stale_login(self):
        self.client.account_state.update(week(False))
        for response in ({}, {"PVPWeekInfo": {}}, {"PVPWeekInfo": {"IsChoosedReward": "false"}}):
            self.client.call.reset_mock()
            self.client.call.side_effect = [response]
            with self.assertRaisesRegex(GameError, "缺少领取状态"):
                reward.claim(self.client, "6")
            self.assertEqual(self.client.call.call_count, 1)

    def test_uncertain_receipt_queries_again_but_query_drop_is_not_an_award(self):
        self.client.call.side_effect = [week(False), {}, receipt(count=1234)]
        result = reward.claim(self.client, "6")
        self.assertEqual(result["lastReward"]["items"], [])
        self.assertEqual(self.client.call.call_args_list[-1].args[0], reward.QUERY_ROUTE)
        self.client.call.side_effect = [week(False), {}, week(False)]
        with self.assertRaisesRegex(GameError, "未确认"):
            reward.claim(self.client, "6")

    def test_errors_do_not_record_success_or_complete_schedule(self):
        for error in (GameMessage("No Reward"), DuplicateLoginError("Duplicate Login"), GameError("timeout")):
            cfg = self.enable()
            previous = deepcopy(cfg)
            self.client.call.side_effect = [week(False), error]
            with self.assertRaises(type(error)):
                reward.run(self.client, cfg)
            self.assertEqual(cfg, previous)

    def test_specific_already_claimed_message_is_not_a_new_receipt(self):
        cfg = self.enable()
        self.client.call.side_effect = [week(False), GameMessage("Already Rewarded")]
        self.assertTrue(reward.run(self.client, cfg)["alreadyClaimed"])
        self.assertNotIn("lastReward", cfg)
        self.assertNotIn("lastCompletedAtMs", cfg)

    def test_json_error_is_not_a_success_even_if_it_contains_claimed_state_or_drop(self):
        for error in ({"Success": False, "Message": "No Reward"}, {"Error": "No Reward"}):
            cfg = self.enable()
            before = deepcopy(cfg)
            self.client.call.side_effect = [week(False), {**receipt(), **error}]
            with self.assertRaisesRegex(GameMessage, "No Reward"):
                reward.run(self.client, cfg)
            self.assertEqual(cfg, before)
        self.client.call.side_effect = [{**week(False), "Error": "Query failed"}]
        with self.assertRaisesRegex(GameError, "Query failed"):
            reward.claim(self.client, "6")

    def test_json_already_claimed_error_never_records_an_award(self):
        cfg = self.enable()
        self.client.call.side_effect = [week(False), {**receipt(), "Error": "Already Rewarded"}]
        self.assertTrue(reward.run(self.client, cfg)["alreadyClaimed"])
        self.assertNotIn("lastReward", cfg)
        self.assertNotIn("lastCompletedAtMs", cfg)

    def test_query_crossing_cutoff_never_sends_claim(self):
        cfg = self.enable()
        self.now = END-1
        def slow_query(*args):
            self.now = END
            return week(False)
        self.client.call.side_effect = slow_query
        self.assertTrue(reward.run(self.client, cfg)["skipped"])
        self.assertEqual(self.client.call.call_count, 1)
        self.assertNotIn("completedWindowStartMs", cfg)
        self.assertEqual(cfg["nextRunAtMs"], START+WEEK)

    def test_final_confirmation_query_is_not_sent_after_cutoff(self):
        cfg = self.enable()
        previous = {"claimedAtMs": START-WEEK, "requestedItem": "6", "items": [
            {"itemStaticId": "6", "name": reward.REWARDS["6"], "count": 20}]}
        cfg["lastReward"] = deepcopy(previous)
        self.now = END-1
        def delayed_response(route, payload):
            if route == reward.QUERY_ROUTE:
                return week(False)
            self.now = END
            return {}
        self.client.call.side_effect = delayed_response
        self.assertFalse(self.acc.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(cfg["lastReward"], previous)
        self.assertEqual(self.acc._retry_at_ms(reward.TASK_ID), START+WEEK)
        self.assertEqual(self.acc.next_fixed_task_at_ms(), START+WEEK)
        self.assertEqual(self.acc._next_wake_delay(), accounts.SCAN_MAX_GAP)
        self.now = END+tasks.TASK_RETRY_MS
        self.assertEqual(self.acc.run_due(), [])
        self.assertEqual(self.client.call.call_count, 2)

    def test_query_and_claim_recheck_time_after_auth_payload_is_built(self):
        for cross_on in (1, 2, 3):
            # First auth is before the initial query, second before the claim,
            # and third before the post-claim confirmation query.
            with self.subTest(cross_on=cross_on):
                self.now = END-1
                self.client.call.reset_mock()
                self.client.call.side_effect = [week(False), {}, week(True)]
                auth_calls = 0
                def auth():
                    nonlocal auth_calls
                    auth_calls += 1
                    if auth_calls == cross_on:
                        self.now = END
                    return {"AID": "aid", "SessionID": "sid"}
                self.client._auth_data.side_effect = auth
                if cross_on == 2:
                    self.assertTrue(reward.claim(self.client, "6")["skipped"])
                else:
                    with self.assertRaisesRegex(GameError, "不再重查或重试"):
                        reward.claim(self.client, "6")
                self.assertEqual(self.client.call.call_count, cross_on-1)

    def test_transport_wait_rechecks_after_obtaining_request_lock(self):
        transport = GameClient.__new__(GameClient)
        transport._client = Mock()
        class DelayedLock:
            def __enter__(inner):
                self.now = END
            def __exit__(inner, *args):
                return False
        transport._request_serial_lock = DelayedLock()
        self.now = END-1
        with self.assertRaisesRegex(GameError, "窗口外不发送"):
            transport._request_http("PUT", "https://offline.invalid", _game_route=reward.CLAIM_ROUTE)
        transport._client.request.assert_not_called()
        # The arena guard does not constrain unrelated read-only routes.
        transport._request_http("PUT", "https://offline.invalid", _game_route=reward.QUERY_ROUTE)
        transport._client.request.assert_called_once()

    def test_internal_proxy_fallback_never_sends_claim_after_cutoff(self):
        transport = GameClient.__new__(GameClient)
        transport._proxy = "offline-proxy"
        transport._sync_proxy = Mock()
        transport._client = Mock()
        direct = Mock()
        transport._build_client = Mock(return_value=direct)
        def proxy_failure(*args, **kwargs):
            self.now = END
            raise httpx.ProxyError("offline fixture")
        transport._client.request.side_effect = proxy_failure
        proxied = transport._client
        self.now = END-1
        with self.assertRaisesRegex(GameError, "窗口外不发送"):
            transport.call(reward.CLAIM_ROUTE, {})
        proxied.request.assert_called_once()
        direct.request.assert_not_called()

    def test_internal_proxy_fallback_still_works_inside_window(self):
        transport = GameClient.__new__(GameClient)
        transport.account_state = {}
        transport._proxy = "offline-proxy"
        transport._sync_proxy = Mock()
        transport._client = Mock()
        transport._client.request.side_effect = httpx.ProxyError("offline fixture")
        direct = Mock()
        direct.request.return_value = httpx.Response(200, json=week(True),
            request=httpx.Request("PUT", "https://offline.invalid"))
        transport._build_client = Mock(return_value=direct)
        self.assertEqual(transport.call(reward.CLAIM_ROUTE, {}), week(True))
        direct.request.assert_called_once()

    def test_valid_claim_finishing_after_cutoff_is_recorded_without_another_claim(self):
        cfg = self.enable()
        self.now = END-1
        def call(route, payload):
            if route == reward.QUERY_ROUTE:
                return week(False)
            self.now = END+1000
            return receipt()
        self.client.call.side_effect = call
        result = reward.run(self.client, cfg)
        self.assertEqual(result["lastReward"]["claimedAtMs"], END+1000)
        self.assertEqual(cfg["completedWindowStartMs"], START)
        self.assertEqual(cfg["nextRunAtMs"], START+WEEK)
        self.assertTrue(reward.run(self.client, cfg)["skipped"])
        self.assertEqual(self.client.call.call_count, 2)

    def test_scheduler_exact_open_and_receipt_persist_across_restart(self):
        self.now = START-1000
        cfg = self.enable()
        self.assertEqual(self.acc._due_ids(), [])
        self.assertEqual(self.acc._next_wake_delay(), 1)
        self.assertEqual(self.acc.next_fixed_task_at_ms(), START)
        self.assertEqual(self.acc.run_due(), [])
        self.client.call.assert_not_called()
        self.now = START
        self.client.call.side_effect = [week(False), receipt()]
        self.assertTrue(self.acc.run_due()[0]["ok"])
        self.assertEqual(cfg["nextRunAtMs"], START+WEEK)
        self.assertEqual(self.acc.run_due(), [])
        restored = accounts.Account(self.acc.email, self.client, None)
        self.assertEqual(restored.params[reward.TASK_ID], cfg)
        self.assertEqual(restored.next_claims()[reward.TASK_ID]["lastReward"]["items"][0]["count"], 42)
        self.assertEqual(restored._due_ids(), [])

    def test_overdue_restart_outside_window_waits_until_next_monday(self):
        cfg = self.enable()
        config.save_account(self.acc.email, params=self.acc.params, toggles=self.acc.toggles)
        self.now = START+WEEK+86400_000
        restored = accounts.Account(self.acc.email, self.client, None)
        self.assertEqual(restored._due_ids(), [])
        self.assertEqual(restored.run_due(), [])
        self.assertEqual(restored.next_claims()[reward.TASK_ID]["nextMs"], START+2*WEEK)
        self.assertGreater(restored._next_wake_delay(), 0)
        self.client.call.assert_not_called()
        self.now = START+2*WEEK
        self.client.call.side_effect = [week(False), receipt()]
        self.assertTrue(restored.run_due()[0]["ok"])

    def test_recovery_inside_window_can_claim_current_week(self):
        self.enable()
        config.save_account(self.acc.email, params=self.acc.params, toggles=self.acc.toggles)
        self.now = START+WEEK+60_000
        restored = accounts.Account(self.acc.email, self.client, None)
        self.client.call.side_effect = [week(False), receipt()]
        self.assertTrue(restored.run_due()[0]["ok"])
        self.assertEqual(restored.params[reward.TASK_ID]["completedWindowStartMs"], START+WEEK)

    def test_failed_claim_backoff_requeries_already_claimed_without_fabricating_receipt(self):
        cfg = self.enable()
        self.client.call.side_effect = [week(False), GameError("timeout")]
        self.assertFalse(self.acc.run_due()[0]["ok"])
        self.assertTrue(self.acc.next_claims()[reward.TASK_ID]["retrying"])
        count = self.client.call.call_count
        self.now += tasks.TASK_RETRY_MS-1
        self.assertEqual(self.acc.run_due(), [])
        self.assertEqual(self.client.call.call_count, count)
        self.now += 1
        self.client.call.side_effect = [week(True)]
        self.assertTrue(self.acc.run_due()[0]["alreadyClaimed"])
        self.assertNotIn("lastReward", cfg)
        self.assertEqual(cfg["nextRunAtMs"], START+WEEK)

    def test_retry_at_cutoff_or_later_is_deferred_to_next_window(self):
        for when in (END-tasks.TASK_RETRY_MS, END-1):
            with self.subTest(when=when):
                self.now = when
                self.acc.task_runtime.clear()
                self.enable()
                self.client.call.reset_mock()
                self.client.call.side_effect = [week(False), GameError("timeout")]
                self.assertFalse(self.acc.run_due()[0]["ok"])
                state = self.acc.next_claims()[reward.TASK_ID]
                self.assertFalse(state["retrying"])
                self.assertFalse(state["ready"])
                self.assertEqual(state["nextMs"], START+WEEK)
                self.assertEqual(self.acc._retry_at_ms(reward.TASK_ID), START+WEEK)
                self.assertEqual(self.acc.next_fixed_task_at_ms(), START+WEEK)
                self.assertEqual(self.acc._next_wake_delay(), accounts.SCAN_MAX_GAP)
                self.now = END+tasks.TASK_RETRY_MS
                self.assertEqual(self.acc.run_due(), [])
                self.assertEqual(self.client.call.call_count, 2)
                # A stale-client request is blocked by the scheduler deadline;
                # bypassing that scheduler still reaches the task's own gate.
                self.assertEqual(self.acc.run_toggles([reward.TASK_ID], "Test"), [])
                self.assertTrue(reward.run(self.client, self.acc.params[reward.TASK_ID])["skipped"])
                self.assertEqual(self.client.call.call_count, 2)

    def test_retry_just_before_cutoff_can_run_and_does_not_repeat_a_confirmed_claim(self):
        self.now = END-tasks.TASK_RETRY_MS-1
        self.enable()
        self.client.call.side_effect = [week(False), GameError("timeout")]
        self.assertFalse(self.acc.run_due()[0]["ok"])
        self.now = END-1
        self.client.call.side_effect = [week(False), receipt()]
        self.assertTrue(self.acc.run_due()[0]["ok"])
        self.assertEqual(self.acc.run_due(), [])

    def test_multi_account_receipts_currency_and_failures_are_independent(self):
        cfg = self.enable(rewardItem="2")
        other_client = Mock(account_state={"Info": {"Name": "Other"}})
        other_client.is_connected.return_value = True
        other_client._auth_data.return_value = {"AID": "other", "SessionID": "other"}
        other = accounts.Account("other@example.com", other_client, None)
        other.toggles[reward.TASK_ID] = True
        other.params[reward.TASK_ID] = reward.configure({}, {"enabled": True, "rewardItem": "6"}, self.now)
        self.client.call.side_effect = [week(False), GameError("timeout")]
        self.assertFalse(self.acc.run_due()[0]["ok"])
        other_client.call.side_effect = [week(False), receipt("6", 12)]
        self.assertTrue(other.run_due()[0]["ok"])
        self.assertEqual(other.params[reward.TASK_ID]["lastReward"]["items"][0]["count"], 12)
        self.assertNotIn("lastReward", cfg)
        self.assertTrue(self.acc.next_claims()[reward.TASK_ID]["retrying"])
        self.assertFalse(other.next_claims()[reward.TASK_ID]["retrying"])
        self.assertNotIn(self.acc.email, config.load_accounts())
        self.assertIn(other.email, config.load_accounts())

    def test_save_api_migrates_time_fields_and_generic_sync_preserves_receipt(self):
        with patch.object(appcore, "_require", return_value=self.acc):
            out = server._route_api("POST", "/api/arena/reward/config",
                {"account": self.acc.email, "config": {"enabled": True, "rewardItem": "2",
                 "weekday": 0, "hour": 99, "minute": "bad"}}, {})
            self.assertTrue(out["arenaReward"]["enabled"])
            self.client.call.side_effect = [week(False), receipt("2", 100)]
            self.assertTrue(self.acc.run_due()[0]["ok"])
            saved = deepcopy(self.acc.params[reward.TASK_ID])
            appcore.set_toggles(self.acc.email, {"reactor": False, reward.TASK_ID: True},
                                {reward.TASK_ID: {"enabled": False, "lastReward": {"items": []}}})
            self.assertEqual(self.acc.params[reward.TASK_ID], saved)
            self.assertEqual(config.load_accounts()[self.acc.email]["params"][reward.TASK_ID], saved)
            for changes in ({"rewardItem": "5"}, {"rewardItem": ["6", "2"]}, {"enabled": "true"}):
                with self.subTest(changes=changes), self.assertRaises(appcore.ApiError):
                    appcore.arena_reward_set(self.acc.email, {**reward.DEFAULTS, **changes})
                self.assertEqual(self.acc.params[reward.TASK_ID], saved)

    def test_concurrent_toggle_save_cannot_discard_running_claim_receipt(self):
        self.enable()
        entered, release, saving, saved = (threading.Event() for _ in range(4))
        failures = []
        def slow_claim(route, payload):
            if route == reward.QUERY_ROUTE:
                return week(False)
            entered.set()
            if not release.wait(3):
                raise AssertionError("Offline claim fixture was not released")
            return receipt()
        def runner():
            try:
                self.acc.run_toggles([reward.TASK_ID], "Test")
            except Exception as error:
                failures.append(error)
        def setter():
            try:
                appcore.set_toggles(self.acc.email, {reward.TASK_ID: True},
                                   {reward.TASK_ID: {"enabled": False}})
            except Exception as error:
                failures.append(error)
            finally:
                saved.set()
        self.client.call.side_effect = slow_claim
        def require(*args, **kwargs):
            saving.set()
            return self.acc
        with patch.object(appcore, "_require", side_effect=require):
            running = threading.Thread(target=runner)
            running.start()
            try:
                self.assertTrue(entered.wait(3))
                writer = threading.Thread(target=setter)
                writer.start()
                self.assertTrue(saving.wait(3))
                self.assertFalse(saved.wait(0.05), "Switch save must wait for the in-flight receipt")
            finally:
                release.set()
                running.join(3)
            writer.join(3)
        self.assertFalse(running.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        saved = config.load_accounts()[self.acc.email]["params"][reward.TASK_ID]
        self.assertEqual(saved["lastReward"]["items"][0]["count"], 42)
        self.assertEqual(saved, self.acc.params[reward.TASK_ID])


if __name__ == "__main__":
    unittest.main()
