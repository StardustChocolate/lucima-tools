"""Android (Chaquopy) 端的 Python 入口。

Kotlin 侧通过 Chaquopy 调用 start(...)：先把运行时目录/资源根/代理写入
环境变量（必须在 import config 之前，因为 config 在导入时读取它们），
再启动标准库 HTTP 服务。服务在本调用线程里 serve_forever——Kotlin 应在
后台线程调用本函数。

login_from_token(token) 供 Kotlin 在 WebView 拿到 erolabsjwt 后直接调用，
完成游戏 bootstrap，返回 JSON 字符串（成功=账号状态，失败=错误信息）。
"""
from __future__ import annotations

import json
import os


def start(data_dir: str, asset_root: str, proxy: str = "", port: int = 8000):
    """初始化环境并启动服务器（阻塞）。由 Kotlin 后台线程调用。"""
    os.environ["ARK_PLATFORM"] = "android"
    os.environ["ARK_DATA_DIR"] = data_dir
    os.environ["ARK_ASSET_ROOT"] = asset_root
    if proxy:
        os.environ["ARK_PROXY"] = proxy

    # 延迟导入：确保上面的环境变量先生效
    from . import config, server
    if proxy:
        config.set_proxy(proxy)
    server.serve(port=port, host="127.0.0.1")


def set_proxy(proxy: str) -> str:
    from . import config
    return config.set_proxy(proxy)


def effective_proxy() -> str:
    """当前实际生效的代理地址（空串=直连，靠系统 VPN/TUN 截流）。供登录 WebView 用。"""
    from . import config
    return config.get_proxy() or ""


def login_from_token(token: str, account: str = "", password: str = "",
                     save_pwd: bool = False) -> str:
    """Kotlin WebView 拿到 erolabsjwt cookie 后调用，完成游戏登录。

    account = 用户在登录页填的邮箱，作为账号注册表的 key（否则会兜底成占位
    "android"，导致左下角账号显示成 android 而非真实账号名）。
    password/save_pwd = 供保存账号存档用（勾选保存密码时持久化，见 login_token）。
    返回 JSON 字符串：成功为登录结果（含 status），失败为 {"ok":false,"detail":...}。
    """
    from . import appcore
    from .appcore import ApiError
    try:
        result = appcore.login_token(token, (account or "").strip() or None,
                                     password or "", bool(save_pwd))
        return json.dumps(result, ensure_ascii=False)
    except ApiError as e:
        return json.dumps({"ok": False, "detail": e.message}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "detail": str(e)}, ensure_ascii=False)


def saved_password(account: str) -> str:
    """取账号存档里的明文密码，供 Android 登录 WebView 自动填表（选已保存账号时）。"""
    from . import appcore
    try:
        return appcore.saved_password(account)
    except Exception:
        return ""
