"""Gateway 5xx handling: replay only when the game backend never answered."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from backend import game_client
from backend.game_client import GameClient, GameError, GameUnavailable
from backend import abyss_battle, config, intimacy
from tools.test_abyss_auto import AssetServer
from tools.test_intimacy import farm_state

GATEWAY_TEXT = ('upstream connect error or disconnect/reset before headers. '
                'reset reason: remote connection failure')


class GatewayRecovery(unittest.TestCase):
    def wire(self, responses):
        seen = []

        def transport(method, url, **kwargs):
            seen.append(json.loads(kwargs['content'])['route'])
            status, text = responses[min(len(seen) - 1, len(responses) - 1)]
            return httpx.Response(status, text=text, request=httpx.Request(method, url))

        client = GameClient.__new__(GameClient)
        client.aid = 'offline'
        client.session_id = 'offline-session'
        client._sync_proxy = lambda: None
        client._request_http = transport
        return client, seen

    def test_transient_gateway_503_is_replayed_until_the_game_answers(self):
        client, seen = self.wire([(503, GATEWAY_TEXT), (503, GATEWAY_TEXT), (200, '{"Utc":1}')])
        with patch.object(game_client.time, 'sleep'):
            body = client.call('GameServerDBSettingHandler.QueryBulletinInfoResult', {})
        self.assertEqual(body, {'Utc': 1})
        self.assertEqual(len(seen), 3)

    def test_persistent_gateway_503_reports_a_retryable_condition(self):
        client, seen = self.wire([(503, GATEWAY_TEXT)])
        with patch.object(game_client.time, 'sleep'):
            with self.assertRaises(GameUnavailable):
                client.call('GameServerDBSettingHandler.QueryBulletinInfoResult', {})
        self.assertEqual(len(seen), 1 + game_client._GATEWAY_RETRIES)

    def test_game_json_error_response_is_never_replayed(self):
        client, seen = self.wire([(503, '{"Event":"None"}')])
        with patch.object(game_client.time, 'sleep'):
            with self.assertRaises(httpx.HTTPStatusError):
                client.call('GameServerDBSettingHandler.QueryBulletinInfoResult', {})
        self.assertEqual(len(seen), 1)

    def test_background_tasks_treat_the_gateway_error_as_retryable(self):
        self.assertTrue(intimacy.retryable_transport(GameUnavailable('游戏服务器暂时不可用')))
        self.assertFalse(intimacy.retryable_transport(GameError('No Role')))

    def test_friendship_sync_survives_a_gateway_outage_instead_of_ending(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch.object(config, 'data_dir', return_value=Path(temp.name)).start()
        self.addCleanup(patch.stopall)
        client = AssetServer(farm_state())
        attempts, original = [], client.bootstrap

        def bootstrap():
            attempts.append(1)
            if len(attempts) == 1:
                raise GameUnavailable('游戏服务器暂时不可用（HTTP 503），已自动重试，请稍后再试')
            return original()

        client.bootstrap = bootstrap
        battles = []

        def battle(c, params):
            battles.append(deepcopy(params['waves']))
            for row in c.server['LineRoleDataContainer']['RoleList']:
                row['Info']['IntimacyLV'] = 10
            return {'ok': True, 'settlementConfirmed': True}

        with patch.object(intimacy, 'RETRY_BASE_SECONDS', 0), \
             patch.object(config, 'get_request_delay_config', return_value={'enabled': False}), \
             patch.object(abyss_battle, 'run_abyss_battle', side_effect=battle):
            result = intimacy.run(client, {'_account_key': 'offline', '_cancel_event': threading.Event(),
                                           'mains': {}, 'autoConfigure': True})
        self.assertTrue(result['ok'], result)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(len(battles), 1)


if __name__ == '__main__':
    unittest.main()
