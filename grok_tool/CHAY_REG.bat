@echo off
:: Double-click: mo UI menu (Python + mau/icon), khong can Grok CLI
chcp 65001 >nul
cd /d "%~dp0"
title Grok Register Tool

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

if not exist "venv\Scripts\python.exe" (
    echo [LOI] Chua co venv.
    pause
    exit /b 1
)

venv\Scripts\python.exe -u ui_menu.py
exit /b %ERRORLEVEL%
