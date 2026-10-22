@echo off
setlocal
python "%~dp0sync_full.py"
set "SYNC_RESULT=%errorlevel%"
echo.
pause
exit /b %SYNC_RESULT%
