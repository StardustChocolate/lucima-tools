#!/usr/bin/env bash
# LucimaTools 双端构建脚本 (git-bash 版)。
# 用法:
#   ./build.sh           构建 Windows exe + Android APK
#   ./build.sh win       仅 Windows 桌面 exe
#   ./build.sh android   仅 Android debug APK
# 可用环境变量覆盖: JAVA_HOME, ANDROID_HOME
set -u
cd "$(dirname "$0")"

TARGET="${1:-all}"

build_win() {
  echo
  echo "=== [Windows] 构建桌面 exe ==="
  taskkill //F //IM LucimaTools.exe >/dev/null 2>&1 || true
  rm -rf dist/LucimaTools 2>/dev/null || true
  sleep 1
  python -m PyInstaller desktop/LucimaTools.spec --noconfirm --clean
}

build_android() {
  echo
  echo "=== [Android] 构建 debug APK ==="
  (cd android && ./gradlew :app:assembleDebug --console=plain)
}

case "$TARGET" in
  win|windows) build_win ;;
  android)     build_android ;;
  all)         build_win; build_android ;;
  *) echo "未知目标 \"$TARGET\"，可用: win / android / all"; exit 1 ;;
esac

echo
echo "============================================"
echo "  构建完成"
[ -f dist/LucimaTools/LucimaTools.exe ] && echo "  Windows : dist/LucimaTools/  (含 exe + _internal，整套分发)"
[ -f android/app/build/outputs/apk/debug/app-debug.apk ] && echo "  Android : android/app/build/outputs/apk/debug/app-debug.apk"
echo "============================================"
