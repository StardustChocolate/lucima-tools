<div align="center">

# LucimaTools · Lucima's Deep Phantom

<img src="./frontend/assets/app-icon.png" width="200" alt="LucimaTools">

✨ Native, lightweight, cross-platform ✨

[简体中文](README.md) | English

![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat-square&logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4.svg?style=flat-square&logo=windows11&logoColor=white)
![Android](https://img.shields.io/badge/Android-7.0%2B-3DDC84.svg?style=flat-square&logo=android&logoColor=white)
[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg?style=flat-square)](LICENSE)
[![QQ Group](https://img.shields.io/badge/QQ%20Group-1032070842-5865F2.svg?style=flat-square)](https://qm.qq.com/q/YsvXane7Wq)

</div>

## About

LucimaTools is an open-source project built to reduce the daily grind for *Ark Re:Code* players. It uses the game's lower-level interactions to provide a headless framework that can execute tasks without running the game client. On either supported platform, LucimaTools offers ready-to-use automation so you can spend less time on repetitive chores and more time on team building, chatting with the community, or being with the people who matter.

## Features

**Automated tasks** (run on schedule)
- Base rewards (power conversion / growth potions / star-up crystals / skill modules)
- Dispatch missions
- Free Secret Shop refreshes
- Daily free recruitment
- Virtual Illusion
- Arena NPC battles

**Manual tasks**
- Claim all mail
- Secret Shop refreshes using diamonds
- Hunt / element / event stages
- Store purchases

> More tasks are in development.

## Quick Start

Download the release for your platform and run it directly.

### Developer Build

The project uses Python 3.12. The Windows desktop build also requires the system Edge / WebView2 runtime; Android builds require JDK 17, the Android SDK, and NDK 26. Installing dependencies in a virtual environment is recommended:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install PyInstaller before packaging the Windows build. The scripts in the repository root can build either platform or both:

```powershell
python -m pip install pyinstaller==6.20.0
.\build.bat win       # Windows desktop app
.\build.bat android   # Android debug APK
.\build.bat all       # Build both platforms
```

Build outputs:

- Windows: `dist/LucimaTools/`
- Android: `android/app/build/outputs/apk/debug/app-debug.apk`

Android falls back to debug signing when no signing configuration is present. For a release signature, create a local `keystore.properties` from `android/keystore.properties.example`. Change the application version only through `APP_VERSION` in `backend/version.py`. After updating the external resource archive, run `python tools/sync_assets.py` to regenerate the `assets/` directory included in the repository and application packages.

> Rebuilding the Windows package clears `dist/LucimaTools/`. Back up `settings.json` and other user data stored there first.

## License

LucimaTools source code is released under the [GNU General Public License v3.0](LICENSE).
In-game artwork, character portraits, equipment icons, and other third-party resources in this repository are not licensed under the GPL; their copyrights remain with their respective authors and rights holders.

## Notice

- This project is provided solely for **learning, research, and reference**. It does not constitute formal advice, representation, or warranty of any kind.
- This software is provided **as is**, without any express or implied warranty, including warranties of merchantability, fitness for a particular purpose, and non-infringement. The authors and copyright holders are not responsible for results arising from use or other handling of the software.
- Under no circumstances shall the authors or contributors be liable for claims, damages, or other liability arising from use of, or inability to use, this software, whether in contract, tort, or otherwise, even if advised of the possibility of such damage.
- Links to third-party websites, resources, or code are provided for reference only. The authors do not warrant their accuracy, legality, or security.
- Users are responsible for ensuring that their use complies with the laws and regulations of their country or region. Any legal risk arising from use is borne by the user.
- If this project unintentionally infringes the rights of any person or entity, contact us through an Issue or by email. We will investigate and take appropriate action, including removal where necessary.
