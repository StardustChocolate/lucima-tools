"""全局配置常量。

代理有三种模式（proxy_mode）：
  - "system"（默认）：跟随系统代理。Windows 读注册表里的 IE/系统代理（Clash 等
    开启"系统代理"时会写这里）；读不到就直连——此时若 Clash 开的是 TUN/虚拟网卡
    模式，流量仍会被网卡层截走，所以"跟随系统"能同时兼容这两种常见用法，用户
    只需开关代理软件，无需在本工具里配置。Android 上直连（靠系统 VPN/TUN 截流）。
  - "manual"：使用用户手填的地址（代理在别的机器/端口时用）。
  - "direct"：强制直连。

设置持久化在 settings.json（数据目录）。环境变量 ARK_PROXY 若存在则初始为 manual。
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

_DEFAULT_MANUAL = "http://127.0.0.1:7890"


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
_env_proxy = os.environ.get("ARK_PROXY")
# 模式：env 指定了代理→manual；否则用存档；再否则默认 system（跟随系统）
PROXY_MODE: str = (
    "manual" if _env_proxy else _settings.get("proxy_mode", "system")
)
# 手动地址（manual 模式用）
MANUAL_PROXY: str = _env_proxy or _settings.get("proxy") or _DEFAULT_MANUAL


def _system_proxy() -> str:
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


def get_proxy() -> str:
    """按当前模式解析出实际代理地址（空串=直连）。每次调用实时读取。"""
    if PROXY_MODE == "direct":
        return ""
    if PROXY_MODE == "manual":
        return MANUAL_PROXY
    return _system_proxy()  # "system"


def get_proxy_config() -> dict:
    """给前端展示：当前模式 + 手动地址 + 当前实际生效的代理。"""
    return {"mode": PROXY_MODE, "manual": MANUAL_PROXY, "effective": get_proxy()}


def set_proxy_config(mode: str | None = None, manual: str | None = None) -> dict:
    """更新代理模式/手动地址并持久化。"""
    global PROXY_MODE, MANUAL_PROXY
    if mode in ("system", "manual", "direct"):
        PROXY_MODE = mode
    if manual is not None:
        MANUAL_PROXY = manual.strip() or _DEFAULT_MANUAL
    s = _load_settings()
    s["proxy_mode"] = PROXY_MODE
    s["proxy"] = MANUAL_PROXY
    _save_settings(s)
    return get_proxy_config()


# 兼容旧接口（Android 桥可能仍调 set_proxy）
def set_proxy(proxy: str) -> str:
    set_proxy_config(mode="manual", manual=proxy)
    return get_proxy()


def data_dir() -> Path:
    """Return the writable application data root used by this process."""
    return _DATA_DIR


def get_ignored_update_version() -> str:
    return str(_load_settings().get("ignored_update_version") or "")


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
