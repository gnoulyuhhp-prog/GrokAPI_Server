@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Check secrets before GitHub push

if not exist "venv\Scripts\python.exe" (
    echo [LOI] Thieu venv
    pause
    exit /b 1
)

echo.
echo  === Quet secret truoc khi push GitHub ===
echo.

venv\Scripts\python.exe scripts\check_no_secrets.py
set E1=%ERRORLEVEL%

echo.
if exist ".git" (
    echo  === Quet file dang git add --staged ===
    venv\Scripts\python.exe scripts\check_no_secrets.py --staged
    set E2=%ERRORLEVEL%
) else (
    echo  [i] Chua git init — chi quet working tree.
    set E2=0
)

echo.
if not "%E1%"=="0" goto fail
if not "%E2%"=="0" goto fail
echo  OK — co the push (van nen xem git status).
echo.
pause
exit /b 0

:fail
echo  UNSAFE — dung push. Doc SAFE_GITHUB.md
echo.
pause
exit /b 1
