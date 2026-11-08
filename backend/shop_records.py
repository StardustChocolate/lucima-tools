"""Secret-shop purchases and spending from confirmed native responses.

The legacy shop table retains appearances. Actions are independently deduplicated
by shelf generation and purchase count, and can include purchases on login shelves.
"""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
import time

from .game_client import GameError


def _count(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or value<0 or int(value)!=value:
        raise GameError('商店记录数量无效')
    return int(value)


def _cost(client,response,*,paid):
    """Item is the spent amount; NowItem is the absolute balance, never a cost."""
    costs={'1':0,'2':0};known=True
    entries=response.get('CostItems')
    if not isinstance(entries,list):return costs,not paid
    balances={str(r.get('StaticID')):r.get('Count') for r in
              ((client.account_state or {}).get('ItemContainer') or {}).get('Items') or []}
    for row in entries:
        item=row.get('Item') or {};now=row.get('NowItem') or {}
        sid=str(item.get('StaticID') or now.get('StaticID') or '')
        if sid not in costs:continue
        if 'Count' in item:
            costs[sid]+=_count(item['Count'])
        elif now.get('Count') is not None and balances.get(sid) is not None:
            delta=_count(balances[sid])-_count(now['Count'])
            if delta>0:costs[sid]+=delta
            else:known=False
        else:known=False
    return costs,known


def _insert(db,key,identifier,stamp,kind,payload):
    db.execute('INSERT OR IGNORE INTO shop_actions VALUES (?,?,?,?,?)',
               (key,identifier,stamp,kind,json.dumps(payload,ensure_ascii=False,separators=(',',':'))))


def record_refresh_cost(db,client,identifier,stamp,request,response):
    from .recruitment import account_key
    cost,known=_cost(client,response,paid=bool(request.get('IsUseGold')))
    _insert(db,account_key(client),identifier,stamp,'refresh',{'cost':cost,'costKnown':known})


def purchase_time(response):
    """CommodityRecord dates are BSON ms; native Utc is Unix seconds (a string)."""
    from .recruitment import milliseconds
    last_buy=milliseconds((response.get('CommodityRecord') or {}).get('LastBuyTime'))
    if last_buy>=946684800000:return last_buy
    value=response.get('Utc')
    if isinstance(value,(str,int,float)) and not isinstance(value,bool):
        try:
            number=float(value)
            if math.isfinite(number) and 946684800<=number<4102444800:
                return int(round(number*1000))
        except (ValueError,OverflowError):pass
    stamp=milliseconds(value)
    return stamp if stamp>=946684800000 else int(time.time()*1000)


def observe_purchase(client,request,response):
    from .recruitment import account_key,database,milliseconds
    from .tasks import _oid
    record=request.get('Record') or {}
    if record.get('Store')=='RainbowStarSource':
        from .star_source_records import observe_purchase as observe_star_purchase
        return observe_star_purchase(client,request,response)
    if record.get('Store')!='SecretShop':return
    identifier=_oid(record.get('_id'))
    if not identifier:raise GameError('商店购买记录缺少货架标识')
    current=response.get('CommodityRecord') or {}
    if current and (current.get('Store')!='SecretShop' or _oid(current.get('_id'))!=identifier):
        raise GameError('商店购买响应货架不匹配')
    before=_count(record.get('BuyCount') or 0)
    bought=_count(current['BuyCount']) if 'BuyCount' in current else before+_count(request.get('Count',1))
    drops=(response.get('Drop') or {}).get('Items')
    if not isinstance(drops,list) or not drops:
        # A returned increased BuyCount also proves a purchase. Shelf goods
        # describe one unit; the returned increment is the actual bought count.
        if bought<=before or 'BuyCount' not in current:
            raise GameError('商店购买响应缺少已购道具，未计入统计')
        drops=(record.get('DropResult') or {}).get('Items') or []
        multiplier=bought-before
    else:multiplier=1
    items=defaultdict(int);equipment=[]
    from equipment.plans import equip_slot
    for row in drops:
        item=row.get('Item') or {}
        if item.get('StaticID'):
            items[str(item['StaticID'])]+=_count(item.get('Count',row.get('Count',0)))*multiplier
        equip=row.get('Equipment')
        if isinstance(equip,dict) and equip.get('StaticID'):
            equipment.append({'slot':equip_slot(equip) or '', 'set':str(equip.get('Set') or ''),
                              'staticID':str(equip['StaticID']),'count':multiplier})
    if not items and not equipment:raise GameError('商店购买响应没有可统计的产出')
    stamp=purchase_time(response)
    # The first login shelf may use DateTime.MinValue. Generation zero is stable
    # until its first reset; subsequent positive reset times distinguish rounds.
    generation=max(0,milliseconds(record.get('LastResetTime')) or milliseconds(current.get('LastResetTime')))
    identity=json.dumps([str(identifier),generation,bought],separators=(',',':'))
    event_id='buy-'+hashlib.sha256(identity.encode()).hexdigest()
    cost,known=_cost(client,response,paid=True)
    db=database()
    try:
        with db:_insert(db,account_key(client),event_id,stamp,'purchase',
                        {'items':items,'equipment':equipment,'cost':cost,'costKnown':known})
    finally:db.close()


def chip_types():
    from .tasks import BUY_TARGETS
    slots=('weapon','helmet','armor','necklace','ring','boots')
    return [{'id':slot,'prefix':part['id'][:-1],'name':part['label']+'芯片','icon':f'/assets/items/{part["id"]}.png'}
            for slot,part in zip(slots,next(t['parts'] for t in BUY_TARGETS if t['key']=='chip'))]


def equipment_icons(slot, set_id):
    from .tasks import SLOT_CN, SET_ICON
    return {'slotIcon':f'/assets/slots/{slot}.png' if slot in SLOT_CN else None,
            'setIcon':f'/assets/sets/{SET_ICON[set_id]}.png' if set_id in SET_ICON else None}


def _totals():
    return {'refreshes':0,'paid':0,'free':0,'recruit':0,'mystery':0,'recruitHits':0,'mysteryHits':0,
            'boughtRecruit':0,'boughtMystery':0,'chips':{},'equipment':{},'gold':0,'diamonds':0,
            'legacyRefreshes':0,'unknownCosts':0,'purchases':0,'gains':{},'purchaseDiamonds':0}


def summary(key,data):
    from .recruitment import database,TZ
    from .tasks import SLOT_CN,SET_CN,item_name
    db=database()
    try:
        rows=db.execute('SELECT id,time,paid,recruit,mystery,recruit_hits,mystery_hits FROM shop WHERE account=?', (key,)).fetchall()
        actions=db.execute("SELECT id,time,kind,data FROM shop_actions WHERE account=? AND kind IN ('refresh','purchase')", (key,)).fetchall()
    finally:db.close()
    months={};chips=chip_types();refresh_ids={r[0] for r in actions if r[2]=='refresh'}
    def buckets(stamp):
        day=datetime.fromtimestamp(stamp/1000,TZ).strftime('%Y-%m-%d')
        month=months.setdefault(day[:7],{'month':day[:7],**_totals(),'days':{}})
        daily=month['days'].setdefault(day,{'day':day,**_totals()})
        return month,daily
    for identifier,stamp,paid,recruit,mystery,rh,mh in rows:
        for entry in buckets(stamp):
            entry['refreshes']+=1;entry['paid' if paid else 'free']+=1
            entry['recruit']+=recruit;entry['mystery']+=mystery
            entry['recruitHits']+=int(rh>0);entry['mysteryHits']+=int(mh>0)
            entry['legacyRefreshes']+=int(identifier not in refresh_ids)
    for identifier,stamp,kind,raw in actions:
        payload=json.loads(raw)
        for entry in buckets(stamp):
            entry['gold']+=payload['cost'].get('1',0);entry['diamonds']+=payload['cost'].get('2',0)
            entry['unknownCosts']+=int(not payload.get('costKnown'))
            if kind!='purchase':continue
            entry['purchases']+=1
            entry['purchaseDiamonds']+=payload['cost'].get('2',0)
            entry['boughtRecruit']+=payload['items'].get('5',0);entry['boughtMystery']+=payload['items'].get('6',0)
            for sid,count in payload['items'].items():
                entry['gains'][sid]=entry['gains'].get(sid,0)+count
                chip=next((c for c in chips if sid.startswith(c['prefix']) and sid[len(c['prefix']):].isdigit()),None)
                if chip:entry['chips'][chip['id']]=entry['chips'].get(chip['id'],0)+count
            for equip in payload['equipment']:
                # Unknown part/set stays identifiable instead of being guessed.
                k=(equip['slot'],equip['set'],equip['staticID'] if not equip['slot'] or not equip['set'] else '')
                if k not in entry['equipment']:
                    entry['equipment'][k]={**equip,**equipment_icons(equip['slot'],equip['set']),'count':0,'slotName':SLOT_CN.get(equip['slot'],'未知部位'),
                                            'setName':SET_CN.get(equip['set'],equip['set'] or '未知套装')}
                entry['equipment'][k]['count']+=equip['count']
    def finish(entry):
        entry['gains']=[{'id':sid,'name':item_name(sid),'count':count} for sid,count in sorted(entry['gains'].items()) if count>0]
        entry['chips']=[{'id':c['id'],'count':entry['chips'].get(c['id'],0)} for c in chips]
        order={c['id']:i for i,c in enumerate(chips)}
        entry['equipment']=sorted(entry['equipment'].values(),key=lambda e:(order.get(e['slot'],9),e['set'],e['staticID']))
    ordered=[]
    for month_id in sorted(months,reverse=True):
        month=months[month_id];finish(month)
        month['days']=[month['days'][d] for d in sorted(month['days'],reverse=True)]
        for day in month['days']:finish(day)
        ordered.append(month)
    stamps=[r[1] for r in rows]+[r[1] for r in actions]
    return {'months':ordered,'firstRecordAt':min(stamps,default=None),'baselines':data['shop'],'chipTypes':chips,
            'coverage':'仅统计本工具确认成功的秘密商店刷新与购买；芯片、装备和花费以游戏回包为准，其他客户端的操作不计入。'}
