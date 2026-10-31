"""Extract member-shop goods from the unpacked commodity and item tables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path('assets/guild_store.json')


def build(source: Path) -> dict:
    from backend.guild_data_build import build_guild_store
    return build_guild_store(source)


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
