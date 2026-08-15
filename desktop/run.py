"""Windows 桌面入口：双击直接启动的应用。

- 后台线程起标准库 HTTP 服务（backend.server）
- 主线程用 pywebview 弹出独立原生窗口，加载本地服务的前端界面
- 登录时由 backend 的 Playwright 辅助登录弹 Edge 让用户点验证码

PyInstaller 打包后即为单个 LucimaTools.exe，双击运行。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

# 兼容两种运行方式：
#   1) 源码运行 python -m desktop.run（项目根在 sys.path）
#   2) PyInstaller 冻结后（_MEIPASS 为资源根）
if getattr(sys, "frozen", False):
    _ROOT = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    _ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 资源根（frontend/ assets/）与数据目录（settings.json / .browser_profile）
os.environ.setdefault("ARK_ASSET_ROOT", str(_ROOT))
# 数据目录：冻结时放 exe 同级（可写），源码时放项目根
if getattr(sys, "frozen", False):
    _DATA = Path(sys.executable).resolve().parent
else:
    _DATA = _ROOT
os.environ.setdefault("ARK_DATA_DIR", str(_DATA))
os.environ.setdefault("ARK_PLATFORM", "windows")

import webview  # noqa: E402

from backend import server  # noqa: E402


def _free_port(preferred: int = 8000) -> int:
    """优先用 preferred 端口，被占用则让系统分配一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.bind(("127.0.0.1", 0))
                return s2.getsockname()[1]


def _start_server(port: int):
    srv = server.make_server(port, "127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def main():
    port = _free_port(8000)
    _start_server(port)
    # 等服务器起来
    time.sleep(0.4)
    url = f"http://127.0.0.1:{port}/"
    webview.create_window(
        "璐茜玛的深层幻影",
        url,
        width=1180,
        height=800,
        min_size=(900, 640),
        text_select=True,  # pywebview 默认 False（禁止选中）——显式打开以便复制页面文字
    )
    webview.start()  # 阻塞至窗口关闭


if __name__ == "__main__":
    main()
