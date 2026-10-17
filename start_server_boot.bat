@echo off
rem Boot-time launcher. Started by the Startup-folder shortcut. The watchdog loop itself is start_server_boot.ps1
rem (PowerShell, so a killed child never leaves the window stuck on "Terminate batch job (Y/N)?").
title Stock Auto Program server (auto)
cd /d D:\theme_radar
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\theme_radar\start_server_boot.ps1"
