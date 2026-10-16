@echo off
title Theme Radar stop
cd /d D:\theme_radar
rem tell the boot watchdog (start_server_boot.bat) to stop looping, then kill the server
echo stop > data\autostart.stop
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[Theme Radar] stopped server PID ' + $_.ProcessId) } } else { Write-Output '[Theme Radar] no server was running.' }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*start_server_boot.bat*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[Theme Radar] stopped auto-start watchdog PID ' + $_.ProcessId) }"
echo [Theme Radar] auto-start stays installed; it runs again at next boot. To remove it, delete the shortcut in the Startup folder.
echo Press any key to close.
pause >nul
