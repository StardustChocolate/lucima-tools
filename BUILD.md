# LucimaTools 跨平台构建说明

同一份 Python 核心（`backend/`）+ 前端（`frontend/`）+ 运行资源（`assets/`）
分别打包成 Windows 桌面应用与 Android 应用。

## 快速构建（推荐）

改完内容后，直接用根目录的构建脚本，两端一键出包：

| 方式 | 命令 |
|---|---|
| Windows 双击 | 双击 `build.bat`（= 构建双端） |
| 命令行 | `build.bat`（双端） / `build.bat win` / `build.bat android` |
| git-bash | `./build.sh` / `./build.sh win` / `./build.sh android` |

脚本已处理：构建前结束运行中的 exe、清理旧 `dist` 和 PyInstaller 缓存，并通过
仓库自带的 Gradle wrapper 构建 Android。Android 构建环境由 Android Studio、
`JAVA_HOME` / `ANDROID_HOME` 或 `android/local.properties` 提供。

> 改了 `frontend/` 里的 css/js 记得在 `frontend/index.html` 顶部把 `?v=NN`
> +1（桌面 WebView2 会缓存静态资源；HTML 本身不缓存）。这是**静态资源缓存令牌**，
> 与下面的应用版本号是两回事，改前端就要动，不必等发版。

## 版本号（唯一真源）

应用版本号只写在 **`backend/version.py`** 的 `APP_VERSION` 一处，其余全部派生：

| 用处 | 来源 |
|---|---|
| 关于页版本徽章 | `/api/config` 下发 → `app.js` 注入 `#aboutVer`（HTML 不写死） |
| HTTP `Server:` 响应头 | `server.py` 读 `config.APP_VERSION` |
| Android `versionName` | `build.gradle.kts` 构建时正则读 `version.py` |
| Android `versionCode` | 同上派生：主×10000 + 次×100 + 修订（`1.1.0` → `10100`） |

所以**发版只改 `APP_VERSION` 一行**，两端一起跟上。`versionCode` 也不用再手工 bump ——
`AssetSync` 的戳记已改成 `versionCode:lastUpdateTime`，同版本内重装也会重新释放
web 资源，不会再出现"装了新包却跑旧前端"。

## 更新运行资源

外部资源库不与本仓库建立依赖。需要更新图片时，把它的内容原样复制到
`assets_full/`，无需调整其中的中文目录或文件名，然后在项目根执行：

```powershell
python tools/sync_assets.py
```

脚本通过固定映射读取 `团员/头像/`、`装备/图标/` 与 `装备/套装图标/`，把全部可用
角色头像、`backend/equip_ref.json` 引用的装备图，以及 `backend/tasks.py` 使用的套装图
同步到 `assets/`。`assets_full/` 的资源内容被 Git 忽略，只有筛选后的 `assets/` 会进入
仓库和安装包。映射细节见 `assets_full/README.md`。

产物：
- Windows：`dist/LucimaTools/`（整个文件夹，含 `LucimaTools.exe` + `_internal`，一起分发）
- Android：`android/app/build/outputs/apk/debug/app-debug.apk`

下面是各平台的详细说明与原理。

## 架构

- **服务器层用标准库 `http.server`**（`backend/server.py`），不依赖 FastAPI/uvicorn/pydantic，
  故 Windows(PyInstaller) 与 Android(Chaquopy) 可共用同一份服务器代码。
- **平台无关核心** `backend/appcore.py`：会话、状态摘要、各 API 端点处理。
- **代理运行时可配置** `backend/config.py`：支持跟随系统、手动地址和强制直连三种模式，
  前端“系统设置 → 网络代理”可随时切换。
- **登录**是唯一的平台分叉：
  - Windows：`backend/browser_login.py` 用 Playwright 弹 Edge，用户手点验证码。
  - Android：Kotlin 用 WebView 打开登录页（经代理），用户手点验证码，`CookieManager`
    取 `erolabsjwt` → 交给 Python `android_entry.login_from_token()`。

## Windows（双击直接启动的应用）

依赖：`pip install pywebview playwright pyinstaller httpx`（Playwright 用系统 Edge，无需下载 Chromium）。

- **开发运行**：`python -m desktop.run`
- **打包**：`build.bat win`（或 `pyinstaller desktop/LucimaTools.spec --noconfirm --clean`）
- **产物**：`dist\LucimaTools\LucimaTools.exe`，双击运行，弹出独立原生窗口。
- 网络代理可在应用“系统设置”中选择跟随系统、手动地址或强制直连。

## Android（Chaquopy 复用 Python）

依赖：Android Studio（JDK 17+）、Android SDK 与 NDK 26。构建时自动下载 Chaquopy
Python 3.12 运行时 + pip 装 httpx（走代理）。

- **构建前**：`android/app/build.gradle.kts` 的 Sync task 会自动把项目根的 `backend/`
  （排除 Windows 专用的 `browser_login.py`）复制进 Chaquopy 源目录，把 `frontend/`+`assets/`
  复制进 assets。**改了这些共享源后重新构建即自动同步**（单一真源在项目根，不在 android/ 里）。
- **签名**：复制 `android/keystore.properties.example` → `keystore.properties` 填入信息；
  缺失则回退 debug 签名。
- **构建**（在 `android/` 目录）：
  ```
  export JAVA_HOME="<JDK 17 路径>"
  export ANDROID_HOME="<Android SDK 路径>"
  ./gradlew :app:assembleDebug     # 或用 Android Studio 打开 android/ 直接运行
  ```
- **产物**：`android/app/build/outputs/apk/debug/app-debug.apk`（约 75MB，含 Python 运行时）。
- **运行**：手机上先跑代理 App（Clash 等）提供本地代理，在应用“系统设置”里填入其地址
  （如 `http://127.0.0.1:7890`），再登录。

## 待人工验证项

- **Windows**：双击 exe 后窗口是否正常显示（agent 无法可视化确认）。
- **Android**：真机安装 APK → 配代理 → 登录（WebView 手点验证码）→ 各日常任务。
  需真机 + 代理 App，无法在开发机自动验证。
