@echo off
chcp 65001 >nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /T /PID %%p >nul 2>&1 && echo [테마 레이더] 서버(PID %%p)를 껐습니다.
echo 끝. 아무 키나 누르면 닫힙니다.
pause >nul
