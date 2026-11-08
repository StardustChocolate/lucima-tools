"""Durable equipment-match alerts; delivery does not depend on a visible WebView."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from . import config
from .logutil import log

_LOCK = threading.RLock()
SOURCES = {'shop_free':'秘密商店（自动）','shop_diamond':'秘密商店（主动）','star_source_shop':'星源商店'}


def _path(): return config.data_dir()/'equipment-notifications.json'


def _read():
    try:
        data=json.loads(_path().read_text(encoding='utf-8'))
        return data if isinstance(data,list) else []
    except (OSError,ValueError):return []


def _save(rows):
    path=_path();path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.equipment-alert-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(rows[-200:],out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _native_notify(event):
    if config.PLATFORM!='android':return False
    try:
        from java import jclass
        payload={key:event[key] for key in ('id','title','text','image')}
        return bool(jclass('com.openrubi.lucimatools.EquipmentNotifications').show(json.dumps(payload,ensure_ascii=False)))
    except Exception as error:
        log.warning('[equipment-alert] delivery unavailable: %s',type(error).__name__)
        return False


def card_text(card):
    title='·'.join(str(card.get(key)or'') for key in ('setCN','slotCN') if card.get(key)) or str(card.get('name')or'装备')
    props=[]
    for prop in card.get('subProps') or []:
        if isinstance(prop,dict):props.append(str(prop.get('label')or'')+' '+str(prop.get('text')or''))
    return f'{title}｜副属性评分 {card.get("auxScore", "—")}\n'+'、'.join(props)


def publish(account,source,cards,scheme,*,context=None,purchased=False):
    """One alert per matching shelf; failures must never interrupt game tasks."""
    if source not in SOURCES or not cards or not isinstance(scheme,dict):return None
    try:
        identity=json.dumps([account,source,context,[c.get('equipmentKey') or c for c in cards]],ensure_ascii=False,sort_keys=True,default=str)
        event_id=hashlib.sha256(identity.encode()).hexdigest()[:32]
        event={'id':event_id,'account':account,'source':source,'cards':deepcopy(cards),'scheme':deepcopy(scheme),
               'createdAt':int(time.time()*1000),'delivered':False,'purchased':bool(purchased),
               'title':f'{SOURCES[source]}：命中{len(cards)}件装备'+('（已自动购买）'if purchased else''),
               'text':'\n\n'.join(card_text(card)for card in cards),
               'image':cards[0].get('img') or cards[0].get('setIcon') or ''}
        with _LOCK:
            rows=_read()
            existing=next((row for row in rows if row.get('id')==event_id),None)
            if existing and existing.get('delivered'):return event_id
            if existing:event=existing
            else:rows.append(event);_save(rows)
        if _native_notify(event):
            with _LOCK:
                rows=_read()
                for row in rows:
                    if row.get('id')==event_id:row['delivered']=True
                _save(rows)
        return event_id
    except Exception as error:
        log.warning('[equipment-alert] could not record match: %s',type(error).__name__)
        return None


def get(event_id):
    if not isinstance(event_id,str) or not re.fullmatch(r'[a-f0-9]{32}',event_id):return None
    with _LOCK:
        return deepcopy(next((row for row in _read()if row.get('id')==event_id),None))


def publish_result(account, source, result, scheme):
    if not result.get('ok'):return None
    cards = result.get('notificationMatches') or result.get('matches') or result.get('legendaryMatches') or []
    if source == 'star_source_shop':
        context = [result.get('refreshCount'), result.get('revertTime')]
    else:
        context = [[record.get('_id'), record.get('LastResetTime')] for record in result.get('records') or []]
    return publish(account,source,cards,scheme,context=context,purchased=result.get('equipmentAutoBought',False))


def retry_pending():
    if config.PLATFORM!='android':return
    with _LOCK:pending=[row for row in _read() if not row.get('delivered') and time.time()*1000-row.get('createdAt',0)<86400000][-10:]
    for row in pending:
        if _native_notify(row):
            with _LOCK:
                rows=_read()
                for item in rows:
                    if item.get('id')==row['id']:item['delivered']=True
                _save(rows)


def forget_account(account):
    with _LOCK:
        rows=_read();removed=[row['id'] for row in rows if row.get('account')==account]
        if removed:_save([row for row in rows if row.get('account')!=account])
    if config.PLATFORM=='android':
        try:
            from java import jclass
            native=jclass('com.openrubi.lucimatools.EquipmentNotifications')
            for ident in removed:native.cancel(ident)
        except Exception:pass
