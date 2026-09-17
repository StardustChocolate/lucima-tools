@echo off
rem Always run the server in a separate visible console window.
if /i not "%~1"=="--server-console" (
  start "LucimaTools Server" "%ComSpec%" /k call "%~f0" --server-console
  exit /b
)

chcp 437
cd /d "%~dp0"
title LucimaTools Server
set "ARK_TEST_SERVER=1"

echo ============================================
echo   LucimaTools - Local Server
echo ============================================
echo.
echo   Server:  http://127.0.0.1:27843
echo   (Keep this window open. Press Ctrl+C to stop.)
echo.

python -m backend.server

echo.
echo   Server stopped. Press any key to close.
pause
