"""Offline boundary checks for action eligibility and its displayed deadline."""
import copy
from datetime import datetime
import unittest
from unittest.mock import patch

from backend import game_clock, tasks
from tools.test_game_clock import ClockClient


NOW = 1_790_000_000_000
LOCAL = NOW + 2 * 3600_000
DAY = 86_400_000


class TaskBoundaryTests(unittest.TestCase):
    def test_production_claims_share_the_padded_deadline_with_countdowns(self):
        for task_id, (_, field, route), run in zip(
            ("sf_potion", "sf_starforce", "sf_tesseract"), tasks.STARFORCE_REWARDS,
            (tasks.task_potion, tasks.task_starforce, tasks.task_tesseract),
        ):
            for elapsed in (0, 4_999, 5_000):
                with self.subTest(task=task_id, elapsed=elapsed):
                    state = {"ArkStarForceLabData": {field: {"$date": NOW}}}
                    client = ClockClient(copy.deepcopy(state))
                    view = tasks.next_claim_times(state, now_ms=NOW + elapsed,
                        local_now_ms=LOCAL + elapsed)[task_id]
                    self.assertEqual(view["nextMs"], LOCAL + 5_000)
                    self.assertEqual(view["ready"], elapsed == 5_000)
                    with patch.object(game_clock, "now_ms", return_value=NOW + elapsed):
                        result = run(client)
                    self.assertEqual(bool(result.get("skipped")), elapsed < 5_000)
                    self.assertEqual([r for r, _ in client.calls], [route] if elapsed == 5_000 else [])
                    self.assertEqual(client.account_state, state)

    def test_tesseract_claim_margin_does_not_change_raw_acceleration_window(self):
        # Padding the reward deadline must not make the last 24 hours chargeable.
        for remaining in (DAY, DAY + 1):
            lab = {"NextCanReceiveTesseractTime": {"$date": NOW + remaining + DAY},
                   "TesseractChargeCount": 1, "NextCanChargeTime": {"$date": NOW - 5_000}}
            with self.subTest(remaining=remaining):
                view = tasks.next_claim_times({"ArkStarForceLabData": lab}, now_ms=NOW)["sf_tesseract"]
                self.assertEqual(view["nextMs"], NOW + remaining + 5_000)
                self.assertEqual(view["charge"]["ready"], remaining > DAY)

    def test_tesseract_acceleration_waits_five_seconds_after_cooldown(self):
        lab = {"NextCanReceiveTesseractTime": {"$date": NOW + 3 * DAY},
               "NextCanChargeTime": {"$date": NOW}, "TesseractChargeCount": 0}
        for elapsed in (0, 4_999, 5_000):
            with self.subTest(elapsed=elapsed):
                client = ClockClient({"ArkStarForceLabData": copy.deepcopy(lab)})
                view = tasks.next_claim_times(client.account_state, now_ms=NOW + elapsed,
                    local_now_ms=LOCAL + elapsed)["sf_tesseract"]["charge"]
                self.assertEqual(view["nextMs"], LOCAL + 5_000)
                self.assertEqual(view["ready"], elapsed == 5_000)
                with patch.object(game_clock, "now_ms", return_value=NOW + elapsed):
                    tasks.task_tesseract(client)
                self.assertEqual([r for r, _ in client.calls],
                    ["ArkStarForceLabHandler.ChargeTesseract"] if elapsed == 5_000 else [])

    def test_tesseract_drops_acceleration_deadline_if_margin_exceeds_charge_window(self):
        lab = {"NextCanReceiveTesseractTime": {"$date": NOW + DAY + 4_999},
               "NextCanChargeTime": {"$date": NOW}, "TesseractChargeCount": 0}
        view = tasks.tesseract_charge_status(lab, NOW)
        self.assertFalse(view["ready"])
        self.assertIsNone(view["nextMs"])

    def test_dispatch_claim_and_redispatch_wait_but_preserve_server_payload(self):
        quest = {"StaticID": "QuestA", "FinishTime": {"$date": NOW}, "DispatchedHeroIDs": ["hero"]}
        for elapsed in (0, 4_999, 5_000):
            with self.subTest(elapsed=elapsed):
                state = {"ArkHighCommandData": {"DispatchedQuests": [copy.deepcopy(quest)]}}
                client = ClockClient(state)
                view = tasks.next_claim_times(state, now_ms=NOW + elapsed,
                    local_now_ms=LOCAL + elapsed)["dispatch"]
                self.assertEqual(view["ready"], elapsed == 5_000)
                if elapsed < 5_000:
                    self.assertEqual(view["nextMs"], LOCAL + 5_000)
                with patch.object(game_clock, "now_ms", return_value=NOW + elapsed):
                    tasks.task_dispatch(client)
                self.assertEqual([r for r, _ in client.calls], [
                    "ArkHighCommandHandler.RewardQuest", "ArkHighCommandHandler.DispatchQuest",
                ] if elapsed == 5_000 else [])
                self.assertEqual(state["ArkHighCommandData"]["DispatchedQuests"], [quest])
                if elapsed == 5_000:
                    self.assertEqual(client.calls[1][1]["Quest"]["FinishTime"], tasks._date_str(quest["FinishTime"]))

    def test_dispatch_ready_slot_is_not_blocked_by_another_slots_margin(self):
        quests = [{"StaticID": sid, "FinishTime": {"$date": finish}}
                  for sid, finish in (("ready", NOW - 5_000), ("waiting", NOW - 1))]
        state = {"ArkHighCommandData": {"DispatchedQuests": quests}}
        client = ClockClient(state)
        self.assertTrue(tasks.next_claim_times(state, now_ms=NOW)["dispatch"]["ready"])
        with patch.object(game_clock, "now_ms", return_value=NOW):
            tasks.task_dispatch(client)
        self.assertEqual([payload["QuestStaticID"] for route, payload in client.calls
            if route == "ArkHighCommandHandler.RewardQuest"], ["ready"])

    def test_dinner_schedules_and_becomes_ready_five_seconds_after_twenty(self):
        start = int(datetime(2026, 11, 7, 20, tzinfo=tasks.MEAL_TIMEZONE).timestamp() * 1000)
        state = {"TimingMailData": {"LastSentMailTime": {"$date": start - 11 * 3600_000}}}
        for elapsed in (-1, 0, 4_999, 5_000):
            with self.subTest(elapsed=elapsed):
                view = tasks.send_meal_status(state, now_ms=start + elapsed,
                    local_now_ms=start + elapsed + 3600_000)
                self.assertEqual(view["nextMs"], start + 5_000 + 3600_000)
                self.assertEqual(view["ready"], elapsed == 5_000)

    def test_daytime_meal_still_waits_for_post_login_refresh(self):
        start = int(datetime(2026, 11, 7, 8, tzinfo=tasks.MEAL_TIMEZONE).timestamp() * 1000)
        runtime = {"checkAfterMs": start - 1, "stateRefreshAtMs": start + 7 * 60_000}
        view = tasks.send_meal_status({}, runtime, now_ms=start + 5_000)
        self.assertFalse(view["ready"])
        self.assertEqual(view["nextMs"], runtime["stateRefreshAtMs"])


if __name__ == "__main__":
    unittest.main()
