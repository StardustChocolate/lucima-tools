"""Windows 桌面入口：双击直接启动的应用。

- 后台线程起标准库 HTTP 服务（backend.server）
- 主线程用 pywebview 弹出独立原生窗口，加载本地服务的前端界面
- 关闭窗口时提供主题化确认弹窗，可勾选最小化到 Windows 系统托盘
- 登录由 Python 后端直接调用 EROLABS V2 JSON API

PyInstaller 打包后即为单个 LucimaTools.exe，双击运行。
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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
from backend.updater import mark_launch_success  # noqa: E402


class LucimaDesktop:
    """Native bridge and Windows shell integration for the desktop window."""

    def __init__(self):
        self._window = None
        self._allow_close = False
        self._close_prompt_pending = False
        self._tray_icon = None
        self._tray_menu = None
        self._winforms_module = None
        self._state_lock = threading.RLock()

    def bind_window(self, window):
        """Attach the close lifecycle after pywebview has created the window."""
        self._window = window
        window.events.closing += self._on_closing
        window.events.closed += self._on_closed

    def _winforms(self):
        """Load pywebview's already bundled WinForms runtime on demand."""
        if sys.platform != "win32":
            raise RuntimeError("Windows shell integration is only available on Windows")
        if self._winforms_module is None:
            # Importing this lazily keeps the module importable on non-Windows
            # hosts used for backend tests and tooling.
            from webview.platforms import winforms

            self._winforms_module = winforms
        return self._winforms_module

    def _request_close_prompt(self, window):
        """Ask the loaded WebView to show the themed close prompt."""
        try:
            result = window.evaluate_js(
                "typeof window.showDesktopCloseDialog === 'function' "
                "? (window.showDesktopCloseDialog(), true) : false"
            )
            if result is False:
                raise RuntimeError("桌面关闭弹窗尚未加载")
        except Exception:
            with self._state_lock:
                self._close_prompt_pending = False
            logger.exception("无法显示关闭确认弹窗")

    def _run_on_ui(self, window, callback):
        """Run a WinForms callback on the window thread when called by JS."""
        native = getattr(window, "native", None)
        if native is None or not getattr(native, "InvokeRequired", False):
            return callback()
        winforms = self._winforms()
        return native.Invoke(winforms.Func[winforms.Type](callback))

    def cancelClose(self):
        """Cancel the pending native close request from the WebView prompt."""
        with self._state_lock:
            self._close_prompt_pending = False
        return {"ok": True}

    def confirmClose(self, minimize_to_tray: bool = False):
        """Finish a close request, optionally keeping the app in the tray."""
        window = self._window
        if window is None:
            return {"ok": False, "detail": "桌面窗口不可用"}

        if minimize_to_tray:
            try:
                def minimize():
                    self._ensure_tray(window)
                    window.hide()

                self._run_on_ui(window, minimize)
            except Exception as error:
                with self._state_lock:
                    self._close_prompt_pending = False
                logger.exception("最小化到系统托盘失败")
                return {"ok": False, "detail": str(error) or "无法创建系统托盘图标"}
            with self._state_lock:
                self._close_prompt_pending = False
            return {"ok": True, "action": "tray"}

        with self._state_lock:
            self._allow_close = True
            self._close_prompt_pending = False
        # The closing callback disposes the tray on the WinForms UI thread
        # before allowing the form to close.
        try:
            window.destroy()
        except Exception as error:
            with self._state_lock:
                self._allow_close = False
            logger.exception("确认退出失败")
            return {"ok": False, "detail": str(error) or "无法关闭桌面窗口"}
        return {"ok": True, "action": "exit"}

    def _ensure_tray(self, window):
        """Create the notification-area icon and its restore/exit menu once."""
        with self._state_lock:
            if self._tray_icon is not None:
                return

            forms = self._winforms().WinForms
            icon = forms.NotifyIcon()
            native_icon = getattr(window.native, "Icon", None)
            if native_icon is None:
                from System.Drawing import SystemIcons

                native_icon = SystemIcons.Application
            icon.Icon = native_icon
            icon.Text = "LucimaTools"

            menu = forms.ContextMenuStrip()
            show_item = forms.ToolStripMenuItem("显示主窗口")
            exit_item = forms.ToolStripMenuItem("退出")
            show_item.Click += self._on_tray_show
            exit_item.Click += self._on_tray_exit
            menu.Items.Add(show_item)
            menu.Items.Add(exit_item)
            icon.ContextMenuStrip = menu
            icon.DoubleClick += self._on_tray_show
            icon.Visible = True
            self._tray_menu = menu
            self._tray_icon = icon

    def _dispose_tray(self):
        with self._state_lock:
            icon = self._tray_icon
            menu = self._tray_menu
            self._tray_icon = None
            self._tray_menu = None
        if icon is not None:
            try:
                icon.ContextMenuStrip = None
                icon.Visible = False
                icon.Dispose()
            except Exception:
                logger.exception("释放系统托盘图标失败")
        if menu is not None:
            try:
                menu.Dispose()
            except Exception:
                logger.exception("释放系统托盘菜单失败")

    def _on_tray_show(self, *_):
        window = self._window
        if window is None:
            return
        try:
            native = window.native
            if native is not None:
                forms = self._winforms().WinForms
                native.WindowState = forms.FormWindowState.Normal
            window.show()
        except Exception:
            logger.exception("从系统托盘恢复窗口失败")

    def _on_tray_exit(self, *_):
        window = self._window
        with self._state_lock:
            self._allow_close = True
        self._dispose_tray()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                logger.exception("从系统托盘退出失败")

    def _on_closing(self, window):
        with self._state_lock:
            if self._allow_close:
                self._dispose_tray()
                return True

            if self._close_prompt_pending:
                return False
            self._close_prompt_pending = True

        # FormClosing runs on the WinForms UI thread. Calling WebView2 from
        # that callback can deadlock, so notify the page from a worker after
        # returning False and keep the native close request cancelled.
        threading.Thread(
            target=self._request_close_prompt,
            args=(window,),
            name="desktop-close-prompt",
            daemon=True,
        ).start()
        return False

    def _on_closed(self, *_):
        self._dispose_tray()

    def chooseDownloadDirectory(self, initial: str = "") -> str:
        if not webview.windows:
            return ""
        directory = str(initial or "")
        try:
            selected = webview.windows[0].create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=directory if Path(directory).is_dir() else "",
            )
        except Exception:
            return ""
        return str(selected[0]) if selected else ""


def _free_port(preferred: int = server.DEFAULT_PORT) -> int:
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
    port = _free_port()
    srv = _start_server(port)
    # 等服务器起来
    time.sleep(0.4)
    url = f"http://127.0.0.1:{port}/"
    bridge = LucimaDesktop()
    try:
        window = webview.create_window(
            "璐茜玛的深层幻影",
            url,
            js_api=bridge,
            width=1180,
            height=800,
            min_size=(900, 640),
            text_select=True,  # pywebview 默认 False（禁止选中）——显式打开以便复制页面文字
        )
        if window is None:
            raise RuntimeError("pywebview 未创建桌面窗口")
        bridge.bind_window(window)
        mark_launch_success()
        webview.start()  # 阻塞至窗口关闭
    finally:
        bridge._dispose_tray()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
