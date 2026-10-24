@echo off
rem G3 Together setup in English (Einrichten.cmd: the same in German).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1" -Language en %*
echo.
pause
