@echo off
rem G3 Together einrichten, auf Deutsch (Setup.cmd: dasselbe auf Englisch).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1" -Language de %*
echo.
pause
