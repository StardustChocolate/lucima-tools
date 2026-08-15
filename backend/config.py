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
from pathlib import Path

from .version import APP_VERSION  # noqa: F401  （版本唯一真源，见 version.py）

# ---------- 平台标志 ----------
# 由入口（desktop/run.py 或 Android 桥）设置为 "windows" / "android"。
PLATFORM = os.environ.get("ARK_PLATFORM", "windows")

# ---------- 可持久化设置 ----------
_DATA_DIR = Path(os.environ.get("ARK_DATA_DIR", Path(__file__).resolve().parent.parent))
_SETTINGS_FILE = _DATA_DIR / "settings.json"

_DEFAULT_MANUAL = "http://127.0.0.1:7890"


def _load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# 运行时内存态
_settings = _load_settings()
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


# ---------- 多账号持久化 ----------
# settings.json 里 accounts 是个字典：email -> {password_b64, save_pwd, toggles, params, order}
#   - password_b64: base64 混淆的密码（save_pwd=True 时才存；仅本机，非加密，防肩窥级别）
#   - save_pwd: 用户是否勾选了"保存密码"
#   - toggles: {task_id: bool} 该账号开启的自动任务开关
#   - params:  {task_id: {...}} 自动任务参数（如购买目标白名单）
#   - order:   排序用的整数（添加顺序）
import base64 as _b64


def _enc_pwd(pw: str) -> str:
    return _b64.b64encode(pw.encode("utf-8")).decode("ascii")


def _dec_pwd(s: str) -> str:
    try:
        return _b64.b64decode(s.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def load_accounts() -> dict:
    """返回 email -> 账号存档 dict（含解密后的 password 字段，供后端登录用）。"""
    s = _load_settings()
    accs = s.get("accounts") or {}
    out = {}
    for email, rec in accs.items():
        rec = dict(rec or {})
        rec["password"] = _dec_pwd(rec.get("password_b64", "")) if rec.get("save_pwd") else ""
        out[email] = rec
    return out


def save_account(email: str, *, password: str | None = None, save_pwd: bool | None = None,
                 toggles: dict | None = None, params: dict | None = None) -> None:
    """新增/更新一个账号存档。只更新传入的字段，其余保留。"""
    s = _load_settings()
    accs = s.get("accounts") or {}
    rec = dict(accs.get(email) or {})
    if "order" not in rec:
        rec["order"] = len(accs)
    if save_pwd is not None:
        rec["save_pwd"] = bool(save_pwd)
    # 密码：save_pwd 为真且给了密码才存；save_pwd 为假则清除
    if rec.get("save_pwd"):
        if password:
            rec["password_b64"] = _enc_pwd(password)
    else:
        rec.pop("password_b64", None)
    if toggles is not None:
        rec["toggles"] = toggles
    if params is not None:
        rec["params"] = params
    accs[email] = rec
    s["accounts"] = accs
    _save_settings(s)


def delete_account(email: str) -> None:
    s = _load_settings()
    accs = s.get("accounts") or {}
    if email in accs:
        del accs[email]
        s["accounts"] = accs
        _save_settings(s)


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


# ---------- EROLABS 门户 ----------
# 三个入口指向**同一个后端**(实测 /api/v2/config/turnstileSiteKey 返回同一个
# Turnstile key 0x4AAAAAACArc0cJVkc8ZTET，登录页表单字段 ID 也完全一致)。
#   game.ero-labs.art  —— **当前默认**，实测可直连(curl 无代理 HTTP 200)
#   game.ero-labs.tech —— 上一个入口，2026-08-10 起 302 跳到 .art（页面和 API 都跳）
#   www.ero-labs.com   —— 最早的入口，本机需走代理才通
# 站点的 baseUrl 在页面 JS 里是相对路径 "../api"，所以 API base = 站点根 + /api。
#
# ⚠️ 上游换过两次址(.com → .tech → .art)，**别指望靠 302 兜着**：多一跳会吃掉
# Playwright 的 goto 预算(源站本身就慢且抖，实测同一 HTML 1.4s~17.3s)，2026-08-10
# 桌面辅助登录就是这么超时崩的。换址时改这里一处，别留着让重定向顶。
PORTAL_SITE_ART = "https://game.ero-labs.art"
PORTAL_SITE_TECH = "https://game.ero-labs.tech"   # 旧入口：现 302 → .art
PORTAL_SITE_COM = "https://www.ero-labs.com"      # 最早入口：需代理

PORTAL_SITE = PORTAL_SITE_ART
PORTAL_BASE = f"{PORTAL_SITE}/api"
PORTAL_LOGIN = f"{PORTAL_BASE}/v2/login"
PORTAL_USERINFO = f"{PORTAL_BASE}/v2/accountManagement/userInfo"
PORTAL_LOGIN_PAGE = f"{PORTAL_SITE}/cn/login.html"
PORTAL_HOME_PAGE = f"{PORTAL_SITE}/cn/index.html"
TURNSTILE_SITE_KEY = "0x4AAAAAACArc0cJVkc8ZTET"

# 门户是否需要代理：新入口可直连，走代理反而多一跳(且用户代理不一定开着)。
# 用"可直连集合"而不是跟某一个站点比——再加入口时往集合里塞即可，不用改判断。
PORTAL_SITES_DIRECT = {PORTAL_SITE_ART, PORTAL_SITE_TECH}
PORTAL_NEEDS_PROXY = PORTAL_SITE not in PORTAL_SITES_DIRECT

# 游戏后端(Ark Re:Code)
GAME_ROUTER = "https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx"
GAME_ORIGIN = "https://game-arkre-labs.ecchi.xxx"
GAME_REFERER = "https://game-arkre-labs.ecchi.xxx/WebGL/index.html"
GAME_VERSION = "4.0.0.111888"

# 登录握手 common 参数
LOGIN_COMMON = {
    "GuestID": "",
    "Platform": "WebGLPlayer",
    "Version": GAME_VERSION,
    "DeviceID": "n/a",
    "LoginType": "Erolabs",
    "IsNewSDK": 0,
}

HTTP_TIMEOUT = 30.0

# ---------- 开发者模式（原始协议控制台）----------
# 可任意构造 route + 参数直接打游戏后端，用来抓协议、核对报文（例如免费刷商店被拒时的
# 真实响应，那次因为 route 没被 trace 而事后无法复原）。
# 入口靠**口令**解锁：设置页底部一个无提示输入框，输对口令才显示控制台。
# （原先是绑定测试账号白名单，换成口令后不再与某个具体账号耦合。）
# ⚠️ 口令必须在**后端**校验：本工具会打包成 exe/APK 分发，光靠前端隐藏入口等于
# 没有限制——任何能访问 localhost:8000 的人都能直接 POST /api/dev/call。
# 故 /api/dev/call 每次都要带上口令，由这里判定；前端不写死口令（真源只此一处）。
DEV_PASSPHRASE = "openrubi"


def check_dev_pass(value: str | None) -> bool:
    """口令是否正确。大小写敏感（它是口令不是账号名），容错首尾空格。

    用 compare_digest 而非 `==`：避免逐字符短路带来的时序侧信道。
    """
    if not value:
        return False
    return secrets.compare_digest(value.strip(), DEV_PASSPHRASE)
