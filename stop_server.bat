@echo off
title Theme Radar stop
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do (
  taskkill /F /T /PID %%p >nul 2>&1
  echo [Theme Radar] stopped server PID %%p
  set FOUND=1
)
if "%FOUND%"=="0" echo [Theme Radar] no server was running.
echo Press any key to close.
pause >nul
