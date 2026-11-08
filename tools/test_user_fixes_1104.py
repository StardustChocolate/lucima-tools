"""Offline regressions for interrupted loans, concurrent stores and daily gates."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from backend import abyss_loadout as loans, accounts, activity_store, appcore, config, tasks
from backend.game_client import GameClient, GameMessage
from backend.guild import GuildService
from tools.test_abyss_auto import AssetServer, gear_fixture
from tools.test_activity_store import Client as StoreClient, GOODS
from tools.test_guild_checkin_progress import CheckInProgress
from tools.test_guild_weekly import NOW


class LoanRecovery(unittest.TestCase):
    def setUp(self):
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(config,'data_dir',return_value=Path(folder.name)))
        state,_=gear_fixture();self.client=AssetServer(state)
        self.items=self.client.server['EquipmentContainer']['Datas']
        self.gear=[next(item for item in self.items if loans.plans.equip_slot(item)==slot) for slot in loans.plans.SLOTS]
        self.ids=[tasks._oid(item['_id']) for item in self.gear]
        for item in self.gear:item['EquipRole']='r1'
        self.client.bootstrap()

    def save(self, equipment, artifacts=None, account='account-A'):
        self.path=loans._path(self.client,account)
        loans._save(self.path,{'schema':1,'accountKey':account,'equipment':equipment,
                             'artifacts':artifacts or {},'roles':[]})

    def assert_closed(self):
        self.assertFalse(loans.pending(self.client,'account-A'))
        with sqlite3.connect(loans._database_path()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_recovery_assets').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_recovery_journals').fetchone()[0],0)

    def test_restart_tracks_ids_from_third_holder_and_skips_deleted_and_already_returned(self):
        ids=self.ids[:4]
        self.save(dict.fromkeys(ids,'r0'))
        self.gear[0]['EquipRole']='r2'
        self.gear[1]['EquipRole']='r0'
        self.items.remove(self.gear[2])
        restarted=AssetServer(self.client.server)
        # SQL must remain usable even if the old JSON mirror is gone.
        self.path.unlink()
        result=loans.restore(restarted,account_key='account-A')
        actual=loans._owners(restarted.server['EquipmentContainer']['Datas'])
        self.assertEqual([actual[i] for i in (ids[0],ids[1],ids[3])],['r0']*3)
        self.assertEqual(result['fallback']['missingEquipment'],[ids[2]])
        self.assertTrue(all(len(body['EquipIDs'])==1 for route,body in restarted.calls if route=='EquipmentHandler.EquipsToRole'))
        self.assert_closed()

    def test_one_rejected_piece_does_not_block_other_pieces_or_bond_and_never_replays(self):
        ids=self.ids[:3]
        self.client.server['Artifacts']={'Datas':[{'_id':'bond','StaticID':'unknown','EquipRole':'r2'}]}
        self.client.bootstrap();self.save(dict.fromkeys(ids,'r0'),{'bond':'r8'})
        original=self.client.call
        def call(route,body):
            if route=='EquipmentHandler.EquipsToRole' and body['EquipIDs']==[ids[1]]:
                raise GameMessage('No Role')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=call) as wire:
            result=loans.restore(self.client,account_key='account-A')
            count=wire.call_count
            self.assertTrue(loans.restore(self.client,account_key='account-A')['restored'])
            self.assertEqual(wire.call_count,count)
        owners=loans._owners(self.client.server['EquipmentContainer']['Datas'])
        self.assertEqual([owners[i] for i in ids],['r0','','r0'])
        self.assertEqual(self.client.server['Artifacts']['Datas'][0]['EquipRole'],'r8')
        self.assertEqual(result['fallback']['equipment'],[ids[1]])
        self.assert_closed()

    def test_sql_removes_each_attempt_before_next_piece_and_accounts_are_isolated(self):
        self.save(dict.fromkeys(self.ids[:3],'r0'))
        other=loans._path(self.client,'account-B')
        loans._save(other,{'schema':1,'accountKey':'account-B','equipment':{'other':'r8'},'artifacts':{}})
        original=self.client.call;seen=[]
        def call(route,body):
            if route=='EquipmentHandler.EquipsToRole':
                with sqlite3.connect(loans._database_path()) as db:
                    seen.append(db.execute('SELECT COUNT(*) FROM asset_recovery_assets WHERE journal_key=?',
                                           (loans._journal_key(self.path),)).fetchone()[0])
            return original(route,body)
        with patch.object(self.client,'call',side_effect=call):loans.restore(self.client,account_key='account-A')
        self.assertEqual(seen,[3,2,1])
        self.assertTrue(loans.pending(self.client,'account-B'))
        self.assertFalse(loans.pending(self.client,'account-A'))

    def test_crash_during_second_piece_resumes_only_remaining_sql_rows(self):
        self.save(dict.fromkeys(self.ids[:3],'r0'))
        original=self.client.call;count=0
        def crash(route,body):
            nonlocal count
            if route=='EquipmentHandler.EquipsToRole':
                count+=1
                if count==2:raise SystemExit('simulated app termination')
            return original(route,body)
        with patch.object(self.client,'call',side_effect=crash),self.assertRaises(SystemExit):
            loans.restore(self.client,account_key='account-A')
        restarted=AssetServer(self.client.server)
        self.assertTrue(loans.restore(restarted,account_key='account-A')['restored'])
        sent=[body['EquipIDs'][0] for route,body in restarted.calls if route=='EquipmentHandler.EquipsToRole']
        self.assertNotIn(self.ids[0],sent)
        actual=loans._owners(restarted.server['EquipmentContainer']['Datas'])
        self.assertEqual([actual[aid] for aid in self.ids[:3]],['r0']*3)
        self.assert_closed()


class TransportRecovery(unittest.TestCase):
    def wire(self):
        first,second=Mock(),Mock()
        self.enterContext(patch.object(config,'get_proxy',return_value=''))
        build=self.enterContext(patch.object(GameClient,'_build_client',side_effect=[first,second]))
        client=GameClient('offline');client.aid='aid';client.session_id='session';client.account_state={}
        second.request.return_value=httpx.Response(200,json={'Mails':[]},request=httpx.Request('PUT','https://offline.invalid'))
        return client,first,second,build

    def test_tls_connect_timeout_rebuilds_pool_and_retries_once(self):
        client,first,second,build=self.wire()
        first.request.side_effect=httpx.ConnectTimeout('_ssl.c:989: The handshake operation timed out')
        self.assertEqual(client.call('MailHandler.QueryNewestMails',{}),{'Mails':[]})
        self.assertEqual((first.request.call_count,second.request.call_count,build.call_count),(1,1,2))
        first.close.assert_called_once()

    def test_read_timeout_does_not_replay_a_potentially_committed_request(self):
        client,first,second,build=self.wire();first.request.side_effect=httpx.ReadTimeout('reply lost')
        with self.assertRaises(httpx.ReadTimeout):client.call('MailHandler.ReceiveMail',{})
        self.assertEqual((first.request.call_count,second.request.call_count,build.call_count),(1,0,1))

    def test_second_connect_timeout_is_reported_without_an_unbounded_retry(self):
        client,first,second,_=self.wire()
        first.request.side_effect=second.request.side_effect=httpx.ConnectTimeout('TLS timeout')
        with self.assertRaises(httpx.ConnectTimeout):client.call('MailHandler.QueryNewestMails',{})
        self.assertEqual((first.request.call_count,second.request.call_count),(1,1))


class ConcurrentStore(unittest.TestCase):
    def setUp(self):
        self.client=StoreClient(100)
        self.acc=SimpleNamespace(email='offline',client=self.client,params={},lock=threading.Lock())
        self.enterContext(patch.object(appcore,'_require',return_value=self.acc))
        self.enterContext(patch.object(appcore.accounts,'get',return_value=self.acc))
        self.enterContext(patch.object(activity_store,'catalog',return_value={'shops':GOODS}))
        self.enterContext(patch.object(config,'save_account'))
        self.enterContext(patch.object(appcore,'_status_snapshot',return_value={}))

    def test_store_read_works_while_farming_holds_account_lock(self):
        with self.acc.lock:
            result=appcore.activity_store_options('offline')
        self.assertEqual(result['activityStore']['shops'][0]['balance'],100)

    def test_store_save_and_purchase_wait_for_short_farming_operation(self):
        self.acc.lock.acquire();started=threading.Event();finished=threading.Event();results=[]
        def work():
            started.set()
            try:
                appcore.activity_store_config('offline','ActivityH806',{'items':['p'],'stopWhenBought':False})
                results.append(appcore.activity_store_buy('offline','ActivityH806'))
            except Exception as error:results.append(error)
            finally:finished.set()
        thread=threading.Thread(target=work,daemon=True);thread.start();started.wait(1)
        self.acc.lock.release();self.assertTrue(finished.wait(3));thread.join()
        self.assertEqual(len(results),1)
        self.assertIsInstance(results[0],dict)
        self.assertEqual(len(self.client.calls),1)


class AttendanceRecovery(unittest.TestCase):
    def test_optional_entry_failure_cannot_prevent_real_attendance(self):
        client=CheckInProgress().setup_client();original=client.call
        def call(route,data=None):
            if route=='ServerStatusHandler.QueryGuildStatus':raise httpx.ConnectTimeout('TLS timeout')
            return original(route,data)
        client.call=call
        self.assertTrue(GuildService(client,{}).enter()['checkedIn'])
        self.assertEqual(len(client.routes('GuildMemberCheckIn')),1)

    def test_day_change_requeues_attendance_without_a_relogin_or_enabled_switch(self):
        client=CheckInProgress().setup_client()
        self.enterContext(patch.object(config,'load_accounts',return_value={}))
        self.enterContext(patch.object(accounts.time,'time',return_value=NOW/1000))
        acc=accounts.Account('offline',client,None)
        acc.guild_runtime['enteredRevision']=acc.login_revision
        self.assertEqual(acc._guild_due_delay(),0)
        self.assertFalse(acc.toggles.get('guild_auto_signin'))
        acc._run_guild_if_due()
        self.assertEqual(len(client.routes('GuildMemberCheckIn')),1)


class DailySigninGate(unittest.TestCase):
    def setUp(self):
        self.now=NOW
        self.enterContext(patch.object(tasks.time,'time',side_effect=lambda:self.now/1000))
        self.client=Mock(spec=GameClient)
        self.client.account_state={'Info':{},'Utc':str(self.now//1000),
            'WeekSignInDataContainer':{'WeekSignInDataList':[]},'MailContainer':{'Mails':[]}}
        self.client._auth_data.side_effect=lambda data=None:data or {}
        def call(route,data):
            if route=='MonthSignInHandler.SignIn':return {'NowDay':7,'LastSignInTime':{'$date':self.now},'Utc':str(self.now//1000)}
            if route=='ServerStatusHandler.Query':return {'HasMail':False}
            if route=='MailHandler.QueryNewestMails':return {'Mails':[]}
            raise AssertionError(route)
        self.client.call.side_effect=call
        self.runtime={'loginRevision':1,'stateSyncedAtMs':self.now}

    def test_repeated_polls_and_same_day_relogin_remain_complete_until_next_game_day(self):
        self.assertTrue(tasks.task_month_signin(self.client,runtime=self.runtime)['ok'])
        count=self.client.call.call_count
        self.assertFalse(tasks.month_signin_status(self.runtime,self.now)['ready'])
        self.runtime['loginRevision']=2
        for _ in range(5):
            self.assertTrue(tasks.task_month_signin(self.client,runtime=self.runtime)['skipped'])
        self.assertEqual(self.client.call.call_count,count)
        self.now+=86_400_000
        self.assertTrue(tasks.month_signin_status(self.runtime,self.now)['ready'])
        self.assertTrue(tasks.task_month_signin(self.client,runtime=self.runtime)['ok'])
        self.assertEqual(sum(row.args[0]=='MonthSignInHandler.SignIn' for row in self.client.call.call_args_list),2)

    def test_account_runtime_reset_preserves_completed_day(self):
        self.assertTrue(tasks.task_month_signin(self.client,runtime=self.runtime)['ok'])
        self.enterContext(patch.object(config,'load_accounts',return_value={}))
        acc=accounts.Account('offline',self.client,None)
        acc.task_runtime['month_signin']=self.runtime
        acc.reset_task_runtime()
        acc.login_revision=2
        self.assertIs(acc.task_runtime['month_signin'],self.runtime)
        self.assertFalse(acc.next_claims(self.now)['month_signin']['ready'])


if __name__=='__main__':unittest.main()
