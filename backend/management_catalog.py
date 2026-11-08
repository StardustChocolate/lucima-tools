"""Compile inventory and current-event shop rules from official tables."""
from functools import lru_cache
import json

from . import resource_store


def read_rows(path):
    # Some unused legacy titles in the official table have invalid UTF-8.
    # All consumed IDs, stars, prices and flags are ASCII and validated below.
    lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
    headers = lines[0].split('@')
    for line in lines[1:]:
        if line.strip():
            yield dict(zip(headers, line.split('@')))


def build(rows):
    def yes(value):
        return str(value).lower() in ('true', '1')
    # Native RoleStaticData/ArtifactStaticData constructors default CanSell
    # to True. An empty cell keeps that default; explicit FALSE forbids sale.
    def sellable(row):
        value = row.get('CanSell', '')
        if str(value).lower() not in ('', 'true', 'false', '0', '1'):
            raise ValueError('Invalid native CanSell flag')
        return value == '' or yes(value)
    roles = {r['ID']: {'star': int(r['InitStar']), 'sell': sellable(r)}
             for r in rows('Role') if yes(r.get('IsHero'))}
    bonds = {r['ID']: {'star': int(r['Star']), 'sell': sellable(r)}
             for r in rows('Artifact')}
    shops = {}
    for r in rows('Commodity'):
        if not r.get('Store', '').startswith('ActivityH'):
            continue
        # Only native fixed-price goods; no inferred drop/price expressions.
        if not r.get('Price', '').isdigit() or not r.get('ItemCount', '').isdigit() or not r.get('ItemID'):
            continue
        shops[r['ID']] = {'store': r['Store'], 'activity': r.get('ActivityID', ''),
            'activities': [s for s in r.get('ActivityIDs', '').split('~') if s],
            'currency': r['PriceItemID'], 'price': int(r['Price']),
            'item': r['ItemID'], 'count': int(r['ItemCount']),
            'limit': max(0, int(r['LimitCount'])) if int(r.get('LimitCount') or -1) >= 0 else None,
            'nameKey': r.get('Name', '')}
    if '\ufffd' in json.dumps([roles, bonds, shops]):
        raise ValueError('Invalid consumed management metadata')
    return {'schema': 1, 'roles': roles, 'bonds': bonds, 'shops': shops}


@lru_cache(maxsize=2)
def _load(path, modified):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1:
        raise ValueError('Invalid inventory management catalog')
    return data


def catalog():
    path = resource_store.path('assets/management_catalog.json')
    return _load(path, path.stat().st_mtime_ns)
