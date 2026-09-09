@echo off
chcp 65001 >nul
title 테마 레이더 서버
cd /d D:\theme_radar
echo [테마 레이더] 8000번 포트에 남아 있는 서버가 있으면 정리합니다...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /T /PID %%p >nul 2>&1
echo [테마 레이더] 서버를 시작합니다. 이 창을 닫으면 서버가 꺼집니다. (끄기: Ctrl+C)
echo [테마 레이더] 잠시 뒤 브라우저가 자동으로 열립니다. 안 열리면 http://127.0.0.1:8000
start "" /min cmd /c "timeout /t 8 >nul && start "" http://127.0.0.1:8000"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
