"""平台无关的核心业务逻辑。

本模块不依赖任何 Web 框架。每个函数对应一个 API 端点，返回可 JSON 序列化的
dict；出错时抛 ApiError(status, message)，由 server 层转成对应的 HTTP 响应。
Windows(server.py) 与 Android(Chaquopy) 共用本模块。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config
from . import accounts
from .logutil import log
from .game_client import GameClient, GameError
from .portal import portal_login, PortalError
from .tasks import (
    TASKS, run_task, run_auto_tasks, run_toggle_tasks, toggle_tasks, manual_tasks,
    shop_reset, shop_buy_index, UnknownEquipmentError,
    HUNT_SWEEP_CANDIDATES, SWEEP_KINDS, list_teams, sweep_once, next_claim_times,
    activity_scenes, query_support_friends, activity_once,
    support_brief, trim_support_entry,
    store_shelves, store_buy,
    item_name,
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


def _roster(state: dict) -> dict:
    """从登录快照抽取团员与物品清单，供基本信息页展示。

    团员来自 RoleDataContainer.Roles；物品来自 ItemContainer.Items，
    但排除已在顶部单独展示的 7 种货币（金币/钻石/能源/旗帜/招募/神秘/银河），
    避免与顶部资产列重复。
    """
    rank = _item_rank()

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
            "star": r.get("Star"),
            "awaken": r.get("AwakenLV"),
        })
    # 排序：星级 > 潜能 > 等级 > ID
    heroes.sort(key=lambda h: (-(h["star"] or 0), -(h["imprint"] or 0),
                               -(h["level"] or 0), h["id"]))

    shown = set(CURRENCY_IDS.values())  # 顶部已展示的货币，物品栏不重复
    items = []
    for it in state.get("ItemContainer", {}).get("Items", []):
        sid = str(it.get("StaticID", ""))
        if sid in shown:
            continue
        items.append({
            "id": sid,
            "name": item_name(sid),
            "count": it.get("Count", 0),
            "rank": rank.get(sid, 0),
        })
    # 排序：稀有度高在前，同稀有度按 ID
    items.sort(key=lambda i: (-(i["rank"] or 0), i["id"]))

    return {"heroes": heroes, "items": items}


def roster(account: str) -> dict:
    """GET /api/roster —— 返回某账号的团员 + 物品清单（基本信息页按需拉取）。

    不塞进 summarize()/_status_snapshot()，避免每次刷商店/扫荡响应都附带上百 KB
    团员/物品数据。只有基本信息页在登录切号 / 手动刷新 / 首次进入时拉一次。
    """
    acc = accounts.get(account or "")
    if not acc or not acc.client.account_state:
        raise ApiError(401, "账号未登录")
    return _roster(acc.client.account_state)


# ---------- bootstrap / 登录 ----------
def _bootstrap_and_register(email: str, game_token: str, portal: dict | None) -> accounts.Account:
    """用 token 建立游戏会话 → 登记为在线账号 → 启动其后台调度线程。返回 Account。"""
    client = GameClient(game_token)
    try:
        client.bootstrap()
    except Exception as e:
        log.exception("游戏 bootstrap 失败: %s", e)
        raise ApiError(400, f"游戏登录失败: {e}")
    return accounts.add(email, client, portal)


def _account_payload(acc: accounts.Account, auto_results: list | None = None) -> dict:
    """登录/切换账号后返回给前端的统一负载。"""
    state = acc.client.account_state or {}
    payload = {
        "account": acc.email,
        "status": summarize(state),
        "nextClaims": next_claim_times(state),
        "toggles": acc.toggles,
        "params": acc.params,
        "logSeq": acc.log_seq,
    }
    if acc.portal:
        payload["portal"] = {
            "nickname": acc.portal.get("nickname") or acc.portal.get("nickName"),
            "userId": acc.portal.get("userId"),
        }
    if auto_results is not None:
        payload["autoResults"] = auto_results
    return payload


def _require(account: str | None) -> accounts.Account:
    acc = accounts.get(account or "")
    if not acc:
        raise ApiError(401, "账号未登录或已下线")
    return acc


# ---------- 端点处理（平台无关） ----------
def login(account: str, password: str, save_pwd: bool = False) -> dict:
    """辅助登录（Windows：Playwright 弹 Edge 手点验证码）。Android 不走这里。

    密码为空时回退用存档里保存的密码（前端选了"已保存账号"就不必再输密码）。
    """
    account = (account or "").strip()
    if not account:
        raise ApiError(400, "账号不能为空")
    if not password:
        saved = config.load_accounts().get(account, {})
        if saved.get("password"):
            password = saved["password"]
            save_pwd = True   # 用存档密码登录 → 保持已保存
        else:
            raise ApiError(400, "请输入密码")
    try:
        from .browser_login import interactive_portal_login, BrowserLoginError
    except Exception as e:
        raise ApiError(500, f"辅助登录不可用（缺少 Playwright）：{e}")
    try:
        p = interactive_portal_login(account, password)
    except BrowserLoginError as e:
        log.warning("辅助登录失败: %s", e)
        raise ApiError(400, f"{e}")
    except Exception as e:
        log.exception("辅助登录异常: %s", e)
        raise ApiError(500, f"辅助登录出错：{e}")
    acc = _bootstrap_and_register(account, p["jwt"], portal={"account": account})
    config.save_account(account, password=password, save_pwd=save_pwd)
    auto = _run_login_tasks(acc)
    return _account_payload(acc, auto_results=auto)


def login_direct(account: str, password: str, turnstile_token: str | None,
                 save_pwd: bool = False) -> dict:
    account = (account or "").strip()
    try:
        p = portal_login(account, password, turnstile_token)
    except PortalError as e:
        raise ApiError(400, f"门户登录失败: {e}")
    acc = _bootstrap_and_register(account, p["jwt"], portal=p)
    config.save_account(account, password=password, save_pwd=save_pwd)
    auto = _run_login_tasks(acc)
    return _account_payload(acc, auto_results=auto)


def login_token(game_token: str, account: str | None = None,
                password: str = "", save_pwd: bool = False) -> dict:
    """直接用 erolabsjwt bootstrap。Android 端 WebView 拿到 cookie 后走这里。
    account 缺省时用一个占位 email。

    与桌面 login()/login_direct() 对齐：登录成功后调 config.save_account 把账号
    写进存档注册表（这样登录页"已保存账号"下拉里才有它、退出后仍可选），save_pwd
    为真则同时保存密码。此前 Android 路径漏了这步，导致密码不保存、账号无法复选。
    """
    email = (account or "").strip() or "android"
    acc = _bootstrap_and_register(email, game_token, portal=None)
    if email != "android":   # 占位账号不写存档
        config.save_account(email, password=password, save_pwd=save_pwd)
    auto = _run_login_tasks(acc)
    return _account_payload(acc, auto_results=auto)


def saved_password(account: str) -> str:
    """取某账号存档里保存的明文密码（供 Android 登录 WebView 自动填表用）。

    前端只知道 hasPwd 布尔，拿不到明文（明文只在 settings.json）；Android 选
    "已保存账号"重登时密码框为空，Kotlin 需据此取回真实密码填 ero-labs 表单。
    """
    rec = config.load_accounts().get((account or "").strip(), {})
    return rec.get("password") or ""


def _run_login_tasks(acc: accounts.Account) -> list:
    """登录必跑任务（月签/活动签/助战/月卡投递）+ 立即按开关跑一次到点项。

    两类结果**都直接返回给前端**，各带自己的日志前缀（always→"自动·"，开关→"登录·"）。

    ⚠️ 开关项以前只写日志缓冲、指望前端轮询 `/api/logs/auto?since=` 拉走，结果一条都
    显示不出来：`_account_payload` 是在本函数**之后**读 `acc.log_seq` 的，游标已经越过
    了这几条，`logs_since(游标)` 永远返回空。所以登录时跑的动力转换/成长药剂/升星水晶/
    技能模块/作战派遣全都静默消失，只有 always 任务（搭 autoResults 返回）看得见。
    现在直接随响应返回，前端即时渲染；缓冲里那几条被游标跳过正好不会重复显示。
    """
    with acc.lock:
        auto = run_auto_tasks(acc.client)
    results = [{**r, "prefix": "自动·"} for r in auto]
    # 登录后立即按已开启的开关跑一次到点该跑的
    try:
        due = acc.run_due("登录·")
    except Exception as e:
        log.exception("账号 %s 登录后跑开关异常: %s", acc.email, e)
    else:
        # skipped（未到点/未满，没发请求）不入日志，与后台巡检口径一致
        results += [{**r, "prefix": "登录·"} for r in due if not r.get("skipped")]
    return results


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
            "savePwd": bool(rec.get("save_pwd")),
            "hasPwd": bool(rec.get("save_pwd") and rec.get("password")),
        }
        if acc:
            st = summarize(acc.client.account_state or {})
            item["name"] = st.get("name")
            item["level"] = st.get("level")
            item["avatar"] = st.get("avatar")
            item["toggles"] = acc.toggles
            item["params"] = acc.params
            item["logSeq"] = acc.log_seq
        items.append(item)
    return {"accounts": items}


def logout(account: str) -> dict:
    """下线一个在线账号（保留存档，可再连接）。"""
    accounts.remove(account)
    return {"ok": True}


def delete_account(account: str) -> dict:
    """删除账号：下线 + 清除存档（密码/开关）。"""
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
    acc = _require(account)
    if isinstance(toggles, dict):
        acc.toggles = {k: bool(v) for k, v in toggles.items()}
    if isinstance(params, dict):
        merged = dict(acc.params)
        merged.update(params)
        acc.params = merged
    config.save_account(account, toggles=acc.toggles, params=acc.params)
    acc.notify_config_changed()   # 调度线程立刻重算档期，不必等下一个到点时刻
    return {"ok": True, "toggles": acc.toggles, "params": acc.params}


def auto_logs(account: str, since: int = 0) -> dict:
    """拉取某账号后台调度产生的新日志（seq > since），供前端增量显示。"""
    acc = accounts.get(account or "")
    if not acc:
        return {"entries": [], "logSeq": 0}
    return {"entries": acc.logs_since(since), "logSeq": acc.log_seq}


def _status_snapshot(acc) -> dict:
    """从当前(可能已被 NowItem 增量更新的)缓存 account_state 生成 status 快照。

    操作类接口(刷商店/扫荡)复用它把最新货币/资源随响应一起返回，前端每次操作后
    可立即 applyStatus，零额外请求实时反映——招募契约等背包物品同金币一样走
    NowItem 增量，故购买后余额会即时变化。
    """
    st = acc.client.account_state
    if not st:
        return {}
    return {"status": summarize(st), "nextClaims": next_claim_times(st)}


def status(account: str) -> dict:
    acc = accounts.get(account or "")
    if not acc or not acc.client.account_state:
        raise ApiError(401, "账号未登录")
    # 带上竞技场档期：前端后台轮询(每 8s 有新巡检日志时)据此刷新展开页的倒计时，
    # 让后台扫荡完成后"下次挑战时间"自动跟上。不塞进 _status_snapshot——那个还被
    # 刷商店/扫荡的响应复用，没必要每次操作都捎带这份。
    return {**_status_snapshot(acc), "arena": arena_state(acc)}


def refresh_status(account: str) -> dict:
    """手动刷新：重新 AccountHandler.Login 拉全量账号状态。

    游戏没有单独查背包/货币的轻量接口，只有重登能拿到含游戏客户端内变化 + 体力恢复的
    权威全量。故此接口只在用户主动点"刷新"时调用，不做自动轮询（避免频繁重登）。
    """
    acc = _require(account)
    try:
        with acc.lock:
            state = acc.client.bootstrap()
    except Exception as e:
        log.exception("刷新（重登）失败: %s", e)
        raise ApiError(400, f"刷新失败: {e}")
    # 竞技场的失败退避在刷新时清零（用户要求"刷新和重登都立即重置冷却直接扫荡"）
    acc.reset_task_runtime()
    return {
        "status": summarize(state),
        "nextClaims": next_claim_times(state),
        "arena": arena_state(acc),
    }


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
    snap_map = arena_next_times(acc.client.account_state or {})
    params = (acc.params.get("arena_npc") or {})
    picked = [int(x) for x in (params.get("picked") or [])]
    items = []
    for it in arena_npcs():
        sid = it["sceneId"]
        nxt = max(snap_map.get(sid) or 0, next_map.get(sid) or 0) or None
        items.append({
            **it,
            "picked": it["n"] in picked,
            "nextMs": nxt,
            "retryMs": retry_map.get(sid),
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
        merged.update(params)
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


def shop_reset_ep(account: str, use_gold: bool, auto_buy: list[str]) -> dict:
    acc = _require(account)
    with acc.lock:
        r = shop_reset(acc.client, use_gold, auto_buy)
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


def store_options(account: str) -> dict:
    """商店购买的货架（VIP礼包/友情/荣誉勋章）+ 当前相关货币余额。

    货架读的是登录快照，不额外发请求（游戏本身也是从登录快照渲染这三家商店的，
    R.har/buy.har 里打开商店界面都没有任何查询 route）。余额随快照一起给，前端
    卡片上直接对照"够不够买"。
    """
    acc = _require(account)
    state = acc.client.account_state or {}
    shelves = store_shelves(state)
    items = {str(it.get("StaticID")): int(it.get("Count") or 0)
             for it in state.get("ItemContainer", {}).get("Items", [])}
    wallet = {s["currency"]: items.get(s["currency"], 0) for s in shelves}
    return {"stores": shelves, "wallet": wallet}


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
    scenes = SWEEP_KINDS.get(kind, HUNT_SWEEP_CANDIDATES)
    return {"scenes": scenes, "teams": list_teams(acc.client)}


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
    """活动关卡选项：当期关卡族 + 队伍 + 助战好友 + 助战收藏夹。

    助战列表原始条目缓存到 Account.support_cache 供 activity_run 构建 payload。
    收藏夹(按账号存，见 config.load_support_favs)里的人**即使已不在助战列表**也照样
    返回，用存下来的快照打；若他还在列表里，则用列表新快照覆盖存档(自动刷新，对方
    升级/换装后数据保持最新)。
    """
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


def activity_run(account: str, scene_id: str, team_id: str,
                 support_cuid: str | None = None, quick_battle: bool = True) -> dict:
    acc = _require(account)
    entry = None
    if support_cuid:
        # 先查在线助战列表，再回落到收藏夹快照（收藏的人可能已不在列表里）
        entry = acc.support_cache.get(str(support_cuid)) \
            or config.load_support_favs(acc.email).get(str(support_cuid))
        if entry is None:
            raise ApiError(400, "助战成员已失效，请重新展开选项刷新助战列表")
    try:
        with acc.lock:
            r = activity_once(acc.client, scene_id, team_id, entry, quick_battle)
    except GameError as e:
        raise ApiError(400, str(e))
    r.update(_status_snapshot(acc))
    return r


def get_config() -> dict:
    from .logutil import LOG_FILE
    pc = config.get_proxy_config()
    return {
        "version": config.APP_VERSION,      # 关于页版本徽章（前端不写死，见 backend/version.py）
        "turnstileSiteKey": config.TURNSTILE_SITE_KEY,
        "platform": config.PLATFORM,
        "proxyMode": pc["mode"],
        "proxy": pc["manual"],
        "proxyEffective": pc["effective"],
        "logPath": str(LOG_FILE),
    }


def dev_unlock(passphrase: str | None) -> dict:
    """校验开发者模式口令。设置页那个无提示输入框每次输入都问这里。

    只回 {ok: bool}，不回提示文案——前端也不显示任何反馈：输对了控制台出现，
    输错了什么都不发生。
    """
    return {"ok": config.check_dev_pass(passphrase)}


def dev_call(account: str, route: str, data: dict | None, use_auth: bool = True,
             passphrase: str | None = None) -> dict:
    """开发者模式：用该账号的活会话发一条任意 route，回**未加工**的收发报文。

    与 GameClient.call 的区别（故意不复用它）：
      - 不吞明文消息：`call` 遇到非 JSON 响应会抛 GameMessage，只剩一句消息文本，
        原始 body 就没了。抓协议恰恰要看原始 body（例如 `CantReset` 在线上到底是
        裸串还是 `Msg_Msg` 重复形式，事后从异常反推不出来）。
      - 不做任何写回：不碰 ItemContainer / 货架缓存，免得手工试探污染账号状态快照。
    返回 {ok, request, status, rawText, json?, isJson, gzip, elapsedMs}。
    """
    import json as _json
    import time as _time
    import gzip as _gzip

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
    # use_auth：自动补 AID/SessionID（绝大多数 route 都要）。关掉可试探免登录接口。
    sent = c._auth_data(data) if use_auth else dict(data)
    payload = {"data": sent, "route": route}
    body = _json.dumps(payload).encode("utf-8")

    t0 = _time.time()
    with acc.lock:
        c._sync_proxy()
        resp = c._client.request("PUT", config.GAME_ROUTER, content=body)
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


def set_config(proxy: str | None, proxy_mode: str | None = None) -> dict:
    config.set_proxy_config(mode=proxy_mode, manual=proxy)
    pc = config.get_proxy_config()
    return {"proxyMode": pc["mode"], "proxy": pc["manual"], "proxyEffective": pc["effective"]}
