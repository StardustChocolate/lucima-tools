@echo off
setlocal
cd /d "%~dp0"
python tools\publish_update.py --promote releases\preview.json
if errorlevel 1 goto failed
 echo Stable metadata is ready for distribution.
pause
exit /b 0
:failed
echo Promotion failed. No update was published.
pause
exit /b 1
