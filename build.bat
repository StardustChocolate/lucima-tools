@echo off
chcp 437 >nul
setlocal
cd /d "%~dp0"
title LucimaTools Release Builder

REM Release-only local settings. This file is ignored by Git.
if not exist "build.local.bat" goto missing_config
call "build.local.bat"
if errorlevel 1 goto failed

if not defined LUCIMA_UPDATE_URL goto missing_update_url
if not defined LUCIMA_UPDATE_BASE_URL goto missing_base_url

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=all"

if /i "%TARGET%"=="win"     goto do_win
if /i "%TARGET%"=="windows" goto do_win
if /i "%TARGET%"=="android" goto do_android
if /i "%TARGET%"=="all"     goto do_all
echo Unknown target "%TARGET%". Use: win / android / all
pause
exit /b 1

:do_all
call :win
if errorlevel 1 goto failed
call :android
if errorlevel 1 goto failed
call :package_all
if errorlevel 1 goto failed
goto summary

:do_win
call :win
if errorlevel 1 goto failed
call :package_win
if errorlevel 1 goto failed
goto summary

:do_android
call :android
if errorlevel 1 goto failed
call :package_android
if errorlevel 1 goto failed
goto summary

:win
echo.
echo === [Windows] Building release package ===
taskkill /F /IM LucimaTools.exe >nul 2>&1
if exist "dist\LucimaTools" rmdir /s /q "dist\LucimaTools"
python -m PyInstaller desktop\LucimaTools.spec --noconfirm --clean
exit /b %errorlevel%

:android
echo.
echo === [Android] Building signed release APK ===
if not exist "android\keystore.properties" (
  echo Missing android\keystore.properties.
  exit /b 1
)
if not exist "android\signing\lucima.keystore" (
  echo Missing android\signing\lucima.keystore.
  exit /b 1
)
if "%JAVA_HOME%"=="" if exist "%ProgramFiles%\Android\Android Studio\jbr" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
if "%ANDROID_HOME%"=="" if exist "%LOCALAPPDATA%\Android\Sdk" set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
pushd "%~dp0android"
call gradlew.bat :app:assembleRelease --console=plain
set "GRADLE_RESULT=%errorlevel%"
popd
exit /b %GRADLE_RESULT%

:package_all
python tools\package_release.py --clean --windows-dir dist\LucimaTools --android-apk android\app\build\outputs\apk\release\app-release.apk --update-url "%LUCIMA_UPDATE_URL%"
exit /b %errorlevel%

:package_win
python tools\package_release.py --windows-dir dist\LucimaTools --update-url "%LUCIMA_UPDATE_URL%"
exit /b %errorlevel%

:package_android
python tools\package_release.py --android-apk android\app\build\outputs\apk\release\app-release.apk --update-url "%LUCIMA_UPDATE_URL%"
exit /b %errorlevel%

:summary
call :manifest
if errorlevel 1 goto failed
echo.
echo ============================================
echo   Verified release build finished.
python -c "from backend.version import APP_VERSION; print(f'  Output: releases\\{APP_VERSION}\\')"
echo   Candidate manifest: releases\preview.json
echo   Manual promotion only: promote-release.bat
echo ============================================
pause
exit /b 0

:manifest
echo.
echo === [Update] Preparing signed manifest from verified packages ===
for /f "delims=" %%V in ('python -c "from backend.version import APP_VERSION; print(APP_VERSION)"') do set "RELEASE_VERSION=%%V"
python tools\publish_update.py --release-dir "releases\%RELEASE_VERSION%" --update-url "%LUCIMA_UPDATE_URL%" --base-url "%LUCIMA_UPDATE_BASE_URL%"
exit /b %errorlevel%

:missing_config
echo Missing build.local.bat. Copy build.local.bat.example and configure it locally.
goto failed

:missing_update_url
echo LUCIMA_UPDATE_URL is missing from build.local.bat.
goto failed

:missing_base_url
echo LUCIMA_UPDATE_BASE_URL is missing from build.local.bat.
goto failed

:failed
echo.
echo ============================================
echo   Release build failed. No package is ready to publish.
echo ============================================
pause
exit /b 1
