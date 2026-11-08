"""应用版本号——**全项目唯一真源**。

本机打包技能通过 tools/release_version.py 管理版本，并写入本文件。
APP_VERSION_BASE 是固定的前三段；仅安卓打包递增第四段，Windows 和 iOS
使用最近成功安卓包的完整版本，不额外加号。各处从这里读取：
  - 后端  `config.APP_VERSION`（转发本模块）、HTTP `Server:` 响应头（server.py）
  - 前端  `/api/config` 下发 `version` → 关于页版本徽章（app.js 注入，HTML 不写死）
  - Android  `app/build.gradle.kts` 构建时正则读取本文件 →
             `versionName` = 本值，`versionCode` 取派生值与 ANDROID_VERSION_CODE 的较大值

⚠️ 不要在这里 import 项目内其它模块：Gradle 只做正则匹配、不执行 Python，
   仅保留常量与纯函数；config.py 也依赖它无副作用。
"""
from __future__ import annotations

APP_VERSION = "1.3.4.43"
APP_VERSION_BASE = "1.3.4"

# Monotonic installation code, independent of the four-part display label.
# The shared build-number helper increments this when reserving a release.
ANDROID_VERSION_CODE = 10356

# 版本号三段（Android versionCode 用；非法格式时回退 0，避免构建期炸掉）
def version_tuple() -> tuple[int, int, int]:
    parts = (APP_VERSION.split("-")[0].split(".") + ["0", "0", "0"])[:3]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)  # type: ignore[return-value]


def version_code() -> int:
    """Android versionCode：主*10000 + 次*100 + 修订。1.1.0 → 10100。

    兼容指定较低显示版本号的修复包，使用显式安装版本号避免降级限制。
    """
    major, minor, patch = version_tuple()
    return max(major * 10000 + minor * 100 + patch, ANDROID_VERSION_CODE)
