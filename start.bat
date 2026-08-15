@echo off
chcp 437
cd /d "%~dp0"
title LucimaTools Server

echo ============================================
echo   LucimaTools - Local Server
echo ============================================
echo.
echo   Server:  http://127.0.0.1:8000
echo   (Keep this window open. Press Ctrl+C to stop.)
echo.

python -m backend.server

echo.
echo   Server stopped. Press any key to close.
pause
