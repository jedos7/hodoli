@echo off
title Theme Radar server
cd /d D:\theme_radar
echo [Theme Radar] stopping any old server (even one still starting up)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn app.main:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /T /PID %%p >nul 2>&1
echo [Theme Radar] starting server. Closing this window stops the server. (stop: Ctrl+C)
echo [Theme Radar] the browser opens automatically when the server is ready (about 20s).
echo               if it does not open: http://127.0.0.1:8000
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 90;$i++){ try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1',8000); $c.Close(); break } catch { Start-Sleep 1 } }; Start-Process 'http://127.0.0.1:8000'"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo [Theme Radar] server stopped. Press any key to close.
pause >nul
