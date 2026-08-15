<div align="center">

# LucimaTools · 璐茜玛的深层幻影

<img src="./frontend/assets/app-icon.png" width="200" alt="LucimaTools">

✨原生、轻量、跨平台✨

简体中文 | [English](README.en.md)

![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat-square&logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4.svg?style=flat-square&logo=windows11&logoColor=white)
![Android](https://img.shields.io/badge/Android-7.0%2B-3DDC84.svg?style=flat-square&logo=android&logoColor=white)
[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg?style=flat-square)](LICENSE)
[![QQ Group](https://img.shields.io/badge/QQ%20Group-1032070842-5865F2.svg?style=flat-square)](https://qm.qq.com/q/YsvXane7Wq)

</div>

## 项目简介

LucimaTools · 璐茜玛的深层幻影 是一个致力于降低星陨玩家日常肝度的开源项目。我们利用了游戏的底层交互，构建了一套无需 Client 自动执行任务的 Headless 框架。无论你在用哪个平台，LucimaTools 都能提供开箱即用的自动任务，告别繁琐的日常，将更多时间用于研究配队、水群，或者陪伴重要的人~❤

## 功能

**自动任务**（按档期自动执行）
- 基地领取（动力转换 / 成长药剂 / 升星水晶 / 技能模块）
- 派遣任务
- 秘密商店刷新（免费）
- 每日免费招募
- 虚拟幻境
- 竞技场NPC

**主动任务**
- 一键领取邮件
- 秘密商店刷新（钻石）
- 讨伐 / 元素 / 活动关卡
- 商店购买

> 更多任务开发中~

## 快速开始

根据自身平台下载对应的release版本，开箱即用

### 开发者构建

项目使用 Python 3.12，Windows 桌面端还需要系统 Edge / WebView2；Android 构建需要
JDK 17、Android SDK 与 NDK 26。建议先在虚拟环境中安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

打包 Windows 版本前需额外安装 PyInstaller；根目录构建脚本可以分别或同时构建两端：

```powershell
python -m pip install pyinstaller==6.20.0
.\build.bat win       # Windows 桌面端
.\build.bat android   # Android 调试 APK
.\build.bat all       # 同时构建两端
```

构建产物位于：

- Windows：`dist/LucimaTools/`
- Android：`android/app/build/outputs/apk/debug/app-debug.apk`

Android 未配置签名时会使用 debug 签名；正式签名可从
`android/keystore.properties.example` 创建本地 `keystore.properties`。应用版本号只修改
`backend/version.py` 中的 `APP_VERSION`。更新外部资源后，执行
`python tools/sync_assets.py` 生成实际进入仓库和安装包的 `assets/`。

> Windows 重新打包会清理 `dist/LucimaTools/`，请先备份其中的 `settings.json` 等用户数据。

## 许可证

LucimaTools 的源代码依据 [GNU General Public License v3.0](LICENSE) 发布。
仓库中的游戏美术、角色头像、装备图标及其他第三方资源不在 GPL 授权范围内，
其版权归各自的原作者或权利人所有。

## 注意

- 本项目仅供**学习、研究和参考**用途，不构成任何形式的正式建议或承诺。
- 本软件按“**原样**”（AS IS）提供，不附带任何明示或暗示的担保，包括但不限于适销性、特定用途适用性及不侵权的担保。作者或版权持有人不对软件的使用或其他处理方式所产生的结果负责。
- 在任何情况下，即使事先被告知可能发生损害，作者或贡献者均不对因使用本软件或无法使用本软件而引起的任何索赔、损害或其他责任负责，无论是合同诉讼、侵权行为还是其他原因。
- 如果本项目中包含指向第三方网站、资源或代码的链接，这些内容仅供参考，作者不对其准确性、合法性或安全性承担任何责任。
- 用户应自行确保其使用本项目的行为符合所在国家/地区的法律法规。因使用本项目而产生的任何法律风险由用户自行承担。
- 若本项目无意中侵犯了任何个人或实体的权益，请通过 [Issue] 或邮件联系我们，我们将在核实后第一时间处理（如删除相关内容）。
