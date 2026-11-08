"""Account-scoped summon history and confirmed secret/star-source shop ledger.

Protocol evidence: SummonLogManager.QuerySummonLogs uses CommodityStaticID;
QueryMysticSummonLogs has a shared history. Both return SummonLogResult.Logs
({_id, StaticID, Time}). CommodityRecord.GuaranteedCount is the server counter.
No purchase or shop-reset requests are made by the page's refresh operation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import time

from . import config, resource_store
from .game_client import DuplicateLoginError, GameError
from .game_data import activity_open

TZ = timezone(timedelta(hours=8))


def catalog():
    data = json.loads(resource_store.path('assets/recruitment_catalog.json').read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not data.get('pools') or not data.get('results'):
        raise GameError('招募资料缺失，请更新游戏资料后重试')
    # Existing installs may have a previously compiled resource generation whose
    # artifact entries predate bundled icons. Reuse its rules with the new art.
    for sid,result in data['results'].items():
        if result.get('kind')=='artifact' and not result.get('icon') and resource_store.bundled(f'assets/items/{sid}.png').is_file():
            result['icon']=f'/assets/items/{sid}.png'
    return data


def milliseconds(value):
    if isinstance(value, dict):
        value = value.get('$date')
    if isinstance(value, str) and not value.lstrip('-').isdigit():
        try:
            try:stamp=datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:stamp=datetime.strptime(value,'%Y/%m/%d %H:%M:%S')
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            return int(stamp.timestamp() * 1000)
        except ValueError:
            return 0
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def account_key(client):
    aid = getattr(client, 'aid', None) or (client.account_state or {}).get('Info', {}).get('_id')
    if isinstance(aid, dict):
        aid = aid.get('$oid')
    if not aid:
        raise GameError('账号标识缺失，请重新登录')
    return hashlib.sha256(str(aid).encode()).hexdigest()


def database():
    path = config.data_dir() / 'records' / 'recruitment.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS summon (
              account TEXT NOT NULL, pool TEXT NOT NULL, id TEXT NOT NULL,
              sid TEXT NOT NULL, time INTEGER NOT NULL,
              PRIMARY KEY(account,pool,id));
            CREATE TABLE IF NOT EXISTS sync (
              account TEXT NOT NULL, pool TEXT NOT NULL, first INTEGER NOT NULL,
              updated INTEGER NOT NULL, PRIMARY KEY(account,pool));
            CREATE TABLE IF NOT EXISTS shop (
              account TEXT NOT NULL, id TEXT NOT NULL, time INTEGER NOT NULL,
              paid INTEGER NOT NULL, recruit INTEGER NOT NULL, mystery INTEGER NOT NULL,
              recruit_hits INTEGER NOT NULL, mystery_hits INTEGER NOT NULL,
              PRIMARY KEY(account,id));
            CREATE TABLE IF NOT EXISTS shop_actions (
              account TEXT NOT NULL, id TEXT NOT NULL, time INTEGER NOT NULL,
              kind TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(account,id));
            CREATE INDEX IF NOT EXISTS shop_actions_account_time ON shop_actions(account,time);
            CREATE TABLE IF NOT EXISTS record_migrations (name TEXT PRIMARY KEY);
        ''')
        # The first shop ledger interpreted native Utc (Unix seconds) as ms.
        # Repair only purchase timestamps in that impossible 1970-era range;
        # refresh dates and all quantities, costs and deduplication IDs survive.
        migration='shop-purchase-utc-seconds-v1'
        if not db.execute('SELECT 1 FROM record_migrations WHERE name=?',(migration,)).fetchone():
            with db:
                db.execute("UPDATE shop_actions SET time=time*1000 WHERE kind='purchase' AND time>=946684800 AND time<4102444800")
                db.execute('INSERT OR IGNORE INTO record_migrations VALUES (?)',(migration,))
        return db
    except Exception:
        db.close()
        raise


def merge_logs(key, pool, logs, *, now):
    if not isinstance(logs, list):
        raise GameError('游戏抽卡历史响应缺少 Logs，保留上次记录')
    normalized = []
    for row in logs:
        if not isinstance(row, dict):
            raise GameError('游戏抽卡历史格式无效，保留上次记录')
        identifier = row.get('_id')
        if isinstance(identifier, dict):
            identifier = identifier.get('$oid')
        stamp = milliseconds(row.get('Time'))
        if not identifier or not row.get('StaticID') or stamp <= 0:
            raise GameError('游戏抽卡历史缺少标识或时间，保留上次记录')
        normalized.append((key, pool, str(identifier), str(row['StaticID']), stamp))
    db = database()
    try:
        with db:
            db.executemany('INSERT OR IGNORE INTO summon VALUES (?,?,?,?,?)', normalized)
            db.execute('''INSERT INTO sync VALUES (?,?,?,?) ON CONFLICT(account,pool)
                          DO UPDATE SET updated=excluded.updated''', (key, pool, now, now))
    finally:
        db.close()


def read_logs(key, pool):
    db = database()
    try:
        logs = [{'id': identifier, 'sid': sid, 'time': stamp} for identifier, sid, stamp in
                db.execute('SELECT id,sid,time FROM summon WHERE account=? AND pool=? ORDER BY time,id', (key, pool))]
        sync = db.execute('SELECT first,updated FROM sync WHERE account=? AND pool=?', (key, pool)).fetchone()
        return logs, {'firstSyncAt': sync[0], 'updatedAt': sync[1]} if sync else {}
    finally:
        db.close()


def observe_shop(client, request, response):
    """Called only after a successful ResetRandomStore response, before auto-buy."""
    if request.get('StoreID') != 'SecretShop':
        return
    records = [r for r in response.get('Records', []) if r.get('Store') == 'SecretShop']
    if not records:
        raise GameError('商店刷新响应缺少货架，无法保存本轮记录')
    stamp = max(milliseconds(r.get('LastResetTime')) for r in records)
    if stamp <= 0:
        raise GameError('商店刷新响应缺少时间，无法保存本轮记录')
    # Server shelf IDs + reset time identify the round, even when a response is replayed.
    identity = json.dumps([stamp, sorted(str(r.get('_id')) for r in records)], sort_keys=True)
    identifier = hashlib.sha256(identity.encode()).hexdigest()
    counts, hits = defaultdict(int), defaultdict(int)
    for record in records:
        for drop in (record.get('DropResult') or {}).get('Items') or []:
            item = drop.get('Item') or {}
            sid = str(item.get('StaticID') or '')
            if sid in ('5', '6'):
                counts[sid] += int(item.get('Count', drop.get('Count', 0)))
                hits[sid] += 1
    db = database()
    try:
        with db:
            db.execute('INSERT OR IGNORE INTO shop VALUES (?,?,?,?,?,?,?,?)',
                       (account_key(client), identifier, stamp, int(bool(request.get('IsUseGold'))),
                        counts['5'], counts['6'], hits['5'], hits['6']))
            from .shop_records import record_refresh_cost
            record_refresh_cost(db,client,identifier,stamp,request,response)
    finally:
        db.close()


def observe_shop_purchase(client,request,response):
    from .shop_records import observe_purchase
    return observe_purchase(client,request,response)


def shop_summary(key, data):
    from .shop_records import summary
    from .star_source_records import summary as star_summary
    result=summary(key,data)
    result['starSource']=star_summary(key)
    return result


def statistics(pool, logs, record, data, sync):
    definitions = data['results']
    counts = defaultdict(int)
    fives = []
    for index, row in enumerate(logs):
        definition = definitions.get(row['sid'], {})
        star = definition.get('star')
        if not definition:
            counts['unknown'] += 1
        if star == 4:
            counts['four'] += 1
        if star != 5:
            continue
        counts['five'] += 1
        is_ml = definition.get('kind') == 'role' and definition.get('element') in ('Light', 'Dark')
        kind = ('lightDark' if is_ml else 'rgb') if definition.get('kind') == 'role' else 'artifact'
        counts[kind] += 1
        if row['sid'] in pool['upRoles']:
            counts['upRole'] += 1
        elif definition.get('kind') == 'role':
            counts['otherRole'] += 1
        if definition.get('kind') == 'artifact':
            counts['upArtifact' if row['sid'] in pool['upArtifacts'] else 'otherArtifact'] += 1
        fives.append({**row, **definition, 'lightDark': is_ml, 'position': index + 1})
    limit = pool['pityLimit']
    current = record.get('GuaranteedCount') if record else None
    # A missing server counter is unknown, never inferred from truncated history.
    pity = {'limit': limit, 'current': current, 'remaining': max(0, limit - current) if type(current) is int and limit else None}
    total = len(logs)
    rates = pool.get('rates')
    measured = counts['lightDark'] if pool['kind'] == 'mystic' else counts['five']
    baseline = rates.get('lightDark' if pool['kind'] == 'mystic' else 'five') if rates else None
    luck = '暂无记录' if not total else '资料待同步' if counts['unknown'] else '—' if baseline is None else (
        '欧非均衡' if abs(measured / total - baseline) <= baseline * .2 else '偏欧' if measured / total > baseline else '偏非')
    ml = [r for r in fives if r['lightDark']]
    return {**pool, **sync, 'total': total, 'counts': dict(counts), 'pity': pity, 'luck': luck,
            'fiveRate': counts['five'] / total if total and not counts['unknown'] else None,
            'lightDarkRate': counts['lightDark'] / total if total and not counts['unknown'] else None,
            'recent': list(reversed(fives[-12:])), 'recentLightDark': list(reversed(ml[-12:])),
            'lightDarkIntervals': [{'name': b['name'], 'time': b['time'], 'pulls': b['position'] - a['position']}
                                   for a, b in zip(ml, ml[1:])][-12:][::-1],
            'startAt': logs[0]['time'] if logs else None,
            'coverage': '统计已收录记录，游戏接口保留范围之外的历史无法补齐；保底以当前账号快照为准。'}


def refresh(client):
    """Fetch fresh official logs; isolate per-pool failures and retain previous rows."""
    data, key = catalog(), account_key(client)
    state = client.account_state or {}
    now = int(time.time() * 1000)
    records = {r.get('StaticID'): r for r in state.get('StoreRecordContainer', {}).get('Records', []) if r.get('Store') == 'Summon'}
    settings = {r.get('ActivityID'): r for r in state.get('ActivityDBSettingContainer', {}).get('SettingList', [])}
    db = database()
    try:
        saved = {r[0] for r in db.execute('SELECT pool FROM sync WHERE account=?', (key,))}
    finally:
        db.close()
    candidates = []
    for pool in data['pools'].values():
        sid = pool['id']
        active = sid in ('NormalSummon', 'GalaxySummon')
        if sid in records and not active:
            activities = [pool['activity'], *pool['activities']]
            active = all(activity_open(a, state, now=now / 1000) for a in activities if a)
        if active or sid in saved:
            candidates.append({**pool, 'active': active})
    errors, pools, shared, shared_shown = [], [], None, False
    for pool in sorted(candidates, key=lambda p: (not p['active'], {'normal': 0, 'mystic': 1, 'limited': 2, 'galaxy': 3}[p['kind']], p['id'])):
        sid, error = pool['id'], None
        storage = 'mystic-shared' if pool['kind'] == 'mystic' else sid
        try:
            if not pool['active']:
                pass  # Archived pools use saved history; current pools sync on entry.
            elif pool['kind'] == 'mystic':
                if shared is None:
                    try:
                        response = client.call('SummonLogHandler.QueryMysticSummonLogs', client._auth_data({}))
                        merge_logs(key, storage, response.get('Logs'), now=now)
                        shared = True
                    except DuplicateLoginError:
                        raise
                    except Exception as exc:
                        shared = str(exc)
                if shared is not True:
                    raise GameError(shared)
            else:
                response = client.call('SummonLogHandler.QuerySummonLogs', client._auth_data({'CommodityStaticID': sid}))
                merge_logs(key, storage, response.get('Logs'), now=now)
        except DuplicateLoginError:
            raise
        except Exception as exc:
            error = str(exc)
            errors.append({'pool': pool['name'], 'message': error})
        logs, sync = read_logs(key, storage)
        if pool['kind'] == 'mystic':
            setting = settings.get(pool['activity']) or {}
            start, end = milliseconds(setting.get('StartTime')), milliseconds(setting.get('EndTime'))
            if start > 0 and end > start:
                logs = [r for r in logs if start <= r['time'] <= end]
                pool['periodStart'], pool['periodEnd'] = start, end
                if not error and pool['active']:
                    # Keep this confirmed banner slice discoverable after rotation.
                    merge_logs(key, sid, [dict(_id=row['id'], StaticID=row['sid'], Time=row['time']) for row in logs], now=now)
            elif not pool['active']:
                logs, sync = read_logs(key, sid)
            else:
                if shared_shown:
                    continue
                shared_shown = True
                pool['name'] = '神秘招募 · 共享历史'
                pool['upRoles'], pool['upArtifacts'], pool['rates'] = [], [], None
        else:
            setting = settings.get(pool['activity']) or {}
            pool['periodStart'] = milliseconds(setting.get('StartTime')) or None
            pool['periodEnd'] = milliseconds(setting.get('EndTime')) or None
            if pool['kind'] == 'limited' and pool['periodStart'] and pool['periodEnd'] and pool['periodEnd'] > pool['periodStart']:
                logs = [r for r in logs if pool['periodStart'] <= r['time'] <= pool['periodEnd']]
        pools.append({**statistics(pool, logs, records.get(sid), data, sync), 'error': error})
    return {'updatedAt': now if not errors else None, 'attemptedAt': now, 'version': data['version'],
            'pools': pools, 'errors': errors, 'shop': shop_summary(key, data),
            'recordingError': getattr(client, '_recruitment_record_error', None)}
