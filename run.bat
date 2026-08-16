@echo off
REM Double-clickable wrapper around run.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
