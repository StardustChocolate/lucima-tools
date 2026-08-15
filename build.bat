@echo off
chcp 437 >nul
cd /d "%~dp0"
title LucimaTools Builder

REM ============================================
REM  LucimaTools two-platform build script
REM  Usage (double-click = build all):
REM    build.bat           build Windows exe + Android APK
REM    build.bat win       Windows desktop exe only
REM    build.bat android   Android debug APK only
REM
REM  Override paths via env vars if your setup differs:
REM    JAVA_HOME, ANDROID_HOME
REM ============================================

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=all"

if /i "%TARGET%"=="win"     goto do_win
if /i "%TARGET%"=="windows" goto do_win
if /i "%TARGET%"=="android" goto do_android
if /i "%TARGET%"=="all"     goto do_all
echo Unknown target "%TARGET%".  Use: win / android / all
pause
exit /b 1

:do_all
call :win
call :android
goto summary

:do_win
call :win
goto summary

:do_android
call :android
goto summary

REM =============== Windows exe (PyInstaller) ===============
:win
echo.
echo === [Windows] Building desktop exe ===
taskkill /F /IM LucimaTools.exe >nul 2>&1
if exist "dist\LucimaTools" rmdir /s /q "dist\LucimaTools"
python -m PyInstaller desktop\LucimaTools.spec --noconfirm --clean
exit /b

REM =============== Android APK (Gradle + Chaquopy) ===============
:android
echo.
echo === [Android] Building debug APK ===
if "%JAVA_HOME%"=="" if exist "%ProgramFiles%\Android\Android Studio\jbr" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
if "%ANDROID_HOME%"=="" if exist "%LOCALAPPDATA%\Android\Sdk" set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
pushd "%~dp0android"
call gradlew.bat :app:assembleDebug --console=plain
popd
exit /b

:summary
echo.
echo ============================================
echo   Build finished.
if exist "dist\LucimaTools\LucimaTools.exe" echo   Windows : dist\LucimaTools\LucimaTools.exe  (folder: dist\LucimaTools)
if exist "android\app\build\outputs\apk\debug\app-debug.apk" echo   Android : android\app\build\outputs\apk\debug\app-debug.apk
echo ============================================
pause
