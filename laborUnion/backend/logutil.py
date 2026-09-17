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
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import config

_LOG_DIR = Path(config._DATA_DIR) / "logs"
LOG_FILE = _LOG_DIR / "app.log"

REDACTED = "[REDACTED]"

# Credential/session fields are retained structurally but never with values.
_REDACT_KEYS = frozenset({
    "aid", "sessionid", "token", "accesstoken", "refreshtoken",
    "password", "password_b64", "authorization", "cookie", "set-cookie",
    "proxy-authorization",
})


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
            "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
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
