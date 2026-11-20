@echo off
setlocal
cd /d "D:\xiaozhi\server"

echo =======================================================
echo    XiaoZhi ESP32 ^<--^> LangMind Bridge Server
echo =======================================================
echo.

set "PY_CMD="

if exist "C:\Python313\python.exe" (
    set "PY_CMD=C:\Python313\python.exe"
    goto :found
)

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=py -3"
    goto :found
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=python"
    goto :found
)

echo [ERROR] Python not found. Please install Python 3.10 or higher.
pause
exit /b 1

:found
"%PY_CMD%" "D:\xiaozhi\server\server.py"
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server exited with error code %ERRORLEVEL%
)

pause
