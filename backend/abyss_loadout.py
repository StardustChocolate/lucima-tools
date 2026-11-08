"""Durable, per-account temporary loadouts with server-confirmed restoration.

The journal is committed before any mutation. Recovery never trusts assumed local
ownership. Each asset is returned independently; confirmed failures are isolated
with their original owners intact so other assets can serve later tasks.
"""
from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from . import config
from .game_client import GameError, GameMessage
from .logutil import log
from .abyss_state import metadata
from .tasks import _oid,item_name
from equipment import plans, stats, bonds, recommend


class LoadoutUnavailable(GameError):
    """This particular lineup cannot get a complete non-overlapping outfit."""


def _path(client, account_key=None):
    key=str(account_key or getattr(client,'login_id',None) or getattr(client,'aid',None) or '')
    if not key:raise GameError('缺少账号标识，无法保存临时配装恢复记录')
    return config.data_dir()/'abyss-recovery'/(hashlib.sha256(key.encode()).hexdigest()+'.json')


_JOURNAL_DB_LOCK = threading.RLock()


def _journal_key(path):
    """Keep the legacy filename only as a stable key for the SQL journal."""
    return ('deferred:' if path.parent.name == 'deferred' else 'active:') + path.stem


def _database_path():
    return config.data_dir() / 'records' / 'asset_recovery.sqlite3'


def _database():
    """Open the local durable loan ledger and create its small schema once."""
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS asset_recovery_journals (
            journal_key TEXT PRIMARY KEY,
            account_key TEXT NOT NULL,
            deferred INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS asset_recovery_assets (
            journal_key TEXT NOT NULL,
            asset_kind TEXT NOT NULL CHECK(asset_kind IN ('equipment', 'artifacts')),
            asset_id TEXT NOT NULL,
            original_role_id TEXT NOT NULL,
            original_role_static_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(journal_key, asset_kind, asset_id),
            FOREIGN KEY(journal_key) REFERENCES asset_recovery_journals(journal_key)
                ON DELETE CASCADE
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS asset_recovery_assets_owner_idx '
                   'ON asset_recovery_assets(original_role_id)')
        db.execute('''CREATE TABLE IF NOT EXISTS asset_recovery_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_key TEXT NOT NULL,
            account_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload TEXT NOT NULL,
            archived_at_ms INTEGER NOT NULL
        )''')
        return db
    except Exception:
        db.close()
        raise


def _journal_destinations(journal, field):
    original = journal.get('originalDestinations') or {}
    values = original.get(field) if isinstance(original, dict) else None
    if not isinstance(values, dict):
        values = journal.get(field) or {}
    return values if isinstance(values, dict) else {}


def _write_legacy(path, payload):
    """Keep a read-only-compatible JSON mirror while SQL remains authoritative."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.restore-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _history_path(path):
    history = path.parent / 'history'
    history.mkdir(parents=True, exist_ok=True)
    return history / f'{path.stem}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json'


def _save(path, journal):
    """Commit the per-asset SQL rows and recovery payload before mutations."""
    payload = json.dumps(journal, ensure_ascii=False, sort_keys=True)
    key = _journal_key(path)
    account_key = str(journal.get('accountKey') or '')
    deferred = int(path.parent.name == 'deferred')
    owner_roles = journal.get('ownerRoles') or {}
    rows = []
    for field in ('equipment', 'artifacts'):
        for asset_id, owner in _journal_destinations(journal, field).items():
            owner = str(owner or '')
            rows.append((key, field, str(asset_id), owner,
                         str(owner_roles.get(owner) or '')))
    now = int(time.time() * 1000)
    with _JOURNAL_DB_LOCK, _database() as db:
        previous = db.execute(
            'SELECT created_at_ms FROM asset_recovery_journals WHERE journal_key = ?', (key,)
        ).fetchone()
        db.execute('''INSERT INTO asset_recovery_journals
                      (journal_key, account_key, deferred, payload, created_at_ms, updated_at_ms)
                      VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(journal_key) DO UPDATE SET
                        account_key=excluded.account_key, deferred=excluded.deferred,
                        payload=excluded.payload, updated_at_ms=excluded.updated_at_ms''',
                   (key, account_key, deferred, payload, previous[0] if previous else now, now))
        db.execute('DELETE FROM asset_recovery_assets WHERE journal_key = ?', (key,))
        if rows:
            db.executemany('''INSERT INTO asset_recovery_assets
                            (journal_key, asset_kind, asset_id, original_role_id, original_role_static_id)
                            VALUES (?, ?, ?, ?, ?)''', rows)
    # Previous releases used this file format.  Leave a synchronized mirror so
    # upgrades can be inspected and older recovery tools remain able to import
    # a record; restore always reads the SQL payload first.
    _write_legacy(path, payload)


def _load(path):
    """Read SQL first; a legacy JSON journal is imported on first access."""
    key = _journal_key(path)
    with _JOURNAL_DB_LOCK, _database() as db:
        row = db.execute('SELECT payload FROM asset_recovery_journals WHERE journal_key = ?', (key,)).fetchone()
    if row is not None:
        return json.loads(row[0])
    journal = json.loads(path.read_text(encoding='utf-8'))
    _save(path, journal)
    return journal


def _exists(path):
    key = _journal_key(path)
    with _JOURNAL_DB_LOCK, _database() as db:
        row = db.execute('SELECT 1 FROM asset_recovery_journals WHERE journal_key = ?', (key,)).fetchone()
    return row is not None or path.exists()


def _remove(path):
    key = _journal_key(path)
    with _JOURNAL_DB_LOCK, _database() as db:
        db.execute('DELETE FROM asset_recovery_assets WHERE journal_key = ?', (key,))
        db.execute('DELETE FROM asset_recovery_journals WHERE journal_key = ?', (key,))
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _archive(path, journal, reason):
    """Keep an audit record for fallback/invalid restores, then clear active rows."""
    key = _journal_key(path)
    payload = json.dumps(journal, ensure_ascii=False, sort_keys=True)
    with _JOURNAL_DB_LOCK, _database() as db:
        db.execute('''INSERT INTO asset_recovery_history
                    (journal_key, account_key, reason, payload, archived_at_ms)
                    VALUES (?, ?, ?, ?, ?)''',
                   (key, str(journal.get('accountKey') or ''), reason, payload,
                    int(time.time() * 1000)))
    _write_legacy(_history_path(path), payload)
    _remove(path)


def _archive_raw(path, reason):
    key = _journal_key(path)
    with _JOURNAL_DB_LOCK, _database() as db:
        row = db.execute('SELECT account_key, payload FROM asset_recovery_journals WHERE journal_key = ?', (key,)).fetchone()
    if row is not None:
        account_key, payload = row
    else:
        account_key = ''
        try:
            payload = path.read_text(encoding='utf-8')
        except OSError:
            payload = '{}'
    with _JOURNAL_DB_LOCK, _database() as db:
        db.execute('''INSERT INTO asset_recovery_history
                    (journal_key, account_key, reason, payload, archived_at_ms)
                    VALUES (?, ?, ?, ?, ?)''',
                   (key, account_key, reason, payload, int(time.time() * 1000)))
    _write_legacy(_history_path(path), payload)
    _remove(path)


def pending(client,account_key=None):
    """Only active recovery blocks borrowing; isolated assets are excluded."""
    path=_path(client,account_key)
    return _exists(path)


def _deferred_path(client, account_key=None):
    path=_path(client,account_key)
    return path.parent/'deferred'/path.name


def deferred_assets(client, account_key=None):
    """Rejected old loans remain durable, separate from a new active journal."""
    key=account_key or getattr(client,'_asset_recovery_key',None) or getattr(client,'login_id',None) or getattr(client,'aid',None)
    result={'equipment':set(),'artifacts':set()}
    if not key:return result
    for scope in (key,f'{key}::intimacy-passengers::upper',f'{key}::intimacy-passengers::lower'):
        path=_deferred_path(client,scope)
        if not _exists(path):continue
        try:
            record=_load(path)
            if record.get('schema')!=1 or any(not isinstance(record.get(field),dict) for field in result):
                raise ValueError('invalid deferred journal')
            for field in result:result[field].update(record[field])
        except (OSError,ValueError,TypeError) as error:
            raise GameError('历史借装隔离记录异常，请保留记录后重试') from error
    return result


def is_no_role(error):
    """Recognize the native rejection, without masking unrelated exceptions."""
    return isinstance(error,GameMessage) and error.msg.strip().casefold()=='no role'


def sync_owned_assets(client, *, equipment=True, artifacts=True):
    """Fetch owned instances; an omitted container is not an empty inventory."""
    client.bootstrap()
    state=client.account_state or {}
    required=[('RoleDataContainer','Roles','角色')]
    if equipment:required.append(('EquipmentContainer','Datas','装备'))
    if artifacts:required.append(('Artifacts','Datas','羁绊'))
    for container,field,label in required:
        if not isinstance((state.get(container)or{}).get(field),list):
            raise GameError(f'服务器未返回完整{label}信息，保留借装记录等待同步')


def protected_deferred_assets(client, account_key=None):
    protected=deferred_assets(client,account_key)
    rows={'equipment':plans._equips(client),'artifacts':bonds.inventory(client.account_state or {})}
    holders={_oid(item.get('EquipRole')) for field,items in rows.items() for item in items
             if _oid(item['_id']) in protected[field]}-{'',None}
    for field,items in rows.items():
        protected[field].update(_oid(item['_id']) for item in items if _oid(item.get('EquipRole')) in holders)
    return protected


def recover_before_challenge(client,*,account_key=None):
    """Attempt legacy and current loans before borrowing again."""
    return restore_many(client,[account_key])


def isolate_rejected(client,account_keys,*,retry_deferred=True):
    """Sync, isolate loans the server refuses, and report assets to keep out of new plans.

    An account can keep an old loan whose original hero instance no longer exists
    (native `No Role`). Per the recovery rules that asset stays isolated with its
    recorded owner so a later task borrows other equipment instead of stopping.
    Assets still pending because the account could not be read stay pending.
    """
    keys=list(dict.fromkeys(account_keys))
    result=restore_many(client,keys,defer_no_role=True,retry_deferred=retry_deferred,defer_unreturned=True)
    deferred=protected_deferred_assets(client,keys[0])
    return {**result,'warnings':[result['warning']] if result.get('warning') else [],
            'equipment':set(result.get('unresolvedEquipment')or[])|deferred['equipment'],
            'artifacts':set(result.get('unresolvedArtifacts')or[])|deferred['artifacts']}


def _owners(items):
    owners={}
    for item in items:
        aid=_oid(item.get('_id'))
        if not aid or aid in owners:raise GameError('资产编号缺失或重复，无法安全临时借装')
        owners[aid]=_oid(item.get('EquipRole')) or ''
    return owners


def _clean_ownership(state):
    """Plan orphan/duplicate ownership repairs without changing live state."""
    roles={_oid(role['_id']) for role in (state.get('RoleDataContainer')or{}).get('Roles')or[]}
    equips=(state.get('EquipmentContainer')or{}).get('Datas')or[]
    equipment=_owners(equips);artifacts=_owners(bonds.inventory(state))
    cleanup_equipment=set();cleanup_artifacts=set();occupied={}
    for item in equips:
        aid=_oid(item['_id']);owner=equipment[aid];slot=plans.equip_slot(item)
        if not owner:continue
        if owner not in roles:
            cleanup_equipment.add(aid)
        elif slot in plans.SLOTS:
            key=(owner,slot)
            if key in occupied:cleanup_equipment.add(occupied[key])
            occupied[key]=aid  # Native worn_items uses the final item per slot.
    by_owner=defaultdict(list)
    for aid,owner in artifacts.items():
        if owner:by_owner[owner].append(aid)
    for owner,ids in by_owner.items():
        if owner not in roles or len(ids)>1:cleanup_artifacts.update(ids)
    repairs={'equipment':{aid:equipment[aid] for aid in cleanup_equipment},
             'artifacts':{aid:artifacts[aid] for aid in cleanup_artifacts}}
    for item in equips:
        aid=_oid(item['_id'])
        if aid in cleanup_equipment:item['EquipRole']='';equipment[aid]=''
    for item in bonds.inventory(state):
        aid=_oid(item['_id'])
        if aid in cleanup_artifacts:item['EquipRole']='';artifacts[aid]=''
    return equipment,artifacts,sorted(cleanup_equipment),sorted(cleanup_artifacts),repairs


def _take_off_artifact(client, aid):
    """UnEquip is keyed by artifact ID, including an orphaned prior owner."""
    item=next((row for row in bonds.inventory(client.account_state) if _oid(row['_id'])==aid),None)
    owner=_oid((item or {}).get('EquipRole'))or''
    if not owner:return
    response=client.call('ArtifactHandler.UnEquip',client._auth_data({'RoleID':owner,'ArtifactID':aid}))
    bonds._merge(client,response)
    item=next((row for row in bonds.inventory(client.account_state) if _oid(row['_id'])==aid),None)
    if _oid((item or {}).get('EquipRole')):
        client.bootstrap()
        item=next((row for row in bonds.inventory(client.account_state) if _oid(row['_id'])==aid),None)
        if _oid((item or {}).get('EquipRole')):raise GameError('服务器尚未确认羁绊卸下')


def _profile(role, purposes, offensive):
    from .abyss_team import combat_role
    value=deepcopy(combat_role(role)['profile'])
    if purposes:
        value.update(id='abyss-utility',title='破甲/克制先手：速度＋命中',priorities=['Speed','EffectHitRate','HP'],
                     ranking='speed-first',tieBreak=['EffectHitRate','HP','Defence'],critical=False,
                     hardConditions=[],hitTarget=True,metricLabel='速度优先')
    return value


def _assign_artifacts(ids, options, *, allow_empty=False):
    """Maximum-weight distinct compatible bonds, including universal-job bonds."""
    empty={rid:'__empty_bond_'+str(i) for i,rid in enumerate(ids)} if allow_empty else {}
    if allow_empty:
        options={rid:[*options[rid],(-1e6,0,False,empty[rid])] for rid in ids}
    aids=sorted({row[-1] for choices in options.values() for row in choices})
    if len(aids)<len(ids):raise LoadoutUnavailable('缺少足够的兼容且不重复的羁绊')
    values={rid:{row[-1]:row[0]+row[1]*1e-8+int(row[2])*1e-10 for row in options[rid]} for rid in ids}
    # Rectangular Hungarian assignment, O(roles² * inventory).
    n,m=len(ids),len(aids);u=[0.]*(n+1);v=[0.]*(m+1);p=[0]*(m+1);way=[0]*(m+1)
    for i in range(1,n+1):
        p[0]=i;j0=0;minimum=[float('inf')]*(m+1);used=[False]*(m+1)
        while True:
            used[j0]=True;i0=p[j0];delta=float('inf');j1=0
            for j in range(1,m+1):
                if used[j]:continue
                cost=-values[ids[i0-1]].get(aids[j-1],-1e12)-u[i0]-v[j]
                if cost<minimum[j]:minimum[j]=cost;way[j]=j0
                if minimum[j]<delta:delta=minimum[j];j1=j
            for j in range(m+1):
                if used[j]:u[p[j]]+=delta;v[j]-=delta
                else:minimum[j]-=delta
            j0=j1
            if not p[j0]:break
        while True:
            j1=way[j0];p[j0]=p[j1];j0=j1
            if not j0:break
    result={ids[p[j]-1]:aids[j-1] for j in range(1,m+1) if p[j]}
    if any(aid not in values[rid] for rid,aid in result.items()):raise LoadoutUnavailable('没有可同时装备的完整羁绊组合')
    return {rid:('' if aid==empty.get(rid) else aid) for rid,aid in result.items()}


def plan(state,waves,*,cancelled=None,progress=None,offensive=True,utility_roles=None,role_ids=None,equip_artifacts=False,
         survival_fallback=False,exclude_equipment=(),exclude_artifacts=(),profile_overrides=None):
    profile_overrides=profile_overrides or {}
    ids=list(role_ids) if role_ids is not None else [rid for wave in ('upper','lower') for rid in waves[wave]]
    count=len(ids) if role_ids is not None else 8
    if not 1<=count<=8 or len(ids)!=count or len(set(ids))!=count:raise GameError(f'自动配装必须为 {count} 名不同角色，不能共用装备')
    state=deepcopy(state)
    eq_owners,art_owners,cleanup_equipment,cleanup_artifacts,repairs=_clean_ownership(state)
    # Old assets whose unload was explicitly rejected are left out of this run.
    # They must not become a second preflight gate through ownership repair.
    cleanup_equipment=[aid for aid in cleanup_equipment if aid not in exclude_equipment]
    cleanup_artifacts=[aid for aid in cleanup_artifacts if aid not in exclude_artifacts]
    for aid in exclude_equipment:
        if aid in eq_owners:eq_owners[aid]=''
    for aid in exclude_artifacts:
        if aid in art_owners:art_owners[aid]=''
    roles={_oid(r['_id']):r for r in (state.get('RoleDataContainer')or{}).get('Roles')or[]}
    if any(rid not in roles for rid in ids):raise GameError('自动配装角色不属于当前账号')
    if utility_roles is None:
        from .abyss_team import role_traits
        utility_roles={}
        for wave in ('upper','lower'):
            available=[rid for rid in waves[wave] if role_traits(roles[rid])['break']]
            if available:
                primary=max(available,key=lambda rid:(role_traits(roles[rid])['soulBreak'],stats.role_base(roles[rid]).get('Speed',0),rid))
                utility_roles[primary]=['break']
    if not set(utility_roles)<=set(ids):raise GameError('先手破甲/克制角色必须来自本次八人队伍')
    equips=(state.get('EquipmentContainer')or{}).get('Datas')or[]
    reserved=set(exclude_equipment);targets={};touched=set(cleanup_equipment)
    reserved.update(_oid(item['_id']) for item in equips if plans.equip_slot(item) not in plans.SLOTS)
    excluded_artifacts=set(exclude_artifacts)
    artifact_targets={}
    # Unknown bonds cannot be scored. Remove them only in the planned copy,
    # then journal an actual unload (or replacement) before fighting. Their
    # original owners remain in art_owners for the normal return transaction.
    for art in bonds.inventory(state):
        owner=_oid(art.get('EquipRole')) or ''
        if owner in ids and not bonds.permanent_mods(art)[0]:
            artifact_targets[owner]=''
            art['EquipRole']=''
    if equip_artifacts:
        # Evaluate real compatible objects, including their permanent modifiers,
        # before choosing gear. The journal preserves every displaced owner.
        state=deepcopy(state)
        artifacts=bonds.inventory(state);art_table=metadata()['tables']['Artifact']
        options={}
        baseline={_oid(art['_id']):_oid(art.get('EquipRole'))or'' for art in artifacts}
        for rid in ids:
            if cancelled and cancelled():raise GameError('自动配装已中止')
            profile=deepcopy(profile_overrides.get(rid) or _profile(roles[rid],None,True))
            worn,_=plans.worn_items(equips,rid)
            choices=[]
            for art in artifacts:
                aid=_oid(art['_id']);definition=art_table.get(art.get('StaticID'),{})
                if aid in excluded_artifacts or definition.get('Job') not in ('Other',bonds.job(roles[rid])) or not bonds.permanent_mods(art)[0]:continue
                for item in artifacts:
                    owner=baseline[_oid(item['_id'])]
                    item['EquipRole']='' if owner==rid else owner
                art['EquipRole']=rid
                panel=stats.role_panel(state,roles[rid],worn,include_artifact_effects=True)['panel']
                choices.append((recommend.metric(panel,profile),int(art.get('LV')or 1),baseline[aid]==rid,aid))
            for item in artifacts:item['EquipRole']=baseline[_oid(item['_id'])]
            options[rid]=choices
        artifact_targets=_assign_artifacts(ids,options,allow_empty=True)
        assigned={aid:rid for rid,aid in artifact_targets.items() if aid}
        for item in artifacts:
            aid=_oid(item['_id']);owner=baseline[aid]
            item['EquipRole']=assigned.get(aid,'' if owner in ids else owner)
    for rid in ids:
        if rid not in roles:raise GameError('自动配装角色不属于当前账号')
        worn,conflicts=plans.worn_items(equips,rid)
        if conflicts:raise GameError('存在角色同部位重复穿戴，无法安全自动借装')
        # A utility opener gets first access to fast pieces, including pieces
        # worn by these eight teammates. Rebuild their outfits afterwards.
        if len(worn)==6 and not offensive and not utility_roles:
            targets[rid]=[ _oid(e['_id']) for e in worn ];reserved.update(targets[rid])
    ordered=sorted((rid for rid in ids if rid not in targets),key=lambda rid:(rid not in utility_roles,'break' not in utility_roles.get(rid,[]),
                   not any('damage' in p['priorities'] for p in recommend.profiles(roles[rid])),ids.index(rid)))
    for rid in ordered:
        if cancelled and cancelled():raise GameError('自动配装已中止，未开始借装')
        profile=deepcopy(profile_overrides.get(rid) or _profile(roles[rid],utility_roles.get(rid),offensive))
        if progress:progress({'phase':'equipping','message':f'正在为{item_name(roles[rid]["StaticID"])}选择{profile["title"]}装备','waves':waves})
        scale=profile['damageStat'];output_properties={scale+'Value',scale+'Rate','SpeedValue'}
        if profile.get('critical'):output_properties.update(('CriticalRate','CriticalDamageRate'))
        output_exclude={_oid(item['_id']) for item in equips if plans.equip_slot(item) in ('necklace','ring','boots')
                        and (item.get('MainProp')or{}).get('PropertyType') not in output_properties} if survival_fallback and 'damage' in profile['priorities'] else set()
        def search(only_max,hit_target=100,extra_exclude=()):
            value=recommend.search(state,roles[rid],profile_id=profile['id'],profile_override=profile,depth='standard',include_worn=True,
                                   only_max=only_max,hit_target=hit_target,free_roles=set(ids),exclude=reserved|set(extra_exclude),cancelled=cancelled)
            return value['results']
        rows=search(True,extra_exclude=output_exclude)
        if not rows:
            rows=search(False,extra_exclude=output_exclude)
        if not rows and survival_fallback:
            profile=dict(profile,id='intimacy-survival',title='生存备用',priorities=['HP','Defence','Speed'],critical=False,
                         ranking=None,hardConditions=[],hitTarget=False)
            if progress:progress({'phase':'equipping','message':f'{item_name(roles[rid]["StaticID"])}输出装备不足，改用完整生存装备','waves':waves})
            rows=search(True) or search(False)
        if not rows and utility_roles.get(rid):
            # A limited inventory must not remove the breaker from the team.
            # Keep speed first, then effectiveness, without inventing a hit guarantee.
            rows=search(False,0)
            if progress and rows:progress({'phase':'equipping','message':f'{item_name(roles[rid]["StaticID"])}未找到命中达标套装，改用可用的高速＋命中装备；有星源时优先烧魂关键减益','waves':waves})
        if not rows:raise LoadoutUnavailable(f'{count} 名角色无法取得 {count*6} 件互斥装备，请补足缺少部位后重试')
        target=list(rows[0]['equipIds'].values());targets[rid]=target;reserved.update(target)
        touched.update(target);touched.update(aid for aid,owner in eq_owners.items() if owner==rid)
    if len({aid for group in targets.values() for aid in group})!=count*6:raise GameError(f'自动配装未形成 {count*6} 件互斥装备，已停止')
    return {'targets':targets,'equipment':{aid:eq_owners[aid] for aid in sorted(touched)},
            'artifacts':{aid:'' for aid in cleanup_artifacts},'artifactTargets':artifact_targets,
            'cleanupEquipment':cleanup_equipment,'cleanupArtifacts':cleanup_artifacts,
            'ownershipRepairs':repairs,
            'roles':ids,'artifactBaseline':art_owners,'createdAt':int(time.time())}


def apply(client,loadout,*,account_key=None,cancelled=None):
    if account_key:client._asset_recovery_key=str(account_key).split('::intimacy-passengers::',1)[0]
    path=_path(client,account_key)
    if _exists(path):raise GameError('上次深渊借装尚未恢复，请先恢复装备')
    protected=protected_deferred_assets(client,getattr(client,'_asset_recovery_key',None) or account_key)
    holders={_oid(item.get('EquipRole')) for items,ids in
             ((plans._equips(client),protected['equipment']),(bonds.inventory(client.account_state or {}),protected['artifacts']))
             for item in items if _oid(item['_id']) in ids}-{'',None}
    if (protected['equipment'].intersection(loadout['equipment']) or
        protected['artifacts'].intersection(set(loadout.get('artifacts',{}))|set(loadout.get('artifactTargets',{}).values())) or
        holders.intersection(set(loadout.get('targets',{}))|set(loadout.get('artifactTargets',{})))):
        raise LoadoutUnavailable('本次配装包含隔离中的历史借装，已停止更换，请使用其他可用装备')
    if not loadout['equipment'] and not loadout.get('artifactTargets'):return False
    record={'schema':1,'phase':'applying',
            'accountKey':str(account_key or getattr(client,'login_id',None) or getattr(client,'aid',None) or ''),
            **deepcopy(loadout)}
    # Scoring works on a cleaned copy. Journal the actual pre-mutation owners,
    # including orphan/duplicate ownership, rather than that copy's empty IDs.
    original_equipment=_owners(plans._equips(client))
    repairs=record.get('ownershipRepairs')or{}
    for aid,owner in record['equipment'].items():
        expected=(repairs.get('equipment')or{}).get(aid,owner)
        if aid not in original_equipment or original_equipment[aid]!=expected:
            raise LoadoutUnavailable('配装前装备归属已变化，请刷新账号后重试')
        record['equipment'][aid]=expected
    original_artifacts=_owners(bonds.inventory(client.account_state))
    baseline=record.get('artifactBaseline')or original_artifacts
    for rid,aid in record.get('artifactTargets',{}).items():
        if aid and aid not in original_artifacts:raise GameError('临时羁绊不属于当前账号')
        record['artifacts'].update({key:baseline.get(key,owner) for key,owner in original_artifacts.items() if owner==rid or key==aid})
    for aid,owner in record['artifacts'].items():
        expected=(repairs.get('artifacts')or{}).get(aid,owner)
        if aid not in original_artifacts or original_artifacts[aid]!=expected:
            raise LoadoutUnavailable('配装前羁绊归属已变化，请刷新账号后重试')
        record['artifacts'][aid]=expected
    record['originalDestinations']={key:deepcopy(record[key]) for key in ('equipment','artifacts')}
    record['ownerRoles']={_oid(role['_id']):str(role.get('StaticID')or'') for role in plans._roles(client)}
    # Known inconsistent slots were deliberately planned for inventory cleanup.
    record['warehouseDestinations']={key:{aid:'invalid_baseline' for aid in record.get(field,[])}
        for key,field in (('equipment','cleanupEquipment'),('artifacts','cleanupArtifacts'))}
    _save(path,record)
    for aid in record.get('cleanupEquipment',[]):
        if cancelled and cancelled():raise GameError('自动借装已中止，正在恢复原装备')
        item=next((row for row in plans._equips(client) if _oid(row['_id'])==aid),None)
        if _oid((item or {}).get('EquipRole')):plans.take_off(client,aid,verify=True)
    for aid in record.get('cleanupArtifacts',[]):
        if cancelled and cancelled():raise GameError('自动借装已中止，正在恢复原羁绊')
        _take_off_artifact(client,aid)
    for rid,target in record['targets'].items():
        if cancelled and cancelled():raise GameError('自动借装已中止，正在恢复原装备')
        plans.equip_to_role(client,rid,target,verify=True)
        actual,_=plans.worn_items(plans._equips(client),rid)
        if {_oid(e['_id']) for e in actual if plans.equip_slot(e) in plans.SLOTS}!=set(target):
            raise GameError('临时配装尚未获服务器确认，停止开战并恢复')
    for rid,aid in record.get('artifactTargets',{}).items():
        if cancelled and cancelled():raise GameError('自动借装已中止，正在恢复原羁绊')
        if not bonds.set_equipped(client,rid,aid).get('verified'):raise GameError('临时羁绊尚未确认，停止开战并恢复')
    record['phase']='equipped';_save(path,record)
    return True


def reserved_equipment(client, account_key=None):
    """Durable loans stay protected from sales even after a worker restarts."""
    key=account_key or getattr(client,'_asset_recovery_key',None) or getattr(client,'login_id',None) or getattr(client,'aid',None)
    if not key:return set()
    result=set()
    for scope in (key, f'{key}::intimacy-passengers::upper', f'{key}::intimacy-passengers::lower'):
        try:
            record=_load(_path(client,scope))
            result.update(record.get('equipment')or{})
        except (OSError,ValueError,TypeError):pass
    result.update(deferred_assets(client,key)['equipment'])
    return result


def _return_artifact(client, aid, owner):
    if not owner:
        _take_off_artifact(client,aid)
        return
    # Return a real, journaled item even when a new bond is absent from local
    # scoring metadata. The server validates its original owner and vocation.
    response=client.call('ArtifactHandler.Equip',client._auth_data({'RoleID':owner,'ArtifactID':aid}))
    bonds._merge(client,response)


def restore(client,*,account_key=None):
    """Sync first, return independently, and retain every unconfirmed owner.

    Only an explicit No Role can be retried once after a fresh read. Transport
    failures are read back, never blindly replayed. SQL checkpoints remove only
    confirmed returns; an unsuccessful attempt is not a completed return.
    """
    path=_path(client,account_key)
    if not _exists(path):return {'restored':True,'changed':False}
    try:
        record=_load(path)
        if record.get('schema')!=1 or any(not isinstance(record.get(key),dict) for key in ('equipment','artifacts')):
            raise ValueError('恢复记录格式异常')
    except (OSError,ValueError,TypeError) as error:
        _archive_raw(path,'invalid_journal')
        return {'restored':True,'changed':False,'closed':True,'warning':str(error)}
    fields=('equipment','artifacts')
    original={key:deepcopy(_journal_destinations(record,key)) for key in fields}
    audit=deepcopy(record)
    audit.update(originalDestinations=deepcopy(original),returnResults=[])
    warehouse=audit.setdefault('warehouseDestinations',{})
    diagnostics=audit.setdefault('ownerDiagnostics',{})
    for key in fields:warehouse.setdefault(key,{})
    destinations=deepcopy(original)
    errors=[];rejected={key:set() for key in fields};attempted=0;state_fresh=False

    def sync():
        nonlocal state_fresh
        state_fresh=False
        sync_owned_assets(client,equipment=bool(original['equipment']),artifacts=bool(original['artifacts']))
        state_fresh=True

    def items(key):
        return plans._equips(client) if key=='equipment' else bonds.inventory(client.account_state)

    def current(key,aid):
        return next((row for row in items(key) if _oid(row.get('_id'))==aid),None)

    def matches(key,aid,owner):
        item=current(key,aid)
        return item is not None and (_oid(item.get('EquipRole'))or'')==owner

    def resolve(key,aid):
        owner=original[key][aid]
        if aid in warehouse[key]:return ''
        roles=plans._roles(client)
        if not owner or owner in {_oid(row['_id']) for row in roles}:return owner
        sid=(audit.get('ownerRoles')or{}).get(owner)or owner
        aliases=[_oid(row['_id']) for row in roles if row.get('StaticID')==sid]
        resolved=aliases[0] if len(aliases)==1 else ''
        diagnostics[key+':'+aid]={'originalOwner':owner,'resolvedOwner':resolved,
            'reason':'unique_role_remap' if resolved else 'original_role_missing'}
        if not resolved:warehouse[key][aid]='original_role_missing'
        return resolved

    def save_remaining(remaining):
        record.update(phase='restoring',warehouseDestinations=deepcopy(warehouse),
                      ownerDiagnostics=deepcopy(diagnostics),returnResults=deepcopy(audit['returnResults']))
        record['originalDestinations']={key:{aid:original[key][aid] for aid in remaining[key]} for key in fields}
        for key in fields:record[key]=deepcopy(record['originalDestinations'][key])
        _save(path,record)

    remaining={key:set(original[key]) for key in fields}
    save_remaining(remaining)
    try:
        # Login returns the owned role instances, equipment and bonds together.
        sync()
    except Exception as error:
        return {'restored':False,'changed':False,'stateConfirmed':False,
                'error':'同步角色、装备和羁绊失败：'+str(error),
                'unresolvedEquipment':sorted(remaining['equipment']),
                'unresolvedArtifacts':sorted(remaining['artifacts'])}
    for key in fields:
        occupied=defaultdict(list)
        for aid in original[key]:
            owner=destinations[key][aid]=resolve(key,aid)
            item=current(key,aid)
            slot=plans.equip_slot(item) if item and key=='equipment' else 'artifact'
            if owner and item and slot:occupied[(owner,slot)].append(aid)
        for ids in occupied.values():
            for aid in ids[:-1]:
                destinations[key][aid]='';warehouse[key][aid]='duplicate_original_slot'
    save_remaining(remaining)

    def send(key,aid,owner):
        nonlocal attempted
        attempted+=1
        if key=='artifacts':_return_artifact(client,aid,owner)
        elif owner:
            response=client.call('EquipmentHandler.EquipsToRole',client._auth_data({'RoleID':owner,'EquipIDs':[aid]}))
            plans._merge_equips(client,response)
        else:plans.take_off(client,aid,verify=True)

    for key in fields:
        for aid in original[key]:
            owner=destinations[key][aid]
            result={'kind':key,'id':aid,'originalOwner':original[key][aid],'destination':owner}
            # At most two returns, with a successful full sync between them.
            for attempt in range(2):
                if not state_fresh:break
                if current(key,aid) is None or matches(key,aid,owner):break
                try:
                    send(key,aid,owner)
                    break
                except Exception as error:
                    errors.append(str(error));result['error']=str(error)
                    no_role=is_no_role(error)
                    if no_role:rejected[key].add(aid)
                    try:sync()
                    except Exception as sync_error:
                        errors.append(str(sync_error));break
                    if matches(key,aid,owner) or current(key,aid) is None:break
                    if not no_role:break
                    # Re-resolve instance IDs only from the newly synced roster.
                    owner=destinations[key][aid]=resolve(key,aid)
                    log.warning('[asset-recovery] %s %s: No Role; full account synced, original role present=%s',
                                key,aid,original[key][aid] in {_oid(r['_id']) for r in plans._roles(client)})
                    if attempt==0:continue
                    if owner:
                        # A confirmed rejection can fall back to the warehouse.
                        # Persist that decision before unloading the current wearer.
                        warehouse[key][aid]='server_no_role'
                        diagnostics[key+':'+aid]={'originalOwner':original[key][aid],
                            'currentOwner':_oid((current(key,aid)or{}).get('EquipRole'))or'',
                            'reason':'server_no_role','resolvedOwner':''}
                        owner=destinations[key][aid]=''
                        save_remaining(remaining)
                        if not matches(key,aid,''):
                            try:send(key,aid,'')
                            except Exception as unload_error:
                                errors.append(str(unload_error));result['error']=str(unload_error)
                                try:sync()
                                except Exception as sync_error:errors.append(str(sync_error))
            result['destination']=owner
            if not state_fresh:
                result['state']='unconfirmed'
            elif current(key,aid) is None:
                result['state']='missing'
            elif matches(key,aid,owner):
                result['state']='warehouse' if aid in warehouse[key] else 'returned'
                remaining[key].discard(aid)
                if key=='equipment':
                    for attribute in ('_automation_reserved_equipment','_intimacy_reserved_equipment'):
                        reserved=getattr(client,attribute,None)
                        if isinstance(reserved,set):reserved.discard(aid)
            else:result['state']='unconfirmed'
            audit['returnResults'].append(result)
            save_remaining(remaining)

    # Do not infer success/missing assets from a stale or optimistic local cache.
    confirmed=False
    try:
        sync();confirmed=True
    except Exception as error:errors.append(str(error))
    fallback={key:[] for key in ('equipment','artifacts','missingEquipment','missingArtifacts')}
    returned={key:[] for key in fields}
    if confirmed:
        remaining={key:set() for key in fields}
        for key in fields:
            actual=_owners(items(key))
            for aid,owner in destinations[key].items():
                if aid not in actual:
                    fallback['missingEquipment' if key=='equipment' else 'missingArtifacts'].append(aid)
                elif actual[aid]!=owner:remaining[key].add(aid)
                elif aid in warehouse[key]:fallback[key].append(aid)
                else:returned[key].append(aid)
    else:
        remaining={key:set(original[key]) for key in fields}
    for row in audit['returnResults']:
        key,aid=row['kind'],row['id']
        missing_key='missingEquipment' if key=='equipment' else 'missingArtifacts'
        row['state']=('unconfirmed' if aid in remaining[key] else 'missing' if aid in fallback[missing_key]
                      else 'warehouse' if aid in fallback[key] else 'returned')
    audit.update(fallback=fallback,syncErrors=errors,phase='return_verified' if confirmed else 'recovery_required')
    summary={'changed':True,'stateConfirmed':confirmed,'attempted':attempted,'fallback':fallback,'returned':returned}
    if any(remaining.values()) or not confirmed:
        save_remaining(remaining)
        detail='；'.join(dict.fromkeys(errors))or'服务器尚未确认部分装备/羁绊归还'
        record.update(phase='recovery_required',returnError=detail,fallback=fallback)
        _save(path,record)
        return {**summary,'restored':False,'error':detail,
                'noRoleEquipment':sorted(remaining['equipment']&rejected['equipment']),
                'noRoleArtifacts':sorted(remaining['artifacts']&rejected['artifacts']),
                'unresolvedEquipment':sorted(remaining['equipment']),
                'unresolvedArtifacts':sorted(remaining['artifacts'])}
    if any(fallback.values()) or diagnostics or errors:_archive(path,audit,'return_verified')
    else:_remove(path)
    return {**summary,'restored':True}


def _combine_records(records):
    """Keep the oldest destination when active and deferred loans overlap."""
    combined={'schema':1,'phase':'restoring','equipment':{},'artifacts':{},'roles':[],
              'ownerRoles':{},'warehouseDestinations':{'equipment':{},'artifacts':{}},'ownerDiagnostics':{}}
    for value in records:
        for field in ('equipment','artifacts'):
            desired=(value.get('originalDestinations')or{}).get(field)or value[field]
            for aid,owner in desired.items():
                if aid not in combined[field]:
                    combined[field][aid]=owner
                    reason=(value.get('warehouseDestinations')or{}).get(field,{}).get(aid)
                    if reason:combined['warehouseDestinations'][field][aid]=reason
        for rid,sid in (value.get('ownerRoles')or{}).items():combined['ownerRoles'].setdefault(rid,sid)
        combined['ownerDiagnostics'].update(value.get('ownerDiagnostics')or{})
        combined['roles']=list(dict.fromkeys([*combined['roles'],*value.get('roles',[])]))
    return combined


def isolate_unreturned(client,account_key,result,*,retry_deferred=True):
    """Move a server-confirmed unreturned remainder into the isolated journal.

    The caller has already synced and attempted every asset. This writes the
    durable deferred entry and archives the attempt without resending anything,
    so a hero instance the server no longer recognizes cannot block the run.
    """
    if result.get('restored') or not result.get('stateConfirmed'):return result
    remaining={'equipment':set(result.get('unresolvedEquipment')or[]),
               'artifacts':set(result.get('unresolvedArtifacts')or[])}
    if not any(remaining.values()):return result
    target=_path(client,account_key)
    value=_load(target)
    deferred=deepcopy(value)
    deferred['phase']='deferred_return'
    for field in remaining:
        deferred[field]={aid:value[field][aid] for aid in remaining[field]}
        deferred['originalDestinations'][field]={aid:value['originalDestinations'][field][aid] for aid in remaining[field]}
        deferred['warehouseDestinations'][field]={aid:reason for aid,reason in value['warehouseDestinations'][field].items() if aid in remaining[field]}
    # Save the unresolved original owners before removing the active gate;
    # retain the full recovery attempt in the audit history too.
    deferred_path=_deferred_path(client,account_key)
    if not retry_deferred and _exists(deferred_path):
        # Keep previous quarantined owners when a different asset is rejected
        # at the final return. Never replace the old file with a journal that
        # contains only the newly rejected item.
        previous=_load(deferred_path)
        if previous.get('schema')!=1 or any(not isinstance(previous.get(field),dict) for field in remaining):
            raise GameError('历史借装隔离记录异常，已保留本轮恢复记录')
        merged=_combine_records([previous,deferred])
        merged['originalDestinations']={field:deepcopy(merged[field]) for field in remaining}
        merged.update(accountKey=str(account_key),phase='deferred_return',returnError=deferred.get('returnError'))
        deferred=merged
    _save(deferred_path,deferred)
    _archive(target,value,'deferred_return')
    result={**result,'restored':True,'fullyRestored':False,'deferred':{field:sorted(ids) for field,ids in remaining.items()},
            'warning':'部分借装未能归还，已跳过并保留原始归属，本次使用其他资产继续'}
    log.warning('[asset-recovery] %s isolated rejected loans: equipment=%s artifacts=%s; original owners retained',
                target.stem,result['deferred']['equipment'],result['deferred']['artifacts'])
    return result


def restore_many(client, account_keys, *, defer_no_role=False, retry_deferred=True, defer_unreturned=False):
    """Flatten nested loans to the oldest original owner before any return.

    A passenger may borrow an item displaced by its carrier's loadout. Restoring
    the parent after a failed child and later replaying that child would return
    it to an intermediate owner. One durable final baseline prevents that.
    """
    scopes=list(dict.fromkeys(account_keys));records=[];invalid=[];invalid_paths=set()
    # Automatic final cleanup returns this run's loans. A confirmed rejection
    # isolated before farming must not be replayed during every later cleanup.
    # A new task start or explicit restore can still retry those old entries.
    sources=[(key,_deferred_path(client,key)) for key in scopes] if (defer_no_role or defer_unreturned) and retry_deferred else []
    sources.extend((key,_path(client,key)) for key in scopes)
    for key,path in sources:
        if not _exists(path):continue
        try:
            value=_load(path)
            if value.get('schema')!=1 or not isinstance(value.get('equipment'),dict) or not isinstance(value.get('artifacts'),dict):
                raise ValueError('恢复记录格式异常')
            records.append((key,path,value))
        except (OSError,ValueError,TypeError) as error:
            invalid.append(str(error));invalid_paths.add(path);_archive_raw(path,'invalid_journal')
    if not records:
        return {'restored':True,'changed':False,**({'closed':True,'warning':'；'.join(invalid)} if invalid else {})}
    target_key=scopes[0] if _path(client,scopes[0]) not in invalid_paths else records[0][0]
    target=_path(client,target_key)
    combined=_combine_records(value for _,_,value in records)
    combined['accountKey']=str(target_key)
    _save(target,combined)
    # The combined journal is durable before obsolete intermediate baselines go.
    for _,path,_ in records:
        if path!=target:_remove(path)
    result=restore(client,account_key=target_key)
    if (defer_no_role or defer_unreturned) and not result.get('restored'):
        remaining={'equipment':set(result.get('unresolvedEquipment')or[]),
                   'artifacts':set(result.get('unresolvedArtifacts')or[])}
        rejected={'equipment':set(result.get('noRoleEquipment')or[]),
                  'artifacts':set(result.get('noRoleArtifacts')or[])}
        if any(remaining.values()) and (defer_unreturned or all(remaining[field]<=rejected[field] for field in remaining)):
            result=isolate_unreturned(client,target_key,result,retry_deferred=retry_deferred)
    if invalid:result['warning']='；'.join([result.get('warning',''),*invalid]).strip('；')
    return result
