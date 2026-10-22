"""Small shared game configuration and client-native activity time checks."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path

from .game_client import GameError


@lru_cache(maxsize=1)
def metadata() -> dict:
    root = Path(os.environ.get("ARK_ASSET_ROOT") or Path(__file__).resolve().parent.parent)
    try:
        data = json.loads((root / "assets/game_config.json").read_text(encoding="utf-8"))
        if data.get("schema") == 1 and all(isinstance(data["tables"][key], dict)
                                          for key in ("PublicVariable", "Activity")):
            return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    raise GameError("公共游戏配置缺失或无效，请更新应用资源")


def public_int(identifier: str) -> int:
    try:
        return int(metadata()["tables"]["PublicVariable"][identifier]["VALUE"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise GameError(f"公共游戏参数 {identifier} 无效，请更新应用资源") from None


def enabled(value) -> bool:
    return value is True or str(value).lower() in {"true", "1"}


def timestamp(value) -> float:
    if isinstance(value, dict):
        value = value.get("$date")
    try:
        return float(value) / 1000
    except (TypeError, ValueError) as exc:
        raise GameError("活动时间数据无效，请刷新账号状态") from exc


def activity_open(identifier: str, state: dict, *, now: float, tables: dict | None = None) -> bool:
    if not identifier:
        return True
    tables = metadata()["tables"] if tables is None else tables
    definition = tables["Activity"].get(identifier, {})
    if enabled(definition.get("IsUnlimitTime")):
        return True
    settings = state.get("ActivityDBSettingContainer", {}).get("SettingList") or []
    setting = next((item for item in settings if item.get("ActivityID") == identifier), None)
    if setting is None:
        return False
    if enabled(setting.get("IsIgnoreTime")):
        return True
    start, end = timestamp(setting.get("StartTime")), timestamp(setting.get("EndTime"))
    off_days = int(setting.get("OffDays") or 0)
    if enabled(setting.get("IsRepeat")) or off_days > 0:
        # Match ActivityDBSetting's truncation of TotalHours and cycle index.
        open_hours = int((end - start) / 3600)
        period_hours = open_hours + off_days * 24
        if period_hours <= 0:
            raise GameError(f"活动 {identifier} 的重复周期无效")
        start += int((now - start) / (period_hours * 3600)) * period_hours * 3600
        end = start + open_hours * 3600
    return start <= now <= end
