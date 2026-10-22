@echo off
chcp 437 >nul
setlocal
cd /d "%~dp0"
title LucimaTools Debug Builder

REM Local test packages intentionally do not connect to the production updater.
set "LUCIMA_UPDATE_URL="

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
echo === [Windows] Building debug package ===
taskkill /F /IM LucimaTools.exe >nul 2>&1
if exist "dist\LucimaTools" rmdir /s /q "dist\LucimaTools"
python -m PyInstaller desktop\LucimaTools.spec --noconfirm --clean
exit /b %errorlevel%

:android
echo.
echo === [Android] Building debug APK ===
if "%JAVA_HOME%"=="" if exist "%ProgramFiles%\Android\Android Studio\jbr" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
if "%ANDROID_HOME%"=="" if exist "%LOCALAPPDATA%\Android\Sdk" set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
pushd "%~dp0android"
call gradlew.bat :app:assembleDebug --console=plain
set "GRADLE_RESULT=%errorlevel%"
popd
exit /b %GRADLE_RESULT%

:package_all
python tools\package_debug.py --clean --windows-dir dist\LucimaTools --android-apk android\app\build\outputs\apk\debug\app-debug.apk
exit /b %errorlevel%

:package_win
python tools\package_debug.py --windows-dir dist\LucimaTools
exit /b %errorlevel%

:package_android
python tools\package_debug.py --android-apk android\app\build\outputs\apk\debug\app-debug.apk
exit /b %errorlevel%

:summary
echo.
echo ============================================
echo   Debug build finished.
python -c "from backend.version import APP_VERSION; print(f'  Output: debug\\{APP_VERSION}\\')"
echo ============================================
pause
exit /b 0

:failed
echo.
echo ============================================
echo   Debug build failed.
echo ============================================
pause
exit /b 1
