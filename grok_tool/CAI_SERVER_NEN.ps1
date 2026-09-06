# Cai web server chay nen + tu mo khi dang nhap Windows (khong can .bat)
# Chay:  chuot phai -> Run with PowerShell
#   hoac:  powershell -ExecutionPolicy Bypass -File CAI_SERVER_NEN.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[LOI] Thieu venv\Scripts\python.exe" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  === Grok Reg Web — server nen (luon chay) ===" -ForegroundColor Cyan
Write-Host "  URL: http://127.0.0.1:8787/" -ForegroundColor Green
Write-Host ""

# 1) Cai Task Scheduler @ logon + chay ngay
& $py -m web_console.daemon --install --host 127.0.0.1 --port 8787
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] schtasks that bai — van start daemon nen bang pythonw" -ForegroundColor Yellow
}

# 2) Dam bao process dang chay (neu task chua kip)
Start-Sleep -Seconds 2
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health" -TimeoutSec 3
    Write-Host "  Server ONLINE: $($h | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
    Write-Host "  Starting daemon in background..." -ForegroundColor Yellow
    $pyw = Join-Path $PSScriptRoot "venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pyw)) { $pyw = $py }
    Start-Process -FilePath $pyw -ArgumentList "-m","web_console.daemon","--loop","--host","127.0.0.1","--port","8787" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    Start-Sleep -Seconds 3
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health" -TimeoutSec 5
        Write-Host "  Server ONLINE" -ForegroundColor Green
    } catch {
        Write-Host "  [LOI] Van chua len: $_" -ForegroundColor Red
        Write-Host "  Xem log: data\web_daemon.log" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "  Tu gio chi can mo trinh duyet:  http://127.0.0.1:8787/" -ForegroundColor Cyan
Write-Host "  Log: data\web_daemon.log" -ForegroundColor DarkGray
Write-Host "  Tat autostart:  venv\Scripts\python.exe -m web_console.daemon --uninstall" -ForegroundColor DarkGray
Write-Host "  Kiem tra:       venv\Scripts\python.exe -m web_console.daemon --status" -ForegroundColor DarkGray
Write-Host ""
Read-Host "Enter de dong"
