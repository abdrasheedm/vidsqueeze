@echo off
REM Double-clickable wrapper around setup.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
