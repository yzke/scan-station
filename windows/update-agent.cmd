@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-agent.ps1"
set SCAN_STATION_UPDATE_RESULT=%ERRORLEVEL%
echo.
pause
exit /b %SCAN_STATION_UPDATE_RESULT%
