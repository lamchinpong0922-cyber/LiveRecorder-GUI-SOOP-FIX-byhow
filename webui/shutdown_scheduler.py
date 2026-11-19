
from __future__ import annotations

import json
import threading
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

        self.exit_program: Callable[[], None] | None = None

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
            data = json.loads(
                self.state_file.read_text(encoding="utf-8")
            )

            if isinstance(data, dict):
                mode = data.get("mode", "once")
                value = data.get("time", "03:00")
                enabled = data.get("enabled") is True

                if mode in ("once", "daily"):
                    self.state["mode"] = mode

                try:
                    hour, minute = map(int, value.split(":"))

                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        self.state["time"] = value
                except (ValueError, AttributeError):
                    pass

                if mode == "daily" and enabled:
                    self.state["enabled"] = True

        except (OSError, ValueError, TypeError):
            pass

        if self.state["enabled"]:
            target = self._next_daily(
                self.state["time"],
                datetime.now(),
            )

            self.state.update({
                "nextRun": target.isoformat(timespec="seconds"),
                "phase": "scheduled",
                "remaining": 0,
                "message": "每日定時關閉已恢復",
            })
        else:
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

    def get_state(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _stop_old_thread(self) -> None:
        with self.lock:
            old_event = self.cancel_event
            old_thread = self.thread

            old_event.set()

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

            self.cancel_event = threading.Event()
            event = self.cancel_event

            self.state = {
                "enabled": True,
                "mode": mode,
                "time": value,
                "nextRun": target.isoformat(timespec="seconds"),
                "phase": "scheduled",
                "remaining": 0,
                "message": "定時關閉程式已啟用",
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
                "message": "定時關閉程式已取消",
            })

            self._save()

            return dict(self.state)

    def stop(self) -> None:
        self.cancel()

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)
            self._save()

    def _run(self, event: threading.Event) -> None:
        while not event.is_set():
            with self.lock:
                enabled = self.state.get("enabled")
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

            self._trigger(event)
            return

    def _trigger(self, event: threading.Event) -> None:
        with self.lock:
            if event.is_set():
                return

            mode = self.state.get("mode", "once")

            self.state.update({
                "enabled": mode == "daily",
                "nextRun": None,
                "phase": "stopping",
                "remaining": 0,
                "message": "時間已到，正在停止錄製並關閉程式",
            })

            self._save()

        if event.is_set():
            return

        try:
            stop_ok, stop_message = self.stop_recorder()

            if not stop_ok:
                process = self.get_process()

                if process is not None and process.poll() is None:
                    self._update(
                        phase="error",
                        message=f"停止錄製失敗：{stop_message}",
                    )
                    return

        except Exception as exc:
            self._update(
                phase="error",
                message=f"停止錄製失敗：{exc}",
            )
            return

        process = self.get_process()

        if process is not None:
            while process.poll() is None:
                if event.wait(1):
                    return

        if event.is_set():
            return

        self._update(
            phase="completed",
            remaining=0,
            message="錄製核心已結束，正在關閉 LiveRecorder",
        )

        callback = self.exit_program

        if callback is not None and not event.is_set():
            callback()
