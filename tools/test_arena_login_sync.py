"""Offline regressions for NPC result uncertainty and login-only recovery."""
from __future__ import annotations

import copy
from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


_import_data = tempfile.TemporaryDirectory(prefix="lucima-arena-tests-")


def _cleanup_import_data():
    # Other unittest modules may keep using the same process-wide logger.
    logger = logging.getLogger("lucima")
    for handler in list(logger.handlers):
        if (isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename).is_relative_to(Path(_import_data.name))):
            logger.removeHandler(handler)
            handler.close()
    _import_data.cleanup()


unittest.addModuleCleanup(_cleanup_import_data)
with patch.dict(os.environ, {"ARK_DATA_DIR": _import_data.name}):
    from backend import accounts, appcore, config, tasks
    from backend.game_client import GameClient, GameError, GameMessage
    from backend import resource_recovery
    from backend.updater import service as update_service


HOUR = 3_600_000
DAY = 24 * HOUR
GRACE = 5_000


class ArenaLoginSyncTests(unittest.TestCase):
    def setUp(self):
        self.now_ms = 1_792_376_897_000
        case = self

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(case.now_ms / 1000, tz)

        self.enterContext(patch.object(tasks, "datetime", FrozenDateTime))
        self.enterContext(patch.object(accounts.time, "time", side_effect=lambda: self.now_ms / 1000))
        self.enterContext(patch.object(config, "load_accounts", return_value={}))
        self.enterContext(patch.object(config, "save_account"))
        self.enterContext(patch.object(config, "get_auto_relogin_config", return_value={"enabled": False}))
        self.enterContext(patch.object(accounts, "_next_daily_state_refresh", side_effect=lambda: (self.now_ms + DAY) / 1000))
        self.enterContext(patch.object(accounts.redeem_codes, "next_refresh_delay", return_value=DAY / 1000))
        self.enterContext(patch.object(update_service, "is_installing", return_value=False))
        self.enterContext(patch("httpx.Client.request", side_effect=AssertionError("Unexpected real HTTP request")))
        self.enterContext(patch.object(tasks, "_build_role_for_battle", return_value={}))
        self.enterContext(patch.object(tasks, "_build_arena_enemy", return_value=({}, [])))
        self.enterContext(patch.object(resource_recovery, "intervals", return_value={"3": 300, "4": 3600}))

        self.client = GameClient.__new__(GameClient)
        self.client.aid = "offline-aid"
        self.client.session_id = "offline-session"
        self.client.account_state = {
            "Info": {"Name": "Offline"},
            "ItemContainer": {"Items": [{"StaticID": "4", "Count": 5,
                "TimeCollect": {"MaxCount": 5, "LastCollectTime": {"$date": self.now_ms}}}]},
            "Teams": {"Settings": [{"ID": "0", "TeamSetting": {"RolePosMap": {"role": 0}}}]},
        }
        self.client._mark_connected()
        self.client.call = Mock()
        self.client.bootstrap = Mock()
        self.account = accounts.Account("offline@example.com", self.client, None)
        self.account.toggles = {"arena_npc": True}
        self.account.params = {"arena_npc": {"picked": [1, 2], "teamId": "0"}}
        self.addCleanup(self.account.stop_scheduler)

    def set_flags(self, count, *, last=None, timer=True):
        state = copy.deepcopy(self.client.account_state)
        state["Utc"] = self.now_ms / 1000
        item = {"StaticID": "4", "Count": count}
        if timer:
            item["TimeCollect"] = {"MaxCount": 5, "LastCollectTime": {
                "$date": self.now_ms if last is None else last}}
        state["ItemContainer"]["Items"] = [item]
        self.client._replace_account_state(state)

    def settlement(self, npc=1, next_ms=None):
        return {"IsWin": True, "NPCInfo": {"NPCID": str(npc),
                "NextTime": {"$date": next_ms if next_ms is not None else self.now_ms + HOUR}}}

    def seed_pause(self):
        self.account.task_runtime["arena_npc"] = {
            "waitingForLogin": {"HellNPC_1": True},
            "nextMs": {"HellNPC_1": self.now_ms - HOUR},
            "retryMs": {"HellNPC_1": self.now_ms - 1},
        }

    def install_login_snapshot(self, next_ms):
        state = copy.deepcopy(self.client.account_state)
        state["PVPData"] = {"NPCPVPInfoList": [{"NPCID": "1", "NextTime": {"$date": next_ms}}]}

        def bootstrap():
            self.client.account_state = copy.deepcopy(state)
            self.client._mark_connected()
            return self.client.account_state

        self.client.bootstrap.side_effect = bootstrap

    def test_cooldown_pauses_one_npc_and_other_npc_keeps_its_schedule(self):
        self.client.call.side_effect = [
            {"LogID": "npc-1"}, GameMessage("Still In Cooldown"),
            {"LogID": "npc-2"}, self.settlement(2),
        ]
        self.assertFalse(self.account.run_due()[0]["ok"])
        runtime = self.account.task_runtime["arena_npc"]
        self.assertEqual(runtime["waitingForLogin"], {"HellNPC_1": True})
        self.assertNotIn("HellNPC_1", runtime["retryMs"])
        self.assertNotIn("HellNPC_1", runtime["nextMs"])
        self.assertEqual(runtime["nextMs"]["HellNPC_2"], self.now_ms + HOUR)
        tickets = [call.args[1]["NPCSceneID"] for call in self.client.call.call_args_list
                   if call.args[0] == tasks.ARENA_CHECK_ROUTE]
        self.assertEqual(tickets, ["HellNPC_1", "HellNPC_2"])
        self.assertEqual(self.account._arena_due_delay(self.now_ms), 3605)
        self.assertEqual(self.account._next_wake_delay(), 3600)
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), self.now_ms + HOUR + GRACE)
        view = appcore.arena_state(self.account)["npcs"]
        self.assertTrue(view[0]["waitingForLogin"])
        self.assertIsNone(view[0]["nextMs"])
        self.assertFalse(view[1]["waitingForLogin"])
        self.assertIn("等待登录同步刷新", self.account.logs_since(0)[0]["result"]["detail"])

        self.now_ms += 6 * tasks.ARENA_RETRY_GAP_MS
        self.account.notify_config_changed()
        self.account.run_due()
        self.assertEqual(self.client.call.call_count, 4)
        self.account.params["arena_npc"]["picked"] = [1]
        self.account.toggles["arena_npc"] = False
        self.account.notify_config_changed()
        self.account.toggles["arena_npc"] = True
        self.account.notify_config_changed()
        self.account.run_due()
        self.assertEqual(self.client.call.call_count, 4)
        self.assertIsNone(self.account.next_fixed_task_at_ms(self.now_ms))
        self.assertEqual(self.account._next_wake_delay(), accounts.SCAN_MAX_GAP)
        self.client.bootstrap.assert_not_called()

    def test_lost_settlement_is_not_retried_even_after_many_retry_periods(self):
        self.account.params["arena_npc"]["picked"] = [1, 1]
        self.client.call.side_effect = [{"LogID": "npc-1"}, TimeoutError("response lost")]
        self.account.run_due()
        self.now_ms += 10 * tasks.ARENA_RETRY_GAP_MS
        self.assertTrue(self.account.run_due()[0]["skipped"])
        self.assertEqual(self.client.call.call_count, 2)
        self.assertTrue(appcore.arena_state(self.account)["npcs"][0]["waitingForLogin"])

    def test_ticket_uncertainty_or_cooldown_never_submits_a_settlement(self):
        for response in (TimeoutError("ticket response lost"), {}, [], GameMessage("Still In Cooldown")):
            with self.subTest(response=response):
                self.client.call.reset_mock(side_effect=True)
                self.client.call.side_effect = [response]
                result = tasks.arena_sweep_once(self.client, 1, "0")
                self.assertTrue(result["waitingForLogin"])
                self.assertIn("等待登录同步刷新", result["detail"])
                self.assertEqual(self.client.call.call_count, 1)

    def test_incomplete_or_unconfirmed_settlements_pause_instead_of_retrying(self):
        responses = [TimeoutError("result lost"), GameMessage("Still In Cooldown"),
                     GameMessage("Server busy"), None, [], {}, {"IsWin": True},
                     {"IsWin": True, "NPCInfo": {}},
                     {"NPCInfo": {"NextTime": {"$date": self.now_ms + HOUR}}},
                     {**self.settlement(), "Drop": "invalid drop"},
                     self.settlement(2)]
        for next_ms in ("bad", True, 0, float("nan")):
            response = self.settlement()
            response["NPCInfo"]["NextTime"] = {"$date": next_ms}
            responses.append(response)
        for response in responses:
            with self.subTest(response=response):
                self.client.call.reset_mock(side_effect=True)
                self.client.call.side_effect = [{"LogID": "npc-1"}, response]
                result = tasks.arena_sweep_once(self.client, 1, "0")
                self.assertFalse(result["ok"])
                self.assertTrue(result["waitingForLogin"])
                self.assertEqual(self.client.call.call_count, 2)

    def test_flag_shortage_waits_for_recovery_instead_of_retrying_stale_balance(self):
        self.account.params["arena_npc"]["picked"] = [1]

        def refuse(*args):
            self.now_ms += 20_000
            raise GameMessage("Item not enough")

        self.client.call.side_effect = refuse
        result = self.account.run_due()[0]
        detail = f"{tasks.arena_npc_name(1)}：旗帜不足（Item not enough），等待旗帜恢复"
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], detail)
        self.assertEqual(self.account.logs_since(0)[0]["result"]["detail"], detail)
        self.assertEqual(self.client.call.call_count, 1)
        runtime = self.account.task_runtime["arena_npc"]
        self.assertFalse(runtime["waitingForLogin"])
        self.assertNotIn("HellNPC_1", runtime["retryMs"])
        view = appcore.arena_state(self.account)["npcs"][0]
        self.assertFalse(view["waitingForLogin"])
        self.assertTrue(view["waitingForFlags"])
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 0)
        deadline = self.now_ms + HOUR + GRACE
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), deadline)
        self.assertEqual(self.account._arena_due_delay(self.now_ms), 3605)
        self.now_ms += tasks.ARENA_RETRY_GAP_MS
        self.account.run_due()
        self.assertEqual(self.client.call.call_count, 1)
        self.assertEqual(len(self.account.logs_since(0)), 1)
        self.now_ms = deadline
        self.client.call.side_effect = [{"LogID": "recovered"}, self.settlement()]
        self.assertTrue(self.account.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 3)

    def test_zero_flags_never_send_a_request_or_repeat_logs(self):
        self.account.params["arena_npc"]["picked"] = [1]
        self.set_flags(0, last=self.now_ms - 20_000)
        deadline = self.now_ms + HOUR - 20_000 + GRACE
        self.assertTrue(self.account.run_due()[0]["skipped"])
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), deadline)
        self.assertEqual(self.account._arena_due_delay(self.now_ms), (deadline - self.now_ms) / 1000)
        self.assertEqual(self.account._next_wake_delay(), (deadline - self.now_ms) / 1000)
        self.now_ms += tasks.ARENA_RETRY_GAP_MS
        self.account.run_due()
        self.client.call.assert_not_called()
        self.assertEqual(self.account.logs_since(0), [])

    def test_resumes_five_seconds_after_recovery_without_delaying_display(self):
        self.account.params["arena_npc"]["picked"] = [1]
        self.set_flags(0, last=self.now_ms - HOUR + 1234)
        self.assertEqual(self.account._next_wake_delay(), 6.234)
        self.now_ms += 1233
        self.account.run_due()
        self.client.call.assert_not_called()
        self.now_ms += 1
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 1)
        self.assertTrue(appcore.arena_state(self.account)["npcs"][0]["waitingForFlags"])
        self.account.run_due()
        self.client.call.assert_not_called()
        self.now_ms += GRACE - 1
        self.account.run_due()
        self.client.call.assert_not_called()
        self.now_ms += 1
        self.client.call.side_effect = [{"LogID": "recovered"}, self.settlement()]
        self.assertTrue(self.account.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 2)

    def test_rechecks_flags_between_npcs_and_resumes_remaining_npc(self):
        self.set_flags(1)
        first_deadline = self.now_ms + 3 * HOUR

        def challenge(route, data):
            n = int(data["NPCSceneID"].split("_")[1])
            if route == tasks.ARENA_CHECK_ROUTE:
                count = self.client.resource_gauges()["flag"]["current"]
                self.client._apply_now_items({"CostItems": [{"NowItem": {
                    "StaticID": "4", "Count": count - 1, "TimeCollect": {
                        "MaxCount": 5, "LastCollectTime": {"$date": self.now_ms}}}}]})
                return {"LogID": f"npc-{n}"}
            return self.settlement(n, first_deadline)

        self.client.call.side_effect = challenge
        self.assertTrue(self.account.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 2)
        view = appcore.arena_state(self.account)["npcs"]
        self.assertFalse(view[0]["waitingForFlags"])
        self.assertTrue(view[1]["waitingForFlags"])
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), self.now_ms + HOUR + GRACE)
        self.now_ms += HOUR + GRACE
        self.assertTrue(self.account.run_due()[0]["ok"])
        tickets = [call.args[1]["NPCSceneID"] for call in self.client.call.call_args_list
                   if call.args[0] == tasks.ARENA_CHECK_ROUTE]
        self.assertEqual(tickets, ["HellNPC_1", "HellNPC_2"])

    def test_flag_rewards_wake_scheduler_before_predicted_recovery(self):
        self.set_flags(0)
        self.account._wake.clear()
        self.assertTrue(self.account.run_due()[0]["skipped"])
        self.client._apply_now_items({"Drop": {"Items": [{"NowItem": {
            "StaticID": "4", "Count": 1, "TimeCollect": {
                "MaxCount": 5, "LastCollectTime": {"$date": self.now_ms}}}}]}})
        self.assertTrue(self.account._wake.is_set())
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), self.now_ms)

    def test_inferred_flag_waits_even_when_display_reaches_its_cap(self):
        self.account.params["arena_npc"]["picked"] = [1]
        state = copy.deepcopy(self.client.account_state)
        state["ItemContainer"]["Items"] = [{"StaticID": "4", "Count": 0,
            "TimeCollect": {"MaxCount": 1, "LastCollectTime": {"$date": self.now_ms - HOUR}}}]
        self.client._replace_account_state(state)
        self.assertEqual(self.client.resource_gauges()["flag"]["current"], 1)
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), self.now_ms + GRACE)
        self.assertFalse(tasks.arena_flag_status(self.client)["ready"])
        self.account.run_due()
        self.client.call.assert_not_called()
        self.now_ms += GRACE
        self.assertTrue(tasks.arena_flag_status(self.client)["ready"])

    def test_npc_cooldowns_share_margin_without_padding_local_retries(self):
        self.account.params["arena_npc"]["picked"] = [1]
        deadline = self.now_ms
        self.client.account_state["PVPData"] = {"NPCPVPInfoList": [
            {"NPCID": "1", "NextTime": {"$date": deadline - 2_000}}]}
        self.account.task_runtime["arena_npc"] = {
            "nextMs": {"HellNPC_1": deadline}, "retryMs": {"HellNPC_1": deadline + 3_000}}
        view = appcore.arena_state(self.account)["npcs"][0]
        self.assertEqual(view["nextMs"], deadline + GRACE)
        self.assertEqual(self.account._arena_due_delay(self.now_ms), 5)
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), deadline + GRACE)
        self.now_ms += GRACE - 1
        # Also test the final action guard, independently of scheduler filtering.
        self.assertTrue(tasks.task_arena_npc(self.client, self.account.params["arena_npc"],
            self.account.task_runtime["arena_npc"])["skipped"])
        self.client.call.assert_not_called()
        self.now_ms += 1
        self.client.call.side_effect = [{"LogID": "cooled"}, self.settlement()]
        self.assertTrue(self.account.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 2)

        self.client.call.reset_mock()
        self.account.task_runtime["arena_npc"] = {"retryMs": {"HellNPC_1": self.now_ms + 10_000}}
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), self.now_ms + 10_000)
        self.assertEqual(self.account._arena_due_delay(self.now_ms), 10)

    def test_missing_recovery_timer_waits_for_inventory_without_guessing(self):
        self.set_flags(0, timer=False)
        self.assertTrue(self.account.run_due()[0]["skipped"])
        self.assertIsNone(self.account.next_fixed_task_at_ms(self.now_ms))
        self.assertEqual(self.account._next_wake_delay(), accounts.SCAN_MAX_GAP)
        self.now_ms += 3 * HOUR
        self.account.run_due()
        self.client.call.assert_not_called()

    def test_shortage_stops_other_due_npcs_but_other_failures_keep_retry(self):
        self.client.call.side_effect = GameMessage("Item not enough")
        self.account.run_due()
        self.assertEqual(self.client.call.call_count, 1)
        self.assertTrue(all(npc["waitingForFlags"] for npc in appcore.arena_state(self.account)["npcs"][:2]))
        self.set_flags(5)
        self.account.params["arena_npc"]["picked"] = [1]
        self.client.call.side_effect = GameMessage("Server busy")
        self.assertFalse(self.account.run_due()[0]["ok"])
        self.assertEqual(self.account.task_runtime["arena_npc"]["retryMs"]["HellNPC_1"],
                         self.now_ms + tasks.ARENA_RETRY_GAP_MS)

    def test_local_configuration_failure_happens_before_ticket_consumption(self):
        self.account.params["arena_npc"]["picked"] = [1]
        with patch.object(tasks, "_build_role_for_battle", side_effect=GameError("missing role")):
            self.assertFalse(self.account.run_due()[0]["ok"])
        self.client.call.assert_not_called()
        self.assertFalse(self.account.task_runtime["arena_npc"]["waitingForLogin"])

    def test_manual_refresh_resumes_using_new_snapshot_and_obeys_its_deadline(self):
        self.seed_pause()
        deadline = self.now_ms + 2 * HOUR
        self.install_login_snapshot(deadline)
        with patch.object(appcore, "_require", return_value=self.account):
            view = appcore.refresh_status(self.account.email)["arena"]["npcs"]
        self.assertFalse(view[0]["waitingForLogin"])
        self.assertEqual(view[0]["nextMs"], deadline + GRACE)
        self.account.params["arena_npc"]["picked"] = [1]
        self.account.run_due()
        self.client.call.assert_not_called()
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), deadline + GRACE)
        self.now_ms = deadline + GRACE - 1
        self.account.run_due()
        self.client.call.assert_not_called()
        self.now_ms += 1
        self.client.call.side_effect = [{"LogID": "resumed"}, self.settlement()]
        self.assertTrue(self.account.run_due()[0]["ok"])
        self.assertEqual(self.client.call.call_count, 2)

    def test_daily_login_restores_only_npcs_with_a_concrete_snapshot_time(self):
        self.seed_pause()
        self.account.task_runtime["arena_npc"]["waitingForLogin"]["HellNPC_2"] = True
        deadline = self.now_ms + HOUR
        self.install_login_snapshot(deadline)
        self.account._state_refresh_at = self.now_ms / 1000
        self.assertTrue(self.account._refresh_state_if_due())
        view = appcore.arena_state(self.account)["npcs"]
        self.assertFalse(view[0]["waitingForLogin"])
        self.assertEqual(view[0]["nextMs"], deadline + GRACE)
        self.assertTrue(view[1]["waitingForLogin"])
        self.assertEqual(self.account.next_fixed_task_at_ms(self.now_ms), deadline + GRACE)
        self.client.call.assert_not_called()

    def test_failed_daily_login_keeps_the_pause(self):
        self.seed_pause()
        self.client.bootstrap.side_effect = TimeoutError("login unavailable")
        self.account._state_refresh_at = self.now_ms / 1000
        with self.assertLogs("lucima", level="WARNING"):
            self.assertFalse(self.account._refresh_state_if_due())
        self.assertTrue(appcore.arena_state(self.account)["npcs"][0]["waitingForLogin"])
        self.client.call.assert_not_called()

    def test_manual_refresh_without_npc_timestamp_keeps_the_pause(self):
        self.seed_pause()
        self.client.bootstrap.return_value = self.client.account_state
        with patch.object(appcore, "_require", return_value=self.account):
            result = appcore.refresh_status(self.account.email)
        self.assertTrue(result["arena"]["npcs"][0]["waitingForLogin"])
        self.client.call.assert_not_called()

    def test_automatic_relogin_restores_from_new_snapshot(self):
        self.seed_pause()
        deadline = self.now_ms + HOUR
        self.install_login_snapshot(deadline)
        self.client._mark_disconnected("duplicate_login")
        self.now_ms += 1_000
        with patch.object(config, "get_auto_relogin_config", return_value={"enabled": True, "delaySeconds": 1}):
            self.assertTrue(self.account._auto_relogin_if_due())
        view = appcore.arena_state(self.account)["npcs"]
        self.assertFalse(view[0]["waitingForLogin"])
        self.assertEqual(view[0]["nextMs"], deadline + GRACE)
        self.client.call.assert_not_called()

    def test_new_account_uses_login_snapshot_without_old_pause(self):
        self.seed_pause()
        deadline = self.now_ms + HOUR
        self.install_login_snapshot(deadline)
        self.client.bootstrap()
        other = accounts.Account(self.account.email, self.client, None)
        self.addCleanup(other.stop_scheduler)
        view = appcore.arena_state(other)["npcs"]
        self.assertFalse(view[0]["waitingForLogin"])
        self.assertEqual(view[0]["nextMs"], deadline + GRACE)


class ArenaLoginSyncUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the frontend projection check")
    def test_waiting_label_overrides_expired_or_future_countdowns_until_login(self):
        source = (Path(__file__).resolve().parents[1] / "frontend/app.js").read_text(encoding="utf-8")
        function = source[source.index("function arenaWhen("):source.index("function renderArenaList(")]
        script = """
            const assert = require('node:assert/strict');
            const fmtClock = () => 'time';
            const fmtRemain = () => 'remaining';
        """ + function + """
            for (const deadline of [0, Date.now() - 1000, Date.now() + 86400000]) {
                assert.deepEqual(arenaWhen({waitingForLogin: true, nextMs: deadline, retryMs: deadline}),
                    {txt: '等待登录同步刷新', cls: ''});
            }
            assert.equal(arenaWhen({waitingForLogin: false}).txt, '可挑战');
            assert.match(arenaWhen({waitingForLogin: false, nextMs: Date.now() + 3600000}).txt, /^下次挑战:/);
            assert.match(arenaWhen({retryMs: Date.now() + 300000}).txt, /^重试:/);
            assert.deepEqual(arenaWhen({waitingForFlags: true}), {txt: '等待旗帜恢复', cls: ''});
            assert.equal(arenaWhen({waitingForFlags: true, waitingForLogin: true}).txt, '等待登录同步刷新');
            // Existing countdown refreshes can derive flag waiting without a new challenge log.
            assert.equal(arenaWhen({nextMs: Date.now() - 1}, 0).txt, '等待旗帜恢复');
            assert.match(arenaWhen({nextMs: Date.now() + 1000}, 0).txt, /^下次挑战:/);
            assert.equal(arenaWhen({waitingForFlags: true}, 1).txt, '可挑战');
        """
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
