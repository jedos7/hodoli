@echo off
title Stock Auto Program stop
cd /d D:\theme_radar
rem tell the boot watchdog (start_server_boot.ps1) to stop looping, then kill the watchdog and the server
echo stop > data\autostart.stop
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*start_server_boot.ps1*') -or ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*start_server_boot.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[Stock Auto Program] stopped auto-start watchdog PID ' + $_.ProcessId) }"
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ('[Stock Auto Program] stopped server PID ' + $_.ProcessId) } } else { Write-Output '[Stock Auto Program] no server was running.' }"
echo [Stock Auto Program] auto-start stays installed; it runs again at next boot. To remove it, delete the shortcut in the Startup folder (shell:startup).
echo Press any key to close.
pause >nul
