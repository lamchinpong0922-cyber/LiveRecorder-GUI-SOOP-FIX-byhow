
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


class ShutdownScheduler:
    def __init__(
        self,
        state_file: Path,
        stop_recorder: Callable[[], tuple[bool, str]],
        get_process: Callable[[], object | None],
        stop_sentinel: Path,
    ) -> None:
        self.state_file = Path(state_file)
        self.stop_recorder = stop_recorder
        self.get_process = get_process
        self.stop_sentinel = Path(stop_sentinel)

        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.shutdown_requested = False

        self.state = {
            "enabled": False,
            "mode": "once",
            "time": "03:00",
            "nextRun": None,
            "phase": "idle",
            "remaining": 0,
            "message": "",
        }

        self._load()

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def _load(self) -> None:
        try:
            data = json.loads(
                self.state_file.read_text(encoding="utf-8")
            )

            if isinstance(data, dict):
                mode = data.get("mode", "once")
                value = data.get("time", "03:00")

                if mode in ("once", "daily"):
                    self.state["mode"] = mode

                try:
                    hour, minute = map(int, value.split(":"))
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        self.state["time"] = value
                except (ValueError, AttributeError):
                    pass

        except (OSError, ValueError, TypeError):
            pass

        # 程式重新啟動後不恢復監控。
        # 保留使用者上次選擇的模式及時間，但必須重新按設定排程。
        self.state.update({
            "enabled": False,
            "nextRun": None,
            "phase": "idle",
            "remaining": 0,
            "message": "排程尚未啟用，請重新設定",
        })

        self._save()

    @staticmethod
    def _next_daily(value: str, now: datetime) -> datetime:
        hour, minute = map(int, value.split(":"))

        target = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if target <= now:
            target += timedelta(days=1)

        return target

    @staticmethod
    def _hidden_run(command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )

    def get_state(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _stop_old_thread(self) -> None:
        with self.lock:
            old_event = self.cancel_event
            old_thread = self.thread
            should_abort_shutdown = self.shutdown_requested

            old_event.set()
            self.shutdown_requested = False

        if should_abort_shutdown and os.name == "nt":
            try:
                self._hidden_run(["shutdown.exe", "/a"])
            except OSError:
                pass

        if (
            old_thread is not None
            and old_thread.is_alive()
            and old_thread is not threading.current_thread()
        ):
            old_thread.join(timeout=10)

            if old_thread.is_alive():
                raise RuntimeError(
                    "上一個排程執行緒尚未停止，請稍後再試"
                )

    def set_schedule(self, mode: str, value: str) -> dict:
        if os.name != "nt":
            raise RuntimeError("定時關機目前只支援 Windows")

        if mode not in ("once", "daily"):
            raise ValueError("排程模式不正確")

        try:
            hour, minute = map(int, value.split(":"))

            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError

        except (ValueError, AttributeError):
            raise ValueError("請輸入有效的 24 小時制時間")

        self._stop_old_thread()

        with self.lock:
            now = datetime.now()
            target = self._next_daily(value, now)

            # 每次排程使用新的 Event，避免舊執行緒復活。
            self.cancel_event = threading.Event()
            event = self.cancel_event

            self.state = {
                "enabled": True,
                "mode": mode,
                "time": value,
                "nextRun": target.isoformat(timespec="seconds"),
                "phase": "scheduled",
                "remaining": 0,
                "message": "定時關機已啟用",
            }

            self._save()

            self.thread = threading.Thread(
                target=self._run,
                args=(event,),
                name="LiveRecorderShutdownScheduler",
                daemon=True,
            )

            self.thread.start()

            return dict(self.state)

    def cancel(self) -> dict:
        self._stop_old_thread()

        with self.lock:
            self.state.update({
                "enabled": False,
                "nextRun": None,
                "phase": "cancelled",
                "remaining": 0,
                "message": "定時關機已取消",
            })

            self._save()

            return dict(self.state)

    def stop(self) -> None:
        # 程式真正結束時停止排程。
        self.cancel()

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)
            self._save()

    def _wait(self, event: threading.Event, seconds: int) -> bool:
        for remaining in range(seconds, 0, -1):
            if event.wait(1):
                return False

            if remaining % 5 == 0 or remaining <= 5:
                self._update(remaining=remaining)

        return not event.is_set()

    def _run(self, event: threading.Event) -> None:
        while not event.is_set():
            with self.lock:
                enabled = self.state.get("enabled")
                mode = self.state.get("mode")
                next_run_text = self.state.get("nextRun")

            if not enabled or not next_run_text:
                return

            try:
                next_run = datetime.fromisoformat(next_run_text)
            except (TypeError, ValueError):
                self._update(
                    enabled=False,
                    phase="error",
                    message="排程時間資料錯誤",
                )
                return

            now = datetime.now()

            if now < next_run:
                event.wait(
                    min(1, max(0, (next_run - now).total_seconds()))
                )
                continue

            try:
                self._trigger(event)
            except Exception as exc:
                self._update(
                    phase="error",
                    message=f"排程執行發生錯誤：{exc}",
                )

            if event.is_set():
                return

            with self.lock:
                enabled = self.state.get("enabled")
                mode = self.state.get("mode")

            # 單次排程觸發後結束。
            if not enabled or mode != "daily":
                return

            # 每天模式若當天停止流程發生錯誤，
            # 保留排程，繼續等待下一個指定時間。


    def _trigger(self, event: threading.Event) -> None:
        with self.lock:
            if event.is_set():
                return

            mode = self.state.get("mode")
            value = self.state.get("time")

            if mode == "daily":
                next_run = self._next_daily(
                    value,
                    datetime.now() + timedelta(seconds=1),
                )

                self.state["nextRun"] = next_run.isoformat(
                    timespec="seconds"
                )
            else:
                self.state["enabled"] = False
                self.state["nextRun"] = None

            self.state.update({
                "phase": "stopping",
                "remaining": 0,
                "message": "時間已到，正在停止錄製及轉檔",
            })

            self._save()

        if event.is_set():
            return

        try:
            # 正常通知核心停止，不強制終止 FFmpeg。
            stop_ok, stop_message = self.stop_recorder()

            if not stop_ok:
                process = self.get_process()

                # 如果核心仍在執行，代表停止要求未成功送達。
                # 此時禁止繼續倒數關機。
                if process is not None and process.poll() is None:
                    self._update(
                        phase="error",
                        message=f"停止錄製失敗，取消本次關機：{stop_message}",
                    )
                    return

                # 核心已經結束或原本就沒有執行錄製，
                # 可以安全地繼續關機流程。

        except Exception as exc:
            self._update(
                phase="error",
                message=f"停止錄製失敗，取消本次關機：{exc}",
            )
            return

        # 等待核心真正退出。
        # 核心必須先完成 TS 收尾及 MP4 轉檔。
        process = self.get_process()

        if process is not None:
            while process.poll() is None:
                if event.wait(1):
                    return

        if event.is_set():
            return

        self._update(
            phase="countdown",
            remaining=300,
            message="錄製及轉檔已完成，5 分鐘後關機",
        )

        if not self._wait(event, 300):
            return

        self._update(
            phase="shutdown_retry",
            remaining=0,
            message="正在執行 Windows 關機",
        )

        while not event.is_set():
            try:
                result = self._hidden_run([
                    "shutdown.exe",
                    "/s",
                    "/t",
                    "0",
                ])

                if result.returncode == 0:
                    with self.lock:
                        self.shutdown_requested = True

                    self._update(
                        message="已送出關機指令，若未關機將於 120 秒後重試",
                    )
                else:
                    self._update(
                        message=(
                            f"關機指令失敗，120 秒後重試"
                            f"（代碼 {result.returncode}）"
                        ),
                    )

            except OSError as exc:
                self._update(
                    message=f"關機指令異常：{exc}，120 秒後重試",
                )

            if not self._wait(event, 120):
                return
