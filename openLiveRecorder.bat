
@echo off
chcp 65001 >nul
setlocal
title LiveRecorder GUI SOOP FIX

cd /d "%~dp0"

echo ==========================================
echo   LiveRecorder GUI SOOP FIX
echo ==========================================
echo.

REM ===== 選擇 Python 環境 =====
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] 找不到 Python。
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo [INFO] Python: %PYTHON%
echo.

REM ===== 檢查 Pillow =====
echo [CHECK] 正在檢查 Pillow...

"%PYTHON%" -c "import PIL" >nul 2>&1

if errorlevel 1 (
    echo [INSTALL] 缺少 Pillow，正在自動安裝...
    "%PYTHON%" -m pip install --upgrade Pillow

    if errorlevel 1 (
        echo.
        echo [ERROR] Pillow 安裝失敗。
        pause
        exit /b 1
    )
) else (
    echo [OK] Pillow 已安裝。
)

REM ===== 啟動 GUI =====
if not exist "gui.py" (
    echo [ERROR] 找不到 gui.py
    pause
    exit /b 1
)

echo.
echo ==========================================
echo [INFO] 正在啟動 GUI...
echo ==========================================
echo.

"%PYTHON%" gui.py

set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ==========================================
echo 程式結束，代碼：%EXIT_CODE%
echo ==========================================
pause
exit /b %EXIT_CODE%
