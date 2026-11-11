"""Current summon pools and explicit, cancellable single-draw jobs."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import random
import re
import threading
import time
import uuid

from . import config, runtime_data as resources, game_clock
from .game_client import GameError, GameMessage, TaskDeferred
from .game_data import activity_open
from .logutil import log


def build_catalog(commodities, activities, drops, names, ranks, loc, catalog=""):
    activities = {row['ID']: row for row in activities}
    drops_by_id = {}
    for row in drops:
        drops_by_id.setdefault(row['DropID'], set()).add(row.get('TagString', ''))
    pools = {}
    for row in commodities:
        aid = row.get('ActivityID', '')
        activity = activities.get(aid, {})
        roles = list(filter(None, row.get('GuaranteedRoleIDs', '').split('~')))
        # Selective, combination and Mystic target selection have separate rules.
        if row.get('Store') != 'Summon' or not (
            row['ID'] == 'NormalSummon' or activity.get('Type') == 'SummonPickUp' and len(roles) == 1
        ):
            continue
        if any(row.get(key) for key in ('Condition', 'Cost', 'Discount', 'VersionControl')):
            continue
        if row.get('OutList') in ('1', 'true', 'True'):
            continue
        prices = []
        for suffix in ('', '2'):
            sid, amount = row.get('PriceItemID' + suffix), row.get('Price' + suffix)
            if sid and amount:
                amount = int(amount)
                if amount <= 0:
                    raise ValueError('抽卡价格无效')
                prices.append({'item': sid, 'name': names.get(sid, sid), 'count': amount})
        if not prices:
            continue
        artifact = activity.get('SummonArtifactID', '')
        if artifact and artifact not in drops_by_id.get(row.get('Drops'), set()):
            raise ValueError(f'卡池 {row["ID"]} 的 UP 羁绊配置不一致')
        pools[row['ID']] = {
            'id': row['ID'], 'name': loc.get(row.get('Name'), row['ID']),
            'activities': list(dict.fromkeys(filter(None, [aid, *row.get('ActivityIDs', '').split('~')]))),
            'prices': prices, 'freeCount': int(row.get('FreeCount') or 0),
            'guarantee': int(row.get('GuaranteedCount') or 0),
            'roles': [{'id': sid, 'name': names.get(sid, sid), 'star': int(ranks.get(sid, 4)) + 1,
                       'image': f'/assets/avatars/{sid}.png'} for sid in roles],
            'artifacts': [{'id': artifact, 'name': names.get(artifact, artifact),
                           'star': int(ranks.get(artifact, 0)) + 1,
                           'image': f'/assets/gacha/artifacts/{artifact}.webp'}] if artifact else [],
        }
    used = {aid for pool in pools.values() for aid in pool['activities']}
    return {'schema': 1, 'catalog': catalog, 'pools': pools,
            'activities': {aid: {'IsUnlimitTime': activities.get(aid, {}).get('IsUnlimitTime', '')}
                           for aid in used}}


@resources.cached
def catalog():
    value = resources.read_json('assets/game_config.json', {}).get('gacha')
    if value is None:
        # An older compiler can have the same official data but omit this feature.
        root = Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[1])
        bundled = json.loads((root / 'assets/game_config.json').read_text(encoding='utf-8')).get('gacha', {})
        current = resources.snapshot().manifest.get('catalog', '')
        if not current or current == bundled.get('catalog'):
            value = bundled
    return value if isinstance(value, dict) and value.get('schema') == 1 else {}


def balance(state, sid):
    return next((max(0, int(row.get('Count') or 0)) for row in
                 (state.get('ItemContainer') or {}).get('Items', []) if row.get('StaticID') == sid), 0)


def contract_prices(pool, prefer_exclusive=True):
    """Optionally keep pool-specific contracts ahead of the ordinary contract."""
    if not prefer_exclusive:
        return [price for price in pool['prices'] if price['item'] == '5']
    return sorted(pool['prices'], key=lambda price: price['item'] == '5')


def record(state, pool_id):
    return next((row for row in (state.get('StoreRecordContainer') or {}).get('Records', [])
                 if row.get('Store') == 'Summon' and row.get('StaticID') == pool_id), None)


def pool_open(pool, state, now=None):
    return all(activity_open(aid, state, now=time.time() if now is None else now,
                             tables={'Activity': catalog().get('activities', {})})
               for aid in pool['activities'])


def options(state, *, now=None):
    result = []
    settings = {row.get('ActivityID'): row for row in
                (state.get('ActivityDBSettingContainer') or {}).get('SettingList', [])}
    for definition in catalog().get('pools', {}).values():
        if not pool_open(definition, state, now=now):
            continue
        row = deepcopy(definition)
        saved = record(state, row['id']) or {}
        row['prices'] = [{**price, 'balance': balance(state, price['item'])} for price in contract_prices(row)]
        row['freeRemaining'] = max(0, row['freeCount'] - int(saved.get('FreeBuyCount') or 0))
        row['guaranteedCount'] = int(saved.get('GuaranteedCount') or 0)
        row['order'] = min((int(settings.get(aid, {}).get('Order') or 0) for aid in row['activities']), default=999)
        result.append(row)
    return sorted(result, key=lambda row: (row['order'], row['id']))


def normalize(value):
    if not isinstance(value, dict):
        raise GameError('抽卡设置无效')
    count = value.get('count')
    if isinstance(count, bool) or not re.fullmatch(r'[1-9][0-9]{0,3}', str(count)):
        raise GameError('抽取次数须为 1–9999 的整数')
    pool_id = value.get('poolId')
    pool = catalog().get('pools', {}).get(pool_id) if isinstance(pool_id, str) else None
    if not pool or not pool['prices']:
        raise GameError('请选择有效的卡池')
    role, artifact = value.get('stopRole') is True, value.get('stopArtifact') is True
    if role and not pool['roles'] or artifact and not pool['artifacts']:
        raise GameError('该卡池没有所选的 UP 目标')
    return {'poolId': pool_id, 'count': int(count),
            'stopRole': role, 'stopArtifact': artifact,
            'preferExclusive': value.get('preferExclusive', True) is True}


class Unconfirmed(GameError):
    pass


def results(response):
    """VirtualDrop can contain the only copy of a successfully summoned hero."""
    output, seen = [], set()
    for field in ('Drop', 'VirtualDrop'):
        value = response.get(field) or {}
        if not isinstance(value, dict) or not isinstance(value.get('Items', []), list):
            raise Unconfirmed('抽卡结果格式异常')
        for entry in value.get('Items', []):
            if not isinstance(entry, dict):
                raise Unconfirmed('抽卡结果格式异常')
            for key, kind in (('RoleData', 'role'), ('Artifact', 'artifact')):
                obj = entry.get(key)
                if not isinstance(obj, dict):
                    continue
                sid = obj.get('StaticID')
                if not isinstance(sid, str) or not sid:
                    raise Unconfirmed('抽卡结果缺少编号')
                oid = obj.get('_id')
                oid = oid.get('$oid') if isinstance(oid, dict) else oid
                identity = (kind, str(oid)) if oid else (kind, json.dumps(obj, sort_keys=True))
                if identity in seen:
                    continue
                seen.add(identity)
                names = resources.read_json('backend/item_names.json', {})
                ranks = resources.read_json('backend/item_rank.json', {})
                output.append({'kind': kind, 'id': sid, 'name': names.get(sid, sid),
                               'star': int(obj.get('Star') or int(ranks.get(sid, 0)) + 1),
                               'virtual': field == 'VirtualDrop'})
    return output


def settle(response, pool, currency, previous, free):
    if not isinstance(response, dict):
        raise Unconfirmed('未收到有效的抽卡响应')
    found = results(response)
    saved = response.get('CommodityRecord') or {}
    if len(found) != 1 or not isinstance(saved, dict) or saved.get('StaticID') != pool['id'] or saved.get('Store') != 'Summon':
        raise Unconfirmed('抽卡结果或卡池记录不完整')
    key = 'FreeBuyCount' if free else 'BuyCount'
    if saved.get(key) != int(previous.get(key) or 0) + 1:
        raise Unconfirmed('抽卡计数未能确认')
    costs = response.get('CostItems') or []
    if not isinstance(costs, list):
        raise Unconfirmed('抽卡消耗未能确认')
    spent = {}
    for entry in costs:
        item = entry.get('Item') if isinstance(entry, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('Count'), int) or isinstance(item['Count'], bool):
            raise Unconfirmed('抽卡消耗未能确认')
        sid = item.get('StaticID')
        spent[sid] = spent.get(sid, 0) + item['Count']
    price = next(price['count'] for price in pool['prices'] if price['item'] == currency)
    if spent != ({} if free else {currency: price}):
        raise Unconfirmed('实际抽卡消耗与所选契约不一致')
    if not free:
        now_item = next((entry.get('NowItem') for entry in costs
                         if (entry.get('NowItem') or {}).get('StaticID') == currency), None)
        if not now_item or not isinstance(now_item.get('Count'), int) or now_item['Count'] < 0:
            raise Unconfirmed('抽后契约余额未能确认')
    return found[0], 0 if free else price


class JobManager:
    def __init__(self, account_lookup=None):
        self._lookup = account_lookup
        self._lock = threading.RLock()
        self._jobs, self._latest, self._events, self._uncertain = {}, {}, {}, {}
        self._sequence = 0

    def _current(self, acc):
        if self._lookup:
            return self._lookup(acc.email) is acc
        from . import accounts
        return accounts.get(acc.email) is acc

    def blocked(self, acc):
        with self._lock:
            return self._uncertain.get(acc.email) == (acc, acc.state_revision)

    def status(self, acc):
        with self._lock:
            job = self._jobs.get((acc.email, self._latest.get(acc.email)))
            if job is None or job['_account'] is not acc:
                return {'state': 'idle', 'running': False, 'completed': 0, 'requested': 0,
                        'message': '等待开始', 'results': [], 'blocked': self.blocked(acc)}
            value = {**deepcopy({k: v for k, v in job.items() if not k.startswith('_') and k != 'results'}),
                     'results': deepcopy(job['results'][-10:]), 'blocked': self.blocked(acc)}
            value['balances'] = {price['item']: balance(acc.client.account_state or {}, price['item'])
                                 for price in job['_pool']['prices']}
            saved = record(acc.client.account_state or {}, job['params']['poolId']) or {}
            value['guaranteedCount'] = int(saved.get('GuaranteedCount') or 0)
            if value['state'] == 'uncertain' and not value['blocked']:
                value.update(state='stopped', message='登录同步已完成，可重新设置抽数后开始')
            return value

    def start(self, acc, value, request_id):
        if not isinstance(request_id, str):
            raise GameError('缺少抽卡请求标识，请重新打开页面')
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            raise GameError('抽卡请求标识无效') from None
        with self._lock:
            prior = self._jobs.get((acc.email, request_id))
            if prior:
                if prior['_account'] is not acc:
                    raise GameError('登录会话已变更，请重新开始')
                if self._latest.get(acc.email) == request_id:
                    return self.status(acc)
                return {**deepcopy({k: v for k, v in prior.items() if not k.startswith('_')}),
                        'blocked': self.blocked(acc)}
            if self.status(acc)['running']:
                raise GameError('当前账号已有抽卡任务正在运行')
            if not self._current(acc) or not acc.client.is_connected():
                raise GameError('账号已断联，请先重新登录')
            if self.blocked(acc):
                raise GameError('上次抽卡结果未确认，等待登录同步刷新')
            params = normalize(value)
            pool = catalog()['pools'][params['poolId']]
            if not pool_open(pool, acc.client.account_state or {},
                             now=game_clock.now_ms(acc.client) / 1000):
                raise GameError('该卡池尚未开放或已经结束，请重新选择')
            event = threading.Event()
            from .maintenance import service as maintenance_service
            maintenance_service.track_event(event)
            self._sequence += 1
            job = {'id': request_id, 'sequence': self._sequence, 'revision': 1,
                   'state': 'running', 'running': True, 'completed': 0,
                   'requested': params['count'], 'params': params, 'poolName': pool['name'],
                   'spent': 0, 'results': [], 'message': '等待账号空闲',
                   '_account': acc, '_revision': acc.state_revision, '_pool': deepcopy(pool)}
            self._jobs[(acc.email, request_id)] = job
            self._latest[acc.email] = request_id
            self._events[(acc.email, request_id)] = event
            try:
                threading.Thread(target=self._run, args=(acc, job, event),
                                 name=f'gacha-{request_id[:8]}', daemon=True).start()
            except RuntimeError:
                job.update(state='failed', running=False, message='任务未能启动，请重试')
                self._events.pop((acc.email, request_id), None)
                raise GameError(job['message']) from None
            return self.status(acc)

    def cancel(self, acc, request_id=None):
        with self._lock:
            request_id = request_id or self._latest.get(acc.email)
            job = self._jobs.get((acc.email, request_id))
            event = self._events.get((acc.email, request_id))
            if job and job['_account'] is acc and event and job['running']:
                event.set()
                job.update(state='stopping', message='正在停止，等待当前抽取结果')
                job['revision'] += 1
            return self.status(acc)

    def _check(self, acc, job, event):
        from .updater import service as update_service
        if event.is_set():
            raise GameError('已停止抽卡')
        if not self._current(acc) or acc._stop.is_set() or acc.state_revision != job['_revision']:
            raise GameError('账号会话已变化，抽卡已停止')
        if update_service.is_installing():
            raise GameError('正在更新软件，抽卡已停止')
        if not acc.client.is_connected():
            raise GameError('账号已断联，抽卡已停止')

    def _finish(self, acc, job, message, state='done'):
        with self._lock:
            job.update(state=state, running=False, message=message)
            job['revision'] += 1
        acc.push_log({'id': 'gacha', 'name': '自动抽卡', 'ok': state != 'failed' and state != 'uncertain',
                      'detail': f'{message}（已抽 {job["completed"]}/{job["requested"]}）'}, '')

    def _run(self, acc, job, event):
        from .tasks import _buy_payload, _synth_record
        params = job['params']
        pending = False
        try:
            while job['completed'] < job['requested']:
                self._check(acc, job, event)
                if not acc.lock.acquire(timeout=.2):
                    continue
                try:
                    with resources.pin():
                        self._check(acc, job, event)
                        pool = catalog().get('pools', {}).get(params['poolId'])
                        if not pool or not pool_open(pool, acc.client.account_state or {},
                                                     now=game_clock.now_ms(acc.client) / 1000):
                            raise GameError('卡池已结束或资料已变化，抽卡已停止')
                        if pool != job['_pool']:
                            raise GameError('卡池资料已变化，抽卡已停止，请重新确认设置')
                        state = acc.client.account_state or {}
                        saved = deepcopy(record(state, pool['id']) or _synth_record(pool['id'], 'Summon'))
                        free = int(saved.get('FreeBuyCount') or 0) < pool['freeCount']
                        price = contract_prices(pool)[0] if free else next(
                            (p for p in contract_prices(pool, params['preferExclusive'])
                             if balance(state, p['item']) >= p['count']), None)
                        if price is None:
                            self._finish(acc, job, '契约不足，抽卡已停止')
                            return
                        currency = price['item']
                        with self._lock:
                            job['message'] = f'正在抽取第 {job["completed"] + 1} 次'
                            job['revision'] += 1
                        payload = _buy_payload(acc.client, saved, 1,
                                               {'SelcetCostItemID': '' if free else currency})
                        pending = True
                        response = acc.client.call('StoreHandler.BuyCommodity', payload)
                        result, spent = settle(response, pool, currency, saved, free)
                        pending = False
                        # Keep confirmed balances and records even for lightweight clients.
                        acc.client._apply_now_items(response)
                        acc.client._apply_commodity_record(response)
                        with self._lock:
                            job['completed'] += 1
                            job['revision'] += 1
                            job['spent'] += spent
                            job['guaranteedCount'] = int(response['CommodityRecord'].get('GuaranteedCount') or 0)
                            job['freeRemaining'] = max(0, pool['freeCount'] - int(response['CommodityRecord'].get('FreeBuyCount') or 0))
                            result.update(number=job['completed'], cost=spent, currency='' if free else currency)
                            job['results'].append(result)
                            del job['results'][:-100]
                        label = '角色' if result['kind'] == 'role' else '羁绊'
                        line = f'第 {job["completed"]} 抽：{result["star"]}★ {label}·{result["name"]}'
                        line += f'，消耗 {spent} {price["name"]}' if spent else '（免费）'
                        acc.push_log({'id': 'gacha', 'name': '自动抽卡', 'ok': True, 'detail': line}, '')
                        role_hit = params['stopRole'] and result['kind'] == 'role' and result['id'] in [r['id'] for r in pool['roles']]
                        artifact_hit = params['stopArtifact'] and result['kind'] == 'artifact' and result['id'] in [r['id'] for r in pool['artifacts']]
                        if role_hit or artifact_hit:
                            self._finish(acc, job, f'已抽到 UP {label}·{result["name"]}')
                            return
                finally:
                    acc.lock.release()
                if job['completed'] < job['requested']:
                    delay = config.get_request_delay_config()
                    seconds = delay.get('ms', 0) / 1000 if delay.get('enabled') else 0
                    seconds *= random.uniform(1 - delay.get('jitter', 0) / 100, 1 + delay.get('jitter', 0) / 100)
                    event.wait(max(.1, seconds))
            self._finish(acc, job, '已达到设定抽数')
        except (GameMessage, TaskDeferred) as error:
            # Explicit server refusals and pre-request scheduling deferrals are not retried.
            from .tasks import _scene_failure_detail
            detail = (_scene_failure_detail(error.msg, '抽卡').replace('挑战失败：', '抽卡失败：', 1)
                      if isinstance(error, GameMessage) else str(error))
            self._finish(acc, job, detail, 'failed')
        except Exception as error:
            if pending:
                log.warning('[gacha] pool=%s result unconfirmed: %s', params['poolId'], error)
                with self._lock:
                    self._uncertain[acc.email] = (acc, job['_revision'])
                self._finish(acc, job, '本次抽卡结果未确认，等待登录同步刷新', 'uncertain')
            else:
                if not isinstance(error, GameError):
                    log.exception('[gacha] job failed before request')
                self._finish(acc, job, str(error) if isinstance(error, GameError) else '抽卡执行失败，已停止',
                             'stopped' if isinstance(error, GameError) else 'failed')
        finally:
            with self._lock:
                self._events.pop((acc.email, job['id']), None)


manager = JobManager()
