"""Connection-reset handling: replay read-only queries only, never mutating routes."""
from __future__ import annotations

import json
import unittest

import httpx

from backend import game_client
from backend.game_client import GameClient, GameUnavailable, is_readonly_route, _truncate_payload

DISCONNECT = "Server disconnected without sending a response."


class ConnectionResetHandling(unittest.TestCase):
    def wire(self, responses):
        """Build a client whose transport replays a scripted sequence.

        ``responses`` is a list of either a ``httpx.Response`` to return or an
        exception to raise, consumed one entry per transport call.
        """
        seen = []

        def transport(method, url, **kwargs):
            seen.append(json.loads(kwargs["content"])["route"])
            step = responses[min(len(seen) - 1, len(responses) - 1)]
            if isinstance(step, Exception):
                raise step
            return step

        client = GameClient.__new__(GameClient)
        client.aid = "offline"
        client.session_id = "offline-session"
        client._login_in_progress = False
        client._proxy = ""
        client._client = httpx.Client()
        client._sync_proxy = lambda: None
        client._request_http = transport
        return client, seen

    @staticmethod
    def _reset():
        return httpx.RemoteProtocolError(DISCONNECT)

    def test_readonly_route_is_retried_once_after_a_dropped_connection(self):
        ok = httpx.Response(200, json={"Utc": 1}, request=httpx.Request("PUT", "http://g"))
        client, seen = self.wire([self._reset(), ok])
        body = client.call("MailHandler.QueryNewestMails", {})
        self.assertEqual(body, {"Utc": 1})
        self.assertEqual(seen, ["MailHandler.QueryNewestMails", "MailHandler.QueryNewestMails"])

    def test_mutating_route_is_never_replayed_after_a_dropped_connection(self):
        client, seen = self.wire([self._reset()])
        with self.assertRaises(GameUnavailable):
            client.call("StoreHandler.BuyCommodity", {"Store": "SecretShop"})
        self.assertEqual(seen, ["StoreHandler.BuyCommodity"])

    def test_persistent_reset_reports_a_retryable_condition(self):
        client, seen = self.wire([self._reset(), self._reset()])
        with self.assertRaises(GameUnavailable):
            client.call("MailHandler.QueryNewestMails", {})
        self.assertEqual(len(seen), 2)

    def test_route_classification_keeps_mutating_methods_out(self):
        self.assertTrue(is_readonly_route("MailHandler.QueryNewestMails"))
        self.assertTrue(is_readonly_route("ServerStatusHandler.Query"))
        self.assertFalse(is_readonly_route("SupportFriendHandler.GetReward"))
        self.assertFalse(is_readonly_route("GuildHandler.GuildMemberCheckIn"))
        self.assertFalse(is_readonly_route("PVPHandler.PVPCheckTicket"))
        self.assertFalse(is_readonly_route("StoreHandler.BuyCommodity"))
        self.assertFalse(is_readonly_route(""))

    def test_large_payload_is_truncated_for_logging(self):
        body = {"Mails": [{"StaticID": "x" * 100} for _ in range(100)]}
        text = _truncate_payload(body, game_client._MAX_RESPONSE_LOG)
        self.assertLessEqual(len(text), game_client._MAX_RESPONSE_LOG + 40)
        self.assertIn("已截断", text)


if __name__ == "__main__":
    unittest.main()
