"""Offline logging regressions: keep login diagnostics without credential values."""
from copy import deepcopy
import gzip
import json
import unittest
from unittest.mock import Mock

import httpx

from backend.game_client import DuplicateLoginError, GameClient, GameMessage
from backend.logutil import REDACTED, format_payload, redact


class LoginLoggingTests(unittest.TestCase):
    def setUp(self):
        self.client = GameClient.__new__(GameClient)
        self.client.account_state = None
        self.client._sync_proxy = Mock()
        self.client._request_http = Mock()

    def response(self, body, *, status=200, compressed=False):
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8') if not isinstance(body, bytes) else body
        if compressed:
            raw = gzip.compress(raw)
        self.client._request_http.return_value = httpx.Response(
            status, content=raw, request=httpx.Request('PUT', 'https://example.invalid/router'))

    def test_full_login_snapshot_is_logged_and_response_is_unchanged(self):
        body = {
            'AID': 'private-aid', 'SessionID': 'private-session',
            'Info': {'_id': {'$oid': 'player-id'}, 'Name': 'Player',
                     'GuildSubInfo': {'_id': {'$oid': 'guild-id'}, 'GuildLV': 18}},
            'PlayerGuildInfo': {'Authority': 3},
            'ItemContainer': {'Items': [{'StaticID': '3', 'Count': 999}]},
            'Nested': [{'accessToken': 'private-access', 'refreshToken': 'private-refresh'}],
            'Diagnostic': 'x' * 6000,
        }
        original = deepcopy(body)
        self.response(body, compressed=True)
        with self.assertLogs('lucima', level='INFO') as trace:
            result = self.client.call('AccountHandler.Login', {'Token': 'private-request'})
        response = next(line for line in trace.output if '[game<-]' in line)
        self.assertIn('AccountHandler.Login HTTP 200', response)
        self.assertNotIn('suppressed', response)
        logged = json.loads(response[response.index('{'):])
        self.assertEqual(logged, redact(original))
        self.assertEqual(len(logged['Diagnostic']), 6000)
        for secret in ('private-aid', 'private-session', 'private-access', 'private-refresh', 'private-request'):
            self.assertNotIn(secret, '\n'.join(trace.output))
        self.assertEqual(result, original)
        self.assertEqual(self.client.recent_requests(), [])

    def test_query_user_id_response_is_logged_and_redacted(self):
        self.response({'LoginID': 'player-login', 'Token': 'private-token'})
        with self.assertLogs('lucima', level='INFO') as trace:
            result = self.client.call('ErolabsHandler.QueryUserID', {'Token': 'private-request'})
        self.assertEqual(result['LoginID'], 'player-login')
        output = '\n'.join(trace.output)
        self.assertIn('player-login', output)
        self.assertIn(REDACTED, output)
        self.assertNotIn('private-token', output)

    def test_non_json_login_keeps_raw_repeated_body_but_preserves_business_error(self):
        self.response(b'No Token_No Token')
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(GameMessage) as error:
                self.client.call('AccountHandler.Login', {})
        self.assertEqual(str(error.exception), 'No Token')
        self.assertIn('HTTP 200 [non-JSON] No Token_No Token', '\n'.join(trace.output))

    def test_malformed_json_login_redacts_fields_and_keeps_diagnostic_tail(self):
        raw = b'{"SessionID":"private-session","Token":"private-token","GID":"guild-id", broken here'
        self.response(raw)
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(GameMessage):
                self.client.call('AccountHandler.Login', {})
        output = '\n'.join(trace.output)
        self.assertIn('[non-JSON]', output)
        self.assertIn('guild-id', output)
        self.assertIn('broken here', output)
        self.assertNotIn('private-session', output)
        self.assertNotIn('private-token', output)

    def test_unterminated_credential_value_is_redacted(self):
        self.response(b'{"SessionID":"private-session')
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(GameMessage):
                self.client.call('AccountHandler.Login', {})
        self.assertIn(REDACTED, '\n'.join(trace.output))
        self.assertNotIn('private-session', '\n'.join(trace.output))

    def test_non_json_http_failure_logs_html_before_raising(self):
        self.response(b'<html><title>Bad Gateway</title><p>upstream unavailable</p></html>', status=502)
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(httpx.HTTPStatusError):
                self.client.call('AccountHandler.Login', {})
        self.assertIn('HTTP 502 [non-JSON] <html>', '\n'.join(trace.output))
        self.assertIn('upstream unavailable', '\n'.join(trace.output))

    def test_json_http_failure_is_logged_before_raising(self):
        self.response({'message': 'server unavailable', 'SessionID': 'private-session'}, status=500)
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(httpx.HTTPStatusError):
                self.client.call('AccountHandler.Login', {})
        output = '\n'.join(trace.output)
        self.assertIn('HTTP 500', output)
        self.assertIn('server unavailable', output)
        self.assertNotIn('private-session', output)

    def test_non_json_duplicate_login_still_disconnects(self):
        self.response(b'Duplicate Login_Duplicate Login')
        with self.assertLogs('lucima', level='INFO') as trace:
            with self.assertRaises(DuplicateLoginError):
                self.client.call('AccountHandler.Login', {})
        self.assertFalse(self.client.is_connected())
        self.assertIn('[non-JSON] Duplicate Login_Duplicate Login', '\n'.join(trace.output))

    def test_json_string_login_error_redacts_embedded_text(self):
        self.response('upstream error: Token=private-token; retry later')
        with self.assertLogs('lucima', level='INFO') as trace:
            self.assertEqual(self.client.call('AccountHandler.Login', {}),
                             'upstream error: Token=private-token; retry later')
        output = '\n'.join(trace.output)
        self.assertIn('retry later', output)
        self.assertNotIn('private-token', output)

    def test_ordinary_response_logging_remains_available(self):
        self.response({'ok': True, 'SessionID': 'private-session'})
        with self.assertLogs('lucima', level='INFO') as trace:
            self.client.call('ServerStatusHandler.Query', {'AID': 'private-aid'})
        output = '\n'.join(trace.output)
        self.assertIn('ServerStatusHandler.Query HTTP 200', output)
        self.assertNotIn('private-aid', output)
        self.assertNotIn('private-session', output)


class TextRedactionTests(unittest.TestCase):
    def test_plain_text_fields_and_http_headers(self):
        cases = [
            'Token=private-secret; reason=invalid',
            'token=private-secret&reason=invalid',
            'SESSIONID: private-secret, reason=invalid',
            'Password="private-secret"; reason=invalid',
            "accessToken='private-secret'; reason=invalid",
            'Authorization: Bearer private-secret\nreason=invalid',
            'Proxy-Authorization: Basic private-secret\nreason=invalid',
            'Cookie: first=private-secret; second=private-cookie\nreason=invalid',
            'Set-Cookie: auth=private-secret; HttpOnly\nreason=invalid',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                safe = format_payload(raw)
                self.assertIn(REDACTED, safe)
                self.assertNotIn('private-secret', safe)
                self.assertNotIn('private-cookie', safe)
                self.assertIn('reason=invalid', safe)

    def test_recursive_redaction_preserves_source_and_noncredential_ids(self):
        source = {'AID': 'private-aid', 'nested': [{'sEsSiOnId': 'private-session'}],
                  'message': 'upstream Token=private-token; reason=invalid',
                  'Info': {'_id': {'$oid': 'player-id'}, 'GuildSubInfo': {'_id': 'guild-id'}},
                  'PlayerGuildInfo': {'GID': 'guild-id'}, 'Password': 'private-password'}
        original = deepcopy(source)
        safe = json.loads(format_payload(source))
        self.assertEqual(source, original)
        self.assertEqual(safe['AID'], REDACTED)
        self.assertEqual(safe['nested'][0]['sEsSiOnId'], REDACTED)
        self.assertEqual(safe['Info'], source['Info'])
        self.assertEqual(safe['PlayerGuildInfo'], source['PlayerGuildInfo'])
        self.assertIn('reason=invalid', safe['message'])
        self.assertNotIn('private-', json.dumps(safe))

    def test_diagnostic_text_without_credentials_is_kept_verbatim(self):
        raw = '  <html>upstream unavailable</html>\nmalformed response at offset 17  '
        self.assertEqual(format_payload(raw), raw)


if __name__ == '__main__':
    unittest.main()
