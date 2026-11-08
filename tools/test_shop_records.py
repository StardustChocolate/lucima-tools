"""Native purchase accounting, precise spending, legacy retention and deduplication."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3
import unittest

import httpx
from backend import config,recruitment as archive,shop_records as ledger,tasks
from backend.game_client import GameClient,GameMessage
from tools.test_recruitment import STAMP,shelves


def shelf(sid='EC11',*,slot='a',stamp=STAMP,equip=None):
    return {'_id':{'$oid':slot},'Store':'SecretShop','StaticID':'SecretShop1','BuyCount':0,
            'LastResetTime':{'$date':stamp},'DropResult':{'Items':[
                {'Equipment':equip} if equip else {'Item':{'StaticID':sid,'Count':2}}]}}


def purchase(record,*,gold=100,diamonds=0,stamp=STAMP):
    after=deepcopy(record);after['BuyCount']=record['BuyCount']+1
    return {'CommodityRecord':after,'Utc':{'$date':stamp},'Drop':deepcopy(record['DropResult']),
            'CostItems':[{'Item':{'StaticID':sid,'Count':count},'NowItem':{'StaticID':sid,'Count':999999}}
                         for sid,count in (('1',gold),('2',diamonds)) if count]}


class ShopAccounting(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data_dir=Path(self.temp.name)
        patcher=patch.object(config,'_DATA_DIR',self.data_dir);patcher.start();self.addCleanup(patcher.stop)
        self.client=SimpleNamespace(aid='shop-account',account_state={})

    def summary(self,client=None):
        return archive.shop_summary(archive.account_key(client or self.client),archive.catalog())

    def buy(self,record,**kw):
        response=purchase(record,**kw)
        archive.observe_shop_purchase(self.client,{'Record':tasks._to_send_format(record),'Count':1},response)
        return response

    def test_appearance_is_never_counted_as_a_purchase(self):
        body=shelves();body['CostItems']=[{'Item':{'StaticID':'2','Count':30}}]
        archive.observe_shop(self.client,{'StoreID':'SecretShop','IsUseGold':1},body)
        m=self.summary()['months'][0]
        self.assertEqual((m['recruit'],m['mystery']),(5,50))
        self.assertEqual((m['boughtRecruit'],m['boughtMystery'],m['purchases']),(0,0,0))
        self.assertEqual((m['gold'],m['diamonds'],m['paid'],m['free']),(0,30,1,0))

    def test_all_six_chip_types_count_only_bought_units(self):
        for i in range(1,7):self.buy(shelf('EC'+str(i)+'1',slot=str(i)),gold=100*i)
        # Higher grades of the same part belong to that chip type too.
        self.buy(shelf('EC13',slot='higher'))
        m=self.summary()['months'][0]
        self.assertEqual([c['count'] for c in m['chips']],[4,2,2,2,2,2])
        self.assertEqual(m['gold'],2200)
        self.assertEqual(m['days'][0]['chips'],m['chips'])

    def test_equipment_groups_by_actual_part_and_set(self):
        for i,(sid,set_id) in enumerate((('E0011','Speed'),('E0021','Speed'),('E0012','Speed'),('E0011','Attack'))):
            self.buy(shelf(slot=str(i),equip={'_id':str(i),'StaticID':sid,'Set':set_id}),gold=500)
        equipment=self.summary()['months'][0]['equipment']
        self.assertEqual({(e['slot'],e['set']):e['count'] for e in equipment},
                         {('weapon','Speed'):2,('weapon','Attack'):1,('helmet','Speed'):1})
        self.assertTrue(all(e['slotName'] and e['setName'] for e in equipment))

    def test_contract_purchases_use_returned_quantity_not_shelf_appearance(self):
        r=shelf('5');r['DropResult']['Items'][0]['Item']['Count']=5;response=purchase(r)
        response['Drop']['Items'][0]['Item']['Count']=10
        archive.observe_shop_purchase(self.client,{'Record':r,'Count':1},response)
        m=self.summary()['months'][0];self.assertEqual(m['boughtRecruit'],10);self.assertEqual(m['recruit'],0)

    def test_replayed_resets_and_purchases_never_double_count(self):
        r=shelf();response=purchase(r)
        for _ in range(3):
            body={**shelves(),'CostItems':[{'Item':{'StaticID':'2','Count':30}}]}
            archive.observe_shop(self.client,{'StoreID':'SecretShop','IsUseGold':1},body)
            archive.observe_shop_purchase(self.client,{'Record':tasks._to_send_format(r),'Count':1},response)
        m=self.summary()['months'][0];self.assertEqual((m['purchases'],m['refreshes'],m['gold'],m['diamonds']),(1,1,100,30))

    def test_serialized_utc_generation_separates_the_same_shelf_after_each_reset(self):
        r=shelf()
        for stamp in (STAMP,STAMP+3600000):
            r['LastResetTime']={'$date':stamp};body=purchase(r,stamp=stamp);body.pop('CommodityRecord')
            archive.observe_shop_purchase(self.client,{'Record':tasks._to_send_format(r),'Count':1},body)
        self.assertEqual(self.summary()['months'][0]['purchases'],2)

    def test_purchase_day_uses_actual_server_time_not_the_shelf_generation(self):
        r=shelf(stamp=1793462340000)  # October 31 23:59 in game time.
        self.buy(r,stamp=1793462460000)  # November 1 00:01 in game time.
        m=self.summary()['months'][0];self.assertEqual((m['month'],m['days'][0]['day']),('2026-11','2026-11-01'))

    def test_purchase_without_a_tool_refresh_is_recorded_and_persisted(self):
        self.buy(shelf(stamp=0))
        other=SimpleNamespace(aid=self.client.aid,account_state={})
        m=self.summary(other)['months'][0]
        self.assertEqual((m['refreshes'],m['purchases'],m['gold']),(0,1,100))

    def test_purchase_and_refresh_spending_are_both_in_month_and_day_totals(self):
        for paid in (0,1):
            body=shelves(STAMP+1000*paid);body['CostItems']=[{'Item':{'StaticID':'2','Count':30}}] if paid else []
            archive.observe_shop(self.client,{'StoreID':'SecretShop','IsUseGold':paid},body)
        self.buy(shelf(),gold=100000,diamonds=4)
        m=self.summary()['months'][0]
        self.assertEqual((m['gold'],m['diamonds'],m['paid'],m['free']),(100000,34,1,1))
        self.assertEqual([m[k] for k in ('gold','diamonds','paid','free')],[m['days'][0][k] for k in ('gold','diamonds','paid','free')])

    def test_old_database_is_retained_and_marks_missing_financial_history(self):
        path=self.data_dir/'records/recruitment.sqlite3';path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE shop (account TEXT,id TEXT,time INTEGER,paid INTEGER,recruit INTEGER,mystery INTEGER,recruit_hits INTEGER,mystery_hits INTEGER,PRIMARY KEY(account,id))')
            db.execute('INSERT INTO shop VALUES (?,?,?,?,?,?,?,?)',(archive.account_key(self.client),'old',STAMP,1,15,50,3,1))
        m=self.summary()['months'][0]
        self.assertEqual((m['recruit'],m['mystery'],m['paid'],m['legacyRefreshes']),(15,50,1,1))
        self.assertEqual((m['gold'],m['diamonds']),(0,0))
        self.buy(shelf());self.assertEqual(self.summary()['months'][0]['legacyRefreshes'],1)

    def test_cost_item_is_spent_amount_now_item_is_not(self):
        self.buy(shelf(),gold=123)
        self.assertEqual(self.summary()['months'][0]['gold'],123)

    def test_missing_cost_is_unknown_and_never_guessed_from_price(self):
        r=shelf();body=purchase(r);body.pop('CostItems')
        archive.observe_shop_purchase(self.client,{'Record':r,'Count':1},body)
        m=self.summary()['months'][0];self.assertEqual((m['gold'],m['unknownCosts'],m['purchases']),(0,1,1))

    def test_other_shops_and_accounts_are_isolated(self):
        r=shelf();r['Store']='MemberShop'
        archive.observe_shop_purchase(self.client,{'Record':r,'Count':1},purchase(r))
        self.assertEqual(self.summary()['months'],[])
        self.buy(shelf())
        self.assertEqual(self.summary(SimpleNamespace(aid='another',account_state={}))['months'],[])

    def test_bare_ack_is_not_proof_of_a_purchase(self):
        with self.assertRaisesRegex(Exception,'缺少已购道具'):
            archive.observe_shop_purchase(self.client,{'Record':shelf(),'Count':1},{'ok':True})
        self.assertEqual(self.summary()['months'],[])

    def test_confirmed_buy_count_can_recover_goods_when_drop_is_omitted(self):
        r=shelf();body=purchase(r);body.pop('Drop')
        archive.observe_shop_purchase(self.client,{'Record':r,'Count':1},body)
        self.assertEqual(self.summary()['months'][0]['chips'][0]['count'],2)

    def test_real_transport_measures_balance_only_cost_before_inventory_merge(self):
        r=shelf();body=purchase(r);body['CostItems']=[{'NowItem':{'StaticID':'1','Count':750}}]
        client=GameClient('test');self.addCleanup(client.close);client.aid=self.client.aid
        client.account_state={'ItemContainer':{'Items':[{'StaticID':'1','Count':1000}]}}
        client._client.close();client._client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body)))
        with patch.object(client,'_sync_proxy'):
            client.call('StoreHandler.BuyCommodity',{'Record':tasks._to_send_format(r),'Count':1})
        self.assertEqual(self.summary()['months'][0]['gold'],250)
        self.assertEqual(client.account_state['ItemContainer']['Items'][0]['Count'],750)

    def test_failed_purchase_response_is_not_recorded(self):
        client=GameClient('test');self.addCleanup(client.close);client.aid=self.client.aid;client.account_state={}
        client._client.close();client._client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='NotEnoughGold')))
        with patch.object(client,'_sync_proxy'),self.assertRaises(GameMessage):
            client.call('StoreHandler.BuyCommodity',{'Record':shelf(),'Count':1})
        self.assertEqual(self.summary()['months'],[])

    def test_recording_failure_never_retries_or_rejects_a_successful_purchase(self):
        r=shelf();body=purchase(r);calls=[]
        client=GameClient('test');self.addCleanup(client.close);client.aid=self.client.aid;client.account_state={}
        client._client.close()
        def respond(req):calls.append(req);return httpx.Response(200,json=body)
        client._client=httpx.Client(transport=httpx.MockTransport(respond))
        with (patch.object(client,'_sync_proxy'),patch.object(archive,'observe_shop_purchase',side_effect=OSError('只读')),
              patch.object(__import__('backend.game_client',fromlist=['log']).log,'exception') as log_error):
            self.assertEqual(client.call('StoreHandler.BuyCommodity',{'Record':r,'Count':1}),body)
            log_error.assert_called_once()
        self.assertEqual(len(calls),1);self.assertIn('只读',client._recruitment_record_error)


if __name__=='__main__':unittest.main()
