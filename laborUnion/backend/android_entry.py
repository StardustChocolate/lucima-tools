"""Android (Chaquopy) 端的 Python 入口。

Kotlin 侧通过 Chaquopy 调用 start(...)：先把运行时目录/资源根/代理写入
环境变量（必须在 import config 之前，因为 config 在导入时读取它们），
再启动标准库 HTTP 服务。服务在本调用线程里 serve_forever——Kotlin 应在
后台线程调用本函数。登录统一由本地 HTTP API 完成，不再需要 Android JS 桥。
"""
from __future__ import annotations

import os


def start(data_dir: str, asset_root: str, proxy: str = "", port: int = 27843):
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
    """当前实际生效的代理地址（空串=直连，靠系统 VPN/TUN 截流）。"""
    from . import config
    return config.get_proxy() or ""
