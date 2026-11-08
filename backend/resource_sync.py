"""Automatically synchronize official game metadata, without game-account login.

Check at process startup and while the app runs. Download into a staging folder,
compile data locally with the same parser as the build tools, then atomically
publish only when battles/asset transactions are idle. No package replacement,
remote code, account actions or purchases are involved.
"""
from __future__ import annotations
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx
from . import config, resource_store
from .resource_tables import TABLE_FIELDS, build_battle_metadata
from .table_dsl import DSLParser
from .unity_text import extract_texts

log = logging.getLogger(__name__)
_lock = threading.Lock()
_sync_lock = threading.Lock()
_waiting_accounts = set()
_thread = None
_wake = threading.Event()
_initial_ready = threading.Event()
_status = {'phase': 'idle', 'version': '', 'error': '', 'revision': 0}
CHECK_SECONDS = 30 * 60


def status():
    with _lock:
        return dict(_status)


def _note(**values):
    with _lock:
        _status.update(values)


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')


def _read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return deepcopy(default)


def _download(client, url, path, *, limit=64 * 1024 * 1024):
    if urlsplit(url).scheme != 'https' or urlsplit(url).username:
        raise ValueError('游戏资料下载地址无效')
    temporary = path.with_suffix('.part')
    size = 0
    try:
        with client.stream('GET', url) as response, temporary.open('wb') as stream:
            response.raise_for_status()
            for chunk in response.iter_bytes(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError('游戏资料下载超出大小限制')
                stream.write(chunk)
        if size == 0:
            raise ValueError('游戏资料下载为空')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rows(path):
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    headers = lines[0].split('@')
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split('@')
        if len(fields) == len(headers) + 1 and not fields[-1]:
            fields.pop()
        if len(fields) != len(headers):
            raise ValueError('游戏资料表格列数不符：' + path.name)
        yield dict(zip(headers, fields))


def compile_snapshot(source, destination, version, bundles):
    """Pure local compilation; also used by offline verification."""
    from equipment.gen_stat_tables import build as build_stats
    hashes = {name: hashlib.sha256((source / '文本/数据表' / ('sd_' + name + '.txt')).read_bytes()).hexdigest()
              for name in TABLE_FIELDS}
    battle, evidence = build_battle_metadata(source, {'version': version.replace('catalog_', 'ark2-'), 'table_sha256': hashes})
    if evidence['unparsed_fields']:
        raise ValueError('新版游戏资料包含尚未支持的属性，保留当前版本')
    _json(destination / 'assets/abyss_battle_static.json', battle)
    tables = battle['tables']
    raw = source / '文本/数据表'
    flat = source / 'stat-source'
    flat.mkdir(exist_ok=True)
    for path in raw.glob('sd_*.txt'):
        if path.stem not in ('sd_CHS', 'sd_CHT', 'sd_ENG'):
            shutil.copyfile(path, flat / path.name[3:])
    panels = build_stats(flat)
    heroes = {sid for sid, row in tables['Role'].items() if str(row.get('IsHero')).lower() in ('1', 'true')}
    if len(heroes) < 8 or heroes != set(panels['roles']) or set(tables['Artifact']) != set(panels['artifacts']):
        raise ValueError('新版角色或羁绊资料不完整，保留当前版本')
    if any(not panels['props'].get(role['prop']) for role in panels['roles'].values()):
        raise ValueError('新版角色等级属性不完整，保留当前版本')
    _json(destination / 'equipment/stat_tables.json', panels)
    local = _read(resource_store.path('assets/localization/loc_CHS_FINAL.json'), {})
    for line in (raw / 'sd_CHS.txt').read_text(encoding='utf-8-sig').splitlines()[1:]:
        if '@' in line:
            key, value = line.split('@', 1)
            if key in local or key.startswith(('T_Role_', 'T_Artifact_', 'T_Item_', 'T_Commodity_Name_', 'T_Skill_', 'T_Status_', 'T_Scene_Name_', 'T_Chapter_Name_')):
                local[key] = value
    _json(destination / 'assets/localization/loc_CHS_FINAL.json', local)
    effects, configurations, souls = {}, [], {}
    for scene, rows in sorted(tables['Fantasy'].items()):
        for row in rows:
            configurations.append({'scene': scene, 'activities': row['ActivityIDs'], 'waves': row['Waves']})
            for wave in row['Waves']:
                for group in ('Bs', 'DBs'):
                    for buff in (wave.get('Buffs') or {}).get(group) or []:
                        sid = buff['B']
                        text = local.get('T_Status_Description_' + sid)
                        effects[sid] = {'id': sid, 'description': text,
                                        'definitionSha256': hashlib.sha256(json.dumps(tables['Status'][sid], sort_keys=True).encode()).hexdigest()}
    for row in _rows(raw / 'sd_Skill.txt'):
        if row.get('SkillSoulCost') and int(row['SkillSoulCost']) > 0:
            souls[row['ID']] = {'cost': int(row['SkillSoulCost']), 'functions': DSLParser(row['SoulSkillFunc']).parse() if row.get('SoulSkillFunc') else {}}
    _json(destination / 'assets/abyss_auto_catalog.json', {
        'schema': 1, 'version': battle['version'], 'tableSha256': hashes, 'effects': effects,
        'configurations': configurations, 'soulSkills': souls, 'source': 'official Skill.SoulSkillFunc'})
    _json(destination / 'assets/game_config.json', {'schema': 1, 'tables': {
        'PublicVariable': tables['PublicVariable'], 'Activity': tables['Activity']}})
    # Keep existing image references and names for historical equipment IDs.
    references = _read(resource_store.path('backend/equip_ref.json'), {})
    parts = {'Weapon': 'weapon', 'Head': 'helmet', 'Body': 'armor', 'Necklace': 'necklace', 'Ring': 'ring', 'Shoes': 'boots'}
    for sid, row in tables['Equipment'].items():
        if row.get('Part') in parts:
            entry = references.setdefault(sid, {})
            entry.update(level=int(row.get('EquipLV') or 0), slot=parts[row['Part']])
            name = local.get('T_Item_NAME_' + sid)
            if name:
                entry['name'] = re.sub(r'<[^>]*>', '', name)
    _json(destination / 'backend/equip_ref.json', references)
    commodities = {}
    for row in _rows(raw / 'sd_Commodity.txt'):
        reset = row.get('ResetCountType') or None
        if reset and reset not in ('Day', 'Week', 'Month', 'Hour'):
            raise ValueError('新版商品限购周期不受支持')
        commodities[row['ID']] = {'store': row.get('Store') or '', 'resetType': reset,
                                   'limit': max(0, int(row['LimitCount'])) if row.get('LimitCount') else None}
    _json(destination / 'backend/commodity_catalog.json', {'schema': 1, 'source': version, 'commodities': commodities})
    from .recruitment_catalog import build_catalog, table_rows
    recruitment = build_catalog(lambda name: table_rows(raw / f'sd_{name}.txt'), local, version)
    _json(destination / 'assets/recruitment_catalog.json', recruitment)
    from .inventory_metadata import build as build_inventory_labels
    _json(destination / 'assets/inventory_labels.json', build_inventory_labels(_rows(raw / 'sd_Item.txt'),local))
    from .management_catalog import build as build_management, read_rows as management_rows
    _json(destination / 'assets/management_catalog.json', build_management(lambda name: management_rows(raw / f'sd_{name}.txt')))
    from .account_buffs import build_catalog as build_buffs
    _json(destination / 'assets/account_buff_catalog.json', build_buffs(management_rows(raw / 'sd_ServerStatus.txt')))
    from .energy_exchange import build_catalog as build_energy
    _json(destination / 'assets/energy_exchange.json', build_energy(lambda name: management_rows(raw / f'sd_{name}.txt'), version))
    files = {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in destination.rglob('*.json')}
    manifest = {'version': version, 'bundles': bundles, 'files': files, 'createdAt': int(time.time())}
    _json(destination / 'manifest.json', manifest)
    return manifest


def _publish_when_idle(root):
    from . import appcore, accounts
    # Never hold session locks while waiting for a worker which may publish
    # progress under those same locks. Try account locks without blocking.
    with ExitStack() as stack:
        for account in sorted(accounts.all_accounts(), key=lambda item: item.email):
            if not account.lock.acquire(blocking=False):
                return False
            stack.callback(account.lock.release)
        with appcore._abyss_sessions_lock, appcore._activity_sessions_lock:
            with _lock:
                waiting = set(_waiting_accounts)
            active = {key for key in appcore._abyss_sessions
                      if key not in waiting or appcore._abyss_session_modes.get(key) != 'intimacy'}
            if active or appcore._activity_sessions:
                return False
            resource_store.activate(root)
            # A new resource period can also have new login-only scheduling.
            # Re-read it at this idle boundary, without changing task switches.
        for account in accounts.all_accounts():
            try:
                if account.client.is_connected():
                    account.client.bootstrap()
                    account.note_state_refreshed()
            except Exception:
                log.warning('游戏资料已更新，账号排期暂未刷新', exc_info=True)
    return True


def synchronize(*, info=None, download=_download, publish=_publish_when_idle, force=False, cancel_event=None):
    """Serialize background and task-requested updates, including cancellation."""
    while not _sync_lock.acquire(timeout=.1):
        if cancel_event is not None and cancel_event.is_set():
            from .game_client import GameError
            raise GameError('资料同步已中止')
    try:
        return _synchronize(info=info, download=download, publish=publish, force=force, cancel_event=cancel_event)
    finally:
        _sync_lock.release()


def _synchronize(*, info=None, download=_download, publish=_publish_when_idle, force=False, cancel_event=None):
    """One transaction. A failure leaves current.json and all live caches intact."""
    _note(phase='checking', error='')
    if info is None:
        from .game_client import GameClient
        client = GameClient('')
        try:
            info = client.call('GameServerDBSettingHandler.QueryBulletinInfoResult', {})['Info']
        finally:
            client.close()
    version = info.get('NewCatalogName', '')
    if not re.fullmatch(r'catalog_\d+', version):
        raise ValueError('服务器没有返回有效的游戏资料版本')
    current = _read(resource_store.path('manifest.json'), {}) if resource_store.active_root() else _read(resource_store.bundled('assets/game_resource_manifest.json'), {})
    if current.get('version') == version and not force and {'assets/management_catalog.json', 'assets/account_buff_catalog.json'} <= current.get('files', {}).keys():
        _note(phase='ready', version=version, checkedAt=int(time.time()))
        return False
    if current.get('version') and int(current['version'].split('_')[1]) > int(version.split('_')[1]):
        raise ValueError('服务器资料版本暂时落后，保留本地新版')
    base = config.data_dir() / 'game-resources'; base.mkdir(parents=True, exist_ok=True)
    domain = info.get('PathDomains') or info.get('PathDomain') or ''
    if not isinstance(domain, str) or not domain.startswith('https://'):
        raise ValueError('服务器资源地址不受支持')
    domain = domain.split(',')[0].rstrip('/')
    with tempfile.TemporaryDirectory(prefix='.sync-', dir=base) as temporary:
        work = Path(temporary)
        with httpx.Client(proxy=config.get_proxy() or None, trust_env=False,
                          timeout=httpx.Timeout(45, connect=15), follow_redirects=False) as client:
            catalog_path = work / 'catalog.json'
            download(client, domain + '/Android/' + version + '.json', catalog_path)
            catalog = _read(catalog_path)
            if not isinstance(catalog, dict) or not isinstance(catalog.get('m_InternalIds'), list):
                raise ValueError('游戏资料索引无效')
            bundle_ids = {name for name in catalog['m_InternalIds'] if isinstance(name, str)
                          and name.startswith('http://PatchDomain/') and '/game/staticdata' in name and name.endswith('.bundle')}
            static = [name for name in bundle_ids if '/game/staticdata_' in name]
            texts = [name for name in bundle_ids if '/game/staticdata/text_' in name]
            if len(static) != 1 or len(texts) != 1:
                raise ValueError('游戏资料索引没有唯一的静态表和文本包')
            bundle_ids = sorted(bundle_ids)
            del catalog
            _note(phase='downloading', targetVersion=version)
            raw = work / 'source/文本/数据表'
            for name in [static[0], texts[0]]:
                path = work / Path(name).name
                download(client, name.replace('http://PatchDomain', domain, 1), path)
                _note(phase='extracting')
                extract_texts(path, raw, wanted={'CHS'} if name == texts[0] else None)
        _note(phase='compiling')
        staged = work / 'generation'; staged.mkdir()
        manifest = compile_snapshot(work / 'source', staged, version, bundle_ids)
        destination = base / version
        if destination.exists():
            # A repair of the active version must never remove its live files.
            destination = base / (version + '-' + uuid.uuid4().hex[:8])
        os.replace(staged, destination)
    _note(phase='waiting', targetVersion=version)
    while not publish(destination):
        if cancel_event is not None:
            if cancel_event.wait(.5):
                from .game_client import GameError
                raise GameError('资料同步已中止')
        else:
            _wake.wait(5); _wake.clear()
    _note(phase='ready', version=version, checkedAt=int(time.time()), revision=status()['revision'] + 1)
    return manifest


def ensure_roles(role_ids, *, account_key, cancel_event, on_progress=None):
    """A paused friendship worker can fetch data between completed battles."""
    from .abyss_state import metadata
    from .game_client import GameError
    role_ids = set(role_ids)
    def missing():
        table = metadata()['tables']['Role']
        return sorted(sid for sid in role_ids if not table.get(sid, {}).get('InitStar'))
    failures = 0
    force = True
    with _lock:
        _waiting_accounts.add(account_key)
    try:
        while missing():
            if cancel_event.is_set():
                raise GameError('自动刷亲密度已中止')
            if on_progress:
                on_progress('正在自动获取角色资料：' + '、'.join(missing()) + '，获取后继续刷亲密度')
            try:
                synchronize(force=force, cancel_event=cancel_event)
                force = False
                if not missing():
                    return
                detail = '官方资料尚未包含该角色'
            except Exception as error:
                if cancel_event.is_set():
                    raise GameError('自动刷亲密度已中止') from error
                detail = str(error)
            failures += 1
            seconds = min(60, failures * 10)
            if on_progress:
                on_progress(f'角色资料同步暂未完成：{detail}，{seconds}秒后自动重试')
            if cancel_event.wait(seconds):
                raise GameError('自动刷亲密度已中止')
    finally:
        with _lock:
            _waiting_accounts.discard(account_key)


def _run():
    retry = False
    while True:
        try:
            synchronize()
            retry = False
        except Exception as error:
            log.warning('自动同步游戏资料失败，将保留上一版并重试：%s', error)
            _note(phase='error', error=str(error), checkedAt=int(time.time()))
            retry = True
        finally:
            _initial_ready.set()
        _wake.wait(60 if retry else CHECK_SECONDS)
        _wake.clear()


def start():
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name='game-resource-sync', daemon=True)
        _thread.start()


def request_check():
    """Foreground/open events request a fresh check, with a short debounce."""
    snapshot = status()
    if snapshot['phase'] in ('ready', 'error', 'idle') and time.time() - snapshot.get('checkedAt', 0) >= 60:
        _wake.set()


def wait_initial_sync():
    """Let first-start metadata finish before a new battle reserves its session.

    The HTTP/UI thread remains independent of the background worker. After a
    failed check, the last complete bundled/cached generation is still usable.
    Later periodic checks do not suspend already-running tasks.
    """
    if _thread and _thread.is_alive():
        _initial_ready.wait(120)
