"""平台无关的核心业务逻辑。

本模块不依赖任何 Web 框架。每个函数对应一个 API 端点，返回可 JSON 序列化的
dict；出错时抛 ApiError(status, message)，由 server 层转成对应的 HTTP 响应。
Windows(server.py) 与 Android(Chaquopy) 共用本模块。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from . import accounts
from . import background_timer
from . import redeem_codes
from .guild import GuildService, describe_error
from .guild_jobs import manager as guild_jobs
from .logutil import log
from .game_client import GameClient, GameError, GameMessage, is_duplicate_login_message
from .portal import (
    PortalError, access_token_expires_at, access_token_needs_refresh,
    portal_login_v2, portal_refresh_v2,
)
from .tasks import (
    TASKS, run_task, run_toggle_tasks, toggle_tasks, manual_tasks,
    shop_reset, shop_buy_index, star_source_query, star_source_refresh, star_source_buy,
    customized_equip_query, customized_equip_choose_set, customized_equip_choose_part,
    customized_equip_choose_main, customized_equip_reset, customized_equip_refresh,
    customized_equip_choose_sub, customized_equip_reward, UnknownEquipmentError,
    HUNT_ELEMENTS, HUNT_SWEEP_CANDIDATES, SWEEP_KINDS, hunt_sweep_scenes,
    ELEMENT_SWEEP_ELEMENTS, element_sweep_scenes,
    list_teams, sweep_once,
    activity_scenes, query_support_friends, activity_once, cleanup_urgent_missions,
    support_brief, trim_support_entry,
    store_shelves, store_buy,
    item_name, abyss_scenes, abyss_scene_effects, abyss_role_briefs, normalize_abyss_config, abyss_claim_rewards,
)


class ApiError(Exception):
    """业务错误，携带 HTTP 状态码。server 层据此返回。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


# ---------- 资源目录 ----------
# 桌面：项目根；Android：入口通过 ARK_ASSET_ROOT 指向解包后的 assets 目录。
ROOT_DIR = Path(os.environ.get("ARK_ASSET_ROOT", Path(__file__).resolve().parent.parent))
FRONTEND_DIR = ROOT_DIR / "frontend"
ASSETS_DIR = ROOT_DIR / "assets"
AVATAR_DIR = ASSETS_DIR / "avatars"


# ---------- 多账号会话 ----------
# 会话状态全部移到 accounts 模块（每账号一个 Account，含 client/开关/日志/调度线程）。
# 每个 API 端点带一个 account(email) 参数选择目标账号。


# ---------- 货币/资源 StaticID 映射（实抓确认） ----------
CURRENCY_IDS = {
    "gold": "1",      # 金币
    "diamond": "2",   # 钻石
    "stamina": "3",   # 体力（Count=当前，TimeCollect.MaxCount=上限，可溢出）
    "flag": "4",      # 旗帜（同为随时间恢复型，MaxCount=5）
    "recruit": "5",   # 绿票 招募契约
    "mystery": "6",   # 黄票 神秘契约
    "galaxy": "7",    # 银河契约（月签到第28天奖励，实抓确认）
    "battery": "37",  # 能源电池
}

_AVATAR_INDEX: dict[str, str] | None = None


def _avatar_index() -> dict[str, str]:
    """HeroID(如 H076) -> 头像文件名。懒加载，扫描 avatars 目录一次。"""
    global _AVATAR_INDEX
    if _AVATAR_INDEX is None:
        idx: dict[str, str] = {}
        if AVATAR_DIR.exists():
            for f in AVATAR_DIR.iterdir():
                hid = f.stem.split("_", 1)[0]
                idx[hid] = f.name
        _AVATAR_INDEX = idx
    return _AVATAR_INDEX


def avatar_url(hero_id: str | None) -> str | None:
    fname = _avatar_index().get(hero_id or "")
    return f"/assets/avatars/{fname}" if fname else None


def summarize(state: dict) -> dict:
    """从庞大的登录响应里抽取要展示的关键信息。"""
    info = state.get("Info", {})

    def oid(d):
        return d.get("$oid") if isinstance(d, dict) else d

    def ms(d):
        return d.get("$date") if isinstance(d, dict) else d

    items = {it.get("StaticID"): it for it in state.get("ItemContainer", {}).get("Items", [])}

    def count(sid):
        it = items.get(sid)
        return it.get("Count", 0) if it else 0

    def gauge(sid):
        it = items.get(sid, {})
        return {
            "current": it.get("Count", 0),
            "max": (it.get("TimeCollect") or {}).get("MaxCount"),
        }

    return {
        "name": info.get("Name"),
        "level": info.get("LV"),
        "cuid": info.get("CUID"),
        "leaderSID": info.get("LeaderSID"),
        "avatar": avatar_url(info.get("LeaderSID")),
        "aid": oid(info.get("_id")),
        "loginTime": ms(info.get("LoginTime")),
        "currencies": {
            "gold": count(CURRENCY_IDS["gold"]),
            "diamond": count(CURRENCY_IDS["diamond"]),
            "battery": count(CURRENCY_IDS["battery"]),
            "recruit": count(CURRENCY_IDS["recruit"]),
            "mystery": count(CURRENCY_IDS["mystery"]),
            "galaxy": count(CURRENCY_IDS["galaxy"]),
        },
        "stamina": gauge(CURRENCY_IDS["stamina"]),
        "flag": gauge(CURRENCY_IDS["flag"]),
    }


# ---------- 团员 / 物品清单（基本信息页展示） ----------
# 潜能等级 = 团员 ImprintLV（潜能突破级别）。1~7 依次 D→C→B→A→S→SS→SSS，
# 0=尚未开发（前端不挂徽章）。上限 SSS，已用满级号 251 团员数据核实。
GRADE_BY_IMPRINT = {1: "D", 2: "C", 3: "B", 4: "A", 5: "S", 6: "SS", 7: "SSS"}

_ITEM_RANK: dict | None = None
_ITEM_CATEGORY: dict | None = None
_ROLE_ELEMENT: dict | None = None
_ROLE_JOB: dict | None = None


def _item_rank() -> dict:
    """读取随应用发布的 StaticID -> 稀有度(0~5) 索引，懒加载。"""
    global _ITEM_RANK
    if _ITEM_RANK is None:
        p = Path(__file__).resolve().parent / "item_rank.json"
        try:
            _ITEM_RANK = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _ITEM_RANK = {}
    return _ITEM_RANK


def _item_category() -> dict:
    """读取由官方 sd_Item 表生成的 StaticID -> 物品页分类索引。"""
    global _ITEM_CATEGORY
    if _ITEM_CATEGORY is None:
        p = ASSETS_DIR / "item_categories.json"
        try:
            _ITEM_CATEGORY = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _ITEM_CATEGORY = {}
    return _ITEM_CATEGORY


def _item_category_fallback(sid: str) -> str:
    """兼容未带分类索引的旧资源包；新资源以官方数据表生成结果为准。"""
    if sid == "38" or sid.startswith(("EC", "AC", "UEC", "UAC")):
        return "enhancement"
    if sid == "74" or (sid.startswith("R") and sid[1:].isdigit()):
        return "element"
    if sid.startswith("CR"):
        return "crystal"
    if sid.startswith("AH"):
        return "activity"
    if sid.startswith("C") and sid[1:].isdigit():
        return "catalyst"
    if sid.isdigit():
        return "wealth"
    return "other"


def _role_element() -> dict:
    """读取由官方 sd_Role 表生成的角色 StaticID -> 属性索引。"""
    global _ROLE_ELEMENT
    if _ROLE_ELEMENT is None:
        p = ASSETS_DIR / "role_elements.json"
        try:
            _ROLE_ELEMENT = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _ROLE_ELEMENT = {}
    return _ROLE_ELEMENT


def _role_job() -> dict:
    """复用随应用发布的图鉴索引，读取角色 StaticID -> 职业。"""
    global _ROLE_JOB
    if _ROLE_JOB is None:
        try:
            data = json.loads((ASSETS_DIR / "encyclopedia/index.json").read_text(encoding="utf-8"))
            _ROLE_JOB = {row["id"]: row["job"] for row in data["characters"]
                         if isinstance(row, dict) and row.get("id") and row.get("job")}
        except (OSError, ValueError, KeyError, TypeError):
            _ROLE_JOB = {}
    return _ROLE_JOB


def _roster_items(raw_items: list[dict]) -> list[dict]:
    """Project stackable inventory into the visible warehouse list."""
    rank = _item_rank()
    category = _item_category()
    shown = set(CURRENCY_IDS.values())
    items = []
    for it in raw_items:
        sid = str(it.get("StaticID", ""))
        if not sid or sid in shown or (it.get("Count") or 0) <= 0:
            continue
        items.append({
            "id": sid,
            "name": item_name(sid),
            "count": it.get("Count", 0),
            "rank": rank.get(sid, 0),
            "category": category.get(sid) or _item_category_fallback(sid),
        })
    items.sort(key=lambda item: (-(item["rank"] or 0), item["id"]))
    return items


def _roster(state: dict) -> dict:
    """从登录快照抽取团员与物品清单，供基本信息页展示。

    团员来自 RoleDataContainer.Roles；物品来自 ItemContainer.Items，
    但排除已在顶部单独展示的资源（金币/钻石/能源电池/能源/旗帜/招募/神秘/银河），
    避免与顶部资产列重复。
    """
    elements = _role_element()
    jobs = _role_job()

    heroes = []
    for r in state.get("RoleDataContainer", {}).get("Roles", []):
        sid = str(r.get("StaticID", ""))
        imp = int(r.get("ImprintLV") or 0)
        heroes.append({
            "id": sid,
            "name": item_name(sid),
            "level": r.get("LV"),
            "imprint": imp,
            "grade": GRADE_BY_IMPRINT.get(imp, ""),
            "avatar": avatar_url(sid),
            "element": elements.get(sid),
            "job": jobs.get(sid),
            "star": r.get("Star"),
            "awaken": r.get("AwakenLV"),
        })
    # 排序：星级 > 潜能 > 等级 > ID
    heroes.sort(key=lambda h: (-(h["star"] or 0), -(h["imprint"] or 0),
                               -(h["level"] or 0), h["id"]))

    return {"heroes": heroes,
            "items": _roster_items(state.get("ItemContainer", {}).get("Items", []))}


def _inventory_meta(acc) -> dict:
    getter = getattr(acc.client, "inventory_meta", None)
    if not callable(getter):
        return {}
    meta = getter()
    if not isinstance(meta, dict):
        return {}
    return meta


def inventory(account: str, since: int | None = None, epoch: str | None = None) -> dict:
    """Read versioned item changes from memory, without contacting the game."""
    acc = _require(account, allow_disconnected=True)
    snapshot = acc.client.inventory_snapshot(since=since, epoch=epoch)
    shown = set(CURRENCY_IDS.values())
    removed = set(snapshot["removed"])
    removed.update(str(item["StaticID"]) for item in snapshot["items"]
                   if (item.get("Count") or 0) <= 0)
    return {
        "inventory": {**snapshot, "items": _roster_items(snapshot["items"]),
                      "removed": sorted(removed - shown)},
        "inventoryEpoch": snapshot["epoch"],
        "inventoryRevision": snapshot["revision"],
        "status": summarize(acc.client.account_state or {}),
    }


def roster(account: str) -> dict:
    """GET /api/roster —— 返回某账号的团员 + 物品清单（基本信息页按需拉取）。

    团员按需拉取；道具以这里的版本为基准，通过 /api/inventory 同步后续变化。
    """
    acc = accounts.get(account or "")
    if not acc or not acc.client.account_state:
        raise ApiError(401, "账号未登录")
    snapshot = acc.client.inventory_snapshot()
    state = {**acc.client.account_state, "ItemContainer": {"Items": snapshot["items"]}}
    return {**_roster(state), "inventoryEpoch": snapshot["epoch"],
            "inventoryRevision": snapshot["revision"]}


# ---------- bootstrap / 登录 ----------
_AUTH_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_AUTH_REFRESH_LOCKS_GUARD = threading.Lock()


def _auth_refresh_lock(email: str) -> threading.Lock:
    with _AUTH_REFRESH_LOCKS_GUARD:
        return _AUTH_REFRESH_LOCKS.setdefault(email, threading.Lock())


def _auth_version(auth: dict) -> tuple[float, int]:
    return (
        access_token_expires_at(str(auth.get("access_token") or "")) or 0.0,
        int(auth.get("saved_at") or 0),
    )


def _refresh_account_auth(email: str, auth: dict, force: bool = False) -> dict:
    """按需续期并原子覆盖安全存储；同一账号并发请求只允许一个进入门户。"""
    if not force and not access_token_needs_refresh(str(auth.get("access_token") or "")):
        return auth
    with _auth_refresh_lock(email):
        latest = (config.load_accounts().get(email) or {}).get("auth")
        if latest and latest.get("access_token") != auth.get("access_token"):
            latest_is_usable = not access_token_needs_refresh(
                str(latest.get("access_token") or "")
            )
            if latest_is_usable and _auth_version(latest) >= _auth_version(auth):
                auth.clear()
                auth.update(latest)
                return auth
        if not force and not access_token_needs_refresh(str(auth.get("access_token") or "")):
            return auth
        refreshed = portal_refresh_v2(auth)
        # Refresh Token 通常会轮换，必须先安全落盘再让运行中的客户端采用新值。
        config.save_account(email, auth=refreshed)
        auth.clear()
        auth.update(refreshed)
        log.info("账号 %s 的登录令牌已无感续期", email)
        return auth


def _bootstrap_and_register(email: str, auth: dict) -> accounts.Account:
    """用 V2 Token 建立游戏会话并登记在线账号。"""
    client = GameClient(
        auth["access_token"],
        login_id=auth["user_id"],
        login_common=config.android_login_common(auth["device_id"]),
        auth_refresher=lambda force=False: _refresh_account_auth(email, auth, force),
    )
    try:
        client.bootstrap()
    except PortalError as e:
        client.close()
        log.warning("账号 %s 登录凭证续期失败: %s", email, e)
        raise ApiError(401, "登录凭证已过期，请重新输入密码登录")
    except Exception as e:
        client.close()
        log.exception("游戏 bootstrap 失败: %s", e)
        raise ApiError(400, f"游戏登录失败: {e}")
    return accounts.add(email, client, {"userId": auth["user_id"]})


def connection_status(account: str | None) -> dict | None:
    """Return the in-memory game-session status for an online account."""
    acc = accounts.get(account or "")
    if not acc:
        return None
    getter = getattr(acc.client, "connection_status", None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, dict):
                changed_at = value.get("changedAt")
                if isinstance(changed_at, bool) or not isinstance(changed_at, (int, float)):
                    changed_at = None
                return {
                    "connected": bool(value.get("connected", True)),
                    "reason": value.get("reason"),
                    "changedAt": int(changed_at) if changed_at is not None else None,
                }
        except Exception:
            pass
    return {"connected": True, "reason": None, "changedAt": None}


def attach_connection(result: dict, account: str | None) -> dict:
    """Attach session and inventory versions to account API responses."""
    if not isinstance(result, dict):
        return result
    status = connection_status(account)
    if status is None:
        return result
    acc = accounts.get(account or "")
    return {**(_inventory_meta(acc) if acc else {}), **result, "connection": status}


def _account_payload(acc: accounts.Account) -> dict:
    """登录/切换账号后返回给前端的统一负载。"""
    state = acc.client.account_state or {}
    payload = {
        "account": acc.email,
        "status": summarize(state),
        "nextClaims": acc.next_claims(),
        "toggles": acc.toggles,
        "params": acc.params,
        "guild": acc.guild_snapshot(),
        # 新前端缓冲从头补读，包含测试页恢复时已经产生的后台日志。
        "logSeq": 0,
        "stateRevision": acc.state_revision,
        "stateSyncedAt": acc.state_synced_at,
        "stateSyncError": acc.state_sync_error,
        "redeemCodes": redeem_codes.get_state((acc.params.get("redeem_code") or {}).get("codes")),
        "connection": connection_status(acc.email),
        **_inventory_meta(acc),
    }
    if acc.portal:
        payload["portal"] = {
            "nickname": acc.portal.get("nickname") or acc.portal.get("nickName"),
            "userId": acc.portal.get("userId"),
        }
    return payload


def _require(account: str | None, *, allow_disconnected: bool = False) -> accounts.Account:
    acc = accounts.get(account or "")
    if not allow_disconnected:
        status = connection_status(account)
        if status and not status.get("connected", True):
            raise ApiError(409, "[重复登录] 当前连接已断开")
    if not acc:
        raise ApiError(401, "账号未登录或已下线")
    return acc


@contextmanager
def _guild_account(account):
    acc = _require(account)
    if not acc.lock.acquire(blocking=False):
        raise ApiError(409, "当前账号正在执行任务，请稍后再试")
    try:
        if accounts.get(acc.email) is not acc:
            raise ApiError(401, "登录会话已变更，请使用当前账号")
        yield acc
    except (GameError, GameMessage) as error:
        status = 409 if is_duplicate_login_message(error) else 400
        raise ApiError(status, describe_error(error)) from error
    finally:
        acc.lock.release()


def guild_overview(account):
    with _guild_account(account) as acc:
        try:
            acc.refresh_guild_locked()
            return {"ok": True, **_status_snapshot(acc)}
        finally:
            acc._wake.set()


def guild_action(account, action, data=None):
    data = data or {}
    with _guild_account(account) as acc:
        service = GuildService(acc.client)
        if action == "checkin":
            result = service.checkin()
        elif action == "attendance":
            result = service.attendance()
        elif action == "buy":
            result = service.buy(data.get("itemId"), data.get("count", 1))
        else:
            raise ApiError(400, "未知佣兵团操作")
        return {"ok": True, "result": result, **_status_snapshot(acc)}


def guild_store_buy(account, data):
    from .guild_store import GuildStoreService
    with _guild_account(account) as acc:
        store = GuildStoreService(GuildService(acc.client, acc.guild_runtime))
        try:
            count = store.buy(data.get('id'), data.get('count', 1))
            row = next(row for row in store.snapshot()['goods'] if row['id'] == data.get('id'))
            acc.push_log({'id': 'guild_store', 'name': '团员商店', 'ok': True,
                          'detail': f"已购买{row['name']} ×{count * row['amount']}"}, '主动·')
        finally:
            acc._guild_revision += 1
            acc._wake.set()
        return {'ok': True, **_status_snapshot(acc)}


def guild_quests_query(account):
    with _guild_account(account) as acc:
        return GuildService(acc.client).quest_ids()


def guild_quests_start(account, data):
    acc = _require(account)
    try:
        if data.get("mode") == "score":
            return guild_jobs.start_score(acc, requestId=data.get("requestId"))
        if data.get("mode") not in (None, "rounds"):
            raise GameError("未知周任务执行方式")
        return guild_jobs.start_rewards(acc, rounds=data.get("rounds", 30), requestId=data.get("requestId"))
    except GameError as error:
        raise ApiError(400, str(error)) from error


def guild_quests_status(account, job_id=None):
    acc = _require(account, allow_disconnected=True)
    return {**guild_jobs.status(acc.email, job_id, kind="quest_rewards"),
            "guild": acc.guild_snapshot()}


def guild_quests_cancel(account, job_id):
    acc = _require(account, allow_disconnected=True)
    if not job_id:
        raise ApiError(400, "缺少任务 ID")
    return guild_jobs.cancel(acc.email, job_id, kind="quest_rewards")


# ---------- 端点处理（平台无关） ----------
def login(account: str, password: str) -> dict:
    """精简 V2 登录。密码仅用于本次请求，成功后只持久化加密 Token。"""
    account = (account or "").strip()
    if not account:
        raise ApiError(400, "账号不能为空")
    if not password:
        raise ApiError(400, "请输入密码，或使用该账号已保存的 Token 登录")
    try:
        auth = portal_login_v2(account, password, config.get_device_id())
    except PortalError as e:
        log.warning("V2 门户登录失败: %s", e)
        raise ApiError(400, f"门户登录失败：{e}")
    acc = _bootstrap_and_register(account, auth)
    try:
        config.save_account(account, auth=auth)
    except Exception as e:
        accounts.remove(account)
        log.exception("账号 %s 的登录 Token 无法安全保存: %s", account, e)
        raise ApiError(500, f"登录令牌无法安全保存：{e}")
    return _account_payload(acc)


def login_saved(account: str) -> dict:
    """使用本机安全存储中的 V2 Token 恢复游戏会话。"""
    account = (account or "").strip()
    if not account:
        raise ApiError(400, "账号不能为空")
    record = config.load_accounts().get(account)
    if not record:
        raise ApiError(404, "没有找到该账号的本地登录凭证")
    if record.get("auth_error"):
        raise ApiError(401, "本地 Token 无法解密，请重新输入密码登录")
    auth = record.get("auth")
    if not auth:
        raise ApiError(401, "该账号没有已保存的 Token，请重新输入密码登录")
    acc = _bootstrap_and_register(account, auth)
    return _account_payload(acc)


def start_account_tasks(account: str | None) -> dict:
    """前端主界面就绪后激活后台任务，不等待游戏任务完成。"""
    acc = _require(account)
    try:
        started = acc.start_scheduler()
    except RuntimeError as e:
        raise ApiError(409, str(e)) from e
    return {"ok": True, "started": started}


def list_accounts() -> dict:
    """账号列表：在线账号（带状态）+ 存档里的离线账号。供左下角切换器展示。"""
    online = {a.email: a for a in accounts.all_accounts()}
    saved = config.load_accounts()
    order = {e: (saved.get(e, {}).get("order", i)) for i, e in enumerate(
        list(saved.keys()) + [e for e in online if e not in saved])}
    emails = sorted(set(list(saved.keys()) + list(online.keys())), key=lambda e: order.get(e, 999))
    items = []
    for email in emails:
        acc = online.get(email)
        rec = saved.get(email, {})
        item = {
            "account": email,
            "online": acc is not None,
            "hasToken": bool(rec.get("auth")),
            "tokenError": bool(rec.get("auth_error")),
        }
        if acc:
            st = summarize(acc.client.account_state or {})
            item["name"] = st.get("name")
            item["level"] = st.get("level")
            item["avatar"] = st.get("avatar")
            item["connection"] = connection_status(email)
            item["toggles"] = acc.toggles
            item["params"] = acc.params
            item["logSeq"] = acc.log_seq
            item["stateRevision"] = acc.state_revision
            item["stateSyncedAt"] = acc.state_synced_at
            item["stateSyncError"] = acc.state_sync_error
        items.append(item)
    return {"accounts": items, "autoRestore": config.TEST_SERVER}


def restore_account(account: str) -> dict:
    """Return an already-live in-memory account without logging in again."""
    if not config.TEST_SERVER:
        raise ApiError(404, "该接口仅供 start.bat 测试模式使用")
    return _account_payload(_require(account, allow_disconnected=True))


def logout(account: str) -> dict:
    """下线一个在线账号（保留存档，可再连接）。"""
    accounts.remove(account)
    return {"ok": True}


def delete_account(account: str) -> dict:
    """删除账号：下线 + 清除加密 Token、开关和其他账号设置。"""
    accounts.remove(account)
    config.delete_account(account)
    return {"ok": True}


def set_toggles(account: str, toggles: dict, params: dict | None = None) -> dict:
    """更新某账号的自动任务开关 + 参数，持久化。后台调度线程下轮据此执行。

    ⚠️ params 是**按任务 key 合并**，不是整体替换。整体替换踩过一个真 bug：竞技场的
    勾选是走 `/api/arena/config` 单独写进 `params["arena_npc"]` 的，而前端内存里那份
    `a.params` 副本还是登录时的旧值（不含 arena_npc）；用户一拨开关，`/api/toggles`
    把那份陈旧副本整体盖回来 → 勾选全没了。合并后，不认识某个 key 的调用方删不掉它。
    （"关闭开关看着正常"只是错觉：关闭同样会清掉后端数据，但前端内存里的 a.arena 还
    在显示勾选；打开时多走一次 refreshStatus 从后端重建，才把丢失暴露出来。）
    """
    acc = _require(account, allow_disconnected=True)
    if isinstance(toggles, dict):
        acc.toggles = {k: bool(v) for k, v in toggles.items()}
    if isinstance(params, dict):
        merged = dict(acc.params)
        merged.update({key: value for key, value in params.items() if key != "redeem_code"})
        acc.params = merged
    config.save_account(account, toggles=acc.toggles, params=acc.params)
    acc.notify_config_changed()   # 调度线程立刻重算档期，不必等下一个到点时刻
    return {"ok": True, "toggles": acc.toggles, "params": acc.params}


def auto_logs(account: str, since: int = 0) -> dict:
    """拉取某账号后台调度产生的新日志（seq > since），供前端增量显示。"""
    acc = accounts.get(account or "")
    if not acc:
        return {"entries": [], "logSeq": 0}
    entries = acc.logs_since(since)
    return {
        "entries": entries,
        # 游标只能指向本次已读取的记录，避免并发写入时跳过新日志。
        "logSeq": entries[-1]["seq"] if entries else since,
        "stateRevision": acc.state_revision,
        "stateSyncedAt": acc.state_synced_at,
        "stateSyncError": acc.state_sync_error,
        "abyssBattle": _abyss_snapshot(account or ""),
        "activityProgress": activity_progress_snapshot(account or ""),
        "guild": acc.guild_snapshot(),
        **_inventory_meta(acc),
    }


def _status_snapshot(acc) -> dict:
    """从当前(可能已被 NowItem 增量更新的)缓存 account_state 生成 status 快照。

    操作类接口(刷商店/扫荡)复用它把最新货币/资源随响应一起返回，前端每次操作后
    可立即 applyStatus，零额外请求实时反映——招募契约等背包物品同金币一样走
    NowItem 增量，故购买后余额会即时变化。
    """
    st = acc.client.account_state
    if not st:
        return {}
    return {
        "status": summarize(st),
        "nextClaims": acc.next_claims(),
        "guild": acc.guild_snapshot(),
        "stateRevision": acc.state_revision,
        "stateSyncedAt": acc.state_synced_at,
        "stateSyncError": acc.state_sync_error,
        "redeemCodes": redeem_codes.get_state((acc.params.get("redeem_code") or {}).get("codes")),
        **_inventory_meta(acc),
    }


def status(account: str) -> dict:
    acc = accounts.get(account or "")
    if not acc or not acc.client.account_state:
        raise ApiError(401, "账号未登录")
    # 带上竞技场档期：前端后台轮询有新巡检日志时据此刷新展开页的倒计时，
    # 让后台扫荡完成后"下次挑战时间"自动跟上。不塞进 _status_snapshot——那个还被
    # 刷商店/扫荡的响应复用，没必要每次操作都捎带这份。
    return {**_status_snapshot(acc), "arena": arena_state(acc)}


def refresh_status(account: str) -> dict:
    """手动刷新：重新 AccountHandler.Login 拉全量账号状态。

    游戏没有单独查背包/货币的轻量接口，只有重登能拿到含游戏客户端内变化 + 体力恢复的
    权威全量。故此接口只在用户主动点"刷新"时调用，不做自动轮询（避免频繁重登）。
    """
    acc = _require(account, allow_disconnected=True)
    try:
        with acc.lock:
            state = acc.client.bootstrap()
            # 与后台任务在同一把锁内重置，避免刚完成的体力邮件确认被并发清空。
            acc.reset_task_runtime()
            acc.note_state_refreshed()
    except Exception as e:
        log.exception("刷新（重登）失败: %s", e)
        raise ApiError(400, f"刷新失败: {e}")
    return {**_status_snapshot(acc), "arena": arena_state(acc)}


def arena_state(acc) -> dict:
    """竞技场展开页要的状态：10 个 NPC 的名字 + 各自下次可挑战时刻。

    冷却取 **max(登录快照算出的档期, 本工具扫荡后拿到的 nextMs)**，再和失败退避
    `retryMs` 一起决定闸门。

    快照那份怎么来的：`PVPData.NPCPVPInfoList` 有 30 条(10 NPC × 普通/困难/地狱)，
    每条只有 `{_id, NPCID, NextTime}`、没有难度字段；但**三个难度共享同一个冷却**
    （用户在游戏内确认），所以同 NPCID 三条取 **max** 就是该 NPC 的权威可挑战时刻，
    不需要分辨哪条是地狱级、也不需要额外请求 QueryPVPData。详见
    `tasks.arena_next_times`（含 7/7 实测验证）。
    """
    from .tasks import arena_npcs, arena_next_times
    rt = acc.task_runtime.get("arena_npc") or {}
    next_map = rt.get("nextMs") or {}
    retry_map = rt.get("retryMs") or {}
    waiting = rt.get("waitingForLogin") or {}
    snap_map = arena_next_times(acc.client.account_state or {})
    params = (acc.params.get("arena_npc") or {})
    picked = [int(x) for x in (params.get("picked") or [])]
    items = []
    for it in arena_npcs():
        sid = it["sceneId"]
        nxt = max(snap_map.get(sid) or 0, next_map.get(sid) or 0) or None
        paused = bool(waiting.get(sid))
        items.append({
            **it,
            "picked": it["n"] in picked,
            "nextMs": None if paused else nxt,
            "retryMs": None if paused else retry_map.get(sid),
            "waitingForLogin": paused,
        })
    return {"npcs": items, "teamId": str(params.get("teamId") or "0"),
            "teams": _arena_teams(acc)}


def _arena_teams(acc) -> list[dict]:
    from .tasks import list_teams
    try:
        return list_teams(acc.client)
    except Exception:
        return []


def arena_options(account: str) -> dict:
    """GET /api/arena/options —— 展开页首次打开时拉 NPC 清单 + 队伍 + 冷却。"""
    return arena_state(_require(account))


def arena_set(account: str, picked: list | None, team_id: str | None) -> dict:
    """POST /api/arena/config —— 保存勾选的 NPC 与队伍（持久化到账号存档）。"""
    acc = _require(account)
    cur = dict(acc.params.get("arena_npc") or {})
    if picked is not None:
        cur["picked"] = sorted({int(x) for x in picked})
    if team_id is not None:
        cur["teamId"] = str(team_id)
    acc.params = {**acc.params, "arena_npc": cur}
    config.save_account(acc.email, toggles=acc.toggles, params=acc.params)
    # 勾选一变就唤醒调度线程：开关已打开的情况下，勾完 NPC 应当马上开打，
    # 而不是等下一轮巡检（用户反馈"隔了 5 分钟日志才出来"就是这个）。
    acc.notify_config_changed()
    # 带回整份 params：前端要用它更新内存副本，否则下次 /api/toggles 又会拿旧副本
    # 把这里刚写的勾选盖掉（见 set_toggles 的注释）。
    return {**arena_state(acc), "params": acc.params}


def tasks() -> dict:
    """任务清单 + 购买目标表。buyTargets 由后端给，前端不再自带一份副本
    （两份默认值曾经不一致，导致免费刷商店漏买强化芯片）。"""
    from .tasks import BUY_TARGETS
    return {"toggle": toggle_tasks(), "manual": manual_tasks(),
            "buyTargets": BUY_TARGETS}


def run_auto(account: str, task_ids: list[str], params: dict) -> dict:
    """前台按开关运行一批 toggle 任务（用户手动开开关时立即跑一次）。
    结果直接返回给前端展示，不写日志缓冲（避免与轮询重复）。"""
    acc = _require(account)
    # 前端传来的 params 覆盖到账号级，保证与后台调度一致
    if isinstance(params, dict) and params:
        merged = dict(acc.params)
        merged.update({key: value for key, value in params.items() if key != "redeem_code"})
        acc.params = merged
    results = acc.run_toggles(task_ids, "开关·", log_to_buffer=False)
    return {"results": results}


def run(account: str, task_ids: list[str], params: dict) -> dict:
    acc = _require(account)
    results = []
    with acc.lock:
        for tid in task_ids:
            name = TASKS.get(tid, {}).get("name", tid)
            try:
                r = run_task(acc.client, tid, params.get(tid))
                results.append({"id": tid, "name": name, **r})
            except Exception as e:
                results.append({"id": tid, "name": name, "ok": False, "detail": str(e)})
    return {"results": results}


def shop_reset_ep(account: str, use_gold: bool, auto_buy: list[str],
                  equipment_scheme: dict | None = None) -> dict:
    acc = _require(account)
    if equipment_scheme is not None and not isinstance(equipment_scheme, dict):
        raise ApiError(400, "装备筛选方案格式无效")
    with acc.lock:
        r = shop_reset(acc.client, use_gold, auto_buy, equipment_scheme)
    acc.shop_shelf = r.get("records", [])
    out = {k: v for k, v in r.items() if k != "records"}
    out.update(_status_snapshot(acc))   # 带回最新货币/资源，前端每次刷新后即时更新主页
    return out


def shop_buy_ep(account: str, index: int) -> dict:
    acc = _require(account)
    shelf = acc.shop_shelf or []
    try:
        with acc.lock:
            r = shop_buy_index(acc.client, shelf, index)
    except UnknownEquipmentError as e:
        raise ApiError(422, f"装备 StaticID={e.static_id} 不在图鉴数据中，"
                            f"无法解析——请补充 backend/equip_ref.json")
    r.update(_status_snapshot(acc))
    return r


def star_source_refresh_ep(account: str, scheme: dict) -> dict:
    acc = _require(account)
    if not isinstance(scheme, dict):
        raise ApiError(400, "筛选方案格式无效")
    try:
        with acc.lock:
            r = star_source_refresh(acc.client, scheme)
    except UnknownEquipmentError as e:
        raise ApiError(
            422,
            f"装备 StaticID={e.static_id} 不在图鉴数据中，无法执行筛选——请补充 backend/equip_ref.json",
        )
    r.update(_status_snapshot(acc))
    return r


def star_source_buy_ep(account: str, shop_index: int) -> dict:
    acc = _require(account)
    with acc.lock:
        result = star_source_buy(acc.client, shop_index)
    result.update(_status_snapshot(acc))
    return result


def star_source_query_ep(account: str) -> dict:
    acc = _require(account)
    with acc.lock:
        r = star_source_query(acc.client)
    r.update(_status_snapshot(acc))
    return r


def customized_equip_ep(account: str, action: str, body: dict) -> dict:
    acc = _require(account)
    actions = {
        "query": lambda: customized_equip_query(acc.client, body.get("scheme")),
        "chooseSet": lambda: customized_equip_choose_set(acc.client, str(body.get("set") or "")),
        "choosePart": lambda: customized_equip_choose_part(acc.client, str(body.get("part") or "")),
        "chooseMain": lambda: customized_equip_choose_main(
            acc.client, str(body.get("part") or ""), str(body.get("main") or ""),
        ),
        "reset": lambda: customized_equip_reset(acc.client, str(body.get("level") or "")),
        "refresh": lambda: customized_equip_refresh(acc.client, body.get("scheme")),
        "chooseSub": lambda: customized_equip_choose_sub(acc.client, bool(body.get("accept"))),
        "reward": lambda: customized_equip_reward(acc.client),
    }
    callback = actions.get(str(action or ""))
    if callback is None:
        raise ApiError(400, "无效的装备活动操作")
    try:
        with acc.lock:
            result = callback()
    except UnknownEquipmentError as error:
        raise ApiError(
            422,
            f"装备 StaticID={error.static_id} 不在图鉴数据中，无法执行筛选——请补充 backend/equip_ref.json",
        )
    result.update(_status_snapshot(acc))
    return result


def store_options(account: str) -> dict:
    """商店购买的静态货架 + 当前相关货币余额。

    货架读的是登录快照，不额外发请求（游戏本身也是从登录快照渲染这些商店的，
    R.har/buy.har 里打开商店界面都没有任何查询 route）。余额随快照一起给，前端
    卡片上直接对照"够不够买"。
    """
    acc = _require(account)
    state = acc.client.account_state or {}
    shelves = store_shelves(state)
    items = {str(it.get("StaticID")): int(it.get("Count") or 0)
             for it in state.get("ItemContainer", {}).get("Items", [])}
    wallet = {s["currency"]: items.get(s["currency"], 0) for s in shelves}
    return {
        "stores": shelves,
        "wallet": wallet,
        "stateRevision": acc.state_revision,
        "stateSyncedAt": acc.state_synced_at,
        "stateSyncError": acc.state_sync_error,
    }


def store_buy_ep(account: str, picks: list[dict]) -> dict:
    acc = _require(account)
    if not isinstance(picks, list) or not picks:
        raise ApiError(400, "没有选择要购买的商品")
    with acc.lock:
        r = store_buy(acc.client, picks)
    r.update(_status_snapshot(acc))   # 带回最新货币，前端购买后即时刷新主页
    return r


def sweep_options(account: str, kind: str = "hunt") -> dict:
    acc = _require(account)
    if kind == "hunt":
        scenes = hunt_sweep_scenes(acc.client.account_state or {})
        elements = [{"id": row["id"], "name": row["name"]} for row in HUNT_ELEMENTS]
    elif kind == "elf":
        scenes = element_sweep_scenes(acc.client.account_state or {})
        elements = [
            {"id": row["id"], "name": row["name"]}
            for row in ELEMENT_SWEEP_ELEMENTS
        ]
    else:
        scenes = SWEEP_KINDS.get(kind, HUNT_SWEEP_CANDIDATES)
        elements = []
    return {"elements": elements, "scenes": scenes, "teams": list_teams(acc.client)}


def sweep_run(account: str, scene_id: str, team_id: str, quick_battle: bool = True) -> dict:
    acc = _require(account)
    try:
        with acc.lock:
            r = sweep_once(acc.client, scene_id, team_id, quick_battle)
    except GameError as e:
        raise ApiError(400, str(e))
    r.update(_status_snapshot(acc))
    return r


def _decorate_support(brief: dict) -> dict:
    """给助战精简项补头像 URL + 潜能字形（前端复用信息页 hero-card 渲染）。"""
    h = brief.get("hero") or {}
    h["avatar"] = avatar_url(h.get("id"))
    h["grade"] = GRADE_BY_IMPRINT.get(h.get("imprint") or 0, "")
    return brief


def activity_options(account: str) -> dict:
    """活动关卡选项：当期全部已解锁关卡 + 队伍 + 助战好友 + 助战收藏夹。

    助战列表原始条目缓存到 Account.support_cache 供 activity_run 构建 payload。
    收藏夹(按账号存，见 config.load_support_favs)里的人**即使已不在助战列表**也照样
    返回，用存下来的快照打；若他还在列表里，则用列表新快照覆盖存档(自动刷新，对方
    升级/换装后数据保持最新)。
    """
    from .activity_progress import options as progress_options
    acc = _require(account)
    state = acc.client.account_state or {}
    try:
        with acc.lock:
            friends, raw = query_support_friends(acc.client)
    except GameError as e:
        raise ApiError(400, str(e))
    acc.support_cache = raw

    # 收藏夹：在线的用新快照刷新存档，不在线的沿用旧快照
    favs = config.load_support_favs(acc.email)
    fav_list = []
    for cuid, entry in favs.items():
        live = raw.get(str(cuid))
        if live is not None:
            entry = trim_support_entry(live)
            config.save_support_fav(acc.email, cuid, entry)
        brief = support_brief(entry)
        if brief is None:                     # 存档损坏(无队长)，跳过不阻断
            continue
        brief["online"] = live is not None    # 前端标"当前在助战列表里"
        fav_list.append(_decorate_support(brief))
    fav_list.sort(key=lambda b: str(b.get("name") or ""))

    fav_cuids = set(favs.keys())
    for f in friends:
        f["fav"] = str(f["cuid"]) in fav_cuids
        _decorate_support(f)
    return {
        "scenes": activity_scenes(state),
        "activities": progress_options(state),
        "teams": list_teams(acc.client),
        "supports": friends,
        "favorites": fav_list,
    }


def support_fav_add(account: str, support_cuid: str) -> dict:
    """把助战列表里的某人加入收藏夹（存裁剪后的队长快照）。"""
    acc = _require(account)
    entry = acc.support_cache.get(str(support_cuid))
    if entry is None:
        raise ApiError(400, "该助战成员不在当前列表里，请重新展开选项刷新助战列表")
    try:
        trimmed = trim_support_entry(entry)
    except GameError as e:
        raise ApiError(400, str(e))
    config.save_support_fav(acc.email, support_cuid, trimmed)
    brief = support_brief(trimmed)
    brief["online"] = True
    return {"ok": True, "favorite": _decorate_support(brief)}


def support_fav_remove(account: str, support_cuid: str) -> dict:
    """从收藏夹移除。已不在收藏夹里也返回 ok（幂等）。"""
    acc = _require(account)
    return {"ok": True, "removed": config.delete_support_fav(acc.email, support_cuid)}


def _activity_support_entry(acc, support_cuid: str | None) -> dict | None:
    """解析活动页选中的在线助战或收藏快照。"""
    if support_cuid:
        # 先查在线助战列表，再回落到收藏夹快照（收藏的人可能已不在列表里）
        entry = acc.support_cache.get(str(support_cuid)) \
            or config.load_support_favs(acc.email).get(str(support_cuid))
        if entry is None:
            raise ApiError(400, "助战成员已失效，请重新展开选项刷新助战列表")
        return entry
    return None


def _normalize_urgent_specializations(value) -> list[str] | None:
    """None 表示未启用；空列表表示启用但不设置即时清理目标。"""
    if value is None:
        return None
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ApiError(400, "无效的紧急任务特化类型")
    allowed = {"gold", "catalyst"}
    if any(not isinstance(mode, str) or mode not in allowed for mode in values):
        raise ApiError(400, "无效的紧急任务特化类型")
    return list(dict.fromkeys(values))


def activity_run(account: str, scene_id: str, team_id: str,
                 support_cuid: str | None = None, quick_battle: bool = True,
                 urgent_specializations=None) -> dict:
    acc = _require(account)
    entry = _activity_support_entry(acc, support_cuid)
    urgent_modes = _normalize_urgent_specializations(urgent_specializations)
    try:
        with acc.lock:
            r = activity_once(acc.client, scene_id, team_id, entry, quick_battle)
            if r.get("ok") and urgent_modes is not None:
                try:
                    urgent = cleanup_urgent_missions(
                        acc.client, team_id, entry, specializations=urgent_modes)
                except GameError as error:
                    urgent = {"ok": False, "results": [{
                        "ok": False, "detail": f"自动清理失败：{error}",
                        "urgentName": "紧急任务",
                    }], "remaining": []}
                r["urgentCleanup"] = urgent
    except GameError as e:
        raise ApiError(400, str(e))
    r.update(_status_snapshot(acc))
    return r


_activity_sessions_lock = threading.Lock()
_activity_sessions: dict[str, threading.Event] = {}
_activity_progress: dict[str, dict] = {}


def activity_progress_snapshot(account: str) -> dict:
    with _activity_sessions_lock:
        return {"running": account in _activity_sessions, "progress": _activity_progress.get(account)}


def activity_progress_cancel(account: str) -> dict:
    acc = _require(account, allow_disconnected=True)
    with _activity_sessions_lock:
        event = _activity_sessions.get(acc.email)
        if event:
            event.set()
    return {"ok": True, "running": event is not None}


def activity_progress_run(account: str, family: str, team_id: str, support_cuid=None) -> dict:
    from . import activity_progress
    acc = _require(account)
    if not re.fullmatch(r"BH\d+", family or ""):
        raise ApiError(400, "请选择当前活动")
    entry = _activity_support_entry(acc, support_cuid)
    event = threading.Event()
    with _activity_sessions_lock:
        if acc.email in _activity_sessions:
            raise ApiError(409, "当前账号已有自动推图正在运行")
        _activity_sessions[acc.email] = event
        _activity_progress.pop(acc.email, None)

    def on_progress(snapshot):
        if snapshot.get("battle"):
            snapshot = {**snapshot, "battle": _battle_progress_brief(snapshot["battle"])}
        with _activity_sessions_lock:
            if _activity_sessions.get(acc.email) is event:
                _activity_progress[acc.email] = snapshot

    try:
        with acc.lock:
            result = activity_progress.run(acc.client, family, team_id, entry,
                                           cancel_event=event, on_progress=on_progress,
                                           on_partial=lambda partial: acc.push_log(partial, ""))
    except GameError as error:
        raise ApiError(400, str(error)) from error
    finally:
        with _activity_sessions_lock:
            if _activity_sessions.get(acc.email) is event:
                del _activity_sessions[acc.email]
    return {**result, "activityProgress": activity_progress_snapshot(acc.email), **_status_snapshot(acc)}


def activity_clear_urgent(account: str, team_id: str,
                          support_cuid: str | None = None) -> dict:
    """活动扫荡结束时，清空账号当前剩余的限时紧急任务。"""
    acc = _require(account)
    entry = _activity_support_entry(acc, support_cuid)
    try:
        with acc.lock:
            result = cleanup_urgent_missions(
                acc.client, team_id, entry, clear_all=True)
    except GameError as e:
        raise ApiError(400, str(e))
    result.update(_status_snapshot(acc))
    return result


def abyss_options(account: str) -> dict:
    """Return current stage effects, warehouse roles, and saved team config."""
    acc = _require(account)
    state = acc.client.account_state or {}
    heroes = []
    for hero in abyss_role_briefs(state):
        hero = dict(hero)
        hero["avatar"] = avatar_url(hero.get("staticId"))
        hero["grade"] = GRADE_BY_IMPRINT.get(int(hero.get("imprint") or 0), "")
        heroes.append(hero)
    saved = acc.params.get("abyss_auto") or {}
    # Return only the normalized, user-editable fields; old malformed records are
    # ignored so a stale config cannot break opening the options panel.
    try:
        selected = normalize_abyss_config(state, saved.get("sceneId"), saved.get("waves"))
    except Exception:
        selected = {"sceneId": "", "waves": {"upper": [None] * 4, "lower": [None] * 4}}
    selected["autoAdvance"] = saved.get("autoAdvance") is True
    return {
        "scenes": [{**scene, "effects": abyss_scene_effects(state, scene["id"])}
                   for scene in abyss_scenes(state)],
        "heroes": heroes,
        "roles": heroes,
        "teams": [],
        "config": selected,
        "stateRevision": acc.state_revision,
        "stateSyncedAt": acc.state_synced_at,
        "stateSyncError": acc.state_sync_error,
    }


def abyss_set(account: str, scene_id, waves, auto_advance: bool = False) -> dict:
    """Validate and persist the two-wave Abyss picker configuration."""
    acc = _require(account)
    try:
        normalized = normalize_abyss_config(acc.client.account_state or {}, scene_id, waves)
    except GameError as error:
        raise ApiError(400, str(error)) from error
    if auto_advance is True:
        normalized["autoAdvance"] = True
    acc.params = {**acc.params, "abyss_auto": normalized}
    config.save_account(acc.email, toggles=acc.toggles, params=acc.params)
    acc.notify_config_changed()
    return {"ok": True, "config": normalized}


_abyss_sessions_lock = threading.Lock()
_abyss_sessions: dict[str, threading.Event] = {}
_abyss_progress: dict[str, dict] = {}


def _abyss_snapshot(account: str) -> dict:
    # Reading progress must not wait on the account lock held by the battle.
    with _abyss_sessions_lock:
        return {"running": account in _abyss_sessions, "progress": _abyss_progress.get(account)}


def _abyss_member_brief(member: dict) -> dict:
    sid = str(member.get("staticId") or "")
    # Fantasy/activity hero variants share their base hero's HeadS portrait.
    variant = re.fullmatch(r"(?:F|Ac[BE]|AcStory)(H\d+)(?:_.*)?", sid)
    portrait_id = variant.group(1) if variant else (sid[2:] if re.fullmatch(r"AcM\d+", sid) else sid)
    return {**member, "avatar": avatar_url(portrait_id), "name": item_name(portrait_id)}


def _battle_progress_brief(snapshot: dict) -> dict:
    return {**snapshot, "enemies": [_abyss_member_brief(member) for member in snapshot.get("enemies", [])],
            "allies": [_abyss_member_brief(member) for member in snapshot.get("allies", [])]}


def abyss_cancel(account: str) -> dict:
    """Signal the running executor without waiting on its account task lock."""
    acc = _require(account)
    with _abyss_sessions_lock:
        event = _abyss_sessions.get(acc.email)
        if event is not None:
            event.set()
    return {"ok": True, "running": event is not None}


def abyss_rewards(account: str) -> dict:
    acc = _require(account)
    if _abyss_snapshot(acc.email)["running"]:
        raise ApiError(409, "请在深渊战斗结束后领取奖励")
    try:
        with acc.lock:
            result = abyss_claim_rewards(acc.client)
    except GameError as error:
        raise ApiError(400, str(error)) from error
    return {**result, **_status_snapshot(acc)}


def abyss_run(account: str, scene_id, waves, auto_advance: bool = False) -> dict:
    """Persist a configuration and execute one Abyss battle or stage sequence."""
    acc = _require(account)
    try:
        normalized = normalize_abyss_config(acc.client.account_state or {}, scene_id, waves)
    except GameError as error:
        raise ApiError(400, str(error)) from error
    if auto_advance is True:
        normalized["autoAdvance"] = True
    cancel_event = threading.Event()
    with _abyss_sessions_lock:
        if acc.email in _abyss_sessions:
            raise ApiError(409, "当前账号已有深渊战斗正在运行")
        _abyss_sessions[acc.email] = cancel_event
        _abyss_progress.pop(acc.email, None)

    def on_progress(snapshot):
        progress = _battle_progress_brief(snapshot)
        with _abyss_sessions_lock:
            if _abyss_sessions.get(acc.email) is cancel_event:
                _abyss_progress[acc.email] = progress

    try:
        acc.params = {**acc.params, "abyss_auto": normalized}
        config.save_account(acc.email, toggles=acc.toggles, params=acc.params)
        acc.notify_config_changed()
        with acc.lock:
            if cancel_event.is_set():
                raise GameError("深渊战斗已中止，未自动重新开战")
            result = run_task(acc.client, "abyss_auto", {**normalized, "_cancel_event": cancel_event,
                                                       "_on_progress": on_progress})
    except GameError as error:
        raise ApiError(400, str(error)) from error
    finally:
        with _abyss_sessions_lock:
            if _abyss_sessions.get(acc.email) is cancel_event:
                del _abyss_sessions[acc.email]
                _abyss_progress.pop(acc.email, None)
    # Keep the standard task result shape so the existing log renderer can show
    # ``lines``/``detail`` directly while retaining the normalized config for
    # callers that need to inspect it.
    return {
        "ok": bool(result.get("ok", True)),
        "detail": result.get("detail", ""),
        "lines": result.get("lines", []),
        "config": normalized,
        "result": result,
        **_status_snapshot(acc),
    }


def get_config() -> dict:
    from .logutil import LOG_FILE
    pc = config.get_proxy_config()
    return {
        "version": config.APP_VERSION,      # 关于页版本徽章（前端不写死，见 backend/version.py）
        "platform": config.PLATFORM,
        "proxyMode": pc["mode"],
        "proxy": pc["manual"],
        "proxyEffective": pc["effective"],
        "requestDelay": config.get_request_delay_config(),
        "autoRelogin": config.get_auto_relogin_config(),
        "logPath": str(LOG_FILE),
        "downloadPath": ("系统下载文件夹" if config.PLATFORM == "android"
                         else str(config.get_download_directory())),
        "defaultDownloadPath": ("系统下载文件夹" if config.PLATFORM == "android"
                                else str(config.default_download_directory())),
    }


def get_frontend_settings() -> dict:
    return {"settings": config.load_frontend_settings()}


def set_frontend_settings(settings: dict | None) -> dict:
    try:
        return {"settings": config.save_frontend_settings(settings or {})}
    except (ValueError, RuntimeError) as error:
        raise ApiError(400, str(error)) from error


def _next_background_task_at_ms() -> int | None:
    """Find the earliest fixed task deadline across active account sessions.

    The background timer is a global, credential-free diagnostic endpoint, so
    it must not ask the browser to choose an account.  Active accounts already
    own the same task snapshots used by the scheduler; projecting their next
    un-clamped deadline gives the application clock its real schedule axis.
    Accounts without a fixed-time task are simply ignored and the timer keeps
    its lightweight open-ended fallback.
    """
    candidates: list[int] = []
    for account in accounts.all_accounts():
        getter = getattr(account, "next_fixed_task_at_ms", None)
        if not callable(getter):
            continue
        try:
            target = getter()
        except Exception:
            log.exception("[background-test] unable to read account task deadline")
            continue
        if target is None:
            continue
        try:
            candidates.append(int(target))
        except (TypeError, ValueError, OverflowError):
            continue
    return min(candidates) if candidates else None


def runtime_diagnostics(phase: str | None = None,
                        planned_duration_ms: float | None = None,
                        planned_wake_at_ms: float | None = None) -> dict:
    """Return the backend application clock and the wall clock.

    The endpoint is global and deliberately does not require an account.  The
    timer itself lives in a native Python ``Event.wait`` thread; a browser
    heartbeat cannot advance ``appElapsedMs``.
    """
    if phase == "start" and planned_duration_ms is None and planned_wake_at_ms is None:
        planned_wake_at_ms = _next_background_task_at_ms()
    if planned_duration_ms is None and planned_wake_at_ms is None:
        # Keep the no-argument call shape for lightweight integrations that
        # replace the timer provider in tests or embedded builds.
        result = background_timer.runtime_diagnostics(phase)
    else:
        result = background_timer.runtime_diagnostics(
            phase, planned_duration_ms, planned_wake_at_ms,
        )
    log.info(
        "[background-test] phase=%s snapshot=%s",
        phase if phase in {"start", "stop"} else "snapshot",
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )
    return result


def dev_unlock(passphrase: str | None) -> dict:
    """校验开发者模式口令。设置页那个无提示输入框每次输入都问这里。

    只回 {ok: bool}，不回提示文案——前端也不显示任何反馈：输对了控制台出现，
    输错了什么都不发生。
    """
    return {"ok": config.check_dev_pass(passphrase)}


def dev_request_history(account: str, passphrase: str | None = None) -> dict:
    """Return request replays and recent unique routes for the developer console."""
    if not config.check_dev_pass(passphrase):
        raise ApiError(403, "开发者模式未解锁")
    acc = _require(account)
    getter = getattr(acc.client, "recent_requests", None)
    route_getter = getattr(acc.client, "recent_routes", None)
    if callable(getter):
        try:
            history = getter(30)
        except TypeError:
            history = getter()
    else:
        history = []
    if callable(route_getter):
        try:
            routes = route_getter(30)
        except TypeError:
            routes = route_getter()
    else:
        routes = []
    return {
        "history": history,
        "routes": routes,
    }


def dev_call(account: str, route: str, data: dict | None, use_auth: bool = True,
             passphrase: str | None = None) -> dict:
    """开发者模式：用该账号的活会话发一条任意 route，回**未加工**的收发报文。

    与 GameClient.call 的区别（故意不复用它）：
      - 不吞明文消息：`call` 遇到非 JSON 响应会抛 GameMessage，只剩一句消息文本，
        原始 body 就没了。抓协议恰恰要看原始 body（例如 `CantReset` 在线上到底是
        裸串还是 `Msg_Msg` 重复形式，事后从异常反推不出来）。
      - 当前会话成功响应中的已确认道具余额走统一合并，货架等其他缓存不变。
    返回 {ok, request, status, rawText, json?, isJson, gzip, elapsedMs}。
    """
    from contextlib import nullcontext

    # 门禁：每次调用都验口令。前端把用户输过的口令存在本地、随请求带上——
    # 不是"解锁一次就永久放行"，因为服务端不留会话态（多端/重启后行为一致）。
    if not config.check_dev_pass(passphrase):
        raise ApiError(403, "开发者模式未解锁")
    acc = _require(account)
    route = (route or "").strip()
    if not route:
        raise ApiError(400, "route 不能为空")
    if not isinstance(data, dict):
        data = {}

    c = acc.client
    ensure_transport = getattr(c, "_ensure_request_transport", None)
    if callable(ensure_transport):
        ensure_transport()
    # Keep response application in the same order as normal game requests.
    # The inventory lock itself is only held briefly by the shared merger.
    with acc.lock:
        with getattr(c, "_call_serial_lock", nullcontext()):
            return _dev_call_locked(c, route, data, use_auth)


def _dev_call_locked(c, route: str, data: dict, use_auth: bool) -> dict:
    import json as _json
    import time as _time
    import gzip as _gzip

    # use_auth：自动补 AID/SessionID（绝大多数 route 都要）。关掉可试探免登录接口。
    sent = c._auth_data(data) if use_auth else dict(data)
    payload = {"data": sent, "route": route}
    body = _json.dumps(payload).encode("utf-8")
    # 控制台请求不走 GameClient.call（需要保留原始响应），这里单独写入
    # 完整请求历史；兼容旧的假 client，它们可能只有 record_route。
    history_id = None
    record_request = getattr(c, "record_request", None)
    if callable(record_request):
        try:
            history_id = record_request(
                route,
                sent,
                request_payload=payload,
                source="console",
                allow_login=True,
            )
        except TypeError:
            history_id = record_request(route, sent)
    else:
        record_route = getattr(c, "record_route", None)
        if callable(record_route):
            record_route(route)

    t0 = _time.time()
    try:
        c._sync_proxy()
        request_http = getattr(c, "_request_http", None)
        if callable(request_http):
            resp = request_http("PUT", config.GAME_ROUTER, content=body)
        else:
            # Compatibility for lightweight test doubles predating pacing.
            resp = c._client.request("PUT", config.GAME_ROUTER, content=body)
    except Exception as error:
        update_history = getattr(c, "update_request_history", None)
        if callable(update_history):
            update_history(history_id, error=str(error))
        raise
    elapsed = int((_time.time() - t0) * 1000)

    raw = resp.content
    was_gzip = raw[:2] == b"\x1f\x8b"
    if was_gzip:
        try:
            raw = _gzip.decompress(raw)
        except Exception:
            pass
    text = raw.decode("utf-8", "replace")
    out = {
        "ok": True,
        "request": payload,          # 原样回显真正发出去的东西（含补上的鉴权字段）
        "status": resp.status_code,
        "rawText": text[:200_000],   # 大响应截断，避免把前端卡死
        "rawLen": len(text),
        "isJson": False,
        "gzip": was_gzip,
        "elapsedMs": elapsed,
        "route": route,
    }
    try:
        out["json"] = _json.loads(text)
        out["isJson"] = True
    except _json.JSONDecodeError:
        pass
    if is_duplicate_login_message(text):
        mark_disconnected = getattr(c, "_mark_disconnected", None)
        if callable(mark_disconnected):
            mark_disconnected("duplicate_login")
        log.warning("[dev] %s returned Duplicate Login; connection marked disconnected", route)
    elif (200 <= resp.status_code < 300 and isinstance(out.get("json"), dict)
          and route != "AccountHandler.Login" and c.aid and c.session_id
          and sent.get("AID") == c.aid and sent.get("SessionID") == c.session_id):
        apply_items = getattr(c, "_apply_now_items", None)
        if callable(apply_items):
            apply_items(out["json"])
    update_history = getattr(c, "update_request_history", None)
    if callable(update_history):
        update_history(
            history_id,
            response=out.get("json"),
            response_text=text,
            status=resp.status_code,
            is_json=out["isJson"],
            gzip=was_gzip,
        )
    log.info("[dev] %s -> HTTP %s %s %d 字节%s", route, resp.status_code,
             "JSON" if out["isJson"] else "明文", len(text),
             " (gzip)" if was_gzip else "")
    return out


def get_logs(lines: int = 300) -> dict:
    """返回开发者日志末尾若干行，供应用内查看/复制发给开发者。"""
    from .logutil import LOG_FILE
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        tail = "".join(text.splitlines(keepends=True)[-lines:])
        return {"path": str(LOG_FILE), "content": tail}
    except FileNotFoundError:
        return {"path": str(LOG_FILE), "content": "（暂无日志）"}
    except Exception as e:
        return {"path": str(LOG_FILE), "content": f"（读取失败：{e}）"}


def save_logs() -> dict:
    """Save all retained developer logs on Windows; Android uses its native bridge."""
    if config.PLATFORM == "android":
        raise ApiError(400, "Android 端需要通过系统文件接口保存日志")
    try:
        from .logutil import save_developer_logs
        target = save_developer_logs()
        return {"ok": True, "path": str(target), "name": target.name}
    except Exception as error:
        raise ApiError(400, f"保存开发者日志失败：{error}") from error


_EQUIPMENT_FILTER_EXPORT_FORMAT = "ark-equipment-filter-scheme"
_EQUIPMENT_FILTER_EXPORT_VERSION = 2
_EQUIPMENT_FILTER_EXPORT_MAX_BYTES = 1024 * 1024


def _equipment_filter_export_filename(name: str) -> str:
    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    safe_name = safe_name[:80].rstrip(" .") or "未命名方案"
    return f"LucimaTools-equipment-filter-{safe_name}.json"


def export_equipment_filter_scheme(payload: dict) -> dict:
    """Save one equipment-filter scheme to the configured download directory."""
    if config.PLATFORM == "android":
        raise ApiError(400, "Android 端需要通过系统文件接口导出方案")
    if not isinstance(payload, dict):
        raise ApiError(400, "导出内容不是有效的筛选方案")
    if payload.get("format") != _EQUIPMENT_FILTER_EXPORT_FORMAT:
        raise ApiError(400, "导出方案格式无效")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= _EQUIPMENT_FILTER_EXPORT_VERSION:
        raise ApiError(400, "导出方案版本无效")
    scheme = payload.get("scheme")
    if not isinstance(scheme, dict):
        raise ApiError(400, "导出方案数据缺失")
    name = scheme.get("name")
    slots = scheme.get("slots")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "导出方案名称缺失")
    if not isinstance(slots, dict):
        raise ApiError(400, "导出方案的部位规则缺失")

    export_data = {
        "format": _EQUIPMENT_FILTER_EXPORT_FORMAT,
        "version": version,
        "scheme": scheme,
    }
    contents = (json.dumps(export_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(contents) > _EQUIPMENT_FILTER_EXPORT_MAX_BYTES:
        raise ApiError(400, "方案文件超过 1 MB，无法导出")
    try:
        directory = config.get_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        base = directory / _equipment_filter_export_filename(name.strip())
        target = base
        suffix = 1
        while target.exists():
            target = directory / f"{base.stem}-{suffix}{base.suffix}"
            suffix += 1
        target.write_bytes(contents)
        return {"ok": True, "path": str(target), "name": target.name}
    except OSError as error:
        raise ApiError(400, f"导出装备筛选方案失败：{error}") from error


_ENCYCLOPEDIA_IMAGE_MAX_BYTES = 32 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _encyclopedia_image_filename(character_id: str, name: str) -> str:
    """Build a filesystem-safe, human-readable encyclopedia image name."""
    def safe_part(value: str, fallback: str, limit: int) -> str:
        value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
        return (value[:limit].rstrip(" .") or fallback)

    safe_id = safe_part(character_id, "unknown", 40)
    safe_name = safe_part(name, "未命名角色", 80)
    return f"LucimaTools-encyclopedia-{safe_id}-{safe_name}.png"


def save_encyclopedia_image(payload: dict) -> dict:
    """Save a rendered static encyclopedia card to the configured download folder."""
    if config.PLATFORM == "android":
        raise ApiError(400, "Android 端需要通过系统文件接口保存图鉴图片")
    if not isinstance(payload, dict):
        raise ApiError(400, "图鉴图片数据无效")

    character_id = payload.get("characterId")
    name = payload.get("name")
    encoded = payload.get("pngBase64")
    if not isinstance(character_id, str) or not character_id.strip():
        raise ApiError(400, "图鉴角色编号缺失")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "图鉴角色名称无效")
    if not isinstance(encoded, str) or not encoded:
        raise ApiError(400, "图鉴图片数据缺失")
    # Reject oversized input before decoding so a malformed request cannot
    # allocate an unbounded buffer. Whitespace/data URLs are intentionally not
    # accepted: the frontend contract sends the raw base64 payload only.
    encoded_limit = ((_ENCYCLOPEDIA_IMAGE_MAX_BYTES + 2) // 3) * 4
    if len(encoded) > encoded_limit:
        raise ApiError(400, "图鉴图片超过 32 MB，无法保存")
    try:
        contents = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ApiError(400, "图鉴图片不是有效的 Base64 数据") from error
    if len(contents) > _ENCYCLOPEDIA_IMAGE_MAX_BYTES:
        raise ApiError(400, "图鉴图片超过 32 MB，无法保存")
    if not contents.startswith(_PNG_SIGNATURE):
        raise ApiError(400, "图鉴图片不是有效的 PNG 文件")

    try:
        directory = config.get_download_directory()
        directory.mkdir(parents=True, exist_ok=True)
        base = directory / _encyclopedia_image_filename(
            character_id.strip(), name.strip()
        )
        suffix = 0
        while True:
            target = base if suffix == 0 else directory / (
                f"{base.stem}-{suffix}{base.suffix}"
            )
            try:
                # Exclusive creation preserves the no-overwrite guarantee even
                # when two browser clicks arrive concurrently.
                with target.open("xb") as handle:
                    handle.write(contents)
                break
            except FileExistsError:
                suffix += 1
        return {"ok": True, "path": str(target), "name": target.name}
    except OSError as error:
        raise ApiError(400, f"保存图鉴图片失败：{error}") from error


def set_config(proxy: str | None, proxy_mode: str | None = None,
               download_path: str | None = None,
               reset_download_path: bool = False,
               request_delay_enabled=None,
               request_delay_ms=None,
               request_delay_jitter=None,
               auto_relogin_enabled=None,
               auto_relogin_delay_seconds=None) -> dict:
    config.set_proxy_config(mode=proxy_mode, manual=proxy)
    try:
        config.set_request_delay_config(
            enabled=request_delay_enabled,
            delay_ms=request_delay_ms,
            jitter_percent=request_delay_jitter,
        )
    except (ValueError, RuntimeError) as error:
        raise ApiError(400, str(error)) from error
    try:
        auto_relogin = config.set_auto_relogin_config(
            enabled=auto_relogin_enabled,
            delay_seconds=auto_relogin_delay_seconds,
        )
    except (ValueError, RuntimeError) as error:
        raise ApiError(400, str(error)) from error
    if auto_relogin_enabled is not None or auto_relogin_delay_seconds is not None:
        for account in accounts.all_accounts():
            account._wake.set()
    try:
        if reset_download_path:
            config.set_download_directory(None)
        elif download_path is not None:
            config.set_download_directory(download_path)
    except (ValueError, RuntimeError) as error:
        raise ApiError(400, str(error)) from error
    pc = config.get_proxy_config()
    request_delay = config.get_request_delay_config()
    android = config.PLATFORM == "android"
    return {
        "proxyMode": pc["mode"], "proxy": pc["manual"], "proxyEffective": pc["effective"],
        "requestDelay": request_delay,
        "autoRelogin": auto_relogin,
        "downloadPath": ("系统下载文件夹" if android
                         else str(config.get_download_directory())),
        "defaultDownloadPath": ("系统下载文件夹" if android
                                else str(config.default_download_directory())),
    }
