"""Refresh the local assets_full archive with verified backups of changed files."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ('团员', '装备', '道具', '其它', '界面图标', '羁绊', '文本', 'spine')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for category in CATEGORIES:
        directory = root / category
        if not directory.is_dir():
            raise ValueError(f'Missing complete archive category: {directory}')
        for path in directory.rglob('*'):
            if path.is_symlink() or path.is_junction():
                raise ValueError(f'Archive contains a linked path: {path}')
            if path.is_file():
                result[path.relative_to(root).as_posix()] = digest(path)
    return result


def prune_retired_empty_dirs(source: Path, target: Path, records: list[dict]) -> list[str]:
    """Remove empty parents of retired files, preserving all current source dirs."""
    target_root = target.resolve()
    removed = []
    for row in records:
        if row['change'] != 'removed':
            continue
        directory = (target / row['path']).parent
        while directory != target:
            if not directory.resolve().is_relative_to(target_root):
                raise ValueError(f'Retired directory escapes mirror: {directory}')
            if directory.is_symlink() or directory.is_junction():
                raise ValueError(f'Retired directory is linked: {directory}')
            relative = directory.relative_to(target)
            if (source / relative).exists():
                break
            if directory.exists():
                if not directory.is_dir() or any(directory.iterdir()):
                    break
                directory.rmdir()
                removed.append(relative.as_posix())
            directory = directory.parent
    return sorted(set(removed))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Ark2 out/assets')
    parser.add_argument('--report-root', type=Path, required=True, help='Isolated report and backup directory')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source, target = args.source.resolve(), ROOT / 'assets_full'
    report_root = args.report_root.resolve()
    if target.is_symlink() or target.is_junction():
        raise ValueError('assets_full must be a normal local directory')
    if source == target.resolve() or source.is_relative_to(target.resolve()) or target.resolve().is_relative_to(source):
        raise ValueError('Source and mirror must be separate archives')
    if report_root.is_relative_to(source) or report_root.is_relative_to(target.resolve()):
        raise ValueError('Reports and backups must be outside both archives')
    expected, previous = inventory(source), inventory(target)
    records = []
    for relative in sorted(expected.keys() | previous.keys()):
        before, after = previous.get(relative), expected.get(relative)
        if before != after:
            records.append({'path': relative, 'before': before, 'after': after,
                            'change': 'added' if before is None else 'removed' if after is None else 'changed'})
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / 'mirror_sync.json'
    if report_path.exists():
        raise ValueError(f'Preserve the existing mirror run; choose a new report directory: {report_path}')
    backup = report_root / 'before_publish/assets_full'
    report = {'source': str(source), 'target': str(target), 'backup': str(backup),
              'source_files': len(expected), 'previous_files': len(previous),
              'changes': dict(Counter(row['change'] for row in records)),
              'files': records, 'applied': False, 'verification_errors': []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.apply:
        for row in records:
            relative = row['path']
            destination = target / relative
            if destination.resolve().is_relative_to(target.resolve()) is False:
                raise ValueError(f'Target escapes mirror: {destination}')
            before = digest(destination) if destination.exists() else None
            if before != row['before']:
                raise ValueError(f'Mirror changed after inventory: {destination}')
            if before is not None:
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
                if digest(saved) != before:
                    raise ValueError(f'Backup verification failed: {saved}')
            if row['after'] is None:
                destination.unlink()
            else:
                current = source / relative
                if digest(current) != row['after']:
                    raise ValueError(f'Source changed during mirror copy: {current}')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, destination)
        report['empty_directories_removed'] = prune_retired_empty_dirs(source, target, records)
        actual = inventory(target)
        report['verification_errors'] = [relative for relative in sorted(expected.keys() | actual.keys())
                                         if expected.get(relative) != actual.get(relative)]
        report['applied'] = True
        report['verified_files'] = len(actual)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if report['verification_errors']:
            raise ValueError(f"Mirror verification failed: {report['verification_errors']}")
    print(json.dumps({key: value for key, value in report.items() if key != 'files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
