"""Side-story rewards selected by official quest groups and server progress."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from . import game_clock, runtime_data as resources
from .game_client import GameError, GameMessage


KINDS = {'story': '故事奖励', 'achievement': '成就奖励'}


def build_catalog(quests, groups, activities, loc, catalog_version=''):
    definitions = {row['ID']: row for row in activities if row.get('Type') == 'SideStory'}
    owners = {row['ID']: row for row in groups
              if row.get('Type') in {'Branch', 'BranchAchievement'}
              and row.get('ActivityID') in definitions}
    result = {}
    for row in quests:
        group = owners.get(row.get('Group'))
        if not group:
            continue
        aid = group['ActivityID']
        # Some legacy quests use another activity's group (e.g. H806's _10
        # belongs to Branch1). They aren't part of this event's reward panel.
        if row.get('ActivityID') and row['ActivityID'] != aid:
            continue
        definition = definitions[aid]
        name = loc.get('T_Chapter_Name_' + aid) or loc.get(definition.get('Name')) or definition.get('Name') or aid
        event = result.setdefault(aid, {
            'name': re.sub(r'<[^>]*>', '', name).strip(), 'story': [], 'achievement': []})
        kind = 'story' if group['Type'] == 'Branch' else 'achievement'
        guards = list(dict.fromkeys(filter(None, [
            *str(group.get('ActivityIDs') or '').split('~'),
            *str(row.get('ActivityIDs') or '').split('~')])))
        event[kind].append({'id': row['ID'], 'activities': guards})
    return {'schema': 1, 'catalog': catalog_version, 'activities': result}


@resources.cached
def catalog():
    data = resources.read_json('assets/game_config.json', {}).get('activityRewards')
    if data is None:
        # An installed app can still have the same official release compiled
        # by an earlier version. Only use the bundled catalog if it matches.
        root = Path(os.environ.get('ARK_ASSET_ROOT') or Path(__file__).resolve().parents[1])
        bundled = json.loads((root / 'assets/game_config.json').read_text(encoding='utf-8')).get('activityRewards', {})
        current = resources.snapshot().manifest.get('catalog', '')
        if not current or current == bundled.get('catalog'):
            data = bundled
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise GameError('活动奖励资料尚未就绪，请更新游戏数据')
    return data


def _records(state):
    from .tasks import _daily_quest_list
    return {row['StaticID']: row for row in _daily_quest_list(state)
            if isinstance(row, dict) and row.get('StaticID')}


def _available(state, kind, now):
    from .tasks import _activity_schedule, _is_activity_live

    schedule = _activity_schedule(state)
    for aid, event in catalog()['activities'].items():
        # Match the activity page's live schedule instead of the static
        # IsUnlimitTime flag, which can outlive a specific event run.
        if _is_activity_live(schedule.get(aid), int(now * 1000)):
            for quest in event[kind]:
                if all(_is_activity_live(schedule.get(guard), int(now * 1000)) for guard in quest['activities']):
                    yield quest['id']


@resources.pinned
def status(state, *, now=None):
    now = time.time() if now is None else now
    records = _records(state)
    result = {}
    for kind in KINDS:
        ids = list(_available(state, kind, now))
        claimable = [sid for sid in ids if records.get(sid, {}).get('IsFinish') is True
                     and records[sid].get('IsRewarded') is False]
        result[kind] = {'claimable': len(claimable), 'claimed': sum(
            records.get(sid, {}).get('IsRewarded') is True for sid in ids)}
    return result


@resources.pinned
def claim(client, kind):
    from .tasks import _collect_items, _fmt_items, _merge_daily_quests
    if kind not in KINDS:
        raise GameError('请选择故事奖励或成就奖励')
    label = KINDS[kind]
    claimed, attempted, items = [], set(), {}

    def finish(ok, error=''):
        detail = (f'已领取 {len(claimed)} 项{label}：{_fmt_items(items)}' if claimed
                  else f'暂无可领取的{label}')
        if error:
            detail = f'{detail}；{error}' if claimed else error
        return {'ok': ok, 'skipped': ok and not claimed, 'detail': detail,
                'claimedIds': claimed, 'items': items}

    while True:
        records = _records(client.account_state or {})
        ids = [sid for sid in _available(client.account_state or {}, kind, game_clock.now_ms(client) / 1000)
               if sid not in attempted and records.get(sid, {}).get('IsFinish') is True
               and records[sid].get('IsRewarded') is False]
        if not ids:
            return finish(True)
        attempted.update(ids)
        try:
            response = client.call('QuestHandler.RewardQuest', client._auth_data({
                'RewardQuestInfos': [{'ID': sid, 'Index': 0} for sid in ids], 'CommodityID': ''}))
        except GameMessage as error:
            return finish(False, f'{label}领取失败：{error.msg}')
        except GameError as error:
            return finish(False, f'{label}领取失败：{error}')
        results = response.get('Results') or []
        _merge_daily_quests(client, results)
        confirmed = set()
        for result in results:
            if not isinstance(result, dict):
                continue
            for row in result.get('Quests') or []:
                if isinstance(row, dict) and row.get('StaticID') in ids and row.get('IsRewarded') is True:
                    confirmed.add(row['StaticID'])
            _collect_items((result.get('Drop') or {}).get('Items'), items)
        claimed.extend(sid for sid in ids if sid in confirmed)
        if len(confirmed) != len(ids):
            return finish(False, '部分领奖结果未确认，请登录同步后再试')
        # Re-read the merged response: claiming story chapters can unlock its
        # completion reward. Attempt each quest at most once in this operation.
