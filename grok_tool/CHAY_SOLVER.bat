@echo off
:: Local Camoufox Turnstile Solver (from grok-register-web engine)
chcp 65001 >nul
cd /d "%~dp0"
title Turnstile Solver :5072

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

if not exist "venv\Scripts\python.exe" (
    echo [LOI] Thieu venv
    pause
    exit /b 1
)

echo.
echo   ========================================
echo     TURNSTILE SOLVER  (Camoufox)
echo     http://127.0.0.1:5072
echo   ========================================
echo.

venv\Scripts\python.exe -c "import camoufox" 2>nul
if errorlevel 1 (
    echo [..] Cai camoufox + quart + patchright...
    venv\Scripts\pip.exe install "camoufox[geoip]" quart patchright rich -q
    echo [..] camoufox fetch browser...
    venv\Scripts\python.exe -m camoufox fetch
)

if not exist "services\turnstile_solver\start.py" (
    echo [LOI] Thieu services\turnstile_solver — copy tu zip grok-register-web
    pause
    exit /b 1
)

echo   Solver HEADLESS — khong mo cua so Chrome (khong cuop man hinh).
echo   Giu cua so CMD nay MO khi reg.
echo.
cd services\turnstile_solver
..\..\venv\Scripts\python.exe start.py --browser_type camoufox --thread 1 --port 5072 --host 127.0.0.1
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" echo [LOI] Solver exit=%ERR%
pause
exit /b %ERR%
