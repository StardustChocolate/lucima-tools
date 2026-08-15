"""Ark Re:Code 游戏后端协议客户端。

封装 RouterHandler.ashx 单入口协议:
- 请求: PUT，明文 JSON body {"data":{...}, "route":"Handler.Method"}
- 响应: gzip 压缩的 JSON
- 认证: 登录握手拿到 AID + SessionID，之后每个请求都带上
全程走代理。
"""
from __future__ import annotations

import gzip
import json
from typing import Any

import httpx

from . import config
from .logutil import log

# 要把完整请求/响应体写进开发者日志的 route（花资源、需要事后核对真实价格与
# 服务器裁决的操作）。其余 route 不记——巡检每 5 分钟一轮，全记会把日志刷爆。
_TRACE_ROUTES = ("StoreHandler.BuyCommodity", "SceneHandler.PurityScene",
                 # 秘密商店刷新：免费额度未恢复时服务器回明文 `CantReset`（2026-07-30 真机
                 # 实测），但当时这条 route 没被 trace，原始报文没落盘、事后无法复原。
                 # 免费刷新的档期判定就建立在它的 LastResetTime 上，出问题要能对着报文查。
                 "StoreHandler.ResetRandomStore")
_TRACE_MAX = 4000   # 单条截断长度
# 响应只摘这几个键：购买/扫荡响应会把整个账号状态(AccountInfo/Quests/BattlePass…)
# 一起带回来，几十 KB，直接截断会把真正要看的 Drop/CostItems 切掉。
_TRACE_KEYS = ("Drop", "CostItems", "CommodityRecord", "SceneData", "Event",
               "VIPInfo", "GotStars", "Utc")


def _shelf_digest(body: dict) -> str:
    """商店刷新响应里与"免费档期"有关的几个值，压成一小行。

    不把整个 `Records` 塞进日志：6 档货架带完整装备对象，轻易超过 _TRACE_MAX，
    截断后会把后面的 CostItems（判断有没有扣费的关键）挤掉。这里只留档期要用的
    LastResetTime 与付费计数。
    """
    recs = [r for r in (body.get("Records") or []) if isinstance(r, dict) and r.get("Store")]
    if not recs and "AccountSaveData" not in body:
        return ""
    stamps = sorted({
        (r.get("LastResetTime") or {}).get("$date") if isinstance(r.get("LastResetTime"), dict)
        else r.get("LastResetTime")
        for r in recs
    } - {None})
    cnt = (body.get("AccountSaveData") or {}).get("RandomStoreDayRefreshCount")
    return (f"  · 货架 {len(recs)} 档 LastResetTime={stamps}"
            f" RandomStoreDayRefreshCount={cnt}")


def _distill(body: dict) -> str:
    """从大响应里摘出关注键，其余只列键名（供开发者日志核对真实价格/裁决结果）。"""
    if not isinstance(body, dict):
        return str(body)[:_TRACE_MAX]
    kept = {k: body[k] for k in _TRACE_KEYS if k in body}
    other = [k for k in body if k not in kept]
    return (json.dumps(kept, ensure_ascii=False)[:_TRACE_MAX]
            + f"  · 其余键: {other}" + _shelf_digest(body))


class GameError(Exception):
    """游戏后端返回的业务错误。"""


class GameMessage(Exception):
    """游戏后端返回的明文状态消息(非JSON)，如 'Reward is Finish'、'No Token'。

    game 后端对很多业务状态用明文 'Msg_Msg'(重复)返回而非 JSON。
    msg 为去重后的单条消息。
    """

    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


_HEADERS = {
    "Content-Type": "application/octet-stream",
    "Origin": config.GAME_ORIGIN,
    "Referer": config.GAME_REFERER,
    "Accept": "*/*",
    "Accept-Encoding": "gzip",
}


class GameClient:
    def __init__(self, game_token: str):
        # game_token = 门户的 erolabsjwt，也是 WebGL 客户端 ?token= 的值
        self.game_token = game_token
        self.login_id: str | None = None
        self.aid: str | None = None
        self.session_id: str | None = None
        self.account_state: dict[str, Any] | None = None
        self._proxy: str = config.get_proxy()  # 当前 client 使用的代理
        self._client = self._build_client(self._proxy)

    @staticmethod
    def _build_client(proxy: str) -> httpx.Client:
        return httpx.Client(
            proxy=proxy or None,
            timeout=config.HTTP_TIMEOUT,
            headers=_HEADERS,
        )

    def _sync_proxy(self):
        """动态跟随代理配置：若当前生效代理与 client 所用的不一致，则重建。

        模仿浏览器"每次请求重新解析代理"的行为——用户登录后关掉 Clash，
        系统代理注册表变化会被 get_proxy() 读到，这里据此切换到直连。
        """
        cur = config.get_proxy()
        if cur != self._proxy:
            try:
                self._client.close()
            except Exception:
                pass
            self._proxy = cur
            self._client = self._build_client(cur)

    # ---- 底层调用 ----
    def call(self, route: str, data: dict | None = None) -> dict:
        """调用一个 route，返回解压后的 JSON dict。"""
        self._sync_proxy()
        payload = {"data": data or {}, "route": route}
        body = json.dumps(payload).encode("utf-8")
        trace = route in _TRACE_ROUTES
        if trace:
            log.info("[game→] %s %s", route,
                     json.dumps(payload["data"], ensure_ascii=False)[:_TRACE_MAX])
        try:
            resp = self._client.request("PUT", config.GAME_ROUTER, content=body)
        except (httpx.ProxyError, httpx.ConnectError) as e:
            # 代理连不上（用户关了代理软件，注册表尚未更新 / manual 模式失效）。
            # 游戏后端登录后可直连，故自动改直连重试一次——同浏览器的容错表现。
            if not self._proxy:
                raise  # 本来就是直连还失败，说明是真的网络问题
            self._proxy = ""
            self._client = self._build_client("")
            resp = self._client.request("PUT", config.GAME_ROUTER, content=body)
        resp.raise_for_status()
        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            # 游戏后端对业务状态用明文 'Msg_Msg'(重复) 返回。去重成单条。
            msg = text.strip()
            half = len(msg) // 2
            if len(msg) % 2 == 1 and msg[:half] == msg[half + 1:] and msg[half] == "_":
                msg = msg[:half]
            if trace:
                log.info("[game←] %s 明文消息: %s", route, msg)
            raise GameMessage(msg)
        if trace:
            log.info("[game←] %s %s", route, _distill(body))
        # 增量维护本地货币/背包：几乎每个操作响应的 Drop.Items[] 和 CostItems[] 都带
        # NowItem（该物品操作后的绝对新余额）。把它写回缓存 ItemContainer，状态栏就能
        # 零额外请求地反映工具自己领取/购买/花费的结果（游戏没有单独查背包的接口）。
        if isinstance(body, dict):
            self._apply_now_items(body)
            self._apply_commodity_record(body)
            self._apply_store_records(body)
        return body

    def _apply_store_records(self, body: dict):
        """把 ResetRandomStore 响应里的整批 `Records`(新货架)写回缓存。

        必须做，不是优化：秘密商店免费刷新的档期靠货架的 `LastResetTime` + 1h 推算
        （游戏不下发"下次免费刷新时刻"，见 tasks.next_claim_times）。刷新后若不回写，
        缓存里的 LastResetTime 永远停在登录那一刻，倒计时不会走，调度也就永远算错。
        只认带 Store 字段的整条记录，按 _id 覆盖同档、新档追加。
        """
        records = body.get("Records")
        if not isinstance(records, list) or not records or not self.account_state:
            return
        cur_list = (self.account_state.get("StoreRecordContainer") or {}).get("Records")
        if not isinstance(cur_list, list):
            return

        def oid(r):
            v = r.get("_id")
            return v.get("$oid") if isinstance(v, dict) else v

        index = {oid(r): i for i, r in enumerate(cur_list) if oid(r)}
        for new in records:
            if not isinstance(new, dict) or not new.get("Store"):
                continue
            i = index.get(oid(new))
            if i is not None:
                cur_list[i] = new
            else:
                index[oid(new)] = len(cur_list)
                cur_list.append(new)

    def _apply_now_items(self, body: dict):
        """把响应里 Drop.Items / CostItems 的 NowItem 绝对余额写回缓存 ItemContainer。"""
        if not self.account_state:
            return
        items = (self.account_state.get("ItemContainer") or {}).get("Items")
        if not isinstance(items, list):
            return
        by_sid = {it.get("StaticID"): it for it in items}

        def apply(entry_list):
            for entry in entry_list or []:
                now = entry.get("NowItem")
                if not isinstance(now, dict):
                    continue  # 如派遣结算经验值 StaticID=91 无 NowItem，跳过
                sid = now.get("StaticID")
                cnt = now.get("Count")
                if sid is None or cnt is None:
                    continue
                cur = by_sid.get(sid)
                if cur is not None:
                    cur["Count"] = cnt   # 已有条目：更新绝对余额
                else:
                    items.append(dict(now))  # 新物品：整条塞进背包
                    by_sid[sid] = items[-1]

        apply((body.get("Drop") or {}).get("Items"))
        apply(body.get("CostItems"))

    def _apply_commodity_record(self, body: dict):
        """把 BuyCommodity 响应里的 `CommodityRecord`(该档购买后的新状态)写回缓存货架。

        必须做，不是优化：账号状态只在 bootstrap 时刷新，后台调度线程 5 分钟一轮却
        复用同一份缓存。若不回写，`BuyCount`/`FreeBuyCount` 会一直是买之前的值——
        "每日免费招募"就会因为缓存里 FreeBuyCount 仍是 0 而每轮重复购买，第二次起
        不再免费，直接开始扣招募契约。回写后同一会话内只会买一次。
        """
        rec = body.get("CommodityRecord")
        if not isinstance(rec, dict) or not self.account_state:
            return
        rid = (rec.get("_id") or {}).get("$oid") if isinstance(rec.get("_id"), dict) else rec.get("_id")
        if not rid:
            return
        records = (self.account_state.get("StoreRecordContainer") or {}).get("Records")
        if not isinstance(records, list):
            return
        for i, cur in enumerate(records):
            cid = cur.get("_id")
            cid = cid.get("$oid") if isinstance(cid, dict) else cid
            if cid == rid:
                records[i] = rec
                return
        records.append(rec)   # 首次购买的新档：快照里还没有，整条塞进去

    def _auth_data(self, extra: dict | None = None) -> dict:
        if not self.aid or not self.session_id:
            raise GameError("未登录：缺少 AID/SessionID")
        d = {"AID": self.aid, "SessionID": self.session_id}
        if extra:
            d.update(extra)
        return d

    # ---- 登录握手 ----
    def bootstrap(self) -> dict:
        """完整登录握手 -> 拿到 AID + SessionID + 账号状态。返回账号状态。"""
        common = {"Token": self.game_token, **config.LOGIN_COMMON}

        # 1. 公告/服务器配置(可选，确认连通)
        self.call("GameServerDBSettingHandler.QueryBulletinInfoResult", {})

        # 2. 换取游戏内 LoginID
        uid_res = self.call("ErolabsHandler.QueryUserID", dict(common))
        self.login_id = uid_res.get("LoginID")
        if not self.login_id:
            raise GameError(f"QueryUserID 未返回 LoginID: {uid_res}")

        # 3. 正式登录 -> 完整账号状态 + SessionID
        login_data = {**common, "LoginID": self.login_id}
        state = self.call("AccountHandler.Login", login_data)
        self.session_id = state.get("SessionID")
        self.aid = state.get("Info", {}).get("_id", {}).get("$oid")
        if not self.session_id or not self.aid:
            raise GameError("Login 未返回 SessionID/AID")
        self.account_state = state
        return state

    def close(self):
        self._client.close()
