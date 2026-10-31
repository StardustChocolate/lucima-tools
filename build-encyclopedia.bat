@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
python -B tools\build_encyclopedia_release.py %*
exit /b %errorlevel%
