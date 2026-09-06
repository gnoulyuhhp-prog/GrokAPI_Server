@echo off
cd /d "%~dp0"
title Kill old Grok Register processes

echo [*] Stopping old python (main.py / probe_after_otp.py)...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -and ( $_.CommandLine -match 'grok_tool' -or $_.CommandLine -match 'main\.py' -or $_.CommandLine -match 'probe_after_otp' ) } | ForEach-Object { Write-Host ('  kill python PID=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [*] Stopping automation Chrome (remote-debugging + grok_tool profile)...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -and $_.CommandLine -match 'remote-debugging-port' -and ($_.CommandLine -match 'Temp' -or $_.CommandLine -match 'grok_tool' -or $_.CommandLine -match 'chrome_profile') } | ForEach-Object { Write-Host ('  kill chrome PID=' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [+] Done.
timeout /t 2 >nul
