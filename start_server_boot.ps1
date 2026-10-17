# 부팅 감시 루프. start_server_boot.bat 이 이 파일을 실행한다 (시작프로그램 바로가기 → bat → 이 스크립트).
# - 25초 기다린 뒤(네트워크) 서버를 띄우고, 서버가 끝나면 10초 뒤 다시 띄운다.
# - 이미 서버가 있으면(포트 8000 LISTEN 또는 uvicorn 프로세스) 싸우지 않고 30초 뒤 다시 본다.
# - data\autostart.stop 파일이 생기면 루프를 끝낸다 (stop_server.bat 이 만든다).
# cmd 배치와 달리 자식이 강제 종료돼도 "배치 작업을 종료할까요(Y/N)?" 에 걸리지 않는다.
$ErrorActionPreference = 'Continue'
$root = 'D:\theme_radar'
Set-Location $root
$log = Join-Path $root 'data\server.log'
$stop = Join-Path $root 'data\autostart.stop'
if (Test-Path $stop) { Remove-Item $stop -Force }
function Note($text) { Add-Content -Path $log -Value ("[{0}] autostart: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text) }
Start-Sleep -Seconds 25
while ($true) {
    if (Test-Path $stop) { Note 'stopped by stop_server.bat'; break }
    $running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' }
    $listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($running -or $listening) { Start-Sleep -Seconds 30; continue }
    Note 'launching server'
    # cmd /c 로 감싸 서버 출력을 data\server.log 에 이어 붙인다 (Start-Process 의 리다이렉트는 덮어쓰기만 된다)
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', '".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> data\server.log 2>&1' `
        -WorkingDirectory $root -NoNewWindow -PassThru
    $p.WaitForExit()
    Note ("server exited (code {0}), restart in 10s" -f $p.ExitCode)
    Start-Sleep -Seconds 10
}
