"""Durable self-notifications, exact purchase identity and background recovery."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from backend import equipment_notifications as notices,background,config,appcore,tasks,accounts


def card(key='a'):
    return {'equipmentKey':key*64,'setCN':'速度','slotCN':'靴子','name':'速度靴子','index':0,'auxScore':32,
            'img':'/assets/equip/test.png','subProps':[{'label':'速度','text':'5'},{'label':'攻击','text':'8%'}]}


class EquipmentNotices(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        patcher=patch.object(config,'data_dir',return_value=Path(self.temp.name))
        patcher.start();self.addCleanup(patcher.stop)

    def test_each_matching_shelf_notifies_once_with_complete_stats_and_image(self):
        native=Mock(return_value=True)
        with patch.object(notices,'_native_notify',native):
            first=notices.publish('alpha','shop_free',[card()],{'id':'rule'},context=['shelf',1])
            self.assertEqual(notices.publish('alpha','shop_free',[card()],{'id':'rule'},context=['shelf',1]),first)
            notices.publish('alpha','shop_free',[card()],{'id':'rule'},context=['shelf',2])
        self.assertEqual(native.call_count,2)
        event=notices.get(first)
        self.assertIn('速度·靴子',event['text']);self.assertIn('32',event['text'])
        self.assertIn('速度 5',event['text']);self.assertIn('攻击 8%',event['text'])
        self.assertEqual(event['image'],'/assets/equip/test.png')
        self.assertTrue(event['delivered'])
        self.assertIsNone(notices.get('../other'))

    def test_native_permission_failure_is_retained_for_retry_and_never_raises_into_game_task(self):
        with patch.object(notices,'_native_notify',return_value=False):
            ident=notices.publish('alpha','star_source_shop',[card()],{})
        self.assertFalse(notices.get(ident)['delivered'])
        with patch.object(config,'PLATFORM','android'),patch.object(notices,'_native_notify',return_value=True) as native:
            notices.retry_pending()
            self.assertEqual(native.call_count,1)
        self.assertTrue(notices.get(ident)['delivered'])

    def test_notification_click_only_queries_the_correct_account_and_reports_stale_shelf(self):
        with patch.object(notices,'_native_notify',return_value=True):
            ident=notices.publish('beta','shop_free',[card()],{'id':'rule'})
        client=SimpleNamespace(account_state={'StoreRecordContainer':{'Records':[]}},bootstrap=Mock(),call=Mock())
        acc=SimpleNamespace(email='beta',client=client,lock=threading.Lock(),shop_shelf=None)
        with patch.object(accounts,'get',side_effect=lambda email:acc if email=='beta' else None), \
             patch.object(tasks,'_equipment_matches_in_shelf',return_value=[card('b')]):
            result=appcore.open_equipment_notification(ident)
        self.assertEqual(result['account'],'beta');self.assertTrue(result['stale'])
        client.bootstrap.assert_called_once();client.call.assert_not_called()
        with patch.object(accounts,'get',return_value=None),patch.object(background,'is_restoring',return_value=False):
            self.assertTrue(appcore.open_equipment_notification(ident)['needsLogin'])

    def test_changed_secret_shop_equipment_is_never_bought_from_a_stale_notification(self):
        equip={'StaticID':'W001','ClassLV':4,'Set':'Speed','Enhance':0,'SubProps':{'SourceValues':[]}}
        rec={'Store':tasks.SECRET_STORE_ID,'DropResult':{'Items':[{'Equipment':equip}]}}
        client=SimpleNamespace(account_state={'StoreRecordContainer':{'Records':[rec]}},bootstrap=Mock())
        acc=SimpleNamespace(email='alpha',client=client,lock=threading.Lock(),shop_shelf=[rec])
        old=tasks.equipment_key(equip)
        equip['SubProps']['SourceValues']=[{'PropertyType':'SpeedValue','Value':5}]
        with patch.object(appcore,'_require',return_value=acc),patch.object(appcore,'shop_buy_index') as buy:
            result=appcore.shop_buy_ep('alpha',0,old)
        self.assertFalse(result['ok']);buy.assert_not_called()

    def test_changed_star_source_equipment_is_never_bought_from_a_stale_notification(self):
        acc=SimpleNamespace(client=SimpleNamespace(),lock=threading.Lock())
        with patch.object(appcore,'_require',return_value=acc), \
             patch.object(appcore,'star_source_query',return_value={'cards':[{'shopIndex':1,'equipmentKey':'new'}]}), \
             patch.object(appcore,'star_source_buy') as buy:
            result=appcore.star_source_buy_ep('alpha',1,'old')
        self.assertFalse(result['ok']);buy.assert_not_called()


class BackgroundRecovery(unittest.TestCase):
    def setUp(self):
        background._retry_at.clear();background._restoring.clear()

    def test_only_previously_started_accounts_restore_and_live_schedulers_are_idempotent(self):
        saved={'active':{'automationActive':True,'auth':{'fake':True}},'offline':{'auth':{'fake':True}},
               'bad':{'automationActive':True,'auth':{'fake':True},'auth_error':True}}
        live={};acc=SimpleNamespace(_stop=threading.Event(),start_scheduler=Mock())
        def login(email):self.assertEqual(email,'active');live[email]=acc
        with patch.object(config,'PLATFORM','android'),patch.object(config,'load_accounts',return_value=saved), \
             patch.object(accounts,'get',side_effect=live.get),patch.object(appcore,'login_saved',side_effect=login) as restore:
            background.recover_once(1000);background.recover_once(1001)
        self.assertEqual(restore.call_count,1);self.assertEqual(acc.start_scheduler.call_count,2)

    def test_auth_failure_retries_with_backoff_instead_of_terminating_watchdog(self):
        saved={'active':{'automationActive':True,'auth':{'fake':True}}}
        with patch.object(config,'PLATFORM','android'),patch.object(config,'load_accounts',return_value=saved), \
             patch.object(accounts,'get',return_value=None),patch.object(appcore,'login_saved',side_effect=RuntimeError('offline')) as login:
            background.recover_once(1000);background.recover_once(1100);background.recover_once(1301)
        self.assertEqual(login.call_count,2);self.assertFalse(background.is_restoring('active'))

    def test_closing_account_waits_for_loan_return_without_blocking_other_accounts(self):
        done=threading.Event();closed=threading.Event()
        account=SimpleNamespace(stop_scheduler=Mock(),intimacy_done_event=done,lock=threading.Lock(),
                                client=SimpleNamespace(close=closed.set))
        accounts.Account.close(account)
        self.assertFalse(closed.wait(.05))
        done.set();self.assertTrue(closed.wait(1))


if __name__=='__main__':unittest.main()
