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
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from typing import Any, Callable

import httpx

from . import config
from .logutil import format_payload, log

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


_LOGIN_ROUTES = frozenset({
    "ErolabsHandler.QueryUserID",
    "AccountHandler.Login",
})


def _log_game_response(route: str, status: int, body: Any, *, login: bool) -> None:
    """Log ordinary responses in full; login responses only get metadata."""
    if login:
        log.info("[game<-] %s HTTP %s response body suppressed (login)", route, status)
    else:
        log.info("[game<-] %s HTTP %s %s", route, status, format_payload(body))


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


class DuplicateLoginError(GameMessage):
    """The game rejected this session because the account logged in elsewhere."""


def is_duplicate_login_message(value: Any) -> bool:
    """Return whether a game response carries the duplicate-login marker."""
    normalized = " ".join(str(value or "").split()).casefold()
    return "duplicate login" in normalized


_HEADERS = {
    "Content-Type": "application/octet-stream",
    "Origin": config.GAME_ORIGIN,
    "Referer": config.GAME_REFERER,
    "Accept": "*/*",
    "Accept-Encoding": "gzip",
}


class GameClient:
    def __init__(self, game_token: str, *, login_id: str | None = None,
                 login_common: dict | None = None,
                 auth_refresher: Callable[[bool], dict] | None = None):
        # game_token 可以是旧 WebGL jwt，也可以是 V2 accessToken；由登录 profile 决定。
        self.game_token = game_token
        self._fixed_login_id = login_id
        self._login_common = dict(login_common or config.LOGIN_COMMON_WEBGL)
        self._auth_refresher = auth_refresher
        self.login_id: str | None = None
        self.aid: str | None = None
        self.session_id: str | None = None
        self.account_state: dict[str, Any] | None = None
        self._ensure_inventory_state()
        self.quest_update_callback: Callable[[], None] | None = None
        self._proxy: str = config.get_proxy()  # 当前 client 使用的代理
        self._client = self._build_client(self._proxy)
        # Serialize transport calls per client. Request pacing is applied by
        # repeated-task loops, not by the transport layer.
        self._request_serial_lock = threading.Lock()
        self._call_serial_lock = threading.RLock()
        # 开发者控制台保留最近 30 条，响应也保存在内存中供复制/复刻。
        self._request_history: deque[dict[str, Any]] = deque(maxlen=30)
        self._request_history_lock = threading.Lock()
        self._request_history_seq = 0
        # Route 下拉框使用的最近运行记录：按 route 去重，保持最新一次在前。
        # 与完整请求历史分开，避免 route 去重影响请求记录。
        self._recent_routes: dict[str, None] = {}
        self._connection_state = "connected"
        self._connection_reason: str | None = None
        self._connection_changed_at: int | None = None
        self._login_in_progress = False

    # ---- connection state ----
    def _ensure_connection_state(self) -> None:
        """Keep lightweight test clients and old in-memory clients compatible."""
        if not hasattr(self, "_connection_state"):
            self._connection_state = "connected"
        if not hasattr(self, "_connection_reason"):
            self._connection_reason = None
        if not hasattr(self, "_connection_changed_at"):
            self._connection_changed_at = None
        if not hasattr(self, "_login_in_progress"):
            self._login_in_progress = False

    def connection_status(self) -> dict[str, Any]:
        self._ensure_connection_state()
        return {
            "connected": self._connection_state == "connected",
            "reason": self._connection_reason,
            "changedAt": self._connection_changed_at,
        }

    def is_connected(self) -> bool:
        return bool(self.connection_status()["connected"])

    def _mark_disconnected(self, reason: str = "connection_error") -> None:
        self._ensure_connection_state()
        changed = self._connection_state != "disconnected" or self._connection_reason != reason
        if changed:
            self._connection_changed_at = int(time.time() * 1000)
        self._connection_state = "disconnected"
        self._connection_reason = reason
        callback = getattr(self, "connection_changed_callback", None)
        if changed and callable(callback):
            callback()

    def _mark_connected(self) -> None:
        self._ensure_connection_state()
        if self._connection_state != "connected" or self._connection_reason is not None:
            self._connection_changed_at = int(time.time() * 1000)
        self._connection_state = "connected"
        self._connection_reason = None

    def _sync_login_auth(self, force: bool = False) -> None:
        """在登录握手前同步安全存储中的 V2 Token，不向调用方暴露凭证。"""
        if not getattr(self, "_auth_refresher", None):
            return
        auth = self._auth_refresher(force)
        access_token = str(auth.get("access_token") or "")
        user_id = str(auth.get("user_id") or "")
        device_id = str(auth.get("device_id") or "")
        if not access_token or not user_id or not device_id:
            raise GameError("续期后的登录凭证不完整")
        self.game_token = access_token
        self._fixed_login_id = user_id
        self._login_common = config.android_login_common(device_id)

    @staticmethod
    def _looks_like_auth_failure(error: GameMessage) -> bool:
        message = str(error.msg or "").lower()
        return any(marker in message for marker in (
            "getuserid", "erolabshandler.login", "invalid token",
            "token expired", "no token", "unauthorized",
        ))

    @staticmethod
    def _build_client(proxy: str) -> httpx.Client:
        return httpx.Client(
            proxy=proxy or None,
            timeout=config.HTTP_TIMEOUT,
            headers=_HEADERS,
        )

    def _sync_proxy(self):
        """读取共享的系统代理缓存，地址变化时才重建连接池。"""
        cur = config.get_proxy()
        if cur != self._proxy:
            try:
                self._client.close()
            except Exception:
                pass
            self._proxy = cur
            self._client = self._build_client(cur)

    # ---- 底层调用 ----
    def _ensure_request_history(self) -> None:
        """Keep lightweight test/dummy clients compatible with history helpers."""
        if not hasattr(self, "_request_history_lock"):
            self._request_history_lock = threading.Lock()
        if not hasattr(self, "_request_history"):
            self._request_history = deque(maxlen=30)
        elif getattr(self._request_history, "maxlen", 30) != 30:
            # 旧版本实例可能仍持有 maxlen=10 的队列；升级时保留已有记录。
            self._request_history = deque(self._request_history, maxlen=30)
        if not hasattr(self, "_request_history_seq"):
            self._request_history_seq = 0
        if not hasattr(self, "_recent_routes"):
            self._recent_routes = {}

    def record_route(self, route: str) -> None:
        """Record one route name for the developer route picker."""
        route = str(route or "").strip()
        if not route:
            return
        self._ensure_request_history()
        with self._request_history_lock:
            # dict 保持插入顺序；移除再写入即可把重复 route 提到最新位置。
            self._recent_routes.pop(route, None)
            self._recent_routes[route] = None
            while len(self._recent_routes) > 30:
                self._recent_routes.pop(next(iter(self._recent_routes)))

    @staticmethod
    def _copy_json(value: Any) -> Any:
        """Copy JSON-compatible protocol data without sharing mutable references."""
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            return str(value)

    def record_request(self, route: str, data: dict | None = None, *,
                       request_payload: dict | None = None,
                       source: str = "task",
                       allow_login: bool = False) -> int | None:
        """Record one request and return its history id.

        ``data`` remains the replay-friendly form (AID/SessionID removed), while
        ``request`` keeps the complete envelope that was sent so developers can
        copy the exact request. Login traffic from normal application calls is
        still excluded; an explicitly recorded console call may opt in.
        """
        route = str(route or "").strip()
        if not route or (route in _LOGIN_ROUTES and not allow_login and source != "console"):
            return None
        self._ensure_request_history()
        self.record_route(route)
        data_source = data if isinstance(data, dict) else {}
        use_auth = "AID" in data_source or "SessionID" in data_source
        replay_data = {
            key: value for key, value in data_source.items()
            if key not in {"AID", "SessionID"}
        }
        replay_data = self._copy_json(replay_data)
        actual = request_payload if isinstance(request_payload, dict) else {
            "data": data_source,
            "route": route,
        }
        with self._request_history_lock:
            self._request_history_seq += 1
            request_id = self._request_history_seq
            self._request_history.append({
                "id": request_id,
                "route": route,
                "data": replay_data,
                "useAuth": use_auth,
                "timestamp": int(time.time() * 1000),
                "source": "console" if source == "console" else "task",
                "request": self._copy_json(actual),
                "response": None,
                "responseText": "",
                "status": None,
                "isJson": False,
                "gzip": False,
            })
            return request_id

    def update_request_history(self, request_id: int | None, *,
                               response: Any = None,
                               response_text: str = "",
                               status: int | None = None,
                               is_json: bool = False,
                               gzip: bool = False,
                               error: str | None = None) -> None:
        """Attach the response (or a transport error) to a saved request."""
        if request_id is None:
            return
        self._ensure_request_history()
        with self._request_history_lock:
            for item in self._request_history:
                if item.get("id") != request_id:
                    continue
                item["response"] = self._copy_json(response) if is_json else None
                item["responseText"] = str(response_text or "")
                item["status"] = status
                item["isJson"] = bool(is_json)
                item["gzip"] = bool(gzip)
                if error:
                    item["error"] = str(error)
                else:
                    item.pop("error", None)
                return

    def recent_requests(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return newest-first snapshots for the gated developer console."""
        self._ensure_request_history()
        count = max(0, min(30, int(limit or 30)))
        with self._request_history_lock:
            rows = list(reversed(self._request_history))[:count]
            return self._copy_json(rows)

    def recent_routes(self, limit: int = 30) -> list[str]:
        """Return newest-first unique routes for the developer route picker."""
        self._ensure_request_history()
        count = max(0, min(30, int(limit or 30)))
        with self._request_history_lock:
            return list(reversed(self._recent_routes))[:count]

    def _ensure_request_transport(self) -> None:
        """Keep lightweight test clients and older instances compatible."""
        if not hasattr(self, "_request_serial_lock"):
            self._request_serial_lock = threading.Lock()
        if not hasattr(self, "_call_serial_lock"):
            self._call_serial_lock = threading.RLock()

    def _request_http(self, method: str, url: str, **kwargs):
        """Send one game request while serializing transport calls.

        Delays intentionally do not live here: login, developer-console calls,
        and unrelated single requests must remain immediate. Repeated task
        loops add the configured delay explicitly between iterations.
        """
        self._ensure_request_transport()
        with self._request_serial_lock:
            return self._client.request(method, url, **kwargs)

    def call(self, route: str, data: dict | None = None) -> dict:
        """调用一个 route，返回解压后的 JSON dict。"""
        self._ensure_request_transport()
        # Keep parsing and inventory settlement in the same order as requests.
        # The short inventory lock is separate, so snapshots never wait on HTTP.
        with self._call_serial_lock:
            return self._call_serial(route, data)

    def _call_serial(self, route: str, data: dict | None = None) -> dict:
        self._ensure_connection_state()
        self._sync_proxy()
        payload = {"data": data or {}, "route": route}
        body = json.dumps(payload).encode("utf-8")
        login = route in _LOGIN_ROUTES
        bootstrap_route = "GameServerDBSettingHandler.QueryBulletinInfoResult"
        if not login and route != bootstrap_route and not self._login_in_progress and not self.is_connected():
            raise DuplicateLoginError("Duplicate Login")
        # route 下拉框仅记录 route 名称，开发者模式下登录 route 也可用于排查流程；
        # 完整请求历史仍由下面的 record_request 保持登录请求排除。
        history_id = None
        if not login:
            history_id = self.record_request(
                route,
                payload["data"],
                request_payload=payload,
                source="task",
            )
        else:
            self.record_route(route)
        if login:
            log.info("[game->] %s request body suppressed (login)", route)
        else:
            log.info("[game->] %s %s", route, format_payload(payload))
        try:
            resp = self._request_http("PUT", config.GAME_ROUTER, content=body)
        except (httpx.ProxyError, httpx.ConnectError) as e:
            # 代理连不上（用户关了代理软件，系统设置/短期缓存尚未更新）。
            # 游戏后端登录后可直连，故自动改直连重试一次——同浏览器的容错表现。
            if not self._proxy:
                self.update_request_history(history_id, error=str(e))
                raise  # 本来就是直连还失败，说明是真的网络问题
            self._proxy = ""
            self._client = self._build_client("")
            try:
                resp = self._request_http("PUT", config.GAME_ROUTER, content=body)
            except Exception as retry_error:
                self.update_request_history(history_id, error=str(retry_error))
                raise
        except Exception as error:
            self.update_request_history(history_id, error=str(error))
            raise
        raw = resp.content
        was_gzip = raw[:2] == b"\x1f\x8b"
        if was_gzip:
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", "replace")
        # The game normally returns this marker as plain text, but a gateway
        # may wrap it in a JSON string/object. Inspect the raw body first so
        # every response shape disconnects the local session consistently.
        duplicate_login = is_duplicate_login_message(text)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            # 游戏后端对业务状态用明文 'Msg_Msg'(重复) 返回。去重成单条。
            msg = text.strip()
            half = len(msg) // 2
            if len(msg) % 2 == 1 and msg[:half] == msg[half + 1:] and msg[half] == "_":
                msg = msg[:half]
            self.update_request_history(
                history_id,
                response_text=text,
                status=resp.status_code,
                is_json=False,
                gzip=was_gzip,
            )
            if login:
                log.info("[game<-] %s HTTP %s response body suppressed (login)",
                         route, resp.status_code)
            else:
                log.info("[game<-] %s HTTP %s %s", route, resp.status_code,
                         format_payload(msg))
            if duplicate_login or is_duplicate_login_message(msg):
                self._mark_disconnected("duplicate_login")
                log.warning("[game] %s returned Duplicate Login; connection marked disconnected", route)
                raise DuplicateLoginError("Duplicate Login")
            resp.raise_for_status()
            raise GameMessage(msg)
        self.update_request_history(
            history_id,
            response=body,
            response_text=text,
            status=resp.status_code,
            is_json=True,
            gzip=was_gzip,
        )
        _log_game_response(route, resp.status_code, body, login=login)
        if duplicate_login or is_duplicate_login_message(body):
            self._mark_disconnected("duplicate_login")
            log.warning("[game] %s returned Duplicate Login; connection marked disconnected", route)
            raise DuplicateLoginError("Duplicate Login")
        resp.raise_for_status()
        # 增量维护本地货币/背包：几乎每个操作响应的 Drop.Items[] 和 CostItems[] 都带
        # NowItem（该物品操作后的绝对新余额）。把它写回缓存 ItemContainer，状态栏就能
        # 零额外请求地反映工具自己领取/购买/花费的结果（游戏没有单独查背包的接口）。
        if isinstance(body, dict):
            self._apply_now_items(body)
            self._apply_commodity_record(body)
            self._apply_store_records(body)
            self._apply_quest_updates(body)
            self._apply_account_status_updates(body)
        return body

    def apply_scene_reward(self, body: dict):
        """Merge confirmed WebSocket settlement using the HTTP inventory rules.

        Returned records are partial updates, so preserve unrelated login fields
        and unreturned roles/scenes. Never add Drop counts to absolute NowItem.
        """
        state = getattr(self, "account_state", None)
        if not isinstance(state, dict) or not isinstance(body, dict):
            return
        self._apply_now_items(body)
        self._apply_quest_updates(body)
        self._apply_account_status_updates(body)
        for source, target in (("AccountInfo", "Info"), ("AccountSaveData", "AccountSaveData")):
            if isinstance(body.get(source), dict):
                state.setdefault(target, {}).update(deepcopy(body[source]))
        for key in ("GotStars", "HasMail"):
            if key in body:
                state[key] = body[key]

        def identifier(row):
            value = row.get("_id")
            return value.get("$oid") if isinstance(value, dict) else value

        def merge(container, field, updates, key):
            if not isinstance(updates, list):
                return
            rows = state.setdefault(container, {}).setdefault(field, [])
            by_id = {key(row): row for row in rows if isinstance(row, dict) and key(row)}
            for update in updates:
                if not isinstance(update, dict) or not key(update):
                    continue
                current = by_id.get(key(update))
                if current is None:
                    current = deepcopy(update)
                    rows.append(current)
                    by_id[key(update)] = current
                else:
                    current.update(deepcopy(update))

        merge("RoleDataContainer", "Roles", body.get("Roles"), identifier)
        scene = body.get("SceneData")
        merge("SceneDataContainer", "Scenes", [scene] if isinstance(scene, dict) else [], lambda row: row.get("StaticID"))
        merge("BattlePassDataContainer", "BattlePassDataList", body.get("BattlePassList"), lambda row: row.get("ActivityID"))
        # Line-role identity is nested in Info, unlike combat role instances.
        if isinstance(body.get("LineRoleDatas"), list):
            rows = state.setdefault("LineRoleDataContainer", {}).setdefault("RoleList", [])
            by_id = {row.get("Info", {}).get("StaticID"): row for row in rows if isinstance(row, dict)}
            for update in body["LineRoleDatas"]:
                sid = (update.get("Info") or {}).get("StaticID") if isinstance(update, dict) else None
                if not sid:
                    continue
                current = by_id.get(sid)
                if current is None:
                    current = deepcopy(update)
                    rows.append(current)
                    by_id[sid] = current
                else:
                    current.setdefault("Info", {}).update(deepcopy(update["Info"]))
                    current.update({key: deepcopy(value) for key, value in update.items() if key != "Info"})

    def _apply_account_status_updates(self, body: dict):
        """Merge partial server-status updates without removing login-only states."""
        state = getattr(self, "account_state", None)
        if not isinstance(state, dict) or not isinstance(body, dict):
            return
        incoming = body.get("AccountStatusList")
        if not isinstance(incoming, list):
            return

        def status_id(status):
            if not isinstance(status, dict):
                return None
            value = status.get("_id")
            if isinstance(value, dict):
                value = value.get("$oid")
            return value if isinstance(value, str) and value else None

        updates = [status for status in incoming if status_id(status)]
        if not updates:
            return
        container = state.setdefault("StatusContainer", {})
        if not isinstance(container, dict):
            return
        statuses = container.setdefault("List", [])
        if not isinstance(statuses, list):
            return
        # VIP/activity entries may have no _id; keep every such entry intact.
        by_id = {status_id(status): status for status in statuses if status_id(status)}
        for update in updates:
            key = status_id(update)
            copied = deepcopy(update)
            current = by_id.get(key)
            if current is None:
                statuses.append(copied)
                by_id[key] = copied
            else:
                current.update(copied)

    def _apply_quest_updates(self, body: dict):
        """Merge quest progress included in full or incremental responses."""
        state = getattr(self, "account_state", None)
        if not isinstance(state, dict) or not isinstance(body, dict):
            return
        updates = []
        container = body.get("QuestContainer")
        if isinstance(container, dict) and isinstance(container.get("Quests"), list):
            updates.extend(container["Quests"])
        direct = body.get("Quests")
        if isinstance(direct, list):
            updates.extend(direct)
        for result in body.get("Results") or []:
            if isinstance(result, dict) and isinstance(result.get("Quests"), list):
                updates.extend(result["Quests"])
        updates = [quest for quest in updates if isinstance(quest, dict) and quest.get("StaticID")]
        if not updates:
            return

        target_container = state.get("QuestContainer")
        if isinstance(target_container, dict) and isinstance(target_container.get("Quests"), list):
            quests = target_container["Quests"]
        elif isinstance(state.get("Quests"), list):
            quests = state["Quests"]
        else:
            target_container = state.setdefault("QuestContainer", {})
            if not isinstance(target_container, dict):
                return
            quests = target_container.setdefault("Quests", [])
        by_id = {
            quest.get("StaticID"): quest
            for quest in quests
            if isinstance(quest, dict) and quest.get("StaticID")
        }
        changed = False
        for update in updates:
            current = by_id.get(update["StaticID"])
            if current is None:
                quests.append(dict(update))
                by_id[update["StaticID"]] = quests[-1]
                changed = True
            elif any(key not in current or current[key] != value for key, value in update.items()):
                current.update(update)
                changed = True
        callback = getattr(self, 'quest_update_callback', None)
        if changed and callable(callback):
            callback()

    def _apply_store_records(self, body: dict):
        """把 ResetRandomStore 响应里的整批 `Records`(新货架)写回缓存。

        必须做，不是优化：秘密商店免费刷新的档期靠货架的 `LastResetTime` + 1h1s 推算
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

    # ---- authoritative stackable inventory ----
    def _ensure_inventory_state(self) -> None:
        """Initialize versioning lazily for old clients and lightweight test fakes."""
        if not hasattr(self, "_inventory_lock"):
            self._inventory_lock = threading.Lock()
            self._inventory_epoch = uuid.uuid4().hex
            self._inventory_revision = 0
            self._inventory_baseline_revision = 0
            self._inventory_items: dict[str, dict] = {}
            self._inventory_changed: dict[str, int] = {}
            self._inventory_source_state = None
        with self._inventory_lock:
            state = getattr(self, "account_state", None)
            if isinstance(state, dict) and state is not self._inventory_source_state:
                self._reset_inventory_locked(state)

    @staticmethod
    def _inventory_item(value: Any) -> dict | None:
        if not isinstance(value, dict) or value.get("StaticID") is None or value.get("Count") is None:
            return None
        item = deepcopy(value)
        item["StaticID"] = str(item["StaticID"])
        return item

    def _reset_inventory_locked(self, state: dict) -> None:
        """Install one complete login baseline while holding the inventory lock."""
        container = state.get("ItemContainer")
        if not isinstance(container, dict):
            container = state["ItemContainer"] = {}
        raw_items = container.get("Items")
        normalized = {}
        for raw in raw_items if isinstance(raw_items, list) else []:
            item = self._inventory_item(raw)
            if item is not None:
                normalized[item["StaticID"]] = item
        self._inventory_items = normalized
        container["Items"] = list(normalized.values())
        self._inventory_source_state = state
        self._inventory_revision += 1
        self._inventory_baseline_revision = self._inventory_revision
        self._inventory_changed.clear()

    def _replace_account_state(self, state: dict) -> None:
        """Atomically replace account inventory after a successful full login."""
        self._ensure_inventory_state()
        with self._inventory_lock:
            self.account_state = state
            self._activity_pending = None
            self._reset_inventory_locked(state)

    def inventory_meta(self) -> dict:
        self._ensure_inventory_state()
        with self._inventory_lock:
            return {"inventoryEpoch": self._inventory_epoch,
                    "inventoryRevision": self._inventory_revision}

    def inventory_snapshot(self, since: int | None = None, epoch: str | None = None) -> dict:
        """Return a detached full baseline or cumulative absolute item changes.

        Keeping each item's latest change revision covers arbitrarily many missed
        polls without an unbounded event log. A relogin forces a full replacement,
        which also removes items absent from the new server snapshot.
        """
        self._ensure_inventory_state()
        with self._inventory_lock:
            full = not (
                epoch == self._inventory_epoch
                and isinstance(since, int) and not isinstance(since, bool)
                and self._inventory_baseline_revision <= since <= self._inventory_revision
            )
            items = (list(self._inventory_items.values()) if full else [
                self._inventory_items[sid]
                for sid, revision in self._inventory_changed.items()
                if revision > since and sid in self._inventory_items
            ])
            return {
                "epoch": self._inventory_epoch,
                "revision": self._inventory_revision,
                "full": full,
                "items": deepcopy(items),
                # Count zero is an authoritative item value, not a deletion.
                "removed": [],
            }

    def _apply_now_items(self, body: dict):
        """Merge confirmed balances only, once per response, including batch rewards."""
        if not isinstance(body, dict) or not isinstance(getattr(self, "account_state", None), dict):
            return
        self._ensure_inventory_state()
        updates = []
        results = body.get("Results")
        settlements = [body] + ([entry for entry in results if isinstance(entry, dict)]
                                if isinstance(results, list) else [])
        for settlement in settlements:
            drop = settlement.get("Drop")
            record = settlement.get("CommodityRecord")
            guild_cost = isinstance(record, dict) and record.get("Store") == "CaptainShop"
            # Costs settle before rewards. FinishScene can spend and refund the
            # same item: captured stamina CostItems=226, then Drop=234 (+8).
            # Batch Results are processed in server order; later balances win.
            for entries in (None if guild_cost else settlement.get("CostItems"),
                            drop.get("Items") if isinstance(drop, dict) else None):
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    item = self._inventory_item(entry.get("NowItem")) if isinstance(entry, dict) else None
                    if item is not None:
                        updates.append(item)
        if not updates:
            return
        with self._inventory_lock:
            # Do not publish intermediate balances from one batch response.
            final = {}
            for item in updates:
                sid = item["StaticID"]
                current = final.get(sid, self._inventory_items.get(sid, {}))
                final[sid] = {**current, **item}
            changed = {sid: item for sid, item in final.items()
                       if self._inventory_items.get(sid) != item}
            if not changed:
                return
            increases = set()
            for sid, item in changed.items():
                previous = self._inventory_items.get(sid, {}).get('Count', 0)
                count = item.get('Count')
                if (isinstance(count, (int, float)) and not isinstance(count, bool)
                        and isinstance(previous, (int, float)) and count > previous):
                    increases.add(sid)
            self._inventory_revision += 1
            for sid, item in changed.items():
                self._inventory_items[sid] = item
                self._inventory_changed[sid] = self._inventory_revision
            self.account_state["ItemContainer"]["Items"] = list(self._inventory_items.values())
        callback = getattr(self, 'inventory_increase_callback', None)
        if callback and increases:
            callback(increases)

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
        if rec.get("Store") == "CaptainShop":
            return  # Shared purchase records are cached in GuildData by GuildService.
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
        self._ensure_request_transport()
        with self._call_serial_lock:
            return self._bootstrap_serial()

    def _bootstrap_serial(self) -> dict:
        self._sync_login_auth(False)
        common = {"Token": self.game_token, **self._login_common}
        uid_res = {}

        # 1. 公告/服务器配置(可选，确认连通)
        self.call("GameServerDBSettingHandler.QueryBulletinInfoResult", {})

        # V2 门户已经返回 userId，可直接作为 LoginID；旧 WebGL jwt 才需要 QueryUserID。
        if self._fixed_login_id:
            self.login_id = self._fixed_login_id
        else:
            uid_res = self.call("ErolabsHandler.QueryUserID", dict(common))
            self.login_id = uid_res.get("LoginID")
        if not self.login_id:
            raise GameError(f"QueryUserID 未返回 LoginID: {uid_res}")

        # 3. 正式登录 -> 完整账号状态 + SessionID
        login_data = {**common, "LoginID": self.login_id}
        try:
            state = self.call("AccountHandler.Login", login_data)
        except GameMessage as error:
            if not self._auth_refresher or not self._looks_like_auth_failure(error):
                raise
            # Token 可能被门户提前撤销，即使 JWT exp 尚未到期也强制续期并只重试一次。
            self._sync_login_auth(True)
            self.login_id = self._fixed_login_id
            common = {"Token": self.game_token, **self._login_common}
            state = self.call("AccountHandler.Login", {**common, "LoginID": self.login_id})
        self.session_id = state.get("SessionID")
        self.aid = state.get("Info", {}).get("_id", {}).get("$oid")
        if not self.session_id or not self.aid:
            raise GameError("Login 未返回 SessionID/AID")
        self._replace_account_state(state)
        self._mark_connected()
        return state

    def close(self):
        self._client.close()
