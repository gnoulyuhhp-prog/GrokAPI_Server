@echo off
:: Double-click: mo Web Control Plane (UI multi-tool)
chcp 65001 >nul
cd /d "%~dp0"
title Reg Control Plane

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set WEB_PORT=8787
set WEB_HOST=127.0.0.1

if not exist "venv\Scripts\python.exe" (
    echo [LOI] Chua co venv. Chay start.bat lan dau.
    pause
    exit /b 1
)

:: dam bao fastapi/uvicorn
venv\Scripts\python.exe -c "import fastapi,uvicorn" 2>nul
if errorlevel 1 (
    echo [..] Cai fastapi uvicorn...
    venv\Scripts\pip.exe install fastapi "uvicorn[standard]" -q
)

echo.
echo   ========================================
echo     REG CONTROL PLANE
echo     http://127.0.0.1:%WEB_PORT%/
echo   ========================================
echo   Giữ cửa sổ này MỞ. Đóng = tắt web.
echo.

venv\Scripts\python.exe -m web_console.app
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" (
    echo [LOI] Server dung bat thuong. Exit=%ERR%
    echo Neu port bi chiem, doi WEB_PORT trong file nay.
)
pause
exit /b %ERR%
