"""Member shop: cached eligibility, periodic automation and explicit purchases."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path

from .game_client import GameError
from .game_data import activity_open
from .guild import has_guild, object_id, positive_int, _nonnegative_int
from .tasks import _buy_payload, _synth_record

UTC = timezone.utc


@lru_cache(maxsize=1)
def _catalog():
    root = Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[1])
    data = json.loads((root / 'assets/guild_store.json').read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not data.get('goods'):
        raise GameError('团员商店清单无效，请更新应用资源')
    return data


def store_catalog():
    return _catalog()['goods']


def store_options(value=None):
    value = value if isinstance(value, dict) else {}
    selected = value.get('items')
    return [key for key, row in store_catalog().items()
            if row['reset'] and isinstance(selected, list) and key in selected]


def cycle(stamp, reset):
    if reset is None:
        return None
    date = datetime.fromtimestamp(stamp / 1000, UTC)
    if reset == 'Month':
        return date.year, date.month
    year, week, _ = date.isocalendar()
    return year, week


def reset_at(now, reset):
    date = datetime.fromtimestamp(now / 1000, UTC)
    if reset == 'Week':
        result = date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7 - date.weekday())
    else:
        result = datetime(date.year + (date.month == 12), date.month % 12 + 1, 1, tzinfo=UTC)
    return int(result.timestamp() * 1000)


def record_stamp(value):
    if isinstance(value, dict):
        value = value.get('$date')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            if '/' in value:
                parsed = datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=UTC)
            else:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
            return (parsed - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1000
        except ValueError:
            pass
    raise GameError('团员商店购买记录时间无效，请重新登录')


class GuildStoreService:
    def __init__(self, guild):
        self.guild, self.client, self.runtime = guild, guild.client, guild.runtime

    def record(self, identifier):
        state = self.client.account_state or {}
        records = (state.get('StoreRecordContainer') or {}).get('Records')
        if not isinstance(records, list):
            raise GameError('团员商店购买记录缺失，请重新登录')
        matches = [r for r in records if isinstance(r, dict) and r.get('Store') == 'MemberShop'
                   and r.get('StaticID') == identifier]
        if len(matches) > 1:
            raise GameError('团员商店购买记录重复，请重新登录')
        record = deepcopy(matches[0]) if matches else _synth_record(identifier, 'MemberShop')
        if _nonnegative_int(record.get('BuyCount')) is None:
            raise GameError('团员商店购买次数无效，请重新登录')
        reset = store_catalog()[identifier]['reset']
        if reset:
            stamp, now = record_stamp(record.get('LastResetTime')), self.guild.now_ms()
            # MinValue on a synthesized record is earlier than Unix epoch and
            # outside Windows datetime.fromtimestamp's supported range.
            if stamp < 0 or cycle(stamp, reset) != cycle(now, reset):
                if stamp > now:
                    raise GameError('团员商店记录时间超前，请重新登录')
                record.update(BuyCount=0, FreeBuyCount=0, LastResetTime={'$date': now},
                              SummonRoleID='', SummonRoleIDs=[])
        return record

    def snapshot(self):
        state, now = self.client.account_state or {}, self.guild.now_ms()
        inventory = (state.get('ItemContainer') or {}).get('Items')
        balances = {str(r.get('StaticID')): _nonnegative_int(r.get('Count'))
                    for r in inventory or [] if isinstance(r, dict)}
        currencies = {key: balances.get(key, 0) if isinstance(inventory, list) else None for key in ('29', '30')}
        rows = []
        for identifier, meta in store_catalog().items():
            error, bought, available = None, None, False
            try:
                record = self.record(identifier)
                bought = record['BuyCount']
                available = has_guild(state) and all(activity_open(
                    key, state, now=now / 1000, tables={'Activity': _catalog()['activities']}) for key in meta['activities'])
            except (GameError, ValueError, TypeError, OverflowError, OSError) as exc:
                error = str(exc)
            remaining = None if meta['limit'] is None or bought is None else max(0, meta['limit'] - bought)
            balance = currencies[meta['currency']]
            affordable = balance // meta['price'] if balance is not None else 0
            pending = self.runtime.get('shopUncertain', {})
            blocked = identifier in pending or any(
                isinstance(value, dict) and value.get('hold') and value.get('currency') == meta['currency']
                for value in pending.values())
            rows.append({'id': identifier, **meta, 'bought': bought, 'remaining': remaining,
                         'available': available, 'error': error, 'blocked': blocked,
                         'buyable': (min(remaining, affordable) if remaining is not None else affordable)
                         if available and not error and not blocked else 0})
        return {'currencies': currencies, 'goods': rows, 'running': self.runtime.get('shopRunning')}

    def next_delay(self, options):
        # No dedicated polling. When unaffordable, only increases of currencies
        # for unfinished selections wake the scheduler; completed goods sleep
        # until their next reset. All of this reads the local login cache.
        selected = store_options(options)
        self.runtime['shopWatchCurrencies'] = set()
        if not has_guild(self.client.account_state) or not selected:
            return None
        now, delays = self.guild.now_ms(), []
        for row in self.snapshot()['goods']:
            if row['id'] not in selected:
                continue
            if not row['error'] and not row['blocked'] and row['available'] and row['remaining']:
                self.runtime['shopWatchCurrencies'].add(row['currency'])
                if row['buyable']:
                    return 0.0
            if not row['blocked'] and not row['error']:
                delays.append((reset_at(now, row['reset']) - now) / 1000)
        return min(delays) if delays else None

    def buy(self, identifier, count, *, automatic=False):
        meta = store_catalog().get(identifier)
        if meta is None or bool(meta['reset']) != automatic:
            raise GameError('该商品不属于此商店')
        if not has_guild(self.client.account_state):
            raise GameError('当前尚未加入佣兵团')
        row = next(r for r in self.snapshot()['goods'] if r['id'] == identifier)
        if row['blocked']:
            raise GameError('上次购买结果未确认，请重新登录后再试')
        if row['error']:
            raise GameError(row['error'])
        if not row['available']:
            raise GameError('该商品尚未开放')
        if row['buyable'] <= 0:
            raise GameError('商品已售罄或货币不足')
        count = positive_int(count, '购买数量', row['buyable'])
        record = self.record(identifier)
        uncertain = self.runtime.setdefault('shopUncertain', {})
        uncertain[identifier] = {'currency': meta['currency'], 'hold': True}
        # Do not clear this guard on a guild refresh: only a new login includes
        # authoritative personal StoreRecordContainer records.
        result = self.guild._call('StoreHandler.BuyCommodity', _buy_payload(self.client, record, count))
        costs = result.get('CostItems')
        confirmed_balance = isinstance(costs, list) and any(
            isinstance(entry, dict) and isinstance(entry.get('NowItem'), dict)
            and str(entry['NowItem'].get('StaticID')) == meta['currency']
            and _nonnegative_int(entry['NowItem'].get('Count')) is not None for entry in costs)
        if not confirmed_balance:
            raise GameError('购买后的货币余额未确认，请重新登录后确认')
        uncertain[identifier]['hold'] = False
        returned = result.get('CommodityRecord')
        if (not isinstance(returned, dict) or returned.get('StaticID') != identifier
                or returned.get('Store') != 'MemberShop' or not object_id(returned.get('_id'))
                or _nonnegative_int(returned.get('BuyCount')) != record['BuyCount'] + count):
            raise GameError('购买结果未确认，请重新登录后确认')
        # Normal GameClient already merges this; also preserve the same contract
        # for transports which only return a parsed response.
        self.client._apply_commodity_record(result)
        uncertain.pop(identifier, None)
        return count
