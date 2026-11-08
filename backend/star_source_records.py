"""Monthly star-source equipment purchases, from confirmed purchase drops only."""
from datetime import datetime
import hashlib
import json

from .game_client import GameError


def observe_purchase(client, request, response):
    from .recruitment import account_key, database, milliseconds
    from .shop_records import _count, _insert, purchase_time
    from .tasks import STAR_SOURCE_STORE_ID, STAR_SOURCE_COMMODITY_ID, _oid
    from equipment.plans import equip_slot

    record = request.get('Record') or {}
    if record.get('Store') != STAR_SOURCE_STORE_ID:
        return
    if record.get('StaticID') != STAR_SOURCE_COMMODITY_ID:
        raise GameError('星源商店购买档位不匹配')
    current = response.get('CommodityRecord') or {}
    if current and (current.get('Store') != STAR_SOURCE_STORE_ID
                    or current.get('StaticID') != STAR_SOURCE_COMMODITY_ID
                    or (_oid(record.get('_id')) and _oid(current.get('_id')) != _oid(record['_id']))):
        raise GameError('星源商店购买响应档位不匹配')
    # The three preview slots are not purchases. Only equipment actually
    # awarded by BuyCommodity can enter this ledger, including the first buy
    # whose synthesized request record has an empty _id.
    equipment = [row['Equipment'] for row in (response.get('Drop') or {}).get('Items') or []
                 if isinstance(row, dict) and isinstance(row.get('Equipment'), dict)
                 and row['Equipment'].get('StaticID')]
    if not equipment:
        raise GameError('星源商店购买响应缺少已购装备，未计入统计')
    stamp = purchase_time(response)
    confirmed_time = max(milliseconds(current.get('LastBuyTime')), milliseconds(response.get('Utc')))
    events = []
    for index, equip in enumerate(equipment):
        equip_id = _oid(equip.get('_id'))
        if equip_id:
            identity = ['equipment', str(equip_id)]
        else:
            # Older responses without equipment IDs must still have a stable
            # server timestamp; never deduplicate using this device's clock.
            if confirmed_time <= 0:
                raise GameError('星源商店购买响应缺少去重标识，未计入统计')
            identity = ['purchase', str(_oid(current.get('_id')) or _oid(record.get('_id')) or ''),
                        stamp, _count(current.get('BuyCount') or 0), request.get('ItemIndex'), index]
        identifier = 'star-buy-' + hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        events.append((identifier, {'equipment': [{'slot': equip_slot(equip) or '',
                        'set': str(equip.get('Set') or ''), 'staticID': str(equip['StaticID']), 'count': 1}]}))
    db = database()
    try:
        with db:
            for identifier, payload in events:
                _insert(db, account_key(client), identifier, stamp, 'star_purchase', payload)
    finally:
        db.close()


def summary(key):
    from .recruitment import database, TZ
    from .shop_records import equipment_icons
    from .tasks import SLOT_CN, SET_CN
    db = database()
    try:
        actions = db.execute("SELECT time,data FROM shop_actions WHERE account=? AND kind='star_purchase'",
                             (key,)).fetchall()
    finally:
        db.close()
    months = {}
    order = {slot: i for i, slot in enumerate(('weapon', 'helmet', 'armor', 'necklace', 'ring', 'boots'))}
    for stamp, raw in actions:
        month = datetime.fromtimestamp(stamp / 1000, TZ).strftime('%Y-%m')
        entry = months.setdefault(month, {'month': month, 'total': 0, 'equipment': {}})
        for equip in json.loads(raw)['equipment']:
            group = (equip['slot'], equip['set'], equip['staticID'] if not equip['slot'] or not equip['set'] else '')
            if group not in entry['equipment']:
                entry['equipment'][group] = {**equip, **equipment_icons(equip['slot'], equip['set']), 'count': 0,
                    'slotName': SLOT_CN.get(equip['slot'], '未知部位'),
                    'setName': SET_CN.get(equip['set'], equip['set'] or '未知套装')}
            entry['equipment'][group]['count'] += equip['count']
            entry['total'] += equip['count']
    ordered = []
    for month in sorted(months, reverse=True):
        entry = months[month]
        entry['equipment'] = sorted(entry['equipment'].values(),
                                   key=lambda e: (order.get(e['slot'], 9), e['set'], e['staticID']))
        ordered.append(entry)
    return {'months': ordered, 'firstRecordAt': min((a[0] for a in actions), default=None),
            'coverage': '按月汇总本工具确认购买成功的星源商店装备，显示部位、套装和件数；启用前及其他客户端的购买不计入。'}
