"""Shared native supply rules, reset periods and server purchase counters."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import time
import threading
from . import resource_store
from .game_client import GameError


@lru_cache(maxsize=1)
def catalog():
    value=json.loads(resource_store.path('assets/automation_supplies.json').read_text(encoding='utf-8'))
    if value.get('schema')!=1:raise GameError('自动补给资料无效')
    return value


def period(now_ms,kind):
    day=int(now_ms)//86_400_000
    if kind=='week':return (day+3)//7
    d=datetime.fromtimestamp(now_ms/1000,timezone.utc)
    return d.year,d.month


def next_reset(now_ms,kind):
    d=datetime.fromtimestamp(now_ms/1000,timezone.utc)
    if kind=='week':return int((d.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=7-d.weekday())).timestamp()*1000)
    return int(d.replace(year=d.year+(d.month==12),month=1 if d.month==12 else d.month+1,day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)


def record(state,identifier,store):
    rows=(state.get('StoreRecordContainer')or{}).get('Records')
    if not isinstance(rows,list):raise GameError('商店限购记录缺失，请刷新账号')
    from .tasks import _synth_record
    return next((row for row in rows if row.get('StaticID')==identifier and row.get('Store')==store),_synth_record(identifier,store))


def count(row,now_ms,kind):
    from .tasks import _ms
    value=row.get('BuyCount')
    if type(value) is not int or value<0:raise GameError('限购次数无效')
    stamp=_ms(row.get('LastResetTime'))
    if isinstance(stamp,(int,float)) and stamp>0 and period(stamp,kind)!=period(now_ms,kind):return 0
    return value


def money(state):
    return next((row.get('Count',0) for row in (state.get('ItemContainer')or{}).get('Items')or[] if str(row.get('StaticID'))=='1'),0)


def now():return int(time.time()*1000)


def game_now(client):
    stamp=getattr(client,'_state_received_at_ms',None)
    if stamp is not None:
        try:return int(float(client.account_state['Utc'])*1000)+now()-stamp
        except (KeyError,ValueError,TypeError):pass
    return now()


def borrower_busy(client):
    return any(owner is not None and owner!=threading.get_ident() for owner in
               (getattr(client,'_intimacy_asset_owner',None),getattr(client,'_automation_asset_owner',None)))
