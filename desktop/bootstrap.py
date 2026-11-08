"""Standard-library startup checks for the private Windows runtime."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import sys

from backend.branding import APP_NAME, WINDOWS_APPLICATION_ID

MIN_WINDOWS_BUILD = 17763  # Windows 10 1809; also supports Windows 11.
MIN_NET_FRAMEWORK = 394802  # .NET Framework 4.6.2 (included in supported Windows).


def _data_directory() -> Path:
    default = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / WINDOWS_APPLICATION_ID / "UserData"
    return Path(os.environ.setdefault("ARK_DATA_DIR", str(default)))


def _net_framework_available() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
            0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        ) as key:
            release, _ = winreg.QueryValueEx(key, "Release")
        return int(release) >= MIN_NET_FRAMEWORK
    except (OSError, ValueError, TypeError):
        return False


def _webview2_available() -> bool:
    """Ask Microsoft's loader, including per-user and system installations."""
    loader = Path(__file__).resolve().parent.parent / "webview/lib/runtimes/win-x64/native/WebView2Loader.dll"
    try:
        library = ctypes.WinDLL(str(loader))
        get_version = library.GetAvailableCoreWebView2BrowserVersionString
        get_version.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        get_version.restype = ctypes.c_long
        version = ctypes.c_void_p()
        result = get_version(None, ctypes.byref(version))
        try:
            return result == 0 and bool(version.value)
        finally:
            if version.value:
                free = ctypes.windll.ole32.CoTaskMemFree
                free.argtypes = [ctypes.c_void_p]
                free.restype = None
                free(version)
    except (OSError, AttributeError):
        return False


def check_environment() -> None:
    if sys.platform != "win32":
        return
    if sys.getwindowsversion().build < MIN_WINDOWS_BUILD or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("需要 Windows 10 1809 或更新版本、Windows 11 的 64 位系统。")
    if not _net_framework_available():
        raise RuntimeError(
            "系统的 .NET Framework 组件不可用。请安装或修复 Microsoft .NET Framework 4.8 后重试。\n"
            "官方下载：https://go.microsoft.com/fwlink/?linkid=2088631"
        )
    if not _webview2_available():
        raise RuntimeError("Microsoft Edge WebView2 运行时不可用。请重新运行完整安装包，安装程序会离线补齐该组件。")


def _launch() -> None:
    # Use the same CLR on every machine, regardless of development tools or
    # other Python/.NET installations in the player's environment.
    if sys.platform == "win32":
        os.environ["PYTHONNET_RUNTIME"] = "netfx"
        os.environ.pop("PYTHONNET_NETFX_RUNTIME_CONFIG", None)
        if (Path(sys.executable).parent / "python311.dll").is_file():
            os.environ["PYTHONNET_PYDLL"] = str(Path(sys.executable).parent / "python311.dll")
    from desktop.run import main as launch

    launch()


def main() -> int:
    if sys.argv[1:] == ["--check-webview2"]:
        return 0 if _webview2_available() else 1
    data = _data_directory()
    log_file = data / "logs/startup.log"
    handler = None
    try:
        from logging.handlers import RotatingFileHandler

        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        logging.info("Starting %s; Python %s; Windows=%s", APP_NAME, sys.version, sys.platform)
        check_environment()
        _launch()
        return 0
    except Exception as error:
        logging.exception("Desktop startup failed")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None, f"启动失败：{error}\n\n错误日志：{log_file}\n请将日志发给开发者排查。",
                APP_NAME, 0x10,
            )
        return 1
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
