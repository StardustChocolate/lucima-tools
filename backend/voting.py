"""Official vote activities: cached eligibility and confirmed claim/vote settlement."""
from __future__ import annotations

import random
import time

from . import runtime_data as resources
from . import config
from .game_client import GameError, GameMessage, TaskDeferred
from .game_data import activity_open, timestamp
from .game_table_dsl import DSLParser


def build_catalog(votes, quests, loc):
    """Compile the complete candidate list, never the leaderboard's top 50."""
    result = {}
    for row in votes:
        aid, ticket = row['ActivityID'], row['VoteItemID']
        roles = DSLParser(row['VoteJsonString']).parse().get('RoleIDs')
        if not aid or not ticket or not isinstance(roles, list) or not roles or not all(isinstance(x, str) and x for x in roles):
            raise ValueError('投票活动配置无效')
        if aid in result:
            raise ValueError('投票活动配置重复')
        entries = []
        for quest in quests:
            if aid not in [quest.get('ActivityID'), *str(quest.get('ActivityIDs') or '').split('~')]:
                continue
            if quest.get('Group') != 'VoteDay' or quest.get('ResetType') != 'Day':
                raise ValueError(f'投票任务 {quest["ID"]} 的规则不受支持')
            target = int(quest.get('ConditionCount') or 1)
            if target <= 0:
                raise ValueError('投票任务目标无效')
            entries.append({'id': quest['ID'], 'name': loc.get(quest['Name'], quest['ID']),
                            'target': target})
        if not entries:
            raise ValueError(f'投票活动 {aid} 缺少任务配置')
        result[aid] = {'id': aid, 'name': loc.get('UI_Vote_Title_' + aid, '团员人气投票'),
                       'ticket': ticket, 'roles': list(dict.fromkeys(roles)), 'quests': entries}
    return {'schema': 1, 'activities': result}


@resources.cached
def catalog():
    value = resources.read_json('assets/game_config.json', {}).get('voting') or {}
    return value.get('activities', {}) if value.get('schema') == 1 else {}


def options(value):
    value = value if isinstance(value, dict) else {}
    targets = value.get('targets')
    return {'targets': {aid: role for aid, role in targets.items()
                        if isinstance(aid, str) and isinstance(role, str)}
            if isinstance(targets, dict) else {}}


def _balance(state, sid):
    rows = (state.get('ItemContainer') or {}).get('Items') or []
    return next((max(0, int(row.get('Count') or 0)) for row in rows
                 if isinstance(row, dict) and row.get('StaticID') == sid), 0)


def status(state, params=None, runtime=None, *, now_ms=None, candidates=False):
    state, runtime = state or {}, runtime or {}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    config, targets = catalog(), options(params)['targets']
    names = resources.read_json('backend/item_names.json', {})
    elements = resources.read_json('assets/role_elements.json', {}) if candidates else {}
    jobs = resources.read_json('assets/role_jobs.json', {}) if candidates else {}
    quests = {row.get('StaticID'): row for row in (state.get('QuestContainer') or {}).get('Quests') or []
              if isinstance(row, dict)}
    settings = {row.get('ActivityID'): row for row in
                (state.get('ActivityDBSettingContainer') or {}).get('SettingList') or [] if isinstance(row, dict)}
    activities, upcoming = [], []
    for aid, definition in config.items():
        setting = settings.get(aid)
        if not setting:
            continue
        if not activity_open(aid, state, now=now_ms / 1000):
            start = int(timestamp(setting.get('StartTime')) * 1000)
            if start > now_ms:
                upcoming.append(start)
            continue
        if 'VoteActivity' in settings and not activity_open('VoteActivity', state, now=now_ms / 1000):
            continue
        target = targets.get(aid, '')
        valid = target in definition['roles']
        waiting = aid in (runtime.get('waitingForLogin') or {})
        rows = []
        for quest in definition['quests']:
            saved = quests.get(quest['id'], {})
            claimed = saved.get('IsRewarded') is True
            ready = saved.get('IsFinish') is True and saved.get('IsRewarded') is False and not waiting
            rows.append({**quest, 'count': saved.get('Count'), 'claimed': claimed, 'ready': ready})
        tickets = _balance(state, definition['ticket'])
        activity = {'id': aid, 'name': definition['name'], 'ticket': definition['ticket'], 'tickets': tickets,
                    'endMs': int(timestamp(setting.get('EndTime')) * 1000),
                    'target': target, 'targetName': names.get(target, target), 'targetValid': valid,
                    'waiting': waiting, 'quests': rows,
                    'ready': not waiting and (any(row['ready'] for row in rows) or valid and tickets > 0)}
        if candidates:
            activity['candidates'] = [{'id': sid, 'name': names.get(sid, sid),
                                       'element': elements.get(sid, ''), 'job': jobs.get(sid, '')}
                                      for sid in definition['roles']]
        activities.append(activity)
    if not config:
        note = '投票配置待更新'
    elif not activities:
        note = '暂无进行中的投票活动'
    elif any(row['waiting'] for row in activities):
        note = '等待登录同步刷新'
    elif any(row['target'] and not row['targetValid'] for row in activities):
        note = '投票目标已失效，请重新选择'
    elif any(not row['target'] for row in activities):
        note = '暂不投票'
    elif any(row['ready'] for row in activities):
        note = '有奖励或选票待处理'
    else:
        note = '等待任务进度或选票更新'
    return {'activities': activities, 'ready': any(row['ready'] for row in activities),
            'nextMs': min(upcoming) if upcoming else None, 'note': note,
            'dataVersion': resources.snapshot().version}


def task(client, params=None, runtime=None, on_partial=None):
    from .tasks import _collect_items, _fmt_items
    runtime = runtime if runtime is not None else {}
    lines, errors = [], []
    sent = False

    def prepare(aid):
        nonlocal sent
        if sent:
            delay = config.get_request_delay_config()
            seconds = (delay['ms'] / 1000 * random.uniform(1 - delay['jitter'] / 100, 1 + delay['jitter'] / 100)
                       if delay['enabled'] else 0)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                snapshot(aid)
                time.sleep(max(0, min(.2, deadline - time.monotonic())))
        if snapshot(aid) is None:
            raise TaskDeferred('投票活动已结束')
        sent = True

    def emit(line):
        lines.append(line)
        if on_partial:
            on_partial({'ok': True, 'detail': line})

    def pending(aid, action):
        runtime.setdefault('waitingForLogin', {})[aid] = action

    def snapshot(aid):
        enabled = getattr(client, 'vote_enabled', None)
        if callable(enabled) and not enabled():
            raise TaskDeferred('活动投票已停止')
        current_params = getattr(client, 'vote_params', None)
        selected = current_params() if callable(current_params) else params
        return next((row for row in status(client.account_state, selected, runtime)['activities'] if row['id'] == aid), None)

    def claim(aid, quest):
        prepare(aid)
        payload = client._auth_data({'RewardQuestInfos': [{'ID': quest['id'], 'Index': 0}], 'CommodityID': ''})
        pending(aid, quest['id'])
        try:
            response = client.call('QuestHandler.RewardQuest', payload)
        except (TaskDeferred, GameMessage):
            runtime['waitingForLogin'].pop(aid, None)
            raise
        results = response.get('Results') or []
        if not any(row.get('StaticID') == quest['id'] and row.get('IsRewarded') is True
                   for result in results for row in result.get('Quests') or []):
            raise GameError('领奖结果未确认')
        gains = {}
        for result in results:
            _collect_items((result.get('Drop') or {}).get('Items'), gains)
        # A confirmed reward with unknown ticket balance cannot authorize spending.
        ticket = catalog()[aid]['ticket']
        if ticket in gains and not any(entry.get('NowItem', {}).get('StaticID') == ticket
                                      and type(entry['NowItem'].get('Count')) is int
                                      for result in results for entry in (result.get('Drop') or {}).get('Items') or []):
            raise GameError('选票余额未确认')
        runtime['waitingForLogin'].pop(aid, None)
        emit(f'领取{quest["name"]}奖励：{_fmt_items(gains)}')

    for initial in status(client.account_state, params, runtime)['activities']:
        aid = initial['id']
        if initial['waiting']:
            continue
        try:
            # One pass before voting, one after: the vote unlocks its daily reward.
            for phase in (0, 1):
                current = snapshot(aid)
                if current is None:
                    break
                for quest in current['quests']:
                    current = snapshot(aid)
                    if current is None:
                        break
                    if any(row['id'] == quest['id'] and row['ready'] for row in current['quests']):
                        claim(aid, quest)
                if phase:
                    break
                current = snapshot(aid)
                if current is None or not current['targetValid'] or current['tickets'] <= 0:
                    break
                prepare(aid)
                current = snapshot(aid)
                if current is None or not current['targetValid'] or current['tickets'] <= 0:
                    break
                count = current['tickets']
                payload = client._auth_data({'ActivityID': aid, 'RoleID': current['target'], 'Language': 'CHS', 'Value': count})
                pending(aid, 'vote')
                try:
                    response = client.call('VoteHandler.VoteRole', payload)
                except (TaskDeferred, GameMessage):
                    runtime['waitingForLogin'].pop(aid, None)
                    raise
                confirmed = any(entry.get('Item', {}).get('StaticID') == current['ticket']
                                and entry['Item'].get('Count') == count
                                and entry.get('NowItem', {}).get('StaticID') == current['ticket']
                                and entry['NowItem'].get('Count') == current['tickets'] - count
                                for entry in response.get('CostItems') or [])
                if not confirmed:
                    raise GameError('投票扣票结果未确认')
                runtime['waitingForLogin'].pop(aid, None)
                emit(f'向{current["targetName"]}投出{count}票')
        except TaskDeferred:
            raise
        except Exception as error:
            waiting = aid in runtime.get('waitingForLogin', {})
            errors.append(f'{error}，等待登录同步刷新' if waiting else str(error))
    return {'ok': not errors, 'skipped': not lines and not errors,
            'detail': '；'.join(([] if on_partial else lines) + errors) or status(client.account_state, params, runtime)['note'],
            'liveLogged': bool(on_partial and lines and not errors)}
