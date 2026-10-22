# LucimaTools 跨平台构建说明

同一份 Python 核心（`backend/`）+ 前端（`frontend/`）+ 运行资源（`assets/`）
分别打包成 Windows 桌面应用与 Android 应用。

## 构建入口

正式发布和本地测试使用不同入口，避免把缺少更新端点或正式签名的包误发给用户：

| 用途 | Windows 命令 | 产物目录 |
|---|---|---|
| 正式发布 | `build.bat` / `build.bat win` / `build.bat android` | `releases/<版本号>/` |
| 本地测试 | `build-debug.bat` / `build-debug.bat win` / `build-debug.bat android` | `debug/<版本号>/` |
| Git Bash 测试 | `./build.sh` / `./build.sh win` / `./build.sh android` | `debug/<版本号>/` |

`build.bat` 会构建 Windows Release 和使用正式证书签名的 Android Release，并在输出前
验证应用版本、更新端点、APK 完整签名与证书指纹。任一检查失败时不会生成可发布结果。
正式产物为：

```text
releases/<版本号>/
├── LucimaTools-windows-x64-<版本号>.zip
├── LucimaTools-android-<版本号>.apk
├── LucimaTools-<平台>-<旧版本>-to-<版本号>.delta.zip
├── candidate.json
└── SHA256SUMS.txt
```

测试构建不连接生产更新服务，成功后生成：

```text
debug/<版本号>/
├── LucimaTools-windows-x64-debug-<版本号>.zip
└── LucimaTools-android-debug-<版本号>.apk
```

`build/`、`dist/` 与 `android/app/build/` 都是可重新生成的中间目录；`debug/` 和
`releases/` 分别保存测试包与已验证的正式包，均不进入 Git。

## 正式构建本机配置

更新地址、APK 私钥和签名密码不进入仓库。首次配置发布电脑时：

1. 运行 `python tools/init_android_signing.py` 生成
   `android/signing/lucima.keystore` 与 `android/keystore.properties`。生成器发现已有文件时
   会拒绝覆盖，避免意外更换发布身份。
2. 复制 `build.local.bat.example` 为 `build.local.bat`，填写更新服务的基础地址
   `LUCIMA_UPDATE_BASE_URL` 和固定清单地址 `LUCIMA_UPDATE_URL`。
3. 将 keystore 和 properties 在项目外至少备份一份；丢失后无法为现有 Android 用户提供
   可覆盖安装的升级包。

上述真实配置均已由 `.gitignore` 明确排除。`android/keystore.properties.example` 和
`build.local.bat.example` 只描述格式，不包含真实地址、密码或证书指纹。

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

## 更新构建与验证

应用更新使用签名清单和文件 SHA-256 校验，与具体托管平台无关。
签名私钥保存在发布电脑的 `%USERPROFILE%/.lucimatools/update-signing/private.pem`，
仓库和客户端仅包含公钥。运行 `pip install -r requirements-release.txt` 安装本地签名工具依赖。

`build.bat` 从被忽略的 `build.local.bat` 读取更新入口，注入安装包运行配置：

```bat
set "LUCIMA_UPDATE_BASE_URL=https://download.example"
set "LUCIMA_UPDATE_URL=https://download.example/releases/stable.json"
```

这些地址仅为格式示例。真实端点、签名材料、安装包和部署工具均在本机管理。
构建流程只生成和校验本地产物，不执行远程上传。

```text
releases/
├── stable.json                  # 当前正式版本
├── preview.json                 # 本轮候选版本
├── stable-history/              # 本地正式发布历史，供增量基准选择
└── <版本号>/
    ├── LucimaTools-windows-x64-<版本号>.zip
    ├── LucimaTools-android-<版本号>.apk
    ├── LucimaTools-<平台>-<旧版本>-to-<版本号>.delta.zip
    ├── candidate.json
    └── SHA256SUMS.txt
```

### 预览与稳定通道

用户默认检查 `stable.json`。测试设备在设置页解锁开发者模式后，可以选择同目录下的
`preview.json`；通道选择仅影响当前设备，切换时清除旧清单和 ETag，并重新检查。
预览通道属于功能测试入口，不能作为访问控制。

1. 修改 `backend/version.py` 的版本号，运行 `build.bat`，生成安装包、增量补丁和预览清单。
2. 在测试设备验证候选版本的安装、启动、设置保留和主要任务。
3. 测试通过后，运行 `promote-release.bat`，或执行以下命令，仅在本地生成稳定清单：

   ```powershell
   python tools/publish_update.py --promote releases/preview.json
   ```

提升前重新验签，并核对两端完整包和所有补丁的大小、SHA-256；稳定清单复用已经测试的
文件地址、哈希及更新说明，只改变通道和发布时间。也可以通过
`--promote releases/<版本号>/candidate.json` 选择特定候选版本。
正式版本不得覆盖构建，变更内容需要增加版本号。

更新说明从 `CHANGELOG.md` 中对应版本的章节提取；缺少该章节时为空。
最低支持版本默认沿用上一份已验签清单，首次未指定时为 `0.0.0`；
仅在明确需要强制更新时设置 `LUCIMA_MIN_SUPPORTED`。

图鉴媒体和兑换码接口可以通过独立的 `encyclopedia-endpoint.json`、
`redeem-codes-endpoint.json` 配置，打包时分别注入两端安装包，避免与软件更新入口混用。
真实配置文件不进入 Git；资源格式和客户端加载行为见 [图鉴资源](docs/encyclopedia-resources.md)。

### 双端增量更新

Windows 补丁包含完整目标文件表和新增、变化的文件。客户端逐个核对并复制本地未变化
文件，构造一个全新目录；不再使用的旧文件自然移除，再沿用原有替换与启动失败回滚流程。
账号、设置和日志位于安装文件之外，不参与补丁替换。

Android 补丁按 ZIP 元数据定位可复用的原始压缩数据，记录旧 APK 字节范围与新增数据。
客户端先核对已安装 APK 的 SHA-256，再流式还原新 APK，包括 ZIP 元数据和原始签名块。
还原后的 APK 必须与完整包大小、SHA-256 完全相同，随后交给 Android 正常覆盖安装，
安装权限及系统确认仍按现有流程执行。这是下载增量，不是绕过安装的热更新。

构建器会实际应用每一份补丁，验证还原结果后才写入签名清单。默认基准为最近两个
正式版及最近一个本地候选版；需要手动指定基准时，可向 `publish_update.py` 重复传入
`--delta-from releases/<旧版本>` 覆盖默认选择（最多 8 个）。请保留正式发布过的原始包，不可重新打包
旧版本作为基准。没有对应补丁、补丁不比全包小、基准不符或还原失败时，下载完整包；
每次安装最多尝试一份补丁，然后回退一次完整包，不循环重试。

1.2.6 是首个具备增量能力的版本，老版本仍需手动安装；其后的双端升级可使用增量。
随 1.2.6 生成的 1.2.5 → 1.2.6 补丁用于本地还原验证，1.2.5 不会自动使用它。

比较新旧包的文件复用空间可执行：

```powershell
python tools/analyze_release_delta.py `
  --previous releases/<旧版本> --current releases/<新版本> `
  --output releases/<新版本>/incremental-analysis.json
```

该报告仅为文件变化分析，实际可安装的补丁由正式构建流程生成，文件名为 `*.delta.zip`。

## 更新运行资源

外部资源库不与编译流程建立依赖。更新资源时，把已提取的内容原样复制到
`assets_full/`，无需调整其中的中文目录或文件名，然后双击 `assets/sync_full.bat`，
或在项目根执行（需要 Python 3.10+，仅使用标准库）：

```powershell
python assets/sync_full.py
```

脚本通过固定映射读取 `团员/头像/`、`装备/图标/`、`装备/套装图标/`、能力/部位图标，
指定的主动任务界面图标，以及 `文本/本地化/loc_CHS_FINAL.json`，把运行所需的图片和官方中文本地化表同步到
`assets/` 是所有运行时图片资源的统一目录：角色头像、装备、界面图标、网页图标、
潜能图标和道具图标均位于这里。道具与装扮清单中的图标按完整 ID 补齐到 `assets/items/`，
名称、稀有度同步到 `backend/item_names.json` 和 `backend/item_rank.json`。
佣兵团援助清单由 `sd_GuildAidItem.txt`、物品 Tags 与中文本地化生成到 `assets/guild_aid.json`，随上述全量同步入口更新。
共用游戏配置 `assets/game_config.json` 也由此入口生成，仅提取已用到的公共参数和活动静态标志；
目前包含团战最低等级 `GuildWarLessLV` 和 `Version_GVG` 的不限时标志，活动实际排期读取登录快照。
此入口只补齐缺失图片，保留各类已有的压缩图片及全量库缺失的旧图；文本和索引会更新。
需要替换已有图片时，先移走对应目标文件再同步。
只更新物品图标、索引和本地化时可执行 `python tools/sync_assets.py --inventory-only`。
名称和物品索引在运行中会缓存，同步后需重启应用或测试后端。
月历与活动签到每次登录先读取快照中的签到时间，当天已签到则跳过请求；
活动 `SignInDay >= 7` 也直接跳过。其余活动按快照提交，由游戏服务器判断奖励条件。
不再生成或打包活动等级、排期规则表，新活动沿用现有七天签到接口时无需更新运行资源。
本次登录已成功或明确奖励领完的记录不再请求，失败按 5 分钟间隔只重试未完成项。
商品限购目录也由此入口通过 `tools/sync_commodity_catalog.py` 从
`文本/数据表/sd_Commodity.txt` 自动生成到 `backend/commodity_catalog.json`。`assets_full/`
的资源内容被 Git 忽略，只有筛选后的运行资源和精简数据表会进入
仓库和安装包。映射细节见 `assets_full/README.md`。

同步是独立的手动步骤；`build.bat`、`build-debug.bat` 和 `build.sh` 直接使用仓库中
已有的运行资源与商品目录，不读取 `assets_full/`。同步后的 `assets/`、
`assets/` 和 `backend/` 中相关 JSON 应一并提交，其他人即可直接编译。
没有 `assets_full/` 时，手动脚本也会提示跳过，不修改现有资源。

中间产物：

- Windows：`dist/LucimaTools/`（含 `LucimaTools.exe` + `_internal`）
- Android Release：`android/app/build/outputs/apk/release/app-release.apk`
- Android Debug：`android/app/build/outputs/apk/debug/app-debug.apk`

下面是各平台的详细说明与原理。

## 架构

- **服务器层用标准库 `http.server`**（`backend/server.py`），不依赖 FastAPI/uvicorn/pydantic，
  故 Windows(PyInstaller) 与 Android(Chaquopy) 可共用同一份服务器代码。
- **平台无关核心** `backend/appcore.py`：会话、状态摘要、各 API 端点处理。
- **网络配置** `backend/config.py`：网络统一跟随操作系统，代理通过系统设置或 VPN/TUN 接管。
- **登录跨平台共用** `backend/portal.py`：调用 EROLABS V2 JSON API 获取 Token，再以
  Android SDK profile 完成游戏握手。密码只存在于当次请求内存；Windows 使用 DPAPI、
  Android 使用 Keystore + AES-GCM 加密持久化 Token。重启后先显示登录页，用户选择保存的
  账号并点击登录，再恢复会话。

## Windows（双击直接启动的应用）

依赖：`pip install pywebview pyinstaller httpx`。

- **浏览器联调**：运行 `start.bat`，服务固定显示在 `LucimaTools Server` 命令窗中；关闭该窗口即结束测试服务。不要将 `python -m backend.server` 直接作为隐藏后台进程启动，以免联调结束后残留端口占用。
- **桌面壳联调**：`python -m desktop.run`
- **正式打包**：`build.bat win`
- **测试打包**：`build-debug.bat win`
- **产物**：`dist\LucimaTools\LucimaTools.exe`，双击运行，弹出独立原生窗口。
- **关闭窗口**：点击标题栏关闭按钮会显示主题化确认弹窗，勾选“最小化到托盘”后点击“确认”
  可让自动化继续在后台运行；托盘菜单可恢复窗口或退出程序。
- 网络统一跟随操作系统，使用代理时在系统侧配置。

## Android（Chaquopy 复用 Python）

依赖：Android Studio（JDK 17+）、Android SDK 与 NDK 26。构建时自动下载 Chaquopy
Python 3.12 运行时 + pip 装 httpx（走代理）。

- **构建前**：`android/app/build.gradle.kts` 的 Sync task 会自动把项目根的 `backend/`
  复制进 Chaquopy 源目录，把 `frontend/`+`assets/` 复制进 assets。**改了这些共享源后重新
  构建即自动同步**（单一真源在项目根，不在 android/ 里）。
- **正式签名**：由 `android/keystore.properties` 指向 `android/signing/lucima.keystore`；
  Release 构建缺失任一配置会直接失败，不会回退到 Debug 签名。
- **构建**（在 `android/` 目录）：
  ```
  export JAVA_HOME="<JDK 17 路径>"
  export ANDROID_HOME="<Android SDK 路径>"
  ./gradlew :app:assembleRelease   # 正式签名包
  ./gradlew :app:assembleDebug     # 本地测试包
  ```
- **产物**：位于 `android/app/build/outputs/apk/release/` 或 `debug/`，约 80MB，包含 Python 运行时。
- **运行**：需要代理时先在手机端启用系统 VPN/TUN，再登录应用。

## 待人工验证项

- **Windows**：双击 exe 后窗口是否正常显示；点击关闭按钮验证主题弹窗、托盘勾选、恢复和退出。
- **Android**：真机安装 APK → 配代理 → 登录 → 重启应用后从账号下拉框选择并点击登录，
  验证 Token 恢复 → 各日常任务。
  需真机 + 代理 App，无法在开发机自动验证。

## Android 正式签名迁移

`1.2.3` 使用旧 Android Debug 证书；首次使用 `lucima.keystore` 的版本无法直接覆盖安装
`1.2.3`。迁移版本必须通过人工渠道分发，并提示用户先导出装备过滤方案，再卸载旧版、
安装新版、重新登录和导入方案。卸载会删除账号令牌、任务设置、装备方案与本地日志。

更新清单的 RSA 签名和 Android APK 安装签名是两套独立机制。更换 APK 签名不需要修改
更新服务器协议，也不能更换现有的更新清单 RSA 私钥。迁移版不应设为强制更新；用户安装
采用 `lucima.keystore` 的版本后，后续版本即可恢复正常覆盖更新。
