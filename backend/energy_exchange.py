"""Quantity purchases using the official commodity and VIP energy limits."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import time

from .game_client import GameError

NAMES = {'Normal6': '能源电池', 'Normal11': '钻石', 'HeartShop8': '心愿币'}
BEIJING = timezone(timedelta(hours=8))


def build_catalog(rows, version):
    """Compile fixed-price exchanges and the native VIP daily override."""
    commodities = {}
    for row in rows('Commodity'):
        sid = row.get('ID')
        if sid not in NAMES:
            continue
        if (row.get('Cost') or row.get('Drops') or not row.get('PriceItemID')
                or not row.get('ItemID') or int(row.get('Price') or 0) <= 0
                or int(row.get('ItemCount') or 0) <= 0):
            raise ValueError('Unsupported energy exchange commodity')
        commodities[sid] = {'store': row['Store'], 'currency': row['PriceItemID'],
                            'price': int(row['Price']), 'item': row['ItemID'],
                            'gain': int(row['ItemCount']), 'daily': row.get('ResetCountType') == 'Day'}
    limits = {row['LV']: int(row['DayBuyEnergyByDiamondCount']) for row in rows('VIP')}
    if set(commodities) != set(NAMES) or not limits or any(value < 0 for value in limits.values()):
        raise ValueError('Incomplete energy exchange rules')
    return {'schema': 1, 'source': 'sd_Commodity.txt + sd_VIP.txt', 'version': version,
            'commodities': commodities, 'diamondDailyLimits': limits}


@lru_cache(maxsize=1)
def catalog():
    from .resource_store import path
    data = json.loads(path('assets/energy_exchange.json').read_text(encoding='utf-8'))
    if data.get('schema') != 1 or set(data.get('commodities', {})) != set(NAMES):
        raise GameError('体力兑换规则缺失，请更新资源')
    return data


def _count(value, label):
    if type(value) is not int or value < 0:
        raise GameError(f'{label}无效，请刷新账号')
    return value


def _day(stamp):
    return datetime.fromtimestamp(stamp / 1000, BEIJING).replace(tzinfo=None) - timedelta(hours=8)


def snapshot(state, *, now_ms=None):
    from .game_data import timestamp
    records = {(r.get('Store'), r.get('StaticID')): r
               for r in (state.get('StoreRecordContainer') or {}).get('Records') or [] if isinstance(r, dict)}
    items = {str(r.get('StaticID')): _count(r.get('Count', 0), '货币数量')
             for r in (state.get('ItemContainer') or {}).get('Items') or [] if isinstance(r, dict)}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    rows = []
    rules = catalog()
    for sid, name in NAMES.items():
        rule = rules['commodities'][sid]
        record = records.get((rule['store'], sid)) or {}
        current = items.get(rule['currency'], 0)
        daily_limit = remaining = None
        reason = ''
        if rule['daily']:
            level = (state.get('VIPInfo') or {}).get('LV')
            daily_limit = rules['diamondDailyLimits'].get(str(level)) if type(level) is int else None
            if daily_limit is None:
                reason = '每日限购信息未同步，请刷新账号'
            else:
                bought = _count(record.get('BuyCount', 0), '今日兑换次数')
                # Projection only: keep the original record intact for the
                # server. Game days reset at 08:00 in Beijing, not midnight.
                raw_stamp = record.get('LastResetTime')
                stamp = timestamp(raw_stamp) if raw_stamp is not None else 0
                if stamp > 0 and _day(stamp * 1000).date() < _day(now_ms).date():
                    bought = 0
                remaining = max(0, daily_limit - bought)
        maximum = current // rule['price']
        if remaining is not None:
            maximum = min(maximum, remaining)
        if reason:
            maximum = 0
        elif remaining == 0:
            reason = '今日可购买次数已用完'
        elif maximum == 0:
            reason = f'{name}不足'
        rows.append({'id': sid, 'name': name, 'currencyName': name, **rule, 'current': current,
                     'resultName': '能源电池' if rule['item'] == '37' else '体力',
                     'dailyLimit': daily_limit, 'remaining': remaining, 'maxBuy': maximum, 'reason': reason})
    return rows


def buy(client, sid, count):
    from .tasks import _buy_payload, _synth_record
    if sid not in NAMES:
        raise GameError('该体力兑换档位不受支持')
    if type(count) is not int or not 1 <= count <= 2_147_483_647:
        raise GameError('兑换数量须为正整数')
    row = next(r for r in snapshot(client.account_state or {}) if r['id'] == sid)
    if count > row['maxBuy']:
        raise GameError(row['reason'] or f"当前最多可购买 {row['maxBuy']} 份，请重新选择数量")
    records = (client.account_state.get('StoreRecordContainer') or {}).get('Records') or []
    record = next((r for r in records if r.get('StaticID') == sid and r.get('Store') == row['store']), None)
    record = record or _synth_record(sid, row['store'])
    client._energy_exchange_uncertain = True
    response = client.call('StoreHandler.BuyCommodity', _buy_payload(client, record, count))
    if not isinstance(response, dict):
        raise GameError('兑换结果未确认，请刷新账号核对余额后再操作')
    costs = response.get('CostItems')
    costs = costs if isinstance(costs, list) else []
    drop = response.get('Drop')
    drops = drop.get('Items') if isinstance(drop, dict) else []
    drops = drops if isinstance(drops, list) else []
    balance = next((r.get('NowItem') for r in costs
                    if isinstance(r, dict) and isinstance(r.get('NowItem'), dict)
                    and str(r['NowItem'].get('StaticID')) == row['currency']), None)
    received = sum(_count(r['Item'].get('Count', 0), '兑换所得数量')
                   for r in drops if isinstance(r, dict) and isinstance(r.get('Item'), dict)
                   and str(r['Item'].get('StaticID')) == row['item'])
    result_balance = next((r['NowItem'] for r in drops
                           if isinstance(r, dict) and isinstance(r.get('NowItem'), dict)
                           and str(r['NowItem'].get('StaticID')) == row['item']), None)
    if (not balance or type(balance.get('Count')) is not int
            or row['current'] - balance['Count'] != row['price'] * count or received <= 0
            or not result_balance or type(result_balance.get('Count')) is not int):
        raise GameError('兑换结果未确认，请刷新账号核对余额后再操作')
    client._apply_now_items(response)
    client._apply_commodity_record(response)
    if row['daily']:
        updated = response.get('CommodityRecord')
        if (not isinstance(updated, dict) or updated.get('Store') != row['store']
                or updated.get('StaticID') != sid or type(updated.get('BuyCount')) is not int
                or updated['BuyCount'] != row['dailyLimit'] - row['remaining'] + count):
            raise GameError('兑换已扣款，但剩余次数未确认，请刷新账号核对后再操作')
        # Some responses omit the record's database ID. Keep the server's
        # confirmed counters even when the generic ID-based merge cannot match.
        container = client.account_state.setdefault('StoreRecordContainer', {})
        records = container.setdefault('Records', [])
        records[:] = [r for r in records if (r.get('Store'), r.get('StaticID')) != (row['store'], sid)]
        records.append(updated)
    client._energy_exchange_uncertain = False
    detail = f"体力管理：消耗 {row['price'] * count} {row['name']}，获得 {received} {row['resultName']}"
    return {'ok': True, 'detail': detail, 'energy': received, 'count': count}
