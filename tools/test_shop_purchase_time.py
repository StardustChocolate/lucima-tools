"""Native seconds-based Utc, real receipt dates and one-time legacy repair."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import sqlite3
import unittest

from backend import config,recruitment as archive,shop_records as ledger

ROOT=Path(__file__).resolve().parents[1]
NATIVE=json.loads((ROOT/'tools/fixtures/secret_shop_native_purchase.json').read_text())


class PurchaseTime(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name)
        patcher=patch.object(config,'_DATA_DIR',self.folder);patcher.start();self.addCleanup(patcher.stop)
        self.client=SimpleNamespace(aid='native-time-test',account_state={})

    def summary(self):return archive.shop_summary(archive.account_key(self.client),archive.catalog())

    def test_native_receipt_is_in_actual_month_and_day_with_exact_spending(self):
        archive.observe_shop_purchase(self.client,NATIVE['request'],NATIVE['response'])
        month=self.summary()['months'][0]
        self.assertEqual(month['month'],'2026-10')
        self.assertEqual(month['days'][0]['day'],'2026-10-29')
        self.assertEqual((month['boughtRecruit'],month['gold']),(5,184000))
        self.assertEqual(month['gains'],[{'id':'5','name':'招募契约','count':5}])

    def test_bson_last_buy_date_takes_precedence_and_preserves_milliseconds(self):
        self.assertEqual(ledger.purchase_time(NATIVE['response']),1793270587150)
        response=deepcopy(NATIVE['response']);response['Utc']='1793270589'
        self.assertEqual(ledger.purchase_time(response),1793270587150)

    def test_utc_fallback_accepts_native_seconds_and_millisecond_variants(self):
        for value in ('1793270587',1793270587,1793270587.125,'1793270587.125'):
            with self.subTest(value=value):
                self.assertEqual(ledger.purchase_time({'Utc':value}),int(round(float(value)*1000)))
        for value in (1793270587000,'1793270587000',{'$date':1793270587000},'2026-10-29T10:43:07Z'):
            with self.subTest(value=value):self.assertEqual(ledger.purchase_time({'Utc':value}),1793270587000)

    def test_invalid_or_missing_server_clock_does_not_invent_a_1970_purchase(self):
        for value in (None,True,'bad',0):
            with self.subTest(value=value),patch.object(ledger.time,'time',return_value=1793270600):
                self.assertEqual(ledger.purchase_time({'Utc':value}),1793270600000)

    def test_seconds_at_game_midnight_are_grouped_in_east_eight_time(self):
        response=deepcopy(NATIVE['response']);response['CommodityRecord'].pop('LastBuyTime')
        response['Utc']='1793462400'  # November 1, 00:00 in game time.
        archive.observe_shop_purchase(self.client,NATIVE['request'],response)
        month=self.summary()['months'][0]
        self.assertEqual((month['month'],month['days'][0]['day']),('2026-11','2026-11-01'))

    def test_existing_second_dates_are_migrated_once_without_changing_money_ids_or_quantities(self):
        path=self.folder/'records/recruitment.sqlite3';path.parent.mkdir(parents=True)
        key=archive.account_key(self.client)
        payload={'cost':{'1':184000,'2':0},'costKnown':True,'items':{'5':10,'6':50,'EC11':15},'equipment':[]}
        other={'cost':{'1':0,'2':270},'costKnown':True}
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE shop_actions (account TEXT,id TEXT,time INTEGER,kind TEXT,data TEXT,PRIMARY KEY(account,id))')
            db.execute('INSERT INTO shop_actions VALUES (?,?,?,?,?)',(key,'original-purchase-id',1793270587,'purchase',json.dumps(payload)))
            db.execute('INSERT INTO shop_actions VALUES (?,?,?,?,?)',(key,'refresh-id',1793270587000,'refresh',json.dumps(other)))
            db.execute('INSERT INTO shop_actions VALUES (?,?,?,?,?)',(key,'valid-ms-purchase',1793184187000,'purchase',json.dumps({**payload,'items':{'5':5}})))
            db.execute('INSERT INTO shop_actions VALUES (?,?,?,?,?)',('other-account','other-id',1793270587,'purchase',json.dumps(payload)))
        for _ in range(3):
            months=self.summary()['months'];self.assertEqual([m['month'] for m in months],['2026-10'])
            month=months[0];self.assertEqual((month['boughtRecruit'],month['boughtMystery'],month['chips'][0]['count']),(15,50,15))
            self.assertEqual((month['gold'],month['diamonds']),(368000,270))
            days={d['day']:d for d in month['days']}
            self.assertEqual(days['2026-10-29']['boughtRecruit'],10)
            self.assertEqual(days['2026-10-28']['boughtRecruit'],5)
        with sqlite3.connect(path) as db:
            actual=db.execute('SELECT id,time,kind,data FROM shop_actions WHERE account=? ORDER BY id',(key,)).fetchall()
            self.assertEqual(len(actual),3)
            self.assertEqual(next(r[1] for r in actual if r[0]=='original-purchase-id'),1793270587000)
            self.assertEqual(json.loads(next(r[3] for r in actual if r[0]=='original-purchase-id')),payload)
            self.assertEqual(next(r[1] for r in actual if r[0]=='valid-ms-purchase'),1793184187000)
            self.assertEqual(db.execute('SELECT time FROM shop_actions WHERE account=?',('other-account',)).fetchone()[0],1793270587000)
            self.assertEqual(db.execute('SELECT count(*) FROM record_migrations').fetchone()[0],1)


if __name__=='__main__':unittest.main()
