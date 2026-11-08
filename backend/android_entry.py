"""Android (Chaquopy) 端的 Python 入口。

Kotlin 侧通过 Chaquopy 调用 start(...)：先把运行时目录/资源根写入
环境变量（必须在 import config 之前，因为 config 在导入时读取它们），
再启动标准库 HTTP 服务并返回实际绑定端口。后台线程托管服务，同一进程
重复调用复用已启动的实例。登录统一由本地 HTTP API 完成。
"""
from __future__ import annotations

import os
import errno
from pathlib import Path
import threading
import uuid

_start_lock = threading.Lock()
_server = None
_server_thread = None
_server_key = None


def _serve(http):
    global _server, _server_thread, _server_key
    try:
        http.serve_forever()
    finally:
        http.server_close()
        with _start_lock:
            if _server is http:
                _server = _server_thread = _server_key = None


def running_port() -> int:
    """Let the Android service recover if the HTTP worker has exited."""
    with _start_lock:
        if _server is not None and _server_thread is not None and _server_thread.is_alive():
            return _server.server_port
        return 0


def start(data_dir: str, asset_root: str, proxy: str = "", port: int = 27843,
          apk_path: str = "", instance_id: str = "") -> int:
    """Bind once, falling back only on EADDRINUSE; return our bound port.

    Android ports are shared across apps. Never probe/reuse an arbitrary listener
    at the preferred address, or reserve a port and close it before binding.
    """
    global _server, _server_thread, _server_key
    key = (str(Path(data_dir).resolve()), str(Path(asset_root).resolve()), instance_id)
    with _start_lock:
        if _server is not None and _server_thread is not None and _server_thread.is_alive():
            if _server_key != key:
                raise RuntimeError('本地服务已由其他启动实例初始化')
            return _server.server_port
        os.environ["ARK_PLATFORM"] = "android"
        os.environ["ARK_DATA_DIR"] = data_dir
        os.environ["ARK_ASSET_ROOT"] = asset_root
        os.environ["ARK_ANDROID_APK_PATH"] = apk_path
        # Environment must be configured before importing any backend modules.
        from . import server, background, resource_sync
        from .logutil import log
        try:
            http = server.make_server(port=port, host="127.0.0.1")
        except OSError as error:
            if error.errno != errno.EADDRINUSE or port == 0:
                raise
            # port=0 atomically chooses and binds a free loopback port.
            http = server.make_server(port=0, host="127.0.0.1")
            log.warning('[android-startup] port %s in use; bound own service on %s', port, http.server_port)
        http.lucima_instance_id = instance_id or uuid.uuid4().hex
        try:
            background.start()
            resource_sync.start()
            worker = threading.Thread(target=_serve, args=(http,), name='android-local-http', daemon=True)
            _server, _server_thread, _server_key = http, worker, key
            worker.start()
        except BaseException:
            _server = _server_thread = _server_key = None
            http.server_close()
            raise
        return http.server_port


def set_proxy(proxy: str) -> str:
    """兼容旧 Android 桥；代理始终由系统 VPN/TUN 接管。"""
    from . import config
    return config.set_proxy(proxy)


def effective_proxy() -> str:
    """当前实际生效的代理地址（空串=直连，靠系统 VPN/TUN 截流）。"""
    from . import config
    return config.get_proxy() or ""
