"""Star-source inventory settlement uses returned costs/balances, never UI guesses."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import httpx

from backend import appcore, config, tasks
from backend.game_client import GameClient
from tools.test_star_source_records import star_receipt


class StarSourceBalance(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        patcher=patch.object(config,'_DATA_DIR',Path(temporary.name));patcher.start();self.addCleanup(patcher.stop)
        self.client=GameClient('test');self.addCleanup(self.client.close)
        self.client.aid='offline-balance';self.client.session_id='offline-session'
        self.client.account_state={'ItemContainer':{'Items':[{'StaticID':'109','Count':13}]}}
        self.responses=[]
        self.client._client.close()
        def respond(request):
            body=self.responses.pop(0)
            return httpx.Response(200,json=body) if isinstance(body,dict) else httpx.Response(200,text=body)
        self.client._client=httpx.Client(transport=httpx.MockTransport(respond))
        patcher=patch.object(self.client,'_sync_proxy');patcher.start();self.addCleanup(patcher.stop)

    def receipt(self, identifier, balance=None):
        body=star_receipt(identifier)
        cost={'Item':{'StaticID':'109','Count':5}}
        if balance is not None:cost['NowItem']={'StaticID':'109','Count':balance}
        body['CostItems']=[cost]
        return body

    def balance(self):return appcore.summarize(self.client.account_state)['currencies']['starSource']

    def test_two_successful_purchases_reduce_the_actual_balance_five_each(self):
        self.responses=[self.receipt('one',8),self.receipt('two',3)]
        for index,expected in ((0,8),(1,3)):
            self.assertTrue(tasks.star_source_buy(self.client,index)['ok'])
            self.assertEqual(self.balance(),expected)

    def test_absolute_server_balance_wins_without_a_second_debit(self):
        self.responses=[self.receipt('one',9)]
        tasks.star_source_buy(self.client,0)
        self.assertEqual(self.balance(),9)

    def test_confirmed_cost_without_now_item_debits_the_known_snapshot_once(self):
        self.responses=[self.receipt('one'),self.receipt('two')]
        tasks.star_source_buy(self.client,0);self.assertEqual(self.balance(),8)
        tasks.star_source_buy(self.client,1);self.assertEqual(self.balance(),3)

    def test_failed_purchase_never_debits_the_balance(self):
        self.responses=['NotEnoughItem']
        self.assertFalse(tasks.star_source_buy(self.client,0)['ok'])
        self.assertEqual(self.balance(),13)

    def test_missing_cost_is_not_fabricated_from_the_five_unit_price(self):
        body=self.receipt('one');body.pop('CostItems');self.responses=[body]
        tasks.star_source_buy(self.client,0)
        self.assertEqual(self.balance(),13)

    def test_other_actions_do_not_enable_cost_only_inventory_updates(self):
        self.client._apply_now_items({'CostItems':[{'Item':{'StaticID':'109','Count':5}}]})
        self.assertEqual(self.balance(),13)


if __name__=='__main__':unittest.main()
