"""Atomic, validated resource generations; bundled resources remain the fallback."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from . import config

_roots = {}
_lock = threading.RLock()


def bundled(relative):
    # Android extracts Python package data separately from ARK_ASSET_ROOT,
    # which contains only web/assets. Preserve those package-local lookups.
    if relative.startswith('backend/'):
        return Path(__file__).resolve().parent / relative[len('backend/'):]
    if relative.startswith('equipment/'):
        from equipment import __file__ as equipment_file
        return Path(equipment_file).resolve().parent / relative[len('equipment/'):]
    return Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parent.parent) / relative


def active_root():
    base = config.data_dir() / 'game-resources'
    with _lock:
        key = str(base)
        if key not in _roots:
            root = None
            try:
                pointer = json.loads((base / 'current.json').read_text())
                version = pointer['version']
                if not re.fullmatch(r'catalog_\d+', version):
                    raise ValueError('Invalid resource generation')
                built_in = json.loads(bundled('assets/game_resource_manifest.json').read_text())['version']
                if int(version.split('_')[1]) < int(built_in.split('_')[1]):
                    raise ValueError('Bundled resources are newer')
                generation = pointer.get('generation', version)
                if not re.fullmatch(re.escape(version) + r'(?:-[0-9a-f]{8})?', generation):
                    raise ValueError('Invalid resource generation path')
                candidate = base / generation
                manifest = json.loads((candidate / 'manifest.json').read_text())
                if manifest['version'] != version or not manifest['files']:
                    raise ValueError('Incomplete resource generation')
                for name, digest in manifest['files'].items():
                    if name.startswith('/') or '..' in Path(name).parts:
                        raise ValueError('Invalid resource path')
                    if hashlib.sha256((candidate / name).read_bytes()).hexdigest() != digest:
                        raise ValueError('Corrupt resource generation')
                root = candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
            _roots[key] = root
        return _roots[key]


def path(relative):
    root = active_root()
    candidate = root / relative if root else None
    return candidate if candidate and candidate.is_file() else bundled(relative)


def activate(root):
    """Caller has excluded active battles and acquired account locks."""
    base = config.data_dir() / 'game-resources'
    temporary = base / '.current.tmp'
    version = json.loads((root / 'manifest.json').read_text())['version']
    temporary.write_text(json.dumps({'version': version, 'generation': root.name}))
    os.replace(temporary, base / 'current.json')
    with _lock:
        _roots[str(base)] = root
    from . import abyss_state, abyss_team, game_data, tasks
    from equipment import stats
    abyss_state.metadata.cache_clear(); abyss_team.catalog.cache_clear()
    game_data.metadata.cache_clear(); stats._TABLES = None
    from . import energy_exchange
    energy_exchange.catalog.cache_clear()
    for key in ('_LOCALIZATION', '_ITEM_NAMES', '_ACTIVITY_NAMES', '_EQUIP_REF', '_EQUIP_REF_CACHE', '_COMMODITY_CATALOG'):
        setattr(tasks, key, None)
    from .gvg import catalog as gvg_catalog
    gvg_catalog.catalog.cache_clear(); gvg_catalog.versions.cache_clear()
