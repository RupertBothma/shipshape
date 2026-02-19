@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\cleanup.ps1" %*
set EXIT_CODE=%ERRORLEVEL%

endlocal & exit /b %EXIT_CODE%
