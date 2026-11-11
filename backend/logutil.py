"""开发者日志（落盘，供排查用户端异常）。

写到数据目录下的 logs/app.log（轮转，最多几 MB）。记录：启动信息、每个 API
请求的 method/path/状态、未预期异常的完整 traceback。用户遇到问题时把这个文件
发来分析即可。

数据目录：
- 桌面：exe 同级（ARK_DATA_DIR，desktop/run.py 设置）
- Android：app 私有目录 filesDir（android_entry.start 设置）
"""
from __future__ import annotations

import json
import logging
import platform as host_platform
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import config

_LOG_DIR = Path(config._DATA_DIR) / "logs"
# The source test server can run beside a preview/desktop process. Keep its
# rotating file separate so Windows does not fail when another process holds
# the normal app.log during rollover. Packaged builds continue using app.log.
LOG_FILE = _LOG_DIR / ("app-test.log" if config.TEST_SERVER else "app.log")

REDACTED = "[REDACTED]"

# Credential/session fields are retained structurally but never with values.
_REDACT_KEYS = frozenset({
    "aid", "sessionid", "token", "accesstoken", "refreshtoken",
    "password", "password_b64", "authorization", "cookie", "set-cookie",
    "proxy-authorization",
})

# Failed responses may be plain text or incomplete JSON. Keep their diagnostic
# text while masking the same credential fields as structured responses.
_TEXT_CREDENTIAL = re.compile(
    r"(?<![\w-])(?P<prefix>[\"']?(?:"
    + "|".join(re.escape(key) for key in sorted(_REDACT_KEYS, key=lambda key: (-len(key), key)))
    + r")[\"']?\s*[:=]\s*)"
    + r"(?:\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|(?:Bearer|Basic)\s+[^\s,;&<>{}\[\]\"']+|[^\s,;&<>{}\[\]\"']+)",
    re.IGNORECASE,
)
_TEXT_CREDENTIAL_HEADER = re.compile(
    r"^([ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)[^\r\n]*",
    re.IGNORECASE | re.MULTILINE,
)


def _redact_text(value: str) -> str:
    value = _TEXT_CREDENTIAL_HEADER.sub(lambda match: match[1] + REDACTED, value)
    return _TEXT_CREDENTIAL.sub(lambda match: match['prefix'] + REDACTED, value)


def redact(value: Any) -> Any:
    """Recursively redact credential/session fields before writing a trace."""
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).casefold() in _REDACT_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def format_payload(value: Any) -> str:
    """Serialize a redacted payload without truncating its contents."""
    safe = redact(value)
    if isinstance(safe, (dict, list, tuple)):
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    return str(safe)

_configured = False


def setup() -> logging.Logger:
    """配置并返回根 logger（幂等）。"""
    global _configured
    logger = logging.getLogger("lucima")
    if _configured:
        return logger
    logger.setLevel(logging.INFO)
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
    except Exception:
        pass  # 日志目录不可写时不影响主流程
    # 源码运行时也往 stderr 打一份（冻结后无控制台，写文件为主）
    if not getattr(sys, "frozen", False):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(sh)
    _configured = True
    logger.info("=" * 50)
    logger.info("LucimaTools 启动 platform=%s proxy_mode=%s log=%s",
                config.PLATFORM, config.PROXY_MODE, LOG_FILE)
    return logger


log = logging.getLogger("lucima")


def read_retained_logs() -> str:
    """Combine rotated files from oldest to newest into one export."""
    paths = [Path(f"{LOG_FILE}.{index}") for index in range(3, 0, -1)] + [LOG_FILE]
    chunks = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        if text:
            chunks.append(text.rstrip("\n"))
    return "\n".join(chunks) + ("\n" if chunks else "")


def developer_log_filename() -> str:
    return f"LucimaTools-developer-log-{datetime.now():%Y%m%d-%H%M%S}.txt"


def _platform_details() -> str:
    system = host_platform.system()
    release = host_platform.release()
    machine = host_platform.machine() or "unknown architecture"
    if system == "Windows":
        return f"Windows {release} ({host_platform.version()}; {machine})"
    return f"{system} {release} ({machine})"


def _game_data_version() -> str:
    try:
        from .resource_update import service as resource_service

        state = resource_service.status()
        version = str(state.get("version") or "").removeprefix("catalog_")
        return version or ("内置数据" if state.get("bundled") else "未知")
    except Exception:
        return "未知"


def developer_log_header() -> str:
    return (
        f"应用版本：{config.APP_VERSION}\n"
        f"平台：{_platform_details()}\n"
        f"游戏数据版本：{_game_data_version()}\n\n"
    )


def save_developer_logs() -> Path:
    directory = config.get_download_directory()
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / developer_log_filename()
    target = base
    suffix = 1
    while target.exists():
        target = directory / f"{base.stem}-{suffix}{base.suffix}"
        suffix += 1
    target.write_text(
        developer_log_header() + (read_retained_logs() or "（暂无日志）\n"),
        encoding="utf-8",
    )
    return target
