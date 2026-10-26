@echo off
rem Instalacja G3 Together po polsku (Setup.cmd: to samo po angielsku, Einrichten.cmd: po niemiecku).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1" -Language pl %*
echo.
pause
