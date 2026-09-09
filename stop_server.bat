@echo off
title Theme Radar stop
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[Theme Radar] stopped server PID ' + $_.ProcessId) } } else { Write-Output '[Theme Radar] no server was running.' }"
echo Press any key to close.
pause >nul
