@echo off
chcp 65001 >nul
cd /d "%~dp0"
title GROK REGISTER + SUB2API VPS CONTROL CENTER

set "PROJECT_DIR=%~dp0grok_tool"

:: 1. Kiem tra Python
where python >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python! Vui long cai Python 3.10+
    pause
    exit /b 1
)

:: 2. Khoi tao venv neu chua co
if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    echo ======================================================
    echo   [KHOI TAO LAN DAU] Dang tao moi truong ao Python venv...
    echo ======================================================
    python -m venv "%PROJECT_DIR%\venv"
    if errorlevel 1 (
        echo [LOI] Tao venv that bai!
        pause
        exit /b 1
    )
    echo [..] Dang cai dat thu vien...
    "%PROJECT_DIR%\venv\Scripts\pip.exe" install -r "%PROJECT_DIR%\requirements.txt"
    echo.
)

:menu
cls
echo ======================================================================
echo    ⚡ GROK REGISTER SUITE - HE THONG TU DONG HOA VA SUB2API VPS
echo ======================================================================
echo    🌐 Sub2API VPS URL: https://grokapi.vorte.me (Chay ngam 24/7)
echo ======================================================================
echo.
echo    [1] 🚀 Chay Giao Dien Web Control Plane (http://127.0.0.1:8787)
echo    [2] ⚡ Chay Menu Reg Tai Khoan (CLI)
echo    [3] 🛡️ Khoi Dong Camoufox Solver (:5072) (Giai Captcha Ngam)
echo    [4] 🔍 Kiem Tra Lien Ket: Tool Reg <---> Sub2API VPS
echo.
echo    [0] 🚪 Thoat
echo ======================================================================
set /p opt="👉 Nhap lua chon cua ban (0-4): "

if "%opt%"=="1" goto run_web
if "%opt%"=="2" goto run_cli
if "%opt%"=="3" goto run_solver
if "%opt%"=="4" goto test_link
if "%opt%"=="0" exit /b 0

echo Lua chon khong hop le!
timeout /t 2 >nul
goto menu

:run_web
cd /d "%PROJECT_DIR%"
call CHAY_WEB.bat
goto menu

:run_cli
cd /d "%PROJECT_DIR%"
call CHAY_REG.bat
goto menu

:run_solver
cd /d "%PROJECT_DIR%"
call CHAY_SOLVER.bat
goto menu

:test_link
cd /d "%PROJECT_DIR%"
venv\Scripts\python.exe test_sub2api_link.py
echo.
pause
goto menu
