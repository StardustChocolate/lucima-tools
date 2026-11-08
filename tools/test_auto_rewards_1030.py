"""Login shelf inspections and native guild-war settlement, entirely offline."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from tools import test_arena_reward as base
from backend import accounts, appcore, arena_reward, config, guild_war_reward as war, tasks
from backend.game_client import GameError,GameMessage


class AutoRewardTests(unittest.TestCase):
    setUp=base.ArenaRewardTests.setUp

    def shelf(self):
        return {'Store':'SecretShop','LastResetTime':{'$date':self.now},
                'DropResult':{'Items':[]}}

    def enable_shop(self):
        self.acc.client.account_state['StoreRecordContainer']={'Records':[self.shelf()]}
        self.acc.params={'shop_free':{'wanted':[]}}
        self.acc.toggles={'shop_free':True}
        self.acc.notify_config_changed()

    def test_saved_shop_toggle_inspects_immediately_after_login_despite_refresh_cooldown(self):
        self.client.account_state['StoreRecordContainer']={'Records':[self.shelf()]}
        config.save_account(self.acc.email,toggles={'shop_free':True},params={'shop_free':{'wanted':[]}})
        restored=accounts.Account(self.acc.email,self.client,None)
        self.assertIn('shop_free',restored._due_ids())
        result=restored.run_due()
        self.assertTrue(result[0]['ok']);self.assertIn('当前货架巡检完成',result[0]['detail'])
        self.client.call.assert_not_called()
        self.assertEqual(restored.run_due(),[])
        self.assertEqual(restored.run_toggles(['shop_free'],'开关·'),[])

    def test_disabled_shop_waits_for_enable_then_patrols_once_and_refreshes_at_deadline(self):
        self.client.account_state['StoreRecordContainer']={'Records':[self.shelf()]}
        self.assertEqual(self.acc.run_due(),[])
        self.enable_shop()
        self.assertTrue(self.acc.run_due()[0]['ok'])
        self.assertEqual(self.acc.run_due(),[])
        self.now+=tasks.SHOP_FREE_COOLDOWN_MS-1
        self.assertEqual(self.acc.run_due(),[])
        self.now+=1
        def refresh(route,data):
            self.assertEqual((route,data['IsUseGold']),('StoreHandler.ResetRandomStore',0))
            response={'Records':[self.shelf()]}
            self.client.account_state['StoreRecordContainer']=deepcopy(response)
            return response
        self.client.call.side_effect=refresh
        self.assertTrue(self.acc.run_due()[0]['ok'])
        self.assertEqual(self.client.call.call_count,1)
        self.assertEqual(self.acc.run_due(),[])

    def test_successful_relogin_and_reenable_each_queue_one_shelf_inspection(self):
        self.enable_shop();self.acc.run_due()
        self.acc.note_state_refreshed()
        self.assertIn('shop_free',self.acc._due_ids());self.acc.run_due()
        self.acc.toggles['shop_free']=False;self.acc.notify_config_changed()
        self.assertEqual(self.acc.run_due(),[])
        self.acc.toggles['shop_free']=True;self.acc.notify_config_changed()
        self.assertIn('shop_free',self.acc._due_ids());self.acc.run_due()
        self.assertEqual(self.acc.run_due(),[])
        self.client.call.assert_not_called()

    def guild(self):
        self.client.account_state['PlayerGuildInfo']={'GID':'guild'}
        self.acc.toggles={war.TASK_ID:True}

    def receipt(self,count=62):
        drop={'Items':[{'Item':{'StaticID':'6','Count':count},'NowItem':{'StaticID':'6','Count':1000}}]}
        return {'WarEndReward':{'WarID':'666~3','IsWin':False,'Drop':deepcopy(drop)},
                'Drop':deepcopy(drop),'Utc':str(self.now//1000)}

    def test_war_days_never_send_a_claim_and_off_days_match_previous_battle(self):
        self.guild()
        for day,previous in [('2026-10-26',None),('2026-10-27','2026-10-26'),
                             ('2026-10-28',None),('2026-10-29','2026-10-28'),
                             ('2026-10-30',None),('2026-10-31','2026-10-30'),('2026-11-01','2026-10-30')]:
            self.now=base.stamp(day+'T12:00:00')
            self.assertEqual(war.period(self.now),previous)
            if previous is None:self.assertTrue(war.run(self.client,{})['skipped'])
        self.client.call.assert_not_called()

    def test_double_present_receipt_counts_62_once_and_survives_restart(self):
        self.now=base.stamp('2026-10-29T09:15:00');self.guild()
        self.client.call.side_effect=[self.receipt()]
        result=self.acc.run_due()[0]
        self.assertEqual(result['yellowTickets'],62)
        self.assertIn('09:15:00',result['detail'])
        self.assertEqual(self.client.call.call_args.args,(war.ROUTE,{'AID':'aid','SessionID':'sid'}))
        self.assertEqual(self.acc.run_due(),[])
        next_claims=self.acc.next_claims()[war.TASK_ID]
        self.assertIn('黄票 ×62',next_claims['note'])
        self.assertIn('2026-10-29 09:15',next_claims['note'])
        restarted=accounts.Account(self.acc.email,self.client,None)
        self.assertEqual(restarted.params[war.TASK_ID]['lastReward']['yellowTickets'],62)
        restarted.toggles={war.TASK_ID:True}
        self.assertEqual(restarted.run_due(),[])

    def test_friday_reward_is_claimed_only_once_across_saturday_and_sunday(self):
        self.now=base.stamp('2026-10-31T08:30:00');self.guild()
        self.client.call.side_effect=[self.receipt()]
        self.assertTrue(self.acc.run_due()[0]['ok'])
        self.now=base.stamp('2026-11-01T13:00:00')
        self.assertEqual(self.acc.run_due(),[])
        self.assertEqual(self.client.call.call_count,1)
        self.assertEqual(self.acc.next_claims()[war.TASK_ID]['nextMs'],war.check_at('2026-11-02','guild'))

    def test_empty_reward_finishes_window_without_looping(self):
        self.now=base.stamp('2026-10-29T08:02:00');self.guild()
        self.client.call.side_effect=[{'GuildWarData':{'IsWarNow':True},'WarEndReward':None}]
        result=self.acc.run_due()[0]
        self.assertTrue(result['skipped'])
        self.assertNotIn('lastReward',self.acc.params[war.TASK_ID])
        self.assertEqual(self.acc.run_due(),[])
        self.now+=3600_000
        self.assertEqual(self.acc.run_due(),[])
        self.assertEqual(self.client.call.call_count,1)
        self.assertEqual(self.acc.next_claims()[war.TASK_ID]['nextMs'],war.check_at('2026-10-30','guild'))

    def test_war_checks_once_at_stable_offset_between_one_and_two_minutes_after_reset(self):
        self.now=base.stamp('2026-10-27T08:00:00');self.guild()
        due=self.acc.next_claims()[war.TASK_ID]['nextMs']
        self.assertGreaterEqual(due,self.now+60_000);self.assertLessEqual(due,self.now+120_000)
        self.assertEqual(self.acc.next_claims()[war.TASK_ID]['nextMs'],due)
        self.assertEqual(self.acc.run_due(),[])
        self.now=due-1;self.assertEqual(self.acc.run_due(),[])
        self.now=due;self.client.call.return_value={}
        self.assertTrue(self.acc.run_due()[0]['skipped'])
        self.assertEqual(self.acc.run_due(),[])
        self.client.call.assert_called_once()

    def test_offline_login_catches_up_unchecked_current_period_then_persists_it(self):
        self.now=base.stamp('2026-10-29T15:05:00');self.guild()
        config.save_account(self.acc.email,toggles={war.TASK_ID:True},params={})
        self.client.call.return_value=self.receipt()
        restored=accounts.Account(self.acc.email,self.client,None)
        self.assertIn(war.TASK_ID,restored._due_ids())
        self.assertEqual(restored.run_due()[0]['yellowTickets'],62)
        again=accounts.Account(self.acc.email,self.client,None)
        self.assertEqual(again.run_due(),[])
        self.client.call.assert_called_once()

    def test_enable_after_missing_check_wakes_scheduler_and_checks_current_period_once(self):
        self.now=base.stamp('2026-10-29T15:05:00');self.guild()
        self.acc.toggles={war.TASK_ID:False};self.assertEqual(self.acc.run_due(),[])
        self.acc.task_runtime[war.TASK_ID]={'retryAtMs':self.now+300_000}
        self.client.call.return_value=self.receipt()
        with patch.object(appcore,'_require',return_value=self.acc):
            appcore.set_toggles(self.acc.email,{war.TASK_ID:True})
            self.assertTrue(self.acc._wake.is_set())
            self.assertEqual(self.acc.run_due()[0]['yellowTickets'],62)
            appcore.set_toggles(self.acc.email,{war.TASK_ID:False})
            appcore.set_toggles(self.acc.email,{war.TASK_ID:True})
        self.assertEqual(self.acc.run_due(),[])
        self.client.call.assert_called_once()

    def test_game_day_uses_eight_am_and_sunday_is_only_a_missed_saturday_fallback(self):
        for value,expected in [('2026-10-27T07:59:59',None),('2026-10-27T08:00:00','2026-10-26'),
                ('2026-10-28T07:59:59','2026-10-26'),('2026-10-28T08:00:00',None),
                ('2026-10-31T07:59:59',None),('2026-10-31T08:00:00','2026-10-30'),
                ('2026-11-02T07:59:59','2026-10-30'),('2026-11-02T08:00:00',None)]:
            with self.subTest(value=value):self.assertEqual(war.period(base.stamp(value)),expected)
        self.now=base.stamp('2026-11-01T08:05:00');self.guild()
        self.client.call.side_effect=[self.receipt()]
        self.assertEqual(self.acc.run_due()[0]['yellowTickets'],62)
        self.now=base.stamp('2026-11-02T07:59:00')
        self.assertEqual(self.acc.run_due(),[])

    def test_war_no_guild_no_call_and_failures_keep_receipt_absent(self):
        self.now=base.stamp('2026-10-29T12:00:00')
        self.assertTrue(war.run(self.client,{})['skipped'])
        self.client.call.assert_not_called()
        self.guild();self.client.call.side_effect=GameMessage('Duplicate Login')
        self.assertFalse(self.acc.run_due()[0]['ok'])
        self.assertNotIn('lastReward',self.acc.params[war.TASK_ID])
        self.assertTrue(self.acc.next_claims()[war.TASK_ID]['retrying'])

    def test_arena_legacy_enabled_setting_migrates_and_auto_toggle_controls_its_schedule(self):
        saved=arena_reward.configure({}, {**arena_reward.DEFAULTS,'enabled':True},self.now)
        config.save_account(self.acc.email,params={arena_reward.TASK_ID:saved})
        migrated=accounts.Account(self.acc.email,self.client,None)
        self.assertTrue(migrated.toggles[arena_reward.TASK_ID])
        self.assertIn(arena_reward.TASK_ID,migrated._enabled_ids())
        with patch.object(appcore,'_require',return_value=migrated):
            appcore.set_toggles(migrated.email,{arena_reward.TASK_ID:False})
            self.assertFalse(migrated.params[arena_reward.TASK_ID]['enabled'])
            self.assertNotIn('nextRunAtMs',migrated.params[arena_reward.TASK_ID])
            appcore.set_toggles(migrated.email,{arena_reward.TASK_ID:True})
            self.assertEqual(migrated.params[arena_reward.TASK_ID]['nextRunAtMs'],saved['nextRunAtMs'])


if __name__=='__main__':unittest.main()
