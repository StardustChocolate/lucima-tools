"""Offline coverage for direct webpage sync and future API endpoint configuration."""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from backend import redeem_code_endpoint, redeem_codes

API = "https://updates.example/api/redeem-codes"
NOW = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)


class ClientWebpageTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW.timestamp()
        self.document = '<table><tr><td>ArkNEW1234</td><td>Valid until October 20th, 2026</td></tr></table>'
        for patcher in (
            patch.object(redeem_codes, "_cache", {}),
            patch.object(redeem_codes, "_next_refresh_at", 0),
            patch.object(redeem_codes, "_revision", 0),
            patch.object(redeem_codes, "_last_error", None),
            patch.object(redeem_codes, "_failures", 0),
            patch.object(redeem_codes.time, "time", side_effect=lambda: self.now),
            patch.object(redeem_codes.config, "save_redeem_codes"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetch = self.enterContext(patch.object(redeem_codes, "_fetch", return_value=self.document))
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
        self.fetch.return_value = self.document.replace('</table>', '<tr><td>ArkNEXT1234</td><td>Gift</td></tr></table>')
        self.now += 3600
        redeem_codes.refresh_if_due()
        self.assertEqual(redeem_codes.revision(), first_revision + 1)
        self.assertEqual(self.wake.call_count, 2)

    def test_unrecognized_page_preserves_cache_and_accepts_fully_expired_table(self):
        redeem_codes.refresh_if_due()
        for invalid in ('', '<html>Access denied</html>', '<table><tr><td>No codes here</td></tr></table>'):
            self.now += 3600
            self.fetch.return_value = invalid
            state = redeem_codes.refresh_if_due()
            self.assertEqual(state["codes"][0]["code"], "ArkNEW1234")
            self.assertTrue(state["refreshError"])
        self.now += 3600
        self.fetch.return_value = '<table><tr><td>ArkOLD1234</td><td>Valid until January 1st, 2000</td></tr></table>'
        state = redeem_codes.refresh_if_due()
        self.assertEqual(state["codes"], [])
        self.assertIsNone(state["refreshError"])

    def test_success_clears_previous_api_errors_and_records_webpage_source(self):
        redeem_codes._cache = {'apiUrl': API, 'sourceError': 'Source HTTP 403', 'refreshError': 'offline'}
        state = redeem_codes.refresh_if_due()
        self.assertIsNone(state["refreshError"])
        self.assertIsNone(state["sourceError"])
        self.assertEqual(state["source"], redeem_codes.SOURCE_URL)
        self.assertEqual(state["lastUpdated"], NOW.isoformat())
        self.assertNotIn('apiUrl', redeem_codes._cache)
        self.assertEqual(redeem_codes._cache['fetchMode'], 'webpage')

    def test_restart_preserves_webpage_deadline_but_api_cache_retries_immediately(self):
        redeem_codes.refresh_if_due()
        saved = dict(redeem_codes._cache)
        self.assertEqual(redeem_codes._restore_schedule(saved, redeem_codes.SOURCE_URL, self.now), self.now + 3600)
        saved.update(fetchMode='api', apiUrl=API)
        self.assertEqual(redeem_codes._restore_schedule(saved, redeem_codes.SOURCE_URL, self.now), 0)


class WebpageTransportTests(unittest.TestCase):
    def test_fetch_uses_source_page_and_app_proxy_even_when_api_is_configured(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, text='<table><tr><td>ArkNEW1234</td></tr></table>')
        original_client = httpx.Client
        def create_client(**kwargs):
            self.assertEqual(kwargs['proxy'], 'http://proxy.test:8080')
            return original_client(transport=httpx.MockTransport(respond))
        with patch.dict(os.environ, {'LUCIMA_REDEEM_CODES_URL': API}), \
                patch.object(redeem_codes.config, 'get_proxy', return_value='http://proxy.test:8080'), \
                patch.object(redeem_codes.httpx, 'Client', side_effect=create_client):
            self.assertIn('ArkNEW1234', redeem_codes._fetch())
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), redeem_codes.SOURCE_URL)
        self.assertEqual(requests[0].headers['Referer'], 'https://www.google.com/')
        self.assertEqual(requests[0].headers['Accept'], 'text/html')

    def test_fetch_reports_http_failure_before_parsing(self):
        original_client = httpx.Client
        with patch.object(redeem_codes.config, 'get_proxy', return_value=''), \
                patch.object(redeem_codes.httpx, 'Client', return_value=original_client(
                    transport=httpx.MockTransport(lambda request: httpx.Response(403, text='Access denied')))):
            with self.assertRaises(httpx.HTTPStatusError):
                redeem_codes._fetch()


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
