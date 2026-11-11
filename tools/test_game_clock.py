"""Offline checks for server-clock anchoring and reactor eligibility."""
import unittest
from unittest.mock import patch

from backend import game_clock, tasks


class ClockClient:
    def __init__(self, state):
        self.account_state = state
        self.calls = []

    def _auth_data(self, data=None):
        return data or {}

    def call(self, route, data):
        self.calls.append((route, data))
        return {"Drop": {"Items": []}}


class GameClockTests(unittest.TestCase):
    def test_server_utc_anchors_time_despite_local_epoch_skew(self):
        local = [2_000_000_000_000]
        server = 1_790_000_000_000
        with patch.object(game_clock.time, "time", side_effect=lambda: local[0] / 1000):
            clock = game_clock.ServerClock()
            clock.sync({"Utc": server / 1000}, reset=True)
            self.assertEqual(clock.now_ms(), server)
            local[0] += 8_000
            self.assertEqual(clock.now_ms(), server + 8_000)
            clock.sync({"Utc": server / 1000 + 3}, reset=False)
            self.assertEqual(clock.now_ms(), server + 8_000)

    def test_reactor_and_countdown_share_the_twelve_hour_plus_five_second_gate(self):
        local = [2_000_000_000_000]
        server = 1_790_000_000_000
        state = {"Utc": server / 1000, "ArkReactorData": {
            "LastMoneyReceivedTime": {"$date": server - 12 * 3600_000}}}
        client = ClockClient(state)
        local_deadline = local[0] + 5_000
        with patch.object(game_clock.time, "time", side_effect=lambda: local[0] / 1000):
            claim = tasks.next_claim_times(state, now_ms=server,
                                           local_now_ms=local[0])["reactor"]
            self.assertEqual(claim["nextMs"], local_deadline)
            self.assertFalse(claim["ready"])

            early = tasks.task_reactor(client)
            self.assertTrue(early["skipped"])
            self.assertEqual(client.calls, [])

            local[0] += 4_999
            self.assertTrue(tasks.task_reactor(client)["skipped"])
            self.assertEqual(client.calls, [])

            local[0] += 1
            ready = tasks.next_claim_times(state, now_ms=server + 5_000,
                                           local_now_ms=local[0])["reactor"]
            self.assertTrue(ready["ready"])
            result = tasks.task_reactor(client)
            self.assertTrue(result["ok"])
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
