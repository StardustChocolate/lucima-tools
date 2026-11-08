"""Rebuild bundled recruitment metadata from extracted official tables."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.recruitment_catalog import build_catalog, table_rows


def build(source, version):
    raw = source / '文本/数据表'
    def rows(name):
        return table_rows(raw / f'sd_{name}.txt')
    location = source / '文本/本地化/loc_CHS_FINAL.json'
    loc = json.loads((location if location.is_file() else ROOT / 'assets/localization/loc_CHS_FINAL.json').read_text(encoding='utf-8-sig'))
    data = build_catalog(rows, loc, version)
    data['sourceHashes'] = {name: hashlib.sha256((raw / f'sd_{name}.txt').read_bytes()).hexdigest()
                            for name in ('Commodity', 'Drop', 'Role', 'Artifact', 'Item')}
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'assets/recruitment_catalog.json')
    parser.add_argument('--version', default=json.loads((ROOT / 'assets/game_resource_manifest.json').read_text())['version'])
    args = parser.parse_args()
    data = build(args.source, args.version)
    args.output.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'{len(data["pools"])} pools, {len(data["results"])} result definitions')
