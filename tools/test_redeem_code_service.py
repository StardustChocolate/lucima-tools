"""Offline coverage for client redeem-code synchronization and endpoint configuration."""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from backend import redeem_code_endpoint, redeem_codes

API = "https://updates.example/api/redeem-codes"
NOW = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)


class ClientAPITests(unittest.TestCase):
    def setUp(self):
        self.now = NOW.timestamp()
        self.payload = {"schemaVersion": 1, "updatedAt": NOW.isoformat(),
                        "codes": [{"code": "ArkNEW1234", "validUntil": "2026-10-20"}]}
        for patcher in (
            patch.object(redeem_codes, "_cache", {}),
            patch.object(redeem_codes, "_next_refresh_at", 0),
            patch.object(redeem_codes, "_revision", 0),
            patch.object(redeem_codes, "_last_error", None),
            patch.object(redeem_codes, "_failures", 0),
            patch.object(redeem_codes, "api_url", return_value=API),
            patch.object(redeem_codes.time, "time", side_effect=lambda: self.now),
            patch.object(redeem_codes.config, "save_redeem_codes"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetch = self.enterContext(patch.object(redeem_codes, "_fetch", return_value=self.payload))
        self.wake = self.enterContext(patch.object(redeem_codes, "_wake_accounts"))

    def test_cross_midnight_failures_and_multiple_accounts_do_not_accumulate_requests(self):
        self.now = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc).timestamp()
        self.fetch.side_effect = TimeoutError("offline")
        barrier = threading.Barrier(4)
        def refresh():
            barrier.wait(timeout=5)
            return redeem_codes.refresh_if_due(initial=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: refresh(), range(4)))
        self.fetch.assert_called_once()
        self.now += 30 * 60  # Midnight in the game timezone.
        for _ in range(10):
            redeem_codes.refresh_if_due(initial=True)
        self.fetch.assert_called_once()
        self.now += 30 * 60
        redeem_codes.refresh_if_due()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(redeem_codes._next_refresh_at, self.now + 3600)

    def test_same_table_does_not_wake_game_accounts_every_hour(self):
        redeem_codes.refresh_if_due()
        first_revision = redeem_codes.revision()
        self.now += 3600
        redeem_codes.refresh_if_due()
        self.assertEqual(redeem_codes.revision(), first_revision)
        self.wake.assert_called_once()
        self.payload["codes"].append({"code": "ArkNEXT1234", "validUntil": None})
        self.now += 3600
        redeem_codes.refresh_if_due()
        self.assertEqual(redeem_codes.revision(), first_revision + 1)
        self.assertEqual(self.wake.call_count, 2)

    def test_invalid_api_preserves_cache_and_accepts_valid_empty_table(self):
        redeem_codes.refresh_if_due()
        for invalid in ({}, {**self.payload, "codes": [{"code": "bad"}]},
                        {**self.payload, "updatedAt": None},
                        {**self.payload, "codes": [{"code": "ArkNEW1234", "validUntil": "tomorrow"}]}):
            self.now += 3600
            self.fetch.return_value = invalid
            state = redeem_codes.refresh_if_due()
            self.assertEqual(state["codes"][0]["code"], "ArkNEW1234")
            self.assertTrue(state["refreshError"])
        self.now += 3600
        self.fetch.return_value = {**self.payload, "codes": []}
        state = redeem_codes.refresh_if_due()
        self.assertEqual(state["codes"], [])
        self.assertIsNone(state["refreshError"])

    def test_upstream_failure_is_distinct_from_client_sync_failure(self):
        self.fetch.return_value = {**self.payload, "refreshError": "Source HTTP 403"}
        state = redeem_codes.refresh_if_due()
        self.assertIsNone(state["refreshError"])
        self.assertEqual(state["sourceError"], "Source HTTP 403")
        self.assertEqual(state["lastUpdated"], NOW.isoformat())


class EndpointTests(unittest.TestCase):
    def test_dedicated_config_and_packaged_update_host_work_without_update_service(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(redeem_code_endpoint, "_read_url", return_value=""):
            self.assertEqual(redeem_code_endpoint.api_url(), "")
            with patch.dict(os.environ, {"LUCIMA_UPDATE_URL": "https://updates.example/stable.json"}):
                self.assertEqual(redeem_code_endpoint.api_url(), API)
                with patch.dict(os.environ, {"LUCIMA_REDEEM_CODES_URL": "https://codes.example/query"}):
                    self.assertEqual(redeem_code_endpoint.api_url(), "https://codes.example/query")


if __name__ == "__main__":
    unittest.main()
