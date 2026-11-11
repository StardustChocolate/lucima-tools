"""Limited event-shop goods, inventory-driven purchases and confirmed settlement."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time

from . import config, game_clock, runtime_data as resources
from .game_client import GameError, GameMessage, TaskDeferred
from .game_data import enabled, timestamp

TASK_ID = 'activity_store'
DAY_MS = 86_400_000


def build_catalog(stores, commodities, activities, names, loc, catalog_version=''):
    """Keep official shelf order and limits; unlimited exchanges are excluded."""
    definitions = {row['ID']: row for row in activities}
    result = {}

    def text(value):
        return re.sub(r'<[^>]*>', '', str(value or '')).strip()

    for store in stores:
        if store.get('Type') != 'SideStory':
            continue
        aid = store.get('ActivityID')
        activity = definitions.get(aid)
        if not activity:
            raise ValueError(f'活动商店 {store["ID"]} 缺少活动配置')
        goods = []
        for row in commodities:
            if row.get('Store') != store['ID']:
                continue
            limit = int(row.get('LimitCount') or -1)
            if limit <= 0:
                continue
            item, currency = row.get('ItemID'), row.get('PriceItemID')
            price, amount = int(row.get('Price') or 0), int(row.get('ItemCount') or 1)
            if not item or not currency or price <= 0 or amount <= 0:
                raise ValueError(f'活动商品 {row["ID"]} 的道具或价格无效')
            # Preserve unfamiliar goods on the shelf, but never spend with a
            # partial interpretation of new pricing or purchase conditions.
            unsupported = any(row.get(key) for key in (
                'PriceItemID2', 'Price2', 'Cost', 'Condition', 'AccountLv', 'VIPLV',
                'LifeTime', 'CDTime', 'IsRandomOnce', 'Discount', 'CostType',
                'ResetCountType', 'OutList'))
            guards = list(dict.fromkeys(filter(None, [
                row.get('ActivityID'), *str(row.get('ActivityIDs') or '').split('~'),
                row.get('VersionControl')])))
            goods.append({'id': row['ID'], 'item': item,
                          'name': text(loc.get(row.get('Name')) or names.get(item) or item),
                          'amount': amount, 'currency': currency, 'price': price,
                          'limit': limit, 'activities': guards,
                          'unsupported': unsupported})
        if goods:
            result[store['ID']] = {'id': store['ID'], 'activity': aid,
                                  'name': text(loc.get('T_Chapter_Name_' + aid)
                                               or loc.get(activity.get('Name')) or activity.get('Name') or aid),
                                  'remainDays': int(activity.get('RemainDay') or 0),
                                  'goods': goods}
    used = {shop['activity'] for shop in result.values()}
    used.update(aid for shop in result.values() for row in shop['goods'] for aid in row['activities'])
    return {'schema': 1, 'catalog': catalog_version, 'stores': result,
            'activities': {aid: {key: definitions[aid].get(key, '')
                                 for key in ('IsUnlimitTime', 'RemainDay')}
                           for aid in used if aid in definitions}}


@resources.cached
def catalog():
    data = resources.read_json('assets/game_config.json', {}).get('activityStores')
    if data is None:
        # Existing caches can contain the same official release compiled by an
        # older app. Use bundled metadata only when its official catalog matches.
        root = Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[1])
        bundled = json.loads((root / 'assets/game_config.json').read_text(encoding='utf-8')).get('activityStores', {})
        current = resources.snapshot().manifest.get('catalog', '')
        if not current or current == bundled.get('catalog'):
            data = bundled
    data = data or {}
    return data if data.get('schema') == 1 else {'stores': {}, 'activities': {}}


def options(value):
    picked = value.get('items') if isinstance(value, dict) else None
    return list(dict.fromkeys(item for item in picked if isinstance(item, str))) if isinstance(picked, list) else []


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _window(aid, settings, definitions, now_ms):
    definition, setting = definitions.get(aid, {}), settings.get(aid)
    if enabled(definition.get('IsUnlimitTime')):
        return True, None, None
    if not setting:
        return False, None, None
    if enabled(setting.get('IsIgnoreTime')):
        return True, None, None
    start, end = (int(timestamp(setting.get(key)) * 1000) for key in ('StartTime', 'EndTime'))
    off_days = int(setting.get('OffDays') or 0)
    if enabled(setting.get('IsRepeat')) or off_days > 0:
        hours = int((end - start) / 3_600_000)
        period = (hours + off_days * 24) * 3_600_000
        if period <= 0:
            raise GameError('活动商店排期无效，请重新登录')
        start += int((now_ms - start) / period) * period
        end = start + hours * 3_600_000
    close = end + int(definition.get('RemainDay') or 0) * DAY_MS
    return start <= now_ms < close, start, close


def _record(state, sid, store):
    from .tasks import _synth_record
    records = (state.get('StoreRecordContainer') or {}).get('Records')
    if not isinstance(records, list):
        raise GameError('购买记录缺失，等待登录同步刷新')
    found = [row for row in records if isinstance(row, dict)
             and row.get('StaticID') == sid and row.get('Store') == store]
    if len(found) > 1 or found and _count(found[0].get('BuyCount')) is None:
        raise GameError('购买记录异常，等待登录同步刷新')
    return deepcopy(found[0]) if found else _synth_record(sid, store)


def status(state, params=None, runtime=None, *, now_ms=None):
    state = state or {}
    runtime = runtime if runtime is not None else {}
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    data, selected = catalog(), set(options(params))
    periods = (params.get('periods') or {}) if isinstance(params, dict) else {}
    settings = {row.get('ActivityID'): row for row in
                (state.get('ActivityDBSettingContainer') or {}).get('SettingList') or [] if isinstance(row, dict)}
    inventory = (state.get('ItemContainer') or {}).get('Items')
    balances = {row.get('StaticID'): _count(row.get('Count')) for row in inventory or [] if isinstance(row, dict)}
    pending, insufficient = runtime.get('waitingForLogin') or {}, runtime.get('waitingForBalance') or {}
    held = {value.get('currency') for value in pending.values() if value.get('hold')}
    stores, deadlines = [], []
    for shop in data['stores'].values():
        live, start, close = _window(shop['activity'], settings, data['activities'], now_ms)
        if not live:
            if start is not None and start > now_ms:
                deadlines.append(start)
            continue
        if close is not None:
            deadlines.append(close)
        goods = []
        for meta in shop['goods']:
            if meta['limit'] <= 0:
                continue
            available = all(_window(aid, settings, data['activities'], now_ms)[0]
                            for aid in meta['activities'])
            error, bought = None, None
            try:
                bought = _record(state, meta['id'], shop['id'])['BuyCount']
            except GameError as exc:
                error = str(exc)
            balance = balances.get(meta['currency'], 0) if isinstance(inventory, list) else None
            remaining = max(0, meta['limit'] - bought) if bought is not None else None
            waiting = meta['id'] in pending or meta['currency'] in held
            short = meta['id'] in insufficient and (balance is None or balance <= insufficient[meta['id']])
            buyable = min(remaining, balance // meta['price']) if remaining is not None and balance is not None else 0
            if waiting or short or error or not available or meta['unsupported']:
                buyable = 0
            picked = meta['id'] in selected and (shop['id'] not in periods or periods[shop['id']] == start)
            goods.append({**meta, 'selected': picked, 'bought': bought,
                          'remaining': remaining, 'balance': balance, 'buyable': buyable,
                          'waiting': waiting, 'error': error, 'available': available,
                          'insufficient': short})
        currencies = list(dict.fromkeys(row['currency'] for row in goods))
        stores.append({key: value for key, value in shop.items() if key != 'goods'} | {
            'goods': goods, 'startMs': start, 'endMs': close,
            'wallet': {key: balances.get(key, 0) if isinstance(inventory, list) else None for key in currencies}})
    stores.sort(key=lambda shop: (shop['endMs'] is None, -(shop['endMs'] or 0), shop['id']))
    targets = [row for shop in stores for row in shop['goods'] if row['selected']]
    unfinished = [row for row in targets if row['remaining'] is None or row['remaining'] > 0]
    ready = any(row['buyable'] for row in targets)
    watched = {row['currency'] for row in unfinished if row['available'] and not row['waiting']
               and not row['error'] and not row['unsupported']}
    if not data['stores']:
        note = '活动商店配置待更新'
    elif not stores:
        note = '暂无开放的活动商店'
    elif not targets:
        note = '请选择购买目标'
    elif not unfinished:
        note = '所选商品已全部买完'
    elif ready:
        note = '有商品可购买'
    elif any(row['waiting'] or row['error'] or row['balance'] is None for row in unfinished):
        note = '等待登录同步刷新'
    elif any(row['unsupported'] for row in unfinished):
        note = '商品购买规则暂不支持'
    elif not any(row['available'] for row in unfinished):
        note = '所选商品暂未开放'
    else:
        note = '活动道具不足'
    return {'stores': stores, 'ready': ready, 'note': note,
            'nextMs': min(deadlines) if deadlines else None, 'watchCurrencies': sorted(watched),
            'serverNowMs': now_ms, 'localNowMs': int(time.time() * 1000),
            'dataVersion': resources.snapshot().version}


def task(client, params=None, runtime=None, on_partial=None):
    from .tasks import _buy_payload, _collect_items, _fmt_items
    runtime = runtime if runtime is not None else {}
    lines, errors, sent = [], [], False

    def snapshot():
        is_enabled = getattr(client, 'activity_store_enabled', None)
        if callable(is_enabled) and not is_enabled():
            raise TaskDeferred('商店购买已停止')
        current = getattr(client, 'activity_store_params', None)
        return status(client.account_state, current() if callable(current) else params,
                      runtime, now_ms=game_clock.now_ms(client))

    while True:
        current = snapshot()
        target = next(((shop, row) for shop in current['stores'] for row in shop['goods']
                       if row['selected'] and row['buyable']), None)
        if target is None:
            break
        if sent:
            delay = config.get_request_delay_config()
            seconds = max(0, delay['ms'] / 1000) if delay['enabled'] else 0
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                snapshot()
                time.sleep(max(0, min(.1, until - time.monotonic())))
            current = snapshot()
            target = next(((shop, row) for shop in current['stores'] for row in shop['goods']
                           if row['selected'] and row['buyable']), None)
            if target is None:
                break
        shop, row = target
        sid, currency, count = row['id'], row['currency'], row['buyable']
        saved = _record(client.account_state, sid, shop['id'])
        pending = runtime.setdefault('waitingForLogin', {})
        pending[sid] = {'currency': currency, 'hold': True}
        try:
            try:
                response = client.call('StoreHandler.BuyCommodity', _buy_payload(client, saved, count))
            except (GameMessage, TaskDeferred):
                pending.pop(sid, None)
                raise
            costs = response.get('CostItems') or []
            if not any(isinstance(entry, dict) and isinstance(entry.get('NowItem'), dict)
                       and entry['NowItem'].get('StaticID') == currency
                       and _count(entry['NowItem'].get('Count')) is not None for entry in costs):
                raise GameError('购买后的活动道具余额未确认')
            client._apply_now_items(response)
            pending[sid]['hold'] = False
            record = response.get('CommodityRecord') or {}
            if (record.get('StaticID') != sid or record.get('Store') != shop['id']
                    or not record.get('_id')
                    or _count(record.get('BuyCount')) != saved['BuyCount'] + count):
                raise GameError('商品购买结果未确认')
            client._apply_commodity_record(response)
            if _record(client.account_state, sid, shop['id'])['BuyCount'] != record['BuyCount']:
                raise GameError('商品购买记录未同步')
            pending.pop(sid, None)
            runtime.setdefault('waitingForBalance', {}).pop(sid, None)
            gains = {}
            _collect_items((response.get('Drop') or {}).get('Items') or [], gains)
            line = f'{shop["name"]}：已购买{row["name"]} ×{count * row["amount"]}'
            if gains:
                line += f'，获得：{_fmt_items(gains)}'
            lines.append(line)
            if on_partial:
                on_partial({'ok': True, 'detail': line})
        except TaskDeferred:
            raise
        except Exception as exc:
            if isinstance(exc, GameMessage) and 'item not enough' in str(exc).lower():
                runtime.setdefault('waitingForBalance', {})[sid] = row['balance']
                error = f'{row["name"]}：活动道具不足（{exc}）'
            else:
                pending.setdefault(sid, {'currency': currency, 'hold': False})
                error = f'{row["name"]}：{exc}，等待登录同步刷新'
            errors.append(error)
            if on_partial:
                on_partial({'ok': False, 'detail': error})
        sent = True
    return {'ok': not errors, 'skipped': not lines and not errors, 'retryManaged': True,
            'detail': '；'.join(lines + errors) if not on_partial and (lines or errors) else current['note'],
            'liveLogged': bool(on_partial and (lines or errors))}
