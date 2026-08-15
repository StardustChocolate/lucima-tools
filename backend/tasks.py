"""日常任务定义。

每个任务是一个函数 (client) -> 结果dict，并在 TASKS 里注册元信息。
新抓到的 route 只要在这里加一个函数即可扩展。
"""
from __future__ import annotations

import inspect
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Callable

from .game_client import GameClient, GameError, GameMessage


class UnknownEquipmentError(Exception):
    """装备的 StaticID 未收录在图鉴映射(backend/equip_ref.json)里。

    不允许用数值区间猜测等级/品质兜底——数据不全就该暴露问题、中止当前档位处理，
    而不是给出可能错的判定（错把非85传说当85去手选，或漏掉真正的85）。
    """

    def __init__(self, static_id: str):
        self.static_id = static_id
        super().__init__(f"未知装备 StaticID={static_id}，图鉴数据未收录，无法判定等级")

# 表示"已经做过/无需再做"的明文消息(视为成功)
_DONE_HINTS = ("Finish", "Already", "Received", "Repeat", "Done", "已", "重复")

# 物品/装备 StaticID -> 官方中文名。
# 数据源：游戏内官方本地化表，发布数据位于 backend/item_names.json
# （1668 条，覆盖货币/道具/箱子/芯片/神器/全部装备名）。未知的回退为"道具#ID"。
# ⚠️ 名称一律以官方 loc 为准（含 91=经验值 而非旧记的金币、3=能源、26=荣誉勋章 等修正）。
_ITEM_NAMES = None


def _item_names() -> dict:
    global _ITEM_NAMES
    if _ITEM_NAMES is None:
        p = os.path.join(os.path.dirname(__file__), "item_names.json")
        try:
            with open(p, encoding="utf-8") as f:
                _ITEM_NAMES = json.load(f)
        except Exception:
            _ITEM_NAMES = {}
    return _ITEM_NAMES


def item_name(sid) -> str:
    return _item_names().get(str(sid), f"道具#{sid}")


def _fmt_items(counter: dict) -> str:
    """把 {StaticID: 总数} 拼成 '550体力、100金币' 这种可读串。"""
    if not counter:
        return "无产出"
    return "、".join(f"{cnt}{item_name(sid)}" for sid, cnt in counter.items())


def _collect_drop(mail: dict, counter: dict):
    """把一封邮件的 DropResult.Items 累加进 counter。"""
    for it in (mail.get("DropResult", {}) or {}).get("Items", []):
        item = it.get("Item", {}) or {}
        sid = item.get("StaticID")
        cnt = item.get("Count", 0)
        if sid is not None:
            counter[sid] = counter.get(sid, 0) + cnt


def _is_done_msg(msg: str) -> bool:
    return any(h.lower() in msg.lower() for h in _DONE_HINTS)


def _oid(v):
    return v.get("$oid") if isinstance(v, dict) else v


def _date_str(v):
    """把 {$date: ms} 转成游戏用的非补零字符串 '2024/2/1 10:52:49'。

    用 epoch+timedelta 计算，兼容 .NET 最小时间戳(-62135596800000, 即 0001/1/1)——
    Windows 上 datetime.fromtimestamp 对负值会抛 OSError。
    """
    if isinstance(v, dict):
        ms = v.get("$date")
    else:
        ms = v
    if ms is None:
        return v
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)
    return f"{dt.year:04d}/{dt.month}/{dt.day} {dt.hour}:{dt.minute}:{dt.second}"


def task_month_signin(c: GameClient) -> dict:
    """每日月签到。只需 AID+SessionID。奖励发放至邮箱。"""
    try:
        res = c.call("MonthSignInHandler.SignIn", c._auth_data())
    except GameMessage as m:
        ok = _is_done_msg(m.msg)
        return {"ok": ok, "detail": ("今日已签到" if ok else f"签到失败：{m.msg}"), "raw": m.msg}
    return {"ok": True, "detail": f"月签到成功（第 {res.get('NowDay')} 天），奖励已发至邮箱", "raw": res}


def task_week_signin(c: GameClient) -> dict:
    """活动周签到。需先从账号状态取当前签到记录再提交。"""
    container = (c.account_state or {}).get("WeekSignInDataContainer", {})
    records = container.get("WeekSignInDataList") or []
    if not records:
        return {"ok": False, "detail": "无可用的周签到活动", "raw": container}

    results = []
    for rec in records:
        data = c._auth_data({
            "_id": _oid(rec.get("_id")),
            "SignInDay": rec.get("SignInDay"),
            "ActivityID": rec.get("ActivityID"),
            "CreateTime": _date_str(rec.get("CreateTime")),
            "LastLoginTime": _date_str(rec.get("LastLoginTime")),
            "LastSignInTime": _date_str(rec.get("LastSignInTime")),
            "LastContinuousSignInTime": _date_str(rec.get("LastContinuousSignInTime")),
        })
        try:
            res = c.call("WeekSignInHandler.SignIn", data)
            results.append({"activity": rec.get("ActivityID"), "day": res.get("SignInDay")})
        except GameMessage as m:
            if _is_done_msg(m.msg):
                results.append({"activity": rec.get("ActivityID"), "done": m.msg})
            else:
                results.append({"activity": rec.get("ActivityID"), "error": m.msg})
        except GameError as e:
            results.append({"activity": rec.get("ActivityID"), "error": str(e)})
    signed = [r for r in results if "error" not in r]
    return {"ok": bool(signed), "detail": f"周签到处理 {len(signed)}/{len(results)} 个活动", "raw": results}


def _mail_id(m: dict):
    v = m.get("_id")
    return v.get("$oid") if isinstance(v, dict) else v


def _expiry_ms(m: dict):
    e = m.get("ExpiryTime")
    return e.get("$date") if isinstance(e, dict) else e


def task_send_meal(c: GameClient) -> dict:
    """月卡/定时投喂：触发 TimingMealHandler.SentMeal（只需 AID+SessionID）。

    与月签到/周签到同样的模式——这个动作本身不直接给奖励，而是把"距上次投递以来
    累计的定时邮件"发进邮箱（月卡 GIFTMonth01/02/03、午/晚餐 Lunch/Dinner），
    之后靠"一键领邮件"领取。游戏客户端在主界面会自动调它；我们做成登录必跑任务，
    这样登录后月卡/餐食奖励就会自动发到邮箱，等领邮件时一并领到。

    响应 Drop 为空（奖励在邮件里）、只更新 LastSentMailTime + HasMail。
    """
    try:
        c.call("TimingMealHandler.SentMeal", c._auth_data())
    except GameMessage as m:
        ok = _is_done_msg(m.msg)
        return {"ok": ok, "detail": ("月卡/餐食已投递" if ok else f"投递失败：{m.msg}"), "raw": m.msg}
    return {"ok": True, "detail": "月卡奖励已发放至邮箱（请领邮件领取）", "raw": None}


def task_claim_mail(c: GameClient) -> dict:
    """一键领取所有未领且未过期的邮件。

    两个坑（均已实测确认）：
    1. 登录响应的 MailContainer 只是"最近若干封"的窗口(实测约36封)，不是全量。
       真正的全量要靠 QueryNewestMails —— 它返回"比游标 MailID 更新的所有邮件"。
       传空 MailID 取不到全量；必须传一个足够老的邮件 id 当游标才能拉全。
    2. 原生 ReceivedMails 若列表含任一过期邮件，整批被拒(Error_MailExpiry)导致漏领，
       故领取前剔除已过期邮件。
    """
    import time
    now_ms = time.time() * 1000

    # 先收集登录窗口里的邮件
    by_id: dict[str, dict] = {}
    window = (c.account_state or {}).get("MailContainer", {}).get("Mails", [])
    for m in window:
        mid = _mail_id(m)
        if mid:
            by_id[mid] = m

    # 用窗口里最老的一封当游标，QueryNewestMails 会返回比它更新的全部邮件（含它之后所有）。
    def _sent_ms(m):
        s = m.get("SentTime")
        return s.get("$date") if isinstance(s, dict) else (s or 0)
    cursor = ""
    if window:
        oldest = min(window, key=_sent_ms)
        cursor = _mail_id(oldest) or ""
    try:
        q = c.call("MailHandler.QueryNewestMails", c._auth_data({"MailID": cursor}))
        for m in q.get("Mails", []):
            mid = _mail_id(m)
            if mid:
                by_id[mid] = m
    except GameMessage:
        pass

    # 未领 + 未过期
    valid, expired = [], 0
    for mid, m in by_id.items():
        if m.get("IsReceived"):
            continue
        exp = _expiry_ms(m)
        if exp is not None and exp <= now_ms:
            expired += 1
            continue
        valid.append(mid)

    if not valid:
        tail = f"（{expired} 封已过期跳过）" if expired else ""
        return {"ok": True, "detail": f"没有可领取的邮件{tail}", "raw": {"expired": expired}}

    try:
        res = c.call("MailHandler.ReceivedMails", c._auth_data({"MailIDList": valid}))
    except GameMessage as msg:
        return {"ok": False, "detail": f"领取失败：{msg.msg}", "raw": msg.msg}

    got = res.get("Results", [])
    # 更新本地快照，避免同会话重复领取
    for m in by_id.values():
        if _mail_id(m) in valid:
            m["IsReceived"] = True

    # 汇总所有领到的物品
    counter: dict = {}
    for r in got:
        _collect_drop(r.get("Mail", {}) or {}, counter)

    lines = [f"领取了 {_fmt_items(counter)}"]
    summary = f"共领取 {len(got)} 封邮件"
    if expired:
        summary += f"，跳过 {expired} 封已过期"
    lines.append(summary)
    return {"ok": True, "detail": summary, "lines": lines, "raw": got}


def _collect_items(items: list, counter: dict):
    """把一组 Drop.Items（[{Item:{StaticID,Count}}]）累加进 counter。"""
    for it in items or []:
        item = it.get("Item", {}) or {}
        sid = item.get("StaticID")
        cnt = item.get("Count", 0)
        if sid is not None:
            counter[sid] = counter.get(sid, 0) + cnt


# 动力转换领取的最短累积阈值：达到即视为满 100%
REACTOR_FULL_HOURS = 12


def _ms(v):
    """{$date: ms} 或 数字 -> 毫秒。"""
    if isinstance(v, dict):
        return v.get("$date")
    return v


def task_reactor(c: GameClient) -> dict:
    """动力转换（ArkReactor）：距上次领取满 12 小时才领，避免频繁领取降低收益。"""
    reactor = (c.account_state or {}).get("ArkReactorData", {}) or {}
    last_ms = _ms(reactor.get("LastMoneyReceivedTime"))
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    if last_ms is not None:
        elapsed_h = (now_ms - last_ms) / 3600_000
        if elapsed_h < REACTOR_FULL_HOURS:
            remain = REACTOR_FULL_HOURS - elapsed_h
            h = int(remain)
            m = int((remain - h) * 60)
            return {
                "ok": True,
                "skipped": True,
                "lines": [f"未满 100%（距上次领取 {elapsed_h:.1f}h），还需约 {h} 小时 {m} 分，跳过"],
                "detail": f"动力转换未满，还需约 {h}h{m}m",
            }

    try:
        res = c.call("ArkReactorHandler.RewardArkReactor", c._auth_data())
    except GameMessage as m:
        ok = _is_done_msg(m.msg)
        return {"ok": ok, "detail": ("动力转换已领取" if ok else f"领取失败：{m.msg}"), "raw": m.msg}

    counter: dict = {}
    _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
    crit = (res.get("Drop", {}) or {}).get("IsReactorCritical")
    # 写回刷新后的动力转换状态：否则同会话内下次巡检仍读登录时的旧 LastMoneyReceivedTime，
    # 会误判"又满了"重复领取（服务器虽会拒，但徒增无效请求与日志噪音）。
    new_reactor = res.get("ArkReactorData")
    if new_reactor is not None and c.account_state is not None:
        c.account_state["ArkReactorData"] = new_reactor
    got = _fmt_items(counter)
    line = f"领取了 {got}" + ("（暴击 x3）" if crit else "")
    return {"ok": True, "lines": [line], "detail": line, "raw": res}


# 生产中心三个定时产出：到点(NextCanReceive*Time)即领，无需额外等待。
# (显示名, 计时字段, 领取route)
STARFORCE_REWARDS = [
    ("成长药剂", "NextCanReceivePotionTime", "ArkStarForceLabHandler.RewardPotion"),
    ("升星水晶", "NextCanReceiveStarForceTime", "ArkStarForceLabHandler.RewardStarForce"),
    ("技能模块", "NextCanReceiveTesseractTime", "ArkStarForceLabHandler.RewardTesseract"),
]


def _fmt_clock(ms) -> str:
    """毫秒时间戳 -> 本地 'MM-DD HH:MM'。"""
    if ms is None:
        return "未知"
    dt = datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%m-%d %H:%M")


def _make_starforce_task(display: str, time_field: str, route: str):
    def _task(c: GameClient) -> dict:
        lab = (c.account_state or {}).get("ArkStarForceLabData", {}) or {}
        next_ms = _ms(lab.get(time_field))
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        if next_ms is not None and now_ms < next_ms:
            return {
                "ok": True,
                "skipped": True,
                "lines": [f"未到领取时间，下次可领 {_fmt_clock(next_ms)}，跳过"],
                "detail": f"{display}未到时间，下次 {_fmt_clock(next_ms)}",
            }

        try:
            res = c.call(route, c._auth_data())
        except GameMessage as m:
            ok = _is_done_msg(m.msg)
            return {"ok": ok, "detail": (f"{display}已领取" if ok else f"领取失败：{m.msg}"), "raw": m.msg}

        counter: dict = {}
        _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
        # 掉落是随机稀有度，用真实物品名（如"传说成长药剂"）；解析不到才回退建筑名
        got = _fmt_items(counter) if counter else display
        # 写回刷新后的实验室状态：同会话内下次巡检据新的 NextCanReceive*Time 判断，
        # 避免读登录旧值重复领取。
        new_lab = res.get("ArkStarForceLabData")
        if new_lab is not None and c.account_state is not None:
            c.account_state["ArkStarForceLabData"] = new_lab
        new_next = _ms((new_lab or {}).get(time_field))
        nxt = f"（下次 {_fmt_clock(new_next)}）" if new_next else ""
        return {"ok": True, "lines": [f"领取了 {got}{nxt}"], "detail": f"领取了 {got}", "raw": res}

    return _task


task_potion = _make_starforce_task(*STARFORCE_REWARDS[0])
task_starforce = _make_starforce_task(*STARFORCE_REWARDS[1])
_claim_tesseract = _make_starforce_task(*STARFORCE_REWARDS[2])


# ---- 技能模块的额外机制：生产途中可"加速" ----
# 官方文案(loc UI_Ark_SeedFarmTips): "每7天可生产 1 个技能模块，可以透过加速来缩短生产
# 时间，且生产途中可以加速 3 次"；UI_Ark_24HDecreased = "减少24小时" → 每次 -24h。
# 实抓 ChargeTesseract: data 只要 {AID,SessionID}，响应只回刷新后的 ArkStarForceLabData
# （无 Drop、无 CostItems=免费），把 NextCanReceiveTesseractTime 提前、
# TesseractChargeCount +1、NextCanChargeTime = now + 24h(两次加速的冷却)。
# **额度按生产周期计**: RewardTesseract 的响应里 TesseractChargeCount 归 0（实抓佐证），
# 所以 count 是"本周期已用次数"而非剩余次数。
TESSERACT_MAX_CHARGES = 3
# 两次加速之间的冷却(小时)。服务器下发 NextCanChargeTime 为准，这里只用于加速被拒时
# 本地退避（实抓: 加速/领取后 NextCanChargeTime 都 = 当时 + 24h）。
TESSERACT_CHARGE_GAP_H = 24


def _charge_tesseract(c: GameClient) -> tuple[str, str]:
    """尝试给技能模块加速一次。返回 (结果, 日志行)。

    结果 "quiet"  = 额度用尽/冷却未到，没发请求（不该进巡检日志）
        "charged" = 加速成功
        "reject"  = 发了请求但被服务器拒（要让用户看见）
    """
    lab = (c.account_state or {}).get("ArkStarForceLabData", {}) or {}
    used = int(lab.get("TesseractChargeCount") or 0)
    quota = f"{used}/{TESSERACT_MAX_CHARGES}"
    if used >= TESSERACT_MAX_CHARGES:
        return "quiet", f"本周期加速已用完（{quota}），跳过"

    next_ms = _ms(lab.get("NextCanChargeTime"))
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if next_ms is not None and now_ms < next_ms:
        return "quiet", f"加速冷却中（已用 {quota}），下次可加速 {_fmt_clock(next_ms)}"

    try:
        res = c.call("ArkStarForceLabHandler.ChargeTesseract", c._auth_data())
    except GameMessage as m:
        # 被拒时本地把冷却推后 24h（= 游戏侧真实冷却）再重试：巡检 5 分钟一轮，
        # 不退避会一直重试刷屏；而挂机会话可能连开数天，也不能就此永久放弃
        # ——领取过早时游戏有"保养期"，现在拒、过后可能就允许了。
        if c.account_state is not None:
            c.account_state.setdefault("ArkStarForceLabData", {})[
                "NextCanChargeTime"] = {"$date": now_ms + TESSERACT_CHARGE_GAP_H * 3600_000}
        return "reject", f"加速被拒：{m.msg}（{TESSERACT_CHARGE_GAP_H} 小时后再试）"

    # 写回刷新后的实验室状态：同会话内下次巡检据新的 NextCanChargeTime/次数判断
    new_lab = res.get("ArkStarForceLabData")
    if new_lab is not None and c.account_state is not None:
        c.account_state["ArkStarForceLabData"] = new_lab
    lab = new_lab or {}
    now_used = int(lab.get("TesseractChargeCount") or used + 1)
    done_ms = _ms(lab.get("NextCanReceiveTesseractTime"))
    tail = f"，预计 {_fmt_clock(done_ms)} 完成" if done_ms else ""
    return "charged", f"已加速 1 次（-24 小时，{now_used}/{TESSERACT_MAX_CHARGES}）{tail}"


def task_tesseract(c: GameClient) -> dict:
    """技能模块：到点即领；未到点则顺手加速（每周期 3 次、每次 -24h、间隔 24h）。

    加速有可能把完成时间提前到当下，所以加速成功后立刻再试领一次。
    """
    res = _claim_tesseract(c)
    if not res.get("skipped"):
        return res                      # 已领到（或领取失败）→ 新周期刚开始，无从加速

    kind, note = _charge_tesseract(c)
    if kind == "quiet":
        # 领取和加速都没发出请求 → 仍算跳过（不入巡检日志），两条原因都留在 detail 里
        return {**res, "lines": [*(res.get("lines") or []), note],
                "detail": f"{res.get('detail', '')}；{note}"}
    if kind == "reject":
        return {"ok": False, "lines": [note], "detail": note}

    after = _claim_tesseract(c)
    if after.get("skipped"):
        return {"ok": True, "lines": [note], "detail": note, "raw": after.get("raw")}
    return {"ok": after.get("ok", True),
            "lines": [note, *(after.get("lines") or [])],
            "detail": f"{note}；{after.get('detail', '')}",
            "raw": after.get("raw")}


# ============派遣任务（ArkHighCommand）============
# 固定 4 人小队 + 固定任务槽循环：任务到点(FinishTime<=now) → RewardQuest 领 → 用同一批
# DispatchedHeroIDs 立即 DispatchQuest 续派。体力(StaticID=3)不足时只领不续派。
def task_dispatch(c: GameClient) -> dict:
    hc = (c.account_state or {}).get("ArkHighCommandData", {}) or {}
    quests = hc.get("DispatchedQuests") or []
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    if not quests:
        return {"ok": True, "skipped": True,
                "lines": ["当前没有进行中的派遣任务，跳过"], "detail": "无派遣任务"}

    lines: list[str] = []
    counter: dict = {}
    redispatched = 0
    no_stamina = False
    latest_hc = None  # 最新一次响应带回的 ArkHighCommandData，循环后写回

    for q in quests:
        sid = q.get("StaticID")
        heroes = q.get("DispatchedHeroIDs") or []
        finish_ms = _ms(q.get("FinishTime"))
        if not sid:
            continue
        if finish_ms is not None and now_ms < finish_ms:
            lines.append(f"{sid}：未完成（{_fmt_clock(finish_ms)} 完成），跳过")
            continue

        # 1) 领取已完成的派遣
        try:
            res = c.call("ArkHighCommandHandler.RewardQuest",
                         c._auth_data({"QuestStaticID": sid}))
        except GameMessage as m:
            lines.append(f"{sid}：领取失败（{m.msg}）")
            continue
        if res.get("ArkHighCommandData") is not None:
            latest_hc = res["ArkHighCommandData"]
        got = {}
        _collect_items((res.get("Drop", {}) or {}).get("Items", []), got)
        for k, v in got.items():
            counter[k] = counter.get(k, 0) + v
        lines.append(f"{sid}：领取 {_fmt_items(got) if got else '完成'}")

        # 2) 原队立即续派（复用同一批角色）。FinishTime 客户端只传占位，服务器重算。
        payload = c._auth_data({"Quest": {
            "StaticID": sid,
            "FinishTime": _date_str(q.get("FinishTime")),
            "DispatchedHeroIDs": heroes,
        }})
        try:
            dres = c.call("ArkHighCommandHandler.DispatchQuest", payload)
            redispatched += 1
            if dres.get("ArkHighCommandData") is not None:
                latest_hc = dres["ArkHighCommandData"]
            # 用响应里的新完成时间提示
            newq = next((x for x in (dres.get("ArkHighCommandData", {}) or {}).get("DispatchedQuests", [])
                         if x.get("StaticID") == sid), None)
            nxt = _fmt_clock(_ms(newq.get("FinishTime"))) if newq else "?"
            lines.append(f"{sid}：原队续派成功（{nxt} 完成）")
        except GameMessage as m:
            # 体力不足等：只领不派，不阻塞其它任务
            no_stamina = no_stamina or ("体力" in m.msg or "Stamina" in m.msg)
            lines.append(f"{sid}：续派跳过（{m.msg}）")

    # 写回刷新后的派遣状态：否则同会话内下次巡检仍读登录时的旧 DispatchedQuests
    # （已领的槽 FinishTime 还停在过去），会重复领已领过的任务。
    if latest_hc is not None and c.account_state is not None:
        c.account_state["ArkHighCommandData"] = latest_hc

    if not counter and redispatched == 0:
        return {"ok": True, "skipped": True, "lines": lines or ["无可领取的派遣"],
                "detail": "派遣无可领取"}
    detail = (f"派遣领取 {_fmt_items(counter)}" if counter else "派遣处理完成")
    if no_stamina:
        detail += "（体力不足，部分未续派）"
    return {"ok": True, "lines": lines, "detail": detail, "raw": None}


# ============ 秘密商店 ============
SECRET_STORE_ID = "SecretShop"
# 可自动购买的目标 —— **唯一真源**，前端 /api/tasks 取此表渲染勾选框。
# ⚠️ 别在前端再写一份：曾经前后端各存一份默认值（前端缺省全选、后端缺省只有 5/6），
# 结果界面显示"强化芯片"打勾、后端却从不买芯片，用户报"免费刷商店漏买芯片"。
# key=存档用的稳定标识（改了会让已存的账号配置失效）；ids=该项展开的物品 StaticID；
# stat=日志统计用的短名（label 带部位说明，汇总里太长）。
BUY_TARGETS = [
    {"key": "recruit", "label": "招募契约", "stat": "招募契约", "ids": ["5"]},
    {"key": "mystery", "label": "神秘契约", "stat": "神秘契约", "ids": ["6"]},
    {"key": "chip", "label": "强化芯片（下级 · 全部位）", "stat": "下级强化芯片",
     "ids": ["EC11", "EC21", "EC31", "EC41", "EC51", "EC61"]},
]
# 缺省 = 全部目标（与前端"未配置时全选"一致）。account 的 params 为空时用它。
DEFAULT_SHOP_WANTED = [sid for t in BUY_TARGETS for sid in t["ids"]]

# 装备品阶（ClassLV -> 名称）。0普通/1一般/2稀有/3史诗/4传说（与图鉴一致）。
LEGENDARY_CLASSLV = 4
CLASSLV_TIER = {0: "普通", 1: "一般", 2: "稀有", 3: "史诗", 4: "传说"}

# 部位（图鉴 slot 键）-> 中文
SLOT_CN = {
    "weapon": "武器", "helmet": "头盔", "armor": "铠甲",
    "necklace": "项链", "ring": "戒指", "boots": "鞋子",
}

# 套装英文 -> 中文（与图鉴 equipment_rules.js 校正一致）
SET_CN = {
    "Attack": "攻击", "Defense": "防御", "Critical": "暴击", "Health": "生命",
    "Speed": "速度", "Hit": "命中", "Resist": "抗性", "Lifesteal": "吸血",
    "Counter": "反击", "Destruction": "暴伤", "Immunity": "免疫", "Penetration": "贯穿",
    "Revenge": "复仇", "Injury": "创伤", "Pinch": "追击", "Rage": "暴怒", "Torrent": "飞流",
}

# 套装英文 -> 图标文件名（与图鉴 equipment_rules.js SETS.icon 一致；Torrent 图标叫 Riptide）
SET_ICON = {
    "Attack": "Attack", "Defense": "Defense", "Critical": "Critical", "Health": "Health",
    "Speed": "Speed", "Hit": "Hit", "Resist": "Resist", "Lifesteal": "Lifesteal",
    "Counter": "Counter", "Destruction": "Destruction", "Immunity": "Immunity",
    "Penetration": "Penetration", "Revenge": "Revenge", "Injury": "Injury",
    "Pinch": "Pinch", "Rage": "Rage", "Torrent": "Riptide",
}

# 属性类型 -> (中文, 是否百分比)。百分比类别展示时加 "%"（攻击力% 等）。
PROP_CN = {
    "AttackValue": ("攻击力", False), "AttackRate": ("攻击力", True),
    "DefenceValue": ("防御力", False), "DefenceRate": ("防御力", True),
    "HPValue": ("生命值", False), "HPRate": ("生命值", True),
    "CriticalRate": ("暴击率", True), "CriticalDamageRate": ("暴击伤害", True),
    "SpeedValue": ("速度", False), "EffectHitRate": ("效果命中", True),
    "ResistanceRate": ("效果抵抗", True), "PenetrationRate": ("贯穿", True),
}

# 副属性分数换算系数（参考 wiki Equipment_Stats/Auxiliary Score）。
# 百分比类按整数百分点计（18% -> 18），白值按原值。总分四舍五入(.5 舍去)。
AUX_FACTOR = {
    "AttackValue": 0.17, "AttackRate": 1.8,
    "DefenceValue": 0.2, "DefenceRate": 1.8,
    "HPValue": 0.05, "HPRate": 1.5,
    "SpeedValue": 3.5, "CriticalRate": 2.0, "CriticalDamageRate": 1.8,
    "EffectHitRate": 1.25, "ResistanceRate": 1.25,
}

# staticID -> {level,name}，发布数据位于 backend/equip_ref.json
_EQUIP_REF = None


def _equip_ref() -> dict:
    global _EQUIP_REF
    if _EQUIP_REF is None:
        p = os.path.join(os.path.dirname(__file__), "equip_ref.json")
        try:
            with open(p, encoding="utf-8") as f:
                _EQUIP_REF = json.load(f)
        except Exception:
            _EQUIP_REF = {}
    return _EQUIP_REF

def _aux_score(subs: list) -> int:
    """副属性分数：Σ(值×系数)，百分比按整数百分点。四舍五入(.5舍去)。"""
    total = 0.0
    for sp in subs:
        f = AUX_FACTOR.get(sp.get("PropertyType"))
        if f is None:
            continue
        v = sp.get("Value", 0)
        # Rate 类（系数≥1.25 且属性名以 Rate 结尾）按百分点
        if sp.get("PropertyType", "").endswith("Rate"):
            v = v * 100
        total += v * f
    # 传统四舍五入但 .5 舍去
    import math
    return int(math.ceil(total - 0.5))


def _is_level_85_legendary(equip: dict) -> bool:
    """严格判定 85 级传说：ClassLV==4 且 staticID 映射等级==85。

    StaticID 在图鉴映射(backend/equip_ref.json)里找不到时，不猜测、不兜底——
    直接抛 UnknownEquipmentError，让上层暴露这个数据缺口而不是悄悄给出可能错误的判定。
    """
    if equip.get("ClassLV") != LEGENDARY_CLASSLV:
        return False
    sid = equip.get("StaticID", "")
    ref = _equip_ref().get(sid)
    if ref is None:
        raise UnknownEquipmentError(sid)
    return ref.get("level") == 85


def _safe_is_85(equip: dict):
    """展示用的宽松 85 判定：图鉴查不到返回 None(不判定)，不抛异常。

    与 _is_level_85_legendary 的区别：那个用于商店"要不要进手选"的严格决策，查不到必须
    fail-fast 报错暴露数据缺口；这个只用于掉落卡片展示 tierName/level，查不到留空即可。
    """
    try:
        return _is_level_85_legendary(equip)
    except UnknownEquipmentError:
        return None


def _prop_text(prop: dict) -> dict:
    """{PropertyType,Value} -> {label, text}。Rate 显示百分比，Value 显示整数。"""
    pt = prop.get("PropertyType", "")
    label, is_pct = PROP_CN.get(pt, (pt, False))
    val = prop.get("Value", 0)
    if is_pct:
        text = f"{round(val * 100)}%"
    else:
        text = str(int(round(val)))
    return {"type": pt, "label": label, "text": text}


def parse_equipment(equip: dict) -> dict:
    """把货架里的 Equipment 对象解析成卡片用数据（用已知字段，缺失项留空待图鉴）。"""
    cl = equip.get("ClassLV")
    main = _prop_text(equip.get("MainProp", {}) or {})
    raw_subs = (equip.get("SubProps", {}) or {}).get("SourceValues", [])
    subs = [_prop_text(s) for s in raw_subs]
    st = equip.get("Set", "")
    sid = equip.get("StaticID")
    ref = _equip_ref().get(sid or "", {})
    slot = ref.get("slot")
    # 名称优先用官方 loc（全中文），回退到 equip_ref（部分英文），再回退 staticID
    name = _item_names().get(sid or "") or ref.get("name") or sid
    return {
        "staticID": sid,
        "name": name,
        "level": ref.get("level"),
        "slot": slot,
        "slotCN": SLOT_CN.get(slot, slot),
        "img": f"/assets/equip/{os.path.basename(ref['img'])}" if ref.get("img") else None,
        "classLV": cl,
        "tierName": CLASSLV_TIER.get(cl, f"品阶{cl}"),
        "isLegendary": cl == LEGENDARY_CLASSLV,
        # 掉落展示用：图鉴查不到时不判定(None)，不阻断展示。
        # 注意：商店 85 过滤的严格判定走 _is_level_85_legendary 直接调用(仍 fail-fast)，此处仅展示。
        "is85Legendary": _safe_is_85(equip),
        "set": st,
        "setCN": SET_CN.get(st, st),
        "setIcon": f"/assets/sets/{SET_ICON[st]}.png" if st in SET_ICON else None,
        "mainProp": main,
        "subProps": subs,
        "auxScore": _aux_score(raw_subs),       # 副属性分数（wiki 换算规则）
    }


def _to_send_format(obj):
    """把服务器下发的对象递归转成购求所需格式：
    {$oid:X}->X, {$date:ms}->日期字符串, bool->0/1, 整数值的float->int。

    整数值 float→int：游戏客户端(C#)序列化时整数值不带 .0(如 167 而非 167.0)，
    Python json 默认给 float 写 .0。整数值转 int 与客户端字节对齐，且避免把 55.0
    发给服务器 int 字段可能触发的严格解析报错(int→float 服务器可安全放宽，反之危险)。
    """
    if isinstance(obj, dict):
        if "$oid" in obj and len(obj) == 1:
            return obj["$oid"]
        if "$date" in obj and len(obj) == 1:
            return _date_str(obj)
        return {k: _to_send_format(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_send_format(v) for v in obj]
    if isinstance(obj, bool):
        return 1 if obj else 0
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def _record_goods_sid(rec: dict):
    """取一档货架卖的道具 StaticID（装备档无 Item，返回 None）。"""
    items = (rec.get("DropResult", {}) or {}).get("Items", [])
    if not items:
        return None
    return (items[0].get("Item") or {}).get("StaticID")


def _record_equipment(rec: dict):
    """取一档货架卖的装备对象（道具档返回 None）。"""
    items = (rec.get("DropResult", {}) or {}).get("Items", [])
    if not items:
        return None
    return items[0].get("Equipment")


def _secret_shop_records(state: dict) -> list:
    """从账号状态取当前秘密商店 6 档货架。"""
    recs = (state or {}).get("StoreRecordContainer", {}).get("Records", [])
    return [r for r in recs if r.get("Store") == SECRET_STORE_ID]


# 秘密商店免费刷新的冷却：1 小时。
# 依据（两侧互相印证，不是猜的）：用户在游戏内刷新后看到冷却显示 59 分钟（秒数进位即 1h）；
# 抓包侧 IsUseGold=0 成功时货架年龄为 1.01h / 2.78h，而刚刷完 5 分钟内再免费刷必被拒。
# 拒绝时服务器回明文 `CantReset`（2026-07-30 在真实账号上连发 3 次实测：第 1 次成功、
# 第 2/3 次均 CantReset，不扣钻不扣金币）。
SHOP_FREE_COOLDOWN_MS = 60 * 60 * 1000


def secret_shop_next_free(state: dict) -> int | None:
    """下次可免费刷新秘密商店的时刻（毫秒）。货架记录缺失时返回 None（视为可刷）。

    取 6 档里最新的 LastResetTime —— 它们本该同值（同一次刷新写的），
    取 max 是为了万一某档滞后也不会把档期算早、导致每轮空发请求换回 CantReset。
    """
    stamps = [
        ms for ms in (_ms(r.get("LastResetTime")) for r in _secret_shop_records(state))
        if ms is not None and ms > 0        # C# DateTime.MinValue 是负数，代表"从未刷过"
    ]
    if not stamps:
        return None
    return max(stamps) + SHOP_FREE_COOLDOWN_MS


def _buy_payload(c: GameClient, rec: dict, count: int = 1, extra: dict | None = None) -> dict:
    """StoreHandler.BuyCommodity 的请求体（已与真实客户端字节级对齐，见 _to_send_format）。"""
    data = {
        "Record": _to_send_format(rec),
        "Count": count,
        "ItemIndex": -1,
        "Platform": "WebGLPlayer",
        "LoginType": "Erolabs",
        "NewErolabs": 0,
        "SelectRoleID": "",
    }
    if extra:
        data.update(extra)
    return c._auth_data(data)


def _buy_from_shelf(c: GameClient, records: list, wanted: list, counter: dict) -> list:
    """买下货架上 StaticID∈wanted 且未买(BuyCount==0)的档位，产出累加进 counter。

    返回购买失败的档位说明列表（缺钱/已售罄等）；单档失败不影响其它档继续买，
    也不丢弃本次已买到的东西——之前 raise 会让上层 catch 后直接返回失败，
    连同一批已经买成功、已经花了钱的道具也从结果里消失了。
    """
    errors = []
    for rec in records:
        if rec.get("BuyCount"):
            continue
        sid = _record_goods_sid(rec)
        if sid is None or str(sid) not in wanted:
            continue
        payload = c._auth_data({
            "Record": _to_send_format(rec),
            "Count": 1,
            "ItemIndex": -1,
            "Platform": "WebGLPlayer",
            "LoginType": "Erolabs",
            "NewErolabs": 0,
            "SelectRoleID": "",
        })
        try:
            res = c.call("StoreHandler.BuyCommodity", payload)
        except GameMessage as m:
            errors.append(f"{item_name(sid)}购买失败：{m.msg}")
            continue
        _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
    return errors


# ---- 钻石刷商店：原子接口（前端编排循环，严格保证"上一次买完再刷下一次") ----
def shop_reset(c: GameClient, use_gold: bool, auto_buy_sids: list) -> dict:
    """刷新一次秘密商店：
    1) ResetRandomStore（use_gold 决定花钻/免费）
    2) 服务器端立即买掉普通档里 StaticID∈auto_buy_sids 的
    3) 收集 85 级传说 的装备档（严格判定，带 index，供前端逐张按 index 购买）
    返回本次结果 + 缓存本次货架 records（供 shop_buy_index 用）。
    """
    wanted = [str(x) for x in (auto_buy_sids or [])]
    try:
        res = c.call("StoreHandler.ResetRandomStore",
                     c._auth_data({"StoreID": SECRET_STORE_ID, "IsUseGold": 1 if use_gold else 0}))
    except GameMessage as m:
        return {"ok": False, "error": m.msg, "records": [], "bought": {}, "boughtLines": [], "legendaries": []}

    records = [r for r in res.get("Records", []) if r.get("Store") == SECRET_STORE_ID]
    counter: dict = {}
    legendaries = []
    buy_errors = []  # 目标道具购买失败(缺钱等)：不吞掉，暴露给前端而不是悄悄漏买
    for idx, rec in enumerate(records):
        if rec.get("BuyCount"):
            continue
        equip = _record_equipment(rec)
        if equip is not None:
            try:
                is85 = _is_level_85_legendary(equip)   # 严格只认 85 级传说
            except UnknownEquipmentError as e:
                # 数据不全：中止本次刷新，把问题暴露给前端而不是猜测判定
                lines = [f"购买 {_fmt_items(counter)}"] if counter else []
                return {
                    "ok": False,
                    "error": f"货架第 {idx + 1} 档装备 StaticID={e.static_id} 不在图鉴数据中，"
                             f"无法判定是否为85级传说，已中止本次刷新——请补充 backend/equip_ref.json",
                    "records": records, "bought": counter,
                    "boughtLines": lines, "legendaries": legendaries,
                }
            if is85:
                card = parse_equipment(equip)
                card["index"] = idx
                legendaries.append(card)
            continue
        sid = _record_goods_sid(rec)
        if sid is not None and str(sid) in wanted:
            payload = c._auth_data({
                "Record": _to_send_format(rec), "Count": 1, "ItemIndex": -1,
                "Platform": "WebGLPlayer", "LoginType": "Erolabs",
                "NewErolabs": 0, "SelectRoleID": "",
            })
            try:
                br = c.call("StoreHandler.BuyCommodity", payload)
                _collect_items((br.get("Drop", {}) or {}).get("Items", []), counter)
            except GameMessage as m:
                # 该档买失败(缺钱/已售罄等)：不阻塞其余档位，但要记下来暴露给前端，
                # 不能悄悄吞掉——否则同屏其它档买成功也会误以为"这次只匹配到一个目标道具"。
                buy_errors.append(f"第 {idx + 1} 档({item_name(sid)})购买失败：{m.msg}")
    # 一次刷新里买到的多个目标合并成一条："购买 1下级项链强化芯片、5招募契约"
    lines = [f"购买 {_fmt_items(counter)}"] if counter else []
    return {
        "ok": True, "records": records, "bought": counter,
        "boughtLines": lines, "buyErrors": buy_errors, "legendaries": legendaries,
    }


def shop_buy_index(c: GameClient, records: list, index: int) -> dict:
    """按 index 购买缓存货架里的某一档（用于传说装备手动购买）。"""
    if not records or index < 0 or index >= len(records):
        return {"ok": False, "detail": "货架已失效，请重新刷新"}
    rec = records[index]
    payload = c._auth_data({
        "Record": _to_send_format(rec), "Count": 1, "ItemIndex": -1,
        "Platform": "WebGLPlayer", "LoginType": "Erolabs",
        "NewErolabs": 0, "SelectRoleID": "",
    })
    try:
        res = c.call("StoreHandler.BuyCommodity", payload)
    except GameMessage as m:
        return {"ok": False, "detail": f"购买失败：{m.msg}"}
    # 装备档：给出品阶+套装描述；道具档：物品名
    equip = _record_equipment(rec)
    if equip is not None:
        card = parse_equipment(equip)
        name = f"{card['tierName']}·{card['setCN']}装备({card['staticID']})"
    else:
        counter: dict = {}
        _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
        name = _fmt_items(counter)
    return {"ok": True, "detail": f"购买了 {name}", "name": name}


def task_shop_free(c: GameClient, params: dict | None = None) -> dict:
    """刷秘密商店（免费）：用每小时免费额度刷新一次，买货架上的目标道具。
    无免费额度时回退读当前货架购买（不花钻）。"""
    params = params or {}
    wanted = [str(x) for x in (params.get("wanted") or DEFAULT_SHOP_WANTED)]

    counter: dict = {}
    note = ""
    try:
        res = c.call("StoreHandler.ResetRandomStore",
                     c._auth_data({"StoreID": SECRET_STORE_ID, "IsUseGold": 0}))
        records = [r for r in res.get("Records", []) if r.get("Store") == SECRET_STORE_ID]
        if res.get("CostItems"):
            note = "（注意：本次刷新产生了扣费）"
    except GameMessage as m:
        # 免费额度未恢复：服务器回明文 `CantReset`（2026-07-30 真机实测，不扣任何货币）。
        # 正常情况下调度已按货架 LastResetTime+1h 闸门过，不该走到这里；真走到了说明
        # 档期算早了或货架被游戏客户端刷过，直接跳过本轮，**不再拿缓存货架顶替**——
        # 那份缓存可能已过期（手动钻石刷不写回旧档），拿旧 record 去买只会被服务器拒。
        if "cantreset" in m.msg.replace(" ", "").lower():
            return {"ok": True, "skipped": True,
                    "detail": "免费刷新冷却中（服务器：CantReset），本轮跳过",
                    "lines": []}
        raise

    errors = _buy_from_shelf(c, records, wanted, counter)

    lines = []
    if counter:
        lines.append(f"购买 {_fmt_items(counter)}{note}")
    elif not errors:
        lines.append(f"本次无可购买的目标道具{note}")
    lines.extend(errors)
    detail = "；".join(lines) if lines else "本次无可购买的目标道具"
    return {"ok": True, "lines": lines, "detail": detail}


# ============ 商店购买（VIP礼包 / 友情 / 荣誉勋章） ============
# 三个固定货架商店（loc: T_Commodity_PageTag_VIPGIFT_VIP0=VIP礼包 /
# T_Store_Name_FriendShip=友情 / T_Store_Name_MedalHonor=荣誉勋章）。
# 与秘密商店不同：这三家的货架是**静态档位**，商品内容不随机，所以账号快照里的
# 记录(StoreRecordContainer)只带购买计数、不带 DropResult —— "这档卖什么/多少钱"
# 全在客户端静态表里，流量里看不到。故本模块的货架 = 三个来源合并：
#   ① backend/commodity_names.json —— 官方档位名 + 限购文案，提供"有哪些档、叫什么"
#   ② COMMODITY_PRICE —— 实抓验证过的价格与产出（下方逐档标注来源）
#   ③ 账号快照的 StoreRecordContainer.Records —— 提供购买必需的 `_id` 与已购次数
# ⚠️ 缺 ② 的档位一律**不可选**：三家里有整套角色降临礼包（loc 标"每个帐号只能购买
# 1次""无法于招募中获得"），极可能是真钱 IAP 档，价格未知就点下去风险不可控。
# ⚠️ 缺 ③ 的档位也不可选：购买请求必须回传服务器下发的整条 Record（含 Mongo _id），
# 快照里没有就无法构造，猜 _id 只会被服务器拒。
BUY_STORES = [
    {"id": "VIPGift", "name": "VIP 礼包", "currency": "1"},
    {"id": "FriendShip", "name": "友情", "currency": "24"},
    {"id": "MedalHonor", "name": "荣誉勋章", "currency": "26"},
]

# 货架白名单（用户定：只放这些档，其余不进 UI）。
#   cost: (货币StaticID, 单价)    gain: [(产出StaticID, 单份数量)]    limit: 每日限购份数
# 价格与产出来自 game.ero-labs.tech_buy.har 实抓（2026-07-29，账号 654211306b03755f04d2bbaf）：
# 每条都是"请求 Count=N → 响应 CostItems/Drop"换算出的单份值，MedalHonor2 是
# Count=3 花 90 得 120能源 → 单价 30、单份 40。
# ⚠️ VIPQuick4 未实抓，价格按用户确认"与前三个同价"、产出按"比 3 多五张快速战斗券"。
# ⚠️ limit 为 None = 每日限购次数未确认，UI 只显示已购次数、不显示 N/M。
COMMODITY_PRICE = {
    "VIPGIFT_VIPQuick1": {"cost": ("1", 500), "gain": [("110", 10), ("3", 30)], "limit": 1},
    "VIPGIFT_VIPQuick2": {"cost": ("1", 500), "gain": [("110", 15), ("3", 30)], "limit": 1},
    "VIPGIFT_VIPQuick3": {"cost": ("1", 500), "gain": [("110", 20), ("3", 30)], "limit": 1},
    "VIPGIFT_VIPQuick4": {"cost": ("1", 500), "gain": [("110", 25), ("3", 30)], "limit": 1},
    # 友情四档均为每日 1 次（用户确认）
    "FriendShip3": {"cost": ("24", 100), "gain": [("3", 40)], "limit": 1},
    "FriendShip4": {"cost": ("24", 100), "gain": [("4", 5)], "limit": 1},
    "FriendShip10": {"cost": ("24", 1000), "gain": [("1", 100000)], "limit": 1},
    "FriendShip11": {"cost": ("24", 100), "gain": [("110", 5)], "limit": 1},
    # limit=3 由用户确认（游戏内该档每日可买 3 次）；抓包里也确实出现过 Count:3 的请求
    "MedalHonor2": {"cost": ("26", 30), "gain": [("3", 40)], "limit": 3},
    # ⚠️ MedalHonor1 = 技能模块：**价格 150 是用户在游戏内读的，未经抓包验证**（没有
    # 任何抓包记录过这档的 CostItems）。档位身份是排除法定的：用户 LV41 号的荣誉勋章
    # 商店只有 MedalHonor1/2 两条记录，2 已实抓是能源，而该商店除能源与六部位芯片
    # (3~10) 外只剩技能模块。
    # ⚠️ **限购是"每周 1 次"而非每日**（用户确认；也与快照吻合：能源档每日重置，
    # MedalHonor1 在 07-21 购买后到 07-24 仍未重置）。limit=1 用于显示"购买 N/1"，
    # 但 bought 要等服务器周重置才归零——买过之后会一直显示 购买 0/1 直到下周。
    # name: loc 未收录该档时的显示名（我们自己起的标签，非官方文案）。
    "MedalHonor1": {"cost": ("26", 150), "gain": [("34", 1)], "limit": 1,
                    "name": "技能模块"},
}

# 每家商店要显示的档位与顺序（白名单，用户定）
STORE_GOODS = {
    "VIPGift": ["VIPGIFT_VIPQuick1", "VIPGIFT_VIPQuick2",
                "VIPGIFT_VIPQuick3", "VIPGIFT_VIPQuick4"],
    "FriendShip": ["FriendShip3", "FriendShip4", "FriendShip11", "FriendShip10"],
    "MedalHonor": ["MedalHonor1", "MedalHonor2"],
}

# 卡片上要不要单独列"礼包内容"。只有 VIP 礼包需要——它的档名是礼包名（"VIP快速战斗
# 应援2"），看不出给什么；友情/荣誉勋章的档名本身就是官方写的产出描述（"40点能源"
# "10万金币"），再列一行是重复（用户定）。
STORE_SHOW_GAIN = {"VIPGift"}

# 账号快照里没有该档记录时的提示（不同商店原因不同）：
# VIP 礼包有 VIP 等级门槛（loc UI_VIPGIFT_VIPLess="须达到VIP{0}才可购买!"），
# 友情/荣誉勋章没有等级门槛，记录只是"还没生成"。两者都仍然尝试购买，由服务器裁决。
NO_RECORD_HINT = {
    "VIPGift": "账号上找不到该礼包，可能是 VIP 等级不足",
    "FriendShip": "账号上还没有该档记录，可直接尝试购买",
    "MedalHonor": "账号上还没有该档记录，可直接尝试购买",
}

_COMMODITY_NAMES = None


def _commodity_names() -> dict:
    """读取随应用发布的档位 StaticID -> {store, name, tip?} 索引。"""
    global _COMMODITY_NAMES
    if _COMMODITY_NAMES is None:
        p = os.path.join(os.path.dirname(__file__), "commodity_names.json")
        try:
            with open(p, encoding="utf-8") as f:
                _COMMODITY_NAMES = json.load(f)
        except Exception:
            _COMMODITY_NAMES = {}
    return _COMMODITY_NAMES


def _gain_text(gain: list) -> str:
    return "、".join(f"{cnt}{item_name(sid)}" for sid, cnt in gain)


# 服务器下发记录里"从未购买"的时间戳（C# DateTime.MinValue）。合成记录时照用，
# 与真实记录的字节形态一致（_to_send_format 会转成 "0001/1/1 0:0:0"）。
_NEVER_MS = -62135596800000


def _store_of(sid: str) -> str:
    """档位 StaticID -> 所属商店（白名单反查）。"""
    for store, ids in STORE_GOODS.items():
        if sid in ids:
            return store
    raise GameError(f"档位 {sid} 不在货架白名单内")


def _synth_record(sid: str, store: str) -> dict:
    """给账号快照里还没有的档位合成一条初始货架记录。

    为什么需要：购买请求必须回传整条 Record，而 `StoreRecordContainer` 只包含
    "服务器已经为该账号建过"的档位——实测同一账号 LV41 快照里 FriendShip 只有 3 档，
    但游戏内 10万金币 那档明明可买（用户报的 bug）。客户端打开商店界面不发任何请求，
    说明它是**用静态表 + 默认值自己拼记录**发出去的，我们照做。
    `_id` 留空串（服务器按 StaticID+Store 定位/建档），计数归零、时间取 MinValue。
    """
    return {
        "_id": "", "StaticID": sid, "Store": store,
        "BuyCount": 0, "FreeBuyCount": 0, "GuaranteedCount": 0,
        "SummonRoleID": "", "SummonRoleIDs": [],
        "LastBuyTime": {"$date": _NEVER_MS},
        "LastResetTime": {"$date": _NEVER_MS},
    }


def store_shelves(state: dict) -> list[dict]:
    """三个商店的货架（白名单档位 × 账号快照）。供前端渲染卡片。

    每档：{id, name, tip, icon, gainText, cost:{item,name,count},
           bought, limit, remain, hasRecord, hint}
    货架档位由 STORE_GOODS 白名单固定（用户只要这些档），所以每档都有价格与产出。
    **不返回"能否购买"的判断**——用户定：售罄/快照缺记录都照发请求，由服务器裁决，
    把它的原话显示出来。本地拦人只会像 FriendShip10 那样误判（快照没记录≠不能买）。
    """
    recs: dict[str, dict] = {}
    for r in (state or {}).get("StoreRecordContainer", {}).get("Records", []):
        sid = r.get("StaticID")
        if sid:
            recs[str(sid)] = r

    catalog = _commodity_names()
    out = []
    for store in BUY_STORES:
        goods = []
        for sid in STORE_GOODS.get(store["id"], []):
            meta = catalog.get(sid, {})
            price = COMMODITY_PRICE[sid]
            rec = recs.get(sid)
            gain = price["gain"]
            limit = price.get("limit")
            bought = int((rec or {}).get("BuyCount") or 0)

            goods.append({
                "id": sid,
                # 优先官方 loc 名，其次我们的显式标签，最后退化成产出描述
                "name": meta.get("name") or price.get("name") or _gain_text(gain),
                "tip": meta.get("tip", ""),
                "icon": gain[0][0],
                # 只有需要单独展示内容的商店才给 gainText（见 STORE_SHOW_GAIN）
                "gainText": (_gain_text(gain) if store["id"] in STORE_SHOW_GAIN else ""),
                "cost": {"item": price["cost"][0],
                         "name": item_name(price["cost"][0]),
                         "count": price["cost"][1]},
                "bought": bought,
                "limit": limit,
                # 剩余可购次数（游戏内"购买 N/M"的 N 是**剩余**而非已买，用户确认）
                "remain": (max(0, limit - bought) if limit is not None else None),
                "hasRecord": rec is not None,
                # 卡片一律可点：售罄/缺记录都照发请求，由服务器裁决并把它的原话显示出来
                # （用户定：不靠本地判断拦人，避免像 10万金币 那样误判成不可买）
                "hint": ("" if rec is not None or store["id"] != "VIPGift"
                         else NO_RECORD_HINT["VIPGift"]),
            })
        out.append({**store, "currencyName": item_name(store["currency"]),
                    "goods": goods})
    return out


def store_buy(c: GameClient, picks: list[dict]) -> dict:
    """按前端选择批量购买。picks = [{"id": 档位StaticID, "count": 份数}]。

    逐档独立处理：某档失败（缺货币/超限购）只记一条错误，不影响其余档——已经花掉
    货币买到的东西不能因为后面一档报错就从结果里消失（同 _buy_from_shelf 的教训）。
    """
    state = c.account_state or {}
    recs = {str(r.get("StaticID")): r
            for r in state.get("StoreRecordContainer", {}).get("Records", [])
            if r.get("StaticID")}

    got: dict = {}
    spent: dict = {}
    lines: list[str] = []
    errors: list[str] = []
    done = 0
    for pick in picks or []:
        sid = str(pick.get("id") or "")
        count = max(1, int(pick.get("count") or 1))
        meta = _commodity_names().get(sid, {})
        name = meta.get("name") or sid

        if sid not in COMMODITY_PRICE:
            errors.append(f"{name}：不在货架白名单内，已跳过")
            continue
        # 快照缺记录时用合成记录尝试（见 _synth_record）；服务器不认就报它的原话
        rec = recs.get(sid) or _synth_record(sid, _store_of(sid))
        try:
            res = c.call("StoreHandler.BuyCommodity", _buy_payload(c, rec, count))
        except GameMessage as m:
            errors.append(f"{name}：购买失败（{m.msg}）")
            continue
        _collect_items((res.get("Drop", {}) or {}).get("Items", []), got)
        _collect_items(res.get("CostItems", []), spent)
        done += 1

    if got:
        line = f"购买 {done} 档：获得 {_fmt_items(got)}"
        if spent:
            line += f"，花费 {_fmt_items(spent)}"
        lines.append(line)
    elif not errors:
        lines.append("没有选择任何可购买的商品")
    lines.extend(errors)
    return {
        "ok": not errors or done > 0,
        "lines": lines,
        "detail": "；".join(lines),
        "bought": got,
        "spent": spent,
        "errors": errors,
    }


# ============ 虚拟幻境（Abyss）快速扫荡 ============
# 官方名"虚拟幻境" = 场景族 Abyss_N（loc T_Chapter_Name_Abyss）；扫荡 route
# SceneHandler.PurityScene，消耗意识同步器(StaticID 39)、产出星源粉末(25)+金币(1)。
# loc T_GuidePage_Content_CombatContent08：「根据虚拟幻境完成的层数，可获得的奖励也会
# 逐步增加。使用快速扫荡功能时，会消耗你持有的意识同步器」——消耗量=当前持有量，
# 请求体里没有数量字段，由服务器结算（实抓：持有 3 个 → CostItems 3 个、余额归 0）。
_ABYSS_RE = re.compile(r"^Abyss_(\d+)$")
# 门槛层数（用户定）：不到这一层不扫，免得把同步器浪费在低层低产出上
ABYSS_MIN_FLOOR = 80
SYNC_ITEM_SID = "39"          # 意识同步器
ABYSS_SWEEP_ROUTE = "SceneHandler.PurityScene"


def _item_count(state: dict, sid: str) -> int:
    for it in (state or {}).get("ItemContainer", {}).get("Items", []):
        if str(it.get("StaticID")) == sid:
            return int(it.get("Count") or 0)
    return 0


def abyss_progress(state: dict) -> dict:
    """虚拟幻境进度：已通关最高层 + 该层场景记录 + 意识同步器持有量。

    ⚠️ `SceneDataContainer.Scenes` 里**只有打过的层才有条目**，且已通关的层
    `Stars[0]==1`（两个号实测：LV13 号只有 Abyss_1..3、满级号能扫的 Abyss_80
    都是 Stars[1,0,0]）。故"已通关最高层" = 现存条目中 Stars[0]==1 的最大层号；
    Stars[0] 不为 1 的条目一律不算通关——宁可判低也不误判高，判高会让工具在没
    资格的层上白扔同步器。
    """
    best_floor, best_rec = 0, None
    for s in (state or {}).get("SceneDataContainer", {}).get("Scenes", []):
        m = _ABYSS_RE.match(str(s.get("StaticID", "")))
        if not m:
            continue
        stars = s.get("Stars") or []
        if not (stars and stars[0]):      # 进过但没通关，不计入
            continue
        floor = int(m.group(1))
        if floor > best_floor:
            best_floor, best_rec = floor, s
    return {
        "floor": best_floor,
        "record": best_rec,
        "sync": _item_count(state, SYNC_ITEM_SID),
    }


def task_abyss_sweep(c: GameClient) -> dict:
    """虚拟幻境快速扫荡：背包有意识同步器时，扫荡已通关的最高层。

    扫最高层而非固定第 80 层：奖励随"完成的层数"递增，用已通关的最高层去扫不会
    比第 80 层差；门槛仍是必须已通关 ABYSS_MIN_FLOOR 层。
    """
    state = c.account_state or {}
    prog = abyss_progress(state)
    floor, rec = prog["floor"], prog["record"]

    if floor < ABYSS_MIN_FLOOR:
        cur = f"当前最高 {floor} 层" if floor else "尚无通关记录"
        return {
            "ok": True, "skipped": True,
            "lines": [f"未通关第 {ABYSS_MIN_FLOOR} 层（{cur}），不扫荡以免浪费意识同步器"],
            "detail": f"未通关第 {ABYSS_MIN_FLOOR} 层（{cur}）",
        }
    if prog["sync"] <= 0:
        return {
            "ok": True, "skipped": True,
            "lines": ["没有意识同步器，跳过"],
            "detail": "没有意识同步器",
        }

    try:
        res = c.call(ABYSS_SWEEP_ROUTE, c._auth_data(_to_send_format(rec)))
    except GameMessage as m:
        return {"ok": False, "detail": f"扫荡失败：{m.msg}", "raw": m.msg}

    counter: dict = {}
    _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
    cost: dict = {}
    _collect_items(res.get("CostItems", []), cost)
    line = f"第 {floor} 层扫荡：获得 {_fmt_items(counter)}"
    if cost:
        line += f"（消耗 {_fmt_items(cost)}）"
    return {"ok": True, "lines": [line], "detail": line, "raw": res}


# ============ 每日免费招募（常规招募的免费额度） ============
# 常规招募档 = StoreRecordContainer 里 Store=="Summon" 且 StaticID=="NormalSummon"。
# 该档有独立的 `FreeBuyCount`（与花契约的 `BuyCount` 分开计数）：实抓一次免费招募后
# FreeBuyCount 0→1，且响应**没有 CostItems**（真的没花任何东西）。
# ⚠️ 服务器不会因为"免费额度已用"而拒绝请求——额度用完再调同一个 route 就变成花
# 招募契约抽。所以"是否免费"完全靠本地严格判定，判错就是直接扣道具。
SUMMON_STORE_ID = "Summon"
NORMAL_SUMMON_SID = "NormalSummon"


def _summon_record(state: dict):
    for r in (state or {}).get("StoreRecordContainer", {}).get("Records", []):
        if r.get("Store") == SUMMON_STORE_ID and r.get("StaticID") == NORMAL_SUMMON_SID:
            return r
    return None


def _fmt_roles(items: list) -> list:
    """招募掉落里的团员（Drop.Items[].RoleData）-> ["3★ 某某", ...]。"""
    out = []
    for entry in items or []:
        role = entry.get("RoleData")
        if not isinstance(role, dict):
            continue
        sid = str(role.get("StaticID", ""))
        star = role.get("Star")
        out.append(f"{star}★ {item_name(sid)}" if star else item_name(sid))
    return out


def task_free_summon(c: GameClient) -> dict:
    """每日免费招募：只在"免费额度未用"时抽一次，绝不动用招募契约。

    严格判定链（任一不满足就不发请求）：
      1. 快照里有 NormalSummon 档（查不到就报错暴露，不猜）
      2. 该档 FreeBuyCount == 0
    抽完由 game_client._apply_commodity_record 把响应的 CommodityRecord 回写缓存，
    同一会话内 FreeBuyCount 立刻变 1，后台巡检不会再抽第二次。
    """
    state = c.account_state or {}
    rec = _summon_record(state)
    if rec is None:
        return {
            "ok": False,
            "detail": "账号快照里找不到常规招募档位（Store=Summon / StaticID=NormalSummon），"
                      "无法判断免费额度是否可用，已中止——请点刷新重新登录后再试",
        }

    free_used = int(rec.get("FreeBuyCount") or 0)
    if free_used > 0:
        return {
            "ok": True, "skipped": True,
            "lines": [f"今日免费招募已用（{free_used} 次），跳过"],
            "detail": f"今日免费招募已用（{free_used} 次）",
        }

    # SelcetCostItemID：官方客户端的招募请求独有此字段（原文拼写如此，商店购买请求里
    # 没有），照抄以与真实客户端对齐。空串 = 不指定用哪种货币结算。
    payload = _buy_payload(c, rec, 1, {"SelcetCostItemID": ""})
    try:
        res = c.call("StoreHandler.BuyCommodity", payload)
    except GameMessage as m:
        return {"ok": False, "detail": f"免费招募失败：{m.msg}", "raw": m.msg}

    items = (res.get("Drop", {}) or {}).get("Items", [])
    counter: dict = {}
    _collect_items(items, counter)
    parts = _fmt_roles(items)
    if counter:
        parts.append(_fmt_items(counter))
    line = "免费招募：获得 " + ("、".join(parts) if parts else "（无掉落）")

    mileage = (res.get("CommodityRecord") or {}).get("GuaranteedCount")
    if mileage is not None:
        line += f"，保底计数 {mileage}"

    # 兜底告警：真免费时响应不该有 CostItems。有就说明判定链被绕过、真花了东西，
    # 必须显著报出来（而不是当成正常成功），好让用户立刻发现并停用开关。
    cost = res.get("CostItems") or []
    if cost:
        spent: dict = {}
        _collect_items(cost, spent)
        line += f" ⚠ 本次并非免费，消耗了 {_fmt_items(spent)}，请关闭该开关并反馈"
        return {"ok": False, "lines": [line], "detail": line, "raw": res}
    return {"ok": True, "lines": [line], "detail": line, "raw": res}


# ============ 讨伐扫荡 ============
# 候选关卡固定 5 个(各元素 Hunt*_11)。System 字段用于匹配账号 Teams.SystemTeamIDMap /
# TeamSettingHandler 里观测到的字符串，仅用于展示，不参与请求体。
HUNT_SWEEP_CANDIDATES = [
    {"id": "HuntFire_11", "system": "HuntFire", "element": "fire", "name": "火讨伐11"},
    {"id": "HuntIce_11", "system": "HuntIce", "element": "water", "name": "水讨伐11"},
    {"id": "HuntEarth_11", "system": "HuntEarth", "element": "wood", "name": "木讨伐11"},
    {"id": "HuntDark_11", "system": "HuntDark", "element": "dark", "name": "暗讨伐11"},
    {"id": "HuntLight_11", "system": "HuntLight", "element": "light", "name": "光讨伐11"},
]

# 元素扫荡（游戏内"元素探索"，loc: T_Episode_Name_Elf）。与讨伐同走 SceneHandler.FinishScene，
# 场景对象结构完全一致，故复用 sweep_once。区别: 元素探索有"难度"维度——每个元素 4 档
# (_1 初级 / _2 中级 / _3 上级 / _4 地狱级)，5 元素共 20 关，全部作为关卡下拉候选。
# 元素→(中文, 前端色点 element key) 映射，与讨伐一致(ElfIce=水, ElfEarth=木)。
_ELF_ELEMENTS = [
    ("ElfFire", "火", "fire"),
    ("ElfIce", "水", "water"),
    ("ElfEarth", "木", "wood"),
    ("ElfDark", "暗", "dark"),
    ("ElfLight", "光", "light"),
]
# 只暴露地狱级(_4)——用户只跑最高难度拿最高级元素。要放开其它档在此加回即可。
_ELF_TIERS = [("4", "地狱级")]
ELEMENT_SWEEP_CANDIDATES = [
    {"id": f"{sys_}_{tier}", "system": sys_, "element": color,
     "name": f"{cn}元素·{tier_cn}"}
    for sys_, cn, color in _ELF_ELEMENTS
    for tier, tier_cn in _ELF_TIERS
]

# 扫荡类任务的候选关卡集合，供 sweep_options 按 kind 选择。
SWEEP_KINDS = {
    "hunt": HUNT_SWEEP_CANDIDATES,
    "elf": ELEMENT_SWEEP_CANDIDATES,
}

# StaticID 末位数字 -> 战斗请求 EquipmentMap 的键名(抓包实测: 武1头2铠3项4戒5鞋6)。
_BATTLE_SLOT_KEY = {"1": "Weapon", "2": "Head", "3": "Body", "4": "Necklace", "5": "Ring", "6": "Shoes"}


def _find_by_oid(items: list, target_id: str, id_field: str = "_id"):
    for it in items:
        if _oid(it.get(id_field)) == target_id:
            return it
    return None


def _build_role_for_battle(state: dict, role_id: str) -> dict:
    """把账号数据里某角色的属性+神器+全套装备拼成 FinishScene 请求要的角色快照。

    字段结构照抓包 CampData1.PositionRoleMap 里的角色对象还原：
    角色本体来自 RoleDataContainer，神器按 EquipRole 从 Artifacts 里找，
    装备按 EquipRole 从 EquipmentContainer 里找、再按 StaticID 末位归到对应部位键。
    """
    role = _find_by_oid(state.get("RoleDataContainer", {}).get("Roles", []), role_id)
    if role is None:
        raise GameError(f"角色 {role_id} 未在账号数据中找到，队伍设置可能已失效，请重新登录后再试")
    payload = _to_send_format(role)

    art = _find_by_oid(state.get("Artifacts", {}).get("Datas", []), role_id, id_field="EquipRole")
    if art:
        payload["ArtifactData"] = _to_send_format(art)

    eq_map = {}
    for eq in state.get("EquipmentContainer", {}).get("Datas", []):
        if _oid(eq.get("EquipRole")) != role_id:
            continue
        key = _BATTLE_SLOT_KEY.get(str(eq.get("StaticID", ""))[-1:])
        if key:
            eq_map[key] = _to_send_format(eq)
    if eq_map:
        payload["EquipmentMap"] = eq_map
    return payload


def list_teams(c: GameClient) -> list[dict]:
    """账号已保存的队伍(供扫荡等功能选队伍用，前端只展示名称)。"""
    settings = (c.account_state or {}).get("Teams", {}).get("Settings", [])
    return [{"id": str(t.get("ID")), "name": t.get("Name") or f"队伍{t.get('ID')}"} for t in settings]


# 各关卡类型的"战斗元信息"照各自抓包请求原样复现(不跨类型套用——只 1~2 个样本，
# 不确定服务器校验哪些，先严格各按各的抓包值)。
#   讨伐扫荡/元素探索(扫荡按钮): IsRestart=0 IsRepeatAuto=1 FinishWave=1 且带 Camp2DeadList=[]
#   活动关卡(带助战):             IsRestart=1 IsRepeatAuto=0 FinishWave=0 且无 Camp2DeadList
_SWEEP_META = {"is_restart": 0, "is_repeat_auto": 1, "finish_wave": 1, "dead_list": True}
_ACTIVITY_META = {"is_restart": 1, "is_repeat_auto": 0, "finish_wave": 0, "dead_list": False}


def _finish_scene(c: GameClient, scene_id: str, team_id: str,
                  quick_battle: bool = True, support: dict | None = None,
                  meta: dict | None = None, fail_label: str = "扫荡") -> dict:
    """提交一次关卡结算(照抓包 SceneHandler.FinishScene 请求复现)。
    讨伐扫荡/元素探索/活动关卡共用；活动关卡多传 support(助战成员)+ 用 _ACTIVITY_META。

    两处暂时简化(先跑通再补)：
    - Camp2DeadList(战斗中被击杀的敌方单位标识)留空/不带——先按抓包原样(扫荡带空列表、活动不带)，若遇到问题再抓更多样本补充。
    - 体力不足/背包已满等失败场景目前无法从请求前置判断(需要专门抓包该失败响应)，
      先用"响应里有没有 Drop.Items"作为唯一成功判据：没有掉落就当失败并停止循环。
    """
    m = meta or _SWEEP_META
    state = c.account_state or {}
    team = next((t for t in state.get("Teams", {}).get("Settings", [])
                 if str(t.get("ID")) == str(team_id)), None)
    if team is None:
        raise GameError(f"队伍 {team_id} 不存在，请检查队伍设置")
    scene = next((s for s in state.get("SceneDataContainer", {}).get("Scenes", [])
                  if s.get("StaticID") == scene_id), None)
    if scene is None:
        raise GameError(f"关卡 {scene_id} 未在账号数据中找到(可能尚未解锁)")

    role_pos_map = team.get("TeamSetting", {}).get("RolePosMap", {})
    if not role_pos_map:
        raise GameError(f"队伍 {team.get('Name') or team_id} 没有配置角色")
    position_role_map = {
        str(pos): _build_role_for_battle(state, role_id)
        for role_id, pos in role_pos_map.items()
    }

    # 按抓包字段顺序构建 StartBattleInfo(Support 在 IsRepeatAuto 之后、仅活动关卡带)
    start_info = {
        "SceneData": _to_send_format(scene),
        "CampData1": {"PositionRoleMap": position_role_map},
        "CampData2": {},
        "IsRestart": m["is_restart"],
        "Round": 0,
        "GM_Wave": 0,
        "IsRepeatAuto": m["is_repeat_auto"],
    }
    if support is not None:
        start_info["Support"] = support
    start_info["BattleCountDown"] = -1
    start_info["IsNPCPVP"] = 0

    battle_end = {"StartBattleInfo": start_info}
    if m["dead_list"]:
        battle_end["Camp2DeadList"] = []
    battle_end["Result"] = "Win"
    battle_end["TurnRole"] = 0
    battle_end["FinishWave"] = m["finish_wave"]
    payload = {
        "BattleEndData": battle_end,
        "IsQuickBattle": 1 if quick_battle else 0,   # 勾选=用快速战斗券
    }
    try:
        res = c.call("SceneHandler.FinishScene", c._auth_data(payload))
    except GameMessage as m:
        return {"ok": False, "detail": f"{fail_label}失败：{m.msg}"}

    # 掉落里既可能是普通道具(Item)，也可能是装备(Equipment)。分开处理：
    # 道具累加数量、装备各自解析成卡片(可点击查看详情)。
    drop_items = (res.get("Drop", {}) or {}).get("Items", [])
    counter: dict = {}
    equips = []
    for it in drop_items:
        eq = it.get("Equipment")
        if eq is not None:
            card = parse_equipment(eq)
            equips.append(card)
        else:
            _collect_items([it], counter)

    if not counter and not equips:
        return {"ok": False, "detail": "未检测到掉落（可能是网络问题/体力不足/背包已满），已停止"}

    items_txt = _fmt_items(counter) if counter else ""
    # 掉落合成**一条**日志：道具文字 + 装备名(前端把每个装备名渲染成可点击链接→
    # 弹详情卡片)。早期版本每件装备单独一行，导致同一次扫荡刷出多条同序号日志。
    if equips:
        eq_list = []
        for card in equips:
            tier = card.get("tierName") or ""
            lvl = card.get("level")
            lv_txt = f"{lvl}级" if lvl else ""
            eq_list.append({"text": f"{lv_txt}{tier}{card.get('name') or card.get('staticID')}",
                            "card": card})
        line = {"type": "drop", "text": items_txt, "equips": eq_list}
        # detail = 同一句式的纯文本版(前端把装备名渲染成链接，这里不带链接)
        names = [e["text"] for e in eq_list]
        detail = "获得 " + "、".join(([items_txt] if items_txt else []) + names)
    else:
        line = f"获得 {items_txt}"
        detail = line

    return {"ok": True, "lines": [line], "detail": detail, "raw": None}


def sweep_once(c: GameClient, scene_id: str, team_id: str, quick_battle: bool = True) -> dict:
    """讨伐扫荡/元素探索一次(无助战)。见 _finish_scene。"""
    return _finish_scene(c, scene_id, team_id, quick_battle, support=None, fail_label="扫荡")


# ============ 活动关卡(角色活动 Branch*) ============
# 场景 StaticID = BH<角色号>_章_关(如 BH188_1_13)。与讨伐/元素同走 FinishScene，
# 唯一区别: 请求带 StartBattleInfo.Support(助战成员)。活动限时、换期即换 BH 族。
_ACTIVITY_RE = re.compile(r"^(BH\d+)_(\d+)_(\d+)$")
_ACTIVITY_NAMES: dict | None = None


def _activity_names() -> dict:
    """读取随应用发布的 BH### -> 章节中文名索引，懒加载。"""
    global _ACTIVITY_NAMES
    if _ACTIVITY_NAMES is None:
        p = os.path.join(os.path.dirname(__file__), "activity_names.json")
        try:
            with open(p, encoding="utf-8") as f:
                _ACTIVITY_NAMES = json.load(f)
        except Exception:
            _ACTIVITY_NAMES = {}
    return _ACTIVITY_NAMES


def _activity_schedule(state: dict) -> dict:
    """服务器下发的活动排期表: ActivityID -> 排期条目。

    来自登录快照顶层 ActivityDBSettingContainer(263 条, 覆盖所有类型活动)，
    每条 {ActivityID, StartTime:{$date}, EndTime:{$date}, IsIgnoreTime, IsRepeat, ...}。
    """
    return {a.get("ActivityID"): a
            for a in state.get("ActivityDBSettingContainer", {}).get("SettingList", [])
            if a.get("ActivityID")}


def _is_activity_live(entry: dict | None, now_ms: int) -> bool:
    """排期条目是否处于当期。无条目=不在当期(排期表只保留当期+上一期 Branch)。

    IsIgnoreTime=True 表示该活动不受时间窗约束(服务器语义，如 Version_* 那类
    时间窗只有一天却长期生效的条目)，直接视为有效。
    """
    if not entry:
        return False
    if entry.get("IsIgnoreTime"):
        return True
    start = _ms(entry.get("StartTime")) or 0
    end = _ms(entry.get("EndTime")) or 0
    return start <= now_ms <= end


# 活动关卡的**难度槽位**(段号 = StaticID 第三段)。
# 破解依据(用满级号 23 个 BH 族 + 官方 loc 对照): loc 键后缀 = 段号 − 1
# (T_Scene_Name_BranchH001_0/2/4.. 这些"有名字的剧情关"正好落在 Stars=[1,1,1] 的
# 段号 1/3/5..)，故 loc 的 `_11 Hard / _12 Elite / _13 Hell`
# (= 困难难度/精英难度/地狱难度) 对应段号 12/13/14。
# 关键佐证: BH112 的段号是 1..9 然后**直接跳到 12,13** —— 12/13 是固定难度槽位，
# 不是"最后一关"，所以只能按段号查表，不能按"第几关"推。
# 段号 ≤11 = 剧情关(无难度标签); 14(地狱) 在现有全部快照里从未出现过。
_ACTIVITY_TIERS = {12: "困难", 13: "精英", 14: "地狱"}


def activity_scenes(state: dict) -> list[dict]:
    """从账号快照抽出**当期**可打的活动关卡族(每族取最高关=boss关)。

    返回 [{id: 最高关StaticID, family: BH###, name: 展示名, hero: 角色名, tier: 难度}]。
    展示名 = "章节名 · 难度"(章节名查 activity_names, 难度按段号查 _ACTIVITY_TIERS);
    章节名缺失回退族标识、难度缺失省略(纯展示，不阻断)。角色名仍随条目返回但不进展示名。

    ⚠️ 必须按排期过滤: 活动结束后 SceneDataContainer 里的 BH 关卡记录**不会**可靠清理
    (实测 BH187 结束 8 天后仍在，满级号积压 22 个历史族)，全列出来用户会选到已过期的
    活动(如水纪 BranchH604 07-22 结束)。族标识 BH### 对应 ActivityID Branch H###，
    只保留排期表判定为当期的族，其余一律不展示。
    """
    fam_stages: dict[str, list[tuple]] = {}
    for s in state.get("SceneDataContainer", {}).get("Scenes", []):
        m = _ACTIVITY_RE.match(str(s.get("StaticID", "")))
        if not m:
            continue
        fam_stages.setdefault(m.group(1), []).append(
            (int(m.group(2)), int(m.group(3)), m.group(0)))
    names = _activity_names()
    sched = _activity_schedule(state)
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    out = []
    for fam, stages in fam_stages.items():
        # BH188 -> BranchH188。排期表非当期(含查不到)的族直接跳过。
        if not _is_activity_live(sched.get("Branch" + fam[1:]), now_ms):
            continue
        chapter_no, stage_no, top = max(stages)   # (章,关) 最大 = 最高难度关
        hero = item_name("H" + fam[2:])           # BH188 -> H188 -> 角色名
        chapter = names.get(fam)
        tier = _ACTIVITY_TIERS.get(stage_no)
        # 展示名: 章节名 · 难度(用户定，不带角色名)。章节名查不到回退族标识、
        # 难度缺失省略——纯展示 fail-soft，不阻断
        label = chapter or fam
        if tier:
            label += f" · {tier}"
        out.append({"id": top, "family": fam, "name": label,
                    "hero": hero, "tier": tier})
    out.sort(key=lambda x: x["family"])
    return out


def _support_captain(entry: dict) -> dict | None:
    """从助战列表条目里取 SupportJob=="Captain"(队长)的角色，无则 None。"""
    for rd in entry.get("RoleDataList", []):
        if rd.get("SupportJob") == "Captain":
            return rd
    return None


# 助战成员 PlayerInfo 只保留这些字段(照抓包 Support.PlayerRoleData.PlayerInfo)。
_SUPPORT_PINFO_KEYS = ["Name", "CUID", "LV", "Dialog", "Frame", "LeaderSID",
                       "LoginTime", "CreateTime"]


def support_brief(entry: dict) -> dict | None:
    """助战条目 -> 前端展示用精简项，无队长/无 CUID 则 None。

    {cuid, name, hero:{id,name,level,star,imprint}}。助战列表和收藏夹共用，
    保证两边卡片长得一样(avatar/grade 由 appcore 补，见 activity_options)。
    """
    cap = _support_captain(entry)
    if cap is None:
        return None
    pi = entry.get("PlayerInfo", {})
    cuid = pi.get("CUID")
    if cuid is None:
        return None
    role = cap.get("Role", {})
    hid = str(role.get("StaticID", ""))
    return {
        "cuid": cuid,
        "name": pi.get("Name") or "",
        "hero": {
            "id": hid,
            "name": item_name(hid),
            "level": role.get("LV"),
            "star": role.get("Star"),
            "imprint": int(role.get("ImprintLV") or 0),
        },
    }


def trim_support_entry(entry: dict) -> dict:
    """裁剪助战条目供收藏夹持久化：只留 PlayerInfo(8字段) + 队长一个职业。

    整条含 7 个职业时 8~28KB，只留队长约 4KB。结构与原始条目同构，
    所以 _build_support 能直接吃裁剪后的条目，无需特殊分支。
    """
    cap = _support_captain(entry)
    if cap is None:
        raise GameError("该助战成员没有队长(Captain)角色，无法收藏")
    pi = entry.get("PlayerInfo", {})
    return {
        "IsFriend": 1 if entry.get("IsFriend") else 0,
        "PlayerInfo": {k: pi[k] for k in _SUPPORT_PINFO_KEYS if k in pi},
        "RoleDataList": [cap],
    }


def query_support_friends(c: GameClient) -> tuple[list[dict], dict]:
    """拉取战斗助战列表，只留好友(IsFriend==true)且有队长(Captain)角色的。

    返回 (精简列表 供前端展示, 原始条目缓存 cuid->entry 供构建 payload)。
    """
    res = c.call("SupportFriendHandler.QueryBattleSupportDataList", c._auth_data())
    friends = []
    raw: dict = {}
    for entry in res.get("Datas", []):
        if not entry.get("IsFriend"):
            continue
        brief = support_brief(entry)
        if brief is None:
            continue
        friends.append(brief)
        raw[str(brief["cuid"])] = entry
    return friends, raw


def _build_support(entry: dict) -> dict:
    """把助战列表里某好友的队长(Captain)角色拼成 FinishScene 的 Support 对象。

    结构照抓包: {PlayerRoleData:{PlayerInfo(裁剪), RoleData(_to_send_format)},
                 IsFriend, Job:"Captain", IsNPC:0}。
    """
    cap = _support_captain(entry)
    if cap is None:
        raise GameError("该助战成员没有队长(Captain)角色")
    role = cap.get("Role", {})
    pi = entry.get("PlayerInfo", {})
    player_info = {k: _to_send_format(pi[k]) for k in _SUPPORT_PINFO_KEYS if k in pi}
    return {
        "PlayerRoleData": {
            "PlayerInfo": player_info,
            "RoleData": _to_send_format(role),
        },
        "IsFriend": 1 if entry.get("IsFriend") else 0,
        "Job": cap.get("SupportJob", "Captain"),
        "IsNPC": 0,
    }


def activity_once(c: GameClient, scene_id: str, team_id: str,
                  support_entry: dict | None = None, quick_battle: bool = True) -> dict:
    """活动关卡一次。support_entry=助战列表原始条目(None=不用助战)。"""
    support = _build_support(support_entry) if support_entry else None
    return _finish_scene(c, scene_id, team_id, quick_battle, support=support,
                         meta=_ACTIVITY_META, fail_label="活动关卡")


# ============ 竞技场 NPC(HellNPC) ============
# 一次扫荡 = 两个请求(与 FinishScene 系完全不同的一条链):
#   ① PVPHandler.PVPCheckTicket {NPCSceneID, IsRevenge:0} -> {LogID}  **扣 1 面旗帜**
#   ② PVPHandler.NPCPVPBattleEnd {NPCSceneID, IsRevenge:0, EnemyLogID:①的LogID, EndData}
#      -> {IsWin, AddScore, Drop, NPCInfo{_id,NPCID,NextTime}}  NextTime=该 NPC 新冷却
# 场景族三档难度 NormalNPC_/HardNPC_/HellNPC_<1..10>，本功能只做地狱级(用户定)。
ARENA_NPC_COUNT = 10
ARENA_CHECK_ROUTE = "PVPHandler.PVPCheckTicket"
ARENA_END_ROUTE = "PVPHandler.NPCPVPBattleEnd"
ARENA_FLAG_ID = "4"          # 旗帜(每次挑战扣 1，MaxCount 5，随时间恢复)

# 竞技场战斗元信息：**与扫荡/活动都不同的第三套**，照抓包原样(别跨类型套用)。
#   扫荡 IsRepeatAuto=1/FinishWave=1；活动 IsRestart=1；竞技场全 0 且 IsNPCPVP=1。
# SceneData 也不是从 SceneDataContainer 取，抓包里固定是 PVP 这一条。
_ARENA_META = {"IsRestart": 0, "Round": 0, "GM_Wave": 0, "IsRepeatAuto": 0,
               "BattleCountDown": -1, "IsNPCPVP": 1}
_ARENA_SCENE_DATA = {"StaticID": "PVP", "Stars": [0, 0, 0], "PassCount": 0}

_ARENA_DATA: dict | None = None


def _arena_data() -> dict:
    """读取随应用发布的竞技场队名表与敌方阵容模板。"""
    global _ARENA_DATA
    if _ARENA_DATA is None:
        p = os.path.join(os.path.dirname(__file__), "arena_npc.json")
        try:
            with open(p, encoding="utf-8") as f:
                _ARENA_DATA = json.load(f)
        except Exception:
            _ARENA_DATA = {}
    return _ARENA_DATA


def arena_npc_name(n: int) -> str:
    """第 n 号 NPC 的队伍中文名。查不到就抛错(名字表是打包必需项，缺了要暴露)。"""
    name = (_arena_data().get("names") or {}).get(str(n))
    if not name:
        raise GameError(
            f"NPC{n} 的队伍名未收录，请更新 backend/arena_npc.json")
    return name


def arena_scene_id(n: int) -> str:
    return f"HellNPC_{n}"


def arena_npcs() -> list[dict]:
    """10 个地狱级 NPC 的清单(供前端展开页渲染)。"""
    return [{"n": n, "sceneId": arena_scene_id(n), "name": arena_npc_name(n)}
            for n in range(1, ARENA_NPC_COUNT + 1)]


def arena_next_times(state: dict) -> dict[str, int]:
    """从登录快照算出每个 NPC 的**权威可挑战时刻**。返回 {sceneId: 毫秒}。

    数据源 `PVPData.NPCPVPInfoList`：30 条 = 10 个 NPC × 普通/困难/地狱三难度，
    每条只有 `{_id, NPCID, NextTime}`、**没有难度字段**。

    **三个难度共享同一个冷却**(用户在游戏内确认)，所以同一 NPCID 的三条里
    **取 NextTime 最大值**就是该 NPC 的真实下次可挑战时刻——挑战任一难度只会更新
    它自己那条，另两条留在更早的旧值/占位值上，故最大值 = 最近一次挑战产生的那条。
    实测验证：账号A 有 7 个 NPC 的地狱级 _id 是扫荡响应 `NPCInfo` 实测确认的，
    `max(三条)` 与那 7 条**逐个吻合**(7/7)。
    (顺带解释了"数组第 0 条"之谜：冷却共享 ⇒ 最近被挑战的那条就是最大值，
     服务器把最近更新的排在最前，所以第 0 条恰好等于 max——但别依赖顺序，直接取 max。)

    这样就**不需要**知道哪条是地狱级，也**不需要**额外请求 `QueryPVPData`：
    登录快照里已经有了。查不到某 NPC 的条目 = 从未挑战过 ⇒ 不设闸门(可立即挑战)。
    """
    out: dict[str, int] = {}
    for e in (state or {}).get("PVPData", {}).get("NPCPVPInfoList") or []:
        try:
            n = int(e.get("NPCID"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= ARENA_NPC_COUNT:
            continue
        ms = _ms(e.get("NextTime"))
        if ms is None:
            continue
        sid = arena_scene_id(n)
        prev = out.get(sid)
        if prev is None or ms > prev:
            out[sid] = ms
    return out


def _build_arena_enemy() -> tuple[dict, list[str]]:
    """构建 CampData2(敌方阵容) + Camp2DeadList(全灭=胜)。返回 (campData2, deadList)。

    **一份模板打全部 10 个 NPC**——用户点破的关键: 敌方 UUID 是**客户端每场随机生成**
    的(同一个 HellNPC_8 在两个账号的抓包里 StaticID 相同、UUID 完全不同)，而 NPC 阵容
    在**服务器侧本来就是固定的**，所以服务器不可能拿我们发的 CampData2 去校验是哪个 NPC。
    旁证: _finish_scene 给讨伐/元素/活动发 `CampData2:{}` 空对象也能过。
    因此不需要"每 NPC 一张阵容表"(抓包缺 NPC2/4/6 也不影响)。
    UUID 每次 uuid4() 现生成，Camp2DeadList 列全 4 个 = 全灭 = 胜。
    """
    tpl = _arena_data().get("enemyTemplate") or {}
    roles = tpl.get("PositionRoleMap") or {}
    if not roles:
        raise GameError(
            "竞技场敌方阵容模板缺失，请更新 backend/arena_npc.json 的 enemyTemplate")
    pos_map = {}
    dead = []
    for pos, role in roles.items():
        uid = str(uuid.uuid4())
        pos_map[pos] = {"_id": uid, **role}
        dead.append(uid)
    return {"Name": tpl.get("Name", ""), "PositionRoleMap": pos_map}, dead


def arena_sweep_once(c: GameClient, n: int, team_id: str) -> dict:
    """挑战一次地狱级 NPC n。返回 {ok, detail, nextMs, oid, ...}。

    失败(冷却中/旗帜不足/服务器拒绝)统一返回 ok=False + detail，由调用方决定退避。
    ⚠️ 已知代价: ①扣旗帜，②**没有"冷却中被拒"的抓包样本**，不确定拒绝发生在扣旗
    之前还是之后——真机首次跑要看日志确认(见 [[ark-arena-npc]])。
    """
    state = c.account_state or {}
    scene_id = arena_scene_id(n)
    name = arena_npc_name(n)

    team = next((t for t in state.get("Teams", {}).get("Settings", [])
                 if str(t.get("ID")) == str(team_id)), None)
    if team is None:
        raise GameError(f"队伍 {team_id} 不存在，请检查队伍设置")
    role_pos_map = (team.get("TeamSetting") or {}).get("RolePosMap") or {}
    if not role_pos_map:
        raise GameError(f"队伍 {team.get('Name') or team_id} 没有配置角色")

    # ① 领战斗券(扣旗帜) -> LogID
    try:
        chk = c.call(ARENA_CHECK_ROUTE,
                     c._auth_data({"NPCSceneID": scene_id, "IsRevenge": 0}))
    except GameMessage as m:
        return {"ok": False, "detail": f"{name}：{m.msg}", "raw": m.msg}
    log_id = chk.get("LogID")
    if not log_id:
        return {"ok": False, "detail": f"{name}：未取到战斗编号(LogID)，已跳过"}

    # ② 提交战斗结果。CampData1 带玩家昵称(照抓包，_finish_scene 那边不带 Name)
    position_role_map = {
        str(pos): _build_role_for_battle(state, role_id)
        for role_id, pos in role_pos_map.items()
    }
    camp2, dead_list = _build_arena_enemy()
    payload = {
        "NPCSceneID": scene_id,
        "IsRevenge": 0,
        "EnemyLogID": log_id,
        "EndData": {
            "StartBattleInfo": {
                "SceneData": dict(_ARENA_SCENE_DATA),
                "CampData1": {
                    "Name": (state.get("Info") or {}).get("Name") or "",
                    "PositionRoleMap": position_role_map,
                },
                "CampData2": camp2,
                **_ARENA_META,
            },
            "Camp2DeadList": dead_list,
            "Result": "Win",
            "TurnRole": 0,
            "FinishWave": 0,
        },
    }
    try:
        res = c.call(ARENA_END_ROUTE, c._auth_data(payload))
    except GameMessage as m:
        return {"ok": False, "detail": f"{name}：{m.msg}", "raw": m.msg}

    info = res.get("NPCInfo") or {}
    next_ms = _ms(info.get("NextTime"))
    oid = _oid(info.get("_id"))
    counter: dict = {}
    _collect_items((res.get("Drop", {}) or {}).get("Items", []), counter)
    items_txt = _fmt_items(counter)
    win = bool(res.get("IsWin"))
    score = res.get("AddScore")
    bits = [name, "胜" if win else "负"]
    if score:
        bits.append(f"积分+{score}")
    if items_txt:
        bits.append(f"获得 {items_txt}")
    return {"ok": True, "detail": "，".join(bits), "nextMs": next_ms, "oid": oid,
            "isWin": win, "sceneId": scene_id, "raw": None}


# 扫荡失败后的固定退避：1.01 小时。
# 为什么是固定值而不是读游戏侧冷却：失败的主因是**旗帜不足**(每次挑战扣 1、上限 5、
# 随时间恢复)，而我们不实时监控旗帜；旗帜约 1 小时恢复 1 面，所以等 1.01h 再试最省事，
# 多的 0.01h 是防边界(刚好差几秒没恢复满又白扣一次判定)。用户拍板。
ARENA_RETRY_GAP_MS = int(1.01 * 3600 * 1000)


def task_arena_npc(c: GameClient, params: dict | None = None,
                   rt: dict | None = None) -> dict:
    """竞技场 NPC(地狱级) 自动扫荡。勾选的 NPC 逐个挑战，失败的挂 1.01h 退避。

    params: {"picked": [n...], "teamId": "0"}
    rt:     该账号的运行时状态(由 accounts.Account 持有、跨轮次保留、重登即清):
            {"nextMs": {sceneId: 毫秒}, "retryMs": {sceneId: 毫秒}}
      - nextMs  = 本工具扫荡后从响应 `NPCInfo.NextTime` 拿到的新冷却
      - retryMs = 失败后的 1.01h 退避

    **闸门 = max(登录快照算出的冷却, 本轮跑出来的 nextMs, retryMs)**。
    快照那份是关键：早先只认内存里的 nextMs，一换账号就是全新 Account ⇒ 全部显示
    "可挑战" ⇒ 盲打一轮，**每个都白扣一面旗帜**才从响应得知还在冷却（实测确认扣旗
    发生在拒绝之前）。现在登录/重登就能从快照读出真实档期，不会再白扣。
    快照冷却由 `arena_next_times` 算（同 NPCID 三条取 max，三难度共享冷却）。
    """
    params = params or {}
    rt = rt if rt is not None else {}
    picked = [int(x) for x in (params.get("picked") or [])]
    team_id = str(params.get("teamId") or "0")
    if not picked:
        return {"ok": True, "skipped": True, "detail": "未勾选任何 NPC，跳过"}

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    next_map = rt.setdefault("nextMs", {})
    retry_map = rt.setdefault("retryMs", {})
    snap_map = arena_next_times(c.account_state or {})

    due, waiting = [], 0
    for n in sorted(picked):
        sid = arena_scene_id(n)
        gate = max(snap_map.get(sid) or 0, next_map.get(sid) or 0,
                   retry_map.get(sid) or 0)
        if gate and now_ms < gate:
            waiting += 1
            continue
        due.append(n)
    if not due:
        return {"ok": True, "skipped": True,
                "detail": f"勾选的 {waiting} 个 NPC 都在冷却中，跳过"}

    lines, ok_count, fail_count = [], 0, 0
    for n in due:
        sid = arena_scene_id(n)
        try:
            r = arena_sweep_once(c, n, team_id)
        except GameError as e:
            # 队伍/名字表这类配置问题：整个任务失败，不逐个重试
            return {"ok": False, "detail": str(e)}
        if r.get("ok"):
            ok_count += 1
            retry_map.pop(sid, None)
            if r.get("nextMs"):
                next_map[sid] = r["nextMs"]
        else:
            fail_count += 1
            retry_map[sid] = now_ms + ARENA_RETRY_GAP_MS
        lines.append(r["detail"])

    head = f"挑战 {len(due)} 个：成功 {ok_count}"
    if fail_count:
        head += f"、失败 {fail_count}（各等 1.01 小时后重试）"
    if waiting:
        head += f"；{waiting} 个仍在冷却"
    return {"ok": fail_count == 0, "lines": [head] + lines, "detail": head}


def task_friend_support(c: GameClient) -> dict:
    """助战好友日常奖励。"""
    try:
        res = c.call("SupportFriendHandler.GetReward", c._auth_data())
    except GameMessage as m:
        ok = _is_done_msg(m.msg)
        return {"ok": ok, "detail": ("助战奖励已领取" if ok else f"领取失败：{m.msg}"), "raw": m.msg}
    point = res.get("SupportInfo", {}).get("Point")
    return {"ok": True, "detail": f"助战奖励已领取，当前点数 {point}", "raw": res}


# 任务注册表：id -> 元信息。
# kind: "always"=登录必跑, "toggle"=登录后按开关跑（可每小时重跑）, "manual"=手动列表按钮
TASKS: dict[str, dict] = {
    "month_signin": {
        "name": "月签到", "desc": "每日月历签到",
        "fn": task_month_signin, "kind": "always",
    },
    "week_signin": {
        "name": "活动签到", "desc": "活动/周签到（自动读取当前活动）",
        "fn": task_week_signin, "kind": "always",
    },
    "friend_support": {
        "name": "助战好友", "desc": "领取助战好友每日奖励",
        "fn": task_friend_support, "kind": "always",
    },
    "send_meal": {
        "name": "月卡领取", "desc": "投递月卡与午晚餐奖励至邮箱（登录自动触发）",
        "fn": task_send_meal, "kind": "always",
    },
    "reactor": {
        "name": "动力转换", "desc": "基地 · 动力转换（满 12 小时才领取）",
        "fn": task_reactor, "kind": "toggle",
    },
    "sf_potion": {
        "name": "成长药剂", "desc": "基地 · 生产中心（到点即领）",
        "fn": task_potion, "kind": "toggle",
    },
    "sf_starforce": {
        "name": "升星水晶", "desc": "基地 · 生产中心（到点即领）",
        "fn": task_starforce, "kind": "toggle",
    },
    "sf_tesseract": {
        "name": "技能模块", "desc": "基地 · 生产中心（到点即领，生产中自动加速 3 次）",
        "fn": task_tesseract, "kind": "toggle",
    },
    "dispatch": {
        "name": "派遣任务", "desc": "到点领取并用原队伍自动续派（体力不足只领不派）",
        "fn": task_dispatch, "kind": "toggle",
    },
    "shop_free": {
        "name": "秘密商店刷新(免费)", "desc": "每小时用免费额度刷新并自动购买目标道具",
        "fn": task_shop_free, "kind": "toggle", "wantsBuyTargets": True,
    },
    # needsFreshState：判定只看账号快照里的"每日重置"字段（同步器补充 / 免费额度），
    # 而快照只在 bootstrap 时刷新 —— 后台调度据此每小时重登一次，否则挂机过夜后
    # 永远看不到第二天的重置（详见 accounts.Account._refresh_state_if_needed）。
    "abyss_sweep": {
        "name": "虚拟幻境", "desc": f"扫荡已通关最高层（未达 {ABYSS_MIN_FLOOR} 层不扫）",
        "fn": task_abyss_sweep, "kind": "toggle", "needsFreshState": True,
    },
    "free_summon": {
        "name": "每日免费招募", "desc": "每日常规招募的免费额度，只在确认免费时抽取",
        "fn": task_free_summon, "kind": "toggle", "needsFreshState": True,
    },
    # 唯一"开关 + 展开页"的 toggle 任务(其余带 ui 的都是 manual)：展开页里勾选要打的
    # NPC。wantsRuntime=True 让 run_toggle_tasks 把该账号的退避状态传给 fn 第三参。
    "arena_npc": {
        "name": "竞技场 NPC", "desc": "自动挑战勾选的地狱级 NPC（每次消耗 1 面旗帜）",
        "fn": task_arena_npc, "kind": "toggle", "ui": "arena", "wantsRuntime": True,
    },
    "claim_mail": {
        "name": "一键领邮件", "desc": "领取邮箱内所有未领奖励",
        "fn": task_claim_mail, "kind": "manual",
    },
    "shop_diamond": {
        "name": "秘密商店刷新(钻石)", "desc": "花钻石刷新指定次数，遇目标道具自动购买",
        "kind": "manual", "ui": "shop",  # 前端自定义渲染(选项展开+卡片)，走 /api/shop/* 而非 /api/tasks/run
    },
    "hunt_sweep": {
        "name": "讨伐扫荡", "desc": "选关卡+队伍，按次数循环扫荡，无掉落自动停止",
        "kind": "manual", "ui": "sweep", "sweepKind": "hunt",  # 前端自定义渲染，走 /api/sweep/* 而非 /api/tasks/run
    },
    "element_sweep": {
        "name": "元素探索", "desc": "选元素关卡(5元素·地狱级)+队伍，按次数循环扫荡，无掉落自动停止",
        "kind": "manual", "ui": "sweep", "sweepKind": "elf",
    },
    "activity_scene": {
        "name": "活动关卡", "desc": "选当期活动关卡+队伍+助战好友，按次数循环扫荡，无掉落自动停止",
        "kind": "manual", "ui": "activity",  # 前端自定义渲染，走 /api/activity/* 而非 /api/tasks/run
    },
    "store_buy": {
        "name": "商店购买", "desc": "按商店切换货架，勾选要买的商品后一键批量购买",
        "kind": "manual", "ui": "storebuy",  # 前端自定义渲染，走 /api/store/*
    },
}


def run_task(client: GameClient, task_id: str, params: dict | None = None,
             runtime: dict | None = None) -> dict:
    if task_id not in TASKS:
        raise GameError(f"未知任务: {task_id}")
    meta = TASKS[task_id]
    fn = meta.get("fn")
    if fn is None:
        raise GameError(f"任务 {task_id} 无执行函数（前端自定义 UI 任务，应走专用接口）")
    # wantsRuntime 的任务(竞技场)要第三参：该账号跨轮次保留的退避状态
    if meta.get("wantsRuntime"):
        return fn(client, params, runtime if runtime is not None else {})
    # 只给接受第二个形参的任务传参（shop 系列）
    if len(inspect.signature(fn).parameters) >= 2:
        return fn(client, params)
    return fn(client)


def run_auto_tasks(client: GameClient) -> list[dict]:
    """登录必跑任务（月签/活动签/助战），返回结果列表。"""
    results = []
    for tid, t in TASKS.items():
        if t.get("kind") != "always":
            continue
        try:
            r = run_task(client, tid)
        except Exception as e:
            r = {"ok": False, "detail": str(e)}
        results.append({"id": tid, "name": t["name"], **r})
    return results


def run_toggle_tasks(client: GameClient, task_ids: list[str], params: dict | None = None,
                     runtime: dict | None = None) -> list[dict]:
    """按前端开关执行 toggle 类任务。params 为 {task_id: {...}} 或全局 dict。

    runtime: {task_id: {...}} 每任务的跨轮次运行时状态(目前只有竞技场用，存退避)。
    由 accounts.Account 持有并原地改写，所以调用方不需要读回返回值。
    """
    params = params or {}
    runtime = runtime if runtime is not None else {}
    results = []
    for tid in task_ids:
        t = TASKS.get(tid)
        if not t or t.get("kind") != "toggle":
            continue
        p = params.get(tid, params)  # 支持按任务传参或共享参数
        try:
            r = run_task(client, tid, p, runtime.setdefault(tid, {}))
        except Exception as e:
            r = {"ok": False, "detail": str(e)}
        results.append({"id": tid, "name": t["name"], **r})
    return results


def _list_by_kind(kind: str) -> list[dict]:
    return [
        {"id": tid, "name": t["name"], "desc": t["desc"],
         "wantsBuyTargets": t.get("wantsBuyTargets", False),
         "wantsCount": t.get("wantsCount", False),
         "ui": t.get("ui", ""), "sweepKind": t.get("sweepKind", "")}
        for tid, t in TASKS.items()
        if t.get("kind") == kind
    ]


def needs_fresh_state(task_ids: list[str]) -> bool:
    """这批任务里是否有"判定依赖每日重置字段"的（需要先刷新账号快照）。"""
    return any(TASKS.get(tid, {}).get("needsFreshState") for tid in task_ids or [])


def toggle_tasks() -> list[dict]:
    """登录后按开关执行的自动任务（前端"自动功能"区）。"""
    return _list_by_kind("toggle")


def manual_tasks() -> list[dict]:
    """手动列表任务。"""
    return _list_by_kind("manual")


def next_claim_times(state: dict) -> dict:
    """各定时 toggle 任务的"下次可领时间"，供前端在开关旁展示。

    返回 {task_id: {"nextMs": 毫秒时间戳 or None, "ready": bool}}。
    读的是 account_state 缓存（登录快照 + 每次领取后的写回），比较绝对时间戳，
    与任务内部的到点判断同源，所以展示与实际巡检行为一致。
      - reactor: 上次领取 + 12h
      - 基地生产中心三项: 各自 NextCanReceive*Time
      - dispatch: 最早一个未完成派遣的 FinishTime（全空/全完成则视为可领/无）
      - shop_free: 无游戏侧下次时间（每小时免费额度），返回 None + ready=True
      - abyss_sweep / free_summon: 不是计时型，靠 `blocked`/`note` 两个附加键表达
        状态（blocked 非空 = 前端把开关置灰并显示原因，与任务内部的拒绝条件同源）
    """
    state = state or {}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    out: dict = {}

    def entry(next_ms):
        return {"nextMs": next_ms, "ready": (next_ms is None or now_ms >= next_ms)}

    # 动力转换：上次领取 + 12h
    reactor = state.get("ArkReactorData", {}) or {}
    last_ms = _ms(reactor.get("LastMoneyReceivedTime"))
    out["reactor"] = entry((last_ms + REACTOR_FULL_HOURS * 3600_000) if last_ms is not None else None)

    # 基地生产中心三项
    lab = state.get("ArkStarForceLabData", {}) or {}
    for tid, (_disp, field, _route) in zip(
        ("sf_potion", "sf_starforce", "sf_tesseract"), STARFORCE_REWARDS
    ):
        out[tid] = entry(_ms(lab.get(field)))

    # 技能模块额外带"加速"档期：每周期 3 次、间隔 24h（见 _charge_tesseract）。
    # 额度用尽时 NextCanChargeTime 仍是旧值（会显示成"可加速"），故单独标 exhausted。
    used = int(lab.get("TesseractChargeCount") or 0)
    charge = {"used": used, "max": TESSERACT_MAX_CHARGES,
              "exhausted": used >= TESSERACT_MAX_CHARGES}
    if charge["exhausted"]:
        charge.update({"nextMs": None, "ready": False})
    else:
        charge.update(entry(_ms(lab.get("NextCanChargeTime"))))
    out["sf_tesseract"]["charge"] = charge

    # 派遣任务：取最早的未完成 FinishTime；无派遣任务则 ready
    quests = (state.get("ArkHighCommandData", {}) or {}).get("DispatchedQuests") or []
    finishes = [fm for fm in (_ms(q.get("FinishTime")) for q in quests) if fm is not None]
    pending = [fm for fm in finishes if fm > now_ms]
    out["dispatch"] = entry(min(pending) if pending else None)

    # 免费刷商店：游戏不下发"下次免费刷新时刻"（整份登录快照 70692 个字段里没有；
    # 对照 PVP 是有 NextCanFreeRefreshEnemyTime 的，所以不是没找到而是确实不存在）。
    # 但货架记录带 LastResetTime = 上次刷新时刻，免费冷却实测 1 小时（用户在游戏内
    # 看到刷新后冷却显示 59 分钟，加上秒数即 1h；抓包侧免费成功时货架年龄 1.01h/2.78h，
    # 且刚刷完立刻再刷必得 CantReset）。故 下次免费时刻 = LastResetTime + 1h。
    # 刷新后 game_client._apply_store_records 会把新 LastResetTime 写回缓存，倒计时自走。
    out["shop_free"] = entry(secret_shop_next_free(state))

    # 虚拟幻境：提示只报当前最高层（用户定，不再堆同步器余量/门槛说明——
    # 门槛已写在任务描述里）。未达门槛仍要 blocked 把开关置灰。
    prog = abyss_progress(state)
    floor_txt = f"当前最高 {prog['floor']} 层" if prog["floor"] else "尚无通关记录"
    if prog["floor"] < ABYSS_MIN_FLOOR:
        out["abyss_sweep"] = {"nextMs": None, "ready": False, "blocked": floor_txt}
    else:
        out["abyss_sweep"] = {"nextMs": None, "ready": prog["sync"] > 0,
                              "note": floor_txt}

    # 每日免费招募：读常规招募档的 FreeBuyCount（与任务的判定同源）
    rec = _summon_record(state)
    if rec is None:
        out["free_summon"] = {
            "nextMs": None, "ready": False,
            "blocked": "账号快照里没有常规招募档位，请刷新后再试",
        }
    else:
        used = int(rec.get("FreeBuyCount") or 0)
        out["free_summon"] = {
            "nextMs": None, "ready": used == 0,
            "note": "免费额度可用" if used == 0 else f"今日免费额度已用（{used} 次）",
        }
    return out
