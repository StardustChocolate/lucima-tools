"""Active purchased/guild/account buffs, excluding innate VIP-level benefits.

StatusContainer.List is authoritative. NeedDisplay controls reward popups,
not whether a benefit is active; TriggerTime is not a benefit's start time.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import html
import math
import json
import re
import time

from .tasks import _localized_text
from . import resource_store


def build_catalog(rows):
    return {'schema': 1, 'statuses': {row['ID']: {
        'source': row.get('StatusSource', ''), 'nameKey': row.get('BuffName', ''),
        'tipKey': row.get('Tip', '')} for row in rows}}


@lru_cache(maxsize=2)
def _load(path, modified):
    data = json.loads(path.read_text(encoding='utf-8'))
    return data.get('statuses', {}) if data.get('schema') == 1 else {}


def definitions():
    path = resource_store.path('assets/account_buff_catalog.json')
    try:
        return _load(path, path.stat().st_mtime_ns)
    except (OSError, ValueError, TypeError):
        return {}


def _text(value):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>','',str(value or '')))).strip()


def _stamp(value):
    if isinstance(value,dict):value=value.get('$date')
    if isinstance(value,dict):value=value.get('$numberLong')
    if isinstance(value,bool) or value is None:return None
    try:
        result=float(value)
        return int(result) if math.isfinite(result) else None
    except (ValueError,TypeError,OverflowError):
        if isinstance(value,str):
            try:
                date=datetime.fromisoformat(value.replace('Z','+00:00'))
                return int(date.timestamp()*1000) if date.tzinfo is not None else None
            except (ValueError,OverflowError,OSError):pass
    return None


def _source(sid):
    if sid.startswith('VIP'):return '会员'
    if sid.startswith('Guild'):return '佣兵团'
    if sid in {'TIME06','TIME07','TIME08','TIME09','TIME23','TIME24'}:return '月卡'
    if sid.startswith('TIME'):return '限时'
    if 'Activity' in sid:return '活动'
    return '账号'


def snapshot(state,now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    container=(state or {}).get('StatusContainer')
    rows=container.get('List') if isinstance(container,dict) else None
    if not isinstance(rows,list):return {'available':False,'items':[]}
    result=[];seen=set();static=definitions()
    for row in rows:
        if not isinstance(row,dict):continue
        sid=_text(row.get('StaticID'))
        if not sid:continue
        definition = static.get(sid, {})
        sources = (definition.get('source'), row.get('StatusSource'), row.get('Source'))
        if sid.upper().startswith('VIP') or any(str(source).upper() == 'VIP' for source in sources):
            continue
        end=_stamp(row.get('EndTime'));start=_stamp(row.get('StartTime'))
        if end is not None and 0<end<=now_ms:continue
        if start is not None and start>now_ms:continue
        # DateTime.MinValue is also used for untimed account/activity states.
        # Preserve them, showing no invented expiry or duration.
        expires=end if end is not None and 0<end<253402214400000 else None
        tip=_localized_text(definition.get('tipKey') or 'T_ServerStatus_Tip_'+sid)
        name=(_localized_text(definition.get('nameKey') or 'T_ServerStatus_BuffName_'+sid)
              or _localized_text('T_Item_Name_'+sid) or tip
              or _text(row.get('Name')or row.get('DisplayName')) or sid)
        description=tip or _localized_text('T_Item_Description_'+sid)or''
        identifier=row.get('_id')
        if isinstance(identifier,dict):identifier=identifier.get('$oid')
        # Missing _id must not collapse distinct active account benefits.
        key=(sid,str(identifier or ''),expires,name,description)
        if key in seen:continue
        seen.add(key)
        result.append({'id':str(identifier or sid),'staticId':sid,'name':_text(name),
                       'description':_text(description),'source':_source(sid),'expiresAtMs':expires})
    return {'available':True,'items':result}
