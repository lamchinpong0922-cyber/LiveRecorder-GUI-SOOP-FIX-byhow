@echo off
setlocal
title LiveRecorder Launcher
cd /d "%~dp0"

set "LOGDIR=O:\userssss\WIN11\TEMP"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "LOG=%LOGDIR%\LiveRecorder_Launch.log"

echo ========================================== > "%LOG%"
echo LiveRecorder startup >> "%LOG%"
echo DATE: %DATE% %TIME% >> "%LOG%"
echo ROOT: %CD% >> "%LOG%"
echo. >> "%LOG%"

set "PYTHON="
set "PYTHONW="

if exist ".venv\Scripts\python.exe" set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined PYTHON if exist "venv\Scripts\python.exe" set "PYTHON=%CD%\venv\Scripts\python.exe"

if not defined PYTHON (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON set "PYTHON=%%P"
)

if not defined PYTHON (
    echo ERROR: Python not found. >> "%LOG%"
    goto FAIL
)

for %%I in ("%PYTHON%") do set "PYTHONW=%%~dpnIw.exe"

if not exist "%PYTHONW%" (
    echo ERROR: pythonw.exe not found: %PYTHONW% >> "%LOG%"
    goto FAIL
)

echo PYTHON: %PYTHON% >> "%LOG%"
echo PYTHONW: %PYTHONW% >> "%LOG%"

if not exist "launcher.py" (
    echo ERROR: launcher.py not found. >> "%LOG%"
    goto FAIL
)

if not exist "desktop-ui\dist\index.html" (
    echo ERROR: desktop-ui\dist\index.html not found. >> "%LOG%"
    goto FAIL
)

"%PYTHON%" -c "import PIL, pystray, webview" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo Installing required Python packages... >> "%LOG%"
    "%PYTHON%" -m pip install Pillow pystray pywebview >> "%LOG%" 2>&1
    if errorlevel 1 goto FAIL
)

"%PYTHON%" -c "import PIL, pystray, webview" >> "%LOG%" 2>&1
if errorlevel 1 goto FAIL
echo Starting desktop application in background mode... >> "%LOG%"
REM === AUTO BUILD DESKTOP UI ===
echo.
echo [BUILD] Building desktop UI...
pushd "%~dp0desktop-ui"
call npm.cmd run build
if errorlevel 1 (
    echo.
    echo [ERROR] Frontend build failed. Application was NOT started.
    popd
    echo.
    echo Please check the build errors above.
    pause
    exit /b 1
)
popd
echo [BUILD] Frontend build completed successfully.
echo.
start "" "%PYTHONW%" launcher.py --desktop --minimize-after-5

if errorlevel 1 (
    echo ERROR: Failed to start desktop application. >> "%LOG%"
    goto FAIL
)

echo Background launch requested. >> "%LOG%"
echo Application should hide to the system tray after approximately 5 seconds.
echo CMD will close in 5 seconds.
timeout /t 5 /nobreak >nul
exit /b 0

:FAIL
echo.
echo STARTUP FAILED. Details:
type "%LOG%"
echo.
echo LOG: %LOG%
pause
exit /b 1