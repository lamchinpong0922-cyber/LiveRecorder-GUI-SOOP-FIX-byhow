
@echo off
chcp 65001 >nul
setlocal

title KoboldCpp - Qwen3-8B

set "MODEL=C:\ai-talk\Models\Downloaded_Models\Qwen_Qwen3-8B-Q4_K_M.gguf"
set "PORT=5001"
set "CONTEXT=8192"
set "LOGDIR=O:\userssss\WIN11\TEMP\KoboldCpp"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\KoboldCpp_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.log"

echo ==========================================
echo       KoboldCpp Qwen3 啟動器
echo ==========================================
echo.

if not exist "%MODEL%" (
    echo [ERROR] 找不到 Qwen3 模型：
    echo %MODEL%
    echo.
    pause
    exit /b 1
)

set "KOBOLD="

for %%P in (
    "C:\KoboldCpp\koboldcpp.exe"
    "C:\Program Files\KoboldCpp\koboldcpp.exe"
    "C:\ai-talk\koboldcpp.exe"
    "O:\userssss\WIN11\DOWNLOAD\koboldcpp.exe"
    "%USERPROFILE%\Downloads\koboldcpp.exe"
) do (
    if exist "%%~P" set "KOBOLD=%%~P"
)

if not defined KOBOLD (
    for /f "delims=" %%P in ('where koboldcpp.exe 2^>nul') do (
        if not defined KOBOLD set "KOBOLD=%%P"
    )
)

if not defined KOBOLD (
    echo [SEARCH] 正在專案及下載資料夾搜尋...
    for %%D in ("C:\ai-talk" "C:\KoboldCpp" "O:\userssss\WIN11\DOWNLOAD") do (
        if exist "%%~D" (
            for /f "delims=" %%P in ('where /r "%%~D" koboldcpp.exe 2^>nul') do (
                if not defined KOBOLD set "KOBOLD=%%P"
            )
        )
    )
)

if not defined KOBOLD (
    echo [ERROR] 找不到 koboldcpp.exe
    echo 請確認 KoboldCpp 是否已下載。
    pause
    exit /b 1
)

echo [FOUND] %KOBOLD%
echo [MODEL] %MODEL%
echo [PORT] %PORT%
echo [CONTEXT] %CONTEXT%
echo.
echo [START] 正在啟動...
echo LOG: %LOG%
echo.

echo START_TIME: %DATE% %TIME% > "%LOG%"
echo EXECUTABLE: %KOBOLD% >> "%LOG%"
echo MODEL: %MODEL% >> "%LOG%"
echo PORT: %PORT% >> "%LOG%"
echo CONTEXT: %CONTEXT% >> "%LOG%"
echo. >> "%LOG%"

"%KOBOLD%" --model "%MODEL%" --port %PORT% --contextsize %CONTEXT% --gpulayers 99 >> "%LOG%" 2>&1

set "EXITCODE=%ERRORLEVEL%"
echo. >> "%LOG%"
echo EXIT_CODE: %EXITCODE% >> "%LOG%"
echo END_TIME: %DATE% %TIME% >> "%LOG%"

echo.
echo KoboldCpp 已結束，代碼：%EXITCODE%
echo 完整 LOG：%LOG%
pause
