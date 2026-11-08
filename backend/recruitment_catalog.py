"""Compile recruitment rules from official Commodity/Drop/Role/Artifact tables."""
from __future__ import annotations

from collections import defaultdict
import re

FIELDS = {
    'Item': ('ID', 'Name'),
    'Role': ('ID', 'IsHero', 'NAME', 'InitStar', 'Element'),
    'Artifact': ('ID', 'Star'),
    'Drop': ('DropID', 'Group', 'GroupWeight', 'TagString', 'ActivityIDs'),
    'Commodity': ('ID', 'Store', 'Name', 'Drops', 'PriceItemID', 'ActivityID', 'ActivityIDs',
                  'GuaranteedCount', 'GuaranteedRoleIDs', 'ItemID', 'ItemCount', 'Rate', 'LV ', 'LV'),
}


def table_rows(path):
    """Reject damaged consumed fields; tolerate broken unused client prose."""
    lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
    headers = lines[0].split('@')
    consumed = FIELDS[path.stem.removeprefix('sd_')]
    for index, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        fields = line.split('@')
        if len(fields) == len(headers) + 1 and not fields[-1]:
            fields.pop()
        if len(fields) != len(headers):
            raise ValueError(f'招募资料列数无效：{path.name}:{index}')
        raw = dict(zip(headers, fields))
        row = {key: raw[key] for key in consumed if key in raw}
        if any('\ufffd' in value for value in row.values()):
            raise ValueError(f'招募资料字段编码无效：{path.name}:{index}')
        yield row


def build_catalog(rows, localization, version):
    def name(key, fallback):
        return re.sub(r'<[^>]*>', '', localization.get(key) or fallback)

    items = {r['ID']: r for r in rows('Item')}
    results = {}
    for r in rows('Role'):
        if r.get('IsHero', '').lower() not in ('1', 'true'):
            continue
        sid = r['ID']
        results[sid] = {'name': name(r.get('NAME'), sid), 'star': int(r.get('InitStar') or 0),
                        'kind': 'role', 'element': r.get('Element') or '',
                        'icon': f'/assets/avatars/{sid}.png'}
    for r in rows('Artifact'):
        sid = r['ID']
        results[sid] = {'name': name(items.get(sid, {}).get('Name'), sid),
                        'star': int(r.get('Star') or 0), 'kind': 'artifact', 'element': '',
                        'icon': f'/assets/items/{sid}.png' if sid in items else None}
    drops = defaultdict(list)
    for r in rows('Drop'):
        drops[r['DropID']].append(r)

    def rates(drop_id):
        definitions = drops[drop_id]
        # Only plain single-result weighted pools have an unambiguous baseline.
        groups = {r.get('Group') for r in definitions}
        if len(groups) != 1 or not next(iter(groups), None) or any(r.get('ActivityIDs') for r in definitions):
            return None
        weights = [float(r.get('GroupWeight') or 0) for r in definitions]
        total = sum(weights)
        if total <= 0:
            return None
        five = light_dark = 0.0
        for r, weight in zip(definitions, weights):
            tag = r.get('TagString') or ''
            result = results.get(tag, {})
            if not result and not re.fullmatch(r'(?:ML|All|Limit)?(?:Star|Art)[345]', tag):
                return None
            if tag in ('Star5', 'MLStar5', 'Art5', 'AllStar5', 'AllArt5', 'LimitStar5', 'LimitArt5') or result.get('star') == 5:
                five += weight
            if tag == 'MLStar5' or (result.get('star') == 5 and result.get('element') in ('Light', 'Dark')):
                light_dark += weight
        return {'five': five / total, 'lightDark': light_dark / total}

    pools, shop = {}, []
    for r in rows('Commodity'):
        sid = r['ID']
        if r.get('Store') == 'SecretShop' and r.get('ItemID') in ('5', '6'):
            shop.append({'id': sid, 'item': r['ItemID'], 'amount': int(r.get('ItemCount') or 1),
                         'slotRate': float(r.get('Rate') or 0), 'level': int(r.get('LV ') or r.get('LV') or 0)})
        if r.get('Store') != 'Summon' or not r.get('Drops') or sid.startswith(('Test', 'NormalSummon_')):
            continue
        kind = 'mystic' if r.get('PriceItemID') == '6' else 'galaxy' if sid == 'GalaxySummon' else 'normal' if sid == 'NormalSummon' else 'limited'
        pool_name = name(r.get('Name'), sid)
        if kind == 'galaxy':
            pool_name = '银河招募'
        pools[sid] = {'id': sid, 'name': pool_name, 'kind': kind,
                      'icon': results.get((r.get('GuaranteedRoleIDs') or '').split('~')[0], {}).get('icon'),
                      'activity': r.get('ActivityID') or '',
                      'activities': list(filter(None, (r.get('ActivityIDs') or '').split('~'))),
                      'pityLimit': int(r.get('GuaranteedCount') or 0),
                      'upRoles': list(filter(None, (r.get('GuaranteedRoleIDs') or '').split('~'))),
                      'upArtifacts': [d['TagString'] for d in drops[r['Drops']] if results.get(d.get('TagString'), {}).get('kind') == 'artifact'],
                      'rates': rates(r['Drops'])}
    return {'schema': 1, 'version': version, 'pools': pools, 'results': results, 'shop': shop}
