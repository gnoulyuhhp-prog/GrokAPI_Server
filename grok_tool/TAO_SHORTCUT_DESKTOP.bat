@echo off
:: Tao shortcut tren Desktop Windows de double-click cho nhanh
chcp 65001 >nul
cd /d "%~dp0"

set "TARGET=%~dp0CHAY_REG.bat"
set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%\OneDrive\Desktop"
if not exist "%DESKTOP%" (
    echo [LOI] Khong tim thay Desktop
    pause
    exit /b 1
)

set "LNK=%DESKTOP%\Grok Register.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%LNK%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 1; $s.Description = 'Grok Register 1 acc'; $s.Save(); Write-Host 'OK: %LNK%'"

echo.
echo  Da tao shortcut Desktop: Grok Register.lnk
echo  Chi can double-click shortcut do la chay.
echo.
pause
