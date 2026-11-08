"""Saved login identities stay account-scoped and survive an app restart."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import config, appcore


class CharacterNamePersistence(unittest.TestCase):
    def test_name_survives_other_preference_writes_without_touching_token(self):
        settings={'accounts':{'one@example.invalid':{'auth_blob':'encrypted-original','order':0},
                              'two@example.invalid':{'character_name':'另一个角色'}}}
        def save(value):
            settings.clear();settings.update(deepcopy(value));return True
        with patch.object(config,'_load_settings',side_effect=lambda:deepcopy(settings)), \
             patch.object(config,'_save_settings',side_effect=save):
            config.save_account('one@example.invalid',character_name='  角色一  ')
            config.save_account('one@example.invalid',toggles={'guild_auto_signin':True})
            config.save_account('one@example.invalid',params={'test':{}},character_name='')
        self.assertEqual(settings['accounts']['one@example.invalid']['character_name'],'角色一')
        self.assertEqual(settings['accounts']['one@example.invalid']['auth_blob'],'encrypted-original')
        self.assertEqual(settings['accounts']['two@example.invalid']['character_name'],'另一个角色')

    def test_offline_list_exposes_cached_name_and_online_list_prefers_live_name(self):
        saved={'one':{'character_name':'旧名','auth':{'test':'not-a-real-token'}},'two':{'character_name':'角色二'}}
        account=SimpleNamespace(email='one',client=SimpleNamespace(account_state={'Info':{'Name':'新名'}}),
                                toggles={},params={},log_seq=0,state_revision=1,state_synced_at=1,state_sync_error=None)
        with patch.object(config,'load_accounts',return_value=saved), \
             patch.object(appcore.accounts,'all_accounts',return_value=[]):
            self.assertEqual([a['name'] for a in appcore.list_accounts()['accounts']],['旧名','角色二'])
        with patch.object(config,'load_accounts',return_value=saved), \
             patch.object(appcore.accounts,'all_accounts',return_value=[account]), \
             patch.object(appcore,'connection_status',return_value={'connected':True}):
            self.assertEqual(appcore.list_accounts()['accounts'][0]['name'],'新名')

    def test_successful_password_login_persists_real_game_name(self):
        account=SimpleNamespace(client=SimpleNamespace(account_state={'Info':{'Name':'密码登录角色'}}))
        auth={'access_token':'fake-token','user_id':'fake-user'}
        with patch.object(appcore,'portal_login_v2',return_value=auth), \
             patch.object(config,'get_device_id',return_value='offline-device'), \
             patch.object(appcore,'_bootstrap_and_register',return_value=account), \
             patch.object(config,'save_account') as save, \
             patch.object(appcore,'_account_payload',return_value={'ok':True}):
            self.assertTrue(appcore.login('one','unused')['ok'])
        self.assertEqual(save.call_args.kwargs['character_name'],'密码登录角色')

    def test_saved_token_login_refreshes_cached_character_name(self):
        account=SimpleNamespace(client=SimpleNamespace(account_state={'Info':{'Name':'改名后的角色'}}))
        with patch.object(config,'load_accounts',return_value={'one':{'auth':{'access_token':'fake'}}}), \
             patch.object(appcore,'_bootstrap_and_register',return_value=account), \
             patch.object(config,'save_account') as save, \
             patch.object(appcore,'_account_payload',return_value={'ok':True}):
            self.assertTrue(appcore.login_saved('one')['ok'])
        save.assert_called_once_with('one',character_name='改名后的角色')


if __name__=='__main__':unittest.main()
