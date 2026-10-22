"""全局配置常量。

网络请求统一跟随系统代理。Windows 读取系统代理设置，Android 由系统 VPN/TUN 接管；
系统代理地址在进程内短暂缓存，避免每个请求重复读取。
旧版代理设置字段仅保留兼容，不再提供手动切换。

设置持久化在 settings.json（数据目录）。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .version import APP_VERSION  # noqa: F401  （版本唯一真源，见 version.py）

# ---------- 平台标志 ----------
# 由入口（desktop/run.py 或 Android 桥）设置为 "windows" / "android"。
PLATFORM = os.environ.get("ARK_PLATFORM", "windows")
# start.bat is the local test server. In this mode a page refresh may restore
# the single in-memory session without forcing another login click.
TEST_SERVER = os.environ.get("ARK_TEST_SERVER", "").lower() in ("1", "true", "yes")

# ---------- 可持久化设置 ----------
_DATA_DIR = Path(os.environ.get("ARK_DATA_DIR", Path(__file__).resolve().parent.parent))
_SETTINGS_FILE = _DATA_DIR / "settings.json"
_SETTINGS_LOCK = threading.RLock()
_FRONTEND_SETTINGS_KEY = "frontend_settings"
_FRONTEND_SETTINGS_MAX_BYTES = 8 * 1024 * 1024

_PROXY_CACHE_TTL_SECONDS = 5.0
_proxy_cache_lock = threading.RLock()
_proxy_cache_value = ""
_proxy_cache_expires_at = 0.0


def _load_settings() -> dict:
    with _SETTINGS_LOCK:
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _save_settings(data: dict) -> bool:
    with _SETTINGS_LOCK:
        temporary = _SETTINGS_FILE.with_suffix(_SETTINGS_FILE.suffix + ".tmp")
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, _SETTINGS_FILE)
            return True
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            return False


def load_frontend_settings() -> dict[str, str]:
    """Return persistent frontend preferences stored in settings.json."""
    value = _load_settings().get(_FRONTEND_SETTINGS_KEY) or {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def save_frontend_settings(value: dict) -> dict[str, str]:
    """Replace persistent frontend preferences stored in settings.json."""
    if not isinstance(value, dict):
        raise ValueError("前端设置必须是对象")
    normalized = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("前端设置键和值必须是字符串")
        normalized[key] = item
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("前端设置不是有效 JSON") from error
    if len(encoded) > _FRONTEND_SETTINGS_MAX_BYTES:
        raise ValueError("前端设置超过 8 MB 限制")
    with _SETTINGS_LOCK:
        settings = _load_settings()
        settings[_FRONTEND_SETTINGS_KEY] = normalized
        if not _save_settings(settings):
            raise RuntimeError("无法保存前端设置，请检查数据目录写入权限")
    return normalized


def _purge_legacy_passwords() -> dict:
    """升级到 Token 登录时立即移除旧版可逆 Base64 密码。"""
    with _SETTINGS_LOCK:
        settings = _load_settings()
        changed = False
        for record in (settings.get("accounts") or {}).values():
            if not isinstance(record, dict):
                continue
            for key in ("password_b64", "save_pwd", "password"):
                if key in record:
                    record.pop(key, None)
                    changed = True
        if changed:
            if not _save_settings(settings):
                raise RuntimeError("无法清除旧版保存密码，请检查数据目录写入权限")
        return settings


# 运行时内存态。迁移必须早于任何账号读取，避免旧密码继续留在磁盘。
_settings = _purge_legacy_passwords()

# Requests are serialized per game client and separated by a small randomized
# delay to reduce the chance of triggering server-side rate limits.
def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


REQUEST_DELAY_ENABLED: bool = _as_bool(_settings.get("request_delay_enabled"), True)
REQUEST_DELAY_MS: int = 300
REQUEST_DELAY_JITTER: int = 20


def _bounded_number(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, number))


REQUEST_DELAY_MS = _bounded_number(
    _settings.get("request_delay_ms", REQUEST_DELAY_MS), REQUEST_DELAY_MS, 0, 60000
)
REQUEST_DELAY_JITTER = _bounded_number(
    _settings.get("request_delay_jitter", REQUEST_DELAY_JITTER), REQUEST_DELAY_JITTER, 0, 100
)

# 自动重登默认关闭，避免在用户仍使用游戏客户端时意外顶号。
AUTO_RELOGIN_ENABLED: bool = _as_bool(_settings.get("auto_relogin_enabled"), False)
AUTO_RELOGIN_DELAY_SECONDS: int = _bounded_number(
    _settings.get("auto_relogin_delay_seconds", 300), 300, 1, 86400
)


def get_request_delay_config() -> dict:
    """Return pacing settings used between repeated task iterations."""
    return {
        "enabled": bool(REQUEST_DELAY_ENABLED),
        "ms": int(REQUEST_DELAY_MS),
        "jitter": int(REQUEST_DELAY_JITTER),
    }


def set_request_delay_config(enabled=None, delay_ms=None, jitter_percent=None) -> dict:
    """Update and persist repeated-task pacing settings.

    ``None`` leaves an individual value unchanged so older callers of
    ``/api/config`` remain compatible.
    """
    global REQUEST_DELAY_ENABLED, REQUEST_DELAY_MS, REQUEST_DELAY_JITTER
    if enabled is not None:
        REQUEST_DELAY_ENABLED = _as_bool(enabled, REQUEST_DELAY_ENABLED)
    if delay_ms is not None:
        REQUEST_DELAY_MS = _bounded_number(delay_ms, REQUEST_DELAY_MS, 0, 60000)
    if jitter_percent is not None:
        REQUEST_DELAY_JITTER = _bounded_number(
            jitter_percent, REQUEST_DELAY_JITTER, 0, 100
        )
    settings = _load_settings()
    settings["request_delay_enabled"] = bool(REQUEST_DELAY_ENABLED)
    settings["request_delay_ms"] = int(REQUEST_DELAY_MS)
    settings["request_delay_jitter"] = int(REQUEST_DELAY_JITTER)
    if not _save_settings(settings):
        raise RuntimeError("无法保存请求延时设置，请检查数据目录写入权限")
    return get_request_delay_config()


def get_auto_relogin_config() -> dict:
    """Return the opt-in disconnected-session recovery settings."""
    return {
        "enabled": bool(AUTO_RELOGIN_ENABLED),
        "delaySeconds": int(AUTO_RELOGIN_DELAY_SECONDS),
    }


def set_auto_relogin_config(enabled=None, delay_seconds=None) -> dict:
    """Update and persist automatic re-login settings."""
    global AUTO_RELOGIN_ENABLED, AUTO_RELOGIN_DELAY_SECONDS
    if enabled is None and delay_seconds is None:
        return get_auto_relogin_config()
    with _SETTINGS_LOCK:
        new_enabled = _as_bool(enabled, AUTO_RELOGIN_ENABLED)
        new_delay = _bounded_number(delay_seconds, AUTO_RELOGIN_DELAY_SECONDS, 1, 86400)
        settings = _load_settings()
        settings["auto_relogin_enabled"] = new_enabled
        settings["auto_relogin_delay_seconds"] = new_delay
        if not _save_settings(settings):
            raise RuntimeError("无法保存自动重登设置，请检查数据目录写入权限")
        AUTO_RELOGIN_ENABLED = new_enabled
        AUTO_RELOGIN_DELAY_SECONDS = new_delay
    return get_auto_relogin_config()
# 保留常量名供旧日志/调用方读取，但模式固定为 system。
PROXY_MODE: str = "system"


def _read_system_proxy() -> str:
    """读取系统代理（Windows 注册表 IE 设置）。读不到返回空串。"""
    if PLATFORM != "windows":
        return ""  # Android 等：直连，靠系统 VPN/TUN 截流
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
        server = (server or "").strip()
        if not server:
            return ""
        # server 形如 "127.0.0.1:7890" 或 "http=host:port;https=host:port;..."
        if "=" in server:
            parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
            hp = parts.get("https") or parts.get("http") or ""
        else:
            hp = server
        return f"http://{hp}" if hp else ""
    except Exception:
        return ""


def _system_proxy() -> str:
    """读取系统代理并缓存 5 秒；不会发起网络请求。"""
    global _proxy_cache_value, _proxy_cache_expires_at
    if PLATFORM != "windows":
        return ""
    now = time.monotonic()
    with _proxy_cache_lock:
        if now < _proxy_cache_expires_at:
            return _proxy_cache_value
        value = _read_system_proxy()
        _proxy_cache_value = value
        _proxy_cache_expires_at = now + _PROXY_CACHE_TTL_SECONDS
        return value


def clear_proxy_cache() -> None:
    """让测试或宿主代理切换立即重新读取系统设置。"""
    global _proxy_cache_expires_at
    with _proxy_cache_lock:
        _proxy_cache_expires_at = 0.0


def get_proxy() -> str:
    """返回系统代理地址（空串=直连）。不会发起网络请求。"""
    return _system_proxy()


def get_proxy_config() -> dict:
    """兼容旧 API 的代理摘要；代理模式始终是 system。"""
    return {"mode": "system", "manual": "", "effective": get_proxy()}


def set_proxy_config(mode: str | None = None, manual: str | None = None) -> dict:
    """兼容旧设置接口，但忽略手动代理参数并固定为 system。"""
    with _SETTINGS_LOCK:
        settings = _load_settings()
        if settings.get("proxy_mode") != "system" or "proxy" in settings:
            settings["proxy_mode"] = "system"
            settings.pop("proxy", None)
            _save_settings(settings)
    return get_proxy_config()


# 兼容旧 Android 桥；传入的地址不再改变代理。
def set_proxy(proxy: str) -> str:
    set_proxy_config()
    return get_proxy()


def data_dir() -> Path:
    """Return the writable application data root used by this process."""
    return _DATA_DIR


def default_download_directory() -> Path:
    """Return the platform's default user-visible download directory."""
    if PLATFORM == "windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            try:
                value, _ = winreg.QueryValueEx(
                    key, "{374DE290-123F-4565-9164-39C4925E467B}"
                )
            finally:
                winreg.CloseKey(key)
            resolved = Path(os.path.expandvars(str(value))).expanduser()
            if str(resolved):
                return resolved
        except Exception:
            pass
    return Path.home() / "Downloads"


def get_download_directory() -> Path:
    value = str(_load_settings().get("download_directory") or "").strip()
    return Path(value).expanduser() if value else default_download_directory()


def set_download_directory(value: str | None) -> Path:
    """Persist a custom Windows-style directory; blank restores the default."""
    with _SETTINGS_LOCK:
        settings = _load_settings()
        raw = str(value or "").strip()
        if raw:
            path = Path(os.path.expandvars(raw)).expanduser()
            if not path.is_absolute():
                raise ValueError("下载路径必须是绝对路径")
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ValueError(f"无法创建下载目录：{error}") from error
            if not path.is_dir():
                raise ValueError("下载路径不是文件夹")
            settings["download_directory"] = str(path)
        else:
            settings.pop("download_directory", None)
        if not _save_settings(settings):
            raise RuntimeError("无法保存下载路径，请检查数据目录写入权限")
    return get_download_directory()


def get_ignored_update_version() -> str:
    return str(_load_settings().get("ignored_update_version") or "")


def get_update_channel() -> str:
    return "preview" if _load_settings().get("update_channel") == "preview" else "stable"


def set_update_channel(channel: str) -> None:
    if channel not in {"stable", "preview"}:
        raise ValueError("无效的更新通道")
    with _SETTINGS_LOCK:
        settings = _load_settings()
        settings["update_channel"] = channel
        settings.pop("ignored_update_version", None)
        if not _save_settings(settings):
            raise RuntimeError("无法保存更新通道")


def set_ignored_update_version(version: str) -> None:
    with _SETTINGS_LOCK:
        settings = _load_settings()
        settings["ignored_update_version"] = str(version or "")
        if not _save_settings(settings):
            raise RuntimeError("无法保存更新偏好，请检查数据目录写入权限")


# ---------- 设备身份 / 多账号持久化 ----------
# settings.json 的账号条目只保存任务配置和平台原生加密后的 auth_blob。密码永不落盘，
# accessToken/userId/refreshToken 也不会以明文出现在 JSON 中。
_AUTH_UNSET = object()


def get_device_id() -> str:
    """返回每个安装实例固定的 40 位随机设备 ID。"""
    with _SETTINGS_LOCK:
        settings = _load_settings()
        value = str(settings.get("device_id") or "").lower()
        if len(value) == 40 and all(ch in "0123456789abcdef" for ch in value):
            return value
        value = secrets.token_hex(20)
        settings["device_id"] = value
        if not _save_settings(settings):
            raise RuntimeError("无法持久化设备 ID，请检查数据目录写入权限")
        return value


def _normalize_auth(auth: dict) -> dict:
    normalized = {
        "kind": str(auth.get("kind") or "erolabs-v2"),
        "user_id": str(auth.get("user_id") or auth.get("userId") or ""),
        "access_token": str(auth.get("access_token") or auth.get("accessToken") or ""),
        "refresh_token": str(auth.get("refresh_token") or auth.get("refreshToken") or ""),
        "device_id": str(auth.get("device_id") or auth.get("deviceId") or get_device_id()),
        "saved_at": int(auth.get("saved_at") or time.time()),
    }
    if not normalized["user_id"] or not normalized["access_token"]:
        raise ValueError("登录 Token 缺少 user_id/access_token")
    return normalized


def _protect_auth(email: str, auth: dict) -> str:
    from .token_vault import protect

    plaintext = json.dumps(_normalize_auth(auth), ensure_ascii=False, separators=(",", ":"))
    return protect(PLATFORM, plaintext, email)


def _unprotect_auth(email: str, ciphertext: str) -> dict:
    from .token_vault import unprotect

    value = json.loads(unprotect(PLATFORM, ciphertext, email))
    if not isinstance(value, dict):
        raise ValueError("登录 Token 内容格式无效")
    return _normalize_auth(value)


def load_accounts() -> dict:
    """返回账号存档；解密后的 ``auth`` 仅供后端使用，不会下发前端。"""
    s = _load_settings()
    accs = s.get("accounts") or {}
    out = {}
    for email, rec in accs.items():
        rec = dict(rec or {})
        rec.pop("password_b64", None)
        rec.pop("save_pwd", None)
        blob = str(rec.get("auth_blob") or "")
        rec["auth"] = None
        rec["auth_error"] = False
        if blob:
            try:
                rec["auth"] = _unprotect_auth(email, blob)
            except Exception:
                rec["auth_error"] = True
        out[email] = rec
    return out


def save_account(email: str, *, auth: dict | None | object = _AUTH_UNSET,
                 toggles: dict | None = None, params: dict | None = None) -> None:
    """新增/更新一个账号存档。只更新传入的字段，其余保留。"""
    with _SETTINGS_LOCK:
        s = _load_settings()
        accs = s.get("accounts") or {}
        rec = dict(accs.get(email) or {})
        if "order" not in rec:
            rec["order"] = len(accs)
        # 无论调用方是否传 auth，都清掉旧版密码字段。
        rec.pop("password_b64", None)
        rec.pop("save_pwd", None)
        rec.pop("password", None)
        if auth is not _AUTH_UNSET:
            if auth:
                rec["auth_blob"] = _protect_auth(email, auth)
            else:
                rec.pop("auth_blob", None)
        if toggles is not None:
            rec["toggles"] = toggles
        if params is not None:
            rec["params"] = params
        accs[email] = rec
        s["accounts"] = accs
        if not _save_settings(s):
            raise RuntimeError("无法保存账号设置，请检查数据目录写入权限")


def load_redeem_codes() -> dict:
    """Return the persisted shared redeem-code cache."""
    value = _load_settings().get("redeem_codes") or {}
    return dict(value) if isinstance(value, dict) else {}


def save_redeem_codes(value: dict) -> None:
    """Persist the shared redeem-code cache without touching accounts."""
    with _SETTINGS_LOCK:
        settings = _load_settings()
        settings["redeem_codes"] = dict(value or {})
        if not _save_settings(settings):
            raise RuntimeError("cannot save redeem code cache")

def delete_account(email: str) -> None:
    with _SETTINGS_LOCK:
        s = _load_settings()
        accs = s.get("accounts") or {}
        if email in accs:
            del accs[email]
            s["accounts"] = accs
            if not _save_settings(s):
                raise RuntimeError("无法删除账号设置，请检查数据目录写入权限")


# ---------- 助战收藏夹 ----------
# **按账号存**: settings.json 的 accounts[email].support_favs（同 toggles/params）。
# 换号即换收藏夹，账号间互不干涉；删号时随 delete_account 一起清掉。
# 结构: {cuid(str): 裁剪后的助战条目}，条目形如
#   {"IsFriend":0/1, "PlayerInfo":{...8字段...}, "RoleDataList":[队长条目]}
# 保持与 QueryBattleSupportDataList 原始条目同构 → tasks._build_support 可零改动复用。
# 只留队长（整条 8~28KB，仅队长约 4KB）。


def load_support_favs(email: str) -> dict:
    s = _load_settings()
    rec = (s.get("accounts") or {}).get(email) or {}
    return dict(rec.get("support_favs") or {})


def save_support_fav(email: str, cuid: str, entry: dict) -> None:
    """新增/覆盖一个收藏（覆盖=用助战列表里的新快照刷新旧数据）。"""
    _update_support_favs(email, lambda favs: favs.__setitem__(str(cuid), entry))


def delete_support_fav(email: str, cuid: str) -> bool:
    """从收藏夹移除。不存在返回 False（幂等，不写盘）。"""
    removed = False

    def op(favs):
        nonlocal removed
        removed = favs.pop(str(cuid), None) is not None

    _update_support_favs(email, op, skip_save=lambda: not removed)
    return removed


def _update_support_favs(email: str, op, skip_save=None) -> None:
    """读改写 accounts[email].support_favs。账号存档不存在时自动建一条。"""
    s = _load_settings()
    accs = s.get("accounts") or {}
    rec = dict(accs.get(email) or {})
    if "order" not in rec:
        rec["order"] = len(accs)
    favs = dict(rec.get("support_favs") or {})
    op(favs)
    if skip_save and skip_save():
        return
    rec["support_favs"] = favs
    accs[email] = rec
    s["accounts"] = accs
    s.pop("support_favs", None)      # 清掉早期版本的全局收藏夹键
    _save_settings(s)


# ---------- EROLABS V2 门户 ----------
# 当前先实现已由 Android 客户端和 Postie 双重确认的 V2 地址；允许环境变量覆盖，便于
# 上游换域名时先热修。后续 DomainResolver 会把远程配置和健康检查接到这里。
PORTAL_V2_SITE = os.environ.get(
    "ARK_PORTAL_V2", "https://sadpki-portal-v2.ebuajk.com"
).rstrip("/")
PORTAL_V2_CAPTCHA = f"{PORTAL_V2_SITE}/api/v2/captcha/verify"
PORTAL_V2_LOGIN = f"{PORTAL_V2_SITE}/api/v2/account/login"
PORTAL_V2_REFRESH = f"{PORTAL_V2_SITE}/api/v2/token/access"
EROLABS_GAME_ID = 32
PORTAL_CAPTCHA_TIMEOUT = 45.0
PORTAL_LOGIN_TIMEOUT = 60.0
PORTAL_REFRESH_TIMEOUT = 30.0

# 游戏后端(Ark Re:Code)
GAME_ROUTER = "https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx"
GAME_ORIGIN = "https://game-arkre-labs.ecchi.xxx"
GAME_REFERER = "https://game-arkre-labs.ecchi.xxx/WebGL/index.html"
GAME_VERSION = "4.0.0.111888"

# 旧 WebGL 握手仍保留给显式的 legacy token 入口；新默认登录使用 Android V2 profile。
LOGIN_COMMON_WEBGL = {
    "GuestID": "",
    "Platform": "WebGLPlayer",
    "Version": GAME_VERSION,
    "DeviceID": "n/a",
    "LoginType": "Erolabs",
    "IsNewSDK": 0,
}
LOGIN_COMMON = LOGIN_COMMON_WEBGL  # 兼容现有内部调用和测试


def android_login_common(device_id: str) -> dict:
    return {
        "GuestID": "",
        "Platform": "Android",
        "Version": GAME_VERSION,
        "DeviceID": device_id,
        "LoginType": "Erolabs",
        "IsNewSDK": 1,
    }

HTTP_TIMEOUT = 30.0

# ---------- 开发者模式（原始协议控制台）----------
# 可任意构造 route + 参数直接打游戏后端，用来抓协议、核对报文（例如免费刷商店被拒时的
# 真实响应，那次因为 route 没被 trace 而事后无法复原）。
# 入口靠**口令**解锁：设置页底部一个无提示输入框，输对口令才显示控制台。
# （原先是绑定测试账号白名单，换成口令后不再与某个具体账号耦合。）
# ⚠️ 口令必须在**后端**校验：本工具会打包成 exe/APK 分发，光靠前端隐藏入口等于
# 没有限制——任何能访问 localhost:27843 的人都能直接 POST /api/dev/call。
# 故 /api/dev/call 每次都要带上口令，由这里判定；前端不写死口令（真源只此一处）。
DEV_PASSPHRASE = "openrubi"


def check_dev_pass(value: str | None) -> bool:
    """口令是否正确。大小写敏感（它是口令不是账号名），容错首尾空格。

    用 compare_digest 而非 `==`：避免逐字符短路带来的时序侧信道。
    """
    if not value:
        return False
    return secrets.compare_digest(value.strip(), DEV_PASSPHRASE)
