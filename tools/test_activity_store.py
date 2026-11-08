"""Event store receipts, affordability and server-side sweep stop gates."""
from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch,Mock

from backend import activity_store as store, appcore
from backend.game_client import GameError


def good(sid, item='34', price=10, limit=2, **fields):
    return {'store':'ActivityH806','activity':'BranchH806','activities':[], 'currency':'AH806',
            'price':price,'item':item,'count':1,'limit':limit,'nameKey':'', **fields}


GOODS={'p':good('p'), 'q':good('q',price=20,limit=1),
       'u':good('u',item='C031',price=25,limit=None),
       'g15':good('g15',item='1',price=15,limit=None),
       'g1':good('g1',item='1',price=1,limit=None),
       'expired':good('expired',store='ActivityH610',activity='BranchH610',currency='AH610')}


class Client:
    def __init__(self,balance=100):
        self.account_state={'ActivityDBSettingContainer':{'SettingList':[{'ActivityID':'BranchH806','IsIgnoreTime':True}]},
             'StoreRecordContainer':{'Records':[]},'ItemContainer':{'Items':[{'StaticID':'AH806','Count':balance}]}}
        self.calls=[];self.bad_receipt=False
    def _auth_data(self,value=None):return value or {}
    def _apply_now_items(self,response):
        now=response['CostItems'][0]['NowItem']
        self.account_state['ItemContainer']['Items']=[deepcopy(now)]
    def _apply_commodity_record(self,response):
        rec=deepcopy(response['CommodityRecord']);rows=self.account_state['StoreRecordContainer']['Records']
        rows[:]=[r for r in rows if r['StaticID']!=rec['StaticID']]+[rec]
    def call(self,route,body):
        self.calls.append((route,deepcopy(body)))
        sid,count=body['Record']['StaticID'],body['Count'];meta=GOODS[sid]
        balance=self.account_state['ItemContainer']['Items'][0]['Count']
        assert meta['store']=='ActivityH806' and meta['price']*count<=balance
        rec={**body['Record'],'BuyCount':body['Record']['BuyCount']+count}
        response={'CommodityRecord':rec,'CostItems':[{'NowItem':{'StaticID':'AH806','Count':balance-meta['price']*count}}],
                  'Drop':{'Items':[{'Item':{'StaticID':meta['item'],'Count':count}}]}}
        if self.bad_receipt:response.pop('CostItems')
        return response


class ActivityStore(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(store,'catalog',return_value={'shops':GOODS});self.patch.start();self.addCleanup(self.patch.stop)
    def config(self,items=('p','q'),stop=True,**values):
        return {'ActivityH806':{'items':list(items),'stopWhenBought':stop,**values}}

    def test_only_live_shop_and_native_full_limits_are_present(self):
        snap=store.snapshot(Client(),self.config())
        self.assertEqual([r['id']for r in snap['shops']],['ActivityH806'])
        rows={r['id']:r for r in snap['shops'][0]['goods']}
        self.assertEqual(rows['p']['target'],2);self.assertFalse(snap['shops'][0]['complete'])

    def test_buy_targets_then_exchange_all_remainder_only_at_one_point_option(self):
        client=Client(103);result=store.process(client,self.config(),'ActivityH806')
        self.assertTrue(result['stop']);self.assertEqual(result['balance'],0)
        self.assertEqual([(body['Record']['StaticID'],body['Count'])for _,body in client.calls],[('p',2),('q',1),('g1',63)])
        store.process(client,self.config(),'ActivityH806');self.assertEqual(len(client.calls),3)

    def test_insufficient_points_buys_affordable_count_then_resumes_after_next_drop(self):
        client=Client(15);result=store.process(client,self.config(),'ActivityH806')
        self.assertFalse(result['stop']);self.assertEqual(result['balance'],5)
        client.account_state['ItemContainer']['Items'][0]['Count']=37
        result=store.process(client,self.config(),'ActivityH806')
        self.assertTrue(result['stop']);self.assertEqual(result['balance'],0)
        self.assertEqual([b['Count']for _,b in client.calls],[1,1,1,7])

    def test_stop_switch_off_keeps_points_after_targets_are_bought(self):
        client=Client();result=store.process(client,self.config(stop=False),'ActivityH806')
        self.assertTrue(result['complete']);self.assertFalse(result['stop']);self.assertEqual(result['balance'],60)

    def test_empty_selection_never_completes_or_sells_points(self):
        client=Client();result=store.process(client,self.config(items=[]),'ActivityH806')
        self.assertFalse(result['stop']);self.assertEqual(client.calls,[])

    def test_unlimited_goods_have_explicit_target_quantity(self):
        client=Client();result=store.process(client,self.config(items=['u'],quantities={'u':3}),'ActivityH806')
        self.assertTrue(result['stop']);self.assertEqual([b['Count']for _,b in client.calls],[3,25])

    def test_record_missing_is_synthesized_but_invalid_selection_is_rejected(self):
        client=Client();store.process(client,self.config(items=['p']),'ActivityH806')
        self.assertEqual(client.calls[0][1]['Record']['_id'],'')
        for items in (['expired'],['g1'],['unknown']):
            with self.subTest(items=items),self.assertRaises(GameError):store.normalize({'items':items},'ActivityH806')

    def test_unknown_purchase_result_stops_without_retry_or_later_purchase(self):
        client=Client();client.bad_receipt=True
        with self.assertRaises(GameError):store.process(client,self.config(),'ActivityH806')
        self.assertEqual(len(client.calls),1);self.assertTrue(client._activity_store_uncertain)

    def test_already_bought_targets_gate_server_before_any_battle_request(self):
        client=Client();cfg=self.config();store.process(client,cfg,'ActivityH806')
        acc=SimpleNamespace(client=client,params={'activity_store':cfg},lock=threading.RLock())
        with patch.object(appcore,'_require',return_value=acc),patch.object(appcore,'_activity_support_entry',return_value=None), \
             patch.object(appcore,'_status_snapshot',return_value={}),patch.object(appcore,'activity_once')as battle:
            result=appcore.activity_run('alpha','BH806_1_12','T')
        self.assertTrue(result['storeStop']);self.assertTrue(result['skipped']);battle.assert_not_called()

    def test_drop_funds_purchase_and_stops_current_sweep_after_one_battle(self):
        client=Client(0);cfg=self.config(items=['q'])
        acc=SimpleNamespace(client=client,params={'activity_store':cfg},lock=threading.RLock())
        def battle(*args):
            client.account_state['ItemContainer']['Items'][0]['Count']=21
            return {'ok':True,'detail':'finished'}
        with patch.object(appcore,'_require',return_value=acc),patch.object(appcore,'_activity_support_entry',return_value=None), \
             patch.object(appcore,'_status_snapshot',return_value={}),patch.object(appcore,'activity_once',side_effect=battle)as run:
            result=appcore.activity_run('alpha','BH806_1_12','T')
        self.assertEqual(run.call_count,1);self.assertTrue(result['storeStop']);self.assertEqual(result['activityStore']['shops'][0]['balance'],0)

    def test_enabling_stop_after_targets_were_already_bought_exchanges_spare_points(self):
        client=Client(23)
        client.account_state['StoreRecordContainer']['Records']=[{'StaticID':'p','Store':'ActivityH806','BuyCount':2}]
        acc=SimpleNamespace(client=client,email='alpha',params={})
        with patch.object(appcore,'_guild_account',return_value=nullcontext(acc)), \
             patch.object(appcore,'_status_snapshot',return_value={}),patch.object(appcore.config,'save_account'):
            result=appcore.activity_store_config('alpha','ActivityH806',{'items':['p'],'stopWhenBought':True})
        self.assertTrue(result['activityStore']['shops'][0]['stop'])
        self.assertEqual(client.calls[0][1]['Record']['StaticID'],'g1')
        self.assertEqual(client.calls[0][1]['Count'],23)

    def test_generic_toggle_save_cannot_roll_back_dedicated_store_configuration(self):
        saved=self.config(items=['q'])
        acc=SimpleNamespace(email='alpha',params={'activity_store':deepcopy(saved)},toggles={},notify_config_changed=Mock())
        with patch.object(appcore,'_require',return_value=acc),patch.object(appcore.config,'save_account'):
            appcore.set_toggles('alpha',{}, {'activity_store':{'ActivityH806':{'items':[]}}})
        self.assertEqual(acc.params['activity_store'],saved)


if __name__=='__main__':unittest.main()
