# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：LucimaTools 桌面版。

用法（在项目根目录）：
    pyinstaller desktop/LucimaTools.spec --noconfirm

产物：dist/LucimaTools/LucimaTools.exe （onedir，双击运行）

说明：
- 前端 frontend/、运行资源 assets/、backend/*.json 数据表作为数据一并打包。
  backend 里的 json（物品名/装备表/活动名/稀有度）都靠 open(dirname(__file__)/x.json)
  读取，冻结后落 _MEIPASS/backend/，所以必须整目录打进去——漏一个就静默退化
  （曾漏 item_names.json 导致 exe 里物品名全变 "道具#ID"，源码启动却正常）。
- Playwright 用 channel="msedge" 复用系统 Edge，不打包 Chromium，
  但需带上 playwright 的 node driver（collect_all）。
- pywebview 用系统 WebView2（Win10/11 自带 Edge 运行时）。
"""
import glob
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.getcwd())

# 数据文件：(源路径, 打包内相对目录)
datas = [
    (os.path.join(ROOT, "frontend"), "frontend"),
    (os.path.join(ROOT, "assets"), "assets"),
]
# backend 下全部 json 数据表（新增数据文件无需再改本 spec）
datas += [(p, "backend") for p in glob.glob(os.path.join(ROOT, "backend", "*.json"))]
assert any(p.endswith("item_names.json") for p, _ in datas), "backend/item_names.json 缺失"
binaries = []
hiddenimports = collect_submodules("backend")

# Playwright 驱动 + 数据
for pkg in ("playwright", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, "desktop", "run.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 服务器已改用标准库，不需要 web 框架
        "fastapi", "uvicorn", "starlette", "pydantic", "pydantic_core",
        # 下列均非运行所需，是依赖分析/collect_all 误带入的大件
        "jedi", "parso",                       # IDE 补全，运行无关
        "numpy", "numpy.libs",                 # 未使用
        "matplotlib", "mpl_toolkits",          # 未使用
        "PIL", "Pillow",                       # 图标已预生成，运行不需要
        "scipy", "pandas",                     # 未使用
        "tkinter", "_tkinter",                 # pywebview 用 WebView2，不需要 tk
        "IPython", "pytest", "setuptools",     # 开发期工具
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LucimaTools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台窗口（纯 GUI）
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "desktop", "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LucimaTools",
)
