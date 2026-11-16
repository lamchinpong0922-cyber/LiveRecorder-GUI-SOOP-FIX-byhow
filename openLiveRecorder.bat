
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

REM ===== 檢查必要套件 =====
echo [CHECK] 正在檢查必要套件...

"%PYTHON%" -c "import PIL, pystray, webview" >nul 2>&1

if errorlevel 1 (
    echo [INSTALL] 正在安裝必要套件...
    echo [INFO] 使用使用者安裝模式，並停用 pip 快取。
    echo.

    "%PYTHON%" -m pip install --user --no-cache-dir Pillow pystray pywebview

    if errorlevel 1 (
        echo.
        echo [ERROR] 套件安裝失敗。
        pause
        exit /b 1
    )
)

REM ===== 再次確認套件 =====
echo.
echo [CHECK] 確認套件載入...

"%PYTHON%" -c "import PIL, pystray, webview"

if errorlevel 1 (
    echo.
    echo [ERROR] 必要套件仍無法載入。
    pause
    exit /b 1
)

echo [OK] 必要套件全部正常。
echo.

REM ===== 檢查 React 桌面版 =====
if not exist "desktop-ui\dist\index.html" (
    echo [ERROR] 找不到 React 桌面版建置檔。
    pause
    exit /b 1
)

if not exist "launcher.py" (
    echo [ERROR] 找不到 launcher.py
    pause
    exit /b 1
)

REM ===== 啟動 React 桌面版 =====
echo ==========================================
echo [INFO] 正在啟動 React 桌面版...
echo ==========================================
echo.

"%PYTHON%" launcher.py --desktop

set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ==========================================
echo 程式結束，代碼：%EXIT_CODE%
echo ==========================================
pause
exit /b %EXIT_CODE%
