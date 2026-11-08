"""Extract member-shop goods from the unpacked commodity and item tables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path('assets/guild_store.json')


def build(source: Path) -> dict:
    def table(name):
        with (source / '文本/数据表' / f'sd_{name}.txt').open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream, delimiter='@'))

    items = {row['ID']: row for row in table('Item')}
    loc = json.loads((source / '文本/本地化/loc_CHS_FINAL.json').read_text(encoding='utf-8-sig'))
    goods = {}
    activities = {row['ID']: row for row in table('Activity')}
    activity_rules = {}
    for row in table('Commodity'):
        if row['Store'] != 'MemberShop':
            continue
        identifier, item, currency = row['ID'], row['ItemID'], row['PriceItemID']
        reset, limit = row['ResetCountType'] or None, int(row['LimitCount']) if row['LimitCount'] else None
        # New or more complex pricing must be implemented deliberately, never
        # silently purchased using only the first currency or a partial rule.
        unsupported = ('PriceItemID2', 'Price2', 'Cost', 'Condition', 'AccountLv', 'VIPLV',
                       'LifeTime', 'CDTime', 'IsRandomOnce', 'Discount', 'ActivityID')
        if (any(row.get(key) for key in unsupported) or reset not in (None, 'Week', 'Month')
                or not item or currency not in ('29', '30') or int(row['Price']) <= 0
                or (reset and (limit is None or limit <= 0))):
            raise ValueError(f'Unsupported member-shop rule: {identifier}')
        name = loc.get(items[item]['Name'])
        if not isinstance(name, str) or not name:
            raise ValueError(f'Missing member-shop name: {item}')
        for key in filter(None, row['ActivityIDs'].split('~')):
            if key not in activities:
                raise ValueError(f'Missing member-shop activity: {key}')
            activity_rules[key] = {'IsUnlimitTime': activities[key].get('IsUnlimitTime', '')}
        goods[identifier] = {'item': item, 'name': name, 'amount': int(row['ItemCount'] or 1),
                             'currency': currency, 'price': int(row['Price']), 'reset': reset,
                             'limit': limit, 'activities': list(filter(None, row['ActivityIDs'].split('~')))}
    if not goods:
        raise ValueError('Empty member shop')
    return {'schema': 1, 'source': {name: hashlib.sha256(
        (source / '文本/数据表' / f'sd_{name}.txt').read_bytes()).hexdigest()
        for name in ('Commodity', 'Item', 'Activity')}, 'goods': goods, 'activities': activity_rules}


def write(target: Path, data: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def sync_icons(source: Path, target: Path, data: dict):
    # Bond cards are outside the regular item icon catalog.
    for row in data['goods'].values():
        if not row['item'].startswith('A0'):
            continue
        files = list((source / '羁绊/图标').glob(row['item'] + '_*.png'))
        if len(files) != 1:
            raise ValueError(f'Missing or ambiguous member-shop icon: {row["item"]}')
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(files[0], target / (row['item'] + '.png'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'assets_full')
    parser.add_argument('--target', type=Path, default=ROOT / TARGET)
    args = parser.parse_args()
    data = build(args.source)
    write(args.target, data)
    sync_icons(args.source, args.target.parent / 'items', data)
    print(f'Member shop: {len(data["goods"])} goods -> {args.target}')
