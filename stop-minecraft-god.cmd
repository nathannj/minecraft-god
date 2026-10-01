@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-minecraft-god.ps1" %*
set "STOP_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%STOP_EXIT_CODE%"=="0" (
    echo Shutdown reported an error. Review the message above.
) else (
    echo It is safe to close this window.
)
pause
exit /b %STOP_EXIT_CODE%
