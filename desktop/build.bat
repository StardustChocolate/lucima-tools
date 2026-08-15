@echo off
chcp 437 >nul
REM Build LucimaTools.exe (Windows desktop, onedir)
REM Run from anywhere; this cd's to project root (parent of desktop\).

cd /d "%~dp0.."

echo ============================================
echo   Building LucimaTools desktop app
echo ============================================
echo.

python -m PyInstaller desktop\LucimaTools.spec --noconfirm --clean

echo.
if exist "dist\LucimaTools\LucimaTools.exe" (
  echo   DONE: dist\LucimaTools\LucimaTools.exe
) else (
  echo   BUILD FAILED - see output above
)
echo.
pause
