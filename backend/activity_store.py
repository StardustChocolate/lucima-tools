"""Current-event purchases; complete selected goals before exchanging spare points."""
from copy import deepcopy
import re
import time

from .game_client import GameError
from .game_data import activity_open
from .management_catalog import catalog


def normalize(value, store):
    value = value if isinstance(value, dict) else {}
    goods = catalog()['shops']
    selected = value.get('items') or []
    if not isinstance(selected, list):
        raise GameError('活动商品选择格式无效')
    selected = list(dict.fromkeys(str(sid) for sid in selected))
    if any(sid not in goods or goods[sid]['store'] != store or goods[sid]['item'] == '1' for sid in selected):
        raise GameError('所选商品不属于当前活动商店')
    quantities = {}
    for sid in selected:
        quantity = (value.get('quantities') or {}).get(sid, 1)
        if type(quantity) is not int or not 1 <= quantity <= 999999:
            raise GameError('商品目标份数须为 1～999999')
        quantities[sid] = quantity
    return {'items': selected, 'quantities': quantities, 'stopWhenBought': value.get('stopWhenBought') is True}


def _balance(state, sid):
    row = next((r for r in (state.get('ItemContainer') or {}).get('Items') or [] if str(r.get('StaticID')) == sid), None)
    count = row.get('Count', 0) if row else 0
    if type(count) is not int or count < 0:
        raise GameError('活动积分余额无效，请刷新账号')
    return count


def snapshot(client, options=None):
    from .tasks import _activity_names, _localized_text, item_name
    state, shops = client.account_state or {}, {}
    records = (state.get('StoreRecordContainer') or {}).get('Records')
    if not isinstance(records, list):
        raise GameError('活动商店购买记录缺失，请刷新账号')
    now = time.time()
    for sid, meta in catalog()['shops'].items():
        if meta['price'] <= 0 or not all(activity_open(a, state, now=now) for a in [meta['activity'], *meta['activities']]):
            continue
        store = meta['store']
        cfg = normalize((options or {}).get(store), store)
        shop = shops.setdefault(store, {'id': store, 'family': 'B' + store[len('Activity'):],
            'name': _activity_names().get('B' + store[len('Activity'):], store), 'config': cfg, 'goods': [],
            'currency': meta['currency'], 'currencyName': item_name(meta['currency']),
            'balance': _balance(state, meta['currency'])})
        rec = next((r for r in records if r.get('Store') == store and r.get('StaticID') == sid), {})
        bought = rec.get('BuyCount', 0)
        if type(bought) is not int or bought < 0:
            raise GameError('活动商品已购数量无效，请刷新账号')
        target = meta['limit'] if meta['limit'] is not None else cfg['quantities'].get(sid, 1)
        shop['goods'].append({'id': sid, **meta, 'name': _localized_text(meta['nameKey']) or item_name(meta['item']),
            'bought': bought, 'target': target, 'remaining': max(0, target - bought), 'selected': sid in cfg['items']})
    for shop in shops.values():
        selected = [r for r in shop['goods'] if r['selected']]
        # Every selection must still be visible in the current official catalog.
        complete = bool(selected) and len(selected) == len(shop['config']['items']) and all(r['remaining'] == 0 for r in selected)
        shop['complete'] = complete
        shop['stop'] = complete and shop['config']['stopWhenBought']
    return {'shops': list(shops.values())}


def store_for_scene(scene):
    match = re.match(r'^B(H\d+)_\d+_\d+$', str(scene or ''))
    if not match:
        raise GameError('活动关卡编号无效')
    return 'Activity' + match[1]


def process(client, options, store):
    from .tasks import _buy_payload, _synth_record
    from .equipment_sale import reward_summary
    lines, rewards = [], []
    if getattr(client, '_activity_store_uncertain', False):
        client.bootstrap()
        client._activity_store_uncertain = False
    def current():
        return next((r for r in snapshot(client, options)['shops'] if r['id'] == store), None)
    def buy(row, count):
        records = client.account_state['StoreRecordContainer']['Records']
        record = next((r for r in records if r.get('StaticID') == row['id'] and r.get('Store') == store), None)
        record = deepcopy(record) if record else _synth_record(row['id'], store)
        client._activity_store_uncertain = True
        result = client.call('StoreHandler.BuyCommodity', _buy_payload(client, record, count))
        returned = result.get('CommodityRecord') if isinstance(result, dict) else None
        balance = next((e.get('NowItem') for e in result.get('CostItems') or []
                        if isinstance(e, dict) and isinstance(e.get('NowItem'), dict)
                        and str(e['NowItem'].get('StaticID')) == row['currency']), None) if isinstance(result, dict) else None
        if (not returned or returned.get('StaticID') != row['id'] or returned.get('Store') != store
                or returned.get('BuyCount') != record['BuyCount'] + count
                or not balance or type(balance.get('Count')) is not int or balance['Count'] < 0):
            raise GameError('活动商店购买结果未确认，已停止购买及扫荡；请刷新账号核对')
        client._apply_commodity_record(result)
        client._apply_now_items(result)
        client._activity_store_uncertain = False
        lines.append(f"{row['name']} ×{count} 份")
        rewards.extend(reward_summary(result))
    shop = current()
    if not shop:
        return {'stop': False, 'complete': False, 'lines': [], 'rewardSummary': []}
    for row in shop['goods']:
        if not row['selected'] or row['remaining'] <= 0:
            continue
        count = min(row['remaining'], _balance(client.account_state, row['currency']) // row['price'])
        if count:
            buy(row, count)
    shop = current()
    if shop['stop']:
        # Native one-point option, explicitly excluding the 15-point gold pack.
        coin = next((r for r in shop['goods'] if r['item'] == '1' and r['price'] == 1 and r['limit'] is None), None)
        if coin:
            count = _balance(client.account_state, coin['currency'])
            if count:
                buy(coin, count)
        shop = current()
    return {**shop, 'lines': lines, 'rewardSummary': rewards}
