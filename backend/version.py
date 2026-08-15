"""应用版本号——**全项目唯一真源**。

改版本只改这一行。各处都从这里派生，不再各自写死：
  - 后端  `config.APP_VERSION`（转发本模块）、HTTP `Server:` 响应头（server.py）
  - 前端  `/api/config` 下发 `version` → 关于页版本徽章（app.js 注入，HTML 不写死）
  - Android  `app/build.gradle.kts` 构建时正则读取本文件 →
             `versionName` = 本值，`versionCode` = 主*10000+次*100+修订（单调递增）

⚠️ 不要在这里 import 项目内其它模块：Gradle 只做正则匹配、不执行 Python，
   保持本文件是"一个字符串常量"最省事；同时 config.py 也依赖它无副作用。
"""
from __future__ import annotations

APP_VERSION = "1.1.4"

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

    与 versionName 同源保证单调递增（旧手工值最大到 8，远小于 10100，可安全覆盖安装）。
    """
    major, minor, patch = version_tuple()
    return major * 10000 + minor * 100 + patch
