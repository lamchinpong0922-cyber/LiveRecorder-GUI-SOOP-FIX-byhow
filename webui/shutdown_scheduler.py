
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
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.state.update(data)
        except (OSError, ValueError, TypeError):
            pass

        if self.state.get("enabled"):
            try:
                next_run = datetime.fromisoformat(self.state["nextRun"])
            except (TypeError, ValueError):
                next_run = None

            now = datetime.now()

            # 開機或程式啟動時，錯過的單次排程不補執行。
            if next_run is None or next_run <= now:
                if self.state.get("mode") == "daily":
                    self.state["nextRun"] = self._next_daily(
                        self.state.get("time", "03:00"), now
                    ).isoformat(timespec="seconds")
                else:
                    self.state.update({
                        "enabled": False,
                        "phase": "missed",
                        "nextRun": None,
                        "message": "單次排程已錯過，不會補執行",
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

    def get_state(self) -> dict:
        with self.lock:
            return dict(self.state)

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

        self.cancel()

        with self.lock:
            now = datetime.now()
            target = self._next_daily(value, now)

            self.cancel_event.clear()
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
                name="LiveRecorderShutdownScheduler",
                daemon=True,
            )
            self.thread.start()

            return dict(self.state)

    def cancel(self) -> dict:
        self.cancel_event.set()

        if os.name == "nt":
            try:
                subprocess.run(
                    ["shutdown.exe", "/a"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            except OSError:
                pass

        with self.lock:
            self.state.update({
                "enabled": False,
                "phase": "cancelled",
                "remaining": 0,
                "nextRun": None,
                "message": "定時關機已取消",
            })
            self._save()

        return self.get_state()

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)
            self._save()

    def _wait(self, seconds: int) -> bool:
        for remaining in range(seconds, 0, -1):
            if self.cancel_event.wait(1):
                return False
            if remaining % 5 == 0 or remaining <= 5:
                self._update(remaining=remaining)
        return not self.cancel_event.is_set()

    def _run(self) -> None:
        while not self.cancel_event.is_set():
            with self.lock:
                enabled = self.state.get("enabled")
                next_run_text = self.state.get("nextRun")

            if not enabled or not next_run_text:
                if self.cancel_event.wait(1):
                    return
                continue

            try:
                next_run = datetime.fromisoformat(next_run_text)
            except (TypeError, ValueError):
                self.cancel()
                return

            now = datetime.now()

            if now < next_run:
                self.cancel_event.wait(min(1, max(0, (next_run - now).total_seconds())))
                continue

            self._trigger()
            return

    def _trigger(self) -> None:
        with self.lock:
            mode = self.state.get("mode")
            value = self.state.get("time")

            if mode == "daily":
                self.state["nextRun"] = self._next_daily(
                    value, datetime.now() + timedelta(seconds=1)
                ).isoformat(timespec="seconds")
            else:
                self.state["enabled"] = False
                self.state["nextRun"] = None

            self.state.update({
                "phase": "stopping",
                "remaining": 0,
                "message": "時間已到，正在停止錄製及轉檔",
            })
            self._save()

        if self.cancel_event.is_set():
            return

        # 通知錄製核心正常停止，不強制終止 FFmpeg。
        try:
            self.stop_recorder()
        except Exception as exc:
            self._update(
                phase="error",
                message=f"停止錄製失敗：{exc}",
            )
            return

        # 等待核心真正退出，確保 TS 收尾及轉檔完成。
        process = self.get_process()
        if process is not None:
            while process.poll() is None:
                if self.cancel_event.wait(1):
                    return

        if self.cancel_event.is_set():
            return

        self._update(
            phase="countdown",
            remaining=300,
            message="錄製及轉檔已完成，5 分鐘後關機",
        )

        if not self._wait(300):
            return

        self._update(
            phase="shutdown_retry",
            remaining=0,
            message="正在執行 Windows 關機",
        )

        while not self.cancel_event.is_set():
            try:
                result = subprocess.run(
                    ["shutdown.exe", "/s", "/t", "0"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )

                if result.returncode != 0:
                    self._update(
                        message=f"關機指令失敗，120 秒後重試（代碼 {result.returncode}）",
                    )
                else:
                    self._update(
                        message="已送出關機指令，若未關機將於 120 秒後重試",
                    )

            except OSError as exc:
                self._update(message=f"關機指令異常：{exc}，120 秒後重試")

            if not self._wait(120):
                return
