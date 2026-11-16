
@echo off
chcp 65001 >nul
setlocal
title 準備睡覺 - 暗屏測試

set "LOGDIR=O:\userssss\WIN11\TEMP"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\Monitor_Off_Test_LOG.txt"

echo ==========================================
echo       準備睡覺 - 暗屏功能測試
echo ==========================================
echo.
echo [INFO] 2 秒後關閉螢幕。
echo [INFO] 電腦仍會繼續運作。
echo [INFO] 移動滑鼠或按鍵盤即可喚醒。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class MonitorPower { [DllImport(\"user32.dll\")] public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam); }'; Write-Output 'MONITOR_OFF_TEST: START'; Write-Output '等待 2 秒...'; Start-Sleep -Seconds 2; [MonitorPower]::SendMessage([IntPtr]0xffff,0x0112,[IntPtr]0xF170,[IntPtr]2) | Out-Null; Write-Output 'MONITOR_OFF_COMMAND: SENT'; } catch { Write-Output ('ERROR: ' + $_.Exception.Message); exit 1 }" > "%LOG%" 2>&1

set "EXIT_CODE=%ERRORLEVEL%"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$raw=[IO.File]::ReadAllText('%LOG%'); $status=if ('%EXIT_CODE%' -eq '0') {'PASS'} else {'FAIL'}; $report=$raw + [Environment]::NewLine + 'BAT_EXIT_CODE: %EXIT_CODE%' + [Environment]::NewLine + 'CLIPBOARD_COPY: ' + $status + [Environment]::NewLine; Set-Clipboard -Value $report; if ((Get-Clipboard -Raw) -ceq $report) { [IO.File]::WriteAllText('%LOG%',$report,[Text.UTF8Encoding]::new($false)); Write-Output 'CLIPBOARD_COPY: PASS' } else { $report=$raw + [Environment]::NewLine + 'BAT_EXIT_CODE: %EXIT_CODE%' + [Environment]::NewLine + 'CLIPBOARD_COPY: FAIL' + [Environment]::NewLine; [IO.File]::WriteAllText('%LOG%',$report,[Text.UTF8Encoding]::new($false)); Write-Output 'CLIPBOARD_COPY: FAIL' }"

echo.
echo ==========================================
echo 測試指令已送出。
echo LOG: %LOG%
echo ==========================================
echo.
echo 喚醒螢幕後，按任意鍵結束。
pause >nul
exit /b %EXIT_CODE%
