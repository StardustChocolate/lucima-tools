"""Login versions follow official acceptance without changing tokens or profiles."""
import unittest
from unittest.mock import Mock, patch

from backend import config, maintenance
from backend.game_client import GameClient, GameMessage, login_version


OLD = "4.0.0.111888"
NEW = "5.0.0.116271"


class LoginVersionTests(unittest.TestCase):
    def test_removed_version_uses_latest_full_official_version_not_generic_entry(self):
        for versions in (["1.0", NEW], [NEW, "1.0"], ["5.0.0.9", "5.0.0.10", "1.0"]):
            expected = NEW if NEW in versions else "5.0.0.10"
            self.assertEqual(login_version({"Info": {"AvailableVersions": versions}}, OLD), expected)

    def test_still_accepted_profile_is_preserved(self):
        self.assertEqual(login_version({"Info": {"AvailableVersions": [NEW, OLD]}}, OLD), OLD)
        self.assertEqual(login_version({"AvailableVersions": ["1.0", NEW]}, "1.0"), "1.0")

    def test_missing_or_malformed_list_does_not_invent_version(self):
        for response in (None, {}, {"Info": None}, {"AvailableVersions": NEW},
                         {"AvailableVersions": [None, {}, "preview", "1.0"]}):
            self.assertEqual(login_version(response, OLD), OLD)

    def client(self, *, legacy=False):
        client = GameClient.__new__(GameClient)
        client.game_token = "offline-token"
        client._login_common = {"Platform": "WebGLPlayer" if legacy else "Android", "Version": OLD, "DeviceID": "device"}
        client._fixed_login_id = None if legacy else "test-user"
        client._auth_refresher = None
        client._sync_login_auth = Mock()
        client._replace_account_state = Mock()
        client._mark_connected = Mock()
        client.call = Mock()
        self.enterContext(patch.object(maintenance, "service", maintenance.Maintenance()))
        return client

    @staticmethod
    def snapshot():
        return {"SessionID": "offline-session", "Info": {"_id": {"$oid": "offline-aid"}}}

    def test_bootstrap_uses_bulletin_version_before_both_legacy_handshake_requests(self):
        client = self.client(legacy=True)
        client.call.side_effect = [{"Info": {"AvailableVersions": ["1.0", NEW]}},
                                   {"LoginID": "test-user"}, self.snapshot()]
        client.bootstrap()
        for call in client.call.call_args_list[1:]:
            self.assertEqual(call.args[1]["Version"], NEW)
            self.assertEqual(call.args[1]["Platform"], "WebGLPlayer")
        self.assertEqual(config.GAME_VERSION, NEW)

    def test_token_refresh_cannot_revert_the_official_login_version(self):
        client = self.client()
        client._auth_refresher = Mock()
        def refresh(force):
            client.game_token = "refreshed-token" if force else "offline-token"
            client._login_common["Version"] = OLD
        client._sync_login_auth.side_effect = refresh
        client.call.side_effect = [{"Info": {"AvailableVersions": ["1.0", NEW]}},
                                   GameMessage("No Token"), self.snapshot()]
        client.bootstrap()
        logins = client.call.call_args_list[1:]
        self.assertEqual([call.args[1]["Version"] for call in logins], [NEW, NEW])
        self.assertEqual(logins[-1].args[1]["Token"], "refreshed-token")
        self.assertEqual(logins[-1].args[1]["DeviceID"], "device")
        self.assertEqual([call.args[0] for call in client._sync_login_auth.call_args_list], [False, True])


if __name__ == "__main__":
    unittest.main()
