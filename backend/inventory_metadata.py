"""Labels for official inventory currencies which intentionally share an icon."""
from functools import lru_cache
import json
import re

from . import resource_store


def build(rows, localization):
    def text(key, fallback=''):
        return re.sub(r'<[^>]*>', '', localization.get(key) or fallback).strip()
    labels={};categories={}
    for row in rows:
        # Activity currencies belong in the activity tab.  Treating them as
        # wealth makes every seasonal/event token disappear into 财物.
        if row.get('ItemType')=='ActivityCurrency':categories[row['ID']]='activity'
        elif row.get('ItemType') in ('AP','DayAP','WeekAP'):categories[row['ID']]='other'
        if row.get('Icon')!='Icon/Item[AP]':continue
        sid=row['ID'];name=text(row.get('Name'),sid);description=text(row.get('Description'))
        # Some AP currencies have no chapter description in the game table.
        # Keep their real ID visible rather than guessing a chapter number.
        chapter=re.fullmatch(r'(第.+章)贡献度',description)
        label=chapter.group(1) if chapter else f'贡献·{sid}' if name=='贡献度' else name
        labels[sid]={'label':label,'name':description if chapter else name}
    return {'schema':1,'items':labels,'categories':categories}


@lru_cache(maxsize=2)
def _load(path, modified):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema')!=1:return {}
        # An older active generation may predate the corrected game filters.
        if 'categories' not in data:
            bundled=json.loads(resource_store.bundled('assets/inventory_labels.json').read_text(encoding='utf-8'))
            data['categories']=bundled.get('categories',{})
        return data
    except (OSError,ValueError):return {}


def _data():
    path=resource_store.path('assets/inventory_labels.json')
    try:return _load(path,path.stat().st_mtime_ns)
    except OSError:return {}


def labels():return _data().get('items',{})


def category_overrides():return _data().get('categories',{})
