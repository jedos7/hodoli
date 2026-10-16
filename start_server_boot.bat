@echo off
rem Boot-time launcher with a watchdog. Started by the Startup-folder shortcut "Theme Radar (auto)".
rem - waits for the network, then runs the server. If the server exits, restarts it after 10s.
rem - if a server already runs (listening on 8000 OR a uvicorn process still starting up), waits instead of fighting.
rem - stop_server.bat creates data\autostart.stop which ends this loop.
title Stock Auto Program server (auto)
cd /d D:\theme_radar
if exist data\autostart.stop del data\autostart.stop
timeout /t 25 /nobreak >nul
:loop
if exist data\autostart.stop goto end
powershell -NoProfile -Command "if ((Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' }) -or (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { exit 1 } else { exit 0 }"
if %errorlevel%==1 (
  timeout /t 30 /nobreak >nul
  goto loop
)
echo [%date% %time%] autostart: launching server >> data\server.log
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> data\server.log 2>&1
echo [%date% %time%] autostart: server exited, restart in 10s >> data\server.log
timeout /t 10 /nobreak >nul
goto loop
:end
echo [%date% %time%] autostart: stopped by stop_server.bat >> data\server.log
